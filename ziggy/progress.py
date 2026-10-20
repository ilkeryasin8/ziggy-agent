"""Readable progress from a run's event stream (claude.jsonl), live or after the fact.

`watch` follows the ticket's newest session while the ticket is locked by a running process,
switching to the next session when a run moves on to its next stage, and stops when the run ends.
Brief mode keeps what a person would want relayed: stages, subagents, commands that change or
test something, file edits, the agent's own remarks, errors and the outcome.
"""
import json
import time
from pathlib import Path

from . import state
from .config import Target
from .dispatch import OUTPUT, PID_FILE

QUIET_TOOLS = {"Read", "Grep", "Glob", "LS", "TodoWrite", "Agent", "Task", "ToolSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _short(text: str, limit: int = 150) -> str:
    line = " ".join(str(text).strip().splitlines()[0].split()) if str(text).strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _rel(path: str, root: str | None) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1 :]
    return path


def _strip_cd(command: str, root: str | None) -> str:
    """Subagents often start every command with `cd <worktree>;`: the summary keeps the command."""
    if root:
        for sep in ("; ", " && "):
            for target in (root, f'"{root}"'):
                prefix = f"cd {target}{sep}"
                if command.startswith(prefix):
                    return command[len(prefix):]
    return command


def _tool(name: str, data: dict, root: str | None) -> str:
    if name == "Bash":
        return "$ " + _short(_strip_cd(data.get("command", ""), root))
    if name in EDIT_TOOLS:
        return "edit " + _rel(data.get("file_path") or data.get("notebook_path", ""), root)
    if name == "Read":
        return "read " + _rel(data.get("file_path", ""), root)
    if name in ("Grep", "Glob"):
        return f"{name.lower()} {_short(data.get('pattern', ''), 80)}"
    if name == "Skill":
        return "skill " + str(data.get("skill") or data.get("command") or "")
    return name


class Describer:
    """Turns stream events into lines; keeps the little state the stream needs (worktree root,
    whether the session start was already shown)."""

    def __init__(self, phase: str, brief: bool):
        self.phase = phase
        self.brief = brief
        self.root: str | None = None
        self.started = False

    def lines(self, event: dict) -> list[str]:
        kind, sub = event.get("type"), event.get("subtype")
        nested = bool(event.get("parent_tool_use_id"))
        prefix = "  ↳ " if nested else ""
        if kind == "system" and sub == "init":
            self.root = event.get("cwd") or self.root
            if self.started:
                return []
            self.started = True
            return [f"session started · {event.get('model')}"]
        if kind == "system" and sub == "task_started":
            return [f"subagent started: {_short(event.get('description', ''), 100)}"]
        if kind == "system" and sub == "task_notification":
            return [f"subagent {event.get('status') or 'finished'}: {_short(event.get('summary') or '', 120)}"]
        if kind == "assistant":
            out = []
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if self.brief and (nested or name in QUIET_TOOLS):
                        continue
                    if name in ("Agent", "Task"):
                        continue  # task_started reports it with its description
                    out.append(prefix + _tool(name, block.get("input") or {}, self.root))
                elif block.get("type") == "text" and not nested and block.get("text", "").strip():
                    out.append("» " + _short(block["text"], 200))
            return out
        if kind == "user":
            out = []
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if not (self.brief and nested):
                        out.append(prefix + "! " + _short(content or "tool error", 160))
            return out
        if kind == "result":
            cost = event.get("total_cost_usd")
            tail = (event.get("result") or "").strip().splitlines()
            return [f"session segment ended · ${cost:.2f} · {_short(tail[-1] if tail else '', 160)}" if isinstance(cost, (int, float)) else "session segment ended"]
        return []


def read_lines(phase_dir: Path, position: int, describer: Describer) -> tuple[list[str], int]:
    """Lines for the whole events written to the session's stream after byte `position`, and the
    position to continue from; a partial last line waits for the next call."""
    stream = phase_dir / OUTPUT
    if not stream.exists():
        return [], position
    with stream.open("rb") as f:
        f.seek(position)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end == -1:
        return [], position
    out = []
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.extend(describer.lines(event))
    return out, position + end + 1


def phase_dirs(target: Target, ticket_id: str) -> list[Path]:
    return state.phase_dirs(target, ticket_id)


def stage_id(phase_dir: Path) -> str:
    """`implement` for 03-implement (legacy dirs too: `plan`, `build`)."""
    return phase_dir.name.split("-", 1)[1]


def header(phase_dir: Path) -> str:
    try:
        record = state.read_json(phase_dir / "run.json")
    except (OSError, ValueError):
        record = {}
    visit = f" (visit {record['visit']})" if isinstance(record.get("visit"), int) and record["visit"] > 1 else ""
    return f"== {phase_dir.name} · {record.get('stage') or stage_id(phase_dir)}{visit} · {record.get('model', '?')} =="


def footer(phase_dir: Path) -> str | None:
    try:
        record = state.read_json(phase_dir / "run.json")
    except (OSError, ValueError):
        return None
    if "finished_at" not in record:
        return None
    if "stage" in record:  # an engine stage: its verdict, or why it has none
        status = record.get("verdict") or "no result"
        if record.get("route"):
            status += f" → {record['route']}"
        run_state = record.get("state")
        extra = f" ({run_state})" if run_state and run_state != "ok" else ""
    else:  # a v1 session
        status = (record.get("result") or {}).get("status", "failed")
        extra = " (stopped)" if record.get("stopped") else " (timed out)" if record.get("timed_out") else ""
    cost = record.get("total_cost_usd")
    cost_text = f" · ${cost:.2f}" if isinstance(cost, (int, float)) else ""
    return f"== {phase_dir.name} ended: {status}{extra}{cost_text} =="


