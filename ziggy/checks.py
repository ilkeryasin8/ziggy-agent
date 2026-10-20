"""Engine-run checks: the target's [[targets.<name>.checks]] commands, run after an implement stage reports READY.

Checks execute code the agent wrote, outside Claude's sandbox, so every check runs under macOS `sandbox-exec` with a
profile generated here: no network except loopback, and file writes only in the worktree, the system temp dirs,
/dev and the target's state dir. Without sandbox-exec a check fails; it never runs unsandboxed.

A target may opt into named additions to the profile (`check_sandbox`, config.CHECK_SANDBOX_PRESETS): `xcode` lets
`swift test` / `xcodebuild test` write SwiftPM's shared cache and reach the simulator's test-manager socket.

A check must leave the worktree as it found it (`git status` before and after): new untracked files it left behind
(a test's stray database or telemetry file) are removed and listed as a warning; tracked files it changed fail it.

A failure whose output shows the check's environment failing rather than the code (ENVIRONMENT: a tool starting its
own sandbox inside this one, which macOS refuses) carries `environment` {line, why}: the pipeline stops there instead
of sending the code back to the implementer, who cannot change the check command.
"""
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import CHECK_SANDBOX_PRESETS, Target
from .dispatch import PID_FILE, STOP_FILE
from .worktree import git

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DETAIL_MAX = 2000
LIST_MAX = 20  # paths named in a check's detail
# System temp dirs (/tmp, $TMPDIR) and devices (/dev/null, /dev/tty) every toolchain writes to.
SYSTEM_WRITABLE = ("/private/tmp", "/private/var/folders", "/dev")
# (pattern, why) of output that means the environment failed, not the code under test.
ENVIRONMENT = (
    (re.compile(r"sandbox-exec: sandbox_apply: Operation not permitted"),
     "a tool started its own sandbox inside the check's sandbox, which macOS refuses; switch that tool's sandbox off "
     "in the check command (the Swift compiler's macro plugins: 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox')"),
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _literal(path: str) -> str:
    """An SBPL string literal; paths with control characters are refused rather than guessed at."""
    if _CONTROL.search(path):
        raise ValueError(f"path has control characters: {path!r}")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _preset_rules(name: str) -> list[str]:
    """The extra rules of one check_sandbox preset; an unknown name is refused (config.py validates it first)."""
    if name == "xcode":
        # SwiftPM's shared cache (package resolution of the project's local packages) and the simulator's
        # testmanagerd socket under /private/var/tmp, which `xcodebuild test` connects to. Nothing else opens.
        swiftpm = os.path.realpath(Path.home() / "Library" / "Caches" / "org.swift.swiftpm")
        return [f"(allow file-write* (subpath {_literal(swiftpm)}))",
                '(allow network-outbound (remote unix-socket (subpath "/private/var/tmp")))']
    raise ValueError(f"unknown check_sandbox preset {name!r} (known: {', '.join(CHECK_SANDBOX_PRESETS)})")


def profile(wt: Path, state_dir: Path, presets: tuple[str, ...] = ()) -> str:
    """The sandbox-exec profile one check runs under, plus the rules of the target's `presets`. Paths are resolved:
    the sandbox matches real paths."""
    roots = [str(Path(wt).resolve()), *SYSTEM_WRITABLE, str(Path(state_dir).resolve())]
    subpaths = " ".join(f"(subpath {_literal(root)})" for root in roots)
    extra = [rule for name in presets for rule in _preset_rules(name)]
    return "\n".join([
        "(version 1)",
        "(allow default)",
        # Loopback only: test databases and servers on localhost work, nothing leaves the machine.
        "(deny network*)",
        '(allow network-outbound (remote ip "localhost:*"))',
        '(allow network-bind network-inbound (local ip "localhost:*"))',
        # Unix sockets (e.g. a local Postgres in /tmp) only where the check may write anyway.
        f"(allow network-outbound (remote unix-socket {subpaths}))",
        f"(allow network-bind (local unix-socket {subpaths}))",
        "(deny file-write*)",
        f"(allow file-write* {subpaths})",
        *extra,
        "",
    ])


def run(target: Target, ticket_id: str, wt: Path, checks: list[dict], log_path: Path, env: dict) -> list[dict]:
    """Runs the checks in order and stops at the first failure; output goes to log_path.

    While a check runs, its pid sits in the stage dir (log_path's parent) like a claude session's does, so
    `dispatch.stop` ends it; the check then fails as "stopped".
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for check in checks:
        before = _status(wt)
        outcome = _run_one(target, wt, check, log_path, env)
        _guard(wt, before, outcome, log_path)
        outcomes.append(outcome)
        if outcome["status"] != "pass":
            break
    return outcomes


def _run_one(target: Target, wt: Path, check: dict, log_path: Path, env: dict) -> dict:
    name, command, timeout_s = check["name"], check["run"], check["timeout_s"]
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f'\n=== check "{name}" · {started_at} · cwd {wt}\n$ {command}\n')
    before = log_path.stat().st_size
    started = time.monotonic()
    exit_code, how = _execute(target, wt, name, command, timeout_s, log_path, env)
    duration_ms = int((time.monotonic() - started) * 1000)
    passed = how == "exit 0"
    tail = _tail(log_path, before)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"=== {name}: {'pass' if passed else 'fail'} ({how}, {short_duration(duration_ms / 1000)})\n")
    outcome = {
        "name": name,
        "status": "pass" if passed else "fail",
        "detail": "\n".join(tail.split("\n")[-3:]) if passed else "\n".join(p for p in (how, tail) if p),
        "source": "engine",
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    environment = None if passed else _environment(log_path, before)
    if environment:
        outcome["environment"] = environment
    return outcome


def _environment(log_path: Path, start: int) -> dict | None:
    """The first line of this check's output (from byte `start` on) that ENVIRONMENT recognises, with its why. The whole
    output is read, line by line: the tell-tale line is usually far above the tail a long build ends with."""
    with log_path.open("rb") as f:
        f.seek(start)
        for raw in f:
            line = _ANSI.sub("", raw.decode("utf-8", errors="replace")).strip()
            for pattern, why in ENVIRONMENT:
                if pattern.search(line):
                    return {"line": line[:DETAIL_MAX], "why": why}
    return None


def _status(wt: Path) -> dict[str, str] | None:
    """Changed and untracked (not ignored) paths of the worktree → their `git status` XY; None when git can't tell."""
    if not Path(wt).is_dir():
        return None
    proc = git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=wt, check=False)
    if proc.returncode != 0:
        return None
    parts, out, i = proc.stdout.split("\0"), {}, 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        out[entry[3:]] = entry[:2]
        if entry[0] in ("R", "C"):
            i += 1  # the old path of a rename/copy
    return out


