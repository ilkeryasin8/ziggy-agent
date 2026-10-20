"""Ticket dependencies and stacked slices: a ticket waits for the tickets it starts after and starts on its own
when the last one is done; cycles and other targets' tickets are refused; a stuck dependency is in the inbox. A
large plan split at the gate becomes stacked slice tickets with the right bases and seeded plans; publishing a slice
publishes the ones below first with stacked PR bases; a merged lower PR moves the next one to the base, and a squash
merge marks it for a restack that rebases it and pushes only when asked."""
import json
import time

import pytest
from conftest import calls, cli, gh_calls, git, script
from test_dashboard import act, panel, request, row  # noqa: F401 - the panel fixture

from ziggy import attention, cleanup, config, stack, state, ticket

PLAN3 = ("## Goal & acceptance\n\nWeekly plan export.\n\n- AC-1: export works\n- AC-2: import works\n\nRisk: medium · Size: large\n\n"
         "## Evidence\n\n- app.py:1\n\n"
         "## Plan\n\n### S1: Model (app.py · core · low)\n- [ ] T001 [S1] add the model (app.py)\n\n"
         "### S2: Export\n- [ ] T002 [S2] write the exporter (export.py)\n\n"
         "### S3: Import\n- [ ] T003 [S3] write the importer (import.py)\n\n"
         "## Verification\n\n- [ ] V001 [AC-1] run the tests\n\n## Questions\n\nNone\n\n"
         "## Decisions (user)\n\n### Note\n\n> keep the CSV format\n")


def target():
    return config.load_target("demo")


def st(ticket_id):
    return state.load_state(target(), ticket_id)


def wait_until(check, timeout=120, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def done(*ids):
    return lambda: all(st(i)["status"] == "done" and not state.running(target(), i) for i in ids)


def stages_of(env, ticket_id):
    return [c["stage"] for c in calls(env) if c.get("stage") and c["env"]["PIPELINE_TICKET"] == ticket_id]


# --- dependencies --------------------------------------------------------------------------------------

def test_a_ticket_waits_for_its_dependency_and_starts_when_it_is_done(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "{ticket}.txt"}]})
    assert cli("new", "chore: first").returncode == 0
    proc = cli("new", "chore: second", "--after", "local-1", "--run")
    assert proc.returncode == 0, proc.stderr
    assert "waiting for local-1" in proc.stderr
    s = st("local-2")
    assert s["status"] == "waiting" and s["waiting_for"] == ["local-1"] and s["next"]["stage"] == "plan"
    assert stages_of(env, "local-2") == [] and s["worktree"] is None
    assert state.load_ticket(target(), "local-2")["after"] == ["local-1"]
    assert "- Starts after: local-1" in (env["state"] / "runs" / "local-2" / "ticket.md").read_text()
    assert "starts on its own" in cli("status", "local-2").stdout
    assert attention.needs(target(), "local-2") is None  # waiting on a ticket that is fine needs nobody

    assert cli("run", "local-1").returncode == 0
    wait_until(done("local-2"), what="local-2 to start and finish after local-1")
    assert stages_of(env, "local-2") == ["plan", "implement", "review"]
    jobs = list((env["state"] / "runs" / "local-2" / "jobs").iterdir())
    assert [j.name.endswith("-run") for j in jobs] == [True]  # started as a panel job, by the engine
    assert "waiting_for" not in st("local-2")


def test_a_failed_dependency_leaves_the_ticket_waiting_with_a_note(env):
    script(env, {"plan": [{"verdict": "FAILED", "summary": "cannot plan"}]})
    cli("new", "chore: first")
    cli("new", "chore: second", "--after", "local-1")
    assert cli("run", "local-2").returncode == 0
    assert cli("run", "local-1").returncode == 1
    assert st("local-2")["status"] == "waiting"
    item = attention.needs(target(), "local-2")
    assert item["kind"] == "blocked" and item["title"] == "Bekliyor ama local-1 başarısız oldu" and item["dismissable"]
    assert item["waiting_for"][0]["id"] == "local-1"
    assert stack.wake(target()) == []  # nothing starts while a dependency is stuck


