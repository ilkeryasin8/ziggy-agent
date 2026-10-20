"""`ziggy migrate`: the one-time move of an install from before the rename (bmad-pipeline) to Ziggy's paths.

Steps, each one a no-op when there is nothing left to do (so a second run changes nothing):
  1. refuse while anything of the old install is active: a live process of the old CLI (a run, job, chat or listener),
     an old panel serving outside launchd, or a live lock owner / job / chat pid under the old state directory;
  2. launchd: boot the old panel service out (only when its plist is installed; it is reinstalled under the new
     label at the end, on the same port);
  3. rename ~/.config/bmad-pipeline and $XDG_STATE_HOME/bmad-pipeline to .../ziggy (refused when the new one already
     exists and is not empty);
  4. rewrite the old absolute paths inside the state's JSON files, config.toml and the virtualenvs the targets'
     prepare commands built under the state (scripts, .pth files, pyvenv.cfg);
  5. `git worktree repair` every moved worktree from its main repository and check `git worktree list` shows it;
  6. rename the Claude Code project directories named after a moved path (~/.claude/projects and every target's
     CLAUDE_CONFIG_DIR), so `claude --resume` and chat history find their sessions;
  7. install the panel under the new label; record migrated.json in the new state directory.
--dry-run prints the same plan from the old directories and changes nothing.
"""
import json
import os
import plistlib
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import config

OLD_NAME = "bmad-pipeline"
OLD_LABEL = "local.bmad-pipeline.ui"
RECORD = "migrated.json"
# Directories never walked for state files: checkouts (they hold a .git entry) are the targets' own content.
_SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "worktrees"}
_OLD_PROCESS = re.compile(r"bin/bmad-pipeline\b|\bbmad_pipeline\b")


class MigrateError(Exception):
    pass


def _xdg(var: str, default: Path) -> Path:
    return Path(os.environ.get(var) or default)


def paths() -> dict[str, Path]:
    config_base = _xdg("XDG_CONFIG_HOME", Path.home() / ".config")
    state_base = _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return {"old_config": config_base / OLD_NAME, "new_config": config_base / "ziggy",
            "old_state": state_base / OLD_NAME, "new_state": config.state_home()}


def old_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{OLD_LABEL}.plist"


def claude_encode(path: Path | str) -> str:
    """The project directory name Claude Code gives a working directory: every character but [A-Za-z0-9-] → '-'."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path))


def _replace_prefix(text: str, old: Path, new: Path) -> str:
    """Every occurrence of the absolute path `old` (as a whole path component) becomes `new`."""
    return re.sub(re.escape(str(old)) + r"(?![A-Za-z0-9._-])", lambda _m: str(new), text)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return pid > 0


def processes() -> list[tuple[int, str]]:
    """(pid, command line) of every process on the machine."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), command.strip()))
    return rows


def launchd_pid(label: str) -> int | None:
    """The pid launchd runs the service under, None when it is not loaded or not running."""
    proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    m = re.search(r"^\s*pid = (\d+)", proc.stdout, re.M) if proc.returncode == 0 else None
    return int(m.group(1)) if m else None


def launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _walk(root: Path):
    """Files under the state root, skipping checkouts, virtualenvs and dependency trees."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not (Path(dirpath, d) / ".git").exists()]
        for name in filenames:
            yield Path(dirpath, name)


def blockers(old_state: Path, launchd_panel: int | None) -> list[str]:
    """Why the move cannot happen now: what of the old install is still running."""
    out = []
    me = os.getpid()
    for pid, command in processes():
        if pid in (me, launchd_panel) or not _OLD_PROCESS.search(command):
            continue
        what = "the old panel is serving" if re.search(r"\sui(\s|$)", command) else "an old process is running"
        out.append(f"{what}: pid {pid} {command[:160]}")
    if not old_state.is_dir():
        return out
    for path in _walk(old_state):
        pid = None
        try:
            if path.name == "owner" and path.parent.parent.name == "locks":
                pid = int(path.read_text().split()[0])
            elif path.name == "job.json" and not (path.parent / "exit").exists():
                pid = int(json.loads(path.read_text()).get("pid") or 0)
            elif path.suffix == ".pid":
                pid = int(path.read_text().split()[0])
        except (OSError, ValueError, IndexError, AttributeError):
            continue
        if pid and pid != me and _pid_alive(pid):
            out.append(f"active: pid {pid} holds {path.relative_to(old_state)}")
    return out


def _nonempty(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _worktrees(root: Path) -> list[Path]:
    """Linked worktrees under the state root (their .git is a file): <target>/worktrees/<id> and the like."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if ".git" in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []
            continue
        dirnames[:] = [] if depth >= 3 else [d for d in dirnames if d not in {"node_modules", ".git", "venv", ".venv"}]
    return sorted(found)


