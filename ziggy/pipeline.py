"""The engine: runs a ticket through its pipeline (pipelines/<name>.toml), one sandboxed headless session
per stage, and routes on each stage's result.

A *run* is one journey through the pipeline, started by `run` (or `request_changes`); it ends done, failed,
or waiting for a person (a question round, or the approval gate). `answer` and `approve` continue the same
run, so its loop caps (`max_visits`) keep counting; a retry after a failure is a new run and starts the caps
over. The ticket's state is `state.json`; every stage session leaves a `NN-<stage>/` directory.

The process that calls these functions holds the ticket's lock and one of the target's run slots for as
long as stages run (an automatic retry's backoff included); waiting for a person holds neither. A nudge
(`nudge`) needs no lock: it leaves `runs/<id>/nudge.json` for the running session's watchdog
(dispatch.run), or for the next stage that starts."""
import getpass
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import checks as engine_checks
from . import bmad, dispatch, feedback, memory, pipelines, plan, protected, pulls, quota, render, results, roadmap, stack, state, worktree
from . import config, host, stalls
from .config import Target
from .ticket import LANGUAGES

WAIT_FOR_SLOT = True  # the CLI's --no-wait turns it off: refuse instead of waiting in line
IGNORE_QUOTA = False  # the CLI's run --now: start the first stage even while the account's limit is reached
IGNORE_HOST = False  # the CLI's run --now too: start (and go on) even while the machine is short of memory
SLOT_POLL = 3  # seconds between looks at the slots while waiting
WAITING = ("needs-user", "awaiting-approval")
RETRY_WHAT = {"lost": "lost its process", "timeout": "ran past its timeout", "quiet": "went quiet"}
EXIT = {"done": 0, "needs-user": 3, "awaiting-approval": 3, "failed": 1, stack.WAITING: 0, stack.SPLIT: 0, quota.STATUS: 0,
        host.STATUS: 0}
ATTEMPTS = "attempts"  # <runs>/<ticket>/attempts: the code an intent gap set aside (attempt-<n>.patch + .json)

plan_path = state.plan_path
next_task_number = plan.next_task_number


class PipelineError(Exception):
    pass


class LeftLine(PipelineError):
    pass


class MissingBmadFiles(PipelineError):
    pass


class _RetryWaitStopped(Exception):
    pass


# --- locks and run slots ------------------------------------------------------------------------

@contextmanager
def _locked(target: Target, ticket_id: str):
    """Yields the ticket's state under its lock and a run slot. A `running` status found under the lock
    was left by a process that died mid-stage: the run is failed there (resume continues the session)."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id}"), ExitStack() as slot:
        _take_slot(target, ticket_id, slot)
        st = state.load_state(target, ticket_id)
        if st.get("status") == "running":
            stage = st.get("stage")
            st.pop("retry_at", None)
            _fail(st, f"the process running {stage} ended without recording it", stage)
            _settle(target, ticket_id, st)
        yield st


def _earlier_waiter(target: Target, ticket_id: str, since: float | None) -> str | None:
    """Another ticket of the target that has waited for a slot longer (first come, first served)."""
    for other in state.ticket_ids(target):
        if other == ticket_id:
            continue
        waited = state.load_state(target, other).get("waiting_since")
        if waited and state.running(target, other) and (since is None or (waited, other) < (since, ticket_id)):
            return other
    return None


def _take_slot(target: Target, ticket_id: str, stack: ExitStack) -> None:
    """One of the target's max_active run slots, under the ticket's lock. When all are taken, wait
    in line (st["waiting_since"] marks it for the panel, and orders the line); SIGTERM while
    waiting (stop) leaves the line cleanly."""
    since = None
    previous = signal.getsignal(signal.SIGTERM)
    try:
        while True:
            if not _earlier_waiter(target, ticket_id, since):
                for name in state.slot_names(target):
                    try:
                        stack.enter_context(state.lock(target, name, f"active run ({ticket_id})"))
                        return
                    except state.LockError:
                        continue
            busy = state.slot_holders(target)
            if not WAIT_FOR_SLOT:
                raise state.LockError(f"all {target.max_active} run slots of {target.name} are busy: {'; '.join(busy) or 'a run ahead in line'}")
            if since is None:
                since = time.time()
                st = state.load_state(target, ticket_id)
                st.update(waiting_since=since, waiting_at=state.now())
                state.save_state(target, ticket_id, st)
                signal.signal(signal.SIGTERM, _leave_line)
                print(f"{ticket_id}: waiting for a run slot ({len(busy)}/{target.max_active} busy)", file=sys.stderr, flush=True)
            time.sleep(SLOT_POLL)
    finally:
        if since is not None:
            signal.signal(signal.SIGTERM, previous)
            st = state.load_state(target, ticket_id)
            st.pop("waiting_since", None)
            st.pop("waiting_at", None)
            state.save_state(target, ticket_id, st)


def _leave_line(signum, frame):
    raise LeftLine("stopped while waiting for a run slot")


# --- state helpers ------------------------------------------------------------------------------

def _pipeline(st: dict, ticket_id: str) -> pipelines.Pipeline:
    if not st.get("pipeline"):
        raise PipelineError(f"{ticket_id} was made by the BMad version of the pipeline and cannot run on this one; "
                            "open a new ticket (its branch and PR still publish, review and clean up as before)")
    try:
        return pipelines.load(st["pipeline"])
    except pipelines.PipelineConfigError as exc:
        raise PipelineError(str(exc)) from None


def _attention(target: Target, ticket_id: str) -> None:
    """Tell the inbox a ticket's state changed (best effort: it never stops a run)."""
    try:
        from . import attention
    except ImportError:
        return
    try:
        attention.raise_for(target, ticket_id)
    except Exception as exc:  # noqa: BLE001 - the inbox must never break a run
        print(f"{ticket_id}: inbox update failed: {exc}", file=sys.stderr)


def _retro(target: Target, ticket_id: str) -> None:
    """A run that ended done may have something to learn from: retro.py starts its session in the background (best
    effort: it never stops or holds up the run)."""
    try:
        from . import retro  # retro reads review state through changes, which imports this module
        job = retro.schedule(target, ticket_id)
    except Exception as exc:  # noqa: BLE001 - a lesson missed is no reason to fail a finished run
        print(f"{ticket_id}: retro not started: {exc}", file=sys.stderr)
        return
    if job:
        print(f"{ticket_id}: retro started in the background (job {job['id']})", file=sys.stderr)


def _memory_used(target: Target, ticket_id: str, result: dict) -> list[str]:
    """The memory ids the stage relied on (`memoryUsed`, review findings' `memory`), counted in usage.json (best
    effort: what was counted is also in run.json)."""
    ids = memory.used_ids(result)
    try:
        return memory.record_usage(target, ticket_id, ids)
    except Exception as exc:  # noqa: BLE001 - a missed count never fails a stage
        print(f"{ticket_id}: memory use not counted: {exc}", file=sys.stderr)
        return ids


def _episode(target: Target, ticket: dict, st: dict, wt: Path) -> None:
    """A run that ended done: its episode in the project memory (no approval; best effort)."""
    try:
        records = _records(target, ticket["id"])
        base = worktree.merge_base(wt, worktree.base_ref(target, ticket))
        files = [f for f in worktree.git("diff", "--name-only", base, "HEAD", cwd=wt).stdout.splitlines() if f] if base else []
        cost = sum(r.get("total_cost_usd") or 0 for _, r in records if isinstance(r.get("total_cost_usd"), (int, float)))
        rounds = sum(1 for _, r in records if r.get("review") and counts_as_visit(r))
        memory.episode_done(target, ticket, st, files, cost, rounds)
    except Exception as exc:  # noqa: BLE001 - a missed episode is no reason to fail a finished run
        print(f"{ticket['id']}: episode not written: {exc}", file=sys.stderr)


def _decision_candidates(target: Target, ticket: dict, st: dict, round_: dict) -> None:
    """The answers of a closed question round as decision candidates (best effort)."""
    try:
        wt = Path(st["worktree"]) if st.get("worktree") and Path(st["worktree"]).is_dir() else None
        memory.from_round(target, ticket, round_, _files_in_play(st, _records(target, ticket["id"]), wt))
    except Exception as exc:  # noqa: BLE001 - a lost proposal never blocks the answer
        print(f"{ticket['id']}: answers not proposed as decisions: {exc}", file=sys.stderr)


def _settle(target: Target, ticket_id: str, st: dict) -> None:
    """Save a status change a person may need to see. A run that ended drops a nudge no session read."""
    if st.get("status") in ("done", "failed"):
        _drop_pending_nudge(target, ticket_id)
    state.save_state(target, ticket_id, st)
    _attention(target, ticket_id)


def _wake(target: Target, ticket_id: str) -> None:
    """A run that ended done may be the last dependency a waiting ticket had: start those (best effort; the panel's
    tick tries again)."""
    try:
        stack.wake(target)
    except Exception as exc:  # noqa: BLE001 - a waiting ticket is started by the panel's next tick instead
        print(f"{ticket_id}: tickets waiting for it not started: {exc}", file=sys.stderr)


def _fail(st: dict, error: str, stage: str | None) -> None:
    st["status"] = "failed"
    st["error"] = error
    st["next"] = {"stage": stage, "via": None, "resume": None} if stage else None
    if st.get("run"):
        st["run"].update(finished_at=state.now(), outcome="failed", error=error)


def _new_run(st: dict, start: str, via: dict | None = None, user_review: dict | None = None) -> dict:
    previous = (st.get("run") or {}).get("n", 0)
    st["run"] = {"n": previous + 1, "started_at": state.now(), "finished_at": None, "start": start, "via": via,
                 "base_commit": None, "visits": {}, "round": 0, "user_review": user_review, "outcome": None, "error": None,
                 "extra_visits": {}, "adjudicated": 0, "retries": {}, "full_review": False, "pre_implement": None}
    st.update(error=None, gate=None)
    return {"stage": start, "via": via, "resume": None}


def _drop_pending_nudge(target: Target, ticket_id: str) -> None:
    """A nudge whose run ended before a session read it is not for a later run."""
    (state.run_dir(target, ticket_id) / dispatch.NUDGE_FILE).unlink(missing_ok=True)


