"""The lead: every morning, per target, what to work on next.

Collect (`collect`, no LLM, free): signals, each {key, kind, title, detail, url} with a key that stays the same while
the thing it names does:
    roadmap   `roadmap:<item>`            roadmap.next_items: the current phase's first items not started yet
    ci        `ci:<workflow>:<sha>`       red CI on the base branch (`gh api …/actions/runs?branch=<base>&per_page=5`):
                                          the latest completed run of a workflow failed; the sha is the first commit of
                                          the red streak among those runs
    issue     `issue:<n>`                 open issues assigned to the target's gh_user, or labelled `lead_issue_label`
    deferred  `deferred:<ticket>:<entry>` deferred review findings no ticket was made from (deferred.py)
GitHub is read as the target's gh_user and skipped without github_repo or gh_user. Memory entries, faded pitfalls
included, are context for the proposal, never work.

Propose (`run`): the signals that no pending or rejected proposal and no open ticket made from a proposal holds go to
one cheap read-only session (the target's `lead_model`, at most BUDGET_USD, no editing tools, no shell, no
subagents, in the main checkout) with the project memory recall and the open tickets; it answers 0–5 ticket drafts
(`results.json_schema("lead")`), and a draft naming no signal it was given is dropped. Without quota headroom (the
account is blocked or its 5-hour window is at HEADROOM or more), or when the session gives no usable answer, the top
signals become drafts as they are (`agentless`: "ajansız" in the panel).

Schedule (`tick`, the panel's attention loop): `[lead] at` (local time, default 08:30) and `enabled`; each target's
lead starts once per local day at or after that time (the first tick after a sleep counts), as a background
`ziggy lead run --now` job, recorded in `state_home()/lead/last_run.json`. `ziggy lead run --now` and the panel's
"Yenile" run it at once.

Proposals live in `<target state>/lead/proposals.json`:
    {next, proposals: [{id p<n>, title, type, body, why, sources [signal keys], signals [{key, kind, title, url}],
     size, agentless, session, status pending|accepted|rejected|expired, created_at, decided_at, reason, ticket}]}
A pending proposal older than EXPIRE_DAYS expires (its keys may be proposed again). A personal target's "Başlat"
creates the ticket (origin {kind: lead, proposal}) and runs it; a controlled target only proposes: the user opens the
ticket from the composer, which accepts the proposal the same way. Accepting links the proposal's roadmap items to
the ticket (roadmap.link), so they move with it: ticket → in-progress → review → pr → merged. Sessions:
`<target state>/lead/NN-lead/` (run.json, prompt.md, signals.json, claude.jsonl, result.json), stage `lead` in the
stats; jobs in `<target state>/lead/jobs/`.
"""
import dataclasses
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import config, deferred, dispatch, memory, notify, pipelines, publish, quota, render, results, roadmap, state, ticket
from .config import Target

PROMPT = "lead.md"
STAGE = "lead"
KIND = "lead"  # a ticket's origin kind, the inbox row's kind
BUDGET_USD = 0.5
TIMEOUT = 600
# Read only and cheap: no editing tools, no shell (the sandbox would let Bash write the checkout), no subagents.
TOOLS_DENY = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task")
FALLBACK_DRAFTS = 3  # drafts made straight from the top signals without a session
EXPIRE_DAYS = 7
HEADROOM = quota.WARN_AT  # a 5-hour window at this utilization or more leaves the session out
ROADMAP_ITEMS = 3
MAX_DEFERRED = 10
MAX_ISSUES = 20
MAX_SIGNALS = 40
MAX_DETAIL = 1500
KEEP_DECIDED = 200  # decided proposals kept for the record (and for their keys)
CI_FAILED = frozenset({"failure", "timed_out", "startup_failure"})
STATUSES = ("pending", "accepted", "rejected", "expired")
WHY = {"ci": "Ana dal kırmızı: başka işlerden önce yeşile dönmeli.",
       "roadmap": "Yol haritasının sıradaki maddesi.",
       "issue": "GitHub'da seni bekleyen açık issue.",
       "deferred": "Review'da ertelenmiş bir bulgu; hâlâ ticket'ı yok."}
