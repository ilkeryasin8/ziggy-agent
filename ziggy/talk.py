"""Free chat ("Sohbet"): a conversation with Claude about a project, not tied to a ticket; each message is one
headless `claude -p` turn that resumes the conversation's session, like a Claude Code session typed into.

A chat is read-only or writes (its mode, fixed when it starts):
    read    the target's main checkout: Read/Grep/Glob and the rest, no editing tools and no Bash (Bash could
            write the developer's own checkout under the sandbox)
    write   the chat's own worktree on branch chat/<id> from <remote>/<base>, prepared like a ticket's: editing,
            Bash and commits; branch-moving git commands are denied and the pre-push hook still refuses a push
Both run under the stages' sandbox settings, but with MCP on: the claude.ai connectors of the target's Claude login
(YouTrack, …) and the repository's own MCP servers are there, so a question about an issue can be answered.

Turns, streams and the session handling are the ticket chat's (chat.py): one live turn per chat (`talk-<id>`) and
chat.MAX_LIVE across ticket chats and these together (the same `chat-live-<n>` slots).

Storage, under <state>/talk/<id>/:
    meta.json       id, title, mode, by, created/updated, branch and worktree once a write chat has them
    turns.json      every turn, oldest first (written only by the lock holder)
    <turn>.jsonl    the turn's claude stream; <turn>.err its stderr; <turn>.pid while it runs; <turn>.stop
    scratch/        the agent's --add-dir (it may write there; nothing here trusts it)
    worktree/       the write chat's worktree; prepare.log its prepare output
    jobs/           the panel's background jobs for this chat
"""
import getpass
import os
import re
import shutil
import signal
import subprocess
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path

from . import chat, config, dispatch, quota, state, worktree
from .config import Target
from .pipeline import PipelineError
from .worktree import GitError

MODES = ("read", "write")
CHAT_ID = re.compile(r"^\d{8}-\d{6}$")
ID_FORMAT = "%Y%m%d-%H%M%S"
MAX_TITLE = 80
LISTED = 60
TRANSCRIPT_TURNS = 12  # turns "Ticket'a çevir" puts in the ticket body
# Connectors look things up, they never act, in either mode: a chat that can read your mail or issues and also send
# or change them is one injected sentence away from leaking them. Two layers:
#   allowlist  once the login's MCP servers are known (from an earlier turn's init event, cached per login), a turn
#              runs with --permission-mode dontAsk and only the reading tools of those servers allowed
#              (`mcp__<server>__get_*`, …); any other tool that needs a permission, whatever its name, is refused.
#              (An allow rule takes a server's own prefix: `mcp__*__get_*` is not honoured, verified.)
#   deny list  always: every MCP tool whose name starts with a writing verb, on any server (deny rules do take
#              `mcp__*__send_*`); the only guard on a login's first turn, before its servers are known.
MCP_WRITE_VERBS = tuple(
    "create update delete add remove send reply forward post edit write set change assign link log manage merge push "
    "fork publish close archive move rename upload complete schedule respond request apply label unlabel mark unmark "
    "trash untrash approve invite share batch check uncheck save authenticate login logout run start stop cancel "
    "trigger execute deploy install enable disable toggle submit revoke grant transfer pay purchase book order import "
    "slack_send slack_create slack_update slack_add slack_schedule slack_complete issue_write".split())
MCP_READ_VERBS = tuple("get list search find read query slack_read slack_search slack_list slack_get".split())
MCP_READ_TOOLS = ("issue_read", "pull_request_read")  # GitHub's readers, named noun-first
MCP_DENY = (*(f"mcp__*__{verb}" for verb in MCP_WRITE_VERBS), *(f"mcp__*__{verb}_*" for verb in MCP_WRITE_VERBS))
# Write mode: the chat's branch stays where it is (no checkout, reset, rebase …); commits are the way to keep work.
# Neither mode schedules cloud agents of its own (RemoteTrigger) from a chat.
WRITE_DENY = (*(f"Bash(git {cmd}:*)" for cmd in "checkout switch reset rebase branch tag push worktree".split()), "RemoteTrigger",
              *MCP_DENY)
READ_DENY = (*chat.READ_ONLY_DENY, "Bash", "RemoteTrigger", *MCP_DENY)
BASE_ALLOW = ("Read", "Grep", "Glob", "ToolSearch", "TodoWrite")
WRITE_ALLOW = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
MCP_CACHE = "talk-mcp.json"  # under the shared state dir: {login key: [tool prefixes]}
BUSY = "the previous message is still being answered: wait for it or stop it"


