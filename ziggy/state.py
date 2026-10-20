"""Run state under ${XDG_STATE_HOME}/ziggy/<target>/.

    runs/<id>/ticket.json      ticket contract (wrapper-owned)
    runs/<id>/ticket.md        ticket text: Pipeline, Ticket (untrusted), Decisions (user)
    runs/<id>/answers.md       every answered question round (wrapper-owned, append-only)
    runs/<id>/inbox/           the agent's --add-dir (sandbox-writable): ticket.md copy, plan.md, deferred.md,
                               result.json of the running stage, artifacts/
    runs/<id>/state.json       status, pipeline, stage, run, next, rounds, gate, ratings, tasks, worktree
    runs/<id>/<nn>-<stage>/    run.json, prompt.md, claude.jsonl (event stream), claude.err, result.json,
                               plan.md snapshot, checks.log, pid while running
    worktrees/<id>/            the ticket's git worktree
    locks/                     mkdir-atomic locks: ticket-<id>, the run slots (active, active-2, …), git
"""
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import Target


class LockError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_dir(target: Target) -> Path:
    return target.state_dir / "runs"


def run_dir(target: Target, ticket_id: str) -> Path:
    return runs_dir(target) / ticket_id


def worktree_dir(target: Target, ticket_id: str) -> Path:
    return target.state_dir / "worktrees" / ticket_id


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ticket_ids(target: Target) -> list[str]:
    root = runs_dir(target)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if (p / "ticket.json").is_file()), key=_split_id)


def _split_id(ticket_id: str) -> tuple[str, int]:
    prefix, _, number = ticket_id.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (ticket_id, 0)


def next_id(target: Target, prefix: str) -> str:
    numbers = [n for p, n in map(_split_id, ticket_ids(target)) if p == prefix]
    return f"{prefix}-{max(numbers, default=0) + 1}"


def load_ticket(target: Target, ticket_id: str) -> dict:
    path = run_dir(target, ticket_id) / "ticket.json"
    if not path.is_file():
        raise LookupError(f"no ticket {ticket_id} for target {target.name}")
    return read_json(path)


def load_state(target: Target, ticket_id: str) -> dict:
    return read_json(run_dir(target, ticket_id) / "state.json")


def save_state(target: Target, ticket_id: str, state: dict) -> None:
    state["updated_at"] = now()
    write_json(run_dir(target, ticket_id) / "state.json", state)


PHASE_DIR = re.compile(r"^(\d{2,})-([a-z][a-z0-9-]*)$")


def phase_dirs(target: Target, ticket_id: str) -> list[Path]:
    """The ticket's session dirs (`NN-<stage>`), oldest first (numeric order, so 100 follows 99)."""
    root = run_dir(target, ticket_id)
    if not root.is_dir():
        return []
    found = [(int(m.group(1)), p) for p in root.iterdir() if p.is_dir() and (m := PHASE_DIR.match(p.name))]
    return [p for _, p in sorted(found)]


def new_phase_dir(target: Target, ticket_id: str, phase: str) -> Path:
    root = run_dir(target, ticket_id)
    count = max((int(PHASE_DIR.match(p.name).group(1)) for p in phase_dirs(target, ticket_id)), default=0)
    path = root / f"{count + 1:02d}-{phase}"
    path.mkdir()
    return path


def inbox_dir(target: Target, ticket_id: str) -> Path:
    return run_dir(target, ticket_id) / "inbox"


def plan_path(target: Target, ticket_id: str) -> Path:
    return inbox_dir(target, ticket_id) / "plan.md"


def running(target: Target, ticket_id: str) -> bool:
    """A live process holds the ticket's lock: a run, plan, build or answer is under way. A lock
    whose owner died (killed, machine slept through a crash) does not count; see lock_left."""
    path = target.state_dir / "locks" / f"ticket-{ticket_id}"
    try:
        line = (path / "owner").read_text()
    except FileNotFoundError:
        try:  # taken this instant, owner not written yet
            return (datetime.now().timestamp() - path.stat().st_mtime) < 10
        except FileNotFoundError:
            return False
    try:
        return _pid_alive(int(line.split()[0]))
    except (ValueError, IndexError):
        return False


def lock_left(target: Target, ticket_id: str) -> bool:
    """The ticket's lock is still there but its process is gone: the run died mid-way."""
    return (target.state_dir / "locks" / f"ticket-{ticket_id}").exists() and not running(target, ticket_id)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def lock(target: Target, name: str, what: str):
    """mkdir-atomic lock; a lock whose owner process is gone is taken over."""
    path = target.state_dir / "locks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            path.mkdir()
            break
        except FileExistsError:
            owner = path / "owner"
            try:
                pid = int(owner.read_text().split()[0])
            except (OSError, ValueError, IndexError):
                pid = None
            if pid is not None and _pid_alive(pid):
                raise LockError(f"{what} is held by pid {pid} ({owner.read_text().strip()})") from None
            if pid is None and not owner.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < 10:
                raise LockError(f"{what} is being taken by another process") from None
            shutil.rmtree(path, ignore_errors=True)
    else:
        raise LockError(f"{what}: could not take {path}")
    (path / "owner").write_text(f"{os.getpid()} {what} {now()}\n")
    try:
        yield
    finally:
        shutil.rmtree(path, ignore_errors=True)


def slot_names(target: Target) -> list[str]:
    """The target's run slots, one lock each; the first keeps the old single-slot name."""
    return ["active", *(f"active-{n}" for n in range(2, target.max_active + 1))]


def holder(target: Target, name: str) -> str | None:
    """The owner line of a lock whose process is alive, else None."""
    try:
        line = (target.state_dir / "locks" / name / "owner").read_text().strip()
        return line if _pid_alive(int(line.split()[0])) else None
    except (OSError, ValueError, IndexError):
        return None


def slot_holders(target: Target) -> list[str]:
    return [line for line in (holder(target, n) for n in slot_names(target)) if line]


@contextmanager
def waiting_lock(target: Target, name: str, what: str, timeout: float = 300, poll: float = 0.5):
    """`lock`, waiting for a live holder to let go (short critical sections such as git fetch)."""
    deadline = time.monotonic() + timeout
    while True:
        held = lock(target, name, what)
        try:
            held.__enter__()  # only taking the lock is retried, never the body
            break
        except LockError:
            if time.monotonic() > deadline:
                raise
            time.sleep(poll)
    try:
        yield
    finally:
        held.__exit__(None, None, None)