KIND_TR = {"ci": "CI", "roadmap": "yol haritası", "issue": "issue", "deferred": "ertelenen bulgu"}


class LeadRefused(Exception):
    """An action the proposal's state or the target's mode does not allow; shown as is."""


def now() -> datetime:
    """The local clock every decision here reads (tests move it)."""
    return datetime.now().astimezone()


def _stamp() -> str:
    return now().astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_day(stamp) -> date | None:
    try:
        return datetime.fromisoformat(stamp).astimezone(now().tzinfo).date() if stamp else None
    except (TypeError, ValueError):
        return None


def disabled() -> bool:
    """ZIGGY_LEAD=off: the panel's tick never starts a lead (the tests set it, so no stray jobs start)."""
    return os.environ.get("ZIGGY_LEAD", "").strip().lower() == "off"


def root(target: Target) -> Path:
    return target.state_dir / "lead"


def jobs_dir(target: Target) -> Path:
    return root(target) / "jobs"


def _proposals_path(target: Target) -> Path:
    return root(target) / "proposals.json"


def _last_run_path() -> Path:
    return config.state_home() / "lead" / "last_run.json"


def session_dirs(target: Target) -> list[Path]:
    """The target's lead sessions (`NN-lead`), oldest first."""
    base = root(target)
    found = [(int(m.group(1)), p) for p in (base.iterdir() if base.is_dir() else [])
             if p.is_dir() and (m := state.PHASE_DIR.match(p.name)) and m.group(2) == STAGE]
    return [p for _, p in sorted(found)]


def _new_session_dir(target: Target) -> Path:
    count = max((int(state.PHASE_DIR.match(p.name).group(1)) for p in session_dirs(target)), default=0)
    path = root(target) / f"{count + 1:02d}-{STAGE}"
    path.mkdir(parents=True)
    return path


