import calendar
import json
import os
import time
from pathlib import Path

import pytest

from ziggy import config as ziggy_config
from ziggy import sessions
from ziggy.config import Target

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SID3 = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    sessions._files.clear()
    sessions._identity.clear()
    sessions._runs.clear()
    calls = []

    def fake_ps(pid):
        calls.append(pid)
        return PS.get(pid)

    monkeypatch.setattr(sessions, "_ps", fake_ps)
    monkeypatch.setattr(sessions, "_pid_alive", lambda pid: pid in ALIVE)
    PS.clear()
    ALIVE.clear()
    return calls


PS: dict = {}
ALIVE: set = set()
START = calendar.timegm((2026, 10, 13, 22, 17, 42, 0, 0, 0))


def claude_dir(tmp_path) -> Path:
    return tmp_path / "home" / ".claude"


def write_transcript(config: Path, cwd: str, sid: str, lines: list[dict], mtime: float | None = None) -> Path:
    folder = config / "projects" / sessions.encode(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def chat(cwd, prompt="fix the login bug", model="claude-opus-5", branch="main"):
    return [
        {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": "2026-10-13T22:17:44Z",
         "message": {"content": prompt}},
        {"type": "assistant", "cwd": cwd, "message": {"model": model, "content": [{"type": "text", "text": "on it"}]}},
    ]


def registry(config: Path, pid: int, sid=SID, cwd="/tmp", status="busy", **extra):
    (config / "sessions").mkdir(parents=True, exist_ok=True)
    entry = {"pid": pid, "sessionId": sid, "cwd": cwd, "startedAt": START * 1000 + 1500,
             "procStart": "Tue Oct 13 22:17:42 2026", "status": status, "entrypoint": "cli", **extra}
    (config / "sessions" / f"{pid}.json").write_text(json.dumps(entry))


def target(tmp_path, name="app", **kw) -> Target:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return Target(name=name, path=path, **kw)


def test_parse_registry_entry_rejections():
    good = {"pid": 42, "sessionId": SID, "cwd": "/x"}
    assert sessions.parse_registry_entry(good)["status"] is None
    for bad in ({**good, "pid": 1}, {**good, "pid": True}, {**good, "pid": "42"}, {**good, "sessionId": "nope"},
                {**good, "cwd": "rel/x"}, [], {**good, "cwd": None}):
        assert sessions.parse_registry_entry(bad) is None
    assert sessions.parse_registry_entry({**good, "status": "shell"})["status"] is None


def test_live_session_listed_and_key_never_opened(tmp_path):
    config = claude_dir(tmp_path)
    cwd = str(tmp_path / "work")
    os.mkdir(cwd)
    registry(config, 500, cwd=cwd, name="my session")
    key = config / "sessions" / f"500.{'a' * 64}.key"
    key.write_text("secret")
    key.chmod(0)
    (config / "sessions" / "501.json").write_text("{bad json")
    registry(config, 502, sid=SID2, cwd=cwd)
    (config / "sessions" / "503.json").write_text((config / "sessions" / "502.json").read_text())  # pid mismatch
    write_transcript(config, cwd, SID, chat(cwd))
    ALIVE.update({500, 502})
    PS[500] = (START, "claude --foo")
    PS[502] = (START, "node something")  # not a claude process: pid reused
    try:
        out = sessions.listing([])
    finally:
        key.chmod(0o600)
    assert [s["id"] for s in out] == [SID]
    s = out[0]
    assert s["status"] == "working" and s["title"] == "my session" and s["model"] == "claude-opus-5"
    assert s["branch"] == "main" and s["config"] == "claude" and s["has_transcript"]
    assert s["resume"] == f"cd {cwd} && claude --resume {SID}"
    assert s["last_prompt"] is None and s["target"] is None


def test_liveness_start_time(tmp_path, env):
    config = claude_dir(tmp_path)
    registry(config, 600, status="idle")
    ALIVE.add(600)
    PS[600] = (START + 3600, "claude")  # same pid, different process
    assert sessions.listing([]) == []
    PS[600] = (START + 1, "claude")
    sessions._identity.clear()
    assert [s["status"] for s in sessions.listing([])] == ["idle"]
    calls = len(env)
    sessions.listing([])
    assert len(env) == calls  # ps asked once per pid + start
    ALIVE.clear()
    assert sessions.listing([]) == []
    assert 600 not in sessions._identity


def test_ended_window_conversation_and_order(tmp_path):
    config = claude_dir(tmp_path)
    now = time.time()
    write_transcript(config, "/a", SID, chat("/a"), mtime=now - 3600)
    write_transcript(config, "/b", SID2, chat("/b"), mtime=now - 5 * 86400)  # outside 3 days
    write_transcript(config, "/c", SID3, [{"type": "user", "cwd": "/c", "message": {"content": "<system-reminder>x</system-reminder>"}}])
    out = sessions.listing([], now=now)
    assert [(s["id"], s["status"], s["title"], s["title_source"]) for s in out] == [(SID, "ended", "fix the login bug", "prompt")]
    assert out[0]["resume"] is None and not out[0]["cwd_exists"]
    assert [s["id"] for s in sessions.listing([], recent_days=7, now=now)] == [SID, SID2]


def test_own_sessions_excluded(tmp_path):
    config = claude_dir(tmp_path)
    t = target(tmp_path)
    phase = t.state_dir / "runs" / "T-1" / "01-plan"
    phase.mkdir(parents=True)
    (phase.parent / "ticket.json").write_text("{}")
    (phase / "run.json").write_text(json.dumps({"session_id": SID}))
    write_transcript(config, "/a", SID, chat("/a"))
    write_transcript(config, "/b", SID2, chat("/b", prompt="do it\n\n## Result protocol (ziggy)\nwrite json"))
    later = chat("/c") + [{"type": "user", "message": {"content": "quote: ## Result protocol (ziggy)"}}]
    write_transcript(config, "/c", SID3, later)
    registry(config, 700, sid=SID)
    ALIVE.add(700)
    PS[700] = (START, "claude")
    assert [s["id"] for s in sessions.listing([t])] == [SID3]


def test_chat_sessions_excluded(tmp_path):
    from ziggy import chat as chat_mod

    chat_mod._ids.clear()
    config = claude_dir(tmp_path)
    t = target(tmp_path)
    worktree = str(t.state_dir / "worktrees" / "T-1")
    turns = t.state_dir / "runs" / "T-1" / "chat" / "turns.json"
    turns.parent.mkdir(parents=True)
    (turns.parent.parent / "ticket.json").write_text("{}")
    turns.write_text(json.dumps([{"id": "20261016-101500-000001", "session_id": SID2.upper(), "cwd": worktree}]))
    write_transcript(config, worktree, SID2, chat(worktree))
    write_transcript(config, "/c", SID3, chat("/c"))
    assert [s["id"] for s in sessions.listing([t])] == [SID3]
    turns.write_text(json.dumps([]))
    os.utime(turns, ns=(time.time_ns(), time.time_ns() + 10**9))  # a new mtime: the cache re-reads
    assert {s["id"] for s in sessions.listing([t])} == {SID2, SID3}


def test_cwd_mapping_to_target_and_ticket(tmp_path):
    config = claude_dir(tmp_path)
    t = target(tmp_path)
    run = t.state_dir / "runs" / "T-3"
    run.mkdir(parents=True)
    (run / "ticket.json").write_text("{}")
    worktree = t.state_dir / "worktrees" / "T-3"
    worktree.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"worktree": str(worktree)}))
    sub = str(t.path / "src")
    write_transcript(config, sub, SID, chat(sub))
    write_transcript(config, str(worktree), SID2, chat(str(worktree)))
    out = {s["id"]: (s["target"], s["ticket"]) for s in sessions.listing([t])}
    assert out == {SID: ("app", None), SID2: ("app", "T-3")}