# ---- storage ---------------------------------------------------------------------------------------

def root(target: Target) -> Path:
    return target.state_dir / "talk"


def _dir(target: Target, chat_id: str) -> Path:
    if not CHAT_ID.match(chat_id or ""):
        raise LookupError(f"no chat {chat_id!r}")
    return root(target) / chat_id


def load(target: Target, chat_id: str) -> dict:
    try:
        return state.read_json(_dir(target, chat_id) / "meta.json")
    except (OSError, ValueError):
        raise LookupError(f"no chat {chat_id} in {target.name}") from None


def _save_meta(target: Target, meta: dict) -> None:
    state.write_json(_dir(target, meta["id"]) / "meta.json", meta)


def _turns(target: Target, chat_id: str) -> list[dict]:
    try:
        turns = state.read_json(_dir(target, chat_id) / "turns.json")
    except (OSError, ValueError):
        return []
    return [t for t in turns if isinstance(t, dict) and chat.TURN_ID.match(str(t.get("id")))] if isinstance(turns, list) else []


def _save(target: Target, chat_id: str, turns: list[dict]) -> None:
    state.write_json(_dir(target, chat_id) / "turns.json", turns)


def _lock_name(chat_id: str) -> str:
    return f"talk-{chat_id}"


def _run_id(chat_id: str) -> str:
    """The id the target's env placeholders ({run_id}) and port ranges see for this chat."""
    return f"talk-{chat_id}"


def _live_turn(target: Target, chat_id: str, turns: list[dict]) -> dict | None:
    last = turns[-1] if turns else None
    if not last or last.get("state") not in chat.LIVE_STATES:
        return None
    if state.holder(target, _lock_name(chat_id)):
        return last
    pid = chat._pid(_dir(target, chat_id) / f"{last['id']}.pid")
    return last if pid and state._pid_alive(pid) else None


# ---- chats -----------------------------------------------------------------------------------------

def create(target: Target, mode: str = "read", title: str | None = None, by: str | None = None) -> dict:
    """A new, empty chat; a write chat gets its worktree with its first message (preparing can take minutes)."""
    if mode not in MODES:
        raise PipelineError(f"mode must be one of {', '.join(MODES)}")
    root(target).mkdir(parents=True, exist_ok=True)
    when = datetime.now()
    while True:
        chat_id = when.strftime(ID_FORMAT)
        try:
            (root(target) / chat_id).mkdir()
            break
        except FileExistsError:
            when += timedelta(seconds=1)
    now = state.now()
    meta = {"id": chat_id, "title": _title(title or ""), "mode": mode, "by": by or getpass.getuser(), "created_at": now,
            "updated_at": now, "branch": f"chat/{chat_id}" if mode == "write" else None, "base_branch": target.base_branch,
            "worktree": None, "prepared": False}
    _save_meta(target, meta)
    return meta


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= MAX_TITLE else line[: MAX_TITLE - 1] + "…"


def list_chats(target: Target) -> list[dict]:
    """The target's chats, the most recently used first."""
    out = []
    base = root(target)
    for d in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
        if not CHAT_ID.match(d.name):
            continue
        try:
            meta = load(target, d.name)
        except LookupError:
            continue
        turns = _turns(target, d.name)
        live = _live_turn(target, d.name, turns)
        out.append({**_public(meta), "turns": len(turns), "live": live is not None,
                    "cost_usd": round(sum(t.get("cost_usd") or 0 for t in turns), 4)})
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return out[:LISTED]


def session_ids(target: Target) -> set[str]:
    """Every session id the target's chats started, so session discovery (Oturumlar) leaves them out."""
    base = root(target)
    return {t["session_id"].lower() for d in (base.iterdir() if base.is_dir() else []) if CHAT_ID.match(d.name)
            for t in _turns(target, d.name) if isinstance(t.get("session_id"), str) and t["session_id"]}


def _public(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("id", "title", "mode", "by", "created_at", "updated_at", "branch", "base_branch", "worktree")}


# ---- one turn --------------------------------------------------------------------------------------

