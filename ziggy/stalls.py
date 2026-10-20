"""The tool calls of a running session: how long each took, one that runs past `stall_after`, and where the time of
a stage that ended without its result went.

`Calls` tails the session's stream (claude.jsonl) like quota.Feed: a `tool_use` block starts a call (timed by the
watcher's clock, the stream has no times), its `tool_result` ends it, and the CLI's `tool_progress` heartbeats
(`parent_tool_use_id`, `elapsed_time_seconds`) say how long a call has been running. A call open past the target's
`stall_after` (10 m) is the stage's `stall` in run.json; the inbox shows it ("Komut 10 dk’dır sürüyor: <komut>",
attention.py) until the call returns or the stage ends. Nothing is stopped for it. Subagent calls (Agent, Task) are
not counted: their own tool calls are.

Heartbeats are output, so they keep the quiet watchdog from calling a session quiet while a tool runs (as the process
table check does): a call that hangs is what the stall item reports.

`explain` turns a stage that ended by its timeout, by going quiet or by losing its process, without a result, into one
sentence: "zaman aşımı (45 dk): sürenin 34 dk’sı (%76) en yavaş 3 komutu beklerken geçti (grep -ril … 25 dk, …);
bellek baskısı kritikti".
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import host, state

SUBAGENT_TOOLS = {"Agent", "Task"}
SLOWEST = 3  # calls an explanation names
KEEP = 5  # calls run.json keeps (`tool_calls`)
_CD = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
WHY = {"timeout": "zaman aşımı", "quiet": "sessiz kaldı", "lost": "süreç sonuçsuz bitti"}


def short(name: str, data: dict, limit: int = 60) -> str:
    """A call in a few words: a command without its leading `cd <dir> &&`, else the tool and its file or pattern."""
    if name == "Bash":
        line = str(data.get("command") or "")
        while (m := _CD.match(line)):
            line = line[m.end():]
        said = " ".join(line.split())
    else:
        what = data.get("file_path") or data.get("pattern") or data.get("url") or data.get("query") or ""
        said = f"{name} {Path(what).name if data.get('file_path') else what}".strip()
    return clip(said, limit)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + " …"


def minutes(seconds: float) -> str:
    """`25 dk`, `40 sn` under a minute."""
    return f"{round(seconds / 60)} dk" if seconds >= 60 else f"{max(0, round(seconds))} sn"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class Calls:
    """The session's tool calls by id: {id, tool, command, started (epoch), ended, elapsed (heartbeat)}."""

    def __init__(self, stream: Path, clock=time.time):
        self.stream, self.pos, self.clock = stream, 0, clock
        self.calls: dict[str, dict] = {}

    def poll(self) -> None:
        try:
            with self.stream.open("rb") as f:
                f.seek(self.pos)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end == -1:
            return
        self.pos += end + 1
        now = self.clock()
        for line in chunk[:end].splitlines():
            if b"tool_" in line:
                self.feed(line, now)

    def feed(self, line: bytes | str, now: float) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "tool_progress":
            call = self.calls.get(str(event.get("parent_tool_use_id") or ""))
            seconds = event.get("elapsed_time_seconds")
            if call and isinstance(seconds, (int, float)) and not call.get("ended"):
                call["elapsed"] = max(call.get("elapsed") or 0, float(seconds))
            return
        if kind not in ("assistant", "user"):
            return
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") and block.get("name") not in SUBAGENT_TOOLS:
                name = str(block.get("name") or "?")
                self.calls.setdefault(block["id"], {"id": block["id"], "tool": name, "started": now, "ended": None,
                                                    "command": short(name, block.get("input") or {})})
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in self.calls:
                call = self.calls[block["tool_use_id"]]
                call["ended"] = call["ended"] or now

    def seconds(self, call: dict, now: float | None = None) -> float:
        """How long the call ran (or runs): the watcher's clock, or the heartbeat's when that says more."""
        end = call["ended"] or (self.clock() if now is None else now)
        return max(end - call["started"], call.get("elapsed") or 0)

    def stalled(self, after: int, now: float | None = None) -> dict | None:
        """The longest call still open past `after` seconds: {id, tool, command, seconds, since}, else None."""
        now = self.clock() if now is None else now
        open_ = [(self.seconds(c, now), c) for c in self.calls.values() if not c["ended"]]
        took, call = max(open_, key=lambda x: x[0], default=(0, None))
        if call is None or took < after:
            return None
        return {"id": call["id"], "tool": call["tool"], "command": call["command"], "seconds": round(took),
                "since": _iso(now - took)}

    def slowest(self, n: int = KEEP, now: float | None = None) -> list[dict]:
        """The n longest calls, longest first: {tool, command, seconds, start (s into the session), open}."""
        now = self.clock() if now is None else now
        first = min((c["started"] for c in self.calls.values()), default=now)
        rows = sorted(self.calls.values(), key=lambda c: self.seconds(c, now), reverse=True)[:n]
        out = []
        for c in rows:
            took = self.seconds(c, now)
            start = (c["ended"] or now) - took
            out.append({"tool": c["tool"], "command": c["command"], "seconds": round(took),
                        "start": round(max(0.0, start - first)), "open": not c["ended"]})
        return out