def test_the_panel_tick_starts_a_ticket_whose_dependency_got_done_meanwhile(env):
    cli("new", "chore: first")
    cli("new", "chore: second", "--after", "local-1")
    assert cli("run", "local-2").returncode == 0
    s = st("local-1")
    s["status"] = "done"  # finished while nothing was there to start local-2 (say the panel was down)
    state.save_state(target(), "local-1", s)
    assert stack.wake(target()) == ["local-2"]
    assert stack.wake(target()) == []  # its job is running: not started twice
    wait_until(done("local-2"), what="local-2")


def test_cycles_other_targets_and_unknown_tickets_are_refused(env, tmp_path):
    other = tmp_path / "other"
    git("init", "-q", "-b", "main", str(other), cwd=tmp_path)
    env["config"].write_text(env["config"].read_text() + f'[targets.other]\npath = "{other}"\n')
    ticket.create(config.load_target("other"), "elsewhere", "chore", "", False, "low",
                  origin={"name": "youtrack", "ref": "PROJ-7", "url": None})
    cli("new", "chore: first")
    assert cli("new", "chore: second", "--after", "local-1").returncode == 0
    out = cli("after", "local-1", "local-2")
    assert out.returncode == 2 and "dependency cycle: local-1 → local-2 → local-1" in out.stderr
    assert "dependency cycle: local-1 → local-1" in cli("after", "local-1", "local-1").stderr
    out = cli("new", "chore: third", "--after", "proj-7")
    assert out.returncode == 2 and "proj-7 is a ticket of other, not demo" in out.stderr
    assert "a bare id" in cli("new", "chore: third", "--after", "other:local-1").stderr
    assert "no ticket local-9 in demo" in cli("new", "chore: third", "--after", "local-9").stderr
    assert not (env["state"] / "runs" / "local-3").exists()
    assert cli("after", "local-2").stdout.strip() == "local-2: no dependencies"
    assert cli("after", "local-1", "local-2").returncode == 0  # no cycle any more
    assert state.load_ticket(target(), "local-1")["after"] == ["local-2"]


# --- splitting at the gate -------------------------------------------------------------------------------

def gate_with_slices(env):
    script(env, {"plan": [{"verdict": "READY", "risk": "medium", "size": "large", "_plan": PLAN3}],
                 "verify": [{"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": "{ticket}.txt"}]})
    assert cli("new", "feat: weekly plan export").returncode == 0
    assert cli("run", "local-1").returncode == 3
    assert st("local-1")["status"] == "awaiting-approval"


def split_and_finish(env):
    gate_with_slices(env)
    proc = cli("split", "local-1")
    assert proc.returncode == 0, proc.stderr
    wait_until(done("local-2", "local-3", "local-4"), what="the three slices")


def test_the_gate_offers_a_split_only_for_a_large_or_risky_plan_with_slices(env):
    gate_with_slices(env)
    assert stack.split_offer(target(), "local-1") == [{"id": "S1", "title": "Model"}, {"id": "S2", "title": "Export"},
                                                      {"id": "S3", "title": "Import"}]
    assert "ziggy split local-1" in cli("status", "local-1").stdout
    s = st("local-1")
    s["size"] = "medium"
    state.save_state(target(), "local-1", s)
    assert stack.split_offer(target(), "local-1") is None
    out = cli("split", "local-1")
    assert out.returncode == 2 and "needs a plan with at least 2" in out.stderr


