"""Ticket dependencies (`after`) and stacked slices (`split`).

Dependencies: ticket.json `after` names tickets of the same target that must be done before this one runs a stage
(ticket.check_after refuses another target's tickets, unknown ones and cycles). The engine checks them before every
stage it would start (pipeline._drive): with one not done, the ticket waits with status `waiting`, `waiting_for` and
the step it would have taken in `next`. `wake` starts every waiting ticket whose dependencies are all done as a
detached `ziggy run` (the panel's job, so it shows like one started from the page); the engine calls it when a
ticket finishes done, and the panel on every inbox tick, so a restart cannot lose a start. In a controlled target
(mode = "controlled") nothing starts on its own: the inbox asks instead (attention kind `start`) and `ziggy run`
starts it. A dependency that failed, was archived without being done, or whose PR was closed leaves the dependant
waiting; the inbox says so (attention).

Stacked slices: at the approval gate, a plan with two or more `### S<n>: <title>` slices that is large or high risk
may be split instead of approved as one PR. Each slice becomes a ticket (`parent`, `slice`, `after` the slice below,
`stack_base`: its branch starts from the local branch of the slice below, the first from the parent's base) that
runs the `slice` pipeline (implement → review) on a plan seeded from the parent's. The parent becomes a container
(status `split`, `children`). Publishing a slice publishes the unpublished slices below it first, each PR's base being
the branch below, and every body lists the stack. When a lower PR is merged, the PR sync moves the next one to the
merged PR's base (`on_merged`); a squash or rebase merge leaves the next branch holding copies of the merged commits,
so it is marked for a restack: `restack` rebases it onto the base (`git rebase --onto`), and only a confirmed
`restack_push` pushes it (force-with-lease, the ticket's own branch).
"""
import getpass
import re
import sys
from pathlib import Path

from . import pipelines, plan, state, worktree
from . import ticket as tickets
from .config import Target
from .worktree import GitError, git

WAITING = "waiting"
SPLIT = "split"
# The gate offers a split for a plan with at least this many slices that is rated at least this large or risky.
SPLIT_MIN_SLICES = 2
SPLIT_SIZE = "large"
SPLIT_RISK = "high"


class StackError(Exception):
    pass


# --- dependencies ---------------------------------------------------------------------------------

def dependency(target: Target, dep_id: str) -> dict:
    """{id, title, state: done | pending | blocked, status, why}: done when the ticket is done (its PR, if it was
    closed unmerged, undoes that) or is a split container whose slices are all done; blocked when it failed, is
    missing, was archived without being done, or one of its slices is blocked; pending otherwise."""
    try:
        t = state.load_ticket(target, dep_id)
        st = state.load_state(target, dep_id)
    except (OSError, ValueError, LookupError):
        return {"id": dep_id, "title": "", "state": "blocked", "status": None, "why": "missing"}
    out = {"id": dep_id, "title": t.get("title") or "", "status": st.get("status"), "why": None}
    status, archived = st.get("status"), st.get("archived") or {}
    if status == "done":
        closed = (archived.get("pr_state") or st.get("pr_state")) == "CLOSED"
        return {**out, "state": "blocked" if closed else "done", "why": "closed" if closed else None}
    if status == SPLIT:
        slices = [dependency(target, c) for c in st.get("children") or []]
        if slices and all(s["state"] == "done" for s in slices):
            return {**out, "state": "done"}
        stuck = next((s for s in slices if s["state"] == "blocked"), None)
        return {**out, "state": "blocked", "why": f"slice {stuck['id']}"} if stuck else {**out, "state": "pending"}
    if status == "failed":
        return {**out, "state": "blocked", "why": "failed"}
    if archived:
        return {**out, "state": "blocked", "why": "archived"}
    return {**out, "state": "pending"}


def dependencies(target: Target, ticket: dict) -> list[dict]:
    return [dependency(target, d) for d in ticket.get("after") or []]


def unmet(target: Target, ticket: dict) -> list[dict]:
    """The dependencies that are not done yet (pending or blocked)."""
    return [d for d in dependencies(target, ticket) if d["state"] != "done"]


