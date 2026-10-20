"""The retro: when a ticket reaches done, one cheap read-only `claude -p` session reads what went wrong on it and
proposes project memory for the target's agents: rules, decisions and pitfalls (memory.py keeps them as candidates
until the user decides).

Evidence (`evidence`): the user's sent review-page comments, findings the AI review dismissed that the user sent
to fix anyway, findings two or more review rounds listed, question-round answers that went against the agents'
recommendation, changes of course (notes at the gate or after a failure), the PR feedback the user sent to the
agent (feedback.py's items with a `sent_round`), engine checks that failed and passed later (with the commits and
files in between), runs that failed before a later one got the ticket done (the error, the user's notes and what
changed), and intent-gap halts with the user's answer. Each item has a stable `ref` (`U1-2`, `R1-7`, `Q2-q1`, `N1`,
`GH-<feedback id>` such as `GH-rc-991`, `C-04-implement-tests`, `F1`, `I1`);
a retro reads only the items no earlier successful retro of the ticket read, and with none it does not run at
all. The engine starts it as a background job when a run ends done (`schedule`), so it never holds up publish;
`retro = false` in the target's config turns that off, and `ziggy retro <id>` runs it by hand.

The session goes through dispatch.run like a stage (the same settings and sandbox), with the target's
`retro_model`, a budget of at most BUDGET_USD, no editing tools, no shell and no subagents, in the ticket's
worktree (else the main checkout). It gets the target's whole active memory, so it does not propose what is there.
Its answer is the structured output (`results.json_schema("retro")`); each proposal must have the fields its kind
needs and name refs it was given, else it is dropped.

Storage, under runs/<id>/retro/ (not a phase dir, so the engine never sees it):
    NN-retro/      one session: run.json (dispatch's record, `evidence_refs`, `candidates`, `dropped`), prompt.md,
                   evidence.json, claude.jsonl, claude.err, result.json
    jobs/          the background jobs that ran it (apart from runs/<id>/jobs: a retro never blocks publish)
"""
import dataclasses
import json
import os
import re
from collections import Counter
from pathlib import Path

from . import changes, dispatch, feedback, memory, pipelines, render, results, state, worktree
from .config import Target
from .ticket import LANGUAGES

PROMPT = "retro.md"
BUDGET_USD = 1.0
TIMEOUT = 600
MAX_ITEMS = 60
MAX_TEXT = 1500
# Read only and cheap: no editing tools, no shell (the sandbox would let Bash write the working copy), no subagents.
TOOLS_DENY = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task")
AGREE = {"ok", "okay", "yes", "evet", "tamam", "kabul", "agree", "agreed", "önerilen", "recommended", "+1", "👍"}


class RetroError(Exception):
    pass


def root(target: Target, ticket_id: str) -> Path:
    return state.run_dir(target, ticket_id) / "retro"


def jobs_dir(target: Target, ticket_id: str) -> Path:
    return root(target, ticket_id) / "jobs"


def session_dirs(target: Target, ticket_id: str) -> list[Path]:
    """The ticket's retro sessions (`NN-retro`), oldest first."""
    base = root(target, ticket_id)
    if not base.is_dir():
        return []
    found = [(int(m.group(1)), p) for p in base.iterdir() if p.is_dir() and (m := state.PHASE_DIR.match(p.name))]
    return [p for _, p in sorted(found)]


def _new_session_dir(target: Target, ticket_id: str) -> Path:
    count = max((int(state.PHASE_DIR.match(p.name).group(1)) for p in session_dirs(target, ticket_id)), default=0)
    path = root(target, ticket_id) / f"{count + 1:02d}-retro"
    path.mkdir(parents=True)
    return path


def _record(d: Path) -> dict:
    try:
        data = state.read_json(d / "run.json")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- evidence -------------------------------------------------------------------------------------

def _clip(text, limit: int = MAX_TEXT) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _simple(text: str) -> str:
    return " ".join(str(text).lower().split()).strip(" .!,;:")


