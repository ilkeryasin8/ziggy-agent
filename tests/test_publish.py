"""publish: push of the ticket branch and a draft PR, with the guards that keep it the only push, the
PR body built from the ticket's plan, review and checks, and the user's edited title and body."""
import json
import re
from pathlib import Path

import pytest
from conftest import ROOT, cli, gh_calls, git

from ziggy import config, publish, state


def target():
    return config.load_target("demo")


def done_ticket(env, title="Sync pymongo pins", kind="chore", files=("change.txt",)):
    """A done ticket with a worktree whose branch has one commit over the base, made without a
    run: publishing does not care how the commits got there."""
    ticket_id = cli("new", title, "--type", kind).stdout.split("\t")[0]
    t = state.load_ticket(target(), ticket_id)
    wt = state.worktree_dir(target(), ticket_id)
    git("fetch", "-q", "origin", cwd=env["repo"])
    git("worktree", "add", "-q", "--no-track", "-b", t["branch"], str(wt), "origin/main", cwd=env["repo"])
    for rel in files:
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(f"{rel}\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", f"{kind}: {title}", cwd=wt)
    st = state.load_state(target(), ticket_id)
    st.update(status="done", worktree=str(wt))
    state.save_state(target(), ticket_id, st)
    return ticket_id


def state_of(env, ticket_id):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def created(env):
    return [c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"]]


def updates(env):
    """PR updates: REST PATCHes of the open PR (never `gh pr edit`, whose GraphQL query fails)."""
    calls = gh_calls(env)
    assert not any(c["argv"][:2] == ["pr", "edit"] for c in calls)
    return [c for c in calls if c["argv"][:3] == ["api", "--method", "PATCH"]]


def test_publish_pushes_branch_and_opens_draft_pr(env):
    ticket_id = done_ticket(env)
    proc = cli("publish", ticket_id)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "https://github.example.test/acme/demo/pull/1"

    wt = Path(state_of(env, ticket_id)["worktree"])
    assert git("rev-parse", "refs/heads/chore/sync-pymongo-pins", cwd=env["origin"]) == git("rev-parse", "HEAD", cwd=wt)
    assert git("rev-parse", "refs/heads/main", cwd=env["origin"]) == git("rev-parse", "origin/main", cwd=wt)

    [create] = created(env)
    argv = create["argv"]
    assert "--draft" in argv
    assert argv[argv.index("--repo") + 1] == "acme/demo"
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == "chore/sync-pymongo-pins"
    assert argv[argv.index("--title") + 1] == "chore: Sync pymongo pins"
    assert create["gh_token"] == "fake-token-for-tester"
    assert create["body"].strip() == f"`ziggy ticket {ticket_id}`"  # no plan, review or checks yet
    assert state_of(env, ticket_id)["pr"] == "https://github.example.test/acme/demo/pull/1"


def test_pr_body_carries_plan_decisions_review_deferred_and_checks(env):
    ticket_id = done_ticket(env)
    run = env["state"] / "runs" / ticket_id
    (run / "inbox").mkdir(exist_ok=True)
    (run / "inbox" / "plan.md").write_text(
        "# Plan\n\n## Goal & acceptance\n\nPin pymongo.\n\n- AC-1 the pin is 4.x\n\n## Plan\n\n- [x] T001 pin it\n\n"
        "## Decisions (user)\n\n### 2026-10-14 — tester\n\nKeep 4.6.\n")
    (run / "inbox" / "deferred.md").write_text("# Deferred\n\n## F3 Upgrade motor too\n\nLater.\n")
    for n, verdict in (("03-review", "NEEDS_USER"), ("05-review", "READY")):
        (run / n).mkdir()
        (run / n / "run.json").write_text(json.dumps({"stage": "review", "verdict": verdict, "result": {
            "verdict": verdict, "summary": f"{n} summary", "converged": verdict == "READY",
            "findings": [{"id": "F1", "disposition": "fix"}, {"id": "F3", "disposition": "defer"}, {"id": "F4", "disposition": "defer"}]}}))
    st = state_of(env, ticket_id)
    st["checks"] = [{"name": "tests", "status": "pass", "detail": "12 passed", "source": "engine"}]
    state.save_state(target(), ticket_id, st)

    assert cli("publish", ticket_id).returncode == 0
    body = created(env)[0]["body"]
    assert "## Acceptance criteria\n\n- AC-1 the pin is 4.x" in body and "Pin pymongo." not in body and "T001" not in body
    assert "## Decisions\n\n### 2026-10-14 — tester\n\nKeep 4.6." in body
    assert "**READY** · converged · 2 defer, 1 fix" in body and "05-review summary" in body and "03-review" not in body
    assert "## Deferred\n\n- F3 Upgrade motor too" in body
    assert "- tests: pass (12 passed)" in body
    assert body.rstrip().endswith(f"`ziggy ticket {ticket_id}`")


def test_pr_body_lists_the_findings_the_review_dismissed_collapsed(env):
    ticket_id = done_ticket(env)
    run = env["state"] / "runs" / ticket_id
    reviews = {"03-review": [{"id": "R1-1", "disposition": "dismiss", "summary": "escape </details> missing",
                              "reason": "input constrained"},
                             {"id": "R1-2", "disposition": "dismiss", "summary": "later reported again"}],
               "05-review": [{"id": "R1-2", "disposition": "fix", "summary": "real after all"},
                             {"id": "R2-1", "disposition": "dismiss", "summary": "unreachable\npath"}]}
    for n, findings in reviews.items():
        (run / n).mkdir()
        (run / n / "run.json").write_text(json.dumps({"stage": "review", "result": {"verdict": "READY", "findings": findings}}))

    assert cli("publish", ticket_id).returncode == 0
    body = created(env)[0]["body"]
    assert ("<details>\n<summary>Review'un elediği bulgular (2)</summary>\n\n"
            "- `R1-1` escape &lt;/details&gt; missing — input constrained\n"
            "- `R2-1` unreachable path\n\n</details>") in body
    assert "later reported again" not in body


def test_project_ticket_pr_is_a_docs_pr_with_the_doc_summary(env):
    doc = ("# demo\n\n## Product\n\nA wardrobe planner.\n\n## Out of scope\n\nShops.\n\n"
           "## Open questions\n\n- Q1 iOS or web?\n")
    ticket_id = done_ticket(env, "Wardrobe planner", kind="project", files=("docs/PROJECT.md",))
    wt = Path(state_of(env, ticket_id)["worktree"])
    (wt / "docs/PROJECT.md").write_text(doc)
    git("commit", "-q", "-am", "docs: project doc — Wardrobe planner", cwd=wt)
    plan = env["state"] / "runs" / ticket_id / "inbox" / "plan.md"
    plan.parent.mkdir(exist_ok=True)
    plan.write_text("## Decisions (user)\n\nWeb first.\n")

    assert cli("publish", ticket_id).returncode == 0
    [create] = created(env)
    argv = create["argv"]
    assert argv[argv.index("--title") + 1] == "docs: Project doc — Wardrobe planner"
    assert argv[argv.index("--head") + 1].startswith("docs/")
    body = create["body"]
    assert "## Summary\n\nA wardrobe planner." in body and "## Decisions\n\nWeb first." in body
    assert "## Still open\n\n- Q1 iOS or web?" in body and "Shops" not in body

    (wt / "docs/PROJECT.md").write_text(doc.replace("- Q1 iOS or web?", "None."))
    git("commit", "-q", "-am", "docs: answer Q1", cwd=wt)
    assert cli("publish", ticket_id).returncode == 0  # the open PR's body follows the doc
    [edit] = updates(env)
    assert edit["argv"][3] == "repos/acme/demo/pulls/1" and not any(a.startswith("title=") for a in edit["argv"])
    assert "Still open" not in edit["body"] and len(created(env)) == 1


def test_publish_reuses_an_open_pr(env):
    ticket_id = done_ticket(env)
    assert cli("publish", ticket_id).returncode == 0
    assert cli("publish", ticket_id).returncode == 0
    assert len(created(env)) == 1


def test_publish_refuses_unfinished_ticket(env):
    ticket_id = cli("new", "x", "--type", "chore").stdout.split("\t")[0]
    out = cli("publish", ticket_id)
    assert out.returncode == 2 and "only a done ticket" in out.stderr
    assert gh_calls(env) == []


def test_publish_refuses_attribution_trailer(env):
    ticket_id = done_ticket(env)
    wt = Path(state_of(env, ticket_id)["worktree"])
    (wt / "extra.txt").write_text("x\n")
    git("add", "extra.txt", cwd=wt)
    git("commit", "-q", "-m", "feat: extra\n\nCo-Authored-By: Someone <x@example.test>", cwd=wt)
    out = cli("publish", ticket_id)
    assert out.returncode == 2 and "Co-Authored-By" in out.stderr
    assert git("for-each-ref", "refs/heads/chore", cwd=env["origin"]) == ""


def test_publish_refuses_base_branch(env):
    ticket_id = done_ticket(env)
    path = env["state"] / "runs" / ticket_id / "ticket.json"
    t = json.loads(path.read_text())
    t["branch"] = "main"
    path.write_text(json.dumps(t))
    out = cli("publish", ticket_id)
    assert out.returncode == 2 and "protected or base branch" in out.stderr


def test_publish_needs_github_settings(env):
    ticket_id = done_ticket(env)
    env["config"].write_text(env["config"].read_text().replace('github_repo = "acme/demo"\n', ""))
    out = cli("publish", ticket_id)
    assert out.returncode == 2 and "github_repo and gh_user" in out.stderr


def test_pipeline_code_never_approves_readies_or_merges():
    # Plan rule: the human approves, marks ready and merges; the wrapper only opens drafts.
    forbidden = re.compile(r"""["'](merge|ready)["']|--approve|pr merge|pr ready|pr review""")
    hits = [
        f"{path.name}:{n}"
        for path in sorted((ROOT / "ziggy").glob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if forbidden.search(line)
    ]
    assert hits == []


def test_publish_uses_the_users_title_and_body_word_for_word(env):
    ticket_id = done_ticket(env)
    body = "## Summary\n\nPinned pymongo to 4.6.\n\n  - keep this indent\n"
    url = publish.publish(target(), ticket_id, title="  chore: pin pymongo 4.6  ", body=body)
    assert url == "https://github.example.test/acme/demo/pull/1"
    [create] = created(env)
    argv = create["argv"]
    assert "--draft" in argv  # still a draft: ready and merge stay with the human
    assert argv[argv.index("--title") + 1] == "chore: pin pymongo 4.6"
    assert create["body"] == body.rstrip() + f"\n\n`ziggy ticket {ticket_id}`\n"
    assert not updates(env)


def test_publish_edits_an_open_pr_only_with_the_users_text(env):
    ticket_id = done_ticket(env)
    publish.publish(target(), ticket_id)
    publish.publish(target(), ticket_id)  # no override: the open PR's title and description stay as they are
    assert updates(env) == []
    publish.publish(target(), ticket_id, title="chore: better title", body="New text.")
    [edit] = updates(env)
    assert edit["argv"][3] == "repos/acme/demo/pulls/1"
    assert edit["argv"][edit["argv"].index("title=chore: better title") - 1] == "-f"
    assert edit["body"] == f"New text.\n\n`ziggy ticket {ticket_id}`\n"
    assert len(created(env)) == 1


def test_publish_refuses_bad_overrides_before_pushing(env):
    ticket_id = done_ticket(env)
    for kwargs, message in (
        ({"title": "   "}, "must not be empty"),
        ({"title": "a\nb"}, "one line"),
        ({"title": "x" * 257}, "too long"),
        ({"body": "y" * (publish.MAX_BODY + 1)}, "too long"),
        ({"title": "ok", "body": "Done.\n\nCo-authored-by: Bot <bot@example.test>\n"}, "Co-Authored-By"),
        ({"title": "Co-Authored-By: Bot <b@example.test>"}, "Co-Authored-By"),
    ):
        with pytest.raises(publish.PublishError, match=message):
            publish.publish(target(), ticket_id, **kwargs)
    assert gh_calls(env) == []
    assert git("for-each-ref", "refs/heads/chore", cwd=env["origin"]) == ""


def test_publish_override_keeps_the_commit_trailer_rule(env):
    ticket_id = done_ticket(env)
    wt = Path(state_of(env, ticket_id)["worktree"])
    (wt / "extra.txt").write_text("x\n")
    git("add", "extra.txt", cwd=wt)
    git("commit", "-q", "-m", "feat: extra\n\nCo-Authored-By: Someone <x@example.test>", cwd=wt)
    with pytest.raises(publish.PublishError, match="Co-Authored-By trailer"):
        publish.publish(target(), ticket_id, title="chore: fine", body="Fine.")
    assert git("for-each-ref", "refs/heads/chore", cwd=env["origin"]) == ""


def test_adopted_pr_refuses_a_new_title_or_body(env):
    ticket_id = done_ticket(env)
    path = env["state"] / "runs" / ticket_id / "ticket.json"
    t = json.loads(path.read_text())
    t["pull"] = {"number": 7, "head": "their-branch", "base": "main", "author": "tester", "fork": False}
    path.write_text(json.dumps(t))
    with pytest.raises(publish.PublishError, match="adopted PR keeps its own title"):
        publish.publish(target(), ticket_id, body="text")
    assert gh_calls(env) == []