def _max_visits(run: dict, stage: pipelines.Stage) -> int:
    """The stage's cap in this run: max_visits plus the visits a user adjudication granted."""
    return stage.max_visits + (run.get("extra_visits") or {}).get(stage.id, 0)


def _review_routes(stage: pipelines.Stage) -> bool:
    """A code review stage: it reports findings and routes `fix` (back to the implementer) or `converged`."""
    return stage.result == "review" and "fix" in stage.routes and "converged" in stage.routes


def _last_review_round(run: dict, pl: pipelines.Pipeline, stage: pipelines.Stage) -> bool:
    """No fix round is left in this run after this review visit: the review's own cap is reached, or the stage
    its `fix` route goes to has used all its visits."""
    cap = _max_visits(run, stage)
    if cap and run["visits"].get(stage.id, 0) >= cap:
        return True
    fix = next((s for s in pl.stages if s.id == stage.routes.get("fix")), None)
    return fix is not None and run["visits"].get(fix.id, 0) >= _max_visits(run, fix)


def _engine_checks(target: Target, pl: pipelines.Pipeline, records: list[tuple[Path, dict]], wt: Path) -> dict | None:
    """The target's own checks when a stage of this pipeline runs them: `commands` [{name, run}] and `results`,
    the newest engine outcomes when they were taken on the current HEAD (else none). None without checks."""
    if not target.checks or not any(s.checks for s in pl.stages):
        return None
    try:
        head = worktree.head(wt)
    except worktree.GitError:
        head = None
    latest = next((r for _, r in reversed(records) if r.get("checks")), None)
    found = latest["checks"] if latest and head and latest.get("head_after") == head else []
    outcomes = [{"name": c.get("name"), "status": c.get("status"),
                 "detail": (" ".join(str(c.get("detail") or "").split())[-300:] or None) if c.get("status") != "pass" else None}
                for c in found]
    return {"commands": [{"name": c["name"], "run": c["run"]} for c in target.checks],
            "results": outcomes, "has_results": bool(outcomes)}


def counts_as_visit(record: dict) -> bool:
    """A session that was lost, timed out, interrupted by a nudge, or died unrecorded is continued in its own
    session, not counted as another visit (the loop caps and the ticket's review rounds)."""
    state_ = record.get("state")
    if state_ in (None, "lost", "timeout", "quota"):
        return False
    return not (state_ == "killed" and str(record.get("kill_reason") or "").startswith("nudge:"))


def open_round(st: dict) -> dict | None:
    for r in reversed(st.get("rounds") or []):
        if r.get("answer") is None:
            return r
    return None


def _records(target: Target, ticket_id: str) -> list[tuple[Path, dict]]:
    out = []
    for d in state.phase_dirs(target, ticket_id):
        try:
            out.append((d, state.read_json(d / "run.json")))
        except (OSError, ValueError):
            continue
    return out


def stages_view(target: Target, ticket_id: str) -> list[dict]:
    """The pipeline bar: every stage of the ticket's pipeline with its state in the current run."""
    st = state.load_state(target, ticket_id)
    if not st.get("pipeline"):
        return []
    try:
        pl = pipelines.load(st["pipeline"])
    except pipelines.PipelineConfigError:
        return []
    run = st.get("run") or {}
    run_n = run.get("n")
    records = [r for _, r in _records(target, ticket_id) if r.get("stage")]
    out = []
    for s in pl.stages:
        mine = [r for r in records if r["stage"] == s.id]
        in_run = [r for r in mine if r.get("run") == run_n]
        stopped_here = st.get("status") == "failed" and (st.get("next") or {}).get("stage") == s.id
        if (st.get("status") == "running" and st.get("stage") == s.id) or (
                st.get("status") in (quota.STATUS, host.STATUS) and (st.get("next") or {}).get("stage") == s.id):
            view = "current"
        elif in_run and in_run[-1].get("state") == "ok" and in_run[-1].get("verdict") != "FAILED" and not stopped_here:
            view = "done"
        elif in_run:
            view = "failed"
        else:
            view = "pending"
        out.append({"id": s.id, "title": s.title, "state": view, "visits": sum(map(counts_as_visit, in_run)),
                    "visit_max": _max_visits(run, s), "cost_usd": round(sum(r.get("total_cost_usd") or 0 for r in mine), 4)})
    return out


def quiet_after(target: Target, st: dict) -> int:
    """Seconds without agent events before a running stage counts as quiet: the stage's own value, else the target's."""
    try:
        stage = pipelines.load(st["pipeline"]).stage(st["stage"])
    except (KeyError, TypeError, pipelines.PipelineConfigError):
        return target.quiet_after
    stage = pipelines.effective(stage, ((st.get("run") or {}).get("visits") or {}).get(stage.id, 1))
    return stage.quiet_after or target.quiet_after


# --- the worktree and the inbox -----------------------------------------------------------------

def _ready_worktree(target: Target, ticket: dict, st: dict, allow_dirty: bool = False) -> tuple[Path, Path, dict]:
    """(worktree, inbox, port env): the worktree created if needed, the target's gitignored files it keeps
    (`preserve`) copied in, the ticket's port range taken, and the prepare commands run once."""
    ticket_id = ticket["id"]
    root = state.run_dir(target, ticket_id)
    wt = state.worktree_dir(target, ticket_id)
    if not (st.get("worktree") and worktree.is_worktree(target, wt)):
        if wt.exists():
            raise PipelineError(f"{wt} exists but is not a worktree of {target.path}; remove it")
        worktree.create(target, ticket, wt)
        st.update(worktree=str(wt), prepared=False)
        st.pop("worktree_removed", None)
        state.save_state(target, ticket_id, st)
    copied = worktree.preserve(target, wt)
    if st.get("pipeline") == bmad.PIPELINE:
        # BMad's per-developer files are gitignored: a worktree has them only as copies from the main checkout.
        copied += worktree.preserve(target, wt, bmad.LOCAL_FILES)
        missing = [rel for rel in bmad.LOCAL_FILES if not (wt / rel).is_file()]
        if missing:
            raise MissingBmadFiles(f"the worktree lacks BMad's per-developer files {', '.join(missing)}: the main checkout "
                                f"{target.path} has no gitignored copy of them (run BMad's setup there first)")
    if copied:
        print(f"{ticket_id}: copied {', '.join(copied)} into the worktree", file=sys.stderr)
    ports = worktree.ports(target, ticket_id, st)
    if not st.get("prepared"):
        worktree.run_prepare(target, ticket_id, wt, root / "prepare.log")
        st["prepared"] = True
        state.save_state(target, ticket_id, st)
    dirty = [] if allow_dirty else worktree.dirty_paths(wt)
    if dirty:
        raise PipelineError(f"worktree {wt} has uncommitted changes: {', '.join(dirty[:10])}")
    inbox = state.inbox_dir(target, ticket_id)
    (inbox / "artifacts").mkdir(parents=True, exist_ok=True)
    return wt, inbox, ports


def _refresh_inbox(target: Target, ticket_id: str, inbox: Path) -> None:
    """The wrapper's own files are rewritten before every stage: the agent can write the inbox. Saved attempts
    (intent gaps) are mirrored to `inbox/attempts` for the sessions to read."""
    root = state.run_dir(target, ticket_id)
    shutil.copy2(root / "ticket.md", inbox / "ticket.md")
    answers = root / "answers.md"
    (inbox / "answers.md").write_text(answers.read_text(encoding="utf-8") if answers.exists() else "", encoding="utf-8")
    (inbox / "result.json").unlink(missing_ok=True)
    shutil.rmtree(inbox / ATTEMPTS, ignore_errors=True)
    if (root / ATTEMPTS).is_dir():
        shutil.copytree(root / ATTEMPTS, inbox / ATTEMPTS, ignore=shutil.ignore_patterns("*.json"))


def _changed_refs(target: Target, ticket_id: str, before: str, after: str) -> list[str]:
    """Remote refs that moved during a session, leaving out what other tickets' publishes push
    (their own branches, an adopted PR's branch) and GitHub's refs/pull/*: with runs side by side,
    those move without this session having pushed anything."""
    def table(text):
        return dict(reversed(line.split("\t", 1)) for line in text.splitlines() if "\t" in line)
    old, new = table(before), table(after)
    others = set()
    for other in state.ticket_ids(target):
        if other == ticket_id:
            continue
        t = state.load_ticket(target, other)
        others.add(f"refs/heads/{t['branch']}")
        if t.get("pull"):
            others.add(f"refs/heads/{t['pull']['head']}")
    return sorted(ref for ref in set(old) | set(new)
                  if old.get(ref) != new.get(ref) and ref not in others and not ref.startswith("refs/pull/"))


# --- prompts ------------------------------------------------------------------------------------

def _partials(target: Target, stage: pipelines.Stage):
    def load(name: str) -> str:
        if name == "role":
            return pipelines.partial_text(f"roles/{stage.role}") if stage.role else ""
        return pipelines.partial_text(name)
    return load


def _files_in_play(st: dict, records: list[tuple[Path, dict]], wt: Path | None) -> list[str]:
    """The files a ticket's memory is matched against: the branch's changed files once something is built, else the
    latest plan's `filesExpected`."""
    base = (st.get("run") or {}).get("base_commit")
    if wt and base:
        try:
            changed = [f for f in worktree.git("diff", "--name-only", base, "HEAD", cwd=wt).stdout.splitlines() if f]
        except (worktree.GitError, OSError):
            changed = []
        if changed:
            return changed
    for _, r in reversed(records):
        files = (r.get("result") or {}).get("filesExpected")
        if r.get("state") == "ok" and isinstance(files, list) and files:
            return [f for f in files if isinstance(f, str)]
    return []


def _memory(target: Target, ticket: dict, stage: pipelines.Stage, files: list[str]) -> dict:
    """The stage's memory section (memory.recall); a memory folder that cannot be read leaves the prompt without it."""
    try:
        return memory.recall(target, ticket, stage, files)
    except Exception as exc:  # noqa: BLE001 - a broken memory must never stop a stage
        print(f"{ticket['id']}: project memory not recalled: {exc}", file=sys.stderr)
        return {"exists": False, "text": "", "ids": [], "left_out": 0, "warnings": [str(exc)]}