def answers_by_question(answer: str, qids: list[str]) -> dict[str, str]:
    """Each question's part of a round's answer: the panel sends `<qid>: <text>` blocks; a round with one question
    takes the whole answer when it has no such block."""
    lookup = {q.lower(): q for q in qids}
    if not lookup:
        return {}
    pattern = re.compile(r"^\s*(" + "|".join(re.escape(q) for q in lookup) + r")\s*[:)]\s*", re.M | re.I)
    matches = list(pattern.finditer(answer))
    found = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(answer)
        found[lookup[m.group(1).lower()]] = answer[m.end():end].strip()
    if not found and len(qids) == 1:
        found[qids[0]] = answer.strip()
    return found


def against_recommendation(recommendation: str, answer: str) -> bool:
    """The user answered something other than what the agents recommended (an "ok" or the recommendation's own
    text, or its option letter, is agreement)."""
    rec, ans = _simple(recommendation), _simple(answer)
    if not rec or not ans or ans in AGREE:
        return False
    if rec in ans or ans in rec:
        return False
    letter = re.match(r"^([a-z0-9])[).:]\s", recommendation.strip().lower())
    return not (letter and ans in (letter.group(1), f"{letter.group(1)})"))


def _comment_items(reviews: list[tuple[str, dict]], comments: list[dict], overruled_ids: set) -> list[dict]:
    """The user's sent review-page comments with text (a reply that only overrules a dismissal is that item)."""
    out = []
    by_round: dict[int, list[dict]] = {}
    for c in comments:
        if isinstance(c, dict) and isinstance(c.get("round"), int):
            by_round.setdefault(c["round"], []).append(c)
    for round_, items in sorted(by_round.items()):
        for n, c in enumerate(sorted(items, key=lambda c: c.get("created_at") or ""), 1):
            body = str(c.get("body") or "").strip()
            if not body or c.get("id") in overruled_ids:
                continue
            item = {"ref": f"U{round_}-{n}", "kind": "user_comment", "text": _clip(body)}
            if c.get("path"):
                item["where"] = f"{c['path']}:{c['line']}" if c.get("line") else c["path"]
            if c.get("line_text"):
                item["line"] = _clip(c["line_text"], 300)
            if c.get("finding_id"):
                ref = changes._finding_ref(reviews, c.get("finding_stage"), c["finding_id"])
                item["reply_to_finding"] = {"action": c.get("finding_action"), "summary": _clip((ref or {}).get("summary"), 500)}
            out.append(item)
    return out


def _round_items(st: dict) -> list[dict]:
    out = []
    for r in st.get("rounds") or []:
        if not isinstance(r, dict) or not isinstance(r.get("answer"), str):
            continue
        questions = [q for q in r.get("questions") or [] if isinstance(q, dict) and q.get("id")]
        parts = answers_by_question(r["answer"], [str(q["id"]) for q in questions])
        for q in questions:
            rec, ans = str(q.get("recommendation") or ""), parts.get(str(q["id"]), "")
            if against_recommendation(rec, ans):
                out.append({"ref": f"Q{r.get('n')}-{q['id']}", "kind": "answer_against_recommendation",
                            "stage": r.get("stage"), "question": _clip(q.get("text"), 800),
                            "recommendation": _clip(rec, 500), "text": _clip(ans)})
    for i, note in enumerate(st.get("answers") or [], 1):
        if isinstance(note, dict) and str(note.get("text") or "").strip():
            out.append({"ref": f"N{i}", "kind": "plan_note", "text": _clip(note["text"])})
    return out


def _github_items(target: Target, ticket_id: str) -> list[dict]:
    """The PR feedback the user sent to the agent (feedback.py's items with a `sent_round`: comments, reviews and
    failing checks they judged worth fixing); none without a feedback.json. Items never sent are not evidence."""
    out = []
    for f in feedback.load(target, ticket_id)["items"]:
        if f.get("sent_round") is None or not feedback.ITEM_ID.match(str(f["id"])) or not str(f.get("body") or "").strip():
            continue
        item = {"ref": f"GH-{f['id']}", "kind": "github", "source": f.get("kind"), "author": f.get("author"),
                "text": _clip(f["body"]), "url": f.get("url") if isinstance(f.get("url"), str) else None,
                "github_round": f["sent_round"], "fixed": bool(f.get("fixed_sha"))}
        if f.get("path"):
            item["where"] = f"{f['path']}:{f['line']}" if f.get("line") else f["path"]
        if f.get("state"):
            item["review_state"] = f["state"]
        out.append(item)
    return out