def _line(text, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(text, limit: int = MAX_DETAIL) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- signals ----------------------------------------------------------------------------------------------------

def _signal(key: str, kind: str, title: str, detail: str = "", url: str | None = None, **extra) -> dict:
    return {"key": key, "kind": kind, "title": _line(title), "detail": _clip(detail), "url": url, **extra}


def _roadmap(target: Target) -> tuple[list[dict], list[str]]:
    try:
        items = roadmap.next_items(target, n=ROADMAP_ITEMS)
    except (OSError, ValueError, KeyError, LookupError) as exc:
        return [], [f"yol haritası okunamadı: {exc}"]
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        phase = item.get("phase")
        detail = "\n".join(x for x in (str(item.get("detail") or "").strip(), f"Faz: {phase}" if phase else "") if x)
        out.append(_signal(f"roadmap:{item['id']}", "roadmap", item.get("title") or str(item["id"]), detail,
                           type="feat", item=str(item["id"]), phase=phase, name=_line(item.get("title") or item["id"])))
    return out, []


def _deferred(target: Target) -> list[dict]:
    out = []
    for ticket_id in reversed(state.ticket_ids(target)):  # the newest tickets' findings first
        try:
            found = deferred.entries(target, ticket_id)
        except (OSError, ValueError):
            continue
        out += [_signal(f"deferred:{ticket_id}:{e['key']}", "deferred", e["title"], e["body"], type=e["type"],
                        ticket=ticket_id, entry=e["key"], name=e["title"])
                for e in found if not e["ticket"]]
    return out[:MAX_DEFERRED]


def _gh_json(target: Target, endpoint: str):
    return json.loads(publish.gh_as(target.gh_user, "api", endpoint, cwd=target.path) or "null")


def _ci(target: Target) -> list[dict]:
    """Red CI on the base branch: per workflow among the last runs there, the latest completed one failed."""
    data = _gh_json(target, f"repos/{target.github_repo}/actions/runs?branch={quote(target.base_branch, safe='')}&per_page=5")
    runs = [r for r in (data or {}).get("workflow_runs") or [] if isinstance(r, dict)] if isinstance(data, dict) else []
    by_workflow: dict[str, list[dict]] = {}
    for r in runs:  # newest first, as the API lists them
        by_workflow.setdefault(str(r.get("workflow_id") or r.get("name") or "ci"), []).append(r)
    out = []
    for workflow, found in by_workflow.items():
        done = [r for r in found if r.get("status") == "completed"]
        if not done or done[0].get("conclusion") not in CI_FAILED:
            continue
        streak = []
        for r in done:
            if r.get("conclusion") not in CI_FAILED:
                break
            streak.append(r)
        latest, first = streak[0], streak[-1]
        name = str(latest.get("name") or "CI")
        sha = str(first.get("head_sha") or first.get("id") or "")[:12]
        detail = (f"{name}, {target.base_branch} dalında kırmızı: son {len(streak)} koşusu başarısız "
                  f"(en son {str(latest.get('head_sha') or '')[:7]}: {_line(latest.get('display_title'), 120)}).")
        out.append(_signal(f"ci:{workflow}:{sha}", "ci", f"{name} iş akışı {target.base_branch} dalında kırmızı", detail,
                           latest.get("html_url"), type="fix", name=f"{name} iş akışını {target.base_branch} dalında yeşile döndür"))
    return out


def _issues(target: Target) -> list[dict]:
    """Open issues (not PRs) assigned to the target's gh_user, and those labelled `lead_issue_label`."""
    repo = target.github_repo
    queries = [f"assignee={quote(target.gh_user, safe='')}"]
    if target.lead_issue_label:
        queries.append(f"labels={quote(target.lead_issue_label, safe='')}")
    found: dict[int, dict] = {}
    for q in queries:
        page = _gh_json(target, f"repos/{repo}/issues?state=open&per_page={MAX_ISSUES}&{q}")
        for issue in page if isinstance(page, list) else []:
            if isinstance(issue, dict) and isinstance(issue.get("number"), int) and not issue.get("pull_request"):
                found.setdefault(issue["number"], issue)
    out = []
    for number, issue in list(found.items())[:MAX_ISSUES]:
        labels = [str(lb.get("name")) for lb in issue.get("labels") or [] if isinstance(lb, dict) and lb.get("name")]
        kind = "fix" if any(re.search(r"bug|hata", lb, re.I) for lb in labels) else "feat"
        out.append(_signal(f"issue:{number}", "issue", f"#{number} {issue.get('title') or ''}", issue.get("body") or "",
                           issue.get("html_url"), type=kind, labels=labels, name=_line(issue.get("title") or f"Issue #{number}")))
    return out


def collect(target: Target) -> tuple[list[dict], list[str]]:
    """Every signal of the target in priority order (red CI, the roadmap, issues, deferred findings), each key once,
    and what could not be read."""
    warnings: list[str] = []
    road, more = _roadmap(target)
    warnings += more

    def github(name: str, read) -> list[dict]:
        try:
            return read(target)
        except (publish.PublishError, ValueError, OSError) as exc:
            warnings.append(f"{name} okunamadı: {exc}")
            return []

    ci, issues = [], []
    if target.github_repo and target.gh_user:
        ci, issues = github("CI", _ci), github("issue'lar", _issues)
    elif target.github_repo:
        warnings.append("gh_user yok: CI ve issue'lar okunmadı")
    seen, out = set(), []
    for s in ci + road + issues + _deferred(target):
        if s["key"] not in seen:
            seen.add(s["key"])
            out.append(s)
    return out[:MAX_SIGNALS], warnings


# --- proposals --------------------------------------------------------------------------------------------------

def _lock(target: Target):
    return state.waiting_lock(target, "lead", f"lead proposals of {target.name}", timeout=30, poll=0.05)


def load(target: Target) -> dict:
    try:
        data = state.read_json(_proposals_path(target))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("proposals"), list):
        data["proposals"] = []
    if not isinstance(data.get("next"), int):
        data["next"] = 1 + max((int(p["id"][1:]) for p in data["proposals"] if re.fullmatch(r"p\d+", str(p.get("id")))), default=0)
    return data


