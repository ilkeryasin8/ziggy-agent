"""`ziggy doctor`: whether this machine can run Ziggy, one line per prerequisite, each failure with what to do about it.
Nothing here changes anything; it reads versions, the config and the state directory."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, dispatch, publish, review
from . import checks as engine_checks
from .config import ConfigError, config_path, load_target, state_home, target_names


def _run(*argv: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_line(out: subprocess.CompletedProcess | None) -> str:
    return ((out.stdout or out.stderr).strip().splitlines() or [""])[0] if out else ""


def checks() -> list[tuple[str, str, str]]:
    """(status, name, detail) rows; status is "ok", "warn" (works, with something missing) or "fail" (Ziggy cannot run
    the part it names)."""
    rows = [("ok", "ziggy", f"{__version__} from {Path(__file__).resolve().parent}")]
    py = sys.version.split()[0]
    rows.append(("ok", "python", py) if sys.version_info >= (3, 12) else ("fail", "python", f"{py}: needs 3.12 or newer"))

    git = _run("git", "--version") if shutil.which("git") else None
    rows.append(("ok", "git", _first_line(git)) if git and git.returncode == 0 else ("fail", "git", "not found: install git"))

    if not shutil.which(dispatch.claude_bin()):
        rows.append(("fail", "claude", f"{dispatch.claude_bin()!r} not found: install Claude Code and log in (`claude`)"))
    else:
        found, least = dispatch.claude_version(), ".".join(map(str, dispatch.MIN_CLAUDE))
        if found is None:
            rows.append(("warn", "claude", f"{dispatch.claude_bin()} reports no version; Ziggy needs {least} or newer"))
        elif found < dispatch.MIN_CLAUDE:
            rows.append(("fail", "claude", f"{'.'.join(map(str, found))} is older than {least}: `claude update`"))
        else:
            rows.append(("ok", "claude", ".".join(map(str, found))))

    if not shutil.which(publish.gh_bin()):
        rows.append(("fail", "gh", f"{publish.gh_bin()!r} not found: install the GitHub CLI (`brew install gh`), then "
                                   "`gh auth login`"))
    else:
        auth = _run(publish.gh_bin(), "auth", "status")
        rows.append(("ok", "gh", "logged in") if auth and auth.returncode == 0
                    else ("fail", "gh", "not logged in: `gh auth login` (publishing and PR sync need it)"))

    which = engine_checks.backend()
    if which:
        rows.append(("ok", "sandbox", which))
    else:
        rows.append(("fail", "sandbox", "no sandbox for engine checks: macOS sandbox-exec, or bubblewrap (`bwrap`) on Linux"))

    hook = dispatch.GITHOOKS / "pre-push"
    rows.append(("ok", "pre-push", str(hook)) if os.access(hook, os.X_OK)
                else ("fail", "pre-push", f"{hook} is missing or not executable: reinstall Ziggy"))
    try:
        json.loads(dispatch.SETTINGS.read_text(encoding="utf-8"))
        rows.append(("ok", "settings", str(dispatch.SETTINGS)))
    except (OSError, ValueError) as exc:
        rows.append(("fail", "settings", f"{dispatch.SETTINGS}: {exc}"))

    rows.extend(_config_rows())

    home = state_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home):
            pass
        rows.append(("ok", "state", str(home)))
    except OSError as exc:
        rows.append(("fail", "state", f"{home} is not writable: {exc}"))

    rows.append(("ok", "uv", "found") if shutil.which("uv")
                else ("warn", "uv", "not found: `ziggy listen` (Slack) and uv projects' prepare need it"))
    rows.append(("ok", "codex", "found") if shutil.which(review.codex_bin())
                else ("warn", "codex", "not found: the optional Codex second review is unavailable"))
    return rows


def _config_rows() -> list[tuple[str, str, str]]:
    path = config_path()
    if not path.exists():
        return [("fail", "config", f"{path} does not exist: copy config.example.toml there, or `ziggy target add <repo>`")]
    try:
        names = target_names()
    except ConfigError as exc:
        return [("fail", "config", str(exc))]
    rows = [("ok", "config", f"{path} ({len(names)} target{'' if len(names) == 1 else 's'})" if names
             else f"{path} has no targets yet: `ziggy target add <repo>`")]
    for name in names:
        try:
            target = load_target(name)
        except ConfigError as exc:
            rows.append(("fail", f"target {name}", str(exc)))
            continue
        if not (target.path / ".git").exists():
            rows.append(("fail", f"target {name}", f"{target.path} is not a git repository"))
        else:
            rows.append(("ok", f"target {name}", str(target.path)))
    return rows


def report() -> int:
    rows = checks()
    width = max(len(name) for _, name, _ in rows)
    for status, name, detail in rows:
        print(f"{status:<4}  {name:<{width}}  {detail}")
    failed = sum(1 for status, _, _ in rows if status == "fail")
    print(f"\n{failed} problem{'' if failed == 1 else 's'} to fix" if failed else "\nready")
    return 1 if failed else 0
