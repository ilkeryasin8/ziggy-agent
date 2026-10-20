"""Subscription quota per Claude login: what the sessions' streams say about the 5-hour and weekly limits.

An *account* is the Claude login a target's sessions use: the resolved CLAUDE_CONFIG_DIR of its environment (the
target's [env] table, else the wrapper's own), or `default` without one. Its label is the login's email from that
login's `.claude.json` (`~/.claude.json` for the default login), else the folder name.

Every session (stage, chat, retro, onboarding) writes `rate_limit_event`s into its stream: status (`allowed`,
`allowed_warning`, `rejected`), the window that decided it (`rateLimitType`, `resetsAt`) and each window's utilization
(`unifiedWindows`). `Feed` tails a running session's stream for them and `record` keeps the latest per account in
`state_home()/quota.json` (machine-wide lock, atomic replace):

    {"accounts": {"<key>": {key, label, status, type, resets_at, windows: {<window>: {utilization, resets_at}},
                            overage, updated_at, blocked_until, blocked_at, blocked_window, blocked_reason}}}

A `rejected` event blocks the account until its window resets (`blocked_until`, epoch seconds); a session that
ended on a usage-limit error without one (`rejection`: the result's text, an HTTP 429, a `rate_limit` error) blocks
it the same way. A later `allowed` event clears the block once the blocking window has rolled over, or when it comes
from a session that started after the block (another session's request already in flight says nothing).

The engine uses this twice (pipeline.py): a stage whose session was rejected does not fail; the ticket waits with
status `quota` until the window resets and then resumes the same session; and a stage is not started on a blocked
account (the ticket waits the same way, `reason` admission). `wake` (the panel's tick) starts those tickets when
they are due, so a panel restart loses nothing.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, notify, state
from .config import Target

LEDGER = "quota.json"
STATUS = "quota"  # the ticket status while it waits for its account's limit to reset
DEFAULT = "default"  # the account key of the default login (no CLAUDE_CONFIG_DIR)
GRACE_S = 60  # a held ticket starts this long after the window's reset time
FALLBACK_S = 30 * 60  # how long a rejection whose reset time is unknown blocks the account
WARN_AT = 0.8  # utilization from which the panel shows a window in the warning colour
WINDOWS = {"five_hour": ("5 sa", "5h"), "seven_day": ("7 gün", "7d"), "seven_day_opus": ("7 gün Opus", "7d opus"),
           "seven_day_sonnet": ("7 gün Sonnet", "7d sonnet")}  # windows shown, in order: (panel, CLI)
# A session that ended in error with this in its result text hit a usage limit (tolerant: the wording changes between
# CLI versions; `quota_error` in run.json keeps the raw text). Seen: "You've hit your session limit · resets 8pm
# (Europe/Istanbul)"; older CLIs: "Claude AI usage limit reached|<epoch>".
USAGE_TEXT = re.compile(r"hit your (?:[\w-]+ )?limit|usage limit|limit reached|(?:session|weekly|5-hour|opus|sonnet) limit"
                        r"|rate[ _-]?limit", re.I)
_EPOCH = re.compile(r"\|(\d{10})\b")
_RESETS = re.compile(r"resets\s+(?:(?P<month>[A-Z][a-z]{2})\s+(?:(?P<day>\d{1,2}),?\s+))?(?:at\s+)?(?P<hour>\d{1,2})"
                     r"(?::(?P<minute>\d\d))?\s*(?P<ampm>am|pm)?\s*(?:\((?P<tz>[A-Za-z_]+/[A-Za-z_/+-]+|UTC)\))?", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def now() -> float:
    """The clock every decision here reads (tests move it)."""
    return time.time()


# --- accounts -------------------------------------------------------------------------------------

def account_key(target: Target) -> str:
    raw = target.env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.realpath(os.path.expanduser(raw)) if raw else DEFAULT


def label_for(key: str) -> str:
    """The login's email (its `.claude.json`, read once per account), else the folder name."""
    path = Path.home() / ".claude.json" if key == DEFAULT else Path(key) / ".claude.json"
    try:
        email = (json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError, AttributeError):
        email = None
    if isinstance(email, str) and email.strip():
        return email.strip()
    return "varsayılan giriş" if key == DEFAULT else Path(key).name


# --- the ledger -----------------------------------------------------------------------------------

def _path() -> Path:
    return config.state_home() / LEDGER