def _save(target: Target, data: dict) -> None:
    decided = [p for p in data["proposals"] if p.get("status") != "pending"]
    drop = {id(p) for p in decided[:-KEEP_DECIDED]} if len(decided) > KEEP_DECIDED else set()
    data["proposals"] = [p for p in data["proposals"] if id(p) not in drop]
    root(target).mkdir(parents=True, exist_ok=True)
    state.write_json(_proposals_path(target), data)


def _expire(data: dict) -> bool:
    """Pending proposals older than EXPIRE_DAYS expire; True when one did."""
    cutoff = now() - timedelta(days=EXPIRE_DAYS)
    changed = False
    for p in data["proposals"]:
        if p.get("status") != "pending":
            continue
        try:
            made = datetime.fromisoformat(p.get("created_at"))
        except (TypeError, ValueError):
            continue
        if made < cutoff:
            p.update(status="expired", decided_at=_stamp())
            changed = True
    return changed


def proposals(target: Target) -> list[dict]:
    """The target's proposals, oldest first, with the expired ones marked (and saved)."""
    data = load(target)
    if _expire(data):  # only looked at here; saved under the lock below
        with _lock(target):
            data = load(target)
            if _expire(data):
                _save(target, data)
    return data["proposals"]


def pending(target: Target) -> list[dict]:
    return [p for p in proposals(target) if p.get("status") == "pending"]


def _open_ticket(target: Target, ticket_id: str | None) -> bool:
    """A ticket that is still being worked on: it exists, is not archived and its PR is neither merged nor closed."""
    if not ticket_id:
        return False
    try:
        st = state.load_state(target, ticket_id)
    except (OSError, ValueError):
        return False
    return not st.get("archived") and st.get("pr_state") not in ("MERGED", "CLOSED")


def held_keys(target: Target, data: dict) -> set[str]:
    """Signal keys that are not proposed again: those of pending and rejected proposals, and of accepted ones whose
    ticket is still open."""
    out: set[str] = set()
    for p in data["proposals"]:
        status = p.get("status")
        if status in ("pending", "rejected") or (status == "accepted" and _open_ticket(target, p.get("ticket"))):
            out |= {k for k in p.get("sources") or [] if isinstance(k, str)}
    return out


def _find(data: dict, proposal_id: str) -> dict:
    found = next((p for p in data["proposals"] if p.get("id") == proposal_id), None)
    if found is None:
        raise LookupError(f"no lead proposal {proposal_id}")
    return found


def _pending_one(data: dict, proposal_id: str) -> dict:
    p = _find(data, proposal_id)
    if p.get("status") != "pending":
        raise LeadRefused(f"{proposal_id} is already {p.get('status')}" + (f" ({p['ticket']})" if p.get("ticket") else ""))
    return p


def _add(target: Target, drafts: list[dict], signals: list[dict], *, agentless: bool, session: str | None) -> list[dict]:
    by_key = {s["key"]: s for s in signals}
    with _lock(target):
        data = load(target)
        _expire(data)
        held = held_keys(target, data)
        added = []
        for d in drafts:
            sources = [k for k in d["sources"] if k in by_key and k not in held]
            if not sources:
                continue
            held |= set(sources)
            p = {"id": f"p{data['next']}", "title": _line(d["title"], 160), "type": d["type"], "body": _clip(d.get("body"), 8000),
                 "why": _line(d.get("why"), 400), "sources": sources,
                 "signals": [{"key": k, "kind": by_key[k]["kind"], "title": by_key[k]["title"], "url": by_key[k].get("url")} for k in sources],
                 "size": d.get("size"), "agentless": agentless, "session": session, "status": "pending", "created_at": _stamp(),
                 "decided_at": None, "reason": None, "ticket": None}
            data["next"] += 1
            data["proposals"].append(p)
            added.append(p)
        _save(target, data)
    return added