def _validate(target: Target, chat_id: str, text: str, model: str | None, models: tuple[str, ...]) -> tuple[dict, str]:
    meta = load(target, chat_id)
    text = (text or "").strip()
    if not text:
        raise PipelineError("empty message")
    if len(text) > chat.MAX_TEXT:
        raise PipelineError(f"message too long (at most {chat.MAX_TEXT} characters)")
    if model and model not in models:
        raise PipelineError(f'model "{model}" is not offered (chat models: {", ".join(models)})')
    return meta, text


def check(target: Target, chat_id: str, text: str, model: str | None = None) -> None:
    """What `send` would refuse, checked before the panel starts the job (the job re-checks under the locks)."""
    models, _ = chat._settings()
    _validate(target, chat_id, text, model, models)
    if _live_turn(target, chat_id, _turns(target, chat_id)) or state.holder(target, _lock_name(chat_id)):
        raise PipelineError(BUSY)
    shared = chat._shared()
    if all(state.holder(shared, f"chat-live-{n}") for n in range(1, chat.MAX_LIVE + 1)):
        raise chat._full()


def send(target: Target, chat_id: str, text: str, model: str | None = None) -> dict:
    """Send one message and wait for the answer (the CLI job's body); returns the finished turn."""
    models, timeout = chat._settings()
    meta, text = _validate(target, chat_id, text, model, models)
    with ExitStack() as stack:
        try:
            stack.enter_context(state.lock(target, _lock_name(chat_id), f"talk {chat_id}"))
        except state.LockError:
            raise PipelineError(BUSY) from None
        shared = chat._shared()
        for n in range(1, chat.MAX_LIVE + 1):
            try:
                stack.enter_context(state.lock(shared, f"chat-live-{n}", f"talk {target.name}/{chat_id}"))
                break
            except state.LockError:
                continue
        else:
            raise chat._full()
        return _run_turn(target, meta, text, model or models[0], timeout)


def _workdir(target: Target, meta: dict) -> Path:
    """Where the turn runs: the main checkout (read), else the chat's worktree, created and prepared once."""
    if meta["mode"] == "read":
        return target.path
    d = _dir(target, meta["id"])
    wt = d / "worktree"
    if not (meta.get("worktree") and wt.is_dir() and worktree.is_worktree(target, wt)):
        if wt.exists():
            raise PipelineError(f"{wt} exists but is not a worktree of {target.path}; remove it")
        if worktree.branch_taken(target, meta["branch"]):
            raise PipelineError(f"branch {meta['branch']} already exists in {target.path}; delete it or start a new chat")
        worktree.create(target, {"id": _run_id(meta["id"]), "branch": meta["branch"], "base_branch": meta["base_branch"]}, wt)
        meta.update(worktree=str(wt), prepared=False)
        _save_meta(target, meta)
    worktree.preserve(target, wt)
    if not meta.get("prepared"):
        worktree.run_prepare(target, _run_id(meta["id"]), wt, d / "prepare.log")
        meta["prepared"] = True
        _save_meta(target, meta)
    return wt