def test_split_creates_stacked_slices_that_run_in_order(env):
    gate_with_slices(env)
    parent = state.load_ticket(target(), "local-1")
    proc = cli("split", "local-1", "--no-run")
    assert proc.returncode == 0, proc.stderr
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    assert [k["title"] for k in kids] == ["weekly plan export — S1: Model", "weekly plan export — S2: Export",
                                          "weekly plan export — S3: Import"]
    assert [k["branch"] for k in kids] == [f"{parent['branch']}-s1", f"{parent['branch']}-s2", f"{parent['branch']}-s3"]
    assert [k["base_branch"] for k in kids] == ["main", kids[0]["branch"], kids[1]["branch"]]
    assert [k["stack_base"] for k in kids] == [None, "local-2", "local-3"]
    assert [k["after"] for k in kids] == [[], ["local-2"], ["local-3"]]
    assert {(k["parent"], k["pipeline"]) for k in kids} == {("local-1", "slice")}
    assert [k["slice"] for k in kids] == ["S1", "S2", "S3"]
    s = st("local-1")
    assert s["status"] == "split" and s["children"] == ["local-2", "local-3", "local-4"] and s["gate"] is None
    assert s["approvals"][-1]["split"] == ["local-2", "local-3", "local-4"]
    assert s["worktree"] is None and not (env["state"] / "worktrees" / "local-1").exists()
    seeded = (env["state"] / "runs" / "local-3" / "inbox" / "plan.md").read_text()
    assert "## Goal & acceptance\n\nWeekly plan export." in seeded and "- AC-2: import works" in seeded
    assert "### S2: Export\n- [ ] T002 [S2] write the exporter" in seeded
    assert "T001" not in seeded and "T003" not in seeded
    assert "- S1: Model (local-2, below this one" in seeded and "- S3: Import (local-4, above this one" in seeded
    assert seeded.rstrip().endswith("## Decisions (user)\n\n### Note\n\n> keep the CSV format")
    assert st("local-3")["tasks"] == {"done": 0, "total": 1} and st("local-3")["size"] == "large"
    md = (env["state"] / "runs" / "local-3" / "ticket.md").read_text()
    assert f"- Base: {kids[0]['branch']} (local branch of local-2, the slice below; stacked)" in md
    assert "- Slice: S2 of local-1" in md
    assert st("local-2")["status"] == "new" and st("local-3")["status"] == st("local-4")["status"] == "waiting"
    assert st("local-4")["waiting_for"] == ["local-3"] and st("local-4")["next"]["stage"] == "implement"

    assert cli("run", "local-2").returncode == 0
    wait_until(done("local-3", "local-4"), what="the slices above")
    for k in ("local-2", "local-3", "local-4"):
        assert stages_of(env, k) == ["implement", "review"]  # no plan of their own
    repo = env["repo"]
    for lower, upper in ((kids[0], kids[1]), (kids[1], kids[2])):
        assert git("merge-base", "--is-ancestor", lower["branch"], upper["branch"], cwd=repo) == ""
    assert git("rev-list", "--count", f"{kids[1]['branch']}..{kids[2]['branch']}", cwd=repo).strip() == "1"
    view = stack.view(target(), parent, st("local-1"))
    # Every slice done, PRs not merged yet: no agent work is left, the owner reviews; the board lands it.
    assert [c["status"] for c in view["children"]] == ["done"] * 3 and not view["split_open"] and view["split_landing"]
    assert stack.dependency(target(), "local-1")["state"] == "done"  # the container is done when its slices are