def set_after(target: Target, ticket_id: str, after: list[str]) -> list[str]:
    """Replace a ticket's dependencies before it runs (new or waiting); cycles and other targets are refused."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id}"):
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        if st.get("status") not in ("new", WAITING):
            raise StackError(f"{ticket_id} is {st.get('status')}; dependencies change only before it runs")
        if t.get("parent"):
            raise StackError(f"{ticket_id} is a slice of {t['parent']}; its order is the stack's")
        t["after"] = tickets.check_after(target, ticket_id, after)
        state.write_json(state.run_dir(target, ticket_id) / "ticket.json", t)
        _rewrite_pipeline_line(target, ticket_id, "- Starts after:",
                               f"- Starts after: {', '.join(t['after'])} (those tickets were done before this one ran)"
                               if t["after"] else None)
    wake(target)
    return t["after"]


def wake(target: Target) -> list[str]:
    """Start every waiting ticket of the target whose dependencies are all done, as a detached `ziggy run`
    job; returns the ids started. A ticket already running (or being started by another caller) is left alone.
    A controlled target starts none: each ready one gets its inbox item (attention kind `start`) instead."""
    started = []
    for ticket_id in state.ticket_ids(target):
        try:
            if state.load_state(target, ticket_id).get("status") != WAITING:
                continue
            if unmet(target, state.load_ticket(target, ticket_id)):
                continue
            if target.mode == "controlled":  # asks instead (attention kind `start`); `ziggy run` starts it
                from . import attention  # attention imports this module
                attention.raise_start(target, ticket_id)
                continue
            with state.lock(target, f"wake-{ticket_id}", f"start of waiting {ticket_id}"):
                from . import dashboard  # the panel's jobs: dashboard imports the engine
                if state.load_state(target, ticket_id).get("status") != WAITING or dashboard._busy(target, ticket_id):
                    continue
                dashboard.start_job(target, ticket_id, "run", ticket_id)
            started.append(ticket_id)
            print(f"{ticket_id}: its dependencies are done; started", file=sys.stderr, flush=True)
        except state.LockError:
            continue
        except (OSError, ValueError, LookupError) as exc:
            print(f"{ticket_id}: could not start after its dependencies: {exc}", file=sys.stderr)
    return started


# --- splitting a plan into stacked slices ------------------------------------------------------------

_SLICE = re.compile(r"^###[ \t]+(S\d+)[ \t]*[:.—-][ \t]*(.*)$", re.M)


def slices(plan_text: str) -> list[dict]:
    """The `### S<n>: <title>` slices of the plan's `## Plan` section: [{id, title, body}] in order."""
    m = re.search(r"^## Plan[ \t]*\n(.*?)(?=^## |\Z)", plan_text, re.M | re.S)
    if not m:
        return []
    found, seen = [], set()
    heads = list(_SLICE.finditer(m[1]))
    for i, head in enumerate(heads):
        if head[1] in seen:
            continue
        seen.add(head[1])
        end = heads[i + 1].start() if i + 1 < len(heads) else len(m[1])
        title = head[2].strip().strip("*").strip()
        bare = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()  # "(files · layer · risk)" stays in the body
        found.append({"id": head[1], "title": bare or title or head[1], "heading": head[0].strip(), "body": m[1][head.end():end].strip()})
    return found


def split_offer(target: Target, ticket_id: str, t: dict | None = None, st: dict | None = None) -> list[dict] | None:
    """The slices a ticket at the approval gate may be split into ([{id, title}]), or None: it must wait at the gate,
    be a code ticket of a standard pipeline (not a slice, an adopted PR or a project doc), be rated large or high
    risk, and its plan must have at least two slices."""
    t = t or state.load_ticket(target, ticket_id)
    st = st or state.load_state(target, ticket_id)
    if st.get("status") != "awaiting-approval" or t.get("parent") or t.get("pull") or t.get("type") == "project":
        return None
    if st.get("pipeline") not in ("feat", "fix", "chore"):
        return None
    if st.get("size") != SPLIT_SIZE and st.get("risk") != SPLIT_RISK:
        return None
    found = slices(plan.read(state.plan_path(target, ticket_id)))
    return [{"id": s["id"], "title": s["title"]} for s in found] if len(found) >= SPLIT_MIN_SLICES else None