def watch(target: Target, ticket_id: str, brief: bool = False, show_all: bool = False, out=print, poll: float = 1.0) -> None:
    state.load_ticket(target, ticket_id)  # LookupError for an unknown ticket
    deadline = time.monotonic() + 30
    while not phase_dirs(target, ticket_id) and state.running(target, ticket_id) and time.monotonic() < deadline:
        time.sleep(poll)  # the run is still preparing its worktree
    dirs = phase_dirs(target, ticket_id)
    if not dirs:
        out(f"{ticket_id}: no sessions yet" + (" (preparing)" if state.running(target, ticket_id) else ""))
        return
    queue = dirs if show_all else dirs[-1:]
    while queue:
        phase_dir = queue.pop(0)
        out(header(phase_dir))
        describer = Describer(stage_id(phase_dir), brief)
        position = 0
        while True:
            lines, position = read_lines(phase_dir, position, describer)
            for text in lines:
                out(text)
            alive = (phase_dir / PID_FILE).exists()
            end_line = footer(phase_dir)
            if end_line and not alive:
                out(end_line)
                break
            if not alive and not state.running(target, ticket_id):
                break
            time.sleep(poll)
        if not queue:
            # the run moves on to its next stage: follow that session, including stages that
            # started (or even finished) while this one was being shown.
            while True:
                current = phase_dirs(target, ticket_id)
                newer = current[current.index(phase_dir) + 1:] if phase_dir in current else []
                if newer or not state.running(target, ticket_id):
                    queue.extend(newer)
                    break
                time.sleep(poll)
    st = state.load_state(target, ticket_id)
    out(f"{ticket_id}: {st['status']}")


# --- full transcript ---------------------------------------------------------------------------
# The panel's per-session view: every message, tool call and tool result, structured, with the
# subagent a block belongs to (`parent`, the Agent tool call's id). Noise events (progress
# heartbeats, token estimates, rate limits, hooks) are left out; thinking is recorded without
# text, so there is nothing to show for it.

INPUT_LIMIT = 16 * 1024  # a subagent's handback report travels as tool input
RESULT_LIMIT = 16 * 1024
TRANSCRIPT_PAGE = 300
TRANSCRIPT_BYTES = 1024 * 1024  # text per page, so a page of long tool results stays small


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _result_text(content) -> str:
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") if c.get("type") == "text" else f"[{c.get('type', 'content')}]")
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _input(data: dict) -> dict:
    out = {}
    for key, value in (data or {}).items():
        out[key] = _clip(value, INPUT_LIMIT)[0] if isinstance(value, str) else value
    return out


def transcript_items(event: dict, root: str | None) -> list[dict]:
    kind, sub = event.get("type"), event.get("subtype")
    parent = event.get("parent_tool_use_id")
    if kind == "system" and sub == "init":
        return [{"kind": "session", "model": event.get("model"), "cwd": event.get("cwd")}]
    if kind == "system" and sub == "task_notification":
        return [{"kind": "task", "parent": parent, "status": event.get("status") or "finished",
                 "summary": _short(event.get("summary") or "", 300)}]
    if kind == "assistant":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"kind": "text", "parent": parent, "text": block["text"]})
            elif block.get("type") == "tool_use":
                data = block.get("input") or {}
                out.append({"kind": "tool", "parent": parent, "id": block.get("id"), "name": block.get("name", ""),
                            "summary": _tool(block.get("name", ""), data, root), "input": _input(data),
                            "description": _short(data.get("description") or "", 160) if block.get("name") in ("Agent", "Task") else None})
        return out
    if kind == "user":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                text, cut = _clip(_result_text(block.get("content")), RESULT_LIMIT)
                out.append({"kind": "result", "parent": parent, "tool_id": block.get("tool_use_id"),
                            "is_error": bool(block.get("is_error")), "content": text, "truncated": cut})
            elif block.get("type") == "text" and parent and block.get("text", "").strip():
                text, cut = _clip(block["text"], RESULT_LIMIT)  # a subagent's prompt
                out.append({"kind": "prompt", "parent": parent, "text": text, "truncated": cut})
        return out
    if kind == "result":
        return [{"kind": "end", "cost": event.get("total_cost_usd"), "turns": event.get("num_turns"),
                 "is_error": bool(event.get("is_error")), "text": (event.get("result") or "").strip()}]
    return []


def transcript(phase_dir: Path, position: int = 0, limit: int = TRANSCRIPT_PAGE) -> dict:
    """Up to about `limit` items from the session's stream after byte `position`; `pos` continues
    the read and `more` says whether whole events are left. A partial last line waits."""
    stream = phase_dir / OUTPUT
    alive = (phase_dir / PID_FILE).exists()
    if not stream.exists():
        try:
            final = state.read_json(phase_dir / "run.json").get("final_message", "")
        except (OSError, ValueError):
            final = ""
        items = [{"kind": "text", "parent": None, "text": final}] if final.strip() and position == 0 else []
        return {"items": items, "pos": 1 if items else position, "more": False, "alive": alive, "legacy": True}
    root = None
    with stream.open("rb") as f:
        for _ in range(20):  # hooks may come first; the init event names the worktree
            line = f.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("subtype") == "init":
                root = event.get("cwd")
                break
        f.seek(position)
        items, pos, size = [], position, 0
        for line in f:
            if not line.endswith(b"\n"):
                break
            pos += len(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            new = transcript_items(event, root)
            items.extend(new)
            size += sum(len(i.get("content") or i.get("text") or "") for i in new)
            if len(items) >= limit or size >= TRANSCRIPT_BYTES:
                break
        more = bool(f.readline().endswith(b"\n"))
    return {"items": items, "pos": pos, "more": more, "alive": alive, "legacy": False}
