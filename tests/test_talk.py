"""Sohbet (talk.py) against tests/fake_claude_chat.py: read chats in the main checkout, write chats in a worktree
of their own, resume, MCP on, deleting with and without work left, and the panel's routes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git
from test_chat import calls, denied, flag, play
from test_dashboard import panel, request  # noqa: F401 - the panel fixture

from ziggy import chat, config, dispatch, sessions, talk
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
    return SimpleNamespace(target=config.load_target("demo"), tmp=tmp_path, env=env)


def test_a_read_chat_resumes_its_session_with_mcp_on_and_no_shell(ch):
    play(ch, {"cost": 0.04, "text": "YouTrack says 20"}, {"cost": 0.06})
    meta = talk.create(ch.target, "read")
    first = talk.send(ch.target, meta["id"], "how many issues are open?")
    second = talk.send(ch.target, meta["id"], "and the oldest?", model="sonnet")
    assert [first["state"], second["state"]] == ["ok", "ok"] and first["answer"] == "YouTrack says 20"
    a, b = (c["argv"] for c in calls(ch))
    assert flag(b, "--resume") == flag(a, "--session-id") and flag(b, "--model") == "sonnet"
    assert "--strict-mcp-config" not in a  # the login's connectors (YouTrack) load
    for tool in ("Edit", "Write", "Bash", "Bash(git commit:*)", "AskUserQuestion", "RemoteTrigger",
                 "mcp__*__update_*", "mcp__*__send_*", "mcp__*__slack_send_*", "mcp__*__reply"):
        assert tool in denied(a)  # connectors read, they do not act
    assert not any(t.startswith(("mcp__*__get", "mcp__*__list", "mcp__*__search")) for t in denied(a))
    system = flag(a, "--append-system-prompt")
    assert system.startswith("# Ziggy: Sohbet") and "You cannot edit files or run shell commands" in system
    assert calls(ch)[0]["cwd"] == os.path.realpath(ch.target.path) and calls(ch)[0]["env"]["PIPELINE_STAGE"] == "talk"
    assert (first["cost_usd"], second["cost_usd"]) == (0.04, 0.02)

    [listed] = talk.list_chats(ch.target)
    assert listed["id"] == meta["id"] and listed["title"] == "how many issues are open?" and listed["turns"] == 2
    view = talk.view(ch.target, meta["id"])
    assert [t["question"] for t in view["turns"]] == ["how many issues are open?", "and the oldest?"]
    assert view["cwd"] == "main" and view["changes"] is None and view["resume_command"]
    assert flag(a, "--session-id").lower() in sessions.own_session_ids([ch.target])
    draft = talk.transcript(ch.target, meta["id"])
    assert draft["title"] == "how many issues are open?" and "**Yanıt:** YouTrack says 20" in draft["body"]


def test_a_write_chat_works_in_its_own_prepared_worktree(ch):
    play(ch, {"commit": "notes.txt"}, {"write": "draft.txt"})
    meta = talk.create(ch.target, "write")
    first = talk.send(ch.target, meta["id"], "add notes")
    wt = Path(talk.load(ch.target, meta["id"])["worktree"])
    assert first["state"] == "ok" and first["head_moved"] and calls(ch)[0]["cwd"] == os.path.realpath(wt)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).strip() == f"chat/{meta['id']}"
    assert (ch.target.state_dir / f"prepared-talk-{meta['id']}").exists()  # the target's prepare ran once
    deny = denied(calls(ch)[0]["argv"])
    assert "Bash" not in deny and "Edit" not in deny and "Bash(git checkout:*)" in deny and "Bash(git push:*)" in deny
    assert "mcp__*__update_*" not in deny  # a write chat may act through the connectors
    assert "You work in this chat's own git worktree" in flag(calls(ch)[0]["argv"], "--append-system-prompt")

    second = talk.send(ch.target, meta["id"], "draft more")
    assert second["changed_files"] == ["draft.txt"] and not second["head_moved"]
    assert talk.view(ch.target, meta["id"])["changes"] == {"ahead": 1, "dirty": ["draft.txt"]}
    assert not (ch.target.state_dir / "nothing").exists()

    with pytest.raises(PipelineError, match="still holds work"):
        talk.delete(ch.target, meta["id"])
    talk.delete(ch.target, meta["id"], force=True)
    assert not wt.exists() and talk.list_chats(ch.target) == []
    assert git("branch", "--list", f"chat/{meta['id']}", cwd=ch.target.path).strip() == ""


def test_validation_and_missing_chats(ch):
    with pytest.raises(PipelineError, match="mode must be"):
        talk.create(ch.target, "admin")
    meta = talk.create(ch.target)
    with pytest.raises(PipelineError, match="empty message"):
        talk.send(ch.target, meta["id"], "  ")
    with pytest.raises(PipelineError, match="not offered"):
        talk.send(ch.target, meta["id"], "hi", model="gpt")
    with pytest.raises(LookupError):
        talk.send(ch.target, "20990101-000000", "hi")
    with pytest.raises(LookupError):
        talk.load(ch.target, "../etc")
    with pytest.raises(LookupError, match="nothing is being answered"):
        talk.stop(ch.target, meta["id"])


def test_panel_creates_lists_and_deletes_chats(ch, panel):  # noqa: F811
    play(ch, {"text": "hello back"})
    code, out, _ = request(panel, "POST", "/api/talk/demo", {"mode": "read", "text": "hello"})
    assert code == 200 and out["job"]["command"] == "talk send"
    chat_id = out["chat"]["id"]
    deadline = 200
    while deadline and not (talk.view(ch.target, chat_id)["turns"] and talk.view(ch.target, chat_id)["turns"][-1]["state"] == "ok"):
        __import__("time").sleep(0.1)
        deadline -= 1
    code, listing, _ = request(panel, "GET", "/api/talk/demo")
    assert code == 200 and [c["id"] for c in listing["chats"]] == [chat_id] and listing["models"] == list(chat.MODELS)
    code, view, _ = request(panel, "GET", f"/api/talk/demo/{chat_id}")
    assert code == 200 and view["turns"][-1]["answer"] == "hello back"
    assert request(panel, "GET", f"/api/talk/demo/{chat_id}/ticket")[1]["title"] == "hello"
    code, out, _ = request(panel, "POST", f"/api/talk/demo/{chat_id}", {"action": "stop"})
    assert code == 409 and "nothing is being answered" in out["error"]
    assert request(panel, "POST", "/api/talk/demo", {"mode": "nope"})[0] == 400
    assert request(panel, "POST", f"/api/talk/demo/{chat_id}", {"action": "delete"})[0] == 200
    assert request(panel, "GET", f"/api/talk/demo/{chat_id}")[0] == 404
    assert json.loads(json.dumps(request(panel, "GET", "/api/talk/demo")[1]))["chats"] == []