def _ticket_body(target: Target, ticket_id: str) -> str:
    """The untrusted ticket text of ticket.md without its title line."""
    text = plan.read(state.run_dir(target, ticket_id) / "ticket.md")
    m = re.search(r"^## Ticket \(untrusted\)\n\n(.*?)\n## Decisions \(user\)", text, re.M | re.S)
    if not m:
        return ""
    return m[1].split("\n", 1)[1].strip() if "\n" in m[1] else ""


def _section(text: str, heading: str) -> str:
    found = re.findall(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return "\n\n".join(b.strip() for b in found if b.strip())


def seed_plan(parent: dict, parent_plan: str, slice_: dict, rows: list[tuple[str, dict]]) -> str:
    """A slice's PLAN: the parent's goal and acceptance criteria, this slice's tasks, where the other slices are, and
    the parent's decisions (last, so the decisions the wrapper appends later land under them)."""
    mine = slice_["id"]
    order = [s["id"] for _, s in rows]
    others = []
    for child_id, s in rows:
        if s["id"] == mine:
            continue
        where = "below this one, already in this branch's base" if order.index(s["id"]) < order.index(mine) else \
            "above this one, stacked on this branch later"
        others.append(f"- {s['id']}: {s['title']} ({child_id}, {where})")
    parts = [f"# {mine} of {parent['id']} — {parent['title']}",
             f"This plan is slice {mine} of {parent['id']}'s approved plan, split into stacked tickets. Implement only "
             f"the tasks under `## Plan`; the other slices are their own tickets.",
             "## Goal & acceptance\n\n" + (_section(parent_plan, "Goal & acceptance") or "(the parent plan has none)"),
             f"## Plan\n\n{slice_['heading']}\n{slice_['body']}".rstrip()]
    if others:
        parts.append("## Other slices\n\nNot this ticket's work:\n\n" + "\n".join(others))
    decisions = _section(parent_plan, "Decisions (user)")
    if decisions:
        parts.append(f"{plan.DECISIONS}\n\n{decisions}")
    return "\n\n".join(parts) + "\n"


def split(target: Target, ticket_id: str) -> list[dict]:
    """Split a ticket waiting at the approval gate into one stacked ticket per slice of its plan; returns the slice
    tickets, lowest first. The parent becomes a container: status `split`, `children`, no run of its own; its
    worktree is removed (planning left nothing in it). The caller starts the first slice."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} split"):
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        if st.get("status") != "awaiting-approval":
            raise StackError(f"{ticket_id} is {st.get('status')}; a plan is split at the approval gate")
        offer = split_offer(target, ticket_id, t, st)
        if not offer:
            raise StackError(f"{ticket_id}: a split needs a plan with at least {SPLIT_MIN_SLICES} `### S<n>:` slices "
                             f"rated size {SPLIT_SIZE} or risk {SPLIT_RISK}")
        parent_plan = plan.read(state.plan_path(target, ticket_id))
        found = {s["id"]: s for s in slices(parent_plan)}
        body = _ticket_body(target, ticket_id)
        made, below = [], None
        for s in offer:
            child = tickets.create(
                target, f"{t['title']} — {s['id']}: {s['title']}", t["type"], body, False, t.get("risk") or "low",
                language=t.get("language"), requester=t.get("requester"), pipeline=tickets.SLICE,
                after=[below["id"]] if below else [],
                stack={"parent": ticket_id, "slice": s["id"], "branch": f"{t['branch']}-{s['id'].lower()}",
                       "base_branch": below["branch"] if below else t["base_branch"],
                       "stack_base": below["id"] if below else None})
            made.append(child)
            below = child
        rows = [(c["id"], found[c["slice"]]) for c in made]
        first = pipelines.load(tickets.SLICE).stages[0].id
        for c in made:
            inbox = state.inbox_dir(target, c["id"])
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "plan.md").write_text(seed_plan(t, parent_plan, found[c["slice"]], rows), encoding="utf-8")
            cst = state.load_state(target, c["id"])
            cst.update(risk=st.get("risk"), size=st.get("size"), tasks=plan.tasks(plan.read(inbox / "plan.md")))
            if c["after"]:  # already asked to run: it starts when the slice below is done (controlled: it asks then)
                cst.update(status=WAITING, waiting_for=c["after"], next={"stage": first, "via": None, "resume": None})
            state.save_state(target, c["id"], cst)
        ids = [c["id"] for c in made]
        st.setdefault("approvals", []).append({**(st.get("gate") or {}), "approved_at": state.now(), "by": getpass.getuser(),
                                               "split": ids})
        st.update(status=SPLIT, gate=None, next=None, children=ids, split_at=state.now())
        if st.get("run"):
            st["run"].update(finished_at=state.now(), outcome=SPLIT)
        state.save_state(target, ticket_id, st)
        if st.get("worktree"):
            from . import cleanup  # cleanup imports this module
            try:
                cleanup._remove(target, ticket_id, force=False)
            except (cleanup.CleanupError, GitError) as exc:
                st = state.load_state(target, ticket_id)
                st["split_cleanup"] = str(exc)
                state.save_state(target, ticket_id, st)
    _attention(target, ticket_id)
    return made


def _attention(target: Target, ticket_id: str) -> None:
    try:
        from . import attention
        attention.raise_for(target, ticket_id)
    except Exception as exc:  # noqa: BLE001 - the inbox must never break a split or a sync
        print(f"{ticket_id}: inbox update failed: {exc}", file=sys.stderr)


def children(target: Target, parent_id: str) -> list[str]:
    try:
        return list(state.load_state(target, parent_id).get("children") or [])
    except (OSError, ValueError):
        return []


def stacked_on(target: Target, ticket_id: str) -> list[str]:
    """The tickets whose branch starts from this ticket's branch (the slice above it)."""
    out = []
    for other in state.ticket_ids(target):
        try:
            if state.load_ticket(target, other).get("stack_base") == ticket_id:
                out.append(other)
        except (OSError, ValueError, LookupError):
            continue
    return out


def stacked_branches(target: Target, ticket_id: str) -> list[str]:
    """The slices stacked on this ticket whose branch exists already (built on this branch's commits)."""
    out = []
    for child_id in stacked_on(target, ticket_id):
        branch = state.load_ticket(target, child_id)["branch"]
        if _exists(target.path, f"refs/heads/{branch}"):
            out.append(child_id)
    return out


def _slice_row(target: Target, child_id: str) -> dict:
    try:
        t = state.load_ticket(target, child_id)
        st = state.load_state(target, child_id)
    except (OSError, ValueError, LookupError):
        return {"id": child_id, "title": "", "status": "error"}
    return {"id": child_id, "slice": t.get("slice"), "title": t.get("title"), "branch": t.get("branch"),
            "base_branch": t.get("base_branch"), "status": st.get("status"), "running": state.running(target, child_id),
            "pr": st.get("pr"), "pr_state": st.get("pr_state"), "archived": bool(st.get("archived")),
            "waiting_for": st.get("waiting_for") or [], "restack": bool(st.get("restack"))}


def view(target: Target, t: dict, st: dict) -> dict:
    """What the panel shows about a ticket's place in dependencies and stacks."""
    deps = dependencies(target, t) if t.get("after") else []
    out = {"after": deps, "waiting_for": [d for d in deps if d["state"] != "done"] if st.get("status") == WAITING else [],
           "parent": None, "slice": t.get("slice"), "stack_base": t.get("stack_base"), "children": [], "split_open": False, "split_landing": False,
           "split_offer": split_offer(target, t["id"], t, st) if st.get("status") == "awaiting-approval" else None,
           "restack": st.get("restack"), "restacked": st.get("restacked"), "retargeted": st.get("retargeted")}
    if t.get("parent"):
        try:
            parent = state.load_ticket(target, t["parent"])
            out["parent"] = {"id": parent["id"], "title": parent["title"]}
        except LookupError:
            out["parent"] = {"id": t["parent"], "title": ""}
        out["children"] = [_slice_row(target, c) for c in children(target, t["parent"])]
    elif st.get("status") == SPLIT:
        out["children"] = [_slice_row(target, c) for c in st.get("children") or []]
        live = [c for c in out["children"] if not c.get("archived")]
        # Open: a slice still has agent work left. Landing: every slice is done and some PR is not merged yet, so
        # what is left is the owner's review; the board shows the container with the open PRs, not as waiting.
        out["split_open"] = any(c.get("status") != "done" for c in live)
        out["split_landing"] = bool(live) and not out["split_open"]
    return out


# --- the stack on GitHub ------------------------------------------------------------------------------

def publish_below(target: Target, ticket: dict) -> list[str]:
    """Before a slice is published: the slice below it (and so on down) gets its PR first, or its new commits
    pushed, so this PR's base branch is on GitHub with everything the slice builds on. Returns the PR URLs pushed."""
    below = ticket.get("stack_base")
    if not below:
        return []
    from . import publish  # publish imports this module
    lst = state.load_state(target, below)
    lower = state.load_ticket(target, below)
    if lst.get("archived"):
        raise publish.PublishError(f"{below}, the slice below {ticket['id']}, is archived; the PR sync moves "
                                   f"{ticket['id']}'s base to {lower['base_branch']} first")
    if lst.get("status") != "done":
        raise publish.PublishError(f"{ticket['id']} is stacked on {below}, which is {lst.get('status')}; the slice below "
                                   "must be done before this one gets a PR")
    head = worktree.git("rev-parse", f"refs/heads/{lower['branch']}", cwd=target.path).stdout.strip()
    if lst.get("pr") and lst.get("published_head") == head:
        return publish_below(target, lower)
    return [publish.publish(target, below)]  # which publishes what is below it first


def pr_stack_section(target: Target, ticket: dict) -> str | None:
    """The `## Stack` part of a slice's PR body: every slice in order, ↓ below this one (merge first), ↑ above it,
    each with its PR or, before it has one, its branch."""
    parent_id = ticket.get("parent")
    if not parent_id:
        return None
    ids = children(target, parent_id)
    if ticket["id"] not in ids:
        return None
    mine = ids.index(ticket["id"])
    try:
        parent_title = state.load_ticket(target, parent_id)["title"]
    except LookupError:
        parent_title = parent_id
    lines = [f"Part {mine + 1} of {len(ids)} of a stacked change ({parent_title}); merge from the bottom up."]
    for i, child_id in enumerate(ids):
        try:
            ct = state.load_ticket(target, child_id)
            cst = state.load_state(target, child_id)
        except (OSError, ValueError, LookupError):
            continue
        mark = "→" if i == mine else "↓" if i < mine else "↑"
        where = "this PR" if i == mine else cst.get("pr") or f"`{ct['branch']}` (no PR yet)"
        lines.append(f"- {mark} {ct.get('slice')}: {ct['title']} — {where}")
    return "## Stack\n\n" + "\n".join(lines)


def on_merged(target: Target, ticket_id: str, pr: dict) -> list[dict]:
    """The PR of `ticket_id` was merged: every slice stacked on it moves to the base the merged PR went into. Its PR's
    base is changed on GitHub (REST), its ticket's base follows, and when the merge did not keep the merged commits
    (a squash or rebase merge: the PR's head is not in <remote>/<base>) a slice whose branch holds them is marked for
    a restack. A failed GitHub update leaves the slice as it was for the next sync. Returns what changed per slice."""
    above = stacked_on(target, ticket_id)
    if not above:
        return []
    from . import publish
    t = state.load_ticket(target, ticket_id)
    base, base_stack = t["base_branch"], t.get("stack_base")
    head = pr.get("headRefOid")
    with worktree.shared_git(target, f"fetch of {base} after {ticket_id} was merged"):
        git("fetch", "--quiet", target.remote, f"refs/heads/{base}:refs/remotes/{target.remote}/{base}",
            cwd=target.path, check=False, timeout=120)
    onto = f"{target.remote}/{base}"
    kept = bool(head) and _ancestor(target.path, head, onto)
    out = []
    for child_id in above:
        with state.lock(target, f"ticket-{child_id}", f"ticket {child_id} retarget"):
            ct = state.load_ticket(target, child_id)
            cst = state.load_state(target, child_id)
            # A slice above that is merged or closed too has nowhere to move (GitHub refuses a closed PR's base:
            # 422), so it is left for its own turn in the sync instead of holding this one up forever.
            if cst.get("pr") and cst.get("pr_state") not in ("MERGED", "CLOSED"):
                try:
                    publish.retarget(target, cst["pr"], base)
                except publish.PublishError as exc:
                    if publish.pr_closed(target, cst["pr"]):
                        out.append({"id": child_id, "skipped": "its PR is merged or closed"})
                        continue
                    cst["retarget_error"] = str(exc)
                    state.save_state(target, child_id, cst)
                    out.append({"id": child_id, "kept": [f"PR base not changed: {exc}"]})
                    continue
            elif cst.get("pr"):
                out.append({"id": child_id, "skipped": "its PR is merged or closed"})
                continue
            old = ct["base_branch"]
            ct.update(base_branch=base, stack_base=base_stack)
            state.write_json(state.run_dir(target, child_id) / "ticket.json", ct)
            _rewrite_pipeline_line(target, child_id, "- Base:", tickets.base_line(ct))
            cst.pop("retarget_error", None)
            cst["retargeted"] = {"from": old, "to": base, "below": ticket_id, "pr": bool(cst.get("pr")), "at": state.now()}
            restack = None
            branch = f"refs/heads/{ct['branch']}"
            if head and not kept and _exists(target.path, branch):
                fork = git("merge-base", branch, head, cwd=target.path, check=False).stdout.strip()
                if fork and not _ancestor(target.path, fork, onto):
                    restack = {"onto": onto, "upstream": fork, "reason": "squash", "below": ticket_id, "at": state.now()}
                    cst["restack"] = restack
            state.save_state(target, child_id, cst)
        _attention(target, child_id)
        out.append({"id": child_id, "retargeted": base, "restack": bool(restack)})
    return out


def _ancestor(cwd: Path, older: str, newer: str) -> bool:
    return git("merge-base", "--is-ancestor", older, newer, cwd=cwd, check=False).returncode == 0


def _exists(cwd: Path, ref: str) -> bool:
    return git("show-ref", "--verify", "--quiet", ref, cwd=cwd, check=False).returncode == 0


def _rewrite_pipeline_line(target: Target, ticket_id: str, prefix: str, line: str | None) -> None:
    """Replace (or add, or with None drop) one `- …` line of ticket.md's trusted Pipeline section."""
    path = state.run_dir(target, ticket_id) / "ticket.md"
    text = plan.read(path)
    head, sep, rest = text.partition("\n## Ticket (untrusted)")
    lines = [x for x in head.split("\n") if not x.startswith(prefix)]
    if line:
        at = max((i for i, x in enumerate(lines) if x.startswith("- ")), default=len(lines) - 1) + 1
        lines.insert(at, line)
    path.write_text("\n".join(lines) + sep + rest, encoding="utf-8")


# --- restacking a slice after a squash merge below it -------------------------------------------------

def restack(target: Target, ticket_id: str) -> dict:
    """Rebase a slice marked for a restack onto its new base (`git rebase --onto <onto> <upstream>` in its
    worktree). Conflicts abort the rebase and leave the branch as it was. A slice without a PR is done; one with a
    PR waits for `restack_push`. Slices stacked on this one are marked for their own restack."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} restack"):
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        info = st.get("restack")
        if not info:
            raise StackError(f"{ticket_id} needs no restack")
        if info.get("rebased"):
            raise StackError(f"{ticket_id} is rebased already; push it (restack --push)")
        wt = Path(st.get("worktree") or "")
        if not st.get("worktree") or not wt.is_dir():
            raise StackError(f"{ticket_id} has no worktree; run it once to recreate the worktree, then restack")
        dirty = worktree.dirty_paths(wt)
        if dirty:
            raise StackError(f"{ticket_id}'s worktree has uncommitted changes: {', '.join(dirty[:5])}")
        if info["onto"].startswith(f"{target.remote}/"):
            branch = info["onto"].split("/", 1)[1]
            with worktree.shared_git(target, f"fetch of {branch} for a restack"):
                git("fetch", "--quiet", target.remote, f"refs/heads/{branch}:refs/remotes/{info['onto']}",
                    cwd=target.path, check=False, timeout=120)
        old = worktree.head(wt)
        proc = git("rebase", "--onto", info["onto"], info["upstream"], cwd=wt, check=False, timeout=600)
        if proc.returncode != 0:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).stdout.split()
            git("rebase", "--abort", cwd=wt, check=False)
            output = (proc.stdout + proc.stderr).strip()[-2000:]
            info["result"] = {"ok": False, "at": state.now(), "conflicts": conflicts, "output": output}
            state.save_state(target, ticket_id, st)
            raise StackError(f"rebase of {t['branch']} onto {info['onto']} stopped on conflicts"
                             + (f" in {', '.join(conflicts[:10])}" if conflicts else "") + "; the branch is as it was")
        new = worktree.head(wt)
        commits = git("log", "--format=%h %s", f"{info['onto']}..HEAD", cwd=wt, check=False).stdout.splitlines()
        info["result"] = {"ok": True, "at": state.now(), "from": old, "to": new, "commits": commits[:50]}
        info["rebased"] = True
        if st.get("pr") and st.get("published_head") != new:
            st["restack"] = info  # the PR still shows the old commits until restack_push
        else:
            st.pop("restack", None)
            st["restacked"] = info
        state.save_state(target, ticket_id, st)
        marked = _mark_above(target, ticket_id, t["branch"], old)
    _attention(target, ticket_id)
    return {**info, "marked": marked, "needs_push": bool(st.get("restack"))}


def _mark_above(target: Target, ticket_id: str, branch: str, old_head: str) -> list[str]:
    """Slices stacked on a branch that was just rebased still hold its old commits: mark each for a restack onto
    the rebased branch."""
    marked = []
    for child_id in stacked_on(target, ticket_id):
        ct = state.load_ticket(target, child_id)
        ref = f"refs/heads/{ct['branch']}"
        if not _exists(target.path, ref):
            continue
        fork = git("merge-base", ref, old_head, cwd=target.path, check=False).stdout.strip()
        if not fork or _ancestor(target.path, fork, f"refs/heads/{branch}"):
            continue
        cst = state.load_state(target, child_id)
        cst["restack"] = {"onto": f"refs/heads/{branch}", "upstream": fork, "reason": "rebased below",
                          "below": ticket_id, "at": state.now()}
        state.save_state(target, child_id, cst)
        _attention(target, child_id)
        marked.append(child_id)
    return marked


def restack_push(target: Target, ticket_id: str) -> str:
    """After a confirmed restack: push the rebased branch to its PR (force-with-lease on what was last pushed, the
    ticket's own branch only). Returns the PR URL."""
    from . import publish
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} restack push"):
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        info = st.get("restack") or {}
        if not info.get("rebased"):
            raise StackError(f"{ticket_id} has no rebased branch to push; restack it first")
        head = publish.push_rebased(target, t, st)
        info["pushed_at"] = state.now()
        st.pop("restack", None)
        st["restacked"] = info
        st["published_head"] = head
        state.save_state(target, ticket_id, st)
    _attention(target, ticket_id)
    return st["pr"]