def _prev(records: list[tuple[Path, dict]], run_n: int) -> dict:
    """Latest ok result per stage id; the current run's override earlier runs'."""
    out: dict = {}
    for current in (False, True):
        for _, r in records:
            if r.get("stage") and r.get("state") == "ok" and r.get("result") and (r.get("run") == run_n) == current:
                out[r["stage"]] = r["result"]
    return out


def _user_dismissed(target: Target, ticket_id: str, review_dir: str) -> set[str]:
    """Finding ids of one review session that the user dismissed in a sent review-page comment."""
    from . import changes  # changes imports this module
    try:
        comments = changes.load_review(target, ticket_id).get("comments") or []
    except (OSError, ValueError):
        return set()
    return {c["finding_id"] for c in comments if c.get("round") is not None and c.get("finding_action") == "dismiss"
            and c.get("finding_stage") == review_dir and isinstance(c.get("finding_id"), str)}


def _review_context(target: Target, ticket_id: str, st: dict, stage: pipelines.Stage, records: list[tuple[Path, dict]],
                    wt: Path, round_: int) -> dict:
    """What a review session reads: the whole branch, or only what changed since the
    newest reviewed HEAD (the previous review of this stage, or the user's review) when that HEAD is still in
    the branch's history; plus the previous review's findings still open ("carried")."""
    reviews = [(d, r) for d, r in records if r.get("stage") == stage.id and r.get("state") == "ok"
               and isinstance((r.get("result") or {}).get("findings"), list)]
    last_dir, last = reviews[-1] if reviews else (None, None)
    user = (st.get("user_reviews") or [None])[-1]
    carried = []
    if last:
        result = last["result"]
        closed = {i for i in result.get("fixedIds") or [] if isinstance(i, str)} | _user_dismissed(target, ticket_id, last_dir.name)
        carried = [{"id": f.get("id"), "class": f.get("class") or "important", "severity": f.get("severity") or "medium",
                    "kind": f.get("kind") or "bug", "location": f.get("location") or "", "summary": f.get("summary") or "",
                    "disposition": f.get("disposition")}
                   for f in result["findings"] if isinstance(f, dict) and f.get("disposition") in ("fix", "decision")
                   and f.get("id") not in closed]
    out = {"round": round_, "prev_round": len(reviews) or None, "prev_head": (last or {}).get("head_before"),
           "user_head": (user or {}).get("head"), "since": None, "full": True, "carried": carried,
           "has_carried": bool(carried), "full_reason": ""}
    try:
        head = worktree.head(wt)
    except worktree.GitError:
        head = None
    if not last:
        return {**out, "full_reason": "first review"}
    if (st.get("run") or {}).get("full_review"):
        return {**out, "full_reason": "a full review was requested"}
    if not stage.incremental:
        return {**out, "full_reason": "the stage reviews the whole branch every round (incremental: false)"}
    if not head:
        return {**out, "full_reason": "HEAD is unknown"}
    candidates = [(last.get("head_before"), last.get("started_at") or "")]
    if user:
        candidates.append((user.get("head"), user.get("at") or ""))
    candidates = sorted((c for c in candidates if c[0]), key=lambda c: c[1], reverse=True)
    for reviewed, _ in candidates:
        if reviewed == head or worktree.is_ancestor(wt, reviewed, head):
            return {**out, "since": reviewed, "full": False}
    return {**out, "full_reason": "the reviewed commits are no longer in the branch's history" if candidates
            else "no reviewed HEAD"}


def _context(target: Target, ticket: dict, st: dict, pl: pipelines.Pipeline, stage: pipelines.Stage, step: dict, wt: Path,
             inbox: Path, records: list[tuple[Path, dict]], ports: dict | None = None) -> dict:
    run = st["run"]
    visit = run["visits"].get(stage.id, 0)
    max_visits = _max_visits(run, stage)
    ticket_visit = 1 + sum(1 for _, r in records if r.get("stage") == stage.id and counts_as_visit(r))
    user_review = run.get("user_review")
    if user_review:
        findings = [f for f in user_review.get("findings") or [] if isinstance(f, str)]
        github = user_review.get("source") == "github"
        user_review = {**user_review, "findings": findings, "has_findings": bool(findings), "github": github,
                       "prefix": "G" if github else "U"}
    ports = ports or {}
    language = LANGUAGES.get(ticket.get("language") or "") or ""
    rounds = st.get("rounds") or []
    answered = [r for r in rounds if r.get("answer") is not None]
    project_doc = wt / "docs" / "PROJECT.md"
    root = state.run_dir(target, ticket["id"])
    return {
        "ticket": {"id": ticket["id"], "title": ticket["title"], "type": ticket["type"], "body": ticket.get("body", ""),
                   "slug": ticket.get("slug", ""), "source_ref": ticket.get("source_ref") or "",
                   "language": ticket.get("language") or "", "language_name": language},
        "project": {"name": target.name, "path": str(target.path), "github_repo": target.github_repo or ""},
        "git": {"main": str(target.path), "worktree": str(wt), "branch": ticket["branch"],
                "base": worktree.base_ref(target, ticket), "base_commit": run.get("base_commit") or ""},
        "paths": {"ticket_file": str(inbox / "ticket.md"), "plan": str(inbox / "plan.md"), "deferred": str(inbox / "deferred.md"),
                  "answers": str(inbox / "answers.md"), "inbox": str(inbox), "artifacts": str(inbox / "artifacts"),
                  "result_file": str(inbox / "result.json"), "project_doc": str(project_doc), "run_dir": str(root)},
        "project_doc": {"exists": project_doc.is_file(), "path": str(project_doc)},
        "run": {"n": run["n"], "stage": stage.id, "visit": visit, "ticket_visit": ticket_visit,
                "via": step.get("via"), "round": run.get("round", 0), "max_visits": max_visits,
                "last_visit": bool(max_visits) and visit >= max_visits,
                "adjudicated": (run.get("extra_visits") or {}).get(stage.id, 0) > 0},
        "user_review": user_review,
        "review": {**_review_context(target, ticket["id"], st, stage, records, wt, ticket_visit),
                   "last_round": _last_review_round(run, pl, stage)} if stage.result == "review" else None,
        "engine_checks": _engine_checks(target, pl, records, wt),
        "ports": {"base": int(ports["PIPELINE_PORT"]), "end": int(ports["PIPELINE_PORT_END"])}
        if ports.get("PIPELINE_PORT") and ports.get("PIPELINE_PORT_END") else None,
        "answers_markdown": plan.answers_markdown(answered),
        "has_answers": bool(answered),
        "attempt": _attempt_context(st, step, inbox),
        "prev": _prev(records, run["n"]),
        "today": date.today().isoformat(),
        "now": state.now(),
        "language": language,
        "memory": _memory(target, ticket, stage, _files_in_play(st, records, wt)),
        "protected": protected.prompt_view(target, ticket["id"], st),
    }


def _prompt(target: Target, pl: pipelines.Pipeline, stage: pipelines.Stage, ctx: dict, nudge: dict | None = None) -> str:
    """The rendered stage prompt (`stage` as it runs on this visit), a pending nudge, then the result protocol."""
    body = render.render(pipelines.prompt_text(stage.prompt), ctx, _partials(target, stage)).rstrip()
    if nudge:
        body += "\n\n" + dispatch.nudge_section(nudge).rstrip()
    can_ask = pl.can_ask(stage)
    return body + "\n" + results.footer(ctx["paths"]["result_file"], stage.result, can_ask, stage.route_names(),
                                        pl.ask_targets(stage) if can_ask else [], ctx["paths"]["artifacts"])


def _model(target: Target, stage: pipelines.Stage) -> str:
    return {"plan": target.plan_model, "build": target.build_model}.get(stage.model, stage.model)


# --- one stage ----------------------------------------------------------------------------------

def _take_pending_nudge(target: Target, ticket_id: str, stage_dir: Path) -> dict | None:
    """The ticket's pending nudge, claimed for this session's prompt (os.replace: it is read once)."""
    claimed = stage_dir / "nudge-prompt.json"
    try:
        os.replace(state.run_dir(target, ticket_id) / dispatch.NUDGE_FILE, claimed)
        data = state.read_json(claimed)
    except (OSError, ValueError):
        return None
    finally:
        claimed.unlink(missing_ok=True)
    return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None


def _log_nudge(st: dict, nudge: dict, stage_id: str, stage_dir: Path, delivery: str) -> None:
    st.setdefault("nudges", []).append({**{k: nudge.get(k) for k in ("text", "by", "via", "kind", "at")},
                                        "stage": stage_id, "phase_dir": stage_dir.name, "delivery": delivery})


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def _commit_records(wt: Path, stage: pipelines.Stage, ticket_id: str) -> str | None:
    """Commit the stage's `commit_paths` when they changed (only them: `commit --only`); returns the commit."""
    paths = list(stage.commit_paths)
    if not worktree.git("status", "--porcelain", "--untracked-files=all", "--", *paths, cwd=wt).stdout.strip():
        return None
    worktree.git("add", "-A", "--", *paths, cwd=wt)
    message = (stage.commit_message or "docs({ticket}): BMad {stage} records").replace("{ticket}", ticket_id).replace("{stage}", stage.id)
    worktree.git("commit", "--no-verify", "--quiet", "--message", message, "--only", "--", *paths, cwd=wt)
    return worktree.head(wt)


def _read_only_changes(wt: Path, stage: pipelines.Stage, head_before: str, record: dict) -> list[str]:
    """What a `writes = false` stage changed: uncommitted paths, else its commits (the head). Its own
    `commit_paths` (records the engine commits) do not count."""
    if not stage.commit_paths:
        return record["dirty_after"] or ([record["head_after"][:10]] if record["commits"] else [])
    committed = worktree.git("diff", "--name-only", head_before, "HEAD", cwd=wt).stdout.split("\n")
    return [p for p in (*record["dirty_after"], *committed) if p and not _under(p, stage.commit_paths)]