def load() -> dict:
    try:
        data = state.read_json(_path())
    except (OSError, ValueError):
        return {"accounts": {}}
    return data if isinstance(data, dict) and isinstance(data.get("accounts"), dict) else {"accounts": {}}


def _locked():
    return notify.state_lock("quota", "quota ledger")


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    state.write_json(_path(), data)


def _epoch(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _windows(info: dict) -> dict:
    out = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict):
            util = w.get("utilization")
            out[name] = {"utilization": float(util) if isinstance(util, (int, float)) and not isinstance(util, bool) else None,
                         "resets_at": _epoch(w.get("resetsAt"))}
    if info.get("rateLimitType") and info["rateLimitType"] not in out and isinstance(info.get("utilization"), (int, float)):
        out[info["rateLimitType"]] = {"utilization": float(info["utilization"]), "resets_at": _epoch(info.get("resetsAt"))}
    return out


def resets_of(info: dict) -> int | None:
    """When the window that decided this event resets: its own resetsAt, else that window's in unifiedWindows."""
    at = _epoch(info.get("resetsAt"))
    if at:
        return at
    w = (info.get("unifiedWindows") or {}).get(info.get("rateLimitType") or "")
    return _epoch(w.get("resetsAt")) if isinstance(w, dict) else None


def record(key: str, info: dict, since: float | None = None) -> dict:
    """Store one rate_limit_event's info as the account's latest (`since`: when its session started); returns the
    entry."""
    status = str(info.get("status") or "")
    with _locked():
        data = load()
        entry = data["accounts"].setdefault(key, {"key": key})
        if not entry.get("label"):
            entry["label"] = label_for(key)
        windows = _windows(info)
        entry.update(status=status or None, type=info.get("rateLimitType"), resets_at=resets_of(info),
                     windows={**(entry.get("windows") or {}), **windows}, overage=info.get("overageStatus"),
                     updated_at=state.now())
        if status == "rejected":
            _block(entry, resets_of(info), info.get("rateLimitType"), "rejected")
        elif status.startswith("allowed") and entry.get("blocked_until"):
            window = entry.get("blocked_window")
            rolled = window and (windows.get(window) or {}).get("resets_at") not in (None, entry["blocked_until"])
            if rolled or (since is not None and since > (entry.get("blocked_at") or 0)):
                _unblock(entry)
        _save(data)
    return entry


def _block(entry: dict, until: int | None, window: str | None, reason: str) -> None:
    entry.update(blocked_until=until or int(now() + FALLBACK_S), blocked_at=int(now()), blocked_window=window,
                 blocked_reason=reason)


def _unblock(entry: dict) -> None:
    for k in ("blocked_until", "blocked_at", "blocked_window", "blocked_reason"):
        entry.pop(k, None)


def block(key: str, until: int | None, window: str | None, reason: str) -> dict:
    """Mark the account blocked (a rejection the stream had no `rejected` event for); returns the entry."""
    with _locked():
        data = load()
        entry = data["accounts"].setdefault(key, {"key": key})
        if not entry.get("label"):
            entry["label"] = label_for(key)
        if not entry.get("blocked_until") or (until or 0) > entry["blocked_until"]:
            _block(entry, until, window, reason)
        entry.update(status="rejected", updated_at=state.now())
        _save(data)
    return entry


def entry(key: str) -> dict | None:
    return load()["accounts"].get(key)


def blocked(key: str, data: dict | None = None) -> dict | None:
    """The account's entry while it is blocked (its window has not reset yet), else None."""
    e = (data or load())["accounts"].get(key)
    return e if e and (e.get("blocked_until") or 0) > now() else None


def observe(target: Target, events: list[dict]) -> dict | None:
    """Record the last rate_limit_event of a finished session's events (chat turns); returns its info."""
    infos = [e["rate_limit_info"] for e in events if e.get("type") == "rate_limit_event" and isinstance(e.get("rate_limit_info"), dict)]
    if not infos:
        return None
    try:
        record(account_key(target), infos[-1])
    except (OSError, state.LockError) as exc:
        print(f"quota: {exc}", file=sys.stderr)
    return infos[-1]


