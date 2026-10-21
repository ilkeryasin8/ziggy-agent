"""Spending caps per target, checked before every stage starts: `ticket_budget_usd` (all of one ticket's sessions,
every run) and `daily_budget_usd` (the target's sessions that started on one local day). Costs are the sessions'
own `total_cost_usd` (on a subscription, what the usage would have cost).

Over its ticket cap a run fails with the numbers: going on is your call (raise the cap, or `ziggy run --now`). Over
the day's cap a ticket waits with status `budget` and its step kept; the panel's tick starts it the next day."""
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from . import state
from .config import Target

STATUS = "budget"


def now() -> datetime:
    """The local clock (tests move it)."""
    return datetime.now().astimezone()


def _record(phase_dir: Path) -> dict:
    try:
        return json.loads((phase_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ticket_spent(target: Target, ticket_id: str) -> float:
    return round(sum(_record(d).get("total_cost_usd") or 0 for d in state.phase_dirs(target, ticket_id)), 4)


def day_spent(target: Target, day=None) -> float:
    day = day or now().date()
    total = 0.0
    for ticket_id in state.ticket_ids(target):
        for d in state.phase_dirs(target, ticket_id):
            r = _record(d)
            try:
                started = datetime.fromisoformat(r["started_at"]).astimezone(now().tzinfo).date() if r.get("started_at") else None
            except ValueError:
                started = None
            if started == day:
                total += r.get("total_cost_usd") or 0
    return round(total, 4)


def over(target: Target, ticket_id: str) -> dict | None:
    """The cap a new stage would break, {kind: "ticket"|"day", spent, cap}, or None."""
    if target.ticket_budget_usd is not None:
        spent = ticket_spent(target, ticket_id)
        if spent >= target.ticket_budget_usd:
            return {"kind": "ticket", "spent": spent, "cap": target.ticket_budget_usd}
    if target.daily_budget_usd is not None:
        spent = day_spent(target)
        if spent >= target.daily_budget_usd:
            return {"kind": "day", "spent": spent, "cap": target.daily_budget_usd}
    return None


def held(st: dict, step: dict, hit: dict) -> None:
    """Status `budget` until the next local day; `next` is the step it takes then."""
    resume = datetime.combine(now().date() + timedelta(days=1), time(0, 1), tzinfo=now().tzinfo)
    st.update(status=STATUS, next=step, budget={**hit, "stage": step.get("stage"), "since": state.now(),
                                               "resume_at": resume.isoformat(timespec="seconds")})


def due(st: dict) -> bool:
    if st.get("status") != STATUS:
        return False
    at = (st.get("budget") or {}).get("resume_at")
    try:
        return at is None or now() >= datetime.fromisoformat(at)
    except ValueError:
        return True


def wake(target: Target) -> list[str]:
    """Start every ticket of the target held for yesterday's cap, as a detached `ziggy run` job (the panel's tick).
    The run checks the caps again before its first stage."""
    started = []
    for ticket_id in state.ticket_ids(target):
        try:
            if not due(state.load_state(target, ticket_id)):
                continue
            with state.lock(target, f"wake-{ticket_id}", f"start of {ticket_id} after its daily budget reset"):
                from . import dashboard  # the panel's jobs: dashboard imports the engine
                if not due(state.load_state(target, ticket_id)) or dashboard._busy(target, ticket_id):
                    continue
                dashboard.start_job(target, ticket_id, "run", ticket_id)
            started.append(ticket_id)
            print(f"{ticket_id}: a new day's budget; started", file=sys.stderr, flush=True)
        except state.LockError:
            continue
        except (OSError, ValueError, LookupError) as exc:
            print(f"{ticket_id}: could not start after its daily budget reset: {exc}", file=sys.stderr)
    return started
