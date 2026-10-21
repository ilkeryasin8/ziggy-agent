"""The weekly report: what the last seven days delivered and cost, what failed, what still waits on you, and what the
project memory has to decide. A Markdown file per ISO week (`<state>/reports/<YYYY>-W<ww>.md`), written by the panel's
tick on Monday morning (with a notification of kind "report") and by `ziggy report`; the panel's İstatistik page shows
the latest. Read-only over the state: it starts nothing."""
from datetime import date, datetime, timedelta
from pathlib import Path

from . import attention, config, memory, notify, state
from .config import Target

DAYS = 7
DUE_WEEKDAY, DUE_HOUR = 0, 9  # Monday, 09:00 local: the week before is complete
LISTED = 10  # tickets a section names before "+N more"


def now() -> datetime:
    """The local clock (tests move it)."""
    return datetime.now().astimezone()


def _day(stamp) -> date | None:
    try:
        return datetime.fromisoformat(stamp).astimezone(now().tzinfo).date() if stamp else None
    except (TypeError, ValueError):
        return None


def week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def report_dir() -> Path:
    return config.state_home() / "reports"


def report(targets: list[Target], today: date | None = None, days: int = DAYS) -> dict:
    """The `days` whole days before `today`: tickets done, merged and failed in them, cost per day, delivery numbers,
    what waits on you now, and memory to decide (candidates waiting, entries suggested for pruning)."""
    from . import dashboard  # the panel's stats; dashboard imports the engine
    today = today or now().date()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    within = lambda stamp: (d := _day(stamp)) is not None and start <= d <= end  # noqa: E731
    done, merged, failed = [], [], []
    for target in targets:
        for ticket_id in state.ticket_ids(target):
            try:
                t, st = state.load_ticket(target, ticket_id), state.load_state(target, ticket_id)
            except (OSError, ValueError):
                continue
            run = st.get("run") or {}
            archived = st.get("archived") or {}
            row = {"target": target.name, "id": ticket_id, "title": t.get("title") or "", "pr": st.get("pr")}
            if (archived.get("pr_state") or st.get("pr_state")) == "MERGED" and within(archived.get("at") or st.get("updated_at")):
                merged.append(row)
            if st.get("status") == "done" and run.get("outcome") == "done" and within(run.get("finished_at")):
                done.append(row)
            if st.get("status") == "failed" and within(run.get("finished_at") or st.get("updated_at")):
                failed.append({**row, "error": (st.get("error") or "").split("\n")[0][:160]})
    stats = dashboard.stats(days=days + 1)
    daily = [d for d in stats["daily"] if start.isoformat() <= d["day"] <= end.isoformat()]
    delivery = dashboard.delivery(days=days + 1)["overall"]
    board = attention.board(targets)
    mem = []
    for target in targets:
        try:
            pending, prune = len(memory.pending(target)), len(memory.prune_candidates(target))
        except (OSError, ValueError, LookupError):
            continue
        if pending or prune:
            mem.append({"target": target.name, "pending": pending, "prune": prune})
    return {"week": week_label(end), "from": start.isoformat(), "to": end.isoformat(), "generated_at": state.now(),
            "done": done, "merged": merged, "failed": failed,
            "cost_usd": round(sum(d["cost"] for d in daily), 2), "sessions": sum(d["sessions"] for d in daily),
            "daily": daily, "delivery": delivery,
            "waiting_on_you": len(board.get("waiting_on_you") or []),
            "ready_for_review": len(board.get("ready_for_review") or []), "memory": mem}


# The report's words in each of config.LANGUAGES ({…} are filled in by markdown()).
TEXT = {
    "tr": {"title": "Haftalık rapor", "more": "- … ve {n} tane daha", "none": "- yok",
           "counts": "- Biten: **{done}** · birleşen PR: **{merged}** · başarısız: **{failed}**",
           "cost": "- Maliyet: **${cost:.2f}** ({sessions} oturum)", "clean": "- Teslim: istek olmadan birleşen %{share}",
           "rounds": " · PR başına tur {rounds}", "no_merge": "- Teslim: bu hafta birleşen PR yok",
           "waiting": "- Şu an seni bekleyen: **{waiting}** · review'a hazır: **{ready}**",
           "merged": "## Birleşenler", "done": "## Bitenler", "failed": "## Başarısızlar", "memory": "## Hafıza",
           "memory_line": "- {target}: {pending} öneri onay bekliyor, {prune} kayıt budanabilir",
           "notify_title": "Haftalık rapor", "notify": "{week}: {merged} PR birleşti, {done} iş bitti, ${cost:.2f}"},
    "en": {"title": "Weekly report", "more": "- … and {n} more", "none": "- none",
           "counts": "- Done: **{done}** · merged PRs: **{merged}** · failed: **{failed}**",
           "cost": "- Cost: **${cost:.2f}** ({sessions} sessions)", "clean": "- Delivery: merged without a change request {share}%",
           "rounds": " · rounds per PR {rounds}", "no_merge": "- Delivery: no PR merged this week",
           "waiting": "- Waiting on you now: **{waiting}** · ready for review: **{ready}**",
           "merged": "## Merged", "done": "## Done", "failed": "## Failed", "memory": "## Memory",
           "memory_line": "- {target}: {pending} proposals to approve, {prune} entries to prune",
           "notify_title": "Weekly report", "notify": "{week}: {merged} PRs merged, {done} tickets done, ${cost:.2f}"},
}