class Feed:
    """Tails a running session's stream (claude.jsonl) for rate_limit_events, recording each change in the ledger;
    `last` is the session's latest event info, `rate_limited` whether an assistant turn ended on a rate-limit error."""

    def __init__(self, key: str, stream: Path):
        self.key, self.stream, self.pos, self.started = key, stream, 0, now()
        self.last: dict | None = None
        self.rate_limited = False
        self._recorded = None

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
        for line in chunk[:end].splitlines():
            if b"rate_limit" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                self.last = event["rate_limit_info"]
            elif event.get("type") == "assistant" and event.get("error") == "rate_limit":
                self.rate_limited = True
        sig = json.dumps(self.last, sort_keys=True) if self.last else None
        if sig and sig != self._recorded:
            try:
                record(self.key, self.last, since=self.started)
                self._recorded = sig
            except (OSError, state.LockError) as exc:  # the ledger must never stop a session
                print(f"quota: {exc}", file=sys.stderr)


# --- a session that hit the limit --------------------------------------------------------------------

def parse_reset(text: str, at: float | None = None) -> int | None:
    """The reset time a usage-limit message names: `…|<epoch>`, or `resets 8pm (Europe/Istanbul)` /
    `resets Oct 20, 3:30pm (UTC)` (the next such time); None when it names none."""
    m = _EPOCH.search(text or "")
    if m:
        return int(m[1])
    m = _RESETS.search(text or "")
    if not m or (not m["ampm"] and not m["minute"]):
        return None
    try:
        tz = ZoneInfo(m["tz"]) if m["tz"] else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        tz = datetime.now().astimezone().tzinfo
    hour, minute = int(m["hour"]), int(m["minute"] or 0)
    if m["ampm"]:
        hour = hour % 12 + (12 if m["ampm"].lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    current = datetime.fromtimestamp(now() if at is None else at, tz)
    if m["month"] and m["month"].lower()[:3] in _MONTHS:
        try:
            when = current.replace(month=_MONTHS.index(m["month"].lower()[:3]) + 1, day=int(m["day"]), hour=hour,
                                   minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if when < current - timedelta(days=1):
            when = when.replace(year=when.year + 1)
        return int(when.timestamp())
    when = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= current:
        when += timedelta(days=1)
    return int(when.timestamp())


def rejection(record_: dict) -> dict | None:
    """How a session that wrote no result hit a usage limit: {source, resets_at, window, text}, or None. Its last
    rate_limit_event was `rejected`; or it ended in error (is_error, HTTP 429, a rate-limit turn error) with a
    usage-limit message."""
    text = (record_.get("final_message") or "").strip()
    info = record_.get("rate_limit") or {}
    if info.get("status") == "rejected":
        return {"source": "event", "resets_at": resets_of(info) or parse_reset(text), "window": info.get("rateLimitType"),
                "text": text}
    errored = record_.get("is_error") or record_.get("api_error_status") == 429 or record_.get("rate_limited")
    if errored and (USAGE_TEXT.search(text) or record_.get("api_error_status") == 429 or record_.get("rate_limited")):
        return {"source": "text", "resets_at": parse_reset(text), "window": None, "text": text}
    return None


def hold_until(resets_at: int | None) -> int:
    """When a ticket held for a limit starts again: the reset time plus a minute, else the fallback wait."""
    return (resets_at or int(now() + FALLBACK_S - GRACE_S)) + GRACE_S


def iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def epoch_of(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else None
    except ValueError:
        return None


def clock(epoch: float | None, *, at: float | None = None) -> str:
    """HH:MM in local time; with the day (`16.10 15:05`) when it is not today."""
    if not epoch:
        return "?"
    when, today = datetime.fromtimestamp(epoch), datetime.fromtimestamp(now() if at is None else at)
    return when.strftime("%H:%M") if when.date() == today.date() else when.strftime("%d.%m %H:%M")


# The locative a time takes in Turkish, by the word it is read with last ("15:05'te", "15:30'da", "15:00'te").
_ONES = {1: "de", 2: "de", 3: "te", 4: "te", 5: "te", 6: "da", 7: "de", 8: "de", 9: "da"}
_TENS = {0: "da", 10: "da", 20: "de", 30: "da", 40: "ta", 50: "de"}


def locative(hhmm: str) -> str:
    """`15:05` → `15:05'te` (the panel's app.js has the same rule)."""
    try:
        hour, minute = (int(x) for x in hhmm.rsplit(" ", 1)[-1].split(":"))
    except ValueError:
        return hhmm
    n = minute or hour
    return f"{hhmm}'{_ONES.get(n % 10) or _TENS.get(n - n % 10, 'de')}"



# --- tickets held for a limit -------------------------------------------------------------------------

def held(target: Target, ticket_id: str, st: dict, step: dict, *, account: str, resets_at: int | None, reason: str,
         stage: str, window: str | None = None, phase_dir: str | None = None) -> None:
    """Put a ticket in the `quota` status: `next` is the step it takes when its account's window resets."""
    e = entry(account) or {}
    st.update(status=STATUS, next=step, quota={
        "account": account, "label": e.get("label") or label_for(account), "reason": reason, "stage": stage,
        "window": window, "resets_at": resets_at, "resume_at": iso(hold_until(resets_at)), "since": state.now(),
        **({"phase_dir": phase_dir} if phase_dir else {})})


def due(st: dict, data: dict | None = None) -> bool:
    """A held ticket may start: its time came, or a newer session of its account cleared the block (an `allowed`
    event; a block that merely ran out waits for resume_at, a minute after the reset)."""
    q = st.get("quota") or {}
    if st.get("status") != STATUS:
        return False
    at = epoch_of(q.get("resume_at"))
    account = (data or load())["accounts"].get(q.get("account") or DEFAULT) or {}
    return (at is not None and now() >= at) or not account.get("blocked_until")


def wake(target: Target) -> list[str]:
    """Start every ticket of the target held for a limit whose time came, as a detached `ziggy run` job (the
    panel's tick; a panel restart re-checks on its first tick). Returns the ids started."""
    started = []
    data = load()
    for ticket_id in state.ticket_ids(target):
        try:
            if not due(state.load_state(target, ticket_id), data):
                continue
            with state.lock(target, f"wake-{ticket_id}", f"start of {ticket_id} after its quota reset"):
                from . import dashboard  # the panel's jobs: dashboard imports the engine
                if not due(state.load_state(target, ticket_id), data) or dashboard._busy(target, ticket_id):
                    continue
                dashboard.start_job(target, ticket_id, "run", ticket_id)
            started.append(ticket_id)
            print(f"{ticket_id}: its account's usage limit has reset; started", file=sys.stderr, flush=True)
        except state.LockError:
            continue
        except (OSError, ValueError, LookupError) as exc:
            print(f"{ticket_id}: could not start after its quota reset: {exc}", file=sys.stderr)
    return started


# --- what the panel and `status` show -------------------------------------------------------------------

def view(targets) -> list[dict]:
    """One row per account the targets use: {key, label, targets, status, type, windows: [{id, label, utilization,
    resets_at, tone}], blocked, blocked_until, resets_at, updated_at, tone}. tone: bad while blocked, warn from WARN_AT."""
    data = load()
    rows: dict[str, dict] = {}
    for t in targets:
        key = account_key(t)
        if key in rows:
            rows[key]["targets"].append(t.name)
            continue
        e = data["accounts"].get(key) or {}
        windows = []
        for name, (label, _) in WINDOWS.items():
            w = (e.get("windows") or {}).get(name)
            if not w or w.get("utilization") is None:
                continue
            past = w.get("resets_at") and w["resets_at"] <= now()  # the window rolled over since: nothing used yet
            util = 0.0 if past else w["utilization"]
            windows.append({"id": name, "label": label, "utilization": util, "percent": round(util * 100),
                            "resets_at": None if past else iso(w.get("resets_at")),
                            "tone": "bad" if util >= 1 else "warn" if util >= WARN_AT else "ok"})
        stop = blocked(key, data)
        tone = "bad" if stop else "warn" if any(w["tone"] != "ok" for w in windows) else "ok" if windows else "muted"
        rows[key] = {"key": key, "label": e.get("label") or label_for(key), "targets": [t.name],
                     "status": e.get("status"), "type": e.get("type"), "windows": windows, "blocked": bool(stop),
                     "blocked_until": iso(stop["blocked_until"]) if stop else None,
                     "blocked_clock": clock(stop["blocked_until"]) if stop else None,
                     "updated_at": e.get("updated_at"), "tone": tone}
    return list(rows.values())


def text(row: dict) -> str:
    """`5h 18% · 7d 11%`, `blocked until 15:05 (five_hour) · …`, or `no rate-limit event seen yet` (the CLI)."""
    parts = [f"{WINDOWS[w['id']][1]} {w['percent']}%" for w in row["windows"]]
    if row["blocked"]:
        parts.insert(0, f"limit reached, blocked until {row['blocked_clock']}" + (f" ({row['type']})" if row.get("type") else ""))
    return " · ".join(parts) or "no rate-limit event seen yet"