def _phase_records(target: Target, ticket_id: str) -> list[tuple[Path, dict]]:
    return [(d, r) for d in state.phase_dirs(target, ticket_id) if (r := _record(d))]


def _between(wt: Path | None, before: str | None, after: str | None) -> dict:
    """The commit subjects and files from `before` to `after` in the worktree ({} when unknown)."""
    if not wt or not before or not after or before == after:
        return {}
    try:
        log = worktree.git("log", "--reverse", "--format=%s", f"{before}..{after}", cwd=wt, timeout=30).stdout
        files = worktree.git("diff", "--name-only", before, after, cwd=wt, timeout=30).stdout
    except (worktree.GitError, OSError, ValueError):
        return {}
    out = {"commits": [_clip(x, 200) for x in log.splitlines() if x][:10], "files": [f for f in files.splitlines() if f][:20]}
    return {k: v for k, v in out.items() if v}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:30] or "check"


def _check_items(records: list[tuple[Path, dict]], wt: Path | None) -> list[dict]:
    """Engine checks that failed in one session and passed in a later one, with what changed in between."""
    out = []
    for i, (d, r) in enumerate(records):
        for c in r.get("checks") or []:
            if not isinstance(c, dict) or c.get("status") in ("pass", None) or not c.get("name"):
                continue
            later = next(((d2, r2) for d2, r2 in records[i + 1:] if any(
                isinstance(x, dict) and x.get("name") == c["name"] and x.get("status") == "pass" for x in r2.get("checks") or [])), None)
            if later is None:
                continue
            out.append({"ref": f"C-{d.name}-{_slug(c['name'])}", "kind": "check_fixed",
                        "text": _clip(f"{c['name']} {c.get('status')}: {c.get('detail') or ''}", 800),
                        "failed_in": d.name, "passed_in": later[0].name,
                        "changes": _between(wt, r.get("head_after"), later[1].get("head_after"))})
    return out


def _run_items(st: dict, records: list[tuple[Path, dict]], wt: Path | None) -> list[dict]:
    """Runs that failed before a later run got the ticket done: the failure, the user's notes in between, and the
    commits and files that changed."""
    if st.get("status") != "done":
        return []
    by_run: dict[int, list[dict]] = {}
    for _, r in records:
        if isinstance(r.get("run"), int):
            by_run.setdefault(r["run"], []).append(r)
    runs = sorted(by_run)
    out = []
    for n in runs[:-1]:
        last = by_run[n][-1]
        failed = last.get("state") != "ok" or last.get("verdict") == "FAILED" or last.get("result_error")
        if not failed:
            continue
        nxt = by_run[runs[runs.index(n) + 1]]
        ended, started = last.get("finished_at") or "", nxt[0].get("started_at") or ""
        notes = [_clip(a.get("text"), 500) for a in st.get("answers") or [] if isinstance(a, dict)
                 and ended <= str(a.get("at") or "") <= (started or "~")]
        error = last.get("result_error") or (last.get("result") or {}).get("summary") or last.get("state")
        out.append({"ref": f"F{n}", "kind": "run_recovered", "stage": last.get("stage"),
                    "text": _clip(f"{last.get('stage')}: {error}", 800), "notes": notes or None,
                    "changes": _between(wt, last.get("head_after"), by_run[runs[-1]][-1].get("head_after"))})
    return out


def _intent_items(st: dict) -> list[dict]:
    out = []
    for r in st.get("rounds") or []:
        gap = r.get("intent_gap") if isinstance(r, dict) else None
        if isinstance(gap, dict) and gap.get("summary"):
            out.append({"ref": f"I{gap.get('attempt') or r.get('n')}", "kind": "intent_gap", "stage": gap.get("stage"),
                        "text": _clip(gap["summary"], 800), "evidence": _clip(gap.get("evidence"), 500),
                        "answer": _clip(r.get("answer"), 800)})
    return out


