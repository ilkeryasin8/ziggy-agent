"""The web panel: request guards, status snapshot, jobs started from the page, live activity,
question rounds and the approval gate answered from the page, publishing and stopping. The server
runs in-process on a free port; the jobs it starts are real `ziggy` processes against the
fake claude and gh."""
import http.client
import json
import subprocess
import threading
import time

import pytest
from conftest import calls, git, gh_calls, script

from ziggy import attention, bmad, changes, config, dashboard


@pytest.fixture
def panel(env):
    server = dashboard.Panel(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def request(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = None if body is None else json.dumps(body)
    hdrs = {"Host": f"127.0.0.1:{port}", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
    conn.request(method, path, body=data, headers=hdrs)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw.decode(errors="replace")
    return res.status, payload, dict(res.getheaders())


def row(server, ticket_id):
    _, snap, _ = request(server, "GET", "/api/status")
    return next(t for t in snap["tickets"] if t["id"] == ticket_id)


def settle(server, ticket_id, timeout=60):
    """Wait until the ticket has no running session or job."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = row(server, ticket_id)
        if not r["running"]:
            return r
        time.sleep(0.2)
    raise AssertionError(f"{ticket_id} still running: {r}")


def wait_for(server, ticket_id, check, timeout=20):
    deadline = time.monotonic() + timeout
    r = None
    while time.monotonic() < deadline:
        r = row(server, ticket_id)
        if check(r):
            return r
        time.sleep(0.2)
    raise AssertionError(f"{ticket_id}: condition not reached: {r}")


def create(server, run=True, title="chore: Sync pins", **extra):
    code, out, _ = request(server, "POST", "/api/new", {"target": "demo", "title": title, "body": "Pin pymongo.", "run": run, **extra})
    assert code == 200, out
    return out


def act(server, ticket_id, action, text="", **extra):
    return request(server, "POST", "/api/action", {"target": "demo", "id": ticket_id, "action": action, "text": text, **extra})


def test_page_assets_and_health(panel):
    code, page, headers = request(panel, "GET", "/")
    assert code == 200 and "<title>Ziggy</title>" in page
    assert "/review.js" in page and "/sessions.js" in page and "/chat.js" in page
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    assert "media-src 'self'" in headers["Content-Security-Policy"]  # evidence videos play in the drawer
    for path in ("/app.js", "/md.js", "/review.js", "/sessions.js", "/chat.js", "/app.css"):
        assert request(panel, "GET", path)[0] == 200, path
    # The panel's fonts are served from the panel itself (the CSP allows no other origin), Turkish letters included.
    css = request(panel, "GET", "/app.css")[1]
    for family in ("bricolage-grotesque", "instrument-sans", "jetbrains-mono"):
        for subset in ("latin", "latin-ext"):
            path = f"/fonts/{family}-{subset}.woff2"
            code, _, font_headers = request(panel, "GET", path)
            assert code == 200 and font_headers["Content-Type"] == "font/woff2" and f"url({path})" in css, path
    assert request(panel, "GET", "/fonts/LICENSE-instrument-sans.txt")[0] == 404  # only what STATIC lists
    assert request(panel, "GET", "/healthz")[1] == "ok\n"
    assert request(panel, "GET", "/etc/passwd")[0] == 404
    assert request(panel, "GET", "/api/targets/demo/bmad")[0] == 404  # BMad setup is gone


def test_guards_reject_foreign_hosts_cross_site_and_non_json(panel):
    assert request(panel, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert request(panel, "POST", "/api/new", {"target": "demo"}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(panel, "POST", "/api/new", {"target": "demo"}, headers={"Content-Type": "text/plain"})[0] == 415
    assert request(panel, "POST", "/api/new", {"target": "demo", "body": "x" * (dashboard.MAX_BODY + 1)})[0] == 413
    assert request(panel, "POST", "/api/action", {"target": "demo", "id": "local-1", "action": "merge"})[0] == 409
    assert request(panel, "POST", "/api/action", {"target": "demo", "id": "../x", "action": "run"})[0] == 404
    assert request(panel, "POST", "/api/action", {"target": "nope", "id": "local-1", "action": "run"})[0] == 404
    assert request(panel, "POST", "/api/new", {"target": "demo", "title": "x", "type": "story"})[0] == 400
    ticket_id = create(panel, run=False)["ticket"]["id"]
    assert request(panel, "GET", f"/api/activity/demo/{ticket_id}?phase=../../x")[0] == 404
    assert request(panel, "GET", f"/api/transcript/demo/{ticket_id}/01-Plan")[0] == 404
    assert request(panel, "GET", f"/api/stage/demo/{ticket_id}/01-plan/../../x")[0] == 404
    assert request(panel, "GET", f"/api/stage/demo/{ticket_id}/01-plan/stdout")[0] == 404
    assert request(panel, "GET", "/api/changes/demo/../x")[0] == 404
    assert request(panel, "GET", "/api/sessions/../etc/transcript")[0] == 404
    assert request(panel, "GET", "/api/sessions/claude/not-a-listed-session/transcript")[0] == 404
    code, out, _ = act(panel, ticket_id, "fix", "- [P1] x", review="../../etc")
    assert code == 409  # a new ticket has no review to fix


def test_new_ticket_runs_its_pipeline_as_a_job(panel, env):
    out = create(panel)
    ticket_id = out["ticket"]["id"]
    assert out["job"]["command"] == f"run {ticket_id}"
    r = settle(panel, ticket_id)
    assert r["status"] == "done" and r["pipeline"] == "chore" and r["cost_usd"] == 3.75
    assert r["needs_you"]["kind"] == "publish" and r["needs_you"]["severity"] == "low"
    assert [s["id"] for s in r["stages"]] == ["plan", "implement", "review"] and all(s["state"] == "done" for s in r["stages"])
    assert r["tasks"] == {"done": 0, "total": 2} and r["round"] is None and r["gate"] is None  # T001 and V001
    assert r["last_job"]["state"] == "ok"
    job_dir = next((env["state"] / "runs" / ticket_id / "jobs").iterdir())
    assert (job_dir / "exit").read_text().strip() == "0"

    code, act_, _ = request(panel, "GET", f"/api/activity/demo/{ticket_id}?offset=0")
    assert code == 200 and act_["phases"] == ["01-plan", "02-implement", "03-review"] and act_["phase"] == "03-review"
    assert "$ python -m pytest -q" in act_["lines"] and "read README.md" not in act_["lines"]  # brief by default
    again = request(panel, "GET", f"/api/activity/demo/{ticket_id}?offset={act_['offset']}")[1]
    assert again["lines"] == [] and again["offset"] == act_["offset"]
    full = request(panel, "GET", f"/api/activity/demo/{ticket_id}?phase=01-plan&all=1")[1]
    assert "read README.md" in full["lines"]

    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{ticket_id}")
    assert "## Ticket (untrusted)" in detail["ticket_md"] and "- [ ] T001" in detail["plan_text"]
    assert detail["plan_path"].endswith("inbox/plan.md")
    runs = detail["stage_runs"]
    assert [s["stage"] for s in runs] == ["plan", "implement", "review"]
    assert all(s["state"] == "ok" and s["verdict"] == "READY" and s["summary"] == "ok" and s["run"] == 1 for s in runs)
    assert runs[0]["remote_unchanged"] is True and runs[0]["cost_usd"] == 1.25
    code, prompt, _ = request(panel, "GET", f"/api/stage/demo/{ticket_id}/01-plan/prompt")
    assert code == 200 and prompt["name"] == "prompt.md" and "## Result protocol (ziggy)" in prompt["text"]
    assert request(panel, "GET", f"/api/stage/demo/{ticket_id}/01-plan/checks")[0] == 404
    code, tx, _ = request(panel, "GET", f"/api/transcript/demo/{ticket_id}/02-implement")
    assert code == 200 and tx["items"]


def test_a_question_round_is_answered_from_the_panel(panel, env):
    script(env, {"verify": [
        {"verdict": "NEEDS_USER", "summary": "two choices", "questions": [
            {"id": "q1", "text": "Which pin?", "options": ["4.6", "4.8"], "recommendation": "4.8"}]},
        {"verdict": "READY", "summary": "verified"}]})
    ticket_id = create(panel, title="feat: Weekly plan")["ticket"]["id"]
    r = settle(panel, ticket_id)
    assert r["status"] == "needs-user" and r["pipeline"] == "feat" and r["last_job"]["state"] == "ok"  # exit 3 is no error
    need = r["needs_you"]
    assert need["kind"] == "question" and need["severity"] == "high"
    assert need["round"]["questions"][0]["options"] == ["4.6", "4.8"] and r["round"]["n"] == 1
    assert r["stage"] == "verify" and [s["state"] for s in r["stages"]] == ["done", "done", "pending", "pending"]
    assert act(panel, ticket_id, "run")[0] == 409
    assert act(panel, ticket_id, "answer", " ")[0] == 409

    code, out, _ = act(panel, ticket_id, "answer", "q1: 4.8")
    assert code == 200, out
    r = settle(panel, ticket_id)
    assert r["status"] == "done" and r["needs_you"]["kind"] == "publish" and r["round"] is None
    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{ticket_id}")
    assert detail["rounds"][0]["answer"] == "q1: 4.8" and detail["rounds"][0]["answered_at"]
    assert "q1: 4.8" in detail["answers_md"] and "q1: 4.8" in detail["plan_text"]


def test_the_approval_gate_waits_for_the_panel(panel, env):
    script(env, {"plan": [{"verdict": "READY", "summary": "planned", "risk": "high", "size": "small"}]})
    ticket_id = create(panel)["ticket"]["id"]
    r = settle(panel, ticket_id)
    assert r["status"] == "awaiting-approval" and r["risk"] == "high"
    assert r["needs_you"]["kind"] == "approval" and r["needs_you"]["gate"]["stage"] == "plan"
    assert act(panel, ticket_id, "run")[0] == 409
    code, out, _ = act(panel, ticket_id, "approve")
    assert code == 200, out
    r = settle(panel, ticket_id)
    assert r["status"] == "done" and r["gate"] is None
    assert act(panel, ticket_id, "approve")[0] == 409


def test_publish_opens_a_draft_pr(panel, env):
    script(env, {"implement": [{"verdict": "READY", "summary": "pinned", "_commit": "requirements.txt"}]})
    ticket_id = create(panel)["ticket"]["id"]
    assert settle(panel, ticket_id)["tasks"]["done"] == 1
    settle(panel, ticket_id)
    code, out, _ = act(panel, ticket_id, "publish")
    assert code == 200, out
    r = settle(panel, ticket_id)
    assert r["pr"] and r["pr"].endswith("/acme/demo/pull/1") and r["needs_you"] is None
    assert act(panel, ticket_id, "run")[0] == 409


def test_review_comments_and_fixes_are_refused_before_there_is_work(panel):
    ticket_id = create(panel, run=False)["ticket"]["id"]
    assert act(panel, ticket_id, "request_changes")[0] == 409
    assert act(panel, ticket_id, "review")[0] == 409
    assert act(panel, ticket_id, "resume")[0] == 409


def test_a_closed_item_stays_closed_until_the_next_occurrence(panel, env):
    script(env, {"plan": [{"verdict": "FAILED", "summary": "cannot plan"}]})
    ticket_id = create(panel)["ticket"]["id"]
    need = settle(panel, ticket_id)["needs_you"]
    assert need["kind"] == "failed" and need["key"]
    assert act(panel, ticket_id, "dismiss", "failed:stale-key")[0] == 409  # only the item shown now can be closed
    code, out, _ = act(panel, ticket_id, "dismiss", need["key"])
    assert code == 200 and out["needs_you"] is None
    assert row(panel, ticket_id)["needs_you"] is None  # closed for good, not just for this response
    assert act(panel, ticket_id, "run")[0] == 200  # it fails again: a new occurrence shows again
    again = settle(panel, ticket_id)["needs_you"]
    assert again["kind"] == "failed" and again["key"] != need["key"]


def test_questions_need_an_answer_not_a_dismissal(panel, env):
    script(env, {"verify": [{"verdict": "NEEDS_USER", "questions": [{"text": "Which pin?"}]}]})
    ticket_id = create(panel, title="feat: Pin it")["ticket"]["id"]
    need = settle(panel, ticket_id)["needs_you"]
    assert need["kind"] == "question" and need["dismissable"] is False
    assert act(panel, ticket_id, "dismiss", need["key"])[0] == 409
    assert act(panel, ticket_id, "dismiss", "")[0] == 409
    # Steering is for a run under way; a ticket waiting for answers gets its answers instead.
    code, out, _ = act(panel, ticket_id, "nudge", "use pin 4.8")
    assert code == 409 and "answer" in out["error"]


def test_tickets_from_the_bmad_version_refuse_runs_at_once(panel, env):
    ticket_id = create(panel, run=False)["ticket"]["id"]
    path = env["state"] / "runs" / ticket_id / "state.json"
    st = json.loads(path.read_text())
    st.update(status="done", pipeline=None, spec="spec.md")
    path.write_text(json.dumps(st))
    assert row(panel, ticket_id)["legacy"] is True
    for action, text in (("run", ""), ("fix", "[P1] x"), ("resume", ""), ("request_changes", "")):
        code, out, _ = act(panel, ticket_id, action, text)
        assert code == 409 and "BMad version" in out["error"], (action, out)
    assert not (env["state"] / "runs" / ticket_id / "jobs").exists()  # refused before any job, so nothing to clear later


def test_stop_and_the_run_slots(panel, env):
    script(env, {"plan": [{"verdict": "READY", "summary": "ok", "_sleep": 60}]})
    ticket_id = create(panel)["ticket"]["id"]
    wait_for(panel, ticket_id, lambda r: r["phase"])
    assert row(panel, ticket_id)["running"]
    code, out, _ = act(panel, ticket_id, "run")
    assert code == 409 and "already running" in out["error"]

    # A second ticket takes the target's second run slot (max_active = 2 by default) ...
    code, out, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: Other", "run": True})
    assert code == 200 and out["job"]["queued"] is False
    other = out["ticket"]["id"]
    wait_for(panel, other, lambda r: r["phase"])
    assert row(panel, other)["running"] and row(panel, other)["queued_at"] is None
    assert len(request(panel, "GET", "/api/status")[1]["targets"][0]["active"]) == 2
    # ... and a third waits in line, until it is taken out of it.
    code, out, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: Third", "run": True})
    assert code == 200 and out["job"]["queued"] is True
    third = out["ticket"]["id"]
    r = wait_for(panel, third, lambda r: r["queued_at"])
    assert r["running"]
    code, out, _ = act(panel, third, "stop")
    assert code == 200 and out["stopped"] == "queue"
    r = settle(panel, third)
    assert r["status"] == "new" and r["needs_you"] is None and r["queued_at"] is None
    act(panel, other, "stop")
    settle(panel, other)

    code, out, _ = act(panel, ticket_id, "stop")
    assert code == 200 and out["stopped"] == "01-plan"
    r = settle(panel, ticket_id)
    assert r["status"] == "failed" and r["needs_you"]["kind"] == "failed" and r["needs_you"]["severity"] == "medium"
    assert r["stages"][0]["state"] == "failed"
    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{ticket_id}")
    assert detail["stage_runs"][0]["state"] == "killed"


def test_a_quiet_session_and_a_dead_run_need_you(panel, env):
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"', 'gh_user = "tester"\nquiet_after = 1'))
    script(env, {"plan": [{"verdict": "READY", "summary": "ok", "_sleep": 6}]})
    ticket_id = create(panel)["ticket"]["id"]
    r = wait_for(panel, ticket_id, lambda r: r["needs_you"])
    need = r["needs_you"]
    assert need["kind"] == "quiet" and need["minutes"] == 0 and need["severity"] == "medium"
    assert "session started · fake-model" in need["details"]
    settle(panel, ticket_id)

    # A lock left by a process that is gone: not "running", and a card to run it again.
    dead = subprocess.Popen(["true"])
    dead.wait()
    other = create(panel, run=False, title="chore: Other")["ticket"]["id"]
    lock = env["state"] / "locks" / f"ticket-{other}"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{dead.pid} ticket {other} 2026-01-01\n")
    r = row(panel, other)
    assert r["running"] is False and r["needs_you"]["kind"] == "lost" and r["needs_you"]["severity"] == "high"
    script(env, {})
    code, out, _ = act(panel, other, "run")
    assert code == 200, out
    assert settle(panel, other)["status"] == "done"


def test_stats_count_cost_by_stage(panel, env):
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"', 'gh_user = "tester"\nbudget_usd = 4.5'))
    ticket_id = create(panel)["ticket"]["id"]
    settle(panel, ticket_id)
    stage_calls = [c for c in calls(env) if c.get("stage")]
    assert stage_calls and all(c["argv"][c["argv"].index("--max-budget-usd") + 1] == "4.5" for c in stage_calls)
    code, s, _ = request(panel, "GET", "/api/stats")
    assert code == 200
    assert s["totals"]["all"] == 3.75 and s["totals"]["today"] == 3.75 and s["totals"]["sessions"] == 3 and s["totals"]["tickets"] == 1
    today = s["daily"][-1]
    assert today["cost"] == 3.75 and today["stages"] == {"plan": 1.25, "implement": 1.25, "review": 1.25} and today["sessions"] == 3
    assert len(s["daily"]) == 14
    assert s["models"][0]["name"] == "fake-model" and s["models"][0]["output"] == 60 and s["models"][0]["cache_read"] == 900
    assert {g["name"] for g in s["by_stage"]} == {"plan", "implement", "review"}
    assert s["outcomes"] == {"ready": 3}
    assert s["top_tickets"][0]["id"] == ticket_id and s["budgets"]["demo"] == 4.5


def test_a_running_ticket_shows_what_it_does(panel, env):
    script(env, {"plan": [{"verdict": "READY", "summary": "ok", "_sleep": 3}]})
    ticket_id = create(panel)["ticket"]["id"]
    r = wait_for(panel, ticket_id, lambda r: r["running"] and r["phase_started_at"])
    assert r["phase"] == "01-plan" and r["stage"] == "plan" and r["run_started_at"]
    assert r["stages"][0]["state"] == "current"
    settle(panel, ticket_id)
    done = row(panel, ticket_id)
    assert done["last_lines"] == [] and done["phase_started_at"] is None and done["activity"] is None


def test_the_composer_takes_a_type_and_the_project_page_routes(panel, env, monkeypatch):
    out = create(panel, run=False, title="Write the product doc", type="project")
    assert out["ticket"]["type"] == "project" and out["ticket"]["branch"].startswith("docs/")
    assert create(panel, run=False, title="fix: Login crash", type="feat")["ticket"]["type"] == "fix"  # a prefix wins
    monkeypatch.setenv("FAKE_ROUTE", json.dumps({"type": "project", "title": "Product doc", "reason": "No PROJECT.md yet."}))
    code, routed, _ = request(panel, "POST", "/api/targets/demo/route", {"text": "let us settle what the product is"})
    assert code == 200 and routed["type"] == "project" and routed["title"] == "Product doc"
    code, status, _ = request(panel, "GET", "/api/targets/demo/project")
    assert code == 200 and status["project_doc"]["exists"] is False


def test_sessions_on_this_machine_are_listed(panel):
    code, snap, _ = request(panel, "GET", "/api/status")
    assert code == 200 and isinstance(snap["sessions"], list)
    code, listed, _ = request(panel, "GET", "/api/sessions")
    assert code == 200 and isinstance(listed["sessions"], list) and isinstance(listed["sessions_hidden"], int)
    assert isinstance(snap["sessions_hidden"], int)


DEFERRED = """## local-1 round 1 (2026-10-16)

- R1-1 [low/risk] pyproject.toml:63 — `extend-exclude` only applies to directory scans. Evidence: no force-exclude.
  Why deferred: out of scope.

## local-1 round 2 (2026-10-16)

- R2-2 [nit/low/bug] src/cli.py:129 — TRIM strips spaces only; a tab category becomes its own row. Evidence: SQLite docs.
"""


def test_deferred_finding_becomes_a_linked_ticket(panel, env):
    source = create(panel, run=False)["ticket"]["id"]
    inbox = env["state"] / "runs" / source / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "deferred.md").write_text(DEFERRED)
    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{source}")
    risk, bug = detail["deferred"]
    assert (risk["key"], risk["round"], risk["type"], risk["ticket"]) == ("R1-1", 1, "chore", None)
    assert risk["title"] == "`extend-exclude` only applies to directory scans"
    assert risk["text"].endswith("Why deferred: out of scope.")  # the wrapped line belongs to the entry
    assert (bug["key"], bug["round"], bug["type"]) == ("R2-2", 2, "fix")
    assert bug["body"] == bug["text"] + f"\n\nKaynak: {source} deferred (round 2)"

    origin = {"kind": "deferred", "ticket": source, "entry": "R2-2"}
    out = create(panel, run=False, title=bug["title"], type=bug["type"], body=bug["body"], origin=origin)
    made = out["ticket"]
    assert made["id"] != source and made["id"].startswith("local-") and made["type"] == "fix" and "job" not in out
    ticket_json = json.loads((env["state"] / "runs" / made["id"] / "ticket.json").read_text())
    assert ticket_json["origin"] == origin and ticket_json["source"] == "local"
    assert f"deferred review finding R2-2 of ticket {source}" in (env["state"] / "runs" / made["id"] / "ticket.md").read_text()
    assert row(panel, made["id"])["origin"] == origin

    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{source}")
    assert [e["ticket"] for e in detail["deferred"]] == [None, made["id"]]
    code, err, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "again", "origin": origin})
    assert code == 400 and "already ticket" in err["error"]
    code, _, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "x", "origin": {**origin, "entry": "R9-9"}})
    assert code == 404
    code, _, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "x", "origin": {"name": "yt", "ref": "X-1"}})
    assert code == 400  # the panel only links deferred findings


def wait_exit(job_dir, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (job_dir / "exit").exists():
            return int((job_dir / "exit").read_text().strip())
        time.sleep(0.2)
    raise AssertionError(f"{job_dir} still running")


def test_an_empty_panel_has_a_board_and_an_inbox(panel):
    code, snap, _ = request(panel, "GET", "/api/status")
    assert code == 200 and snap["unread"] == 0
    assert set(snap["board"]) == {"waiting_on_you", "ready_for_review", "working", "waiting_on_others", "landing"}
    assert not any(snap["board"].values())
    code, out, _ = request(panel, "GET", "/api/attention")
    assert code == 200 and out["items"] == []


def test_evidence_files_are_served_from_the_artifacts_dir_only(panel, env, tmp_path):
    ticket_id = create(panel, run=False)["ticket"]["id"]
    art = env["state"] / "runs" / ticket_id / "inbox" / "artifacts"
    (art / "shots").mkdir(parents=True)
    (art / "shots" / "home.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    (art / ".hidden.png").write_bytes(b"x")
    (art / "notes.sh").write_text("echo hi\n")
    (tmp_path / "secret.png").write_bytes(b"secret")
    (art / "link.png").symlink_to(tmp_path / "secret.png")
    with (art / "big.png").open("wb") as f:
        f.truncate(changes.ARTIFACT_LIMITS[".png"] + 1)
    (art / "clip.mp4").write_bytes(bytes(range(256)) * 3 + bytes(232))  # 1000 bytes
    base = f"/api/files/demo/{ticket_id}/"

    code, body, headers = request(panel, "GET", base + "shots/home.png")
    assert code == 200 and headers["Content-Type"].startswith("image/png")
    assert headers["Content-Security-Policy"] == dashboard.EVIDENCE_CSP and "sandbox" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"
    assert headers["Content-Length"] == "108"
    for bad in ("shots%2Fhome.png", "notes.sh", ".hidden.png", "link.png", "shots/../../../ticket.json", "missing.png", "shots",
                "shots//home.png", "%ff.png"):
        assert request(panel, "GET", base + bad)[0] == 404, bad
    assert request(panel, "GET", f"/api/files/demo/../{ticket_id}/shots/home.png")[0] == 404
    assert request(panel, "GET", base + "big.png")[0] == 413

    def ranged(spec):
        code, _, headers = request(panel, "GET", base + "clip.mp4", headers={"Range": spec})
        return code, headers.get("Content-Range"), headers.get("Content-Length")
    assert ranged("bytes=0-99") == (206, "bytes 0-99/1000", "100")
    assert ranged("bytes=-10") == (206, "bytes 990-999/1000", "10")
    assert ranged("bytes=900-") == (206, "bytes 900-999/1000", "100")
    assert ranged("bytes=900-5000") == (206, "bytes 900-999/1000", "100")
    assert ranged("bytes=2000-") == (416, "bytes */1000", "0")
    assert ranged("bytes=50-10") == (416, "bytes */1000", "0")
    assert ranged("items=0-1") == (200, None, "1000")  # not a byte range: the whole file
    port = panel.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", base + "clip.mp4", headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=256-259"})
    res = conn.getresponse()
    assert res.status == 206 and res.headers["Accept-Ranges"] == "bytes" and res.read() == bytes([0, 1, 2, 3])
    conn.close()


def test_saved_attempt_patches_are_served_as_text(panel, env):
    ticket_id = create(panel, run=False)["ticket"]["id"]
    attempts = env["state"] / "runs" / ticket_id / "attempts"
    attempts.mkdir()
    (attempts / "attempt-1.patch").write_text("# Commits:\ndiff --git a/a.txt b/a.txt\n")
    (attempts / "attempt-1.json").write_text("{}")
    code, body, headers = request(panel, "GET", f"/api/attempts/demo/{ticket_id}/attempt-1.patch")
    assert code == 200 and body.startswith("# Commits:") and headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    for bad in ("attempt-2.patch", "attempt-1.json", "attempt-0.patch", "..%2Fticket.json", "attempt-1.patch.bak"):
        assert request(panel, "GET", f"/api/attempts/demo/{ticket_id}/{bad}")[0] == 404, bad
    assert request(panel, "GET", "/api/attempts/demo/local-99/attempt-1.patch")[0] == 404


def test_stage_cards_carry_their_evidence_urls():
    views = dashboard._artifact_views({"artifacts": ["notes.log", {"path": "shots/home page.png", "kind": "image", "caption": "AC-1"},
                                                     {"path": "../escape.png"}, {"path": "/etc/passwd"}, {"caption": "no path"}, 7]},
                                      "/api/files/demo/local-1/")
    assert [v["path"] for v in views] == ["notes.log", "shots/home page.png"]
    assert views[1]["url"] == "/api/files/demo/local-1/shots/home%20page.png" and views[1]["caption"] == "AC-1"
    assert dashboard._artifact_views(None, "/x/") == []


def test_the_inbox_lists_marks_read_and_counts_unread(panel, env):
    script(env, {"plan": [{"verdict": "FAILED", "summary": "cannot plan"}]})
    ticket_id = create(panel)["ticket"]["id"]
    settle(panel, ticket_id)
    attention.reconcile([config.load_target("demo")])
    code, out, _ = request(panel, "GET", "/api/attention")
    item = next(i for i in out["items"] if i["ticket"] == ticket_id)
    assert code == 200 and item["kind"] == "failed" and item["target"] == "demo" and item["seen_at"] is None
    _, snap, _ = request(panel, "GET", "/api/status")
    assert snap["unread"] >= 1
    assert any(e["id"] == ticket_id for e in snap["board"]["waiting_on_you"])
    assert row(panel, ticket_id)["needs_you"]["unread"] is True

    assert request(panel, "POST", "/api/attention/seen", {})[0] == 400  # say which items
    assert request(panel, "POST", "/api/attention/seen", {"target": "demo", "ticket": ticket_id, "keys": "all"})[0] == 400
    assert request(panel, "POST", "/api/attention/seen", {"target": "demo", "ticket": "../x"})[0] == 404
    assert request(panel, "POST", "/api/attention/seen", {"all": True}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    code, out, _ = request(panel, "POST", "/api/attention/seen", {"target": "demo", "ticket": ticket_id})
    assert code == 200 and out["changed"] >= 1
    _, snap, _ = request(panel, "GET", "/api/status")
    assert snap["unread"] == 0 and row(panel, ticket_id)["needs_you"]["seen_at"]
    code, out, _ = request(panel, "POST", "/api/attention/seen", {"all": True})
    assert code == 200 and out["changed"] == 0


def test_a_running_stage_takes_a_nudge_without_a_busy_refusal(panel, env):
    waiting = create(panel, run=False, title="chore: Not started")["ticket"]["id"]
    code, out, _ = act(panel, waiting, "nudge", "use the v2 API")
    assert code == 409  # no run to steer
    script(env, {"plan": [{"verdict": "READY", "summary": "ok", "_sleep": 60}, {"verdict": "READY", "summary": "steered"}]})
    ticket_id = create(panel)["ticket"]["id"]
    wait_for(panel, ticket_id, lambda r: r["phase"] and r["running"])
    time.sleep(1)  # the session's pid file is down
    assert act(panel, ticket_id, "nudge", "   ")[0] == 409  # an empty message
    code, out, _ = act(panel, ticket_id, "nudge", "use the v2 API")
    assert code == 200 and out["nudged"]["delivery"] == "interrupt", out
    settle(panel, ticket_id)
    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{ticket_id}")
    first = detail["stage_runs"][0]
    assert first["state"] == "killed" and first["kill_reason"] == "nudge:user" and first["nudged"]["text"] == "use the v2 API"
    assert detail["stage_runs"][1]["retry_of"] == first["name"]


def test_cleanup_removes_a_finished_worktree_and_keeps_the_branch(panel, env):
    fresh = create(panel, run=False, title="chore: Not started")["ticket"]["id"]
    code, out, _ = act(panel, fresh, "cleanup")
    assert code == 409 and "finished" in out["error"]
    script(env, {"implement": [{"verdict": "READY", "summary": "pinned", "_commit": "requirements.txt"}]})
    ticket_id = create(panel)["ticket"]["id"]
    r = settle(panel, ticket_id)
    assert r["status"] == "done" and r["worktree"]
    wt = r["worktree"]
    code, out, _ = act(panel, ticket_id, "cleanup")
    assert code == 200 and out["job"]["command"] == f"cleanup {ticket_id}" and "--force" not in out["job"]["argv"]
    r = settle(panel, ticket_id)
    assert r["worktree"] is None and not (env["state"] / "worktrees" / ticket_id).exists() and wt
    assert git("branch", "--list", r["branch"], cwd=env["repo"]).strip()  # the branch stays
    code, out, _ = act(panel, ticket_id, "cleanup", "force")
    assert code == 409 and "no worktree" in out["error"]


def test_publish_takes_the_edited_title_and_description(panel, env):
    script(env, {"implement": [{"verdict": "READY", "summary": "pinned", "_commit": "requirements.txt"}]})
    ticket_id = create(panel)["ticket"]["id"]
    assert settle(panel, ticket_id)["status"] == "done"
    code, draft, _ = request(panel, "GET", f"/api/changes/demo/{ticket_id}/pr-draft")
    assert code == 200 and draft["title"] and isinstance(draft["body"], str)
    assert act(panel, ticket_id, "publish", title="two\nlines", body="x")[0] == 400
    assert act(panel, ticket_id, "publish", title="", body="x")[0] == 400
    assert act(panel, ticket_id, "publish", title="ok", body=7)[0] == 400
    code, out, _ = act(panel, ticket_id, "publish", title="  chore: Pin pymongo for the CI  ", body="Edited by hand.\n\n- one\n")
    assert code == 200, out
    assert out["job"]["argv"][-4:-2] == ["--title", "chore: Pin pymongo for the CI"]
    r = settle(panel, ticket_id)
    assert r["pr"], r
    created = next(c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"])
    assert created["argv"][created["argv"].index("--title") + 1] == "chore: Pin pymongo for the CI"
    assert "--draft" in created["argv"] and created["body"].startswith("Edited by hand.\n\n- one\n")


def test_chat_runs_as_its_own_job_and_never_blocks_the_ticket(panel, env):
    ticket_id = create(panel, run=False)["ticket"]["id"]
    code, view, _ = request(panel, "GET", f"/api/chat/demo/{ticket_id}")
    assert code == 200 and view["turns"] == [] and view["live"] is False
    assert request(panel, "GET", f"/api/chat/demo/{ticket_id}?since=../x")[0] == 200  # a bad cursor is ignored
    assert request(panel, "GET", "/api/chat/demo/local-999")[0] == 404
    assert act(panel, ticket_id, "chat", "   ")[0] == 409
    assert act(panel, ticket_id, "chat", "why?", model="gpt-9")[0] == 409
    assert act(panel, ticket_id, "chat_stop")[0] == 409  # nothing is being answered
    code, out, _ = act(panel, ticket_id, "chat", "Where does this ticket stand?", new=True)
    assert code == 200, out
    run_dir = env["state"] / "runs" / ticket_id
    job_dir = run_dir / "chat" / "jobs" / out["job"]["id"]
    assert job_dir.is_dir() and "--new" in out["job"]["argv"]
    assert row(panel, ticket_id)["running"] is False  # a chat reply is not a run: run/approve stay possible
    assert not (run_dir / "jobs").exists()
    wait_exit(job_dir)
    _, view, _ = request(panel, "GET", f"/api/chat/demo/{ticket_id}")
    assert [t["question"] for t in view["turns"]] == ["Where does this ticket stand?"]
    _, detail, _ = request(panel, "GET", f"/api/ticket/demo/{ticket_id}")
    assert detail["chat"]["turns"] == 1 and detail["jobs"] == []


def test_targets_carry_bmad_and_new_takes_a_pipeline(panel, env):
    from test_bmad import install
    bmad._CACHE.clear()
    _, snap, _ = request(panel, "GET", "/api/status")
    assert snap["targets"][0]["bmad"]["installed"] is False and snap["targets"][0]["bmad"]["missing_local"] == []
    code, out, _ = request(panel, "POST", "/api/new", {"target": "demo", "title": "fix: Login loops", "pipeline": "bmad"})
    assert code == 400 and "BMad is not installed" in str(out)
    install(env)
    (env["repo"] / "_bmad" / "custom" / "config.user.toml").unlink()
    bmad._CACHE.clear()
    _, snap, _ = request(panel, "GET", "/api/status")
    marker = snap["targets"][0]["bmad"]
    assert marker["installed"] and marker["version"] == "6.12.0" and marker["missing_local"] == ["_bmad/custom/config.user.toml"]
    (env["repo"] / "_bmad" / "custom" / "config.user.toml").write_text("[core]\n")
    bmad._CACHE.clear()
    _, snap, _ = request(panel, "GET", "/api/status")
    assert snap["targets"][0]["bmad"]["installed"] and snap["targets"][0]["bmad"]["version"] == "6.12.0"
    assert snap["targets"][0]["bmad"]["missing_local"] == []
    out = create(panel, run=False, title="fix: Login loops", pipeline="bmad")
    assert row(panel, out["ticket"]["id"])["pipeline"] == "bmad" and out["ticket"]["pipeline"] == "bmad"
    assert create(panel, run=False, title="chore: Tidy", pipeline="standard")["ticket"]["pipeline"] == "chore"
    bmad._CACHE.clear()
