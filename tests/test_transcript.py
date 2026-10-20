"""A session's full transcript for the panel's Fazlar tab, and what a ticket row offers after a
Codex review (clean, with findings, or older than the branch)."""
import json

from conftest import cli, git
from test_dashboard import panel, request, row  # noqa: F401 - the panel fixture
from test_review import codex, done_ticket, st  # noqa: F401 - the codex fixture

from ziggy import config, progress
from ziggy.dispatch import OUTPUT

WT = "/wt/local-9"
EVENTS = [
    {"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"},
    {"type": "system", "subtype": "init", "cwd": WT, "model": "claude-opus-5-5"},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
    {"type": "rate_limit_event"},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "Reading the spec first."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"cd {WT} && uv run pytest -q", "description": "tests"}},
    ]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 failed", "is_error": True}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"description": "Implement pass 2", "prompt": "do it"}},
    ]}},
    {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [{"type": "text", "text": "Implement pass 2 of the spec."}]}},
    {"type": "assistant", "parent_tool_use_id": "a1", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": f"{WT}/src/app.py", "old_string": "a", "new_string": "b"}},
    ]}},
    {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "x" * 20000}]}]}},
    {"type": "system", "subtype": "task_progress", "description": "Reading"},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a1", "content": [{"type": "text", "text": "Done: edited app.py."}]}]}},
    {"type": "result", "subtype": "success", "total_cost_usd": 1.25, "num_turns": 7, "result": "All done."},
]


def write_stream(phase_dir, events, partial=""):
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / OUTPUT).write_text("".join(json.dumps(e) + "\n" for e in events) + partial)


def test_transcript_items_keep_messages_tools_results_and_subagents(tmp_path):
    d = tmp_path / "02-build"
    write_stream(d, EVENTS)
    r = progress.transcript(d)
    kinds = [i["kind"] for i in r["items"]]
    assert kinds == ["session", "text", "tool", "result", "tool", "prompt", "tool", "result", "result", "end"]
    assert not r["more"] and not r["alive"] and r["pos"] == (d / OUTPUT).stat().st_size
    by_kind = {}
    for i in r["items"]:
        by_kind.setdefault(i["kind"], []).append(i)
    bash, agent, edit = by_kind["tool"]
    assert bash["summary"] == "$ uv run pytest -q" and bash["input"]["description"] == "tests"
    assert agent["description"] == "Implement pass 2" and agent["parent"] is None
    assert edit["parent"] == "a1" and edit["summary"] == "edit src/app.py"  # relative to the worktree
    failed, long_result, report = by_kind["result"]
    assert failed["is_error"] and failed["tool_id"] == "t1"
    assert long_result["truncated"] and len(long_result["content"]) == progress.RESULT_LIMIT and long_result["parent"] == "a1"
    assert report["tool_id"] == "a1" and report["content"] == "Done: edited app.py."
    assert by_kind["prompt"][0]["parent"] == "a1"
    assert by_kind["end"][0]["cost"] == 1.25 and by_kind["end"][0]["text"] == "All done."


def test_transcript_pages_and_waits_for_a_partial_line(tmp_path):
    d = tmp_path / "01-plan"
    write_stream(d, EVENTS, partial='{"type": "assistant", "mess')
    first = progress.transcript(d, 0, limit=3)
    assert len(first["items"]) >= 3 and first["more"]
    rest, pos = [], first["pos"]
    while True:
        page = progress.transcript(d, pos, limit=3)
        rest += page["items"]
        pos = page["pos"]
        if not page["more"]:
            break
    assert len(first["items"]) + len(rest) == 10
    assert pos < (d / OUTPUT).stat().st_size  # the partial line is not consumed
    assert progress.transcript(d, pos)["items"] == []


def test_a_session_from_before_streaming_shows_its_final_message(tmp_path):
    d = tmp_path / "01-plan"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"final_message": "Spec written."}))
    r = progress.transcript(d)
    assert r["legacy"] and r["items"] == [{"kind": "text", "parent": None, "text": "Spec written."}]
    assert progress.transcript(d, r["pos"])["items"] == []


def test_transcript_endpoint(panel, env):  # noqa: F811
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    assert cli("run", ticket_id).returncode == 0
    first = progress.phase_dirs(config.load_target("demo"), ticket_id)[0].name
    code, out, _ = request(panel, "GET", f"/api/transcript/demo/{ticket_id}/{first}")
    assert code == 200 and out["items"] and not out["alive"]
    assert request(panel, "GET", f"/api/transcript/demo/{ticket_id}/99-implement")[0] == 404
    assert request(panel, "GET", f"/api/transcript/demo/{ticket_id}/..")[0] == 404
    assert request(panel, "GET", f"/api/transcript/demo/{ticket_id}/{first}?pos=-1")[0] == 400
    assert request(panel, "GET", f"/api/transcript/demo/local-99/{first}")[0] == 404


def test_stage_dirs_header_and_footer(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    run = env["state"] / "runs" / ticket_id
    for name in ("02-implement", "01-plan", "100-review", "10-fix-review", "jobs", "reviews", "3-x", "04-Bad"):
        (run / name).mkdir(exist_ok=True)
    dirs = progress.phase_dirs(config.load_target("demo"), ticket_id)
    assert [d.name for d in dirs] == ["01-plan", "02-implement", "10-fix-review", "100-review"]
    impl = run / "02-implement"
    (impl / "run.json").write_text(json.dumps({"stage": "implement", "visit": 2, "model": "opus"}))
    assert progress.header(impl) == "== 02-implement · implement (visit 2) · opus =="
    assert progress.footer(impl) is None
    (impl / "run.json").write_text(json.dumps({"stage": "implement", "model": "opus", "finished_at": "x", "verdict": "READY",
                                               "route": "fix", "state": "ok", "total_cost_usd": 1.5}))
    assert progress.footer(impl) == "== 02-implement ended: READY → fix · $1.50 =="
    (impl / "run.json").write_text(json.dumps({"stage": "implement", "finished_at": "x", "verdict": None, "state": "timeout"}))
    assert progress.footer(impl) == "== 02-implement ended: no result (timeout) =="


def test_row_review_is_clean_with_findings_or_stale(panel, env, codex):  # noqa: F811
    ticket_id = done_ticket(env)
    assert cli("review", ticket_id).returncode == 0
    r = row(panel, ticket_id)["review"]
    assert r["state"] == "ok" and r["findings"] >= 1 and not r["stale"] and r["head"]

    [review_dir] = (env["state"] / "runs" / ticket_id / "reviews").iterdir()
    (review_dir / "review.md").write_text("No issues found.\n")
    r = row(panel, ticket_id)["review"]
    assert r["state"] == "ok" and r["findings"] == 0 and not r["stale"]

    wt = st(env, ticket_id)["worktree"]
    git("commit", "-q", "--allow-empty", "-m", "fix: follow-up", cwd=wt)
    assert row(panel, ticket_id)["review"]["stale"]