def evidence(target: Target, ticket_id: str) -> list[dict]:
    """Every piece of evidence the ticket has, each {ref, kind, text, …}; refs are stable as the ticket goes on."""
    st = state.load_state(target, ticket_id)
    reviews, comments = changes.review_history(target, ticket_id)
    overruled = changes.overruled_findings(reviews, comments)
    items = [{"ref": o["ref"], "kind": "overruled_finding", "text": _clip(o["summary"]), "location": o["location"],
              "review_reason": _clip(o["reason"], 800), "user_reply": _clip(o["comment"], 800)} for o in overruled]
    items += _comment_items(reviews, comments, {o["comment_id"] for o in overruled})
    taken = {o["ref"] for o in overruled}
    items += [{"ref": f["ref"], "kind": "recurring_finding", "text": _clip(f["summary"]), "location": f["location"],
               "rounds": f["rounds"], "dispositions": f["dispositions"]}
              for f in changes.recurring_findings(reviews) if f["ref"] not in taken]
    items += _round_items(st)
    items += _github_items(target, ticket_id)
    records = _phase_records(target, ticket_id)
    wt = Path(st["worktree"]) if st.get("worktree") and Path(st["worktree"]).is_dir() else None
    items += _check_items(records, wt)
    items += _run_items(st, records, wt)
    items += _intent_items(st)
    return [{k: v for k, v in item.items() if v not in (None, "", {}, [])} for item in items]


def seen_refs(target: Target, ticket_id: str) -> set[str]:
    """Refs a finished retro of the ticket has read (one that failed did not count)."""
    out = set()
    for d in session_dirs(target, ticket_id):
        r = _record(d)
        if r.get("state") == "ok" and r.get("verdict") == "READY":
            out |= {x for x in r.get("evidence_refs") or [] if isinstance(x, str)}
    return out


def new_evidence(target: Target, ticket_id: str) -> list[dict]:
    seen = seen_refs(target, ticket_id)
    return [item for item in evidence(target, ticket_id) if item["ref"] not in seen][:MAX_ITEMS]


# --- starting it ----------------------------------------------------------------------------------

def disabled() -> bool:
    """ZIGGY_RETRO=off: no background retro at all (the tests set it, so no stray sessions start)."""
    return os.environ.get("ZIGGY_RETRO", "").strip().lower() == "off"


def schedule(target: Target, ticket_id: str) -> dict | None:
    """Start the retro as a background job when the target allows it and the ticket has evidence no retro has
    read; returns the job, else None. Called by the engine once a run ends done."""
    if not target.retro or disabled() or not new_evidence(target, ticket_id):
        return None
    from .dashboard import spawn_job  # the panel's detached jobs; dashboard imports the engine
    return spawn_job(jobs_dir(target, ticket_id), ["--target", target.name, "retro", ticket_id])


# --- the session ----------------------------------------------------------------------------------