def test_second_config_dir_resume_prefix(tmp_path):
    other = tmp_path / "home" / ".claude-work"
    t = target(tmp_path, env={"CLAUDE_CONFIG_DIR": str(other)})
    write_transcript(other, str(tmp_path), SID, chat(str(tmp_path)))
    write_transcript(claude_dir(tmp_path), str(tmp_path), SID, chat(str(tmp_path)))  # same id: listed once
    assert set(sessions.config_dirs([t])) == {"claude", "claude-work"}
    out = sessions.listing([t])
    assert len(out) == 1
    work = sessions.resume_command(str(tmp_path), SID, other)
    assert work == f"cd {tmp_path} && CLAUDE_CONFIG_DIR={other} claude --resume {SID}"


def test_browser_wrapper_and_commands():
    content = [{"type": "text", "text": "<browser_instruction>stuff</browser_instruction>"}, {"type": "text", "text": "real prompt"}]
    assert sessions.prompt_text(content) == "real prompt"
    assert sessions.prompt_text("<command-name>/review</command-name><command-args>42</command-args>") == "/review 42"
    assert sessions.prompt_text("<bash-input>ls</bash-input>") == "! ls"
    assert sessions.prompt_text("<local-command-stdout>x</local-command-stdout>") is None
    assert sessions.prompt_text([{"type": "image"}, {"type": "text", "text": "see"}]) == "[image] see"