def _run_stage(target: Target, ticket: dict, st: dict, pl: pipelines.Pipeline, base: pipelines.Stage, step: dict,
               wt: Path, inbox: Path, ports: dict) -> dict:
    ticket_id = ticket["id"]
    run = st["run"]
    records = _records(target, ticket_id)
    if run.get("base_commit") is None:
        run["base_commit"] = worktree.merge_base(wt, worktree.base_ref(target, ticket))
    _refresh_inbox(target, ticket_id, inbox)
    visit = run["visits"].get(base.id, 0)
    stage = pipelines.effective(base, visit)
    resume = step.get("resume")
    ctx = _context(target, ticket, st, pl, stage, step, wt, inbox, records, ports)
    stage_dir = state.new_phase_dir(target, ticket_id, stage.id)
    nudge = step.get("nudge")
    pending = _take_pending_nudge(target, ticket_id, stage_dir)
    if pending:  # a person's message replaces the watchdog's
        nudge = pending
        _log_nudge(st, pending, stage.id, stage_dir, "prompt")
    if resume and resume.get("reason") == "nudge" and nudge:
        prompt = dispatch.nudge_resume_prompt(nudge, resume["prompt"])
    elif resume:
        prompt = dispatch.resume_prompt(resume["prompt"], nudge)
    else:
        prompt = _prompt(target, pl, stage, ctx, nudge)
    (stage_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    if stage.result == "implement" and not run.get("pre_implement"):
        run["pre_implement"] = worktree.head(wt)  # where an intent gap in this run sets the branch back to
    st.update(status="running", stage=stage.id)
    state.save_state(target, ticket_id, st)
    roadmap.ticket_changed(target, ticket_id)  # its roadmap item, if any, moves to in-progress (once: idempotent)
    remote_before = worktree.remote_refs(target)
    head_before = worktree.head(wt)
    record = {"phase": stage.id, "stage": stage.id, "title": stage.title, "pipeline": pl.name, "run": run["n"],
              "visit": visit, "ticket_visit": ctx["run"]["ticket_visit"], "max_visits": ctx["run"]["max_visits"],
              "last_visit": ctx["run"]["last_visit"], "overrides": pipelines.override_keys(base, visit),
              "via": step.get("via"), "base_commit": run.get("base_commit"), "head_before": head_before,
              **({"review": ctx["review"]} if ctx["review"] else {}), **({"nudge": nudge} if nudge else {}),
              **({"retry_of": step["retry_of"]} if step.get("retry_of") else {}),
              "memory": {"shown": ctx["memory"]["ids"], "used": [], "warnings": ctx["memory"].get("warnings") or []}}
    env = {"PIPELINE_RESULT_FILE": ctx["paths"]["result_file"], "PIPELINE_STAGE": stage.id, "PIPELINE_TICKET": ticket_id,
           "PIPELINE_INBOX": str(inbox), "PIPELINE_PLAN": ctx["paths"]["plan"], "PIPELINE_WORKTREE": str(wt),
           "PIPELINE_BASE_COMMIT": run.get("base_commit") or "", "PIPELINE_ARTIFACTS_DIR": ctx["paths"]["artifacts"],
           **_visit_env(ctx), **ports}
    try:
        dispatch.run(target, ticket_id, wt, inbox, stage_dir, resume["model"] if resume else _model(target, stage), prompt,
                     record=record, resume_session=resume["session_id"] if resume else None,
                     timeout=stage.timeout, tools_deny=stage.tools_deny, effort=stage.effort,
                     quiet_after=stage.quiet_after or target.quiet_after, interrupt_on_quiet=stage.retry.on_quiet,
                     stall_after=target.stall_after,
                     json_schema=results.json_schema(stage.result), extra_env=env)
    except BaseException:
        _fail(st, f"{stage.id}: the session could not be run", stage.id)
        _settle(target, ticket_id, st)
        raise

    try:
        nudged = state.read_json(stage_dir / dispatch.NUDGE_FILE)
    except (OSError, ValueError):
        nudged = None
    if isinstance(nudged, dict):
        record["nudged"] = nudged
        _log_nudge(st, nudged, stage.id, stage_dir, "interrupt")
    result_file = inbox / "result.json"
    file_text = result_file.read_text(encoding="utf-8") if result_file.exists() else None
    if file_text is not None:
        shutil.move(result_file, stage_dir / "result.json")
    resolved = results.resolve(stage.result, file_text, record.get("structured_output"), record.get("final_message", ""))
    result = resolved["result"]
    if result is not None and "artifacts" in result:
        # Evidence paths are kept relative to inbox/artifacts, the only place the panel serves them from.
        result["artifacts"] = results.normalize_artifacts(result["artifacts"], ctx["paths"]["artifacts"])
    # Patches of protected files: validated here (they may also turn an implement's FAILED into READY), stored below.
    protected_warnings = protected.settle(stage.result, result, ctx["paths"]["artifacts"], wt) if result is not None else []
    record.update(result=result, result_source=resolved["source"], result_warnings=resolved["warnings"] + protected_warnings,
                  result_error=resolved["error"], verdict=(result or {}).get("verdict"), route=(result or {}).get("route"))
    if result is not None:
        record["memory"]["used"] = _memory_used(target, ticket_id, result)
    interrupt = record.get("interrupt") or ""
    # An interrupt decides the outcome even when the agent wrote a result as the signal landed.
    if record.get("stopped"):
        record.update(state="killed", kill_reason="stop")
    elif interrupt == "timeout" or record.get("timed_out"):
        record["state"] = "timeout"
    elif interrupt.startswith("nudge:"):
        record.update(state="killed", kill_reason=interrupt)
    elif result is not None:
        record["state"] = "ok"
    elif (hit := quota.rejection(record)):
        # The account's usage limit, not the work: the ticket waits for the reset and resumes this session.
        record.update(state="quota", quota_error=hit["text"],
                      quota_hit={k: hit[k] for k in ("source", "resets_at", "window")})
    elif record.get("parse_error"):
        record["state"] = "lost"  # ended without a result event: crashed; the session can be resumed
    else:
        record["state"] = "failed"
    why = {"timeout": "timeout", "lost": "lost"}.get(record["state"]) or (
        "quiet" if record.get("kill_reason") == "nudge:quiet" else None)
    if why and file_text is None:
        # Where the time went (the slowest tool calls, the memory pressure seen): run.json and the failure's text.
        record["time_report"] = stalls.explain(why, _seconds(record), record.get("tool_calls") or [],
                                               (record.get("host") or {}).get("worst"))

    retry = _auto_retry(st, base, record, [r for _, r in records])
    if isinstance(retry, dict):
        record["retry"] = retry
        if retry["n"] is not None:
            run.setdefault("retries", {})[stage.id] = retry["n"]
    elif retry:
        record["retry_exhausted"] = retry
    if stage.commit_paths and "retry" not in record and record["state"] != "quota":
        # The stage's records (BMad's spec under _bmad-output/) are committed on the ticket branch, before the
        # tree is judged: they never leave it dirty. A session that resumes on its own keeps them in the tree.
        record["records_commit"] = _commit_records(wt, stage, ticket_id)
    record["head_after"] = worktree.head(wt)
    record["commits"] = int(worktree.git("rev-list", "--count", f"{head_before}..HEAD", cwd=wt).stdout)
    record["dirty_after"] = worktree.dirty_paths(wt)
    if record["state"] in ("timeout", "killed", "lost") and record["dirty_after"] and "retry" not in record:
        # Keep half-done edits (stash, never delete); the next session gets the clean tree it expects.
        # A session that resumes on its own keeps them in the tree.
        record["stash"] = worktree.stash_all(wt, f"ziggy: interrupted {stage_dir.name} of {ticket_id}")
    elif record["state"] == "ok" and not stage.writes and (changed_files := _read_only_changes(wt, stage, head_before, record)):
        record["stash"] = worktree.stash_all(wt, f"ziggy: {stage_dir.name} of {ticket_id} changed files")
        record["state"] = "failed"
        record["result_error"] = (f"{stage.id} must not change the repository, but it "
                                  + ("left changes in" if record["dirty_after"] else "committed") + ": "
                                  + ", ".join(changed_files[:10]))
    remote_after = worktree.remote_refs(target)
    changed = None if remote_before is None or remote_after is None else _changed_refs(target, ticket_id, remote_before, remote_after)
    record["remote_unchanged"] = None if changed is None else not changed
    record["remote_changed"] = changed or []
    if record["state"] == "ok" and result.get("protectedPatches"):
        added = protected.record(target, ticket_id, st, result["protectedPatches"], ctx["paths"]["artifacts"], stage.id,
                                 stage_dir.name, base_head=record["head_after"])
        record["protected_patches"] = [e["n"] for e in added]
    if (inbox / "plan.md").exists():
        shutil.copy2(inbox / "plan.md", stage_dir / "plan.md")
    state.write_json(stage_dir / "run.json", record)
    return record


def recheck_source(target: Target, ticket_id: str, st: dict) -> tuple[Path, dict] | None:
    """The visit a checks-only retry repeats the checks of: on a failed ticket, the newest record of the stage it failed
    in, READY with an engine check that did not pass. The agent's work there stands; only the checks' verdict is
    in doubt (an environment fixed since, a flaky check). None when that is not the case."""
    if st.get("status") != "failed":
        return None
    stage_id = (st.get("next") or {}).get("stage")
    found = [(d, r) for d, r in _records(target, ticket_id) if r.get("stage") == stage_id]
    if not found:
        return None
    d, r = found[-1]
    failing = [c for c in r.get("checks") or [] if c.get("source") == "engine" and c.get("status") != "pass"]
    return (d, r) if r.get("state") == "ok" and r.get("verdict") == "READY" and failing else None


def _recheck_stage(target: Target, ticket: dict, st: dict, pl: pipelines.Pipeline, base: pipelines.Stage, step: dict,
                   wt: Path) -> dict:
    """A checks-only visit: no session; the earlier READY result is carried over as this visit's, and _drive runs the
    checks on the branch as it is and routes on, as after the agent."""
    ticket_id = ticket["id"]
    run = st["run"]
    if run.get("base_commit") is None:
        run["base_commit"] = worktree.merge_base(wt, worktree.base_ref(target, ticket))
    source_dir = Path(step["checks_only"])
    source = state.read_json(source_dir / "run.json")
    records = _records(target, ticket_id)
    visit = run["visits"].get(base.id, 0)
    max_visits = _max_visits(run, base)
    stage_dir = state.new_phase_dir(target, ticket_id, base.id)
    st.update(status="running", stage=base.id)
    state.save_state(target, ticket_id, st)
    head = worktree.head(wt)
    now = state.now()
    record = {"dir": str(stage_dir), "session_id": None, "model": None, "started_at": now, "finished_at": now, "exit_code": 0,
              "phase": base.id, "stage": base.id, "title": base.title, "pipeline": pl.name, "run": run["n"], "visit": visit,
              "ticket_visit": 1 + sum(1 for _, r in records if r.get("stage") == base.id and counts_as_visit(r)),
              "max_visits": max_visits, "last_visit": bool(max_visits) and visit >= max_visits, "overrides": [],
              "via": step.get("via"), "base_commit": run.get("base_commit"), "head_before": head, "head_after": head,
              "checks_only": source_dir.name, "state": "ok", "result": dict(source["result"]), "result_source": "carried",
              "result_warnings": [f"checks only: the result of {source_dir.name} carried over, no session ran"],
              "result_error": None, "verdict": "READY", "route": None, "commits": 0, "dirty_after": worktree.dirty_paths(wt),
              "cost_usd": 0.0, "memory": {"shown": [], "used": [], "warnings": []}}
    state.write_json(stage_dir / "run.json", record)
    return record


def recheck(target: Target, ticket_id: str) -> list[dict]:
    """A failed ticket whose last visit was READY but failed an engine check: a new run that repeats only the checks
    on the branch as it is (no agent, no cost), then routes on as that stage would have: a pass continues to the next
    stage, a code failure goes back to the implementer as usual."""
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        source = recheck_source(target, ticket_id, st)
        if source is None:
            raise PipelineError(f"{ticket_id}: only a failed ticket whose last visit was READY and failed an engine check "
                                "can re-run its checks alone; `run` retries the stage")
        step = _new_run(st, source[1]["stage"])
        step["checks_only"] = str(source[0])
        return _drive(target, ticket, st, step)


def _visit_env(ctx: dict) -> dict[str, str]:
    return {"PIPELINE_VISIT": str(ctx["run"]["visit"]), "PIPELINE_LAST_VISIT": "1" if ctx["run"]["last_visit"] else "0"}


def _auto_retry(st: dict, stage: pipelines.Stage, record: dict, earlier: list[dict]) -> dict | str | None:
    """Whether the session continues in itself: a nudge always; a lost, timed-out or quiet one when the stage's
    retry policy says so, at most retry.max times per run (one count for every cause). Returns the decision
    {cause, n, max, delay_s}, the message when no retries are left, or None."""
    state_, reason = record["state"], record.get("kill_reason") or ""
    if not record.get("session_id"):
        return None
    if state_ == "killed" and reason == "nudge:user":
        return {"cause": "nudge", "n": None, "max": None, "delay_s": 0} if record.get("nudged") else None
    policy = stage.retry
    cause = ("lost" if state_ == "lost" and policy.on_lost else
             "timeout" if state_ == "timeout" and policy.on_timeout else
             "quiet" if state_ == "killed" and reason == "nudge:quiet" and policy.on_quiet else None)
    if cause is None:
        return None
    run_n = st["run"]["n"]
    tries = 1 + sum(1 for r in earlier if r.get("run") == run_n and r.get("stage") == stage.id
                    and (r.get("state") in ("lost", "timeout") or (r.get("state") == "killed" and r.get("kill_reason") == "nudge:quiet")))
    if tries > policy.max:
        return f"{stage.id} {RETRY_WHAT[cause]}; no retries left ({policy.max} per run)"
    delay = 0 if cause == "lost" else min(policy.backoff * 2 ** (tries - 1), policy.max_backoff)
    return {"cause": cause, "n": tries, "max": policy.max, "delay_s": delay}


def _retry_step(target: Target, record: dict, step: dict, stage: pipelines.Stage) -> dict:
    """The step that continues an interrupted session in itself (the same visit: `resume` is set)."""
    cause = record["retry"]["cause"]
    nudge = None
    if cause in ("nudge", "quiet"):
        nudge = record.get("nudged") or {"text": "no output for a long time", "by": "ziggy", "kind": "quiet",
                                         "at": state.now()}
    elif cause == "timeout":
        nudge = {"text": f"it ran past its timeout ({dispatch.fmt_duration(stage.timeout or target.timeout)})",
                 "by": "ziggy", "kind": "timeout", "at": state.now()}
    return {"stage": stage.id, "via": step.get("via"), "retry_of": Path(record["dir"]).name, "nudge": nudge,
            "resume": {"session_id": record["session_id"], "model": record["model"], "dir": record["dir"],
                       "prompt": record["prompt"].split(dispatch.RESUME_MARK, 1)[-1],
                       "reason": "continue" if cause == "lost" else "nudge"}}


def _wait_to_retry(target: Target, ticket_id: str, st: dict, stage_id: str, retry: dict) -> bool:
    """Sleep a retry's backoff, holding the lock and the run slot (nothing else would restart the run);
    `stop` (SIGTERM) ends the wait and fails the run. Returns whether to go on."""
    at = datetime.now(timezone.utc) + timedelta(seconds=retry["delay_s"])
    st["retry_at"] = at.isoformat(timespec="seconds")
    state.save_state(target, ticket_id, st)
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _stop_retry_wait)
    stopped = False
    try:
        time.sleep(retry["delay_s"])
    except _RetryWaitStopped:
        stopped = True
    finally:
        signal.signal(signal.SIGTERM, previous)
        st.pop("retry_at", None)
    if stopped:
        _fail(st, "stopped while waiting to retry", stage_id)
        _settle(target, ticket_id, st)
        return False
    state.save_state(target, ticket_id, st)
    return True