def _fence(text: str, info: str = "") -> str:
    """`text` in a code fence longer than any backtick run inside it, so quoted data cannot close it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def prompt(target: Target, ticket: dict, items: list[dict], cwd: Path) -> str:
    known = memory.full_text(target)
    ctx = {
        "project": {"name": target.name, "path": str(target.path)},
        "ticket": {"id": ticket["id"], "title": ticket["title"], "type": ticket["type"]},
        "cwd": str(cwd),
        "evidence": _fence(json.dumps(items, indent=2, ensure_ascii=False), "json"),
        "memory": {"exists": bool(known), "text": _fence(known, "markdown") if known else ""},
        "max_items": results.MAX_MEMORY,
        "language": LANGUAGES.get(ticket.get("language") or "") or "English",
    }
    return render.render(pipelines.prompt_text(PROMPT), ctx, pipelines.partial_text).rstrip() + "\n"


def _json_in(message: str | None) -> str | None:
    """A final message that is the JSON answer itself (fenced or not): the fallback when the CLI gave no
    structured output."""
    text = (message or "").strip()
    return text if text.startswith(("{", "```")) else None


_FIELDS = {"kural": (("Kural", "rule"), ("Neden", "why")), "karar": (("Karar", "decision"), ("Neden", "why")),
           "tuzak": (("Ne oluyor", "happens"), ("Ne yapılır", "todo"))}


def check_sources(proposed: list[dict], items: list[dict], ticket_id: str) -> tuple[list[dict], list[str]]:
    """Keep the proposals whose sources name evidence this session was given (each source gets the item's kind and
    summary for the panel) and that have the fields their kind needs, as memory candidates ({kind, title, fields,
    scope, sources}). Returns (kept, why each other one was dropped)."""
    by_ref = {item["ref"]: item for item in items}
    kept, dropped = [], []
    for p in proposed:
        sources = []
        for s in p["sources"]:
            item = by_ref.get(s["ref"])
            if item and s["ticket"] in ("", ticket_id) and s["ref"] not in {x["ref"] for x in sources}:
                sources.append({"ticket": ticket_id, "ref": s["ref"], "kind": item["kind"], "summary": _clip(item.get("text"), 300),
                                **({"url": item["url"]} if item.get("url") else {})})
        if not sources:
            dropped.append(f"{p['title'][:120]}: no source among the evidence given")
            continue
        fields = {name: p.get(key) or "" for name, key in _FIELDS[p["kind"]]}
        try:
            title, fields = memory.clean_proposal(p["kind"], p["title"], fields)
        except memory.MemoryRefused as exc:
            dropped.append(f"{p['title'][:120]}: {exc}")
            continue
        kept.append({"kind": p["kind"], "title": title, "fields": fields,
                     "scope": {"paths": p.get("paths") or [], "topics": p.get("topics") or []}, "sources": sources})
    return kept, dropped


def _budget(target: Target) -> float:
    return min(target.budget_usd, BUDGET_USD) if target.budget_usd else BUDGET_USD


def run(target: Target, ticket_id: str) -> dict:
    """One retro of the ticket (the job's body): the session on the evidence no retro read yet, then its
    proposals stored as memory candidates. Returns the session's record ({"state": "skipped"} when there is nothing new)."""
    ticket = state.load_ticket(target, ticket_id)
    root(target, ticket_id).mkdir(parents=True, exist_ok=True)
    added: list[dict] = []
    # Two retros of one ticket take turns; the second reads only what the first did not.
    with state.waiting_lock(target, f"retro-{ticket_id}", f"retro of {ticket_id}", timeout=TIMEOUT + 300, poll=1):
        items = new_evidence(target, ticket_id)
        if not items:
            return {"state": "skipped", "reason": "nothing new to learn from", "candidates": []}
        st = state.load_state(target, ticket_id)
        wt = Path(st["worktree"]) if st.get("worktree") and Path(st["worktree"]).is_dir() else target.path
        session = _new_session_dir(target, ticket_id)
        text = prompt(target, ticket, items, wt)
        (session / "prompt.md").write_text(text, encoding="utf-8")
        state.write_json(session / "evidence.json", items)
        result_file = session / "result.json"
        record = {"stage": "retro", "phase": "retro", "title": "Retro", "ticket": ticket_id,
                  "evidence_refs": [item["ref"] for item in items], "evidence_kinds": dict(Counter(i["kind"] for i in items))}
        env = {"PIPELINE_STAGE": "retro", "PIPELINE_TICKET": ticket_id, "PIPELINE_INBOX": str(session),
               "PIPELINE_RESULT_FILE": str(result_file), "GIT_OPTIONAL_LOCKS": "0"}
        dispatch.run(dataclasses.replace(target, budget_usd=_budget(target)), ticket_id, wt, session, session, target.retro_model,
                     text, record=record, timeout=TIMEOUT, tools_deny=TOOLS_DENY, json_schema=results.json_schema("retro"),
                     extra_env=env)
        file_text = result_file.read_text(encoding="utf-8") if result_file.is_file() else None
        if file_text is None and record.get("structured_output") is None:
            file_text = _json_in(record.get("final_message"))
        resolved = results.resolve("retro", file_text, record.get("structured_output"), record.get("final_message", ""))
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
        kept, dropped = check_sources((result or {}).get("memory") or [], items, ticket_id) if record["verdict"] == "READY" else ([], [])
        if record["state"] == "ok" and kept:
            added = memory.add_candidates(target, kept, "retro", ticket_id, retro=session.name)
        record.update(candidates=[c["id"] for c in added], dropped=dropped)
        state.write_json(session / "run.json", record)
    return record
