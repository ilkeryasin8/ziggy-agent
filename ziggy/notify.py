"""macOS notifications for new needs-you items (the attention ledger decides what is new).

Only our own processes (the engine, the panel, the CLI) call this; it never runs inside a Claude
sandbox, whose settings deny osascript anyway. A notification goes through terminal-notifier when
it is installed (PATH, then the two Homebrew paths launchd's PATH lacks), else `osascript -e
'display notification …'`; always as an argv, never through a shell.

Rate limit: at most one notification per `[notifications] min_interval`. What arrives inside the
window is counted, not shown; the next notification carries the count, and the panel's attention
loop calls `flush_due` so a count left at the end of a window is shown on its own. The window lives
in `state_home()/notify.json` because every engine process sends for itself. Nothing here raises:
a notification must never fail a run. `ZIGGY_NOTIFY=off` turns it all off (the tests set it).
"""
import fcntl
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

from . import config, state

APP = "Ziggy"
SUMMARY_GROUP = "ziggy-more"
DIGEST_GROUP = "ziggy-digest"
DIGEST_MAX = 50  # items a digest keeps; older ones are only counted
DIGEST_NAMED = 3  # items a digest notification names
HOMEBREW = ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
OSASCRIPT = "/usr/bin/osascript"
MESSAGE_MAX = 200
SUBTITLE_MAX = 80
TIMEOUT = 10
MACOS = sys.platform == "darwin"
RATE_FILE = "notify.json"


@contextmanager
def state_lock(name: str, what: str, timeout: float = 30, poll: float = 0.05):
    """A machine-wide lock on `state_home()/<name>.lock` (flock: per open file, so threads of one
    process exclude each other too, and a dead holder's lock goes with its process)."""
    path = config.state_home() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise state.LockError(f"{what}: {path} stayed locked for {timeout:g}s") from None
                time.sleep(poll)
        yield
    finally:
        os.close(fd)


def _warn(text: str) -> None:
    print(f"notify: {text}", file=sys.stderr)


def settings():
    """The [notifications] settings, or None when notifications are off (config, platform or env)."""
    if os.environ.get("ZIGGY_NOTIFY", "").strip().lower() == "off" or not MACOS:
        return None
    try:
        cfg = config.load_notifications()
    except config.ConfigError as exc:
        _warn(f"{exc}; using the defaults")
        cfg = config.NotificationsConfig()
    return cfg if cfg.enabled else None