def _stop_retry_wait(signum, frame):
    raise _RetryWaitStopped()


# --- the account's usage limit ------------------------------------------------------------------

def _quota_hold(target: Target, ticket_id: str, st: dict, step: dict, force: bool = False) -> bool:
    """Admission: a stage is not started while the target's Claude login is over its limit. The ticket waits with
    status `quota` and `step` in `next`; quota.wake starts it when the window resets. `force` (run --now) starts
    it anyway, with a warning. Returns whether it was held."""
    account = quota.account_key(target)
    blocked = quota.blocked(account)
    if blocked and force:
        print(f"{ticket_id}: warning: {blocked.get('label') or account} is over its usage limit until "
              f"{quota.clock(blocked['blocked_until'])}; starting anyway (--now)", file=sys.stderr, flush=True)
        return False
    if not blocked:
        st.pop("quota", None)
        return False
    quota.held(target, ticket_id, st, step, account=account, resets_at=blocked["blocked_until"], reason="admission",
               stage=step["stage"], window=blocked.get("blocked_window"))
    if st.get("run"):
        st["run"]["outcome"] = quota.STATUS
    _settle(target, ticket_id, st)
    print(f"{ticket_id}: {st['quota']['label']} is over its usage limit; {step['stage']} starts at "
          f"{quota.clock(quota.epoch_of(st['quota']['resume_at']))} (ziggy run {ticket_id} --now starts it anyway)",
          file=sys.stderr, flush=True)
    return True


def _host_hold(target: Target, ticket_id: str, st: dict, step: dict) -> bool:
    """Admission: a stage is not started while the machine is short of memory (host.busy: pressure critical, or swap
    at `[host] swap_hold` or more). The ticket waits with status `host` and `step` in `next`; host.wake starts it after
    two calm panel ticks. run --now (IGNORE_HOST) starts it anyway, and its later stages too, with a warning. A running
    stage is never stopped for it. Returns whether it was held."""
    cfg = config.load_host()
    s = host.sample()
    busy = cfg.enabled and host.busy(s, cfg.swap_hold)
    if not busy or IGNORE_HOST:
        st.pop("host", None)
        if busy:
            print(f"{ticket_id}: warning: the machine is short of memory ({host.cli_text(s, cfg)}); starting "
                  f"{step['stage']} anyway (--now)", file=sys.stderr, flush=True)
        return False
    host.held(st, step, s=s)
    if st.get("run"):
        st["run"]["outcome"] = host.STATUS
    _settle(target, ticket_id, st)
    print(f"{ticket_id}: the machine is short of memory ({host.cli_text(s, cfg)}); {step['stage']} starts once it has "
          f"been calm for two panel ticks (ziggy run {ticket_id} --now starts it anyway)", file=sys.stderr, flush=True)
    return True


