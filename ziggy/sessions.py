"""Other Claude Code sessions on this machine, read-only.

Reads `<config dir>/sessions/<pid>.json` (the registry of running claude processes; the `*.key` files
next to them are secrets and never opened) and `<config dir>/projects/<encoded cwd>/<uuid>.jsonl`
transcripts with bounded head/tail reads cached by size + mtime. The panel is a long-lived process,
so the caches live at module level. The pipeline's own stage and chat sessions are left out, and so
are sessions whose working directory's last component starts with one of `[sessions] hide_cwd_prefixes`
(other tools' throwaway headless helpers); only their count is reported.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import chat, config, retro, state, talk
from .config import ConfigError, Target
from .progress import transcript_items

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(rf"^{UUID}$", re.I)
REGISTRY_RE = re.compile(r"^(\d+)\.json$")
TRANSCRIPT_RE = re.compile(rf"^({UUID})\.jsonl$", re.I)
PROTOCOL_HEADING = "## Result protocol (ziggy)"
# Sessions started before the rename carry the old heading; they are still the pipeline's own.
PROTOCOL_HEADINGS = (PROTOCOL_HEADING, "## Result protocol (bmad-pipeline)")

REGISTRY_BYTES = 64 * 1024
HEAD_BYTES = 128 * 1024
TAIL_BYTES = 64 * 1024
TITLE_SCAN_BYTES = 4 * 1024 * 1024
STREAM_BYTES = 4 * 1024 * 1024  # the drawer reads at most the last 4 MB of a transcript
PAGE_ITEMS = 300
PAGE_TEXT = 1024 * 1024
TITLE_MAX = 80
NAME_MAX = 160
PROMPT_MAX = 300
MAX_ENDED = 200
RECENT_DAYS = 3

_lock = threading.Lock()
_files: dict[str, tuple[int, int, dict, dict]] = {}  # path -> (size, mtime_ns, head meta, merged meta)
_identity: dict[int, tuple[str, bool]] = {}  # pid -> ("startedAt|procStart", live)
_runs: dict[str, tuple[int, str | None]] = {}  # run.json path -> (mtime_ns, session_id)


def encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- config dirs ---------------------------------------------------------------------------------

def default_config_dir() -> Path:
    return Path.home() / ".claude"


def config_dirs(targets: list[Target]) -> dict[str, Path]:
    """Safe key -> config dir: `~/.claude` plus every target's CLAUDE_CONFIG_DIR, deduplicated by
    realpath. Clients address a dir by its key, never by a path."""
    out: dict[str, Path] = {}
    seen: set[str] = set()
    dirs = [default_config_dir()]
    dirs += [Path(os.path.expanduser(t.env["CLAUDE_CONFIG_DIR"])) for t in targets if t.env.get("CLAUDE_CONFIG_DIR")]
    for path in dirs:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        base = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "claude"
        key, n = base, 2
        while key in out:
            key, n = f"{base}-{n}", n + 1
        out[key] = path
    return out


# ---- transcript folding --------------------------------------------------------------------------

_WRAPPER_RE = re.compile(r"^\s*<([A-Za-z_][\w-]*)[^>]*>.*</\1>\s*$", re.S)
_PASTED_RE = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content>", re.S)


def prompt_text(content) -> str | None:
    """The human-visible text of a user message, or None for system-injected content."""
    if isinstance(content, str):
        parts, images = [content], 0
    elif isinstance(content, list):
        parts, images = [], 0
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "image":
                images += 1
            elif c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
        if len(parts) > 1:  # IDE / browser context arrives as its own <tag>…</tag> part before the prompt
            parts = [p for p in parts if not _WRAPPER_RE.match(p)] or parts
    else:
        return None
    text = _PASTED_RE.sub("[pasted text]", "\n".join(parts)).strip()
    if text.startswith("<"):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        if name:
            args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
            cmd = name.group(1).strip()
            cmd = cmd if cmd.startswith("/") else "/" + cmd
            return (cmd + " " + args.group(1).strip()).strip() if args else cmd
        bash = re.search(r"<bash-input>(.*?)</bash-input>", text, re.S)
        return "! " + bash.group(1).strip() if bash else None
    text = "[image] " * images + text
    return text or None


def _human(event: dict) -> bool:
    if event.get("isMeta") or event.get("isSidechain") or event.get("parent_tool_use_id"):
        return False
    origin = event.get("origin")
    return not (isinstance(origin, dict) and origin.get("kind") not in (None, "human"))


def _fold(lines: list[bytes], meta: dict | None = None) -> dict:
    m = meta if meta is not None else {}
    for raw in lines:
        try:
            ev = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(ev, dict):
            continue
        if "started_at" not in m and isinstance(ev.get("timestamp"), str):
            m["started_at"] = ev["timestamp"]
        for src, dst in (("cwd", "cwd"), ("gitBranch", "branch"), ("version", "version"), ("entrypoint", "entrypoint")):
            if isinstance(ev.get(src), str) and ev[src]:
                m[dst] = ev[src]
        kind = ev.get("type")
        if kind == "custom-title" and isinstance(ev.get("customTitle"), str):
            m["custom_title"] = _shorten(ev["customTitle"], NAME_MAX)
        elif kind == "ai-title" and isinstance(ev.get("aiTitle"), str):
            m["ai_title"] = _shorten(ev["aiTitle"], NAME_MAX)
        elif kind == "last-prompt":
            text = prompt_text(ev.get("lastPrompt"))
            if text:
                m["last_prompt"] = _shorten(text, PROMPT_MAX)
        elif kind == "assistant" and not ev.get("isSidechain"):
            m["conversation"] = True
            model = (ev.get("message") or {}).get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                m["model"] = model
        elif kind == "user" and _human(ev):
            text = prompt_text((ev.get("message") or {}).get("content"))
            if text is None:
                continue
            if not m.get("conversation") and any(h in text for h in PROTOCOL_HEADINGS):
                m["own"] = True
            m["conversation"] = True
            if "prompt_title" not in m and not text.startswith(("/", "! ")):
                m["prompt_title"] = _shorten(text, TITLE_MAX)
    return m


def _read_at(path: str, start: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(length)


def _whole_lines(data: bytes, drop_first: bool, drop_last: bool) -> list[bytes]:
    lines = data.split(b"\n")
    if drop_first:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    return [ln for ln in lines if ln.strip()]


_LAST = ("cwd", "branch", "version", "entrypoint", "model", "custom_title", "ai_title", "last_prompt")


def _transcript_meta(path: str, size: int, prev: tuple[int, int, dict, dict] | None) -> tuple[dict, dict]:
    if size <= HEAD_BYTES:
        meta = _fold(_whole_lines(_read_at(path, 0, size), False, False))
        return meta, meta
    if prev and prev[0] > HEAD_BYTES and size >= prev[0]:
        head = prev[2]  # transcripts only grow: the head of a large file does not change
    else:
        head = _fold(_whole_lines(_read_at(path, 0, HEAD_BYTES), False, True))
    start = max(0, size - TAIL_BYTES)
    tail = _fold(_whole_lines(_read_at(path, start, size - start), start > 0, False))
    old = prev[3] if prev else {}
    meta = {}
    for key in _LAST:
        value = tail.get(key) or old.get(key) or head.get(key)
        if value:
            meta[key] = value
    for key in ("prompt_title", "started_at"):
        value = head.get(key) or old.get(key) or tail.get(key)
        if value:
            meta[key] = value
    meta["conversation"] = bool(head.get("conversation") or tail.get("conversation") or old.get("conversation"))
    meta["own"] = bool(head.get("own") or old.get("own") or (tail.get("own") and not head.get("conversation")))
    if prev is None and not meta.get("custom_title") and not meta.get("ai_title") and size > HEAD_BYTES + TAIL_BYTES:
        lo, hi = max(HEAD_BYTES, size - TAIL_BYTES - TITLE_SCAN_BYTES), size - TAIL_BYTES
        lines = [ln for ln in _whole_lines(_read_at(path, lo, hi - lo), True, True)
                 if b'custom-title"' in ln or b'ai-title"' in ln]
        scan = _fold(lines)
        for key in ("custom_title", "ai_title"):
            if scan.get(key):
                meta[key] = scan[key]
    return head, meta


def _transcripts(config_dir: Path) -> dict[str, tuple[str, float, dict]]:
    """Session id -> (path, mtime, meta) for every top-level transcript; newest file per id wins."""
    root = config_dir / "projects"
    found: dict[str, tuple[str, int, int]] = {}
    try:
        folders = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return {}
    for folder in folders:
        try:
            entries = list(os.scandir(folder.path))
        except OSError:
            continue
        for entry in entries:
            match = TRANSCRIPT_RE.match(entry.name)
            if not match or not entry.is_file(follow_symlinks=False):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            sid = match.group(1).lower()
            if sid not in found or st.st_mtime_ns > found[sid][2]:
                found[sid] = (entry.path, st.st_size, st.st_mtime_ns)
    out = {}
    for sid, (path, size, mtime_ns) in found.items():
        prev = _files.get(path)
        if prev and prev[0] == size and prev[1] == mtime_ns:
            meta = prev[3]
        else:
            try:
                head, meta = _transcript_meta(path, size, prev)
            except OSError:
                continue
            _files[path] = (size, mtime_ns, head, meta)
        out[sid] = (path, mtime_ns / 1e9, meta)
    return out


# ---- registry + liveness -------------------------------------------------------------------------

def _registry(config_dir: Path) -> list[dict]:
    root = config_dir / "sessions"
    try:
        names = [e.name for e in os.scandir(root)]
    except OSError:
        return []
    out = []
    for name in names:
        match = REGISTRY_RE.match(name)  # `<pid>.<hex>.key` never matches: secrets stay unopened
        if not match:
            continue
        try:
            with open(root / name, "rb") as f:
                entry = json.loads(f.read(REGISTRY_BYTES))
        except (OSError, ValueError):
            continue
        parsed = parse_registry_entry(entry)
        if parsed and parsed["pid"] == int(match.group(1)):
            out.append(parsed)
    return out


def parse_registry_entry(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    pid, sid, cwd = entry.get("pid"), entry.get("sessionId"), entry.get("cwd")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    if not isinstance(sid, str) or not UUID_RE.match(sid) or not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None

    def num(key):
        value = entry.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(key):
        value = entry.get(key)
        return value if isinstance(value, str) and value else None

    status = entry.get("status")
    return {"pid": pid, "session_id": sid.lower(), "cwd": cwd, "started_at": num("startedAt"),
            "proc_start": text("procStart"), "version": text("version"), "entrypoint": text("entrypoint"),
            "name": text("name"), "status": status if status in ("busy", "idle") else None,
            "status_updated_at": num("statusUpdatedAt")}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int) -> tuple[float | None, str] | None:
    """(start time epoch, command) from ps, or None when ps failed or the pid is gone."""
    try:
        proc = subprocess.run(["ps", "-ww", "-o", "lstart=,command=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.strip().split(None, 5)
    if proc.returncode != 0 or len(parts) < 5:
        return None
    try:
        start = time.mktime(time.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None, " ".join(parts)
    return start, parts[5] if len(parts) > 5 else ""


def _utc(text: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def _live(entry: dict) -> bool:
    pid = entry["pid"]
    if not _pid_alive(pid):
        _identity.pop(pid, None)
        return False
    key = f"{entry['started_at']}|{entry['proc_start']}"
    cached = _identity.get(pid)
    if cached and cached[0] == key:
        return cached[1]
    row = _ps(pid)
    if row is None:
        return False  # not cached: asked again next time
    start, command = row
    if "claude" not in command.lower():
        ok = False
    elif start is None:
        ok = True
    else:
        proc = _utc(entry["proc_start"]) if entry["proc_start"] else None
        if proc is not None and abs(proc - start) <= 2:
            ok = True
        elif entry["started_at"] is not None and abs(entry["started_at"] / 1000 - start) <= 15:
            ok = True
        else:
            ok = proc is None and entry["started_at"] is None
    _identity[pid] = (key, ok)
    return ok


# ---- pipeline knowledge --------------------------------------------------------------------------

def own_session_ids(targets: list[Target]) -> set[str]:
    """Every session id the pipeline started: stage sessions (runs/*/NN-*/run.json), retro sessions
    (runs/*/retro/NN-retro/run.json), ticket chats (runs/*/chat/turns.json) and Sohbet chats (talk/*/turns.json)."""
    ids, seen = set(), set()
    for target in targets:
        ids |= chat.session_ids(target) | talk.session_ids(target)
        for ticket_id in state.ticket_ids(target):
            for phase in [*state.phase_dirs(target, ticket_id), *retro.session_dirs(target, ticket_id)]:
                path = str(phase / "run.json")
                try:
                    mtime = os.stat(path).st_mtime_ns
                except OSError:
                    continue
                seen.add(path)
                cached = _runs.get(path)
                if not cached or cached[0] != mtime:
                    try:
                        sid = state.read_json(Path(path)).get("session_id")
                    except (OSError, ValueError, AttributeError):
                        sid = None
                    cached = (mtime, sid.lower() if isinstance(sid, str) else None)
                    _runs[path] = cached
                if cached[1]:
                    ids.add(cached[1])
    for path in set(_runs) - seen:
        del _runs[path]
    return ids


def _roots(targets: list[Target]) -> list[tuple[str, str, str | None]]:
    """(real path, target, ticket) for every target checkout, worktree base and ticket worktree."""
    roots = []
    for target in targets:
        roots.append((os.path.realpath(target.path), target.name, None))
        roots.append((os.path.realpath(target.state_dir / "worktrees"), target.name, None))
        for ticket_id in state.ticket_ids(target):
            try:
                worktree = state.load_state(target, ticket_id).get("worktree")
            except (OSError, ValueError, AttributeError):
                worktree = None
            path = worktree if isinstance(worktree, str) and worktree else state.worktree_dir(target, ticket_id)
            roots.append((os.path.realpath(path), target.name, ticket_id))
    return roots


def match_root(cwd: str, roots: list[tuple[str, str, str | None]]) -> tuple[str | None, str | None]:
    real = os.path.realpath(cwd)
    best = None
    for root, target, ticket in roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            rank = (len(root), ticket is not None)
            if best is None or rank > best[0]:
                best = (rank, target, ticket)
    return (best[1], best[2]) if best else (None, None)


# ---- listing -------------------------------------------------------------------------------------

def _iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def resume_command(cwd: str, session_id: str, config_dir: Path) -> str:
    prefix = ""
    if os.path.realpath(config_dir) != os.path.realpath(default_config_dir()):
        prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))} "
    return f"cd {shlex.quote(cwd)} && {prefix}claude --resume {session_id}"


def _title(meta: dict, name: str | None) -> tuple[str | None, str | None]:
    if name:
        return name, "name"
    if meta.get("custom_title") or meta.get("ai_title"):
        return meta.get("custom_title") or meta.get("ai_title"), "name"
    if meta.get("prompt_title"):
        return meta["prompt_title"], "prompt"
    return None, None


_ORDER = {"working": 0, "idle": 1, "ended": 2}


def hidden_session(cwd: str, prefixes: tuple[str, ...]) -> bool:
    """True for a helper tool's session to leave out: the cwd's last path component (not any substring)
    starts with one of `prefixes` ([sessions] hide_cwd_prefixes)."""
    return bool(prefixes and cwd) and os.path.basename(os.path.normpath(cwd)).startswith(prefixes)


def _hide_prefixes() -> tuple[str, ...]:
    try:
        return config.load_sessions().hide_cwd_prefixes
    except ConfigError:
        return ()


def listing(targets: list[Target], recent_days: int = RECENT_DAYS, now: float | None = None,
            counts: dict | None = None) -> list[dict]:
    """Claude Code sessions on this machine other than the pipeline's own: live ones (working/idle)
    and ones that ended within `recent_days`. Read-only. Sessions `[sessions] hide_cwd_prefixes` names are
    dropped; when `counts` is given, counts["hidden"] gets how many were."""
    now = time.time() if now is None else now
    cutoff = now - recent_days * 86400
    prefixes = _hide_prefixes()
    with _lock:
        own = own_session_ids(targets)
        roots = _roots(targets)
        out, seen, seen_paths, live_pids, hidden = [], set(), set(), set(), set()
        for key, config_dir in config_dirs(targets).items():
            transcripts = _transcripts(config_dir)
            seen_paths.update(path for path, _, _ in transcripts.values())
            live: dict[str, dict] = {}
            for entry in _registry(config_dir):
                if entry["session_id"] in own or not _live(entry):
                    continue
                live_pids.add(entry["pid"])
                other = live.get(entry["session_id"])
                if other is None or (entry["status"] == "busy" and other["status"] != "busy"):
                    live[entry["session_id"]] = entry
            for sid, entry in live.items():
                path, mtime, meta = transcripts.get(sid, (None, None, {}))
                if meta.get("own") or sid in seen:
                    continue
                seen.add(sid)
                if hidden_session(entry["cwd"], prefixes):
                    hidden.add(sid)
                    continue
                title, source = _title(meta, entry["name"])
                times = [t for t in (entry["status_updated_at"] and entry["status_updated_at"] / 1000,
                                     entry["started_at"] and entry["started_at"] / 1000, mtime) if t]
                out.append(_info(key, config_dir, sid, entry["cwd"], roots, meta, title, source,
                                 "working" if entry["status"] == "busy" else "idle", entry["pid"],
                                 meta.get("started_at") or _iso(entry["started_at"] and entry["started_at"] / 1000),
                                 max(times) if times else None, entry["version"] or meta.get("version"),
                                 entry["entrypoint"] or meta.get("entrypoint"), path is not None))
            for sid, (path, mtime, meta) in transcripts.items():
                if sid in seen or sid in live or sid in own or meta.get("own") or mtime < cutoff:
                    continue
                if not meta.get("conversation") or not meta.get("cwd"):
                    continue
                seen.add(sid)
                if hidden_session(meta["cwd"], prefixes):
                    hidden.add(sid)
                    continue
                title, source = _title(meta, None)
                out.append(_info(key, config_dir, sid, meta["cwd"], roots, meta, title, source, "ended", None,
                                 meta.get("started_at"), mtime, meta.get("version"), meta.get("entrypoint"), True))
        for path in set(_files) - seen_paths:
            del _files[path]
        for pid in set(_identity) - live_pids:
            del _identity[pid]
    if counts is not None:
        counts["hidden"] = len(hidden)
    out.sort(key=lambda s: (_ORDER[s["status"]], -(s["_activity"] or 0), s["id"]))
    ended = 0
    result = []
    for s in out:
        if s["status"] == "ended":
            ended += 1
            if ended > MAX_ENDED:
                continue
        del s["_activity"]
        result.append(s)
    return result


def _info(key, config_dir, sid, cwd, roots, meta, title, source, status, pid, started_at, activity,
          version, entrypoint, has_transcript) -> dict:
    target, ticket = match_root(cwd, roots)
    exists = os.path.isdir(cwd)
    return {"config": key, "id": sid, "cwd": cwd, "cwd_exists": exists, "target": target, "ticket": ticket,
            "title": title, "title_source": source, "last_prompt": meta.get("last_prompt"),
            "branch": meta.get("branch"), "model": meta.get("model"), "entrypoint": entrypoint,
            "status": status, "pid": pid, "started_at": started_at, "last_activity": _iso(activity),
            "version": version, "resume": resume_command(cwd, sid, config_dir) if exists else None,
            "has_transcript": has_transcript, "_activity": activity}


# ---- transcript ----------------------------------------------------------------------------------

def find_transcript(config_dir: Path, session_id: str) -> Path | None:
    if not isinstance(session_id, str) or not UUID_RE.match(session_id):
        return None
    root = os.path.realpath(config_dir / "projects")
    best = None
    try:
        folders = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for folder in folders:
        path = os.path.join(folder.path, session_id.lower() + ".jsonl")
        try:
            st = os.lstat(path)
        except OSError:
            continue
        real = os.path.realpath(path)
        if not os.path.isfile(path) or os.path.islink(path) or os.path.commonpath([root, real]) != root:
            continue
        if best is None or st.st_mtime_ns > best[1]:
            best = (real, st.st_mtime_ns)
    return Path(best[0]) if best else None


def _items(event: dict) -> list[dict]:
    if event.get("isSidechain"):
        return []
    kind = event.get("type")
    if kind == "user" and _human(event):
        text = prompt_text((event.get("message") or {}).get("content"))
        if text is not None:
            return [{"kind": "user", "parent": None, "text": text, "at": event.get("timestamp")}]
    if kind in ("user", "assistant"):
        return transcript_items(event, event.get("cwd"))
    return []


def transcript(config_dir: Path, session_id: str, pos: int | None = None) -> dict | None:
    """A page of the session's transcript from byte `pos` (None: the start of the last 4 MB).
    `pos` continues the read; `more` says whether whole lines are left. None when there is none."""
    path = find_transcript(Path(config_dir), session_id)
    if path is None:
        return None
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        truncated = False
        if pos is None or pos < 0 or pos > size:
            pos = 0
            if size > STREAM_BYTES:
                f.seek(size - STREAM_BYTES - 1)
                f.readline()  # move to the first line start inside the window
                pos = f.tell()
                truncated = True
        f.seek(pos)
        items, text = [], 0
        while len(items) < PAGE_ITEMS and text < PAGE_TEXT:
            line = f.readline()
            if not line.endswith(b"\n"):
                break
            pos += len(line)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                new = _items(event)
                items.extend(new)
                text += sum(len(i.get("content") or i.get("text") or "") for i in new)
        more = f.readline().endswith(b"\n")
    return {"items": items, "pos": pos, "more": more, "truncated": truncated, "size": size}


# ---- CLI -----------------------------------------------------------------------------------------

_VIA = {"claude-desktop": "desktop", "cli": "terminal", "sdk-cli": "headless", "sdk-ts": "headless",
        "sdk-py": "headless", "claude-vscode": "vscode"}


def _ago(iso: str | None, now: float) -> str:
    if not iso:
        return "-"
    secs = max(0, now - datetime.fromisoformat(iso).timestamp())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def print_listing(targets: list[Target], show_all: bool = False, as_json: bool = False, out=print) -> None:
    counts: dict = {}
    sessions = listing(targets, counts=counts)
    if as_json:
        out(json.dumps(sessions, indent=2, ensure_ascii=False))
        return
    live = [s for s in sessions if s["status"] != "ended"]
    ended = [s for s in sessions if s["status"] == "ended"]
    shown = live + (ended if show_all else ended[:10])
    if not shown:
        out("no other Claude Code sessions were active recently")
        if counts["hidden"]:
            out(f"{counts['hidden']} session(s) hidden ([sessions] hide_cwd_prefixes)")
        return
    now = time.time()
    rows = [("STATUS", "WHERE", "BRANCH", "VIA", "ACTIVE", "TITLE")]
    for s in shown:
        where = f"{s['target']}/{s['ticket']}" if s["ticket"] else s["target"] or "other"
        title = s["title"] or os.path.basename(s["cwd"].rstrip("/")) or f"session {s['id'][:8]}"
        rows.append((s["status"], where, s["branch"] or "-", _VIA.get(s["entrypoint"], s["entrypoint"] or "-"),
                     _ago(s["last_activity"], now), _shorten(title, 64)))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        out("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[5])
    for s in live:
        if s["resume"]:
            out(f"resume \"{_shorten(s['title'] or s['id'][:8], 40)}\": {s['resume']}")
    if len(ended) > 10 and not show_all:
        out(f"{len(ended) - 10} more ended session(s): --all")
    if counts["hidden"]:
        out(f"{counts['hidden']} session(s) hidden ([sessions] hide_cwd_prefixes)")