def test_publishing_a_slice_publishes_the_ones_below_first_with_stacked_bases(env):
    split_and_finish(env)
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    proc = cli("publish", "local-4")
    assert proc.returncode == 0, proc.stderr
    created = [c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"]]
    heads = [c["argv"][c["argv"].index("--head") + 1] for c in created]
    bases = [c["argv"][c["argv"].index("--base") + 1] for c in created]
    assert heads == [k["branch"] for k in kids]
    assert bases == ["main", kids[0]["branch"], kids[1]["branch"]]
    assert all("--draft" in c["argv"] for c in created)
    assert [st(i)["pr"].rsplit("/", 1)[1] for i in ("local-2", "local-3", "local-4")] == ["1", "2", "3"]
    top = created[2]["body"]
    assert "## Stack" in top and "Part 3 of 3" in top
    assert "- ↓ S1: weekly plan export — S1: Model — https://github.example.test/acme/demo/pull/1" in top
    assert "- → S3: weekly plan export — S3: Import — this PR" in top
    bottom = created[0]["body"]
    assert f"- ↑ S2: weekly plan export — S2: Export — `{kids[1]['branch']}` (no PR yet)" in bottom
    for k in kids:
        assert git("rev-parse", f"refs/heads/{k['branch']}", cwd=env["origin"]) == git("rev-parse", k["branch"], cwd=env["repo"])
    assert cli("publish", "local-4").returncode == 0  # published already: reused, nothing below pushed again
    assert len([c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"]]) == 3


def test_a_slice_below_that_is_not_done_stops_the_publish(env):
    split_and_finish(env)
    s = st("local-2")
    s["status"] = "failed"
    state.save_state(target(), "local-2", s)
    out = cli("publish", "local-3")
    assert out.returncode == 2 and "stacked on local-2, which is failed" in out.stderr
    assert not [c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"]]


def _merge(env, branch, squash):
    repo = env["repo"]
    git("fetch", "-q", "origin", cwd=repo)
    if squash:
        git("merge", "-q", "--squash", f"origin/{branch}", cwd=repo)
        git("commit", "-q", "-m", "squashed S1", cwd=repo)
    else:
        git("merge", "-q", "--no-ff", "-m", "merge S1", f"origin/{branch}", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)


def test_a_merged_lower_pr_moves_the_next_one_to_the_base(env, monkeypatch):
    split_and_finish(env)
    assert cli("publish", "local-4").returncode == 0
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    _merge(env, kids[0]["branch"], squash=False)
    monkeypatch.setenv("FAKE_GH_PR_STATES", json.dumps({"1": "MERGED", "2": "OPEN", "3": "OPEN"}))
    out = cleanup.sync(target())
    assert {"id": "local-3", "retargeted": "main", "restack": False} in out
    patches = [c["argv"] for c in gh_calls(env) if c["argv"][:3] == ["api", "--method", "PATCH"]]
    assert patches == [["api", "--method", "PATCH", "repos/acme/demo/pulls/2", "-f", "base=main", "--silent"]]
    moved = state.load_ticket(target(), "local-3")
    assert moved["base_branch"] == "main" and moved["stack_base"] is None
    assert "- Base: main\n" in (env["state"] / "runs" / "local-3" / "ticket.md").read_text()
    assert st("local-3")["retargeted"]["from"] == kids[0]["branch"] and "restack" not in st("local-3")
    assert state.load_ticket(target(), "local-4")["stack_base"] == "local-3"  # the one above stays on its slice
    assert st("local-2")["archived"]["pr_state"] == "MERGED"  # moved first, then cleaned as before
    assert kids[0]["branch"] not in git("branch", "--list", cwd=env["repo"])
    assert cleanup.sync(target()) == []  # nothing moves twice


def test_a_stack_merged_all_at_once_is_archived_without_moving_merged_prs(env, monkeypatch):
    # The owner merged every slice before a sync ran (a five-slice stack): GitHub refuses a merged PR's base
    # change (422), so a merged slice above is skipped, not retried forever, and every slice gets archived.
    split_and_finish(env)
    assert cli("publish", "local-4").returncode == 0
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    _merge(env, kids[2]["branch"], squash=False)  # the top holds every slice's commits
    monkeypatch.setenv("FAKE_GH_PR_STATES", json.dumps({"1": "MERGED", "2": "MERGED", "3": "MERGED"}))
    out = cleanup.sync(target())
    assert not [o for o in out if o.get("kept")], out
    # A slice above whose state was not recorded yet is tried once; GitHub's 422 is checked and it is skipped.
    assert {o["id"] for o in out if o.get("skipped")} >= {"local-3"}
    for kid in ("local-2", "local-3", "local-4"):
        assert st(kid)["archived"]["pr_state"] == "MERGED", kid
    assert cleanup.sync(target()) == []


def test_a_squash_merge_marks_the_next_slice_for_a_restack_pushed_only_on_request(env, monkeypatch):
    split_and_finish(env)
    assert cli("publish", "local-4").returncode == 0
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    _merge(env, kids[0]["branch"], squash=True)
    monkeypatch.setenv("FAKE_GH_PR_STATES", json.dumps({"1": "MERGED", "2": "OPEN", "3": "OPEN"}))
    out = cleanup.sync(target())
    assert {"id": "local-3", "retargeted": "main", "restack": True} in out
    info = st("local-3")["restack"]
    assert info["onto"] == "origin/main" and info["reason"] == "squash" and info["below"] == "local-2"
    item = attention.needs(target(), "local-3")
    assert item["kind"] == "restack" and item["title"] == "Yeniden düzenleme gerekiyor"
    published = st("local-3")["published_head"]

    proc = cli("restack", "local-3")
    assert proc.returncode == 0, proc.stderr
    assert "local-3: rebased onto origin/main" in proc.stdout and "restack local-3 --push" in proc.stdout
    wt = st("local-3")["worktree"]
    assert git("rev-list", "--count", "origin/main..HEAD", cwd=wt).strip() == "1"  # only its own commit now
    assert git("rev-parse", "HEAD~1", cwd=wt) == git("rev-parse", "origin/main", cwd=wt)
    assert git("rev-parse", f"refs/heads/{kids[1]['branch']}", cwd=env["origin"]).strip() == published  # not pushed yet
    assert st("local-3")["restack"]["rebased"] and attention.needs(target(), "local-3")["title"] == "Yeniden düzenlenen dal push bekliyor"
    assert st("local-4")["restack"]["onto"] == f"refs/heads/{kids[1]['branch']}"  # the slice above follows

    proc = cli("restack", "local-3", "--push")
    assert proc.returncode == 0, proc.stderr
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    assert git("rev-parse", f"refs/heads/{kids[1]['branch']}", cwd=env["origin"]).strip() == head
    s = st("local-3")
    assert "restack" not in s and s["restacked"]["pushed_at"] and s["published_head"] == head

    proc = cli("restack", "local-4")
    assert proc.returncode == 0, proc.stderr
    wt4 = st("local-4")["worktree"]
    assert git("rev-parse", "HEAD~1", cwd=wt4).strip() == head and git("rev-list", "--count", "origin/main..HEAD", cwd=wt4).strip() == "2"


def test_a_restack_that_conflicts_leaves_the_branch_as_it_was(env, monkeypatch):
    split_and_finish(env)
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3", "local-4")]
    wt = st("local-3")["worktree"]
    before = git("rev-parse", "HEAD", cwd=wt)
    repo = env["repo"]
    (repo / "local-3.txt").write_text("main wrote this first\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "conflicting", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    s = st("local-3")
    s["restack"] = {"onto": "origin/main", "upstream": git("rev-parse", kids[0]["branch"], cwd=repo).strip(), "reason": "squash",
                    "below": "local-2", "at": state.now()}
    state.save_state(target(), "local-3", s)
    out = cli("restack", "local-3")
    assert out.returncode == 2 and "stopped on conflicts in local-3.txt" in out.stderr
    assert git("rev-parse", "HEAD", cwd=wt) == before and git("status", "--porcelain", cwd=wt) == ""
    assert st("local-3")["restack"]["result"]["conflicts"] == ["local-3.txt"]


# --- the panel -------------------------------------------------------------------------------------------

def test_the_panel_splits_at_the_gate_and_shows_the_stack(env, panel):  # noqa: F811
    gate_with_slices(env)
    r = row(panel, "local-1")
    assert [s["id"] for s in r["split_offer"]] == ["S1", "S2", "S3"] and r["needs_you"]["kind"] == "approval"
    code, out, _ = act(panel, "local-1", "split")
    assert code == 200, out
    assert out["split"] == ["local-2", "local-3", "local-4"] and out["job"]["command"] == "run local-2"
    r = row(panel, "local-1")
    assert r["status"] == "split" and r["split_open"] and [c["slice"] for c in r["children"]] == ["S1", "S2", "S3"]
    waiting = row(panel, "local-4")
    assert waiting["status"] == "waiting" and [d["id"] for d in waiting["waiting_for"]] == ["local-3"]
    assert waiting["parent"] == {"id": "local-1", "title": "weekly plan export"} and waiting["stack_base"] == "local-3"
    _, snap, _ = request(panel, "GET", "/api/status")
    assert {"local-1", "local-4"} <= {e["id"] for e in snap["board"]["waiting_on_others"]}
    assert act(panel, "local-1", "split")[0] == 409
    wait_until(done("local-2", "local-3", "local-4"), what="the slices started from the panel")

    code, out, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: after the stack", "after": ["local-4"]})
    assert code == 200 and out["ticket"]["after"] == ["local-4"]
    code, out, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: x", "after": ["local-99"]})
    assert code == 400 and "no ticket local-99" in out["error"]


# --- with the other loops: intent gaps, the done hooks --------------------------------------------------

GAP = {"summary": "It answers a different intent.", "evidence": "plan vs code",
       "questions": [{"id": "Q1", "text": "Which way?", "options": ["a", "b"], "recommendation": "a"}]}
CONVERGED = {"verdict": "READY", "route": "converged", "converged": True}


def test_an_intent_gap_in_a_slice_resets_to_its_stacked_base_and_keeps_a_branch_built_on(env):
    script(env, {"plan": [{"verdict": "READY", "risk": "medium", "size": "large", "_plan": PLAN3}],
                 "implement": [{"verdict": "READY", "_commit": "{ticket}.txt"}],
                 "review": [{"verdict": "NEEDS_USER", "intentGap": GAP}, CONVERGED, {"verdict": "NEEDS_USER", "intentGap": GAP}]})
    assert cli("new", "feat: weekly plan export").returncode == 0
    assert cli("run", "local-1").returncode == 3
    assert cli("split", "local-1", "--no-run").returncode == 0
    kids = [state.load_ticket(target(), i) for i in ("local-2", "local-3")]
    main_tip = git("rev-parse", "origin/main", cwd=env["repo"]).strip()
    assert cli("run", "local-2").returncode == 3
    gap = st("local-2")["rounds"][-1]["intent_gap"]
    assert (gap["reset"], gap["reset_to"], gap["kept"]) == (True, main_tip, None)  # the first slice's base: the target's
    assert st("local-2")["next"]["stage"] == "implement"  # slice.toml's [intent_gap] then
    assert cli("answer", "local-2", "--text", "Q1: a").returncode == 0
    wait_until(lambda: st("local-3")["status"] == "needs-user" and not state.running(target(), "local-3"), what="local-3's gap")
    below = git("rev-parse", kids[0]["branch"], cwd=env["repo"]).strip()
    gap = st("local-3")["rounds"][-1]["intent_gap"]
    assert (gap["reset"], gap["reset_to"]) == (True, below)  # an upper slice resets to the branch below, not main
    assert git("rev-parse", kids[1]["branch"], cwd=env["repo"]).strip() == below

    s = st("local-2")  # change course on the lower slice now that local-3 is built on its branch
    s.update(status="failed", next={"stage": "implement", "via": None, "resume": None})
    state.save_state(target(), "local-2", s)
    assert cli("run", "local-2").returncode == 3
    gap = st("local-2")["rounds"][-1]["intent_gap"]
    assert (gap["reset"], gap["kept"]) == (False, "stacked")
    assert git("rev-parse", kids[0]["branch"], cwd=env["repo"]).strip() == gap["head"] != below


def test_a_done_run_pushes_its_github_round_then_wakes_dependants_then_starts_the_retro(env, monkeypatch):
    from ziggy import pipeline, retro
    script(env, {"implement": [{"verdict": "READY", "_commit": "{ticket}-{ticket}.txt"}], "review": [CONVERGED]})
    cli("new", "chore: first")
    cli("new", "chore: second", "--after", "local-1")
    assert cli("run", "local-1").returncode == 0
    order = []
    monkeypatch.setattr(pipeline, "_github_round_done", lambda *a: order.append("github"))
    monkeypatch.setattr(stack, "wake", lambda t: order.append("wake") or [])
    monkeypatch.setattr(retro, "schedule", lambda t, i: order.append("retro"))
    pipeline.request_changes(target(), "local-1", "## Review (github, round 1)\n\n- [ ] T009 fix it\n",
                             heading="Review (github, round 1)", comments=1, source="github")
    assert st("local-1")["status"] == "done" and order == ["github", "wake", "retro"]