def _seconds(record: dict) -> float:
    """How long a session ran, from its run.json times."""
    try:
        return max(0.0, (datetime.fromisoformat(record["finished_at"]) - datetime.fromisoformat(record["started_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def _quota_wait(target: Target, ticket_id: str, st: dict, record: dict, step: dict, stage: pipelines.Stage) -> None:
    """The session hit the account's usage limit: block the account until the window resets, and let the ticket
    wait (status `quota`) to resume this same session then, as the same visit, with a short note on why it stopped."""
    hit = record["quota_hit"]
    try:
        quota.block(record["account"], hit["resets_at"], hit["window"], hit["source"])
    except (OSError, state.LockError) as exc:
        print(f"{ticket_id}: the quota ledger was not updated: {exc}", file=sys.stderr)
    nudge = {"text": "your Claude account's usage limit was reached", "by": "ziggy", "kind": "quota", "at": state.now()}
    resume = {"stage": stage.id, "via": step.get("via"), "retry_of": Path(record["dir"]).name, "nudge": nudge,
              "resume": {"session_id": record["session_id"], "model": record["model"], "dir": record["dir"],
                         "prompt": record["prompt"].split(dispatch.RESUME_MARK, 1)[-1], "reason": "nudge"}}
    quota.held(target, ticket_id, st, resume, account=record["account"], resets_at=hit["resets_at"], reason="rejected",
               stage=stage.id, window=hit["window"], phase_dir=Path(record["dir"]).name)
    st["run"]["outcome"] = quota.STATUS
    _settle(target, ticket_id, st)
    from . import attention  # attention imports this module
    attention.quota_changed()  # 'Kota doldu: <hesap> · HH:MM' in the inbox (never raises)
    print(f"{ticket_id}: {stage.id} hit {st['quota']['label']}'s usage limit; it continues in its session at "
          f"{quota.clock(quota.epoch_of(st['quota']['resume_at']))}", file=sys.stderr, flush=True)


def _run_checks(target: Target, ticket_id: str, wt: Path, stage_dir: Path, record: dict, ports: dict) -> dict | None:
    """The target's checks after a READY implement; returns the first failure (or None). A tree left with
    uncommitted changes fails first: the review diffs commits."""
    outcomes = []
    dirty = worktree.dirty_paths(wt)
    if dirty:
        outcomes.append({"name": "clean-tree", "status": "fail", "source": "engine", "exit_code": None, "duration_ms": 0,
                         "detail": "uncommitted changes: " + ", ".join(dirty[:20])})
    elif target.checks:
        env = dispatch.environment(target, ticket_id, wt, {
            "PIPELINE_TICKET": ticket_id, "PIPELINE_WORKTREE": str(wt), "PIPELINE_VISIT": str(record["visit"]),
            "PIPELINE_LAST_VISIT": "1" if record.get("last_visit") else "0", **ports})
        outcomes = engine_checks.run(target, ticket_id, wt, list(target.checks), stage_dir / "checks.log", env)
    result = record["result"]
    result["checks"] = [*(result.get("checks") or []), *outcomes]
    record["checks"] = outcomes
    state.write_json(stage_dir / "run.json", record)
    return next((o for o in outcomes if o["status"] != "pass"), None)


# --- intent gaps --------------------------------------------------------------------------------

def published(ticket: dict, st: dict) -> bool:
    """The branch is out there (a PR opened or adopted, a push recorded): its history is never rewritten."""
    return bool(st.get("pr") or st.get("published_head") or ticket.get("pull"))


def _pre_implement(run: dict, pl: pipelines.Pipeline, records: list[tuple[Path, dict]]) -> str | None:
    """The commit before the first implement of this run; a run that built nothing yet (it started at a review)
    takes the first implement of the newest run that had one."""
    if run.get("pre_implement"):
        return run["pre_implement"]
    builds = {s.id for s in pl.stages if s.result == "implement"}
    found = [r for _, r in records if r.get("stage") in builds and r.get("head_before")]
    newest = max((r.get("run") or 0 for r in found), default=None)
    return next((r["head_before"] for r in found if (r.get("run") or 0) == newest), None)


def _set_aside(target: Target, ticket: dict, st: dict, pl: pipelines.Pipeline, stage: pipelines.Stage, stage_dir: Path,
               wt: Path, gap: dict) -> dict:
    """An intent gap: save what was built since the commit before the run's first implement as
    `attempts/attempt-<n>.patch` (its commit list, then `git diff`) and `attempt-<n>.json`, then reset the ticket
    branch to that commit (the plan stays: it lives in the inbox). A published branch keeps its code; only the
    question opens. Uncommitted edits are stashed either way. Returns the round's `intent_gap`."""
    ticket_id = ticket["id"]
    root = state.run_dir(target, ticket_id) / ATTEMPTS
    root.mkdir(parents=True, exist_ok=True)
    n = 1 + max((int(m.group(1)) for p in root.iterdir() if (m := re.fullmatch(r"attempt-(\d+)\.json", p.name))), default=0)
    head = worktree.head(wt)
    pre = _pre_implement(st["run"], pl, _records(target, ticket_id))
    stash = worktree.stash_all(wt, f"ziggy: intent gap of {ticket_id}, attempt {n}")
    commits, diff, kept = [], b"", None
    if not pre:
        kept = "nothing built"
    elif not worktree.is_ancestor(wt, pre, head):
        kept = "not in history"
    elif pre == head:
        kept = "nothing built"
    else:
        log = worktree.git("log", "--reverse", "--format=%H %s", f"{pre}..{head}", cwd=wt).stdout
        commits = [{"sha": line[:40], "subject": line[41:]} for line in log.splitlines() if line]
        diff = subprocess.run(["git", "diff", "--binary", f"{pre}..{head}"], cwd=wt, capture_output=True, check=True).stdout
        # A slice whose branch already has the slice above built on it keeps its history too (stack.stacked_branches).
        kept = "published" if published(ticket, st) else "stacked" if stack.stacked_branches(target, ticket_id) else None
    summary = " ".join(gap["summary"].split())
    lines = [f"# ziggy: attempt {n} of {ticket_id}, set aside by {stage.id} ({stage_dir.name})",
             f"# Intent gap: {summary[:400]}", f"# Range: {pre or '?'}..{head}", "# Commits:",
             *(f"#   {c['sha'][:10]} {c['subject']}" for c in commits), *([] if commits else ["#   (none)"]), "#", ""]
    patch = root / f"attempt-{n}.patch"
    patch.write_bytes("\n".join(lines).encode("utf-8") + diff)
    if kept is None:
        worktree.git("reset", "--keep", "--quiet", pre, cwd=wt)  # the tree is clean: the stash took the rest
    out = {"attempt": n, "stage": stage.id, "phase_dir": stage_dir.name, "summary": gap["summary"],
           "evidence": gap.get("evidence") or "", "patch": f"{ATTEMPTS}/{patch.name}", "head": head,
           "reset_to": pre if kept is None else None, "reset": kept is None, "kept": kept, "commits": len(commits),
           "stash": stash}
    state.write_json(root / f"attempt-{n}.json", {**out, "run": st["run"]["n"], "pre_implement": pre,
                                                  "commit_list": commits, "at": state.now()})
    return out


_KEPT = {"published": "the branch is already published, so its code was kept as it is (not reset)",
         "not in history": "the commit before the implement is no longer in the branch's history, so nothing was reset",
         "nothing built": "nothing was committed since the commit before the implement, so nothing was reset",
         "stacked": "the slice above is already built on this branch, so its code was kept as it is (not reset)"}


def _attempt_context(st: dict, step: dict, inbox: Path) -> dict | None:
    """The attempt an intent gap set aside, for the visit its answers continue at."""
    if (step.get("via") or {}).get("route") != "intentGap":
        return None
    gap = next((r["intent_gap"] for r in reversed(st.get("rounds") or []) if r.get("intent_gap")), None)
    if not gap:
        return None
    return {**gap, "patch": str(inbox / gap["patch"]), "kept_note": _KEPT.get(gap.get("kept") or "", "")}


# --- routing ------------------------------------------------------------------------------------

def _drive(target: Target, ticket: dict, st: dict, step: dict) -> list[dict]:
    """Run stages from `step` until the run ends or waits for a person, or, while a ticket it starts after is not
    done, wait for it (status `waiting`, the step kept in `next`; stack.wake runs it later)."""
    ticket_id = ticket["id"]
    pl = _pipeline(st, ticket_id)
    pending = stack.unmet(target, ticket)
    if pending:
        st.update(status=stack.WAITING, waiting_for=[d["id"] for d in pending], next=step)
        _settle(target, ticket_id, st)
        then = "the inbox asks to start it then" if target.mode == "controlled" else "it starts on its own then"
        print(f"{ticket_id}: waiting for {', '.join(d['id'] for d in pending)} to be done; {then}", file=sys.stderr, flush=True)
        return []
    st.pop("waiting_for", None)
    if _quota_hold(target, ticket_id, st, step, force=IGNORE_QUOTA) or _host_hold(target, ticket_id, st, step):
        return []
    try:
        wt, inbox, ports = _ready_worktree(target, ticket, st, allow_dirty=bool(step.get("resume")))
    except MissingBmadFiles as exc:
        _fail(st, str(exc), step["stage"])
        _settle(target, ticket_id, st)
        raise
    records = []
    while True:
        if records and (_quota_hold(target, ticket_id, st, step) or _host_hold(target, ticket_id, st, step)):
            return records  # another session hit the limit meanwhile, or the machine ran short of memory
        stage = pl.stage(step["stage"])
        run = st["run"]
        if not step.get("resume"):
            run["visits"][stage.id] = run["visits"].get(stage.id, 0) + 1
        record = (_recheck_stage(target, ticket, st, pl, stage, step, wt) if step.get("checks_only")
                  else _run_stage(target, ticket, st, pl, stage, step, wt, inbox, ports))
        records.append(record)
        stage_dir = Path(record["dir"])
        if record["state"] == "quota":
            _quota_wait(target, ticket_id, st, record, step, stage)
            return records
        if record["state"] != "ok":
            retry = record.get("retry")
            if retry:
                step = _retry_step(target, record, step, stage)
                if retry["cause"] != "nudge":
                    print(f"{ticket_id}: {stage.id} {RETRY_WHAT[retry['cause']]}; retry {retry['n']}/{retry['max']}"
                          + (f" in {retry['delay_s']}s" if retry["delay_s"] else "") + " in its session",
                          file=sys.stderr, flush=True)
                    state.save_state(target, ticket_id, st)
                    _attention(target, ticket_id)
                if retry["delay_s"] and not _wait_to_retry(target, ticket_id, st, stage.id, retry):
                    return records
                continue
            reason = record.get("result_error") or record["state"]
            error = record.get("retry_exhausted") or f"{stage.id} {record['state']}: {reason}"
            if record.get("time_report"):  # first line: where the time went (the panel's next step shows it)
                error = f"{stage.id}: {record['time_report']}\n{error}"
            _fail(st, error, stage.id)
            _settle(target, ticket_id, st)
            return records
        result = record["result"]
        if result.get("route") == results.CHECKS_FAILED:
            record.setdefault("result_warnings", []).append(f"route {results.CHECKS_FAILED} is the engine's; ignored")
            result.pop("route")
        for kind in ("risk", "size"):
            st[kind] = pipelines.higher(kind, st.get(kind), result.get(kind))
        st["tasks"] = plan.tasks(plan.read(inbox / "plan.md"))
        if stage.checks and result["verdict"] == "READY":
            failed = _run_checks(target, ticket_id, wt, stage_dir, record, ports)
            st["checks"] = record["checks"]
            if failed and failed.get("environment"):
                # The implementer cannot fix the check's environment: sending it back would only burn its visits.
                cause = failed["environment"]
                _fail(st, f'{stage.id}: check "{failed["name"]}" failed in its environment, not in the code: '
                          f'{cause["why"]}. The output said: {cause["line"][:300]}. The code was not sent back; fix the '
                          "check command (or its check_sandbox preset), then re-run the checks alone (recheck).", stage.id)
                _settle(target, ticket_id, st)
                return records
            if failed:
                if results.CHECKS_FAILED in stage.routes:
                    result["route"] = results.CHECKS_FAILED
                else:
                    _fail(st, f'{stage.id}: check "{failed["name"]}" failed: {(failed.get("detail") or "")[:400]}', stage.id)
                    _settle(target, ticket_id, st)
                    return records
        if _review_routes(stage):
            # The prompt's rule, enforced: open `fix` findings go back to the implementer, nits deferred on the last round.
            record.setdefault("result_warnings", []).extend(
                results.settle_review_route(result, _last_review_round(run, pl, stage)))
        target_id, via, warnings = pl.target(stage, result)
        record.update(route=result.get("route"), next=target_id)
        record.setdefault("result_warnings", []).extend(warnings)

        if target_id == "ask_user":
            continue_at, adjudicate = None, False
            gap, gap_then = result.get("intentGap"), pl.intent_gap_target()
            if gap and stage.result not in ("implement", "review"):
                gap = None
            elif gap and not gap_then:
                record["result_warnings"].append("intentGap ignored: the pipeline has no implement stage to continue at")
                gap = None
            elif gap and result.get("askThen"):
                record["result_warnings"].append(f'askThen "{result["askThen"]}" ignored with an intentGap; answers '
                                                 f"continue at {gap_then}")
            asked = None if gap else result.get("askThen")
            if asked:
                allowed = pl.ask_targets(stage)
                if asked not in allowed:
                    record["result_warnings"].append(f'askThen "{asked}" ignored (allowed: {", ".join(allowed)}); '
                                                     f"answers continue at {allowed[0] if allowed else stage.id}")
                elif asked != allowed[0]:
                    last = record["visit"] >= _max_visits(run, stage)
                    if last and (run.get("extra_visits") or {}).get(stage.id, 0) > 0:
                        state.write_json(stage_dir / "run.json", record)
                        _fail(st, f"{stage.id}: still not settled after the user's adjudication (asked again on its "
                                  "last visit); the plan needs a person", stage.id)
                        _settle(target, ticket_id, st)
                        return records
                    continue_at, adjudicate = asked, last
            state.write_json(stage_dir / "run.json", record)
            if run["round"] + 1 > pl.max_rounds:
                _fail(st, f"still needs input after {pl.max_rounds} question rounds", stage.id)
                _settle(target, ticket_id, st)
                return records
            run["round"] += 1
            intent_gap = None
            if gap:
                try:
                    intent_gap = _set_aside(target, ticket, st, pl, stage, stage_dir, wt, gap)
                except (worktree.GitError, subprocess.CalledProcessError, OSError) as exc:
                    _fail(st, f"{stage.id}: reported an intent gap, but its attempt could not be set aside: {exc}", stage.id)
                    _settle(target, ticket_id, st)
                    return records
                continue_at = gap_then
                record["intent_gap"] = intent_gap
                state.write_json(stage_dir / "run.json", record)
            questions = (gap or result)["questions"] or [{"id": "q1", "text": result.get("summary") or "The agent needs a decision.",
                                                          "options": [], "recommendation": ""}]
            st.setdefault("rounds", []).append({
                "n": len(st.get("rounds") or []) + 1, "run": run["n"], "stage": stage.id, "phase_dir": stage_dir.name,
                "questions": [{k: q.get(k) for k in ("id", "text", "options", "recommendation")} for q in questions],
                "context": result.get("summary") or "", "asked_at": state.now(),
                "answer": None, "answered_at": None, "by": None, "via": None,
                "continue_at": continue_at, "adjudicate": adjudicate, "intent_gap": intent_gap})
            st.update(status="needs-user", next={"stage": continue_at or pl.answers_continue_at(stage.id), "via": None,
                                                 "resume": None})
            run["outcome"] = "needs-user"
            _settle(target, ticket_id, st)
            return records
        state.write_json(stage_dir / "run.json", record)
        if target_id == "done":
            st.update(status="done", next=None, error=None)
            run.update(finished_at=state.now(), outcome="done")
            _settle(target, ticket_id, st)
            if (run.get("user_review") or {}).get("source") == "github":
                _github_round_done(target, ticket_id, pl, run["n"])
                st.update(state.load_state(target, ticket_id))
            # Done hooks, each best effort and in this order: the GitHub round's push (above), the episode, the
            # roadmap item (review), the tickets that wait for this one, then the retro.
            _episode(target, ticket, st, wt)
            roadmap.ticket_changed(target, ticket_id)
            _wake(target, ticket_id)
            _retro(target, ticket_id)
            return records
        if target_id == "failed":
            _fail(st, f"{stage.id}: {result.get('summary') or result['verdict']}", stage.id)
            _settle(target, ticket_id, st)
            return records

        nxt = pl.stage(target_id)
        cap = _max_visits(run, nxt)
        if run["visits"].get(target_id, 0) >= cap:
            route = f" (route {via['route']})" if via else ""
            extra = cap - nxt.max_visits
            _fail(st, f"{stage.id} → {target_id}{route}: loop cap reached (max_visits {nxt.max_visits}"
                      + (f" + {extra} after adjudication" if extra else "") + ")", target_id)
            _settle(target, ticket_id, st)
            return records
        # A controlled target always stops at the plan gate, also for tickets made before it was switched.
        forced = bool(ticket.get("plan_checkpoint")) or target.mode == "controlled"
        reason = pl.gated(stage.id, st.get("risk"), st.get("size"), forced) if result["verdict"] == "READY" else None
        if reason:
            st.update(status="awaiting-approval", next={"stage": target_id, "via": via, "resume": None},
                      gate={"stage": stage.id, "risk": st.get("risk"), "size": st.get("size"), "reason": reason, "at": state.now()})
            run["outcome"] = "awaiting-approval"
            _settle(target, ticket_id, st)
            return records
        state.save_state(target, ticket_id, st)
        step = {"stage": target_id, "via": via, "resume": None}


def _github_round_done(target: Target, ticket_id: str, pl: pipelines.Pipeline, run_n: int) -> None:
    """A run that fixed GitHub feedback ended done: push so the draft PR updates, and answer on GitHub when the
    target asks (feedback.after_round). The run's outcome stands whatever happens here; a failed push leaves the
    inbox's push reminder."""
    implement = {s.id for s in pl.stages if s.result == "implement"}
    summary = next((r["result"].get("summary") or "" for _, r in reversed(_records(target, ticket_id))
                    if r.get("run") == run_n and r.get("stage") in implement and r.get("state") == "ok"
                    and isinstance(r.get("result"), dict)), "")
    try:
        out = feedback.after_round(target, ticket_id, summary)
    except Exception as exc:  # noqa: BLE001 - the run is done; the push can be redone from the panel
        print(f"{ticket_id}: pushing the GitHub round failed: {exc}", file=sys.stderr, flush=True)
    else:
        if out and out.get("pushed"):
            print(f"{ticket_id}: pushed {out['sha'][:7]} to the PR" + (f", {out['replied']} repl(ies) on GitHub" if out["replied"] else "")
                  + "".join(f"\n  reply failed: {e}" for e in out["errors"]), file=sys.stderr, flush=True)
        elif out:
            print(f"{ticket_id}: not pushed ({out['reason']})", file=sys.stderr, flush=True)
    _attention(target, ticket_id)


# --- entry points -------------------------------------------------------------------------------

def run(target: Target, ticket_id: str, full_review: bool = False) -> list[dict]:
    """new: a run from the first stage; queued (answered or approved without running): continue the run;
    failed: a new run from the stage that failed. `full_review`: every review in the new run reads the
    whole branch (not only what changed since the last review)."""
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        pl = _pipeline(st, ticket_id)
        status = st["status"]
        if full_review and status not in ("new", "failed"):
            raise PipelineError(f"{ticket_id} is {status}; a full review applies to a new run (new or failed tickets)")
        if status == "new":
            step = _new_run(st, pl.stages[0].id)
        elif status in ("queued", stack.WAITING, quota.STATUS, host.STATUS) and st.get("next"):
            # A slice above another waits from its split on, before any run of its own: its run starts now.
            step = dict(st["next"]) if st.get("run") else _new_run(st, st["next"]["stage"])
        elif status == "failed":
            start = (st.get("next") or {}).get("stage") or pl.stages[0].id
            step = _new_run(st, start if start in pl.ids() else pl.stages[0].id)
        elif status in WAITING:
            what = "answer its questions" if status == "needs-user" else "approve it (or answer with changes)"
            raise PipelineError(f"{ticket_id} is {status}; {what} to continue")
        else:
            raise PipelineError(f"{ticket_id} is {status}; nothing to run")
        if full_review:
            st["run"]["full_review"] = True
        return _drive(target, ticket, st, step)


def _record_note(target: Target, ticket_id: str, st: dict, text: str, by: str, via: str) -> None:
    """A decision that answers no open question (a change of course): plan, answers.md, history."""
    note = f"### Note ({date.today().isoformat()}, {by} via {via})\n\n" + "\n".join(f"> {line}" for line in text.splitlines())
    plan.append_decision(state.plan_path(target, ticket_id), note)
    plan.append(state.run_dir(target, ticket_id) / "answers.md", note)
    st.setdefault("answers", []).append({"at": state.now(), "text": text, "by": by, "via": via})


def answer(target: Target, ticket_id: str, text: str, by: str | None = None, via: str = "cli",
           run_after: bool = True) -> list[dict]:
    """needs-user: close the open question round and continue the run where the pipeline says;
    awaiting-approval: the text is a change to the plan, reviewed by the stage answers go to;
    failed (e.g. stopped to change course): a new run from there with the decision recorded."""
    text = text.strip()
    if not text:
        raise PipelineError("answer is empty")
    by = by or getpass.getuser()
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        pl = _pipeline(st, ticket_id)
        inbox = state.inbox_dir(target, ticket_id)
        inbox.mkdir(parents=True, exist_ok=True)
        status = st["status"]
        if status == "needs-user":
            r = open_round(st)
            if r is None:
                raise PipelineError(f"{ticket_id} has no open question round")
            r.update(answer=text[:20000], answered_at=state.now(), by=by, via=via)
            md = plan.round_markdown(r)
            plan.append(state.run_dir(target, ticket_id) / "answers.md", md)
            plan.append_decision(state.plan_path(target, ticket_id), md)
            _decision_candidates(target, ticket, st, r)
            step = dict(st["next"])
            asker = r.get("stage")
            if r.get("intent_gap") and asker:
                # The prompt there names the attempt that was set aside and why it went the wrong way.
                step["via"] = {"from": asker, "route": "intentGap"}
            elif r.get("adjudicate") and asker:
                # The asker settled its last allowed visit with the user: the answers get one more round
                # (the stage they continue at, and the asker).
                run = st["run"]
                extra = run.setdefault("extra_visits", {})
                extra[step["stage"]] = extra.get(step["stage"], 0) + 1
                if asker != step["stage"]:
                    extra[asker] = extra.get(asker, 0) + 1
                run["adjudicated"] = run.get("adjudicated", 0) + 1
                step["via"] = {"from": asker, "route": "adjudicated"}
            elif r.get("continue_at") and asker:
                step["via"] = {"from": asker, "route": "answers"}
        elif status == "awaiting-approval":
            _record_note(target, ticket_id, st, text, by, via)
            step = {"stage": pl.answers_continue_at(st["gate"]["stage"]), "via": None, "resume": None}
            st["gate"] = None
        elif status == "failed":
            _record_note(target, ticket_id, st, text, by, via)
            failed_at = (st.get("next") or {}).get("stage") or pl.stages[0].id
            step = _new_run(st, pl.answers_continue_at(failed_at) if failed_at in pl.ids() else pl.stages[0].id)
        else:
            raise PipelineError(f"{ticket_id} is {status}; answer applies to a ticket waiting for you or a failed one")
        st.update(status="queued", next=step)
        state.save_state(target, ticket_id, st)
        if not run_after:
            return []
        return _drive(target, ticket, st, step)


def approve(target: Target, ticket_id: str, run_after: bool = True) -> list[dict]:
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        if st["status"] != "awaiting-approval":
            raise PipelineError(f"{ticket_id} is {st['status']}; approve applies to a ticket waiting at the approval gate")
        st.setdefault("approvals", []).append({**(st.get("gate") or {}), "approved_at": state.now(), "by": getpass.getuser()})
        step = dict(st["next"])
        st.update(status="queued", gate=None)
        state.save_state(target, ticket_id, st)
        if not run_after:
            return []
        return _drive(target, ticket, st, step)


def request_changes(target: Target, ticket_id: str, section_md: str, *, heading: str, comments: int, source: str = "user",
                    round: int | None = None, run_after: bool = True, findings=()) -> list[dict]:
    """Review comments (the user's from the review page, or Codex findings the user kept) become a plan
    section with fix tasks, and a new run starts at the pipeline's user_review.then stage. `findings`: the
    reviewer finding ids the user asked to have fixed (the implementer reports an outcome for each)."""
    if not section_md.strip():
        raise PipelineError("no review comments to send")
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        pl = _pipeline(st, ticket_id)
        if st["status"] not in ("done", "failed"):
            raise PipelineError(f"{ticket_id} is {st['status']}; changes can be requested on a done or failed ticket")
        if not st.get("worktree") or not Path(st["worktree"]).is_dir():
            raise PipelineError(f"{ticket_id} has no worktree")
        if ticket.get("pull"):
            pulls.sync(target, ticket, st)  # fix on top of what the PR shows now
        wt = Path(st["worktree"])
        n = round or len([u for u in st.get("user_reviews") or [] if u.get("source") == source]) + 1
        inbox = state.inbox_dir(target, ticket_id)
        inbox.mkdir(parents=True, exist_ok=True)
        plan.append(state.plan_path(target, ticket_id), section_md)
        st.setdefault("user_reviews", []).append({
            "round": n, "source": source, "heading": heading, "comments": comments, "head": worktree.head(wt),
            "base_commit": worktree.merge_base(wt, worktree.base_ref(target, ticket)), "at": state.now()})
        start = pl.user_review_then or next((s.id for s in pl.stages if s.result == "implement"), pl.stages[0].id)
        step = _new_run(st, start, via={"from": source, "route": "changes"},
                        user_review={"round": n, "comments": comments, "heading": heading, "source": source,
                                     "findings": [str(f) for f in findings if str(f).strip()]})
        st["user_reviews"][-1]["run"] = st["run"]["n"]
        st.update(status="queued", next=step)
        state.save_state(target, ticket_id, st)
        if not run_after:
            return []
        return _drive(target, ticket, st, step)


def resumable(target: Target, ticket_id: str) -> dict | None:
    """The last session, when it can be continued: it crashed, its process was killed from outside, it timed
    out, or a nudge interrupted it and the run ended before it resumed. A session the user stopped is not
    resumed: that stop meant a change of course (answer)."""
    found = _records(target, ticket_id)
    if not found:
        return None
    d, record = found[-1]
    if (d / dispatch.PID_FILE).exists() or not record.get("session_id") or record.get("stopped"):
        return None
    nudged = record.get("state") == "killed" and str(record.get("kill_reason") or "").startswith("nudge:")
    if "finished_at" in record and record.get("state") not in ("lost", "timeout") and not record.get("timed_out") and not nudged:
        return None
    return {"dir": str(d), "stage": record.get("stage") or record.get("phase"), "model": record["model"],
            "session_id": record["session_id"], "prompt": record["prompt"].split(dispatch.RESUME_MARK, 1)[-1],
            "stash": record.get("stash"), "run": record.get("run"),
            **({"reason": "nudge", "nudge": record["nudged"]} if nudged and isinstance(record.get("nudged"), dict) else {})}


def resume(target: Target, ticket_id: str) -> list[dict]:
    """Continue the last session where it ended (claude --resume), then route on as usual."""
    ticket = state.load_ticket(target, ticket_id)
    with _locked(target, ticket_id) as st:
        pl = _pipeline(st, ticket_id)
        r = resumable(target, ticket_id)
        if r is None or st["status"] != "failed" or r["stage"] not in pl.ids():
            raise PipelineError(f"{ticket_id} has no interrupted session to resume ({st['status']}); use run")
        wt = Path(st["worktree"])
        if r["stash"]:
            refs = worktree.git("stash", "list", "--format=%H", cwd=wt).stdout.split()
            if r["stash"] in refs:
                worktree.git("stash", "pop", "--quiet", f"stash@{{{refs.index(r['stash'])}}}", cwd=wt)
        if st.get("run"):
            st["run"].update(finished_at=None, outcome=None, error=None)
        st["error"] = None
        return _drive(target, ticket, st, {"stage": r["stage"], "via": None, "resume": r, "nudge": r.get("nudge"),
                                           "retry_of": Path(r["dir"]).name})


def nudge(target: Target, ticket_id: str, text: str, by: str | None = None, via: str = "cli") -> dict:
    """Steer the ticket's run. A running session is interrupted (SIGINT, by its own process within a second)
    and resumes its session with the message, as the same visit; a run that has no session live (queued,
    waiting for a slot or a retry, between stages) gets it in the next stage's prompt. The last message
    wins. Takes no lock: the running process holds it. Refusals raise PipelineError."""
    text = (text or "").strip()[:dispatch.MAX_NUDGE]
    if not text:
        raise PipelineError("empty message")
    state.load_ticket(target, ticket_id)
    st = state.load_state(target, ticket_id)
    status = st.get("status")
    if status == "needs-user":
        raise PipelineError(f"{ticket_id} waits for answers; answer its questions instead")
    if status == "awaiting-approval":
        raise PipelineError(f"{ticket_id} waits for approval; approve it, or answer with the change instead")
    alive = state.running(target, ticket_id)
    waiting = bool(st.get("waiting_since") and alive)  # in line for a run slot (any status that can run)
    if status not in ("running", "queued") and not waiting:
        raise PipelineError(f"{ticket_id} has no active run ({status})")
    if status == "running" and not alive:
        raise PipelineError(f"{ticket_id}: the run's process is gone; use resume")
    message = {"text": text, "by": by or getpass.getuser(), "via": via, "kind": "user", "at": state.now()}
    live = [d for d in state.phase_dirs(target, ticket_id) if (d / dispatch.PID_FILE).exists()]
    pending = status != "running" or waiting or st.get("retry_at") or not live
    if not pending:
        d = live[-1]
        try:
            record = state.read_json(d / "run.json")
        except (OSError, ValueError):
            record = {}
        stage = record.get("stage") or st.get("stage")
        if "finished_at" in record:
            raise PipelineError(f"{stage}'s checks are running; nudge after them")
        if record.get("interrupt") or (d / dispatch.NUDGE_FILE).exists() or (d / dispatch.STOP_FILE).exists():
            raise PipelineError(f"{stage} is already stopping ({record.get('interrupt') or 'stop'})")
    state.write_json(state.run_dir(target, ticket_id) / dispatch.NUDGE_FILE, message)
    if not pending:
        return {"message": f"interrupting {stage}; it resumes with your message", "stage": stage, "delivery": "interrupt"}
    stage = (st.get("next") or {}).get("stage") if status == "queued" else st.get("stage") if (
        status == "running" and st.get("retry_at")) else None
    return {"message": f"{stage or 'the next stage'} gets your message when it starts", "stage": stage, "delivery": "queued"}


def exit_code(target: Target, ticket_id: str) -> int:
    return EXIT.get(state.load_state(target, ticket_id).get("status"), 1)
