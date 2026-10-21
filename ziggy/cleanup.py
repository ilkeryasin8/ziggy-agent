"""Removing the worktrees of finished tickets.

A ticket's worktree is kept after its PR is opened (reviews, fixes and PR updates use it). Once the
PR is merged or closed on GitHub it is only disk: `clean` removes it with `git worktree remove`
(never forced, so a dirty tree stops it), deletes the local branch when the PR was merged (the
remote has every commit), and marks the ticket archived. Run records stay, so history and costs do.

`remove` takes one finished ticket's worktree away on request, whatever its PR: the branch stays
and the next run recreates the worktree from it. Both run the target's teardown commands first
and release the ticket's port range.
"""
import json
import sys
from pathlib import Path

from . import memory, roadmap, stack, state
from .config import Target
from .publish import PublishError, gh_as
from .worktree import GitError, dirty_paths, git, is_worktree, run_teardown, shared_git

CLEANABLE = ("MERGED", "CLOSED")
FINISHED = ("done", "failed", stack.SPLIT)  # a split container keeps no work in its worktree


class CleanupError(Exception):
    pass


def _pr(target: Target, url: str) -> dict:
    """The PR's state and the last commit pushed to it (headRefOid), or {} when unreadable."""
    try:
        return json.loads(gh_as(target.gh_user, "pr", "view", url, "--json", "state,headRefOid", cwd=target.path))
    except (PublishError, ValueError):
        return {}


def _on_pr(wt: Path, pr_head: str | None) -> bool:
    """Every local commit of the branch reached the PR (its head is the PR's head or behind it).
    Works after the remote branch was deleted on merge, since the PR keeps its head commit. A restacked slice was
    pushed as rewritten commits, so the same changes sit in the PR under other ids: the same tree as the PR's head,
    or every local commit patch-equivalent to one in the PR (`git cherry` marks each with '-'), counts as reached."""
    if not pr_head:
        return False
    if git("merge-base", "--is-ancestor", "HEAD", pr_head, cwd=wt, check=False).returncode == 0:
        return True
    if git("cat-file", "-e", f"{pr_head}^{{commit}}", cwd=wt, check=False).returncode != 0:
        return False  # the PR head is not here to compare with: keep the work
    trees = [git("rev-parse", f"{rev}^{{tree}}", cwd=wt, check=False).stdout.strip() for rev in ("HEAD", pr_head)]
    if trees[0] and trees[0] == trees[1]:
        return True
    cherry = git("cherry", pr_head, "HEAD", cwd=wt, check=False)
    lines = cherry.stdout.split()
    return cherry.returncode == 0 and all(line.startswith("-") for line in cherry.stdout.splitlines() if line.strip()) \
        and bool(lines)


def inspect(target: Target, ticket_id: str, pr: dict | None = None, held: bool = False) -> dict | None:
    """What cleaning this ticket would do, and why it cannot when it cannot; None when the
    ticket has nothing to clean (no worktree, or no PR)."""
    st = state.load_state(target, ticket_id)
    t = state.load_ticket(target, ticket_id)
    if not st.get("worktree") or st.get("archived") or not st.get("pr"):
        return None
    wt = Path(st["worktree"])
    pr = pr if pr is not None else _pr(target, st["pr"])
    pr_state = pr.get("state")
    reasons = []
    if not held and state.running(target, ticket_id):
        reasons.append("running")
    if pr_state not in CLEANABLE:
        reasons.append(f"PR is {pr_state.lower() if pr_state else 'unreadable'}")
    dirty = dirty_paths(wt) if wt.is_dir() else []
    if dirty:
        reasons.append(f"uncommitted: {', '.join(dirty[:5])}")
    if wt.is_dir() and not _on_pr(wt, pr.get("headRefOid")):
        reasons.append("local commits that the PR does not have")
    return {"target": target.name, "id": ticket_id, "title": t["title"], "branch": t["branch"], "pr": st["pr"],
            "pr_state": pr_state, "worktree": str(wt), "delete_branch": pr_state == "MERGED",
            "cleanable": not reasons, "reasons": reasons}