def _list(rows: list[dict], line, w: dict) -> list[str]:
    out = [line(r) for r in rows[:LISTED]]
    if len(rows) > LISTED:
        out.append(w["more"].format(n=len(rows) - LISTED))
    return out or [w["none"]]


def markdown(r: dict, language: str = "tr") -> str:
    w = TEXT.get(language, TEXT["en"])
    d = r["delivery"]
    delivery = w["no_merge"]
    if d.get("merged"):
        delivery = w["clean"].format(share=round(d["clean_share"] * 100) if d.get("clean_share") is not None else "-")
        delivery += w["rounds"].format(rounds=d["rounds_per_pr"]) if d.get("rounds_per_pr") is not None else ""
    lines = [f"# {w['title']} · {r['week']}", "", f"{r['from']} – {r['to']}", "",
             w["counts"].format(done=len(r["done"]), merged=len(r["merged"]), failed=len(r["failed"])),
             w["cost"].format(cost=r["cost_usd"], sessions=r["sessions"]), delivery,
             w["waiting"].format(waiting=r["waiting_on_you"], ready=r["ready_for_review"]), "",
             w["merged"], *_list(r["merged"], lambda x: f"- `{x['id']}` {x['title']} ({x['target']})" + (f" · {x['pr']}" if x["pr"] else ""), w),
             "", w["done"], *_list(r["done"], lambda x: f"- `{x['id']}` {x['title']} ({x['target']})", w),
             "", w["failed"], *_list(r["failed"], lambda x: f"- `{x['id']}` {x['title']} ({x['target']}): {x['error']}", w)]
    if r["memory"]:
        lines += ["", w["memory"], *[w["memory_line"].format(**m) for m in r["memory"]]]
    return "\n".join(lines) + "\n"


def save(r: dict, language: str | None = None) -> Path:
    """`<week>.md` in `language` (the notifications' by default) and `<week>.json`, the data any language renders from."""
    report_dir().mkdir(parents=True, exist_ok=True)
    path = report_dir() / f"{r['week']}.md"
    path.write_text(markdown(r, language or notify.language()), encoding="utf-8")
    state.write_json(report_dir() / f"{r['week']}.json", r)
    return path


def latest(language: str | None = None) -> dict | None:
    """The newest saved report: {week, path, markdown}, in `language` when its data was saved (else as written), or
    None."""
    found = sorted(report_dir().glob("*-W*.md")) if report_dir().is_dir() else []
    if not found:
        return None
    path = found[-1]
    data = path.with_suffix(".json")
    if language and data.is_file():
        try:
            return {"week": path.stem, "path": str(path), "markdown": markdown(state.read_json(data), language)}
        except (OSError, ValueError, KeyError):
            pass
    return {"week": path.stem, "path": str(path), "markdown": path.read_text(encoding="utf-8")}


def tick(targets: list[Target], at: datetime | None = None) -> Path | None:
    """From Monday 09:00 on, once per ISO week: write the report for the week before and notify (kind "report",
    so a digest collects it). Returns the file it wrote, else None."""
    at = at or now()
    if at.weekday() == DUE_WEEKDAY and at.hour < DUE_HOUR:
        return None
    week = week_label(at.date() - timedelta(days=at.weekday() + 1))  # the last Sunday's week
    if (report_dir() / f"{week}.md").exists():
        return None
    r = report(targets, today=at.date() - timedelta(days=at.weekday()))  # the Monday that ends it
    path = save(r)
    if notify.wants("report"):
        w = TEXT.get(notify.language(), TEXT["en"])
        notify.send(w["notify_title"], w["notify"].format(week=week, merged=len(r["merged"]), done=len(r["done"]), cost=r["cost_usd"]),
                    key="weekly-report", kind="report")
    return path
