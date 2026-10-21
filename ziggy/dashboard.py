"""Web panel: every configured target's tickets, what needs you, live agent activity, and the
pipeline's commands behind buttons.

The panel runs nothing itself. It starts `ziggy` commands as detached jobs (their own
session, so they outlive a panel restart), reads the same state files the CLI writes, and calls
`dispatch.stop`, `ticket.create`, `changes`, `onboard` (detection; the AGENTS.md draft PR) and `rules` (approving a learned
rule) directly. It listens on 127.0.0.1 only,
and accepts a request only with a loopback Host header; a POST must be JSON and same-origin
(Sec-Fetch-Site), which is what keeps other web pages from driving it. It never marks a PR ready,
approves or merges: those stay on GitHub with the human.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import (attention, bmad, budget, issues, changes, chat, cleanup, config, deferred, dispatch, feedback, host, lead, memory, new_project, notify,
               onboard, onboard_agent, onboard_verify, pipeline, progress, project, protected, publish, pulls, quota, retro, review, roadmap,
               sessions,
               guard, stack, state, talk, ticket, weekly)
from .config import ConfigError, Target
from .worktree import GitError

WEB = Path(__file__).resolve().parent / "web"
STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/md.js": ("md.js", "text/javascript"),
    "/review.js": ("review.js", "text/javascript"),
    "/sessions.js": ("sessions.js", "text/javascript"),
    "/chat.js": ("chat.js", "text/javascript"),
    "/i18n.js": ("i18n.js", "text/javascript"),
    "/i18n/index.en.js": ("i18n/index.en.js", "text/javascript"),
    "/i18n/app.en.js": ("i18n/app.en.js", "text/javascript"),
    "/i18n/review.en.js": ("i18n/review.en.js", "text/javascript"),
    "/i18n/chat.en.js": ("i18n/chat.en.js", "text/javascript"),
    "/i18n/sessions.en.js": ("i18n/sessions.en.js", "text/javascript"),
    "/i18n/server.en.js": ("i18n/server.en.js", "text/javascript"),
    "/vendor/prism.js": ("vendor/prism.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
    "/review.css": ("review.css", "text/css"),
    "/fonts/bricolage-grotesque-latin.woff2": ("fonts/bricolage-grotesque-latin.woff2", "font/woff2"),
    "/fonts/bricolage-grotesque-latin-ext.woff2": ("fonts/bricolage-grotesque-latin-ext.woff2", "font/woff2"),
    "/fonts/instrument-sans-latin.woff2": ("fonts/instrument-sans-latin.woff2", "font/woff2"),
    "/fonts/instrument-sans-latin-ext.woff2": ("fonts/instrument-sans-latin-ext.woff2", "font/woff2"),
    "/fonts/jetbrains-mono-latin.woff2": ("fonts/jetbrains-mono-latin.woff2", "font/woff2"),
    "/fonts/jetbrains-mono-latin-ext.woff2": ("fonts/jetbrains-mono-latin-ext.woff2", "font/woff2"),
}
MAX_BODY = 256 * 1024  # a PR description edited in the panel (publish.MAX_BODY characters) fits, multi-byte text included
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
BLOB_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
# Evidence files (screenshots, recordings, logs the agents saved) open in their own tab: nothing in them runs.
EVIDENCE_CSP = "default-src 'none'; img-src data:; media-src 'self'; style-src 'unsafe-inline'; sandbox"
EVIDENCE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 ._+()-]{0,200}$")
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024
TICKET_ID = re.compile(r"^[a-z]+-\d+$")
PHASE = state.PHASE_DIR
REVIEW_ID = re.compile(r"^\d{8}-\d{6}-\d{6}$")
ATTEMPT = re.compile(r"^attempt-[1-9][0-9]{0,5}\.patch$")
SESSION_CONFIG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ACTIONS = ("run", "recheck", "answer", "approve", "split", "request_changes", "resume", "stop", "publish", "review", "post_review", "fix",
           "dismiss", "nudge", "cleanup", "chat", "chat_stop", "restack", "restack_push", "protected_apply", "protected_reject")
_ERRORS = (ConfigError, project.RouteError, pipeline.PipelineError, publish.PublishError, new_project.SetupError, review.ReviewError,
           cleanup.CleanupError, pulls.PullError, stack.StackError, GitError, state.LockError, onboard_agent.OnboardError, LookupError,
           ValueError)


class Refused(Exception):
    """An action the ticket's state does not allow; shown to the user as is."""


# --- reading state ----------------------------------------------------------------------------

