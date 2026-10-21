"""The ticket plan (`inbox/plan.md`): the agents write it; the wrapper only appends the user's decisions and
review rounds, and reads task progress from it."""
import re
from datetime import datetime
from pathlib import Path

DECISIONS = "## Decisions (user)"
_TASK = re.compile(r"^\s*[-*] \[([ xX])\] [TVU]\d", re.M)
_T_ID = re.compile(r"\bT(\d{3,})\b")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def tasks(text: str) -> dict:
    marks = _TASK.findall(text)
    return {"done": sum(1 for m in marks if m in "xX"), "total": len(marks)}


REMINDER_TASKS = 25  # open tasks a reminder lists at most
_GOAL = re.compile(r"^## Goal[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
_OPEN = re.compile(r"^\s*[-*] \[ \] [TVU]\d.*$", re.M)


def reminder(text: str) -> str:
    """What a resumed or steered session must not lose sight of: PLAN's goal and acceptance criteria and the tasks
    still open, as a prompt section ("" when PLAN has neither)."""
    goal = _GOAL.search(text)
    goal_text = goal.group(1).strip() if goal else ""
    open_tasks = [line.strip() for line in _OPEN.findall(text)]
    if not goal_text and not open_tasks:
        return ""
    parts = ["## Where this step stands (from PLAN)\n"]
    if goal_text:
        parts.append(f"Goal & acceptance, still the bar for done:\n\n{goal_text}\n")
    if open_tasks:
        more = len(open_tasks) - REMINDER_TASKS
        listed = "\n".join(open_tasks[:REMINDER_TASKS]) + (f"\n- … and {more} more" if more > 0 else "")
        parts.append(f"Still open (check each against the tree before you trust its box):\n\n{listed}\n")
    return "\n".join(parts)


def next_task_number(text: str) -> int:
    """One past the highest T-task id anywhere in the plan (V ids do not count)."""
    return max((int(n) for n in _T_ID.findall(text)), default=0) + 1


def append(path: Path, text: str) -> None:
    current = read(path)
    sep = "" if not current else "\n" if current.endswith("\n\n") else "\n\n" if not current.endswith("\n") else "\n"
    path.write_text(current + sep + text.rstrip() + "\n", encoding="utf-8")


def append_decision(path: Path, markdown: str) -> None:
    """Under `## Decisions (user)`, created at the end of the plan the first time."""
    current = read(path)
    append(path, markdown if DECISIONS in current else f"{DECISIONS}\n\n{markdown}")


def _local_date(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def round_markdown(r: dict) -> str:
    """One answered question round, as it goes into answers.md and the plan."""
    lines = [f"### Round {r['n']} ({_local_date(r.get('answered_at'))}, {r.get('by') or 'user'} via {r.get('via') or '?'})"]
    gap = r.get("intent_gap")
    if gap:
        lines += [f"Intent gap (found by {gap.get('stage') or r.get('stage') or '?'}): {gap.get('summary') or ''}",
                  *([f"Evidence: {gap['evidence']}"] if gap.get("evidence") else []),
                  f"Attempt {gap.get('attempt')} saved as `{gap.get('patch')}` in the run directory; "
                  + (f"the branch was reset to `{(gap.get('reset_to') or '')[:10]}`." if gap.get("reset")
                     else "the branch kept its code."), ""]
    lines.append("Questions:")
    for i, q in enumerate(r.get("questions") or [], 1):
        lines.append(f"{i}. {q.get('id') + ': ' if q.get('id') else ''}{q.get('text', '')}")
        if q.get("options"):
            lines.append("   Options: " + "; ".join(f"({chr(97 + j)}) {o}" for j, o in enumerate(q["options"])))
        if q.get("recommendation"):
            lines.append(f"   Recommended: {q['recommendation']}")
    lines += ["", "Answer (verbatim):", *(f"> {line}" for line in (r.get("answer") or "").splitlines() or [""])]
    return "\n".join(lines)


def answers_markdown(rounds: list[dict]) -> str:
    return "\n\n".join(round_markdown(r) for r in rounds if r.get("answer") is not None)
