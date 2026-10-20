"""Ticket chat (chat.py) against tests/fake_claude_chat.py: threads and resume, cost accounting, limits,
stop, timeout, the draft rebuilt from text deltas, lost turns, and worktree changes stashed after a turn."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ziggy import chat, config, dispatch, state, ticket
from ziggy.pipeline import PipelineError

FAKE = Path(__file__).resolve().parent / "fake_claude_chat.py"


@pytest.fixture
def ch(env, tmp_path, monkeypatch):
    monkeypatch.setenv("ZIGGY_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CHAT_LOG", str(tmp_path / "chat.log"))
    monkeypatch.setenv("FAKE_CHAT_SCRIPT", str(tmp_path / "chat-script.json"))
    settings = SimpleNamespace(models=chat.MODELS, timeout=chat.TIMEOUT)
    monkeypatch.setattr(config, "load_chat", lambda: settings, raising=False)
    monkeypatch.setattr(dispatch, "_HELP", {})
    chat._streams.clear()
    chat._ids.clear()
    target = config.load_target("demo")
    t = ticket.create(target, "Fix the login", "fix", "The login page loops.", False, "low")
    return SimpleNamespace(target=target, id=t["id"], tmp=tmp_path, settings=settings)


def play(ch, *entries):
    (ch.tmp / "chat-script.json").write_text(json.dumps(list(entries)))


def calls(ch):
    path = ch.tmp / "chat.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def denied(argv):
    """The --disallowedTools values (up to the next flag)."""
    rest = argv[argv.index("--disallowedTools") + 1:]
    return rest[:next((i for i, a in enumerate(rest) if a.startswith("--")), len(rest))]


def in_thread(fn, *args, **kwargs):
    out = {}

    def body():
        try:
            out["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            out["error"] = exc

    th = threading.Thread(target=body)
    th.start()
    return th, out


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def pid_file(ch):
    return next(iter((state.run_dir(ch.target, ch.id) / "chat").glob("*.pid")), None)


def test_follow_up_resumes_and_new_conversation_starts_fresh(ch):
    play(ch, {"cost": 0.05}, {"cost": 0.08}, {"cost": 0.02})
    first = chat.send(ch.target, ch.id, "where does it stand?")
    second = chat.send(ch.target, ch.id, "and the plan?", model="opus")
    third = chat.send(ch.target, ch.id, "fresh start", new_thread=True)

    assert [first["state"], second["state"], third["state"]] == ["ok", "ok", "ok"]
    assert first["answer"] == "answer 1" and second["answer"] == "answer 2"
    a, b, c = (x["argv"] for x in calls(ch))
    sid = flag(a, "--session-id")
    assert sid and "--resume" not in a
    assert flag(b, "--resume") == sid and "--session-id" not in b
    assert flag(c, "--session-id") not in (None, sid) and "--resume" not in c
    assert flag(a, "--model") == "sonnet" and flag(b, "--model") == "opus"  # sonnet is the default
    assert second["thread"] == first["thread"] == first["id"] and third["thread"] == third["id"]
    assert [t["ordinal"] for t in (first, second, third)] == [1, 2, 3]
    assert (first["cost_usd"], second["cost_usd"], third["cost_usd"]) == (0.05, 0.03, 0.02)

    # The question is the prompt; the ticket's context is a system prompt; the session is read-only.
    assert flag(a, "-p") == "where does it stand?"
    system = flag(a, "--append-system-prompt")
    assert system.startswith(f"# Chat about ticket {ch.id}") and "Fix the login" in system and "The login page loops." in system
    deny = denied(a)
    for tool in ("Edit", "Write", "NotebookEdit", "AskUserQuestion", "ScheduleWakeup", "CronCreate", "Bash(git commit:*)",
                 "Bash(git checkout:*)", "Bash"):
        assert tool in deny
    assert "No shell in this mode" in system  # no worktree: the developer's own checkout, no Bash at all
    assert flag(a, "--settings") == str(dispatch.SETTINGS) and flag(a, "--setting-sources") == "project"
    assert "--strict-mcp-config" in a and flag(a, "--permission-mode") == "auto" and "--include-partial-messages" in a
    env = calls(ch)[0]["env"]
    assert env["PIPELINE_STAGE"] == "chat" and env["ZIGGY_RUN"] == "1" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert calls(ch)[0]["cwd"] == os.path.realpath(ch.target.path)  # no worktree: the main checkout

    view = chat.view(ch.target, ch.id)
    assert [t["question"] for t in view["turns"]] == ["where does it stand?", "and the plan?", "fresh start"]
    assert view["live"] is False and view["cwd"] == "main" and view["models"] == list(chat.MODELS)
    assert view["resume_command"].endswith(f"claude --resume {flag(c, '--session-id')}")
    assert [t["id"] for t in chat.view(ch.target, ch.id, since=second["id"])["turns"]] == [second["id"], third["id"]]


def test_stop_keeps_the_session_and_the_cost_base(ch):
    play(ch, {"cost": 0.05}, {"sleep": 30}, {"cost": 0.08})
    chat.send(ch.target, ch.id, "warm up")
    th, out = in_thread(chat.send, ch.target, ch.id, "a long one")
    wait_for(lambda: pid_file(ch) and len(calls(ch)) == 2)
    view = chat.view(ch.target, ch.id)
    assert view["live"] is True and view["turns"][-1]["state"] == "running"
    assert chat.summary(ch.target, ch.id) == {"turns": 2, "live": True}
    started = time.monotonic()
    assert chat.stop(ch.target, ch.id)["ok"] is True
    th.join(20)
    assert time.monotonic() - started < 15
    stopped = out["value"]
    assert stopped["state"] == "killed" and stopped["error"] == "stopped" and stopped["cost_usd"] is None
    with pytest.raises(LookupError, match="nothing is being answered"):
        chat.stop(ch.target, ch.id)

    after = chat.send(ch.target, ch.id, "go on")
    first_sid = flag(calls(ch)[0]["argv"], "--session-id")
    assert flag(calls(ch)[2]["argv"], "--resume") == first_sid
    assert after["cost_usd"] == 0.03  # 0.08 minus the 0.05 seen before the stopped turn
    turns = state.read_json(state.run_dir(ch.target, ch.id) / "chat" / "turns.json")
    assert turns[-1]["cost_base_usd"] == 0.05 and turns[-1]["session_cost_usd"] == 0.08


def test_follow_up_starts_a_new_session_when_its_directory_is_gone(ch):
    wt = ch.tmp / "wt"
    wt.mkdir()
    st = state.load_state(ch.target, ch.id)
    st["worktree"] = str(wt)
    state.save_state(ch.target, ch.id, st)
    play(ch, {"cost": 0.1}, {"cost": 0.04})
    chat.send(ch.target, ch.id, "one")
    assert calls(ch)[0]["cwd"] == os.path.realpath(wt)
    assert chat.view(ch.target, ch.id)["cwd"] == "worktree"
    wt.rmdir()
    second = chat.send(ch.target, ch.id, "two")
    argv = calls(ch)[1]["argv"]
    assert "--resume" not in argv and flag(argv, "--session-id") != flag(calls(ch)[0]["argv"], "--session-id")
    assert calls(ch)[1]["cwd"] == os.path.realpath(ch.target.path)
    assert second["cost_usd"] == 0.04  # a new session: no base


def test_one_live_turn_per_ticket_and_a_global_limit(ch, monkeypatch):
    other = ticket.create(ch.target, "Another", "feat", "body", False, "low")["id"]
    play(ch, {"sleep": 30}, {"cost": 0.01})
    th, out = in_thread(chat.send, ch.target, ch.id, "slow")
    wait_for(lambda: pid_file(ch) and calls(ch))
    with pytest.raises(PipelineError, match="still being answered"):
        chat.check(ch.target, ch.id, "again")
    with pytest.raises(PipelineError, match="still being answered"):
        chat.send(ch.target, ch.id, "again")
    chat.check(ch.target, other, "fine")  # another ticket may ask while slots are free
    monkeypatch.setattr(chat, "MAX_LIVE", 1)
    with pytest.raises(PipelineError, match="1 chats are being answered already"):
        chat.check(ch.target, other, "full")
    with pytest.raises(PipelineError, match="1 chats are being answered already"):
        chat.send(ch.target, other, "full")
    chat.stop(ch.target, ch.id)
    th.join(20)
    assert out["value"]["state"] == "killed"
    assert chat.send(ch.target, other, "now")["state"] == "ok"
    assert len(calls(ch)) == 2  # the refused sends never started claude


def test_timeout_kills_the_reply(ch):
    ch.settings.timeout = 1
    play(ch, {"sleep": 30})
    started = time.monotonic()
    turn = chat.send(ch.target, ch.id, "slow")
    assert time.monotonic() - started < 15
    assert turn["state"] == "killed" and turn["error"] == "timed out after 1s"
    assert pid_file(ch) is None
    assert turn["steps_total"] == 0 and turn["answer"] is None


def test_draft_is_rebuilt_from_main_thread_text_deltas(ch):
    play(ch, {"cost": 0.01, "text": "the answer is here", "hold": 3})
    th, out = in_thread(chat.send, ch.target, ch.id, "what changed?")
    live = wait_for(lambda: (v := chat.view(ch.target, ch.id))["turns"] and v["turns"][-1]["draft"] and v)
    turn = live["turns"][-1]
    assert live["live"] is True and live["live_turn"] == turn["id"]
    assert turn["draft"] == "the answer is here "  # subagent deltas and tool-input deltas left out
    assert [(s["kind"], s.get("name"), s.get("error")) for s in turn["steps"]] == [("tool", "Bash", True)]
    th.join(20)
    done = chat.view(ch.target, ch.id)["turns"][-1]
    assert done["state"] == "ok" and done["draft"] is None and done["answer"] == "the answer is here"
    assert [s["kind"] for s in done["steps"]] == ["tool"]  # the final text is the answer, not a step


def test_no_partial_messages_without_cli_support(ch, monkeypatch):
    monkeypatch.setattr(dispatch, "_HELP", {"text": ""})
    play(ch, {"cost": 0.01})
    assert chat.send(ch.target, ch.id, "q")["state"] == "ok"
    assert "--include-partial-messages" not in calls(ch)[0]["argv"]


def test_failures_carry_their_reason(ch):
    play(ch, {"no_result": True}, {"error": "rate limited"})
    crashed = chat.send(ch.target, ch.id, "one")
    assert crashed["state"] == "failed" and crashed["error"] == "exit code 1; stderr: boom"
    errored = chat.send(ch.target, ch.id, "two")
    assert errored["state"] == "failed" and errored["error"] == "agent error: rate limited"
    assert errored["cost_usd"] is None


def test_a_dead_turn_reads_lost_and_is_settled_by_the_next(ch):
    d = state.run_dir(ch.target, ch.id) / "chat"
    d.mkdir()
    orphan = {"id": "20260101-000000-000000", "thread": "20260101-000000-000000", "ordinal": 1, "text": "q",
              "state": "running", "session_id": "11111111-2222-3333-4444-555555555555", "cwd": str(ch.target.path)}
    state.write_json(d / "turns.json", [orphan])
    view = chat.view(ch.target, ch.id)
    assert view["live"] is False and view["turns"][0]["state"] == "lost" and "disappeared" in view["turns"][0]["error"]
    with pytest.raises(LookupError):
        chat.stop(ch.target, ch.id)
    play(ch, {"cost": 0.01})
    chat.send(ch.target, ch.id, "again")
    turns = state.read_json(d / "turns.json")
    assert turns[0]["state"] == "lost" and turns[1]["state"] == "ok"
    assert "--resume" not in calls(ch)[0]["argv"]  # the lost turn never showed its session


def test_validation(ch):
    with pytest.raises(PipelineError, match="empty message"):
        chat.send(ch.target, ch.id, "   ")
    with pytest.raises(PipelineError, match="at most 8000"):
        chat.check(ch.target, ch.id, "x" * 8001)
    with pytest.raises(PipelineError, match='model "gpt" is not offered'):
        chat.send(ch.target, ch.id, "q", model="gpt")
    with pytest.raises(LookupError):
        chat.send(ch.target, "local-99", "q")
    assert calls(ch) == []


def test_dash_question_is_not_an_option(ch):
    play(ch, {"cost": 0.01})
    chat.send(ch.target, ch.id, "--help me read this")
    assert flag(calls(ch)[0]["argv"], "-p") == "\n--help me read this"


def test_system_prompt_lists_runs_and_rounds(ch):
    phase = state.new_phase_dir(ch.target, ch.id, "review")
    state.write_json(phase / "run.json", {"stage": "review", "run": 1, "visit": 2, "state": "ok", "verdict": "CHANGES",
                                          "route": "fix", "finished_at": "x", "result": {"summary": "two findings"}})
    st = state.load_state(ch.target, ch.id)
    st["run"] = {"n": 1, "started_at": state.now(), "finished_at": None, "outcome": None, "error": None,
                 "base_commit": "abcdef0123456789"}
    st["status"], st["stage"] = "running", "implement"
    st["rounds"] = [{"n": 1, "questions": [{"text": "Which page?"}], "answer": "The login page", "by": "dev"},
                    {"n": 2, "questions": [{"text": "Keep the old flow?"}], "answer": None}]
    state.save_state(ch.target, ch.id, st)
    text = chat.system_prompt(ch.target, ch.id, ch.target.path)
    assert "- run 1 · started" in text and "· at implement" in text
    assert f"  - review #2 ok ({phase.name}) [CHANGES] route fix — two findings" in text
    assert "Base commit: abcdef0123456789 (the ticket's changes are abcdef0123..HEAD)" in text
    assert "- round 1: Which page? — answered by dev: The login page" in text
    assert "- round 2: Keep the old flow? — not answered yet" in text
    assert "state: running" in text and "The ticket has no worktree" in text


def test_session_ids_cover_every_chat(ch):
    play(ch, {"cost": 0.01}, {"cost": 0.02})
    chat.send(ch.target, ch.id, "one")
    chat.send(ch.target, ch.id, "two", new_thread=True)
    sids = {flag(c["argv"], "--session-id") for c in calls(ch)}
    assert chat.session_ids(ch.target) == sids


def _worktree(ch):
    wt = ch.tmp / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "chat-wt", str(wt)], cwd=ch.target.path, check=True, capture_output=True)
    st = state.load_state(ch.target, ch.id)
    st["worktree"] = str(wt)
    state.save_state(ch.target, ch.id, st)
    return wt


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True, text=True).stdout


def test_files_a_turn_writes_in_the_worktree_are_stashed(ch):
    wt = _worktree(ch)
    (wt / "half.txt").write_text("left by an earlier stage\n")  # already dirty: not the chat's
    play(ch, {"cost": 0.01, "write": "notes.txt"})
    turn = chat.send(ch.target, ch.id, "look around")
    assert turn["state"] == "ok"
    assert turn["changed_files"] == ["notes.txt"] and turn["stash"] and turn["head_moved"] is None
    argv = calls(ch)[0]["argv"]
    assert "Bash" not in denied(argv) and "Bash(git commit:*)" in denied(argv)  # a shell, but no git writes
    assert "No shell in this mode" not in flag(argv, "--append-system-prompt")
    assert _git(wt, "status", "--porcelain") == ""  # the next stage finds a clean tree
    assert "ziggy: chat turn " + turn["id"] + " changed files" in _git(wt, "stash", "list")
    assert chat.view(ch.target, ch.id)["turns"][-1]["stash"] == turn["stash"]


def test_a_commit_is_recorded_not_reset(ch):
    wt = _worktree(ch)
    head = _git(wt, "rev-parse", "HEAD").strip()
    play(ch, {"cost": 0.01, "commit": "slipped.txt"})
    turn = chat.send(ch.target, ch.id, "commit please")
    after = _git(wt, "rev-parse", "HEAD").strip()
    assert after != head and turn["head_moved"] == {"before": head, "after": after}
    assert turn["changed_files"] == [] and turn["stash"] is None


def test_no_stash_while_a_pipeline_process_holds_the_ticket(ch):
    wt = _worktree(ch)
    play(ch, {"cost": 0.01, "write": "notes.txt"})
    with state.lock(ch.target, f"ticket-{ch.id}", "a stage"):
        turn = chat.send(ch.target, ch.id, "while it runs")
    assert turn["changed_files"] == ["notes.txt"] and turn["stash"] is None and "pipeline process" in turn["stash_skipped"]
    assert (wt / "notes.txt").exists()


def test_the_main_checkout_is_not_watched(ch):
    play(ch, {"cost": 0.01, "write": "notes.txt"})
    turn = chat.send(ch.target, ch.id, "no worktree")
    assert turn["changed_files"] == [] and turn["stash"] is None