def _tail(path: Path, limit: int = 2000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_record(phase_dir: Path) -> dict:
    try:
        return state.read_json(phase_dir / "run.json")
    except (OSError, ValueError):
        return {}


def _stage_of(phase_dir: Path, r: dict) -> str:
    return r.get("stage") or r.get("phase") or phase_dir.name.split("-", 1)[-1]


def _stage_state(phase_dir: Path, r: dict) -> str:
    """running | ok | failed | killed | timeout | lost, or a legacy (v1) result status."""
    if (phase_dir / dispatch.PID_FILE).exists():
        return "running"
    if r.get("state"):
        return r["state"]
    if r.get("stopped"):
        return "killed"
    if r.get("timed_out"):
        return "timeout"
    if "finished_at" not in r:
        return "lost"
    return (r.get("result") or {}).get("status") or "failed"  # a v1 session


def _artifact_views(result: dict | None, files_url: str) -> list[dict]:
    """The evidence a stage listed in its result (`artifacts`, kept relative to inbox/artifacts by the
    engine), each with the panel URL that serves it; strings from older results count as paths."""
    out = []
    for item in (result or {}).get("artifacts") or []:
        entry = {"path": item} if isinstance(item, str) else item if isinstance(item, dict) else {}
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip() or path.startswith("/") or ".." in path.split("/"):
            continue
        out.append({"path": path, "kind": entry.get("kind"), "caption": entry.get("caption"),
                    "url": files_url + "/".join(quote(seg, safe="") for seg in path.split("/")) if files_url else None})
    return out


def _stage_run(phase_dir: Path, files_url: str = "") -> dict:
    """One stage session as the Aşamalar tab shows it: verdict, route, result summary, checks, cost,
    how it was interrupted or retried, and the evidence it saved."""
    r = _run_record(phase_dir)
    result = r.get("result") if isinstance(r.get("result"), dict) and "verdict" in r.get("result", {}) else None
    return {
        "name": phase_dir.name,
        "stage": _stage_of(phase_dir, r),
        "title": r.get("title"),
        "run": r.get("run"),
        "visit": r.get("visit"),
        "attempt": r.get("attempt"),
        "model": r.get("model"),
        "via": r.get("via"),
        "state": _stage_state(phase_dir, r),
        "verdict": r.get("verdict"),
        "route": r.get("route"),
        "next": r.get("next"),
        "summary": (result or {}).get("summary"),
        "result": result,
        "result_source": r.get("result_source"),
        "result_warnings": r.get("result_warnings") or [],
        "result_error": r.get("result_error"),
        "checks": r.get("checks") or [],
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
        "cost_usd": r.get("total_cost_usd"),
        "turns": r.get("num_turns"),
        "duration_ms": r.get("duration_ms"),
        "head_before": r.get("head_before"),
        "head_after": r.get("head_after"),
        "remote_unchanged": r.get("remote_unchanged"),
        "remote_changed": r.get("remote_changed") or [],
        "alive": (phase_dir / dispatch.PID_FILE).exists(),
        "has_checks_log": (phase_dir / "checks.log").is_file(),
        "effort": r.get("effort"),
        "nudged": r.get("nudged"),
        "nudge": r.get("nudge"),
        "interrupt": r.get("interrupt"),
        "interrupted_at": r.get("interrupted_at"),
        "kill_reason": r.get("kill_reason"),
        "retry": r.get("retry"),
        "retry_of": r.get("retry_of"),
        "retry_exhausted": r.get("retry_exhausted"),
        "max_visits": r.get("max_visits"),
        "artifacts": _artifact_views(result, files_url),
    }


def _jobs_dir(target: Target, ticket_id: str) -> Path:
    return state.run_dir(target, ticket_id) / "jobs"


def _global_jobs_dir() -> Path:
    return config.state_home() / "jobs"  # jobs before a target exists (new project)


def jobs(target: Target, ticket_id: str) -> list[dict]:
    return read_jobs(_jobs_dir(target, ticket_id))


def busy_tickets(target: Target) -> list[str]:
    """The target's tickets under way, as the status row counts them: a live lock or a live job."""
    return [ticket_id for ticket_id in state.ticket_ids(target)
            if state.running(target, ticket_id) or any(j["state"] == "running" for j in jobs(target, ticket_id)[-1:])]


def read_jobs(root: Path) -> list[dict]:
    out = []
    for job_dir in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            meta = state.read_json(job_dir / "job.json")
        except (OSError, ValueError):
            continue
        exit_path = job_dir / "exit"
        code = None
        if exit_path.exists():
            try:
                code = int(exit_path.read_text().strip())
            except ValueError:
                code = -1
        alive = code is None and state._pid_alive(meta.get("pid", 0))
        out.append({
            "id": job_dir.name,
            "command": meta["command"],
            "started_at": meta["started_at"],
            "exit": code,
            # 3 = the run stopped to wait for a human (a question or an approval): not an error.
            "state": "running" if alive else ("lost" if code is None else ("ok" if code in (0, 3) else "error")),
            "tail": _tail(job_dir / "out.log", 1500),
        })
    return out


def _last_lines(phase_dir: Path, count: int = 4) -> list[str]:
    """The last few brief watch lines of a session, from the tail of its stream only (a long
    session's file is megabytes; the dashboard asks every few seconds)."""
    stream = phase_dir / dispatch.OUTPUT
    try:
        with stream.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
    except OSError:
        return []
    if size > 65536:
        chunk = chunk[chunk.find(b"\n") + 1:]  # drop the partial first line
    describer = progress.Describer("", brief=True)
    describer.started = True  # a tail has no init event worth a line
    lines = []
    for raw in chunk.decode("utf-8", "replace").splitlines():
        try:
            lines.extend(describer.lines(json.loads(raw)))
        except ValueError:
            continue
    return lines[-40:] if count is None else lines[-count:]


def _live(phase_dir: Path) -> dict:
    """What a running session is doing now: its latest tool line and its latest message."""
    lines = _last_lines(phase_dir, None)
    said = [x for x in lines if x.startswith("» ")]
    done = [x for x in lines if not x.startswith("» ")]
    return {"activity": done[-1] if done else None, "last_text": said[-1][2:] if said else None, "lines": lines[-4:]}


def ticket_row(target: Target, ticket_id: str) -> dict:
    t = state.load_ticket(target, ticket_id)
    st = state.load_state(target, ticket_id)
    running = state.running(target, ticket_id)
    dirs = state.phase_dirs(target, ticket_id)
    current = _stage_run(dirs[-1]) if dirs else None
    job_list = jobs(target, ticket_id)
    last_job = job_list[-1] if job_list else None
    if last_job and last_job["state"] == "running":
        running = True
    run = st.get("run") or {}
    live = _live(dirs[-1]) if running and dirs else {"activity": None, "last_text": None, "lines": []}
    return {
        "target": target.name,
        "mode": target.mode,
        "id": ticket_id,
        "title": t["title"],
        "type": t["type"],
        "branch": t["branch"],
        "base_branch": t["base_branch"],
        "plan_checkpoint": t.get("plan_checkpoint"),
        "language": t.get("language"),
        "origin": t.get("origin"),
        "roadmap": roadmap.ticket_ref(target, ticket_id, t),  # {item, phase, title}: the roadmap item it works on
        "pull": t.get("pull"),
        "queued_at": st.get("waiting_at") if running else None,  # waiting in line for a run slot
        "pull_foreign": bool(t.get("pull") and t["pull"]["author"] != target.gh_user),
        "created_at": t["created_at"],
        "status": st["status"],
        "running": running,
        "pipeline": st.get("pipeline"),
        "stage": st.get("stage"),
        "stages": pipeline.stages_view(target, ticket_id),  # [] for a v1 ticket
        "tasks": st.get("tasks"),
        "risk": st.get("risk"),
        "size": st.get("size"),
        "round": pipeline.open_round(st),
        "auto_answer_at": pipeline.default_answer_at(target, st),  # answered with its recommendations then (answer_after)
        "gate": st.get("gate"),
        "checks": st.get("checks") or [],
        "user_reviews": st.get("user_reviews") or [],
        "error": st.get("error"),
        "legacy": not st.get("pipeline"),  # made by the BMad version: it publishes, reviews and cleans up, but never runs
        "phase": current["name"] if current else None,
        "model": current["model"] if current else None,
        "visit": current["visit"] if current else None,
        "phase_started_at": current["started_at"] if current and running else None,
        "run_started_at": run.get("started_at") if running else None,
        "last_lines": live["lines"],
        "activity": live["activity"],
        "last_text": live["last_text"],
        "cost_usd": round(sum(_run_record(d).get("total_cost_usd") or 0 for d in _session_dirs(target, ticket_id, dirs)), 2),
        "updated_at": st.get("updated_at"),
        "pr": st.get("pr"),
        "worktree": st.get("worktree"),
        "last_job": {k: last_job[k] for k in ("command", "state", "started_at")} if last_job else None,
        "review": _review_summary(target, ticket_id, st),
        "drafts": changes.draft_count(target, ticket_id),
        # Findings the AI review dismissed (shown on the done card so the user can still send them to fix).
        "dismissed": changes.dismissed_count(target, ticket_id) if st["status"] == "done" else 0,
        "resumable": not running and pipeline.resumable(target, ticket_id) is not None,
        # "Yalnız check'leri tekrar koştur": a failed READY visit whose engine checks failed (no agent needed).
        "recheckable": not running and pipeline.recheck_source(target, ticket_id, st) is not None,
        "archived": st.get("archived"),
        "pr_state": st.get("pr_state"),
        "retry_at": st.get("retry_at"),  # waiting out a backoff before the stage resumes
        # Held for its Claude login's usage limit (status quota): {account, label, reason, stage, resume_at, …}.
        "quota": st.get("quota") if st.get("status") == quota.STATUS else None,
        # Held while the machine is short of memory (status host): {stage, since, sample}.
        "host": st.get("host") if st.get("status") == host.STATUS else None,
        "budget": st.get("budget") if st.get("status") == budget.STATUS else None,
        "nudges": (st.get("nudges") or [])[-3:],
        "protected_pending": [p["files"] for p in protected.pending(st)],  # patches of protected files waiting for you
        "needs_you": attention.needs_from(target, t, st, running, last_job, dirs),
        # Dependencies and stacks (stack.view): after, waiting_for, parent, slice, stack_base, children, split_open,
        # split_offer (the gate may split this plan), restack, restacked, retargeted.
        **stack.view(target, t, st),
    }


def _review_summary(target: Target, ticket_id: str, st: dict) -> dict | None:
    root = state.run_dir(target, ticket_id) / "reviews"
    latest = sorted(root.iterdir())[-1] if root.is_dir() and any(root.iterdir()) else None
    if latest is None:
        return None
    try:
        meta = state.read_json(latest / "meta.json")
    except (OSError, ValueError):
        return None
    text = _read_text(latest / "review.md")
    done = "finished_at" in meta
    wt = st.get("worktree")
    # The branch moved on since the reviewed commit: the verdict no longer covers what the PR gets.
    head = review._head(Path(wt)) if done and meta.get("head") and wt and Path(wt).is_dir() else None
    return {"id": latest.name, "state": "running" if not done else "ok" if meta.get("exit") == 0 and text.strip() else "error",
            "findings": len(review.findings(text)), "posted": bool(meta.get("posted_at")),
            "head": meta.get("head"), "stale": bool(head and head != meta["head"])}


def _session_dirs(target: Target, ticket_id: str, dirs: list[Path] | None = None) -> list[Path]:
    """Every session the ticket paid for: its stage sessions and its retro sessions."""
    return [*(state.phase_dirs(target, ticket_id) if dirs is None else dirs), *retro.session_dirs(target, ticket_id)]


def _today_cost(target: Target, ticket_id: str, today: str) -> float:
    total = 0.0
    for phase_dir in _session_dirs(target, ticket_id):
        r = _run_record(phase_dir)
        started = r.get("started_at")
        if started and datetime.fromisoformat(started).astimezone().date().isoformat() == today:
            total += r.get("total_cost_usd") or 0
    return total


def bmad_marker(target: Target) -> dict:
    """{installed, version, missing_local}: BMad on the target's base branch (cached), and which of its
    per-developer files the main checkout lacks."""
    try:
        found = bmad.status(target)
    except (OSError, subprocess.SubprocessError) as exc:
        found = {"installed": False, "version": None, "reason": str(exc)}
    return {"installed": found["installed"], "version": found["version"], "reason": found["reason"],
            "missing_local": bmad.local_ready(target) if found["installed"] else []}


def snapshot(started_at: float) -> dict:
    targets, tickets, loaded = [], [], []
    today = datetime.now().date().isoformat()
    cost_today = 0.0
    for name in config.target_names():
        try:
            target = config.load_target(name)
        except ConfigError as exc:
            targets.append({"name": name, "error": str(exc)})
            continue
        loaded.append(target)
        targets.append({
            "name": name,
            "path": str(target.path),
            "github_repo": target.github_repo,
            "active": state.slot_holders(target),
            "max_active": target.max_active,
            "bmad": bmad_marker(target),
            "memory_pending": len(memory.pending(target)),
            "mode": target.mode,  # "controlled": plan approval always, dependants ask to start, no GitHub replies
            "warnings": list(target.warnings),  # config keys the mode overrode
            "default_pipeline": ticket.default_choice(target),  # the composer's choice when none was saved
        })
        for ticket_id in state.ticket_ids(target):
            try:
                tickets.append(ticket_row(target, ticket_id))
                cost_today += _today_cost(target, ticket_id, today)
            except (OSError, ValueError, KeyError) as exc:
                tickets.append({"target": name, "id": ticket_id, "title": "(state okunamıyor)", "status": "error", "running": False,
                                "stages": [], "needs_you": attention.need("error", attention.UNREADABLE, key="error", details=str(exc))})
    tickets.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {
        "generated_at": state.now(),
        "uptime_s": int(time.time() - started_at),
        "targets": targets,
        "tickets": tickets,
        "running": sum(1 for r in tickets if r.get("running")),
        "cost_today_usd": round(cost_today, 2),
        "claude_cli": shutil.which(dispatch.claude_bin()) is not None,
        "unread": attention.unread(loaded),
        "board": attention.board_from(tickets),
        "memory_pending": sum(t.get("memory_pending") or 0 for t in targets),
        "quota": quota.view(loaded),  # one row per Claude login: its windows' utilization, blocked until
        "host": host.view(),  # the machine's memory: {pressure, swap_percent, load1, busy, show, tone, text}
    }


def _outcome(phase_dir: Path, r: dict) -> str:
    """A session's end as the stats count it: the verdict of an ok stage, else how it ended."""
    st = _stage_state(phase_dir, r)
    if st == "ok":
        return (r.get("verdict") or "READY").lower()
    return st


def _onboard_sessions() -> list[Path]:
    """The onboarding sessions (onboard_agent.py): <state home>/onboard/<request>/NN-onboard, before any target exists."""
    base = onboard_verify.root()
    requests = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    return [d for r in requests for d in sorted(r.iterdir()) if d.is_dir() and state.PHASE_DIR.match(d.name)]


DELIVERY_DAYS = 30


def _pr_opened_at(target: Target, ticket_id: str, st: dict) -> str | None:
    """When the ticket's PR opened: recorded by publish since the delivery metrics, else its first panel publish job."""
    if st.get("pr_opened_at"):
        return st["pr_opened_at"]
    started = [j.get("started_at") for j in jobs(target, ticket_id) if str(j.get("command") or "").startswith("publish")]
    return min((s for s in started if s), default=None)


def delivery(days: int = DELIVERY_DAYS) -> dict:
    """Merged PRs per target and overall over the last `days` days: how many, the share merged without a change round
    after the PR opened (no request-changes or GitHub feedback round), change rounds per PR, and quota per merged PR
    (the ticket's sessions' cost as the stats count it). `unit` is "kota" for a target whose Claude login is a
    subscription (its sessions report rate limits, quota.json has it), else "usd"."""
    since = datetime.now().astimezone().date() - timedelta(days=days - 1)
    ledger = quota.load()["accounts"]
    rows, merged_all = [], []
    for name in config.target_names():
        try:
            target = config.load_target(name)
        except ConfigError:
            continue
        unit = "kota" if quota.account_key(target) in ledger else "usd"
        merged = []
        for ticket_id in state.ticket_ids(target):
            try:
                st = state.load_state(target, ticket_id)
            except (OSError, ValueError):
                continue
            archived = st.get("archived") or {}
            if not st.get("pr") or (archived.get("pr_state") or st.get("pr_state")) != "MERGED":
                continue
            at = archived.get("at") or st.get("updated_at")
            if not at or datetime.fromisoformat(at).astimezone().date() < since:
                continue
            opened = _pr_opened_at(target, ticket_id, st)
            rounds = sum(1 for r in st.get("user_reviews") or []
                         if r.get("source") == "github" or (opened and str(r.get("at") or "") > opened))
            cost = sum(_run_record(d).get("total_cost_usd") or 0 for d in _session_dirs(target, ticket_id))
            merged.append({"id": ticket_id, "rounds": rounds, "cost": cost, "unit": unit})
        merged_all += merged
        rows.append({"name": name, "unit": unit, **_delivery_numbers(merged)})
    units = {m["unit"] for m in merged_all}
    return {"days": days, "by_target": rows,
            "overall": {"name": "hepsi", "unit": units.pop() if len(units) == 1 else "mixed" if units else "kota",
                        **_delivery_numbers(merged_all)}}


def _delivery_numbers(merged: list[dict]) -> dict:
    n = len(merged)
    clean = sum(1 for m in merged if not m["rounds"])
    return {"merged": n, "clean": clean, "clean_share": round(clean / n, 3) if n else None,
            "rounds_per_pr": round(sum(m["rounds"] for m in merged) / n, 2) if n else None,
            "cost_per_pr": round(sum(m["cost"] for m in merged) / n, 2) if n else None}


def stats(days: int = 14) -> dict:
    """Cost, tokens and outcomes over every session of every target, from the run records
    (modelUsage carries per-model tokens and cost, subagents included), the roadmap drafts (stage `roadmap`, no
    ticket), the lead sessions (stage `lead`, no ticket) and the onboarding sessions (stage `onboard`, under the name
    the repository was being added as; no ticket); `delivery` is the last 30 days' merged PRs (delivery())."""
    today = datetime.now().astimezone().date()
    found: list[tuple[str | None, str | None, Path]] = []
    for name in config.target_names():
        try:
            target = config.load_target(name)
        except ConfigError:
            continue
        for ticket_id in state.ticket_ids(target):
            found += [(name, ticket_id, d) for d in _session_dirs(target, ticket_id)]
        found += [(name, None, d) for d in roadmap.session_dirs(target)]
        found += [(name, None, d) for d in lead.session_dirs(target)]
    found += [(None, None, d) for d in _onboard_sessions()]
    sessions_ = []
    for name, ticket_id, phase_dir in found:
        r = _run_record(phase_dir)
        if not r.get("started_at"):
            continue
        day = datetime.fromisoformat(r["started_at"]).astimezone().date()
        sessions_.append({
            "target": name or str(r.get("target") or "onboard"), "ticket": ticket_id, "stage": _stage_of(phase_dir, r), "day": day,
            "cost": r.get("total_cost_usd") or 0.0, "duration_ms": r.get("duration_ms") or 0,
            "turns": r.get("num_turns") or 0, "models": r.get("modelUsage") or {},
            "outcome": _outcome(phase_dir, r),
        })

    def total(since):
        return round(sum(x["cost"] for x in sessions_ if x["day"] >= since), 2)

    daily = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        on_day = [x for x in sessions_ if x["day"] == day]
        by_stage = {}
        for x in on_day:
            by_stage[x["stage"]] = by_stage.get(x["stage"], 0.0) + x["cost"]
        daily.append({"day": day.isoformat(), "cost": round(sum(x["cost"] for x in on_day), 2),
                      "stages": {k: round(v, 2) for k, v in by_stage.items()}, "sessions": len(on_day)})

    def group(key):
        out = {}
        for x in sessions_:
            g = out.setdefault(x[key], {"cost": 0.0, "sessions": 0, "duration_ms": 0, "turns": 0})
            g["cost"] += x["cost"]
            g["sessions"] += 1
            g["duration_ms"] += x["duration_ms"]
            g["turns"] += x["turns"]
        return [{"name": k, **{**v, "cost": round(v["cost"], 2)}} for k, v in sorted(out.items(), key=lambda kv: -kv[1]["cost"])]

    models = {}
    for x in sessions_:
        for model, u in x["models"].items():
            m = models.setdefault(model, {"cost": 0.0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            m["cost"] += u.get("costUSD") or 0
            m["input"] += u.get("inputTokens") or 0
            m["output"] += u.get("outputTokens") or 0
            m["cache_read"] += u.get("cacheReadInputTokens") or 0
            m["cache_write"] += u.get("cacheCreationInputTokens") or 0
    outcomes = {}
    for x in sessions_:
        outcomes[x["outcome"]] = outcomes.get(x["outcome"], 0) + 1
    tickets = {}
    for x in [x for x in sessions_ if x["ticket"] is not None]:
        t = tickets.setdefault((x["target"], x["ticket"]), {"target": x["target"], "id": x["ticket"], "cost": 0.0, "sessions": 0})
        t["cost"] += x["cost"]
        t["sessions"] += 1
    return {
        "totals": {"today": total(today), "week": total(today - timedelta(days=6)), "month": total(today - timedelta(days=29)),
                   "all": round(sum(x["cost"] for x in sessions_), 2), "sessions": len(sessions_), "tickets": len(tickets)},
        "daily": daily,
        "by_target": group("target"),
        "by_stage": group("stage"),
        "models": [{"name": k, **{**v, "cost": round(v["cost"], 2)}} for k, v in sorted(models.items(), key=lambda kv: -kv[1]["cost"])],
        "outcomes": outcomes,
        "top_tickets": [{**t, "cost": round(t["cost"], 2)} for t in sorted(tickets.values(), key=lambda t: -t["cost"])[:5]],
        "budgets": {n: config.load_target(n).budget_usd for n in config.target_names()},
        "delivery": delivery(),
    }


def files_url(target: Target, ticket_id: str) -> str:
    """The prefix under which the panel serves a ticket's evidence (inbox/artifacts)."""
    return f"/api/files/{quote(target.name, safe='')}/{quote(ticket_id, safe='')}/"


def ticket_detail(target: Target, ticket_id: str) -> dict:
    row = ticket_row(target, ticket_id)
    st = state.load_state(target, ticket_id)
    plan = state.plan_path(target, ticket_id)
    run_dir = state.run_dir(target, ticket_id)
    return {
        **row,
        "ticket_md": _read_text(run_dir / "ticket.md"),
        "plan_text": _read_text(plan),
        "plan_path": str(plan),
        "deferred_text": _read_text(plan.parent / "deferred.md"),
        "deferred": deferred.entries(target, ticket_id),
        "answers_md": _read_text(run_dir / "answers.md"),
        "rounds": st.get("rounds") or [],
        "stage_runs": [_stage_run(d, files_url(target, ticket_id)) for d in state.phase_dirs(target, ticket_id)],
        "memory": memory.ticket_view(target, ticket_id, [{**_run_record(d), "dir": d.name} for d in state.phase_dirs(target, ticket_id)]),
        "jobs": jobs(target, ticket_id),
        "prepare_log": _tail(run_dir / "prepare.log", 3000),
        "reviews": [{**r, "findings": review.findings(r.get("text") or "")} for r in review.reviews(target, ticket_id)],
        "published_head": st.get("published_head"),
        "chat": chat.summary(target, ticket_id),
    }


class Activity:
    """Live watch lines per session, continued from where each viewer left off: a Describer and a
    byte position per (session, verbosity), so a growing claude.jsonl is not re-read each poll."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cursors: dict[tuple, tuple[int, progress.Describer, list[str]]] = {}

    def read(self, target: Target, ticket_id: str, phase: str | None, offset: int, show_all: bool) -> dict:
        dirs = state.phase_dirs(target, ticket_id)
        names = [d.name for d in dirs]
        if not dirs:
            return {"phases": [], "phase": None, "lines": [], "offset": 0, "reset": False, "ended": True}
        if phase and phase not in names:
            raise LookupError(f"{ticket_id} has no session {phase}")
        phase_dir = dirs[names.index(phase)] if phase else dirs[-1]
        key = (str(phase_dir), show_all)
        with self._lock:
            # A cursor keeps every line it produced, so any offset (line count) can be answered.
            position, describer, lines = self._cursors.get(key) or (0, progress.Describer(phase_dir.name.split("-", 1)[-1], brief=not show_all), [])
            new, position = progress.read_lines(phase_dir, position, describer)
            lines = lines + new
            self._cursors[key] = (position, describer, lines)
        if not lines and not (phase_dir / dispatch.OUTPUT).exists():
            # Sessions from before stream-json kept only the final message (claude.json).
            final = _run_record(phase_dir).get("final_message", "")
            lines = ["(this session ran before live events were recorded; final message only)"]
            lines += ["» " + line for line in final.strip().splitlines() if line.strip()]
        reset = offset > len(lines)
        start = 0 if reset else offset
        footer = progress.footer(phase_dir)
        alive = (phase_dir / dispatch.PID_FILE).exists()
        return {
            "phases": names,
            "phase": phase_dir.name,
            "header": progress.header(phase_dir),
            "lines": lines[start:],
            "offset": len(lines),
            "reset": reset,
            "footer": footer if footer and not alive else None,
            "ended": not alive and footer is not None,
        }


# --- actions ----------------------------------------------------------------------------------

SLOT_COMMANDS = {"run", "answer", "approve", "request-changes", "resume", "fix"}  # the ones that take a run slot


def start_job(target: Target, ticket_id: str, *args: str) -> dict:
    full = args[0] in SLOT_COMMANDS and len(state.slot_holders(target)) >= target.max_active
    return {**spawn_job(_jobs_dir(target, ticket_id), ["--target", target.name, *args]), "queued": full}


def spawn_job(root: Path, cli_args: list[str]) -> dict:
    """Run `ziggy <cli_args>` detached; out.log and the exit code land in the job's
    directory, so a panel restart loses nothing."""
    args = cli_args[2:] if cli_args[:1] == ["--target"] else cli_args
    job_dir = root / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{args[0]}"
    job_dir.mkdir(parents=True)
    argv = [*config.self_argv(), *cli_args]
    # The exit code lands in place with a rename: read_jobs never sees an `exit` the shell has created but not written.
    wrapper = ["/bin/sh", "-c", '"$@" > out.log 2>&1; echo $? > exit.tmp; mv exit.tmp exit', "ziggy-job", *argv]
    proc = subprocess.Popen(wrapper, cwd=job_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    meta = {"command": " ".join(args[:2]), "argv": argv, "pid": proc.pid, "started_at": state.now()}
    state.write_json(job_dir / "job.json", meta)
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it; the exit file is the record
    return {"id": job_dir.name, **meta}


def _busy(target: Target, ticket_id: str) -> bool:
    return state.running(target, ticket_id) or any(j["state"] == "running" for j in jobs(target, ticket_id))


def _chat_jobs_dir(target: Target, ticket_id: str) -> Path:
    # Kept apart from runs/<id>/jobs: a chat reply never blocks run/approve, nor shows in Job'lar.
    return state.run_dir(target, ticket_id) / "chat" / "jobs"


def talk_post(target: Target, chat_id: str | None, body: dict) -> dict:
    """Sohbet from the panel: a new chat (POST /api/talk/<target>, its first message in `text`), or `send`, `stop` and
    `delete` on one (POST /api/talk/<target>/<id>). A message is answered by a background job, like a ticket chat's."""
    text, model = str(body.get("text") or ""), str(body.get("model") or "") or None
    if chat_id is None:
        meta = talk.create(target, str(body.get("mode") or "read"))
        if not text.strip():
            return {"chat": meta}
        try:
            return {"chat": meta, "job": _talk_send(target, meta["id"], text, model)}
        except Refused:
            talk.delete(target, meta["id"])  # never left behind empty
            raise
    action = str(body.get("action") or "")
    if action == "send":
        return {"job": _talk_send(target, chat_id, text, model)}
    if action == "stop":
        try:
            return {"stopped": talk.stop(target, chat_id)}
        except LookupError as exc:  # "nothing is being answered" is a refusal, not a 404
            raise Refused(str(exc)) from None
    if action == "delete":
        try:
            return talk.delete(target, chat_id, force=body.get("force") is True)
        except pipeline.PipelineError as exc:
            raise Refused(str(exc)) from None
    raise Refused(f"unknown action {action!r}")


def _talk_send(target: Target, chat_id: str, text: str, model: str | None) -> dict:
    try:
        talk.check(target, chat_id, text, model)
    except pipeline.PipelineError as exc:
        raise Refused(str(exc)) from None
    args = ["--target", target.name, "talk", "send", chat_id, "--text", text.strip(), *(["--model", model] if model else [])]
    return spawn_job(talk.root(target) / chat_id / "jobs", args)


def _write_pr_body(target: Target, ticket_id: str, body: str) -> Path:
    """The edited PR description goes to a file, not the job's argv (job.json and ps stay small)."""
    path = state.run_dir(target, ticket_id) / "pr-body.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path


def do_action(target: Target, ticket_id: str, action: str, text: str = "", review_id: str = "", opts: dict | None = None) -> dict:
    opts = opts or {}
    st = state.load_state(target, ticket_id)  # also checks the ticket exists
    status = st["status"]
    if action == "stop":
        return {"stopped": dispatch.stop(target, ticket_id)}
    if action == "dismiss":
        try:
            return attention.dismiss(target, ticket_id, text.strip())
        except attention.Refused as exc:
            raise Refused(str(exc)) from None
    if action == "nudge":
        # Steering a running stage is the point: no busy refusal; the engine refuses what it cannot deliver.
        try:
            return {"nudged": pipeline.nudge(target, ticket_id, text, by="panel", via="panel")}
        except pipeline.PipelineError as exc:
            raise Refused(str(exc)) from None
    if action == "chat":
        new, model = bool(opts.get("new")), str(opts.get("model") or "") or None
        try:
            chat.check(target, ticket_id, text, new_thread=new, model=model)
        except pipeline.PipelineError as exc:
            raise Refused(str(exc)) from None
        args = ["--target", target.name, "chat", ticket_id, "--text", text.strip()]
        if new:
            args.append("--new")
        if model:
            args += ["--model", model]
        return {"job": spawn_job(_chat_jobs_dir(target, ticket_id), args)}
    if action == "chat_stop":
        try:
            return {"stopped": chat.stop(target, ticket_id)}
        except (pipeline.PipelineError, LookupError) as exc:  # "nothing is being answered" is a refusal, not a 404
            raise Refused(str(exc)) from None
    if action == "publish":
        if status != "done":
            raise Refused(f"{ticket_id} is {status}; only a done ticket gets a PR")
        if any(j["state"] == "running" for j in jobs(target, ticket_id)):
            raise Refused(f"{ticket_id} has a job running")
        extra = []
        title, body = opts.get("title"), opts.get("body")
        if title is not None or body is not None:
            if not isinstance(title, str) or not isinstance(body, str):
                raise ValueError("title and body must be text")
            title = title.strip()
            if not title or len(title) > publish.MAX_TITLE or "\n" in title or "\r" in title:
                raise ValueError(f"the PR title must be one line of 1–{publish.MAX_TITLE} characters")
            if len(body) > publish.MAX_BODY:
                raise ValueError(f"the PR description is longer than {publish.MAX_BODY} characters")
            extra = ["--title", title, "--body-file", str(_write_pr_body(target, ticket_id, body))]
        # guard.py: what would leave the machine with findings is asked about here, before the job, so the panel can
        # show them and send the publish again approving exactly those.
        if opts.get("approve_findings") is True:
            extra.append("--accept-findings")
        else:
            found = publish.pending_findings(target, ticket_id, title, body)
            if found:
                raise Refused(guard.message(found))
        pull = state.load_ticket(target, ticket_id).get("pull")
        if pull and pull["author"] != target.gh_user:
            # Someone else's PR: the panel asked first, and says so in the text.
            if text != "foreign-ok":
                raise Refused(f"PR #{pull['number']} is {pull['author']}'s; confirm pushing to their branch")
            return {"job": start_job(target, ticket_id, "publish", ticket_id, "--foreign-ok", *extra)}
        return {"job": start_job(target, ticket_id, "publish", ticket_id, *extra)}
    if action in ("review", "post_review"):
        if status != "done":
            raise Refused(f"{ticket_id} is {status}; a review needs a done ticket")
        if _busy(target, ticket_id):
            raise Refused(f"{ticket_id} has a job running")
        if action == "review":
            return {"job": start_job(target, ticket_id, "review", ticket_id)}
        return {"posted": review.post(target, ticket_id, text.strip())}
    if action == "restack_push":
        # The rebased branch goes to its PR only after the panel's confirm (force-with-lease, its own branch).
        if not (st.get("restack") or {}).get("rebased"):
            raise Refused(f"{ticket_id} has no rebased branch to push")
        if _busy(target, ticket_id):
            raise Refused(f"{ticket_id} has a job running")
        return {"job": start_job(target, ticket_id, "restack", ticket_id, "--push")}
    if action in ("protected_apply", "protected_reject"):
        # Uygula / Reddet on a protected file's patch: quick, done here (git apply + commit in the wrapper), never while
        # the ticket runs (the run rewrites its state and its worktree).
        if _busy(target, ticket_id):
            raise Refused(f"{ticket_id} is running; decide on the protected file change once it stops")
        try:
            n = int(opts.get("n"))
        except (TypeError, ValueError):
            raise ValueError("n must be the patch number") from None
        try:
            if action == "protected_apply":
                return {"protected": protected.apply(target, ticket_id, n, by="panel")}
            return {"protected": protected.reject(target, ticket_id, n, text, by="panel")}
        except (protected.ProtectedError, state.LockError) as exc:
            raise Refused(str(exc)) from None
    if _busy(target, ticket_id):
        raise Refused(f"{ticket_id} is already running")
    if action == "restack":
        info = st.get("restack")
        if not info or info.get("rebased"):
            raise Refused(f"{ticket_id} needs no restack" if not info else f"{ticket_id} is rebased already; push it")
        return {"job": start_job(target, ticket_id, "restack", ticket_id)}
    if action == "split":
        # Quick (tickets and plans only): done here; the first slice runs as its own job.
        try:
            made = stack.split(target, ticket_id)
        except (stack.StackError, state.LockError) as exc:
            raise Refused(str(exc)) from None
        return {"split": [c["id"] for c in made], "job": start_job(target, made[0]["id"], "run", made[0]["id"])}
    if action == "cleanup":
        # Teardown commands can take minutes: a job, like the other long actions. The branch stays.
        if status not in ("done", "failed"):
            raise Refused(f"{ticket_id} is {status}; only a finished ticket's worktree can be removed")
        if not st.get("worktree"):
            raise Refused(f"{ticket_id} has no worktree")
        return {"job": start_job(target, ticket_id, "cleanup", ticket_id, *(["--force"] if text == "force" else []))}
    # All run slots busy is no refusal: the job waits in line and starts when one frees up.
    if not st.get("pipeline"):
        # Refused here, at once, rather than as a failed job the user then has to clear.
        raise Refused(f"{ticket_id} was made by the BMad version of the pipeline and cannot run on this one; "
                      "open a new ticket (its branch and PR still publish, review and clean up)")
    if action == "fix":
        if status != "done":
            raise Refused(f"{ticket_id} is {status}; review fixes apply to a done ticket")
        if (state.load_ticket(target, ticket_id).get("pull") or {}).get("fork"):
            raise Refused(f"{ticket_id} is a PR from a fork; fixes could not be pushed to it")
        if not text.strip():
            raise Refused("no findings to fix")
        args = ["fix", ticket_id, "--text", text.strip()]
        if review_id:
            if not REVIEW_ID.match(review_id):
                raise LookupError(f"bad review id {review_id!r}")
            args += ["--review", review_id]
        return {"job": start_job(target, ticket_id, *args)}
    if action == "request_changes":
        if status not in ("done", "failed") or not st.get("worktree"):
            raise Refused(f"{ticket_id} is {status}; review comments go to a done or failed ticket with a worktree")
        if opts.get("github") is not None:  # PR feedback items picked on the review page
            try:
                ids = feedback.check(target, ticket_id, opts["github"])
            except feedback.FeedbackError as exc:
                raise Refused(str(exc)) from None
            return {"job": start_job(target, ticket_id, "request-changes", ticket_id, "--github", *ids)}
        return {"job": start_job(target, ticket_id, "request-changes", ticket_id)}
    if action == "resume":
        if pipeline.resumable(target, ticket_id) is None:
            raise Refused(f"{ticket_id} has no interrupted session to resume; use run")
        return {"job": start_job(target, ticket_id, "resume", ticket_id)}
    if action == "approve":
        if status != "awaiting-approval":
            raise Refused(f"{ticket_id} is {status}; only a ticket waiting for approval can be approved")
        return {"job": start_job(target, ticket_id, "approve", ticket_id)}
    if action == "recheck":
        if pipeline.recheck_source(target, ticket_id, st) is None:
            raise Refused(f"{ticket_id}: only a failed ticket whose last visit was READY and failed an engine check can "
                          "re-run its checks alone")
        return {"job": start_job(target, ticket_id, "recheck", ticket_id)}
    if action == "run":
        if status in ("done", "needs-user", "awaiting-approval"):
            raise Refused(f"{ticket_id} is {status}; " + ("nothing to run" if status == "done" else "answer it instead"))
        # `now`: "Şimdi başlat" on a ticket held for its login's usage limit (the panel warned first).
        return {"job": start_job(target, ticket_id, "run", ticket_id, *(["--full-review"] if opts.get("full_review") else []),
                                 *(["--now"] if opts.get("now") is True else []))}
    if action == "answer":
        if status not in ("needs-user", "awaiting-approval", "failed"):
            raise Refused(f"{ticket_id} is {status}; answer applies to a ticket with questions, waiting for approval, or failed")
        if not text.strip():
            raise Refused("the answer is empty")
        return {"job": start_job(target, ticket_id, "answer", ticket_id, "--text", text.strip(), "--via", "panel")}
    raise Refused(f"unknown action {action}")


def decide_memory(target: Target, candidate_id: str, body: dict) -> dict:
    """POST /api/memory/<target>/candidates/<id> {action: approve|reject, text?, title?, replace?}: `text` and `title`
    are the edited main field and title ("Düzenle ve onayla"), `replace` the "yerine geçsin" box."""
    action, text, title, replace = str(body.get("action") or ""), body.get("text"), body.get("title"), body.get("replace", False)
    for name, value in (("text", text), ("title", title)):
        if value is not None and (not isinstance(value, str) or action != "approve"):
            raise ValueError(f"{name} is the edited {name} of an approval")
    if not isinstance(replace, bool) or (replace and action != "approve"):
        raise ValueError("replace is true or false, with an approval")
    try:
        return {"candidate": memory.decide(target, candidate_id, action, text=text, title=title, replace=replace)}
    except memory.Decided as exc:
        raise Refused(str(exc)) from None


def forget_memory(target: Target, entry_id: str, body: dict) -> dict:
    """POST /api/memory/<target>/entries/<M-id> {action: forget}: `Durum: unutuldu` on the entry."""
    if body.get("action") != "forget":
        raise ValueError("the only action on an entry is forget")
    try:
        return {"entry": memory.forget(target, entry_id)}
    except memory.Decided as exc:
        raise Refused(str(exc)) from None


def roadmap_post(target: Target, action: str, body: dict) -> dict:
    """POST /api/targets/<n>/roadmap/draft {}: "Yol haritası çıkar" (the read-only draft session as a background job);
    /roadmap/save {text}: "Taslak olarak kaydet" (the edited draft becomes roadmap.md). Returns {job} or the view."""
    try:
        if action == "draft":
            return {"job": roadmap.start_draft(target)}
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be the roadmap's markdown")
        roadmap.save(target, text)
    except roadmap.RoadmapError as exc:
        raise Refused(str(exc)) from None
    return roadmap.view(target)


def new_ticket(target: Target, body: dict) -> dict:
    language = str(body.get("language") or "") or None
    chosen = str(body.get("type") or "") or "feat"
    if chosen not in ticket.TYPES:
        raise ValueError(f"type must be one of {', '.join(ticket.TYPES)}")
    ticket_type, title = ticket.split_type(str(body.get("title", "")), default=chosen)
    chosen_pipeline = str(body.get("pipeline") or "") or None
    origin = _composer_origin(target, body.get("origin"))
    after = body.get("after") or []
    if isinstance(after, str):
        after = [after]
    if not isinstance(after, list):
        raise ValueError("after must be a list of ticket ids")
    t = ticket.create(target, title, ticket_type, str(body.get("body", "")), bool(body.get("plan_checkpoint")), "low", language=language,
                      pipeline=chosen_pipeline, origin=origin, after=[str(a) for a in after])
    out = {"ticket": t}
    if origin and origin.get("kind") == lead.KIND:  # "Ticket olarak aç" / "Düzenle" on a lead proposal
        lead.accepted(target, origin["proposal"], t["id"])
    if body.get("run"):
        out["job"] = start_job(target, t["id"], "run", t["id"])
    return out


def _composer_origin(target: Target, body) -> dict | None:
    """Where a composer draft came from: a lead proposal, a roadmap item ("Ticket aç") or a deferred finding
    ("Ticket'a çevir")."""
    if not body:
        return None
    if isinstance(body, dict) and body.get("kind") == lead.KIND:
        return lead.origin(target, body)
    if isinstance(body, dict) and body.get("kind") == "roadmap":
        return roadmap.origin(target, body)
    return deferred.origin(target, body)


def lead_view() -> dict:
    """GET /api/lead: Sıradaki işler per target, the schedule, and Bugün's 'dün biten'."""
    targets = _targets()
    cfg = config.load_lead()
    return {"enabled": cfg.enabled, "at": cfg.at, "targets": [lead.view(t) for t in targets], "yesterday": lead.yesterday(targets)}


def lead_refresh(target: Target) -> dict:
    """POST /api/lead/<target>/run ("Yenile"): the target's lead now, as a background job."""
    if lead.job_running(target):
        raise Refused(f"{target.name}: lead zaten çalışıyor")
    return {"job": lead.start_job(target)}


def decide_lead(target: Target, proposal_id: str, body: dict) -> dict:
    """POST /api/lead/<target>/proposals/<id> {action: start|reject, reason?, language?}: "Başlat" creates the ticket and
    runs it (personal targets only); "Reddet" keeps its signals from being proposed again."""
    action = str(body.get("action") or "")
    if action == "reject":
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be text")
        return {"proposal": lead.reject(target, proposal_id, reason)}
    if action == "start":
        t = lead.start(target, proposal_id, language=str(body.get("language") or "") or None)
        return {"ticket": t, "job": start_job(target, t["id"], "run", t["id"])}
    raise ValueError("action must be start or reject")


def adopt_pull(target: Target, body: dict) -> dict:
    """Make an open PR a ticket (pr-<n>), and start its Codex review when asked."""
    try:
        number = int(body.get("number"))
    except (TypeError, ValueError):
        raise ValueError("number must be a PR number") from None
    t = pulls.adopt(target, number)
    out = {"ticket": t}
    if body.get("review"):
        if _busy(target, t["id"]):
            raise Refused(f"{t['id']} has a job running")
        out["job"] = start_job(target, t["id"], "review", t["id"])
    return out


def create_project(body: dict) -> dict:
    name, parent = str(body.get("name", "")).strip(), str(body.get("parent", "")).strip()
    if not name or not parent:
        raise Refused("name and folder are required")
    if parent not in new_project.project_parents():
        raise Refused(f"{parent} is not one of the project folders")
    if (Path(parent) / name).exists():
        raise Refused(f"{Path(parent) / name} already exists")
    args = ["project", "new", name, "--dir", parent, "--yes"]
    if str(body.get("description", "")).strip():
        args += ["--description", str(body["description"]).strip()]
    if str(body.get("owner", "")).strip():
        args += ["--owner", str(body["owner"]).strip()]
    if str(body.get("gh_user", "")).strip():
        args += ["--gh-user", str(body["gh_user"]).strip()]
    if "claude_config_dir" in body:
        args += ["--claude-config-dir", str(body.get("claude_config_dir") or "")]
    if body.get("private") is False:
        args.append("--public")
    return {"job": spawn_job(_global_jobs_dir(), args)}


def agents_md_pr(body: dict) -> dict:
    """'Draft PR aç' under the AGENTS.md draft: a company repository needs `company_confirmed` (the form's second,
    explicit confirmation), else it is refused."""
    path = str(body.get("path", ""))
    if onboard.company(path) and body.get("company_confirmed") is not True:
        raise Refused("this is a company repository: confirm the PR explicitly")
    url = onboard_agent.agents_md_pr(path, str(body.get("text") or ""), gh_user=str(body.get("gh_user") or "").strip(),
                                     github_repo=str(body.get("github_repo") or "").strip(),
                                     base_branch=str(body.get("base_branch") or "").strip(),
                                     company_confirmed=body.get("company_confirmed") is True)
    return {"url": url}


def global_job(job_id: str) -> dict:
    found = [j for j in read_jobs(_global_jobs_dir()) if j["id"] == job_id]
    if not found:
        raise LookupError(f"no job {job_id}")
    return found[0]


def _targets() -> list[Target]:
    out = []
    for name in config.target_names():
        try:
            out.append(config.load_target(name))
        except ConfigError:
            continue
    return out


class Sessions:
    """Other Claude Code sessions on this machine. Listing them reads every config dir's project
    folders (seconds when cold), so the 3 s poll gets the last list at once and a stale list is
    refreshed in the background, at most one refresh at a time."""

    EVERY = 10

    def __init__(self):
        self._lock = threading.Lock()
        self._at = 0.0
        self._busy = False
        self._list: list[dict] = []
        self.hidden = 0  # sessions [sessions] hide_cwd_prefixes left out of the last list

    def _refresh(self) -> list[dict]:
        counts = {"hidden": self.hidden}
        try:
            found = sessions.listing(_targets(), counts=counts)
        except Exception as exc:  # noqa: BLE001 - a broken session file must not take the panel down
            sys.stderr.write(f"{state.now()} sessions: {exc!r}\n")
            found = self._list
        with self._lock:
            self._list, self.hidden, self._at, self._busy = found, counts["hidden"], time.monotonic(), False
        return found

    def listing(self) -> list[dict]:
        with self._lock:
            stale = not self._busy and time.monotonic() - self._at > self.EVERY
            if stale:
                self._busy = True
        if stale:
            threading.Thread(target=self._refresh, daemon=True, name="sessions").start()
        return self._list

    def transcript(self, config_name: str, session_id: str, pos: int | None) -> dict:
        if not SESSION_CONFIG.match(config_name) or not SESSION_ID.match(session_id):
            raise LookupError("no such session")
        dirs = sessions.config_dirs(_targets())
        known = lambda rows: any(s["config"] == config_name and s["id"] == session_id for s in rows)  # noqa: E731
        if config_name not in dirs or not (known(self._list) or known(self._refresh())):
            raise LookupError("no such session")
        page = sessions.transcript(dirs[config_name], session_id, pos)
        if page is None:
            raise LookupError("no such session")
        return page


# --- HTTP -------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ziggy-panel"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one line per request on stderr (launchd log)
        sys.stderr.write(f"{state.now()} {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, content_type: str, csp: str = CSP, extra: dict | None = None,
              cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith(("text/", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _target(self, name: str) -> Target:
        if not config._NAME_RE.match(name or "") or name not in config.target_names():
            raise LookupError(f"no target {name}")
        return config.load_target(name)

    def _ticket(self, name: str, ticket_id: str) -> Target:
        if not TICKET_ID.match(ticket_id or ""):
            raise LookupError(f"bad ticket id {ticket_id!r}")
        target = self._target(name)
        state.load_ticket(target, ticket_id)
        return target

    def _guarded(self, fn) -> None:
        try:
            fn()
        except (Refused, lead.LeadRefused) as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except LookupError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except _ERRORS as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 - the panel must answer, and the log keeps the rest
            self.log_message("error: %r", exc)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _evidence(self, raw: list[str]) -> None:
        """GET /api/files/<target>/<id>/<path…>: one file the agents saved under inbox/artifacts, only
        through changes.artifact (realpath inside that dir, extension allowlist, size caps). Each path
        segment is decoded on its own, so an encoded '/' or '..' never reaches the file system."""
        try:
            target_name, ticket_id, *segments = [unquote(seg, errors="strict") for seg in raw]
        except UnicodeDecodeError:
            raise LookupError("not found") from None
        if not 1 <= len(segments) <= 4 or not all(EVIDENCE_SEGMENT.match(s) and s not in (".", "..") for s in segments):
            raise LookupError("not found")
        target = self._ticket(target_name, ticket_id)
        try:
            path, content_type = changes.artifact(target, ticket_id, "/".join(segments))
        except changes.ArtifactTooLarge as exc:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc) or "too large to show here")
        self._send_file(path, content_type)

    def _send_file(self, path: Path, content_type: str) -> None:
        """Stream a file in chunks, honouring one byte range (Safari plays video only with ranges)."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        m = RANGE.match(self.headers.get("Range", "").strip())
        if m and (m[1] or m[2]):
            start = int(m[1]) if m[1] else max(0, size - int(m[2]))
            end = min(int(m[2]) if m[1] and m[2] else size - 1, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", EVIDENCE_CSP)
                self.end_headers()
                return
            code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", EVIDENCE_CSP)
        self.send_header("Content-Disposition", "inline")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            while length > 0:
                chunk = f.read(min(CHUNK, length))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def _mark_seen(self, body: dict) -> dict:
        """POST /api/attention/seen {target?, ticket?, keys?: [...], all?: true}, {target, memory: true} (the
        target's memory row), {target, lead: true} (its lead proposals row) or {target, quota: true} (the row of the
        Claude login the target shows it under)."""
        keys = body.get("keys")
        if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
            raise ValueError("keys must be a list of item keys")
        keys = (keys or [])[:500]
        ticket_id = str(body.get("ticket") or "")
        if body.get("all") is True and not ticket_id and not keys:
            name = str(body.get("target") or "")
            changed = sum(attention.seen(t) for t in ([self._target(name)] if name else _targets()))
        elif body.get("memory") is True:
            changed = attention.seen(self._target(str(body.get("target", ""))), attention.MEMORY)
        elif body.get("lead") is True:
            changed = attention.seen(self._target(str(body.get("target", ""))), attention.LEAD)
        elif body.get("quota") is True:
            changed = attention.seen(self._target(str(body.get("target", ""))), attention.QUOTA)
        elif ticket_id:
            changed = attention.seen(self._ticket(str(body.get("target", "")), ticket_id), ticket_id, keys=keys or None)
        else:
            raise ValueError("say which items: keys (with ticket), ticket or all: true")
        return {"ok": True, "message": f"{changed} marked seen", "changed": changed}

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "bad host")
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        q = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            if not (WEB / name).is_file():
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            # A font never changes under its name; everything else stays no-store so a deploy shows at once.
            cache = "public, max-age=31536000, immutable" if url.path.startswith("/fonts/") else "no-store"
            return self._send(200, (WEB / name).read_bytes(), content_type, cache=cache)
        if url.path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if url.path == "/api/projects/defaults":
            parent, name = q("parent"), q("name")
            return self._guarded(lambda: self._json(200, {"parents": new_project.project_parents(),
                                                          **(new_project.project_defaults(parent, name) if parent else {})}))
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            return self._guarded(lambda: self._json(200, global_job(parts[2])))
        if len(parts) == 3 and parts[:2] == ["api", "onboard"]:
            return self._guarded(lambda: self._json(200, onboard_agent.request_view(parts[2])))
        planning = re.match(r"^/api/targets/([a-z0-9-]+)/(project|pulls|roadmap)$", url.path)
        if planning:
            fresh = q("fresh", "0") == "1"
            if planning[2] == "pulls":
                return self._guarded(lambda: self._json(200, {"pulls": pulls.listing(self._target(planning[1]), fresh=fresh)}))
            if planning[2] == "roadmap":
                return self._guarded(lambda: self._json(200, roadmap.view(self._target(planning[1]))))
            return self._guarded(lambda: self._json(200, project.status(self._target(planning[1]), fresh=fresh)))
        if url.path == "/api/targets/candidates":
            return self._guarded(lambda: self._json(200, {"candidates": onboard.candidates()}))
        if url.path == "/api/cleanup":
            return self._guarded(lambda: self._json(200, {"candidates": [c for t in _targets() for c in cleanup.candidates(t)]}))
        if url.path == "/api/stats":
            return self._guarded(lambda: self._json(200, stats()))
        if url.path == "/api/report":  # the newest weekly report; none saved yet: this week's so far, unsaved
            lang = (parse_qs(url.query).get("lang") or [None])[0]
            lang = lang if lang in config.LANGUAGES else None
            return self._guarded(lambda: self._json(200, weekly.latest(lang) or {
                "week": None, "markdown": weekly.markdown(weekly.report(_targets(), today=weekly.now().date() + timedelta(days=1)),
                                                          lang or notify.language())}))
        if url.path == "/api/status":
            return self._guarded(lambda: self._json(200, {**snapshot(self.server.started_at), "pr_sync": self.server.sync_info,
                                                          "sessions": self.server.sessions.listing(),
                                                          "sessions_hidden": self.server.sessions.hidden}))
        if url.path == "/api/sessions":
            return self._guarded(lambda: self._json(200, {"sessions": self.server.sessions.listing(),
                                                          "sessions_hidden": self.server.sessions.hidden}))
        if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[4] == "transcript":
            def session_transcript():
                pos = int(q("pos", "0") or 0)
                if pos < 0:
                    raise ValueError("pos must not be negative")
                self._json(200, self.server.sessions.transcript(parts[2], parts[3], pos or None))
            return self._guarded(session_transcript)
        if len(parts) == 4 and parts[:2] == ["api", "ticket"]:
            return self._guarded(lambda: self._json(200, ticket_detail(self._ticket(parts[2], parts[3]), parts[3])))
        if len(parts) in (4, 5) and parts[:2] == ["api", "changes"]:
            def change_view():
                target = self._ticket(parts[2], parts[3])
                if len(parts) == 4:
                    st = state.load_state(target, parts[3])
                    return self._json(200, {**changes.summary(target, parts[3], range_spec=q("range") or "all", to=q("to") or None),
                                            "github": feedback.view(target, parts[3], st)})
                if parts[4] == "diff":
                    return self._json(200, changes.file_diff(target, parts[3], q("path"), q("from"), q("to"),
                                                             old_path=q("old_path") or None, full=q("context") == "all"))
                if parts[4] == "blob":
                    data, content_type = changes.blob(target, parts[3], q("rev"), q("path"))
                    return self._send(200, data, content_type, csp=BLOB_CSP, extra={"Content-Disposition": "inline"})
                if parts[4] == "pr-draft":
                    return self._json(200, changes.pr_draft(target, parts[3]))
                raise LookupError("not found")
            return self._guarded(change_view)
        if len(parts) == 4 and parts[:2] == ["api", "chat"]:
            def chat_view():
                since = q("since") or None
                target = self._ticket(parts[2], parts[3])
                self._json(200, chat.view(target, parts[3], since=since if since and REVIEW_ID.match(since) else None))
            return self._guarded(chat_view)
        if len(parts) in (3, 4, 5) and parts[:2] == ["api", "talk"]:
            def talk_view():
                target = self._target(parts[2])
                if len(parts) == 3:
                    models, _ = chat._settings()
                    return self._json(200, {"chats": talk.list_chats(target), "models": list(models), "model": models[0]})
                if len(parts) == 5 and parts[4] == "ticket":
                    return self._json(200, talk.transcript(target, parts[3]))
                if len(parts) == 4:
                    since = q("since") or None
                    return self._json(200, talk.view(target, parts[3], since=since if since and REVIEW_ID.match(since) else None))
                raise LookupError("not found")
            return self._guarded(talk_view)
        if url.path == "/api/leftovers":
            return self._guarded(lambda: self._json(200, {"leftovers": onboard.leftovers()}))
        if url.path == "/api/attention":
            return self._guarded(lambda: self._json(200, {"items": attention.items(_targets(), all=q("all") == "1")}))
        if url.path == "/api/lead":
            return self._guarded(lambda: self._json(200, lead_view()))
        if url.path == "/api/memory":
            return self._guarded(lambda: self._json(200, {"targets": [memory.view(t) for t in _targets()]}))
        if len(parts) >= 5 and parts[:2] == ["api", "files"]:
            return self._guarded(lambda: self._evidence(url.path.split("/")[3:]))
        if len(parts) == 5 and parts[:2] == ["api", "attempts"]:
            # GET /api/attempts/<target>/<id>/attempt-<n>.patch: the code an intent gap set aside, as text.
            def attempt_patch():
                if not ATTEMPT.match(parts[4]):
                    raise LookupError("not found")
                target = self._ticket(parts[2], parts[3])
                path = state.run_dir(target, parts[3]) / pipeline.ATTEMPTS / parts[4]
                if not path.is_file():
                    raise LookupError(f"{parts[3]} has no {parts[4]}")
                self._send_file(path, "text/plain; charset=utf-8")
            return self._guarded(attempt_patch)
        if len(parts) == 4 and parts[:2] == ["api", "activity"]:
            def activity():
                phase = q("phase") or None
                if phase and not PHASE.match(phase):
                    raise LookupError(f"bad session {phase!r}")
                offset = int(q("offset", "0"))
                self._json(200, self.server.activity.read(self._ticket(parts[2], parts[3]), parts[3], phase, offset, q("all", "0") == "1"))
            return self._guarded(activity)
        if len(parts) == 5 and parts[:2] == ["api", "transcript"]:
            def transcript():
                if not PHASE.match(parts[4]):
                    raise LookupError(f"bad session {parts[4]!r}")
                target = self._ticket(parts[2], parts[3])
                phase_dir = state.run_dir(target, parts[3]) / parts[4]
                if not phase_dir.is_dir():
                    raise LookupError(f"{parts[3]} has no session {parts[4]}")
                pos = int(q("pos", "0"))
                if pos < 0:
                    raise ValueError("pos must not be negative")
                self._json(200, progress.transcript(phase_dir, pos))
            return self._guarded(transcript)
        if len(parts) == 6 and parts[:2] == ["api", "stage"]:
            # One stage session's own files, as text: the rendered prompt, its result, the engine's checks.
            def stage_file():
                if not PHASE.match(parts[4]) or parts[5] not in ("prompt", "result", "checks"):
                    raise LookupError("not found")
                target = self._ticket(parts[2], parts[3])
                name = {"prompt": "prompt.md", "result": "result.json", "checks": "checks.log"}[parts[5]]
                path = state.run_dir(target, parts[3]) / parts[4] / name
                if not path.is_file():
                    raise LookupError(f"{parts[4]} has no {name}")
                self._json(200, {"name": name, "text": _tail(path, 512 * 1024)})
            return self._guarded(stage_file)
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "bad host")
        if self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            return self._error(HTTPStatus.FORBIDDEN, "cross-site request")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON only")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if path == "/api/new":
            return self._guarded(lambda: self._json(200, new_ticket(self._target(str(body.get("target", ""))), body)))
        if path == "/api/action":
            def action():
                name = str(body.get("action", ""))
                if name not in ACTIONS:
                    raise Refused(f"unknown action {name!r}")
                ticket_id = str(body.get("id", ""))
                self._json(200, do_action(self._ticket(str(body.get("target", "")), ticket_id), ticket_id, name,
                                          str(body.get("text", "")), str(body.get("review") or ""), opts=body))
            return self._guarded(action)
        if len(parts) == 5 and parts[:2] == ["api", "changes"] and parts[4] == "review":
            return self._guarded(lambda: self._json(200, changes.review_post(self._ticket(parts[2], parts[3]), parts[3], body)))
        if len(parts) == 5 and parts[:2] == ["api", "changes"] and parts[4] == "github":
            def github_sync():
                # "Şimdi yenile" on the review page: the same fetch the PR sync makes every few minutes.
                target = self._ticket(parts[2], parts[3])
                got = feedback.fetch(target, parts[3])
                self._json(200, {**got, "github": feedback.view(target, parts[3], state.load_state(target, parts[3]))})
            return self._guarded(github_sync)
        if path == "/api/attention/seen":
            return self._guarded(lambda: self._json(200, self._mark_seen(body)))
        decided = re.match(r"^/api/memory/([a-z0-9-]+)/candidates/(c[0-9]{1,7})$", path)
        if decided:
            return self._guarded(lambda: self._json(200, decide_memory(self._target(decided[1]), decided[2], body)))
        refreshed = re.match(r"^/api/lead/([a-z0-9-]+)/run$", path)
        if refreshed:
            return self._guarded(lambda: self._json(200, lead_refresh(self._target(refreshed[1]))))
        proposal = re.match(r"^/api/lead/([a-z0-9-]+)/proposals/(p[0-9]{1,7})$", path)
        if proposal:
            return self._guarded(lambda: self._json(200, decide_lead(self._target(proposal[1]), proposal[2], body)))
        forgotten = re.match(r"^/api/memory/([a-z0-9-]+)/entries/(M-[0-9]{1,7})$", path)
        if forgotten:
            return self._guarded(lambda: self._json(200, forget_memory(self._target(forgotten[1]), forgotten[2], body)))
        if path == "/api/cleanup":
            def clean():
                items = body.get("items")
                if not isinstance(items, list) or not items:
                    raise Refused("nothing selected")
                results = []
                for item in items:
                    ticket_id = str((item or {}).get("id", ""))
                    try:
                        results.append({"ok": True, **cleanup.clean(self._ticket(str(item.get("target", "")), ticket_id), ticket_id)})
                    except (cleanup.CleanupError, GitError, LookupError) as exc:
                        results.append({"ok": False, "id": ticket_id, "error": str(exc)})
                self._json(200, {"results": results})
            return self._guarded(clean)
        if path == "/api/projects":
            return self._guarded(lambda: self._json(200, create_project(body)))
        talked = re.match(r"^/api/talk/([a-z0-9-]+)(?:/(\d{8}-\d{6}))?$", path)
        if talked:
            return self._guarded(lambda: self._json(200, talk_post(self._target(talked[1]), talked[2], body)))
        adopted = re.match(r"^/api/targets/([a-z0-9-]+)/pulls$", path)
        if adopted:
            return self._guarded(lambda: self._json(200, adopt_pull(self._target(adopted[1]), body)))
        routed = re.match(r"^/api/targets/([a-z0-9-]+)/route$", path)
        if routed:
            return self._guarded(lambda: self._json(200, project.route(self._target(routed[1]), str(body.get("text", "")))))
        drafted = re.match(r"^/api/targets/([a-z0-9-]+)/roadmap/(draft|save)$", path)
        if drafted:
            return self._guarded(lambda: self._json(200, roadmap_post(self._target(drafted[1]), drafted[2], body)))
        if path == "/api/targets/detect":
            return self._guarded(lambda: self._json(200, onboard.detect(str(body.get("path", "")))))
        if path == "/api/onboard":
            # Ajanla doldur (agent: true) and Doğrula: a background job on the form's current lines.
            return self._guarded(lambda: self._json(200, onboard_agent.start_request(
                str(body.get("path", "")), body.get("proposal"), body.get("agent") is True, str(body.get("name") or "") or None)))
        if path == "/api/onboard/agents-md":
            return self._guarded(lambda: self._json(200, agents_md_pr(body)))
        if path == "/api/targets":
            def add():
                spec = body.get("spec")
                if not isinstance(spec, dict):
                    raise ValueError("spec must be an object")
                backup = onboard.add(spec)
                self._json(200, {"added": spec["name"], "backup": str(backup)})
            return self._guarded(add)
        purged = re.match(r"^/api/leftovers/([a-z0-9-]+)/purge$", path)
        if purged:
            def purge():
                try:
                    self._json(200, onboard.purge(purged[1], force=body.get("force") is True))
                except ConfigError as exc:  # uncommitted changes: the panel asks again
                    raise Refused(str(exc)) from None
            return self._guarded(purge)
        removed = re.match(r"^/api/targets/([a-z0-9-]+)/remove$", path)
        if removed:
            # "Ziggy'den çıkar": the config table only; the repository and the target's state stay.
            return self._guarded(lambda: self._json(200, {"removed": removed[1], "backup": str(onboard.remove(removed[1], busy_tickets))}))
        self._error(HTTPStatus.NOT_FOUND, "not found")


class Panel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, sync_every: int | None = None, attention_every: int | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.started_at = time.time()
        self.activity = Activity()
        self.sessions = Sessions()
        self.sync_info = {"last": None, "error": None}
        if sync_every:
            threading.Thread(target=self._sync_loop, args=(sync_every,), daemon=True, name="pr-sync").start()
        if attention_every:
            threading.Thread(target=self._attention_loop, args=(attention_every,), daemon=True, name="attention").start()

    def _sync_loop(self, every: int) -> None:
        """Follow PRs on GitHub: merged or closed tickets get archived and their worktrees cleaned; the open ones'
        new review comments, conversation and failing checks land in their feedback.json (and the inbox)."""
        time.sleep(5)
        while True:
            errors = []
            for name in config.target_names():
                try:
                    target = config.load_target(name)
                    for row in issues.sync(target):  # labelled GitHub issues → tickets (issue_label)
                        sys.stderr.write(f"{state.now()} issues {name} {row['id']} from #{row['number']}\n")
                    for result in cleanup.sync(target) + feedback.sync(target):
                        sys.stderr.write(f"{state.now()} pr-sync {name} {result}\n")
                        if isinstance(result, dict) and result.get("error"):  # one ticket's PR (e.g. no gh token): the panel says so
                            errors.append(f"{name} {result.get('id') or ''}: {result['error']}".replace(" :", ":"))
                except Exception as exc:  # noqa: BLE001 - keep following the other targets
                    errors.append(f"{name}: {exc}")
            shown = list(dict.fromkeys(errors))
            self.sync_info = {"last": state.now(), "error": "; ".join(shown[:3]) + (f" (+{len(shown) - 3})" if len(shown) > 3 else "")
                              if shown else None}
            time.sleep(every)

    def _attention_loop(self, every: int) -> None:
        """Keep the inbox ledger current: the engine records what it settles, but only this loop sees a
        session go quiet, a run's process vanish or a panel job fail. Notifications go out from here too. It also
        starts waiting tickets whose dependencies are done (the engine does at once; this covers a restart), tickets
        held for a usage limit whose window reset (quota.wake), a ticket held for the machine's memory once it has been
        calm for two ticks (host.wake) and each target's lead once a day (lead.tick)."""
        while True:
            try:
                targets = _targets()
                for target in targets:
                    stack.wake(target)
                    quota.wake(target)  # tickets held for a usage limit whose window reset (also after a restart)
                    budget.wake(target)  # tickets held for yesterday's spending cap
                    answer_due(target)  # question rounds past the target's answer_after: the recommendations, then run
                lead.tick(targets)  # today's lead of each target whose time came (also the first tick after a sleep)
                weekly.tick(targets)  # Monday 09:00 on: last week's report, once (a "report" notification)
                host.wake(targets)  # the ticket held longest while the machine was short of memory, after two calm ticks
                attention.reconcile(targets)
                notify.flush_due()
            except Exception as exc:  # noqa: BLE001 - the inbox must not take the panel down
                sys.stderr.write(f"{state.now()} attention: {exc!r}\n")
            time.sleep(every)


def answer_due(target: Target) -> list[str]:
    """Question rounds open past the target's `answer_after`: answered with their recommendations, and the run continues
    as a panel job. Returns the ticket ids it answered."""
    if target.answer_after is None:
        return []
    answered = []
    for ticket_id in state.ticket_ids(target):
        try:
            if pipeline.answer_by_default(target, ticket_id):
                start_job(target, ticket_id, "run", ticket_id)
                answered.append(ticket_id)
        except (OSError, ValueError, KeyError, state.LockError) as exc:
            sys.stderr.write(f"{state.now()} answer_after {target.name}/{ticket_id}: {exc!r}\n")
    return answered


SYNC_EVERY = 180  # seconds between PR checks on GitHub
ATTENTION_EVERY = 60  # seconds between inbox reconciles (quiet, lost, job errors; notifications)


def serve(port: int) -> None:
    for attempt in range(30):
        try:
            panel = Panel(port, sync_every=SYNC_EVERY, attention_every=ATTENTION_EVERY)
            break
        except OSError:  # the previous instance is still letting go of the port
            if attempt == 29:
                raise
            time.sleep(1)
    print(f"Ziggy panel: http://127.0.0.1:{panel.server_address[1]}/", flush=True)
    try:
        panel.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        panel.server_close()