def _main_repo(worktree: Path) -> Path | None:
    """The main repository's git dir of a linked worktree, from its .git file (gitdir: <common>/worktrees/<name>)."""
    try:
        line = (worktree / ".git").read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (worktree / gitdir).resolve()
    return gitdir.parent.parent if gitdir.parent.name == "worktrees" else None


def _git_args(common: Path) -> list[str]:
    return ["git", "-C", str(common.parent)] if common.name == ".git" else ["git", f"--git-dir={common}"]


def _listed(common: Path) -> set[str]:
    proc = subprocess.run([*_git_args(common), "worktree", "list", "--porcelain"], capture_output=True, text=True)
    return {line[len("worktree "):] for line in proc.stdout.splitlines() if line.startswith("worktree ")}


def _venv_files(root: Path) -> dict[Path, list[Path]]:
    """The files of every virtualenv under the state root (shared ones and the worktrees' .venv) that hold absolute
    paths: pyvenv.cfg, the scripts in bin/ and the .pth files."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if "pyvenv.cfg" in filenames:
            venv = Path(dirpath)
            files = [venv / "pyvenv.cfg"]
            files += [p for p in sorted((venv / "bin").iterdir()) if p.is_file() and not p.is_symlink()] if (venv / "bin").is_dir() else []
            out[venv] = files + sorted(venv.glob("lib/python*/site-packages/*.pth"))
            dirnames[:] = []
            continue
        dirnames[:] = [] if depth >= 4 else [d for d in dirnames if d not in {"node_modules", ".git"}]
    return out


def _claude_dirs(config_text: str | None) -> list[Path]:
    dirs = [Path.home() / ".claude" / "projects"]
    try:
        targets = tomllib.loads(config_text or "").get("targets") or {}
    except tomllib.TOMLDecodeError:
        targets = {}
    for table in targets.values():
        cdir = ((table or {}).get("env") or {}).get("CLAUDE_CONFIG_DIR")
        if cdir:
            dirs.append(Path(os.path.expanduser(cdir)) / "projects")
    return list(dict.fromkeys(d for d in dirs if d.is_dir()))


def _plist_port(path: Path) -> int:
    try:
        with path.open("rb") as f:
            argv = plistlib.load(f).get("ProgramArguments") or []
        return int(argv[argv.index("--port") + 1])
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
        return 8787


def run(dry_run: bool, install_panel, out=print) -> int:
    """Plan (dry run) or perform the migration; every action is printed. 0: done or nothing to do, 1: refused."""
    p = paths()
    old_config, new_config, old_state, new_state = p["old_config"], p["new_config"], p["old_state"], p["new_state"]
    verb = "would " if dry_run else ""
    actions: list[str] = []

    def act(text: str) -> None:
        actions.append(text)
        out(f"{verb}{text}")

    refused = [f"{new} exists and is not empty; move or remove it first"
               for old, new in ((old_config, new_config), (old_state, new_state)) if old.is_dir() and _nonempty(new)]
    plist = old_plist()
    panel_pid = launchd_pid(OLD_LABEL) if plist.exists() else None
    refused += blockers(old_state, panel_pid)
    if refused:
        for reason in refused:
            out(f"refused: {reason}")
        return 1

    # The state as it is now (dry run: still under the old name) and where every path in it ends up.
    moving = old_state.is_dir()
    state_now = old_state if moving and dry_run else new_state
    config_now = old_config if old_config.is_dir() and dry_run else new_config

    if plist.exists():
        act(f"stop the old panel: launchctl bootout gui/{os.getuid()}/{OLD_LABEL}"
            + (f" (pid {panel_pid})" if panel_pid else ""))
        if not dry_run:
            launchctl("bootout", f"gui/{os.getuid()}/{OLD_LABEL}")

    for old, new in ((old_config, new_config), (old_state, new_state)):
        if old.is_dir():
            act(f"move {old} → {new}")
            if not dry_run:
                new.parent.mkdir(parents=True, exist_ok=True)
                if new.is_dir():
                    new.rmdir()  # empty (checked above)
                os.rename(old, new)

    prefixes = ((old_state, new_state), (old_config, new_config))

    def rewrite(path: Path) -> bool:
        """Replace the old paths in one text file (binary files are left alone); True when it had any."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        new_text = text
        for old, new in prefixes:
            new_text = _replace_prefix(new_text, old, new)
        if new_text == text:
            return False
        if not dry_run:
            mode = path.stat().st_mode
            path.write_text(new_text, encoding="utf-8")
            os.chmod(path, mode)
        return True

    if state_now.is_dir():
        for path in sorted(_walk(state_now)):
            if path.suffix == ".json" and path.name != RECORD:
                if rewrite(path):
                    act(f"rewrite old paths in {new_state / path.relative_to(state_now)}")
        for venv, files in _venv_files(state_now).items():
            count = sum(rewrite(path) for path in files)
            if count:
                act(f"rewrite old paths in {count} file(s) of the virtualenv {new_state / venv.relative_to(state_now)}")
    if (config_now / "config.toml").is_file() and rewrite(config_now / "config.toml"):
        act(f"rewrite old paths in {new_config / 'config.toml'}")

    # Worktrees: repair them from their main repository, then check git lists them where they are now.
    by_repo: dict[Path, list[Path]] = {}
    for wt in _worktrees(state_now) if state_now.is_dir() else []:
        common = _main_repo(wt)
        if common is not None and common.is_dir():
            by_repo.setdefault(common, []).append(Path(os.path.realpath(new_state)) / wt.relative_to(state_now))
    for common, wts in sorted(by_repo.items()):
        listed = _listed(common)
        stale = [wt for wt in wts if str(wt) not in listed]
        if not stale:
            continue
        cmd = [*_git_args(common), "worktree", "repair", *map(str, stale)]
        act(" ".join(cmd))
        if dry_run:
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        missing = [wt for wt in stale if str(wt) not in _listed(common)]
        if proc.returncode != 0 or missing:
            raise MigrateError(f"git worktree repair in {common}: {proc.stderr.strip() or 'not listed: ' + ', '.join(map(str, missing))}")
        out(f"  git worktree list shows {len(stale)} repaired worktree(s) at the new path")

    # Claude Code project directories named after a moved working directory.
    config_file = config_now / "config.toml"
    config_text = config_file.read_text(encoding="utf-8") if config_file.is_file() else None
    old_enc, new_enc = claude_encode(old_state), claude_encode(new_state)
    for projects in _claude_dirs(config_text):
        for entry in sorted(projects.iterdir()):
            name = entry.name
            if not entry.is_dir() or not (name == old_enc or name.startswith(old_enc + "-")):
                continue
            target = projects / (new_enc + name[len(old_enc):])
            if target.exists():
                out(f"skip {entry}: {target.name} already exists")
                continue
            act(f"rename {entry} → {target.name}")
            if not dry_run:
                os.rename(entry, target)

    if plist.exists():
        port = _plist_port(plist)
        act(f"remove {plist}; install the panel as local.ziggy.ui on port {port} (ziggy ui --install)")
        if not dry_run:
            plist.unlink()
            install_panel(port)

    if not actions:
        out("nothing to migrate")
        return 0
    if dry_run:
        out(f"dry run: {len(actions)} action(s), nothing changed")
        return 0
    new_state.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "from": {"config": str(old_config), "state": str(old_state)},
              "to": {"config": str(new_config), "state": str(new_state)}, "actions": actions}
    path = new_state / RECORD
    try:  # a run that finished an interrupted one adds to the record
        runs = json.loads(path.read_text(encoding="utf-8")).get("runs") or []
    except (OSError, ValueError, AttributeError):
        runs = []
    path.write_text(json.dumps({"runs": [*runs, record]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out(f"recorded {new_state / RECORD}")
    return 0