def _guard(wt: Path, before: dict[str, str] | None, outcome: dict, log_path: Path) -> None:
    """Compares the worktree with `before`: removes untracked files the check created (outcome["left_files"], a
    warning; the status stands) and fails the check when it changed tracked files (they stay as they are)."""
    after = _status(wt) if before is not None else None
    if after is None:
        return
    changed = {path: xy for path, xy in after.items() if before.get(path) != xy}
    left = sorted(path for path, xy in changed.items() if xy == "??")
    tracked = sorted(path for path, xy in changed.items() if xy != "??")
    notes = []
    if left:
        for path in left:
            _remove(Path(wt), path)
        outcome["left_files"] = left
        notes.append("warning: removed files the check left behind: " + _names(left))
    if tracked:
        outcome["status"] = "fail"
        notes.insert(0, "checks modified tracked files: " + _names(tracked))
    if not notes:
        return
    with log_path.open("a", encoding="utf-8") as log:
        log.writelines(f"=== {outcome['name']}: {note}\n" for note in notes)
    outcome["detail"] = "\n".join([*notes, outcome["detail"]] if outcome.get("detail") else notes)


def _names(paths: list[str]) -> str:
    return ", ".join(paths[:LIST_MAX]) + (f" (+{len(paths) - LIST_MAX} more)" if len(paths) > LIST_MAX else "")


def _remove(wt: Path, rel: str) -> None:
    """Deletes one untracked file, then the directories it leaves empty (up to the worktree)."""
    path = wt / rel
    if path.is_dir() and not path.is_symlink():  # an untracked nested repository: git lists it as one entry
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    for parent in path.parents:
        if parent == wt or wt not in parent.parents:
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _execute(target: Target, wt: Path, name: str, command: str, timeout_s: int, log_path: Path,
             env: dict) -> tuple[int | None, str]:
    """Runs one check; returns (exit code or None, how it ended)."""
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return None, "could not start: sandbox-exec not available"
    if not Path(wt).is_dir():
        return None, f"could not start: working directory not found: {wt}"
    try:
        sandbox = profile(wt, target.state_dir, target.check_sandbox)
    except ValueError as exc:
        return None, f"could not start: {exc}"
    argv = [SANDBOX_EXEC, "-p", sandbox, "bash", "-c", f"set -eo pipefail\n{command}"]
    stage_dir = log_path.parent
    pid_path = stage_dir / PID_FILE
    timed_out = False
    with log_path.open("ab") as log:
        try:
            # Own process group (sandbox-exec execs bash, so the pid is the group), killed whole on timeout/stop.
            proc = subprocess.Popen(argv, cwd=wt, env={**env, "PIPELINE_CHECK": name}, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True)
        except OSError as exc:
            return None, f"could not start: {exc}"
        pid_path.write_text(f"{proc.pid}\n")
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            pid_path.unlink(missing_ok=True)
    stopped = (stage_dir / STOP_FILE).exists()
    if timed_out or stopped:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    code = proc.returncode
    exit_code = code if code >= 0 else None
    if timed_out:
        return exit_code, f"timed out after {short_duration(timeout_s)}"
    if stopped:
        return exit_code, "stopped"
    return exit_code, f"exit {code if code >= 0 else signal.Signals(-code).name}"


def _tail(log_path: Path, start: int) -> str:
    """This check's own output (from byte `start` on): ANSI-stripped, trimmed, the last DETAIL_MAX chars."""
    with log_path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(start, size - DETAIL_MAX * 2))
        text = f.read().decode("utf-8", errors="replace")
    return _ANSI.sub("", text).strip()[-DETAIL_MAX:]


def short_duration(seconds: float) -> str:
    """"12s" under a minute, else "3m" / "1h 05m" / "2d 4h"."""
    if seconds < 60:
        return f"{int(seconds + 0.5)}s"
    s = int(seconds)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"