def wants(kind: str) -> bool:
    cfg = settings()
    return cfg is not None and kind in cfg.events


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _quote(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe(text: str) -> str:
    """terminal-notifier reads a value starting with '-' as an option."""
    return " " + text if text.startswith("-") else text


def tool() -> str:
    return shutil.which("terminal-notifier") or next((p for p in HOMEBREW if os.access(p, os.X_OK)), None) or OSASCRIPT


def argv(tool_path: str, subtitle: str, message: str, group: str) -> list[str]:
    subtitle, message = one_line(subtitle, SUBTITLE_MAX), one_line(message, MESSAGE_MAX)
    if os.path.basename(tool_path) == "osascript":
        script = f'display notification {_quote(message)} with title "{APP}"' + (f" subtitle {_quote(subtitle)}" if subtitle else "")
        return [tool_path, "-e", script]
    return [tool_path, "-title", APP, *(["-subtitle", _safe(subtitle)] if subtitle else []),
            "-message", _safe(message), "-group", group]


def _show(subtitle: str, message: str, group: str) -> bool:
    """Put one notification on screen; False (with a stderr line) when the tool fails."""
    cmd = argv(tool(), subtitle, message, group)
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        _warn(f"{os.path.basename(cmd[0])}: {exc}")
        return False
    if done.returncode != 0:
        _warn(f"{os.path.basename(cmd[0])} exited {done.returncode}: {one_line(done.stderr, 300)}")
        return False
    return True


def _load_rate() -> dict:
    """notify.json: when the last one was shown, how many the rate limit held back, and the digest waiting
    (`digest`: [{title, message, at}], `digest_since`, `digest_dropped`: items past DIGEST_MAX)."""
    try:
        data = state.read_json(config.state_home() / RATE_FILE)
        digest = [d for d in data.get("digest") or [] if isinstance(d, dict)]
        return {"last_sent": float(data.get("last_sent") or 0), "suppressed": int(data.get("suppressed") or 0),
                "digest": digest, "digest_since": float(data.get("digest_since") or 0),
                "digest_dropped": int(data.get("digest_dropped") or 0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"last_sent": 0.0, "suppressed": 0, "digest": [], "digest_since": 0.0, "digest_dropped": 0}


def _save_rate(rate: dict) -> None:
    state.write_json(config.state_home() / RATE_FILE, rate)


def language() -> str:
    """The language notifications are written in ([notifications] language; the system locale's by default)."""
    try:
        return config.load_notifications().language
    except config.ConfigError:
        return config.system_language()


def _more(n: int) -> str:
    return f"{n} öğe daha seni bekliyor" if language() == "tr" else f"{n} more waiting for you"


def send(title: str, message: str, *, key: str, kind: str | None = None) -> bool:
    """Show a notification: `title` (what it is about, e.g. "dmo-3 · demo") under the app name,
    `message` below it, `key` its group (a newer one with the same key replaces it). With digests on
    (`digest_every`), a `kind` that is not urgent waits for the digest instead. True when shown."""
    try:
        cfg = settings()
        if cfg is None:
            return False
        with state_lock("notify", "notification rate"):
            rate = _load_rate()
            now = time.time()
            if kind and cfg.digest_every and kind not in cfg.urgent:
                if not rate["digest"]:
                    rate["digest_since"] = now
                rate["digest"].append({"title": title, "message": message, "at": state.now()})
                if len(rate["digest"]) > DIGEST_MAX:
                    rate["digest_dropped"] += len(rate["digest"]) - DIGEST_MAX
                    rate["digest"] = rate["digest"][-DIGEST_MAX:]
                _save_rate(rate)
                return False
            if now - rate["last_sent"] < cfg.min_interval:
                rate["suppressed"] += 1
                _save_rate(rate)
                return False
            extra = rate["suppressed"]
            shown = _show(title, f"{message} (+ {_more(extra)})" if extra else message, key)
            if shown:
                _save_rate({**rate, "last_sent": now, "suppressed": 0})
            return shown
    except Exception as exc:  # noqa: BLE001 — a notification must never fail its caller
        _warn(str(exc))
        return False


def digest_text(items: list[dict], dropped: int = 0) -> tuple[str, str]:
    """(subtitle, message) of a digest: how many, then the first few by title and what they are."""
    n = len(items) + dropped
    named = "; ".join(f"{i.get('title')}: {i.get('message')}" for i in items[:DIGEST_NAMED])
    rest = n - min(len(items), DIGEST_NAMED)
    if language() == "tr":
        return f"Özet · {n} öğe", named + (f" (+ {rest} öğe daha)" if rest > 0 else "")
    return f"Digest · {n} item{'' if n == 1 else 's'}", named + (f" (+ {rest} more)" if rest > 0 else "")


def flush_due() -> bool:
    """Show the count of notifications held back by the rate limit once their window has passed, and the digest
    once `digest_every` has passed since its first item (or right away when digests were turned off)."""
    try:
        cfg = settings()
        if cfg is None:
            return False
        with state_lock("notify", "notification rate"):
            rate = _load_rate()
            now = time.time()
            if rate["digest"] and now - rate["digest_since"] >= cfg.digest_every:
                subtitle, message = digest_text(rate["digest"], rate["digest_dropped"])
                if _show(subtitle, message, DIGEST_GROUP):
                    _save_rate({**rate, "digest": [], "digest_since": 0.0, "digest_dropped": 0})
                    return True
                return False
            if not rate["suppressed"] or now - rate["last_sent"] < cfg.min_interval:
                return False
            shown = _show("", _more(rate["suppressed"]), SUMMARY_GROUP)
            if shown:
                _save_rate({**rate, "last_sent": now, "suppressed": 0})
            return shown
    except Exception as exc:  # noqa: BLE001
        _warn(str(exc))
        return False