def _run_turn(target: Target, meta: dict, text: str, model: str, timeout: int) -> dict:
    chat_id = meta["id"]
    d = _dir(target, chat_id)
    turns = _turns(target, chat_id)
    for t in turns:  # left starting/running by a process that died: we hold the lock now
        if t.get("state") in chat.LIVE_STATES:
            t.update(state="lost", error=chat.LOST, finished_at=t.get("finished_at") or state.now())

    last = turns[-1] if turns else None
    prev = next((t for t in reversed(turns) if t.get("session_seen") and t.get("session_id")), None)
    turn_id = chat._new_turn_id(turns)
    session_id = prev["session_id"] if prev else str(uuid.uuid4())
    base = max((t["session_cost_usd"] for t in turns
                if t.get("session_id") == session_id and isinstance(t.get("session_cost_usd"), (int, float))),
               default=0.0) if prev else 0.0
    turn = {
        "id": turn_id, "thread": chat_id, "ordinal": (last.get("ordinal") or 0) + 1 if last else 1, "text": text,
        "by": getpass.getuser(), "state": "starting", "model": model, "cwd": None, "session_id": session_id,
        "resumed_from": prev["id"] if prev else None, "pid": None, "created_at": state.now(), "started_at": None,
        "finished_at": None, "exit_code": None, "kill_reason": None, "session_seen": False, "answer": None, "error": None,
        "cost_usd": None, "session_cost_usd": None, "cost_base_usd": base, "duration_ms": None, "num_turns": None,
    }
    turns.append(turn)
    _save(target, chat_id, turns)
    if not meta.get("title"):
        meta["title"] = _title(text)
    meta["updated_at"] = state.now()
    _save_meta(target, meta)

    try:
        cwd = _workdir(target, meta)
        turn["cwd"] = str(cwd)
        scratch = d / "scratch"
        scratch.mkdir(exist_ok=True)
        write = meta["mode"] == "write"
        # A message starting with "-" would read as an option after the boolean -p.
        prompt = "\n" + text if text.startswith("-") else text
        argv = [*dispatch.command(model, session_id, prompt, scratch, resume=bool(prev), budget_usd=target.budget_usd,
                                  tools_deny=WRITE_DENY if write else READ_DENY, mcp=True,
                                  tools_allow=_allowlist(target, write)),
                "--append-system-prompt", system_prompt(target, meta, cwd),
                *(["--include-partial-messages"] if dispatch.supports("--include-partial-messages") else [])]
        env = dispatch.environment(target, _run_id(chat_id), cwd, {
            "PIPELINE_STAGE": "talk", "PIPELINE_WORKTREE": str(cwd) if write else "", "GIT_OPTIONAL_LOCKS": "0"})
    except Exception as exc:
        turn.update(state="failed", error=f"could not prepare the turn: {exc}", finished_at=state.now())
        _save(target, chat_id, turns)
        return _turn_view(target, chat_id, turn, None)
    out_path, err_path, pid_path, stop_path = (d / f"{turn_id}{ext}" for ext in (".jsonl", ".err", ".pid", ".stop"))
    head_before = _head(cwd) if write else None
    kill_reason = None
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True)
        except OSError as exc:
            turn.update(state="failed", error=f"could not start claude: {exc}", finished_at=state.now())
            _save(target, chat_id, turns)
            return _turn_view(target, chat_id, turn, None)
        pid_path.write_text(f"{proc.pid}\n")
        turn.update(state="running", pid=proc.pid, started_at=state.now())
        _save(target, chat_id, turns)
        try:
            if stop_path.exists():  # Stop pressed while the turn was starting
                dispatch._end_group(proc)
            else:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    kill_reason = f"timed out after {chat._duration(timeout)}"
                    dispatch._end_group(proc)
        except BaseException:
            dispatch._end_group(proc)
            chat._finish(turn, out_path, err_path, proc.returncode, "interrupted")
            chat._observe_quota(target, out_path)
            _record_changes(turn, cwd, head_before)
            _save(target, chat_id, turns)
            raise
        finally:
            pid_path.unlink(missing_ok=True)
    if kill_reason is None and stop_path.exists():
        kill_reason = stop_path.read_text(encoding="utf-8").strip() or "stopped"
    chat._finish(turn, out_path, err_path, proc.returncode, kill_reason)
    chat._observe_quota(target, out_path)
    _remember_servers(target, out_path)
    _record_changes(turn, cwd, head_before)
    _save(target, chat_id, turns)
    meta["updated_at"] = state.now()
    _save_meta(target, meta)
    return _turn_view(target, chat_id, turn, None)


def _mcp_cache() -> Path:
    return config.state_home() / MCP_CACHE


def _tool_prefix(server: str) -> str:
    """`claude.ai Google Calendar` -> `mcp__claude_ai_Google_Calendar`: how Claude Code names a server's tools."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server)


def _known_servers(target: Target) -> list[str]:
    try:
        cached = state.read_json(_mcp_cache())
    except (OSError, ValueError):
        return []
    found = cached.get(quota.account_key(target)) if isinstance(cached, dict) else None
    return [p for p in found if isinstance(p, str) and p.startswith("mcp__")] if isinstance(found, list) else []


def _remember_servers(target: Target, out_path: Path) -> None:
    """The MCP servers the turn's init event listed, as tool prefixes cached for the login (the allowlist's input)."""
    try:
        events = dispatch.read_events(out_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    if not init or not isinstance(init.get("mcp_servers"), list):
        return
    prefixes = sorted({_tool_prefix(s["name"]) for s in init["mcp_servers"] if isinstance(s, dict) and isinstance(s.get("name"), str)})
    try:
        cached = state.read_json(_mcp_cache())
    except (OSError, ValueError):
        cached = {}
    cached = cached if isinstance(cached, dict) else {}
    key = quota.account_key(target)
    if cached.get(key) != prefixes:
        cached[key] = prefixes
        state.write_json(_mcp_cache(), cached)


def _allowlist(target: Target, write: bool) -> tuple[str, ...] | None:
    """The tools a turn may use under dontAsk, or None (deny list only) while the login's servers are unknown."""
    servers = _known_servers(target)
    if not servers:
        return None
    reads = [f"{p}__{verb}_*" for p in servers for verb in MCP_READ_VERBS] + [f"{p}__{name}" for p in servers for name in MCP_READ_TOOLS]
    return (*BASE_ALLOW, *(WRITE_ALLOW if write else ()), *reads)