def _changed(target: Target) -> None:
    """The inbox's lead row follows what is pending (best effort)."""
    try:
        from . import attention
        attention.lead_changed(target)
    except Exception as exc:  # noqa: BLE001 — the inbox must never fail a decision
        print(f"lead: {exc}", file=sys.stderr)


def ticket_body(p: dict) -> str:
    """The text a ticket made from the proposal starts with: the draft, why now, and the signals it came from."""
    lines = [p.get("body") or "", "", "## Neden şimdi", "", p.get("why") or "-", "", "## Kaynaklar", ""]
    for s in p.get("signals") or []:
        lines.append(f"- {KIND_TR.get(s['kind'], s['kind'])}: {s['title']}" + (f" ({s['url']})" if s.get("url") else "") + f" · `{s['key']}`")
    lines += ["", f"Ziggy'nin iş önerisi {p['id']}" + (" (ajansız: sinyallerden doğrudan)" if p.get("agentless") else "") + "."]
    return "\n".join(lines).strip() + "\n"


def reject(target: Target, proposal_id: str, reason: str | None = None) -> dict:
    """Reddet: the proposal and its keys are not proposed again."""
    with _lock(target):
        data = load(target)
        _expire(data)
        p = _pending_one(data, proposal_id)
        p.update(status="rejected", decided_at=_stamp(), reason=_line(reason, 500) or None)
        _save(target, data)
    _changed(target)
    return p


def origin(target: Target, body) -> dict:
    """A new ticket's origin from the composer ({kind: lead, proposal}): the proposal must be pending."""
    if not isinstance(body, dict) or body.get("kind") != KIND:
        raise ValueError("origin must be {kind: lead, proposal}")
    proposal_id = str(body.get("proposal") or "")
    data = load(target)
    _expire(data)
    _pending_one(data, proposal_id)
    return {"kind": KIND, "proposal": proposal_id}


def accepted(target: Target, proposal_id: str, ticket_id: str) -> dict | None:
    """A ticket was made from the proposal: record it, and link its roadmap items to the ticket (roadmap.link), whose
    life then moves them (roadmap.ticket_changed)."""
    with _lock(target):
        data = load(target)
        try:
            p = _find(data, proposal_id)
        except LookupError:
            return None
        p.update(status="accepted", ticket=ticket_id, decided_at=_stamp())
        _save(target, data)
    for item in [k.split(":", 1)[1] for k in p.get("sources") or [] if k.startswith("roadmap:")]:
        try:
            roadmap.link(target, item, ticket_id)
        except (OSError, ValueError, KeyError, LookupError) as exc:  # the item left the roadmap since: the ticket stands
            print(f"lead: roadmap link of {item}: {exc}", file=sys.stderr)
    _changed(target)
    return p


def start(target: Target, proposal_id: str, language: str | None = None, *, run: bool = True) -> dict:
    """The ticket made from the proposal. With `run` (Başlat: the caller runs it) only in a personal target: a
    controlled target only proposes, and its user opens the ticket (the composer, or `ziggy lead start --no-run`)."""
    if run and target.mode == "controlled":
        raise LeadRefused(f"{target.name} kontrollü: öneriler yalnız öneri; ticket'ı 'Ticket olarak aç' ile sen oluştur")
    data = load(target)
    _expire(data)
    p = _pending_one(data, proposal_id)
    t = ticket.create(target, p["title"], p["type"], ticket_body(p), False, "low", language=language,
                      origin={"kind": KIND, "proposal": proposal_id}, pipeline=ticket.default_choice(target, p["type"]))
    accepted(target, proposal_id, t["id"])
    return t


# --- the session ------------------------------------------------------------------------------------------------