class Watch:
    """What dispatch's watch loop adds to a stage's run.json: `host` {start, end, worst} samples (one every
    HOST_LOOK_S), the open `stall` while a call runs past `stall_after` (saved at once, for the inbox), and at the end
    `tool_calls`, the slowest calls."""

    HOST_LOOK_S = 30

    def __init__(self, stage_dir: Path, record: dict, stall_after: int | None):
        self.stage_dir, self.record, self.stall_after = stage_dir, record, stall_after
        self.calls = Calls(stage_dir / "claude.jsonl")
        first = host.sample()
        record["host"] = {"start": first, "worst": first}
        self.next_look = time.monotonic() + self.HOST_LOOK_S

    def poll(self) -> None:
        self.calls.poll()
        if time.monotonic() >= self.next_look:
            self.next_look = time.monotonic() + self.HOST_LOOK_S
            self._host(host.sample())
        stall = self.calls.stalled(self.stall_after) if self.stall_after else None
        old = self.record.get("stall")
        if (stall or {}).get("id") != (old or {}).get("id"):
            if stall:
                self.record["stall"] = {**stall, "host": host.sample()}
            else:
                self.record.pop("stall", None)
            state.write_json(self.stage_dir / "run.json", self.record)  # the inbox sees it at once

    def _host(self, s: dict) -> None:
        self.record["host"]["worst"] = host.worst(self.record["host"].get("worst"), s)

    def finish(self) -> None:
        self.calls.poll()
        self.record.pop("stall", None)
        end = host.sample()
        self.record["host"]["end"] = end
        self._host(end)
        self.record["tool_calls"] = self.calls.slowest()


def _covered(calls: list[dict]) -> float:
    """Seconds of wall time the calls cover together (calls run in parallel are counted once)."""
    spans = sorted((c["start"], c["start"] + c["seconds"]) for c in calls)
    total, end = 0.0, None
    for a, b in spans:
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def explain(why: str, stage_seconds: float, calls: list[dict], worst: dict | None) -> str:
    """One sentence on where a stage's time went: why it ended, its length, how much of it its slowest calls took
    (their share, each with its time), and the memory pressure seen during it."""
    head = f"{WHY.get(why, why)} ({minutes(stage_seconds)})"
    slow = [c for c in calls[:SLOWEST] if c["seconds"] >= 1]
    parts = []
    if slow:
        took = min(_covered(slow), stage_seconds) if stage_seconds else _covered(slow)
        share = f" (%{round(100 * took / stage_seconds)})" if stage_seconds else ""
        what = f"en yavaş {len(slow)} komutu" if len(slow) > 1 else "bir komutu"
        listed = ", ".join(f"{clip(c['command'] or c['tool'], 40)} {minutes(c['seconds'])}" for c in slow)
        spent = minutes(took)
        parts.append(f"sürenin {spent}’{'sı' if spent.endswith('dk') else 'si'}{share} {what} beklerken geçti ({listed})")
    else:
        parts.append("hiçbir komut uzun sürmedi")
    said = host.phrase(worst)
    if said:
        parts.append(said)
    return f"{head}: " + "; ".join(parts)
