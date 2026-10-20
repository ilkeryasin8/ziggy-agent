"""Open pull requests of a target, and adopting one as a ticket for a Codex review and fixes.

Any open PR (Dependabot's, one opened by hand, a teammate's) can be adopted: it becomes ticket
pr-<n>, done from the start, with a worktree on a local branch at the PR's head and `pr` set to the
PR. From there the ticket's own flow applies unchanged: `review` runs Codex against the PR's base,
the findings the user keeps go back through `fix` (pipeline.request_changes: a new run of the
ticket's pipeline that implements them on top of the PR's commits, keeping the PR's own change), and
`publish` pushes the new commits to the PR's own branch, never forced. A PR from a fork can be
reviewed but not pushed to.

The PR may move on while it is adopted (its author pushes). `sync` brings the worktree up to the
PR's head by fast-forward before a review or a fix; local fix commits not pushed yet are kept, and
a branch that diverged from the PR stops instead of being merged or overwritten.
"""
import json
import shutil
import time
from pathlib import Path

from . import state, worktree
from . import ticket as tickets
from .config import Target
from .publish import PublishError, gh_as
from .worktree import GitError, git

FIELDS = "number,title,url,author,headRefName,baseRefName,isDraft,isCrossRepository,headRefOid,updatedAt"
BODY_LIMIT = 8000  # a Dependabot body carries whole release notes
CACHE_SECONDS = 60
_cache: dict[str, tuple[float, list[dict]]] = {}


class PullError(Exception):
    pass


def ticket_id(number: int) -> str:
    return f"pr-{number}"


def _require(target: Target) -> None:
    if not target.github_repo or not target.gh_user:
        raise PullError(f"[targets.{target.name}] needs github_repo and gh_user to read pull requests")


def _gh_json(target: Target, *args: str):
    try:
        return json.loads(gh_as(target.gh_user, *args, cwd=target.path) or "null")
    except PublishError as exc:
        raise PullError(str(exc)) from None
    except ValueError:
        raise PullError(f"gh {args[0]} {args[1]} did not answer JSON") from None


def _author(pr: dict) -> str:
    return (pr.get("author") or {}).get("login") or "?"


def listing(target: Target, fresh: bool = False) -> list[dict]:
    """The target's open PRs, newest first, each with the ticket that follows it (a pipeline
    ticket whose PR it is, or its adopted pr-<n>) and that ticket's status."""
    _require(target)
    cached = _cache.get(target.name)
    if fresh or not cached or time.time() - cached[0] > CACHE_SECONDS:
        raw = _gh_json(target, "pr", "list", "--repo", target.github_repo, "--state", "open", "--limit", "50", "--json", FIELDS) or []
        cached = (time.time(), raw)
        _cache[target.name] = cached
    by_url = {}
    for tid in state.ticket_ids(target):
        st = state.load_state(target, tid)
        if st.get("pr") and not st.get("archived"):
            by_url[st["pr"]] = tid
    out = []
    for pr in sorted(cached[1], key=lambda p: p.get("updatedAt") or "", reverse=True):
        author = _author(pr)
        out.append({
            "number": pr["number"], "title": pr["title"], "url": pr["url"], "author": author,
            "bot": bool((pr.get("author") or {}).get("is_bot")), "mine": author == target.gh_user,
            "head": pr["headRefName"], "base": pr["baseRefName"], "draft": bool(pr.get("isDraft")),
            "fork": bool(pr.get("isCrossRepository")), "head_sha": pr.get("headRefOid"), "updated_at": pr.get("updatedAt"),
            "ticket": by_url.get(pr["url"]),
        })
    return out


def _typed(title: str) -> tuple[str, str]:
    """('feat', 'Show units') from 'feat: Show units'; a PR title without a known type is a fix."""
    try:
        kind, rest = tickets.split_type(title, default="fix")
    except ValueError:
        return "fix", title
    return (kind, rest) if kind in tickets.TYPES else ("fix", title)


def adopt(target: Target, number: int) -> dict:
    """The ticket of an open PR, made now when there is none: pr-<n>, done, with a worktree at
    the PR's head. Adopting the same PR again returns its ticket."""
    _require(target)
    tid = ticket_id(number)
    if (state.run_dir(target, tid) / "ticket.json").exists():
        return state.load_ticket(target, tid)
    pr = _gh_json(target, "pr", "view", str(number), "--repo", target.github_repo, "--json", f"{FIELDS},body,state")
    if not pr or pr.get("state") != "OPEN":
        raise PullError(f"PR #{number} is {(pr or {}).get('state', 'missing').lower()}; only an open PR is adopted")
    for tid_other in state.ticket_ids(target):
        if state.load_state(target, tid_other).get("pr") == pr["url"] and not state.load_state(target, tid_other).get("archived"):
            raise PullError(f"PR #{number} is the pipeline's own ticket {tid_other}; use that ticket")
    pull = {"number": number, "url": pr["url"], "head": pr["headRefName"], "base": pr["baseRefName"],
            "base_ref": f"{target.remote}/{pr['baseRefName']}", "author": _author(pr), "fork": bool(pr.get("isCrossRepository"))}
    body = (pr.get("body") or "").strip()
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n[… cut by Ziggy]"
    kind, title = _typed(pr["title"])
    t = tickets.create(target, title, kind, body, False, "low", pull=pull)
    wt = state.worktree_dir(target, tid)
    try:
        head = worktree.create_from_pull(target, t, wt)
    except (GitError, OSError):
        shutil.rmtree(state.run_dir(target, tid), ignore_errors=True)  # no half-adopted ticket
        raise
    st = state.load_state(target, tid)
    st.update(status="done", worktree=str(wt), prepared=False, pr=pr["url"], published_head=head, adopted_head=head,
              pr_opened_at=state.now())
    state.save_state(target, tid, st)
    _cache.pop(target.name, None)
    return t


def sync(target: Target, ticket: dict, st: dict) -> str:
    """Bring an adopted ticket's worktree up to its PR's head; updates `st` (the caller saves).
    Returns same | ahead (fix commits not pushed yet) | updated (fast-forwarded)."""
    pull = ticket["pull"]
    wt = Path(st["worktree"])
    remote = worktree.fetch_pull(target, pull["number"])
    local = worktree.head(wt)
    if local == remote:
        return "same"
    if worktree.is_ancestor(wt, remote, local):
        return "ahead"
    if not worktree.is_ancestor(wt, local, remote):
        raise PullError(f"{ticket['id']}: the local branch and PR #{pull['number']} diverged "
                        f"({local[:7]} vs {remote[:7]}); push or drop the local commits by hand")
    dirty = worktree.dirty_paths(wt)
    if dirty:
        raise PullError(f"{ticket['id']}: PR #{pull['number']} moved on, but the worktree is dirty: {', '.join(dirty[:5])}")
    git("reset", "--keep", "--quiet", remote, cwd=wt)  # a fast-forward: local is an ancestor of remote, the tree is clean
    st["published_head"] = remote
    return "updated"


def sync_ticket(target: Target, ticket_id: str) -> str:
    """`sync` for one ticket outside a run, under its lock."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id}"):
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        result = sync(target, t, st)
        state.save_state(target, ticket_id, st)
        return result