def candidates(target: Target) -> list[dict]:
    if not target.gh_user:
        return []
    with shared_git(target, "fetch --prune for cleanup"):
        git("fetch", "--quiet", "--prune", target.remote, cwd=target.path, check=False, timeout=120)
    found = (inspect(target, ticket_id) for ticket_id in state.ticket_ids(target))
    return [c for c in found if c]


def clean(target: Target, ticket_id: str, pr: dict | None = None) -> dict:
    """Remove the worktree of a ticket whose PR is merged or closed; returns what was done."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} cleanup"):
        info = inspect(target, ticket_id, pr=pr, held=True)  # this process holds the ticket lock now
        if info is None:
            raise CleanupError(f"{ticket_id} has no worktree with a PR to clean")
        if not info["cleanable"]:
            raise CleanupError(f"{ticket_id} is kept: {'; '.join(info['reasons'])}")
        wt = Path(info["worktree"])
        if wt.is_dir():
            failed = run_teardown(target, ticket_id, wt, teardown_log(target, ticket_id))
            if failed:
                raise CleanupError(f"{ticket_id} is kept: {failed}")
        if wt.exists():
            git("worktree", "remove", str(wt), cwd=target.path)  # not --force: a dirty tree stops here
        git("worktree", "prune", cwd=target.path, check=False)
        deleted = False
        if info["delete_branch"]:
            deleted = git("branch", "-D", info["branch"], cwd=target.path, check=False).returncode == 0
        st = state.load_state(target, ticket_id)
        st["archived"] = {"at": state.now(), "pr_state": info["pr_state"], "branch_deleted": deleted, "worktree": str(wt)}
        st["worktree"] = None
        st["prepared"] = False
        st["port_base"] = None
        st["pr_state"] = info["pr_state"]
        st.pop("cleanup_blocked", None)
        state.save_state(target, ticket_id, st)
        return {"id": ticket_id, "pr_state": info["pr_state"], "branch_deleted": deleted}


def sync(target: Target) -> list[dict]:
    """Follow every published, unarchived ticket's PR: record its state, move the slices stacked on a merged
    one to its base (stack.on_merged), and archive and clean a ticket whose PR was merged or closed. A worktree
    that still holds work is left alone and the reasons are recorded for the panel."""
    out = []
    if not target.gh_user:
        return out
    for ticket_id in state.ticket_ids(target):
        st = state.load_state(target, ticket_id)
        if not st.get("pr") or st.get("archived") or state.running(target, ticket_id):
            continue
        pr = _pr(target, st["pr"])
        if not pr.get("state"):
            continue
        if pr["state"] != st.get("pr_state"):
            st["pr_state"] = pr["state"]
            state.save_state(target, ticket_id, st)
        if pr["state"] not in CLEANABLE:
            continue
        if pr["state"] == "MERGED":
            try:  # the merge goes on the ticket's episode (once); best effort, like the rest of the sync
                memory.episode_merged(target, ticket_id, st.get("pr"))
                if not any(u.get("source") in ("user", "github") for u in st.get("user_reviews") or []):
                    memory.record_outcome(target, ticket_id, "clean")  # merged without a change asked
            except Exception as exc:  # noqa: BLE001 - a missed line never holds up archiving
                print(f"{ticket_id}: merge not added to its episode: {exc}", file=sys.stderr)
            roadmap.ticket_changed(target, ticket_id)  # its roadmap item, if any, is merged (best effort)
            # Before it is archived (and its local branch deleted): the slices stacked on it move to its base. One
            # not moved yet (running, or GitHub refused) keeps this ticket for the next sync.
            try:
                moved = stack.on_merged(target, ticket_id, pr)
            except (state.LockError, GitError) as exc:
                moved = [{"id": ticket_id, "kept": [f"stacked slices not moved yet: {exc}"]}]
            out.extend(moved)
            if any("kept" in m for m in moved):
                continue
        info = inspect(target, ticket_id, pr=pr) if st.get("worktree") else None
        if info is not None and not info["cleanable"]:
            st = state.load_state(target, ticket_id)
            if st.get("cleanup_blocked") != info["reasons"]:
                st["cleanup_blocked"] = info["reasons"]
                state.save_state(target, ticket_id, st)
            out.append({"id": ticket_id, "kept": info["reasons"]})
            continue
        try:
            if info is None:  # no worktree left: archive the record only
                st = state.load_state(target, ticket_id)
                st["archived"] = {"at": state.now(), "pr_state": pr["state"], "branch_deleted": False, "worktree": None}
                state.save_state(target, ticket_id, st)
                out.append({"id": ticket_id, "archived": True})
            else:
                out.append(clean(target, ticket_id, pr=pr))
        except CleanupError as exc:  # a failing teardown: shown like any other reason the worktree is kept
            st = state.load_state(target, ticket_id)
            if st.get("cleanup_blocked") != [str(exc)]:
                st["cleanup_blocked"] = [str(exc)]
                state.save_state(target, ticket_id, st)
            out.append({"id": ticket_id, "kept": [str(exc)]})
        except state.LockError as exc:
            out.append({"id": ticket_id, "kept": [str(exc)]})
    return out


def teardown_log(target: Target, ticket_id: str) -> Path:
    return state.run_dir(target, ticket_id) / "teardown.log"


def remove(target: Target, ticket_id: str, force: bool = False) -> dict:
    """Remove a finished (done or failed) ticket's worktree: teardown commands first, then `git
    worktree remove`. Uncommitted files or a failing teardown stop it unless force. The branch
    stays; the port range is released; the next run recreates the worktree from the branch."""
    try:
        with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} worktree removal"):
            return _remove(target, ticket_id, force)
    except state.LockError as exc:
        raise CleanupError(f"{ticket_id} has an active run ({exc})") from None


def _remove(target: Target, ticket_id: str, force: bool) -> dict:
    t = state.load_ticket(target, ticket_id)
    st = state.load_state(target, ticket_id)
    if not st.get("worktree"):
        raise CleanupError(f"{ticket_id} has no worktree")
    if st.get("status") not in FINISHED:
        raise CleanupError(f"{ticket_id} is {st.get('status') or 'not started'}; only a done or failed ticket's worktree is removed")
    wt = Path(st["worktree"])
    warnings = []
    if wt.exists():
        if not is_worktree(target, wt):
            raise CleanupError(f"{wt} is not a git worktree; leaving it alone")
        if not force:
            dirty = dirty_paths(wt)
            if dirty:
                raise CleanupError(f"{ticket_id}'s worktree has {len(dirty)} uncommitted file(s) ({', '.join(dirty[:5])}); "
                                   "commit them or pass --force")
        failed = run_teardown(target, ticket_id, wt, teardown_log(target, ticket_id))
        if failed and not force:
            raise CleanupError(f"{failed}; pass --force to remove anyway")
        if failed:
            warnings.append(failed)
        with shared_git(target, f"worktree removal of {ticket_id}"):
            try:
                git("worktree", "remove", *(["--force"] if force else []), str(wt), cwd=target.path)
            except GitError as exc:
                raise CleanupError(f"{exc}; pass --force to remove anyway" if not force else str(exc)) from None
            git("worktree", "prune", cwd=target.path, check=False)
    else:
        git("worktree", "prune", cwd=target.path, check=False)
    st = state.load_state(target, ticket_id)
    released = st.get("port_base")
    st.update(worktree=None, prepared=False, port_base=None,
              worktree_removed={"at": state.now(), "path": str(wt), "forced": force, "teardown_failed": bool(warnings)})
    state.save_state(target, ticket_id, st)
    message = f"removed {ticket_id}'s worktree; branch {t['branch']} is kept" + (" (teardown failed, forced)" if warnings else "")
    return {"id": ticket_id, "worktree": str(wt), "branch": t["branch"], "port_released": released,
            "teardown": warnings, "message": message}