def test_bounded_reads_and_cache(tmp_path, monkeypatch):
    config = claude_dir(tmp_path)
    filler = {"type": "user", "isMeta": True, "message": {"content": "x" * 1000}}
    lines = chat("/a") + [filler] * 400 + [{"type": "ai-title", "aiTitle": "Middle title"}] + [filler] * 4000
    lines += [{"type": "last-prompt", "lastPrompt": "latest ask"}, {"type": "assistant", "gitBranch": "feat", "message": {"model": "m2"}}]
    path = write_transcript(config, "/a", SID, lines)
    reads = []
    real = sessions._read_at
    monkeypatch.setattr(sessions, "_read_at", lambda p, start, n: reads.append(n) or real(p, start, n))
    s = sessions.listing([])[0]
    assert (s["title"], s["last_prompt"], s["branch"], s["model"]) == ("Middle title", "latest ask", "feat", "m2")
    size = path.stat().st_size
    assert reads[:2] == [sessions.HEAD_BYTES, sessions.TAIL_BYTES] and sum(reads) < size
    reads.clear()
    sessions.listing([])
    assert reads == []  # size + mtime unchanged: cached
    with path.open("a") as f:
        f.write(json.dumps({"type": "last-prompt", "lastPrompt": "newer"}) + "\n")
    s = sessions.listing([])[0]
    assert reads == [sessions.TAIL_BYTES] and s["last_prompt"] == "newer" and s["title"] == "Middle title"


def test_transcript_paging(tmp_path, monkeypatch):
    config = claude_dir(tmp_path)
    lines = chat("/a") + [{"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "side"}]}},
                          {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}]
    path = write_transcript(config, "/a", SID, lines)
    page = sessions.transcript(config, SID)
    assert [i["kind"] for i in page["items"]] == ["user", "text", "result"]
    assert page["items"][0]["text"] == "fix the login bug" and not page["more"] and not page["truncated"]
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "again"}}) + "\n" + '{"type": "user", "mess')
    nxt = sessions.transcript(config, SID, page["pos"])
    assert [i["text"] for i in nxt["items"]] == ["again"]  # the partial last line waits
    assert sessions.transcript(config, "../../etc/passwd") is None
    assert sessions.transcript(config, SID2) is None
    monkeypatch.setattr(sessions, "STREAM_BYTES", 100)
    monkeypatch.setattr(sessions, "PAGE_ITEMS", 1)
    tail = sessions.transcript(config, SID)
    assert tail["truncated"] and tail["pos"] > path.stat().st_size - 200


def test_symlinked_transcript_ignored(tmp_path):
    config = claude_dir(tmp_path)
    real = write_transcript(tmp_path / "elsewhere", "/a", SID, chat("/a"))
    folder = config / "projects" / "-a"
    folder.mkdir(parents=True)
    (folder / f"{SID}.jsonl").symlink_to(real)
    assert sessions.listing([]) == []
    assert sessions.transcript(config, SID) is None


def test_sessions_named_by_hide_cwd_prefixes_are_hidden_and_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(ziggy_config, "load_sessions",
                        lambda: ziggy_config.SessionsConfig(hide_cwd_prefixes=("helper-flush-", "helper-compile-stage-")))
    config = claude_dir(tmp_path)
    tmp = tmp_path / "T"
    flush, compile_, normal, inner = (str(tmp / "helper-flush-a1B2"), str(tmp / "helper-compile-stage-x9"),
                                      str(tmp / "scratch"), str(tmp / "helper-flush-zz" / "work"))
    write_transcript(config, flush, SID, chat(flush))
    write_transcript(config, normal, SID2, chat(normal))
    write_transcript(config, inner, "22222222-2222-3333-4444-555555555555", chat(inner))  # only a parent dir matches: kept
    registry(config, 900, sid=SID3, cwd=compile_ + "/", status="busy")  # live, trailing slash
    ALIVE.add(900)
    PS[900] = (START, "claude")
    counts = {}
    out = sessions.listing([], counts=counts)
    assert sorted(s["cwd"] for s in out) == sorted([normal, inner])
    assert counts == {"hidden": 2}
    prefixes = ("helper-flush-", "helper-compile-stage-")
    assert sessions.hidden_session("/x/helper-compile-stage-1", prefixes) and not sessions.hidden_session("/x/my-helper-flush-1", prefixes)
    assert not sessions.hidden_session("/x/helper-flush-1", ())  # nothing configured: nothing hidden
    lines = []
    sessions.print_listing([], out=lines.append)
    assert lines[-1] == "2 session(s) hidden ([sessions] hide_cwd_prefixes)"


def test_print_listing(tmp_path):
    config = claude_dir(tmp_path)
    cwd = str(tmp_path)
    write_transcript(config, cwd, SID, chat(cwd))
    registry(config, 800, sid=SID, cwd=cwd, status="idle")
    ALIVE.add(800)
    PS[800] = (START, "claude")
    lines = []
    sessions.print_listing([], out=lines.append)
    assert lines[0].split()[:2] == ["STATUS", "WHERE"]
    assert lines[1].startswith("idle") and "terminal" in lines[1]
    assert lines[-1].startswith('resume "fix the login bug": cd ')
