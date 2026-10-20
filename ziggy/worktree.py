"""Git side of a run: worktree creation, preserved files, ports, prepare and teardown, remote checks.

Everything here runs in the wrapper, outside the Claude Code sandbox; it is the only code
that touches the network (fetch, ls-remote). Nothing here pushes.
"""
import fnmatch
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Target, expand

TEARDOWN_TIMEOUT = 180  # seconds per teardown command


class GitError(Exception):
    pass


def git(*args: str, cwd: Path, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def branch_taken(target: Target, branch: str) -> bool:
    for ref in (f"refs/heads/{branch}", f"refs/remotes/{target.remote}/{branch}"):
        if git("show-ref", "--verify", "--quiet", ref, cwd=target.path, check=False).returncode == 0:
            return True
    return False


def target_env(target: Target, ticket_id: str, wt: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(environment, placeholder values): os.environ plus the target's [env] table, with
    {run_id}, {state}, {worktree}, {target} and {checkout} (both the main checkout) expanded; plus
    PIPELINE_PORT/PIPELINE_PORT_END (and {port}, {port_end}) once the ticket holds a port range."""
    values = {"run_id": ticket_id, "state": str(target.state_dir), "worktree": str(wt), "target": str(target.path),
              "checkout": str(target.path)}
    try:
        held = port_env(target, state.load_state(target, ticket_id).get("port_base"))
    except (OSError, ValueError):
        held = {}
    if held:
        values.update(port=held["PIPELINE_PORT"], port_end=held["PIPELINE_PORT_END"])
    return {**os.environ, **held, **{k: expand(v, values) for k, v in target.env.items()}}, values


def _port_bases(target: Target) -> list[int]:
    p = target.ports
    return [p["start"] + k * p["size"] for k in range(p["count"])]


def port_env(target: Target, base) -> dict[str, str]:
    """{"PIPELINE_PORT", "PIPELINE_PORT_END"} for a base that is one of the target's ranges, else {}."""
    if target.ports is None or isinstance(base, bool) or base not in _port_bases(target):
        return {}
    return {"PIPELINE_PORT": str(base), "PIPELINE_PORT_END": str(base + target.ports["size"] - 1)}


def _held_ports(target: Target, ticket_id: str) -> set[int]:
    """Every port_base the target's other tickets hold (kept until their worktree is removed)."""
    held = set()
    for other in state.ticket_ids(target):
        if other == ticket_id:
            continue
        try:
            base = state.load_state(target, other).get("port_base")
        except (OSError, ValueError):
            continue
        if isinstance(base, int) and not isinstance(base, bool):
            held.add(base)
    return held


def ports(target: Target, ticket_id: str, st: dict) -> dict[str, str]:
    """The ticket's port range as environment ({} when the target has no `ports`): kept in
    st["port_base"] across runs, else the first range no other ticket of this target holds
    (targets' ranges never overlap, so a per-target lock is enough). Saves st when it changes."""
    if target.ports is None:
        return {}
    env = port_env(target, st.get("port_base"))
    if env:
        return env
    size = target.ports["size"]
    with state.waiting_lock(target, "ports", f"port range for {ticket_id}"):
        held = _held_ports(target, ticket_id)
        base = next((b for b in _port_bases(target) if not any(h < b + size and b < h + size for h in held)), None)
        if base is None:
            sys.stderr.write(f"{ticket_id}: no free port range (all {target.ports['count']} are held by tickets with a "
                             "worktree); stages run without PIPELINE_PORT\n")
            if st.get("port_base") is not None:  # a range the config no longer has
                st["port_base"] = None
                state.save_state(target, ticket_id, st)
            return {}
        st["port_base"] = base
        state.save_state(target, ticket_id, st)
    return port_env(target, base)


def ignored_paths(repo: Path, rels: list[str]) -> set[str]:
    """The paths git ignores in repo. Tracked files never count, even when a pattern matches them."""
    if not rels:
        return set()
    try:
        proc = subprocess.run(["git", "-C", str(repo), "check-ignore", "-z", "--stdin"], input=("\0".join(rels) + "\0").encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:  # 1: none is ignored; above that, an error
        return set()
    return {p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def _glob_regex(pattern: str) -> re.Pattern:
    """A path glob: `**/` any directories (or none), `**` anything, `*` and `?` within one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)  # a symlink is never copied, wherever it points
    except OSError:
        return False


def _matches(repo: Path, pattern: str) -> list[str]:
    real = Path(os.path.realpath(repo))
    if "/" not in pattern:
        return sorted(n for n in os.listdir(repo) if n != ".git" and fnmatch.fnmatchcase(n, pattern) and _regular(repo / n))
    parts = pattern.split("/")
    fixed = []
    for part in parts[:-1]:
        if any(c in part for c in "*?"):
            break
        fixed.append(part)
    start = repo.joinpath(*fixed)
    if not start.is_dir() or Path(os.path.realpath(start)) != real.joinpath(*fixed):
        return []  # missing, or reached through a symlink
    regex, found = _glob_regex(pattern), []
    for root, dirs, files in os.walk(start, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in files:
            rel = (Path(root) / name).relative_to(repo).as_posix()
            if regex.match(rel) and _regular(Path(root) / name):
                found.append(rel)
    return sorted(found)


def _inside(path: Path, root: Path) -> bool:
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))


def preserve(target: Target, wt: Path, patterns: tuple[str, ...] | None = None) -> list[str]:
    """Copy the main checkout's gitignored files matching target.preserve (.env, local settings), or
    `patterns` when given, into the worktree, only where the worktree has nothing yet; returns the
    copied paths. Tracked files are never copied (the branch's version wins), symlinks are never followed."""
    patterns = target.preserve if patterns is None else patterns
    if not patterns:
        return []
    repo, candidates = target.path, []
    for pattern in patterns:
        try:
            found = _matches(repo, pattern)
        except OSError:
            continue
        for rel in found:
            if ".git" in rel.split("/") or rel in candidates or os.path.lexists(wt / rel):
                continue
            candidates.append(rel)
    ignored = ignored_paths(repo, candidates)
    copied = []
    for rel in candidates:
        src, dst = repo / rel, wt / rel
        if rel not in ignored or not _inside(src, repo) or not _inside(dst.parent, wt):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(dst.parent, wt) or os.path.lexists(dst):
            continue
        shutil.copy2(src, dst, follow_symlinks=False)
        copied.append(rel)
    return sorted(copied)


def shared_git(target: Target, what: str):
    """Runs share the target's .git: fetches and worktree adds go one at a time (two writers of
    the same ref fail with 'cannot lock ref')."""
    return state.waiting_lock(target, "git", what)


def base_ref(target: Target, ticket: dict) -> str:
    """The ref a ticket's branch is measured against: an adopted PR's base, the local branch of the slice below
    it while it is stacked (`stack_base`; local branch refs are shared by every worktree, so a slice needs no push
    to start), else <remote>/<base>."""
    if (ticket.get("pull") or {}).get("base_ref"):
        return ticket["pull"]["base_ref"]
    if ticket.get("stack_base"):
        return f"refs/heads/{ticket['base_branch']}"
    return f"{target.remote}/{ticket['base_branch']}"


def create(target: Target, ticket: dict, wt: Path) -> None:
    """git worktree add --no-track -b <branch> <wt> <remote>/<base>, after a fetch of base (a stacked slice: from
    the local branch of the slice below, no fetch); for a ticket whose worktree `cleanup.remove` took away,
    `git worktree add <wt> <branch>` on the kept branch."""
    with shared_git(target, f"worktree for {ticket['id']}"):
        _create(target, ticket, wt)


def _create(target: Target, ticket: dict, wt: Path) -> None:
    base = ticket["base_branch"]
    stacked = bool(ticket.get("stack_base"))
    fetch = ("fetch", "--quiet", target.remote, f"refs/heads/{base}:refs/remotes/{target.remote}/{base}")
    if git("show-ref", "--verify", "--quiet", f"refs/heads/{ticket['branch']}", cwd=target.path, check=False).returncode == 0:
        if not _removed_by_cleanup(target, ticket["id"]):  # a slug collision, not this ticket's kept branch
            raise GitError(f"branch {ticket['branch']} already exists in {target.path}; delete it or create a new ticket")
        if not stacked:
            git(*fetch, cwd=target.path, check=False)  # fresh base for merge-base; the branch itself needs no network
        wt.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--quiet", str(wt), ticket["branch"], cwd=target.path)
        return
    if stacked:
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{base}", cwd=target.path, check=False).returncode != 0:
            raise GitError(f"the slice below ({ticket['stack_base']}) has no branch {base} yet; it must run first")
    else:
        git(*fetch, cwd=target.path)
    wt.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--quiet", "--no-track", "-b", ticket["branch"], str(wt), base_ref(target, ticket), cwd=target.path)


def _removed_by_cleanup(target: Target, ticket_id: str) -> bool:
    try:
        return bool(state.load_state(target, ticket_id).get("worktree_removed"))
    except (OSError, ValueError):
        return False


def pull_ref(target: Target, number: int) -> str:
    # Outside refs/remotes/: `fetch --prune` (cleanup) would delete it there, no branch matching it.
    return f"refs/ziggy/{target.remote}/pull/{number}"


def fetch_pull(target: Target, number: int) -> str:
    """Fetch a pull request's head (GitHub keeps refs/pull/<n>/head for forks too, and force-pushes
    move it, hence the +); returns its commit."""
    with shared_git(target, f"fetch of PR #{number}"):
        return _fetch_pull(target, number)


def _fetch_pull(target: Target, number: int) -> str:
    git("fetch", "--quiet", target.remote, f"+refs/pull/{number}/head:{pull_ref(target, number)}", cwd=target.path, timeout=120)
    return git("rev-parse", pull_ref(target, number), cwd=target.path).stdout.strip()


def create_from_pull(target: Target, ticket: dict, wt: Path) -> str:
    """A worktree on a local branch at an open PR's head (the PR's own branch may be checked out
    in the main checkout, so the local name is the ticket's); returns the head commit."""
    with shared_git(target, f"worktree for {ticket['id']}"):
        return _create_from_pull(target, ticket, wt)


def _create_from_pull(target: Target, ticket: dict, wt: Path) -> str:
    base = ticket["base_branch"]
    git("fetch", "--quiet", target.remote, f"refs/heads/{base}:refs/remotes/{target.remote}/{base}", cwd=target.path)
    head = _fetch_pull(target, ticket["pull"]["number"])
    if git("show-ref", "--verify", "--quiet", f"refs/heads/{ticket['branch']}", cwd=target.path, check=False).returncode == 0:
        raise GitError(f"branch {ticket['branch']} already exists in {target.path}; delete it first")
    wt.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--quiet", "--no-track", "-b", ticket["branch"], str(wt), head, cwd=target.path)
    return head


def is_ancestor(wt: Path, older: str, newer: str) -> bool:
    return git("merge-base", "--is-ancestor", older, newer, cwd=wt, check=False).returncode == 0


def is_worktree(target: Target, wt: Path) -> bool:
    listing = git("worktree", "list", "--porcelain", cwd=target.path).stdout
    return f"worktree {wt.resolve()}" in listing.splitlines()


def run_prepare(target: Target, ticket_id: str, wt: Path, log: Path) -> None:
    env, values = target_env(target, ticket_id, wt)
    with log.open("a", encoding="utf-8") as out:
        for command in target.prepare:
            command = expand(command, values)
            out.write(f"$ {command}\n")
            out.flush()
            code = subprocess.run(command, shell=True, cwd=wt, env=env, stdout=out, stderr=subprocess.STDOUT).returncode
            if code != 0:
                raise GitError(f"prepare command failed with exit {code}: {command} (log: {log})")


def run_teardown(target: Target, ticket_id: str, wt: Path, log: Path) -> str | None:
    """target.teardown in the worktree (outside the sandbox, the target's env), appended to log;
    None when every command passed, else why the first one failed."""
    if not target.teardown:
        return None
    env, values = target_env(target, ticket_id, wt)
    log.parent.mkdir(parents=True, exist_ok=True)
    for command in target.teardown:
        command = expand(command, values)
        with log.open("a", encoding="utf-8") as out:
            out.write(f"$ {command}\n")
            out.flush()
            start = out.tell()
            proc = subprocess.Popen(command, shell=True, cwd=wt, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                code = proc.wait(timeout=TEARDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                out.write(f"timed out after {TEARDOWN_TIMEOUT}s\n")
                code = "timeout"
        if code != 0:
            with log.open("rb") as f:
                f.seek(start)
                tail = [line for line in f.read().decode("utf-8", "replace").splitlines() if line.strip()][-3:]
            return f"teardown failed (exit {code}): {' | '.join(tail) or command}"
    return None


def dirty_paths(wt: Path) -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout
    return [line[3:] for line in out.splitlines() if line]


def head(wt: Path) -> str:
    return git("rev-parse", "HEAD", cwd=wt).stdout.strip()


def merge_base(wt: Path, ref: str) -> str | None:
    proc = git("merge-base", "HEAD", ref, cwd=wt, check=False)
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def stash_all(wt: Path, message: str) -> str | None:
    """Stash every uncommitted change, untracked files included; returns the stash commit or None."""
    if not dirty_paths(wt):
        return None
    git("stash", "push", "--include-untracked", "--message", message, cwd=wt)
    return git("rev-parse", "stash@{0}", cwd=wt).stdout.strip()


def remote_refs(target: Target) -> str | None:
    """The remote's refs as ground truth for 'the run pushed nothing'; None when unreachable."""
    try:
        proc = git("ls-remote", target.remote, cwd=target.path, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    return proc.stdout if proc.returncode == 0 else None