def _head(wt: Path) -> str | None:
    try:
        return worktree.head(wt)
    except (GitError, OSError):
        return None


def _record_changes(turn: dict, cwd: Path, head_before: str | None) -> None:
    """A write chat's turn: the files left changed and the commits it made (nothing is undone)."""
    if head_before is None:
        return
    try:
        turn["changed_files"] = worktree.dirty_paths(cwd)[:50]
        after = worktree.head(cwd)
    except (GitError, OSError):
        return
    if after != head_before:
        turn["head_moved"] = {"before": head_before, "after": after}


def stop(target: Target, chat_id: str) -> dict:
    """End the chat's live reply; its session stays, so the next message resumes it."""
    load(target, chat_id)
    live = _live_turn(target, chat_id, _turns(target, chat_id))
    if not live:
        raise LookupError("nothing is being answered")
    d = _dir(target, chat_id)
    (d / f"{live['id']}.stop").write_text("stopped\n", encoding="utf-8")
    pid = chat._pid(d / f"{live['id']}.pid")
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"ok": True, "message": "stopping the reply", "turn": live["id"]}


def delete(target: Target, chat_id: str, force: bool = False) -> dict:
    """Remove the chat: its record, and for a write chat its worktree and branch. Work the branch still holds
    (uncommitted files, commits not on its base) is refused unless `force`: the refusal says what would go."""
    meta = load(target, chat_id)
    if _live_turn(target, chat_id, _turns(target, chat_id)) or state.holder(target, _lock_name(chat_id)):
        raise PipelineError(BUSY)
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    if wt and wt.is_dir():
        work = changes(target, meta)
        if (work["dirty"] or work["ahead"]) and not force:
            raise PipelineError(f"{meta['branch']} still holds work ({work['ahead']} commit(s) not on "
                                f"{meta['base_branch']}, {len(work['dirty'])} changed file(s)); delete anyway to lose it")
        with worktree.shared_git(target, f"remove chat {chat_id}"):
            worktree.git("worktree", "remove", "--force", str(wt), cwd=target.path, check=False)
    if meta.get("branch"):
        worktree.git("branch", "-D", meta["branch"], cwd=target.path, check=False)
    shutil.rmtree(_dir(target, chat_id), ignore_errors=True)
    return {"deleted": chat_id}


def changes(target: Target, meta: dict) -> dict:
    """A write chat's branch against its base: commits ahead and files left changed."""
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    if not wt or not wt.is_dir():
        return {"ahead": 0, "dirty": []}
    base = f"{target.remote}/{meta['base_branch']}"
    proc = worktree.git("rev-list", "--count", f"{base}..HEAD", cwd=wt, check=False)
    try:
        ahead = int(proc.stdout.strip()) if proc.returncode == 0 else 0
    except ValueError:
        ahead = 0
    try:
        dirty = worktree.dirty_paths(wt)
    except (GitError, OSError):
        dirty = []
    return {"ahead": ahead, "dirty": dirty[:50]}


# ---- reading ---------------------------------------------------------------------------------------