def _fence(text: str, info: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def open_tickets(target: Target) -> list[dict]:
    out = []
    for ticket_id in state.ticket_ids(target):
        if not _open_ticket(target, ticket_id):
            continue
        try:
            t, st = state.load_ticket(target, ticket_id), state.load_state(target, ticket_id)
        except (OSError, ValueError):
            continue
        out.append({"id": ticket_id, "title": t.get("title"), "type": t.get("type"), "status": st.get("status"),
                    **({"pr": st["pr"]} if st.get("pr") else {})})
    return out


def prompt(target: Target, signals: list[dict]) -> str:
    recall = memory.recall(target, {"title": " ".join(s["title"] for s in signals), "body": ""}, None, [])
    shown = [{k: v for k, v in s.items() if k in ("key", "kind", "title", "detail", "url", "phase", "labels")} for s in signals]
    ctx = {
        "project": {"name": target.name, "path": str(target.path), "base": target.base_branch, "mode": target.mode},
        "signals": _fence(json.dumps(shown, indent=2, ensure_ascii=False), "json"),
        "memory": {"exists": recall["exists"], "text": recall["text"]},
        "tickets": _fence(json.dumps(open_tickets(target), indent=2, ensure_ascii=False), "json"),
        "max_drafts": results.MAX_LEAD,
        "types": ", ".join(results.LEAD_TYPES),
    }
    return render.render(pipelines.prompt_text(PROMPT), ctx, pipelines.partial_text).rstrip() + "\n"


def check_drafts(drafts: list[dict], signals: list[dict]) -> tuple[list[dict], list[str]]:
    """The drafts whose sources name signals this session was given (a signal backs one draft at most), and why each
    other one was dropped."""
    keys = {s["key"] for s in signals}
    kept, dropped, used = [], [], set()
    for d in drafts:
        sources = [k for k in d["sources"] if k in keys and k not in used]
        if not sources:
            dropped.append(f"{d['title'][:120]}: no signal among those given")
            continue
        if d["type"] not in ticket.TYPES:
            dropped.append(f"{d['title'][:120]}: type {d['type']}")
            continue
        used |= set(sources)
        kept.append({**d, "sources": sources})
    return kept, dropped


def fallback(signals: list[dict], count: int = FALLBACK_DRAFTS) -> list[dict]:
    """Drafts straight from the top signals, without an agent ("ajansız")."""
    out = []
    for s in signals[:count]:
        body = "\n\n".join(x for x in (s.get("detail") or "", f"Kaynak: {s['url']}" if s.get("url") else "") if x)
        why = WHY.get(s["kind"], "")
        if s["kind"] == "roadmap" and s.get("phase"):
            why = f"Yol haritasının sıradaki maddesi ({s['phase']})."
        elif s["kind"] == "deferred":
            why = f"{s.get('ticket')} review'unda ertelenmiş bulgu; hâlâ ticket'ı yok."
        out.append({"title": s.get("name") or s["title"], "type": s.get("type") if s.get("type") in ticket.TYPES else "feat",
                    "body": body or s["title"], "why": why, "sources": [s["key"]], "size": None})
    return out


def headroom(target: Target) -> tuple[bool, str | None]:
    """Whether the target's Claude login can take the session: not blocked, and its 5-hour window under HEADROOM
    (unknown counts as room). Returns (ok, why not)."""
    key = quota.account_key(target)
    data = quota.load()
    if quota.blocked(key, data):
        return False, "kota doldu"
    w = ((data["accounts"].get(key) or {}).get("windows") or {}).get("five_hour") or {}
    util, resets = w.get("utilization"), w.get("resets_at")
    if util is not None and not (resets and resets <= quota.now()) and util >= HEADROOM:
        return False, f"5 saatlik kota %{round(util * 100)}"
    return True, None


def _budget(target: Target) -> float:
    return min(target.budget_usd, BUDGET_USD) if target.budget_usd else BUDGET_USD


def _json_in(message: str | None) -> str | None:
    text = (message or "").strip()
    return text if text.startswith(("{", "```")) else None


def _session(target: Target, signals: list[dict]) -> tuple[list[dict] | None, dict]:
    """One lead session: (its drafts, or None when it gave no usable answer; its record)."""
    session = _new_session_dir(target)
    text = prompt(target, signals)
    (session / "prompt.md").write_text(text, encoding="utf-8")
    state.write_json(session / "signals.json", signals)
    result_file = session / "result.json"
    record = {"stage": STAGE, "phase": STAGE, "title": "Lead", "target": target.name,
              "signal_keys": [s["key"] for s in signals], "signal_kinds": dict(Counter(s["kind"] for s in signals))}
    env = {"PIPELINE_STAGE": STAGE, "PIPELINE_TICKET": STAGE, "PIPELINE_INBOX": str(session),
           "PIPELINE_RESULT_FILE": str(result_file), "GIT_OPTIONAL_LOCKS": "0"}
    dispatch.run(dataclasses.replace(target, budget_usd=_budget(target)), STAGE, target.path, session, session, target.lead_model,
                 text, record=record, timeout=TIMEOUT, tools_deny=TOOLS_DENY, json_schema=results.json_schema(STAGE), extra_env=env)
    file_text = result_file.read_text(encoding="utf-8") if result_file.is_file() else None
    if file_text is None and record.get("structured_output") is None:
        file_text = _json_in(record.get("final_message"))
    resolved = results.resolve(STAGE, file_text, record.get("structured_output"), record.get("final_message", ""))
    result = resolved["result"]
    record.update(result=result, result_source=resolved["source"], result_warnings=resolved["warnings"],
                  result_error=resolved["error"], verdict=(result or {}).get("verdict"))
    if record.get("stopped"):
        record.update(state="killed", kill_reason="stop")
    elif record.get("timed_out"):
        record["state"] = "timeout"
    elif result is not None:
        record["state"] = "ok"
    else:
        record["state"] = "lost" if record.get("parse_error") else "failed"
    drafts = None
    if record["state"] == "ok" and record["verdict"] == "READY":
        drafts, record["dropped"] = check_drafts(result.get("drafts") or [], signals)
    state.write_json(session / "run.json", record)
    return drafts, record


# --- one run ----------------------------------------------------------------------------------------------------

def run(target: Target) -> dict:
    """The lead of the target now: collect, then propose from the signals not held already (a session when the
    account has room, else ajansız). Returns {state, signals, fresh, proposals, agentless, why, warnings, session}."""
    with state.lock(target, "lead-run", f"lead of {target.name}"):
        signals, warnings = collect(target)
        with _lock(target):
            data = load(target)
            if _expire(data):
                _save(target, data)
            held = held_keys(target, data)
        fresh = [s for s in signals if s["key"] not in held]
        out = {"state": "ok", "signals": len(signals), "fresh": len(fresh), "proposals": [], "agentless": False,
               "why": None, "warnings": warnings, "session": None}
        if not fresh:
            out.update(state="skipped", why="yeni sinyal yok")
        else:
            ok, why = headroom(target)
            drafts, session = None, None
            if ok:
                drafts, record = _session(target, fresh)
                session = Path(record["dir"]).name
                if drafts is None:
                    why = f"oturum sonuç vermedi ({record.get('result_error') or record.get('state')})"
            if drafts is None:
                drafts = fallback(fresh)
                out.update(agentless=True, why=why)
            out["session"] = session
            out["proposals"] = [p["id"] for p in _add(target, drafts, fresh, agentless=out["agentless"], session=session)]
        _changed(target)
    return out


# --- the schedule -----------------------------------------------------------------------------------------------

def last_runs() -> dict:
    try:
        data = state.read_json(_last_run_path())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mark(target: Target, **fields) -> dict:
    """Merge fields into the target's entry of last_run.json (machine-wide lock)."""
    with notify.state_lock("lead", "lead schedule"):
        data = last_runs()
        entry = data.setdefault(target.name, {})
        entry.update(fields)
        _last_run_path().parent.mkdir(parents=True, exist_ok=True)
        state.write_json(_last_run_path(), data)
    return entry


def due(target: Target, cfg: config.LeadConfig, at: datetime | None = None) -> bool:
    """Today's lead has not started and its time has come (the first tick after a sleep is as good as any)."""
    if not cfg.enabled:
        return False
    at = at or now()
    if (at.hour, at.minute) < cfg.clock:
        return False
    return (last_runs().get(target.name) or {}).get("day") != at.date().isoformat()


def job_running(target: Target) -> bool:
    from .dashboard import read_jobs  # the panel's detached jobs; dashboard imports the engine
    return any(j["state"] == "running" for j in read_jobs(jobs_dir(target))) or bool(state.holder(target, "lead-run"))


def start_job(target: Target) -> dict:
    from .dashboard import spawn_job
    return spawn_job(jobs_dir(target), ["--target", target.name, "lead", "run", "--now"])


def tick(targets) -> list[str]:
    """The panel's attention loop: start today's lead of every target whose time came, once per local day. Returns
    the names started."""
    if disabled():
        return []
    try:
        cfg = config.load_lead()
    except config.ConfigError as exc:
        print(f"lead: {exc}", file=sys.stderr)
        return []
    started = []
    for target in targets:
        if not due(target, cfg) or job_running(target):
            continue
        mark(target, day=now().date().isoformat(), started_at=_stamp(), by="schedule", state="started")
        start_job(target)
        started.append(target.name)
    return started


def run_now(target: Target, by: str = "manual") -> dict:
    """`ziggy lead run --now` (also the scheduled job): run and record it; a run at or after `[lead] at` counts as
    today's."""
    cfg = config.load_lead()
    at = now()
    fields = {"started_at": _stamp(), "by": by, "state": "running"}
    if (at.hour, at.minute) >= cfg.clock:
        fields["day"] = at.date().isoformat()
    mark(target, **fields)
    try:
        out = run(target)
    except Exception as exc:
        mark(target, state="failed", finished_at=_stamp(), error=str(exc))
        raise
    mark(target, state=out["state"], finished_at=_stamp(), error=None, signals=out["signals"], fresh=out["fresh"],
         proposals=len(out["proposals"]), agentless=out["agentless"], why=out["why"], warnings=out["warnings"])
    return out


# --- what the panel shows ---------------------------------------------------------------------------------------

def view(target: Target) -> dict:
    """Sıradaki işler for one target: its pending proposals (each with the ticket text it would start), the last run
    and whether one is under way."""
    found = proposals(target)
    rows = [{**p, "ticket_body": ticket_body(p)} for p in found if p.get("status") == "pending"]
    rows.sort(key=lambda p: int(p["id"][1:]))
    return {"target": target.name, "mode": target.mode, "pending": rows, "last_run": last_runs().get(target.name),
            "running": job_running(target)}


def yesterday(targets) -> list[dict]:
    """Bugün's 'dün biten': tickets whose run ended done, or whose PR merged, on the local day before today."""
    day = now().date() - timedelta(days=1)
    out = []
    for target in targets:
        for ticket_id in state.ticket_ids(target):
            try:
                t, st = state.load_ticket(target, ticket_id), state.load_state(target, ticket_id)
            except (OSError, ValueError):
                continue
            run_ = st.get("run") or {}
            archived = st.get("archived") or {}
            merged = (archived.get("pr_state") or st.get("pr_state")) == "MERGED"
            merged_at = (archived.get("at") or st.get("updated_at")) if merged else None
            done_at = run_.get("finished_at") if st.get("status") == "done" and run_.get("outcome") == "done" else None
            how, at = next(((h, a) for h, a in (("merged", merged_at), ("done", done_at)) if _local_day(a) == day), (None, None))
            if how:
                out.append({"target": target.name, "id": ticket_id, "title": t.get("title"), "how": how, "at": at,
                            "pr": st.get("pr"), "pr_state": st.get("pr_state") or archived.get("pr_state")})
    return sorted(out, key=lambda r: r["at"] or "")