def _turn_view(target: Target, chat_id: str, turn: dict, live: dict | None) -> dict:
    stream = chat._stream(_dir(target, chat_id) / f"{turn['id']}.jsonl")
    state_, error = chat._effective(turn, live)
    steps = list(stream["steps"])
    is_live = turn is live
    if not is_live and turn.get("answer"):  # the last main-thread text is the answer, shown on its own
        last = next((i for i in range(len(steps) - 1, -1, -1) if steps[i]["kind"] == "text" and not steps[i]["parent"]), None)
        if last is not None:
            del steps[last]
    return {
        "id": turn["id"], "thread": chat_id, "new_thread": False, "ordinal": turn.get("ordinal"),
        "question": turn.get("text"), "by": turn.get("by"), "state": state_, "answer": turn.get("answer"), "error": error,
        "draft": stream["draft"] if is_live else None, "model": turn.get("model"), "cost_usd": turn.get("cost_usd"),
        "duration_ms": turn.get("duration_ms"), "created_at": turn.get("created_at"), "started_at": turn.get("started_at"),
        "finished_at": turn.get("finished_at"), "steps": steps[-chat.STEPS_SHOWN:], "steps_total": len(steps),
        "changed_files": turn.get("changed_files") or [], "head_moved": turn.get("head_moved"),
        "stash": None, "stash_skipped": None,
    }


def view(target: Target, chat_id: str, since: str | None = None) -> dict:
    """The chat: its newest turns (from turn `since` on, for a poll), the live state, the models offered, and for a
    write chat its branch and what it holds."""
    meta = load(target, chat_id)
    models, _ = chat._settings()
    turns = _turns(target, chat_id)
    live = _live_turn(target, chat_id, turns)
    shown = turns[-chat.TURNS_SHOWN:]
    if since and chat.TURN_ID.match(since):
        shown = [t for t in shown if t["id"] >= since]
    resumable = next((t for t in reversed(turns) if t.get("session_seen") and t.get("session_id") and t.get("cwd")), None)
    return {
        "chat": _public(meta),
        "turns": [_turn_view(target, chat_id, t, live) for t in shown],
        "live": live is not None,
        "live_turn": live["id"] if live else None,
        "truncated": len(turns) > chat.TURNS_SHOWN,
        "models": list(models),
        "model": models[0],
        "cwd": "worktree" if meta["mode"] == "write" else "main",
        "changes": changes(target, meta) if meta["mode"] == "write" else None,
        "resume_command": chat._resume_command(target, resumable) if resumable else None,
    }


def transcript(target: Target, chat_id: str) -> dict:
    """The chat as a ticket draft ("Ticket'a çevir"): its title and the last turns, for the composer to edit."""
    meta = load(target, chat_id)
    turns = [t for t in _turns(target, chat_id) if t.get("text")][-TRANSCRIPT_TURNS:]
    parts = [f"Bu ticket bir Sohbet'ten çıktı ({target.name}, {meta['id']})."]
    if meta.get("branch") and meta.get("worktree"):
        work = changes(target, meta)
        parts.append(f"Sohbetin kendi dalı `{meta['branch']}`: {work['ahead']} commit, {len(work['dirty'])} değişmiş dosya.")
    for t in turns:
        parts.append(f"**Soru:** {t['text']}")
        if t.get("answer"):
            parts.append(f"**Yanıt:** {chat._clip(t['answer'], 1500) if len(t['answer']) > 1500 else t['answer']}")
    return {"title": meta.get("title") or "", "body": "\n\n".join(parts)}


# ---- system prompt ---------------------------------------------------------------------------------

def system_prompt(target: Target, meta: dict, cwd: Path) -> str:
    lines = [
        "# Ziggy: Sohbet",
        "",
        f"You are talking with the developer about the project `{target.name}` "
        f"({target.github_repo or target.path}). This is a free conversation, not a ticket run: answer what they ask, "
        "in their language, as you would in an interactive Claude Code session.",
        "",
    ]
    if meta["mode"] == "write":
        lines += [
            f"You work in this chat's own git worktree `{cwd}` on branch `{meta['branch']}`, made from "
            f"`{target.remote}/{meta['base_branch']}`. Edit files and run commands there when they ask for changes; "
            "commit finished pieces with a clear message. Never switch, reset or rebase the branch, and never push "
            "(it is blocked): the developer opens the PR or turns this chat into a Ziggy ticket.",
            "The sandbox allows writes in the worktree and temp dirs only, and no network for commands.",
        ]
    else:
        lines += [
            f"You read the main checkout `{cwd}` (the developer's own working copy): use Read, Grep and Glob. You cannot "
            "edit files or run shell commands here; if the developer wants changes, say so and suggest a write chat or a "
            "Ziggy ticket.",
        ]
    lines += [
        "",
        "Connected tools (MCP), such as the issue tracker (YouTrack) of this Claude login, may be available: use them "
        "when the developer asks about issues or other things they hold; load them with ToolSearch when deferred.",
    ]
    return "\n".join(lines) + "\n"
