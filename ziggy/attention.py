"""What needs you, per ticket, and the inbox ledger behind the panel's board, its unread count and the
macOS notifications.

`needs` derives the one thing a ticket needs from you now (a question, an approval, a failure, …) from
its state files; every item carries a `key` naming its occurrence (this round, this run's failure, this
job), so closing or reading one says nothing about the next. A ticket shows the first item you have not
closed; closed keys live in `runs/<id>/dismissed.json` for good (only failures, lost runs, job errors,
publish/push reminders, merged-kept worktrees and PR feedback can be closed: questions and approvals need
an answer, a quiet session needs a look). A running session whose tool call runs past the target's `stall_after`
(stalls.py) shows `stall` ("Komut 12 dk’dır sürüyor: <komut> · bellek kritik") until the call returns or the stage
ends. PR feedback (feedback.py) shows while items the panel's PR sync
read on GitHub wait to be sent; its key names the newest item, so a later comment shows again. In a controlled
target a waiting ticket whose dependencies are all done asks to be started (`start`: stack.wake starts nothing there).

The ledger, `state_home()/attention.json`, holds one row per occurrence: {key, target, ticket, kind,
severity, title, details, ticket_title, created_at, seen_at, dismissed_at, resolved_at, notified_at}.
Besides the tickets, each target has one row for its project memory (memory.py), under the pseudo-ticket MEMORY:
"N yeni hafıza önerisi" while candidates wait (its key names the newest candidate, so a new batch is a new, unread
row and deciding some of them is not), else "Hafıza dosyasında N sorun" while a memory file cannot be fully read.
Each target also has one row for the lead's proposals (lead.py), under the pseudo-ticket LEAD: "N yeni iş önerisi"
while proposals wait, keyed by the newest one.
Each Claude login over its usage limit (quota.py) has one row, under the pseudo-ticket QUOTA of the first target that
uses the login: "Kota doldu: <hesap> · HH:MM", keyed by the login and its reset, resolved once the window resets.
`reconcile` (the panel, every 60 s) and `raise_for` (the engine, at every status change it saves) add a
row for each current item that has no active row, refresh an active row's text, and mark rows whose
item is no longer current resolved; an item that comes back after it was resolved is a new, unread
occurrence. New rows are notified (notify.send) unless the target is reconciled for the first time:
that pass records what is already there without a burst. Rows resolved more than 30 days ago are
dropped. Engine processes and the panel all write the ledger, so every change happens under a
machine-wide lock with an atomic replace.
"""
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cleanup, config, dispatch, feedback, lead, memory, notify, pipeline, progress, protected, quota, roadmap, stack, state
from . import host, stalls
from .config import Target

SEVERITY = {"question": "high", "approval": "high", "lost": "high", "error": "high",
            "failed": "medium", "job-error": "medium", "quiet": "medium", "stall": "medium", "pr-feedback": "medium",
            "blocked": "medium", "restack": "medium", "start": "medium", "quota": "medium", "publish": "low", "push": "low",
            "merged-kept": "low", "memory": "low", "lead": "low", "protected": "medium"}
RANK = {"high": 0, "medium": 1, "low": 2}
DISMISSABLE = frozenset({"failed", "lost", "job-error", "publish", "push", "merged-kept", "pr-feedback", "blocked", "restack"})
LABEL = {  # the notification text, as the panel names each kind (pr-feedback's own title says it: "PR'da 2 yeni yorum")
    "question": "Ajanın soruları var", "approval": "Plan onayını bekliyor", "publish": "Bitti, PR açılmadı",
    "failed": "Koşu başarısız oldu", "job-error": "Komut hata verdi", "error": "Ticket state'i okunamıyor",
    "push": "Dal PR'dan sonra değişti", "merged-kept": "PR kapandı ama worktree'de iş kaldı",
    "quiet": "Oturum bir süredir sessiz", "stall": "Komut uzun sürüyor", "lost": "Koşunun süreci ölmüş",
    "memory": "Yeni hafıza önerisi",
    "blocked": "Beklediği ticket takıldı", "restack": "Yeniden düzenleme gerekiyor", "start": "Başlatılmayı bekliyor",
    "quota": "Kota doldu", "lead": "Yeni iş önerisi", "protected": "Korumalı dosya değişikliği",
}
PR_STATE = {"MERGED": "birleştirildi", "CLOSED": "kapandı"}  # cleanup.CLEANABLE, as the titles say it
DEP_WHY = {"failed": "başarısız oldu", "closed": "PR'ı birleşmeden kapandı", "archived": "bitmeden arşivlendi",
           "missing": "bulunamıyor"}  # stack.dependency's reasons, as the titles say them
UNREADABLE = LABEL["error"]
# waiting_on_others: tickets that wait for other tickets (dependencies) and split tickets whose slices are open.
GROUPS = ("waiting_on_you", "ready_for_review", "working", "waiting_on_others", "landing")
WAITING = frozenset({"question", "approval", "failed", "lost", "job-error", "error", "pr-feedback", "blocked", "restack",
                     "start", "protected"})
LANDING = frozenset({"push", "merged-kept"})
# Statuses a finishing run saves before letting go.
SETTLED = ("needs-user", "awaiting-approval", "failed", "done", stack.WAITING, stack.SPLIT, quota.STATUS, host.STATUS)
LEDGER = "attention.json"
DISMISSED = "dismissed.json"
KEEP_RESOLVED = timedelta(days=30)
RECENT = timedelta(hours=24)
DETAILS_MAX = 2000
MEMORY = "memory"  # the pseudo-ticket of a target's memory row (no ticket id looks like it)
QUOTA = "quota"  # the pseudo-ticket of a Claude login's usage-limit row, under the first target that uses the login
LEAD = "lead"  # the pseudo-ticket of a target's lead proposals row


class Refused(Exception):
    """A dismissal the ticket's current item does not allow; shown to the user as is."""


# --- deriving the current item ---------------------------------------------------------------

def need(kind: str, title: str, *, key: str, **extra) -> dict:
    return {"kind": kind, "severity": SEVERITY[kind], "title": title, "key": key, "dismissable": kind in DISMISSABLE, **extra}


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _run_record(phase_dir: Path) -> dict:
    try:
        return state.read_json(phase_dir / "run.json")
    except (OSError, ValueError):
        return {}


def last_job(target: Target, ticket_id: str) -> dict | None:
    """The ticket's newest panel job (runs/<id>/jobs/<stamp>-<command>/{job.json, exit, out.log}):
    {id, command, pid, exit, state (running | lost | ok | error), tail}."""
    root = state.run_dir(target, ticket_id) / "jobs"
    for job_dir in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            meta = state.read_json(job_dir / "job.json")
        except (OSError, ValueError):
            continue
        code = None
        if (job_dir / "exit").exists():
            try:
                code = int((job_dir / "exit").read_text().strip())
            except ValueError:
                code = -1
        alive = code is None and state._pid_alive(meta.get("pid", 0))
        return {"id": job_dir.name, "command": meta["command"], "pid": meta.get("pid"), "exit": code,
                # 3 = the run stopped to wait for a human (a question or an approval): not an error.
                "state": "running" if alive else ("lost" if code is None else ("ok" if code in (0, 3) else "error")),
                "tail": _tail(job_dir / "out.log", 1500)}
    return None


def _needs_push(st: dict) -> bool:
    """The branch moved on since the PR was last pushed (review fixes built on it)."""
    if not (st.get("pr") and st.get("published_head") and st.get("worktree") and Path(st["worktree"]).is_dir()):
        return False
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=st["worktree"], capture_output=True, text=True).stdout.strip()
    return bool(head) and head != st["published_head"]


def _pr_feedback(waiting: list[dict]) -> dict:
    """PR feedback not sent to the agent yet: 'PR'da CI kırmızı', 'PR'da N yeni yorum' or both. The key names the
    newest item, so a comment that comes after a close shows again."""
    ci = [i for i in waiting if i.get("kind") == "ci"]
    comments = len(waiting) - len(ci)
    title = " · ".join((["PR'da CI kırmızı"] if ci else []) + ([f"PR'da {comments} yeni yorum"] if comments else []))
    newest = max(waiting, key=lambda i: (i.get("created_at") or "", i["id"]))
    details = "\n".join(f"CI: {i.get('author')} başarısız" if i.get("kind") == "ci"
                        else f"@{i.get('author')}: {' '.join(str(i.get('body') or '').split())[:160]}" for i in waiting[-5:])
    return need("pr-feedback", title, key=f"pr-feedback:{newest['id']}", count=len(waiting), ci=len(ci), details=details)


def dismissed(target: Target, ticket_id: str) -> set[str]:
    try:
        return set(state.read_json(state.run_dir(target, ticket_id) / DISMISSED))
    except (OSError, ValueError, TypeError):
        return set()


def _dep_why(why: str | None) -> str:
    if why and why.startswith("slice "):
        return f"dilimi {why.split(' ', 1)[1]} takıldı"
    return DEP_WHY.get(why or "", why or "takıldı")


def start_title(t: dict) -> str:
    """`start`'s title: "local-3, local-5 bitti; local-4 başlasın mı?" (no dependencies left: just the question)."""
    after = t.get("after") or []
    return f"{', '.join(after)} bitti; {t['id']} başlasın mı?" if after else f"{t['id']} başlasın mı?"


def start_key(t: dict) -> str:
    return "start:" + (",".join(t.get("after") or []) or "-")


def _stall(phase_dir: Path, stall: dict) -> dict:
    """A tool call running past stall_after (stalls.Watch put it in run.json): "Komut 12 dk’dır sürüyor: <komut> ·
    bellek kritik", with the machine as it is now (else as it was when the call stalled). Resolved when the call
    returns or the stage ends; nothing is stopped for it."""
    since = stall.get("since")
    try:
        took = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds()
    except (TypeError, ValueError):
        took = stall.get("seconds") or 0
    took = max(took, stall.get("seconds") or 0)
    now = host.sample()
    s = now if now.get("pressure") != "unknown" or host.swap_share(now) is not None else (stall.get("host") or now)
    tail = host.suffix(s)
    command = stall.get("command") or stall.get("tool") or "?"
    return need("stall", f"Komut {round(took / 60)} dk’dır sürüyor: {stalls.clip(command, 60)}" + (f" · {tail}" if tail else ""),
                key=f"stall:{phase_dir.name}:{stall.get('id')}", minutes=round(took / 60),
                details=f"Ajan bu komutun bitmesini bekliyor ({stall.get('tool')}): {command}\n"
                        f"Makine: {host.text(s)}" + (f" · yük {s['load1']:.1f}" if s.get("load1") is not None else "")
                        + "\nZiggy komutu durdurmaz: takıldıysa ajana yön ver ya da durdur.")


def _candidates(target: Target, t: dict, st: dict, running: bool, job: dict | None, dirs: list[Path]):
    if running:
        stall = _run_record(dirs[-1]).get("stall") if dirs else None
        if stall:
            yield _stall(dirs[-1], stall)
        quiet = dispatch.quiet_for(dirs[-1], pipeline.quiet_after(target, st)) if dirs else None
        if quiet is not None:
            # Long tool calls are not counted (quiet_for); this asks for a look, not a stop.
            last = progress.read_lines(dirs[-1], 0, progress.Describer("", brief=True))[0][-3:]
            yield need("quiet", f"{int(quiet // 60)} dakikadır ajandan olay yok", key=f"quiet:{dirs[-1].name}",
                       minutes=int(quiet // 60), details="\n".join(last))
        return
    run_n = (st.get("run") or {}).get("n") or "legacy"
    if state.lock_left(target, t["id"]):
        last = dirs[-1] if dirs else None
        details = _tail(last / "claude.err", 800) if last else ""
        yield need("lost", "Koşunun süreci ölmüş", key=f"lost:{last.name if last else '-'}",
                   details=details or "süreç sonuç yazmadan bitti")
    status = st["status"]
    if job and job["state"] == "error" and job["exit"] == 2:
        yield need("job-error", f"`{job['command']}` komutu hata verdi", key=f"job-error:{job['id']}", details=job["tail"])
    if status == stack.WAITING:
        deps = stack.dependencies(target, t)
        stuck = [d for d in deps if d["state"] == "blocked"]
        controlled = target.mode == "controlled"
        if stuck:
            said = "; ".join(f"{d['id']} {_dep_why(d['why'])}" for d in stuck)
            then = "bitince başlatmak için sorulur." if controlled else "bitince bu ticket kendiliğinden başlar."
            yield need("blocked", f"Bekliyor ama {said}", key="blocked:" + ",".join(f"{d['id']}={d['why']}" for d in stuck),
                       details=f"{t['id']} başlamadı: bağımlı olduğu {said}. Onu düzelt (yeniden koştur ya da yeni "
                               f"ticket aç); {then}", waiting_for=stuck)
        elif controlled and all(d["state"] == "done" for d in deps):
            yield need("start", start_title(t), key=start_key(t),
                       details=f"Kontrollü modda bağımlı ticket'lar kendiliğinden başlamaz: {t['id']} senin onayınla başlar.")
    restack = st.get("restack")
    if restack:
        rebased = bool(restack.get("rebased"))
        result = restack.get("result") or {}
        details = (f"{restack.get('below') or 'alttaki dilim'} squash/rebase ile birleşti; bu dal onun eski commit'lerini "
                   f"taşıyor. {restack.get('onto')} üstüne yeniden düzenlenmeli (git rebase --onto)."
                   if restack.get("reason") == "squash" else
                   f"Alttaki dal ({restack.get('below')}) yeniden düzenlendi; bu dal onun üstüne taşınmalı.")
        if result and not result.get("ok"):
            details += "\n\nSon deneme çakıştı: " + (", ".join(result.get("conflicts") or []) or (result.get("output") or "")[-400:])
        yield need("restack", "Yeniden düzenlenen dal push bekliyor" if rebased else "Yeniden düzenleme gerekiyor",
                   key=f"restack:{str(restack.get('upstream') or '-')[:12]}:{'push' if rebased else 'rebase'}",
                   details=details, restack=restack)
    if status == "needs-user":
        open_round = pipeline.open_round(st)
        if open_round:
            n, stage = len(open_round.get("questions") or []), open_round.get("stage")
            title = f"{stage} adımının {n} sorusu var" if stage else f"Ajanın {n} sorusu var"
            if open_round.get("intent_gap"):
                title = f"Niyet sapması: {stage or 'ajan'} {n} soru soruyor"
            yield need("question", title,
                       key=f"question:{open_round.get('n') or len(st.get('rounds') or [])}",
                       round=open_round, details=open_round.get("context") or "")
    if status == "awaiting-approval":
        gate = st.get("gate") or {}
        yield need("approval", f"{gate['stage']} adımı onayını bekliyor" if gate.get("stage") else "Plan onayını bekliyor",
                   key=f"approval:{run_n}:{gate.get('stage') or '-'}", gate=gate, details=gate.get("reason") or "")
    changes = [_protected(target, t["id"], st, p) for p in protected.pending(st)]
    if status != "failed":
        yield from changes
    if status == "done" and not st.get("pr"):
        yield need("publish", "Bitti, PR açılmadı", key=f"publish:{run_n}", details=f"{t['branch']} → {target.github_repo or '?'}")
    if st.get("pr") and not st.get("archived") and status in ("done", "failed"):
        waiting = feedback.pending(target, t["id"])
        if waiting:
            yield _pr_feedback(waiting)
    if st.get("pr_state") in cleanup.CLEANABLE and st.get("cleanup_blocked") and not st.get("archived"):
        yield need("merged-kept", f"PR {PR_STATE[st['pr_state']]} ama worktree'de hâlâ iş var", key=f"merged-kept:{st['pr_state']}",
                   details="\n".join(st["cleanup_blocked"]) + (f"\n{st['worktree']}" if st.get("worktree") else ""))
    if status == "done" and _needs_push(st):
        yield need("push", "Dal PR'dan sonra değişti", key=f"push:{st.get('published_head') or '-'}",
                   details=f"{t['branch']} → {st['pr']}")
    if status == "failed":
        details = st.get("error") or ""
        if dirs:
            last = _run_record(dirs[-1])
            tail = "\n".join(x for x in (last.get("result_error") or "", (last.get("final_message") or "")[-1500:],
                                         _tail(dirs[-1] / "claude.err", 800)) if x)
            details = "\n\n".join(x for x in (details, tail) if x)
        elif job:
            details = details or job["tail"]
        yield need("failed", "Koşu başarısız oldu", key=f"failed:{run_n}:{dirs[-1].name if dirs else '-'}", details=details)
        yield from changes


def _protected(target: Target, ticket_id: str, st: dict, p: dict) -> dict:
    """A pending patch of a protected file: its diff on the card, Uygula / Reddet (protected.py); `replaces` names the
    applied patches of the same files it was prepared without (applying it supersedes them)."""
    files = ", ".join(p.get("files") or [])
    return need("protected", f"Korumalı dosya değişikliği: {files}", key=f"protected:{p['n']}", details=p.get("summary") or "",
                patch={"n": p["n"], "files": p.get("files") or [], "summary": p.get("summary") or "", "stage": p.get("stage"),
                       "created_at": p.get("created_at"), "replaces": protected.replaces(target, ticket_id, st, p),
                       "diff": protected.diff_text(target, ticket_id, p)})


def _pick(target: Target, t: dict, st: dict, running: bool, job: dict | None, dirs: list[Path],
          closed: set[str] | None = None) -> dict | None:
    """The first item you have not closed, without ledger fields."""
    closed = dismissed(target, t["id"]) if closed is None else closed
    return next((n for n in _candidates(target, t, st, running, job, dirs) if n["key"] not in closed), None)


def needs_from(target: Target, t: dict, st: dict, running: bool, last_job: dict | None, dirs: list[Path]) -> dict | None:
    """`needs` from what the caller has read already (the panel's ticket row): `running` includes a live
    job, `last_job` has at least id, command, state, exit and tail. Adds the ledger's created_at and
    seen_at (None before the first reconcile, which counts as unread)."""
    item = _pick(target, t, st, running, last_job, dirs)
    if item is None:
        return None
    row = _active_rows(_cached_ledger()).get((target.name, t["id"], item["key"]))
    seen = row["seen_at"] if row else None
    return {**item, "created_at": row["created_at"] if row else None, "seen_at": seen, "unread": seen is None}


def needs(target: Target, ticket_id: str) -> dict | None:
    """The one thing this ticket needs from you now, or None. Raises what reading its state raises."""
    t = state.load_ticket(target, ticket_id)
    st = state.load_state(target, ticket_id)
    job = last_job(target, ticket_id)
    running = state.running(target, ticket_id) or bool(job and job["state"] == "running")
    return needs_from(target, t, st, running, job, state.phase_dirs(target, ticket_id))


def _ours(pid) -> bool:
    return pid in (os.getpid(), os.getppid())  # the engine itself, or the panel's sh wrapper around it


SKIP = object()  # _current: a ticket the ledger leaves as it is this time


def _current(target: Target, ticket_id: str, *, finishing: bool = False):
    """(ticket title, current item) for the ledger; `SKIP` when the ticket is between states.

    `finishing`: the caller is the engine run of this ticket that just saved a status, so its own lock
    and job do not count as running. Seen from another process, a live run whose status is already a
    settled one is just letting go: skipping it keeps its row instead of resolving and re-raising it."""
    try:
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        job = last_job(target, ticket_id)
        holder = state.holder(target, f"ticket-{ticket_id}")
        self_lock = bool(holder) and _ours(int(holder.split()[0]))
        if finishing and job and job["state"] == "running" and _ours(job["pid"]):
            job = None
        running = (state.running(target, ticket_id) and not (finishing and self_lock)) or bool(job and job["state"] == "running")
        if running and not finishing and st.get("status") in SETTLED:
            return SKIP
        return t.get("title") or "", _pick(target, t, st, running, job, state.phase_dirs(target, ticket_id))
    except (OSError, LookupError, ValueError, TypeError) as exc:
        return "", need("error", UNREADABLE, key="error", details=str(exc))


# --- the ledger ------------------------------------------------------------------------------

def _path() -> Path:
    return config.state_home() / LEDGER


def _load() -> dict:
    try:
        data = state.read_json(_path())
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return {"baselined": list(data.get("baselined") or []), "rows": [r for r in data["rows"] if isinstance(r, dict)]}
    except (OSError, ValueError):
        pass
    return {"baselined": [], "rows": []}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    state.write_json(_path(), data)


_cache: tuple[tuple, dict] | None = None


def _cached_ledger() -> dict:
    """The ledger for read-only callers (the panel reads it for every ticket row), re-read when it changed."""
    global _cache
    try:
        st = _path().stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return {"baselined": [], "rows": []}
    if _cache is None or _cache[0] != stamp:
        _cache = (stamp, _load())
    return _cache[1]


def _locked():
    return notify.state_lock("attention", "attention ledger")


def _active_rows(data: dict) -> dict:
    return {(r["target"], r["ticket"], r["key"]): r for r in data["rows"] if r.get("resolved_at") is None}


def _age(stamp: str | None, now: datetime) -> timedelta:
    try:
        return now - datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return timedelta(0)


def _apply(data: dict, target_name: str, current: dict, *, full: bool, notify_tickets=None) -> list[dict]:
    """Merge one target's current items ({ticket: (title, item|None) or SKIP}) into `data`; a skipped
    ticket's rows stay as they are. `full`: every ticket of the target was looked at, so rows of the others
    (deleted tickets) resolve too. Returns the new rows to notify."""
    now = state.now()
    baselined = target_name in data["baselined"]
    active = {(tk, key): r for (tn, tk, key), r in _active_rows(data).items() if tn == target_name}
    wanted = {(tk, cur[1]["key"]): (tk, *cur) for tk, cur in current.items() if cur is not SKIP and cur[1]}
    for (tk, key), row in active.items():
        if (tk, key) not in wanted and current.get(tk) is not SKIP and (full or tk in current):
            row["resolved_at"] = now
    added = []
    for ident, (tk, title, item) in wanted.items():
        text = {"kind": item["kind"], "severity": item["severity"], "title": item["title"],
                "details": (item.get("details") or "")[-DETAILS_MAX:], "ticket_title": title}
        row = active.get(ident)
        if row is not None:
            row.update(text)
            continue
        # A resolved occurrence of the same key that comes back is a fresh one: the old row gives way.
        data["rows"] = [r for r in data["rows"] if (r["target"], r["ticket"], r["key"]) != (target_name, *ident)]
        row = {"key": item["key"], "target": target_name, "ticket": tk, **text, "created_at": now,
               "seen_at": None, "dismissed_at": None, "resolved_at": None, "notified_at": None}
        data["rows"].append(row)
        if baselined or (notify_tickets is not None and tk in notify_tickets):
            added.append(row)
    if full and not baselined:
        data["baselined"].append(target_name)
    return added


def _prune(data: dict) -> None:
    now = datetime.now(timezone.utc)
    data["rows"] = [r for r in data["rows"] if r.get("resolved_at") is None or _age(r["resolved_at"], now) <= KEEP_RESOLVED]


def _commit(changes: list[tuple[str, dict, bool, set | None]], *, send: bool = True) -> None:
    """Apply [(target name, current, full, notify_tickets)] under the lock, then notify outside it."""
    with _locked():
        data = _load()
        added = [row for name, current, full, only in changes for row in _apply(data, name, current, full=full, notify_tickets=only)]
        _prune(data)
        _save(data)
    if not send:
        return
    shown = []
    for row in added:
        if notify.wants(row["kind"]) and notify.send(
                f"{row['ticket']} · {row['target']}",
                f"{LABEL.get(row['kind'], row['title'])}: {row['ticket_title'] or row['title']}",
                key=f"{row['target']}/{row['ticket']}/{row['key']}"):
            shown.append((row["target"], row["ticket"], row["key"], row["created_at"]))
    if shown:
        with _locked():
            data = _load()
            stamp = state.now()
            for r in data["rows"]:
                if (r["target"], r["ticket"], r["key"], r["created_at"]) in shown:
                    r["notified_at"] = stamp
            _save(data)


def _target_items(target: Target, ticket_ids, finishing: str | None = None) -> dict:
    return {tk: _current(target, tk, finishing=tk == finishing) for tk in ticket_ids}


def memory_current(target: Target):
    """(title, item) for the target's memory: its pending candidates, else what its files' parser warned about
    (roadmap.md too), else (title, None)."""
    data = memory.load_candidates(target)
    waiting = [c for c in data["candidates"] if c.get("status") == "pending"]
    if waiting:
        newest = max((c["id"] for c in data["candidates"]), key=lambda i: int(i[1:]))
        return "Hafıza önerileri", need("memory", f"{len(waiting)} yeni hafıza önerisi", key=f"memory:{newest}",
                                        details="\n".join(f"- {c['kind']}: {c['title']}" for c in waiting[:10]))
    warnings = memory.entries(target)[1] + roadmap.warnings(target)
    if warnings:
        digest = hashlib.sha256("\n".join(warnings).encode()).hexdigest()[:10]
        return "Hafıza", need("memory", f"Hafıza dosyasında {len(warnings)} sorun", key=f"memory-warn:{digest}",
                              details="\n".join(f"- {w}" for w in warnings[:10]))
    return "Hafıza önerileri", None


def memory_changed(target: Target, *, send: bool = True) -> None:
    """The target's candidates changed (a retro or an answer stored some, or the user decided one): bring its row up
    to date, notifying a new batch when `send`. Never raises."""
    try:
        _commit([(target.name, {MEMORY: memory_current(target)}, False, {MEMORY})], send=send)
    except Exception as exc:  # noqa: BLE001 — never fail the caller over the inbox
        _log(exc)


def lead_current(target: Target):
    """(title, item) for the target's lead: 'N yeni iş önerisi' while proposals wait, else (title, None)."""
    waiting = lead.pending(target)
    if not waiting:
        return "İş önerileri", None
    newest = max((p["id"] for p in waiting), key=lambda i: int(i[1:]))
    return "İş önerileri", need("lead", f"{len(waiting)} yeni iş önerisi", key=f"lead:{newest}",
                                details="\n".join(f"- {p['title']}" for p in waiting[:10]))


def lead_changed(target: Target, *, send: bool = True) -> None:
    """The target's proposals changed (a lead run added some, or one was decided): bring its row up to date. Never
    raises."""
    try:
        _commit([(target.name, {LEAD: lead_current(target)}, False, {LEAD})], send=send)
    except Exception as exc:  # noqa: BLE001 — never fail the caller over the inbox
        _log(exc)


def quota_current(target: Target, targets) -> tuple:
    """(title, item) for the usage limit of the target's Claude login, shown under the first of `targets` that uses
    the login (one row per login): 'Kota doldu: <hesap> · HH:MM' while it is blocked, resolved when it resets."""
    key = quota.account_key(target)
    owner = next((t for t in targets if quota.account_key(t) == key), target)
    if owner.name != target.name:
        return "Kota", None
    blocked = quota.blocked(key)
    if not blocked:
        return "Kota", None
    label = blocked.get("label") or quota.label_for(key)
    until = quota.clock(blocked["blocked_until"])
    users = [t.name for t in targets if quota.account_key(t) == key]
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return "Kota", need("quota", f"Kota doldu: {label} · {until}", key=f"quota:{digest}:{blocked['blocked_until']}",
                        account=key, details=f"{label} hesabının kullanım sınırı doldu ({', '.join(users)}); "
                                             f"{quota.locative(until)} sıfırlanır. Bekleyen ticket'lar o zaman kendiliğinden devam eder.")


def _config_targets() -> list[Target]:
    out = []
    for name in config.target_names():
        try:
            out.append(config.load_target(name))
        except config.ConfigError:
            continue
    return out


def quota_changed() -> None:
    """A session hit a Claude login's limit (the engine): bring every target's login row up to date and notify it.
    Never raises."""
    try:
        targets = _config_targets()
        _commit([(t.name, {QUOTA: quota_current(t, targets)}, False, {QUOTA}) for t in targets])
    except Exception as exc:  # noqa: BLE001 — never fail the caller over the inbox
        _log(exc)


def _log(exc: Exception) -> None:
    print(f"attention: {exc}", file=sys.stderr)


def reconcile(targets) -> None:
    """Bring every ticket of these targets into the ledger (the panel, every 60 s). Never raises."""
    for target in targets:
        try:
            _commit([(target.name, {**_target_items(target, state.ticket_ids(target)), MEMORY: memory_current(target),
                                    QUOTA: quota_current(target, targets), LEAD: lead_current(target)}, True, None)])
        except Exception as exc:  # noqa: BLE001 — one target's trouble must not stop the panel's loop
            _log(exc)


def raise_for(target: Target, ticket_id: str) -> None:
    """The engine saved a status for this ticket (its run may still hold the lock): reconcile its current
    item and notify it when new. A target never reconciled before is recorded whole first, silently
    except for this ticket. Never raises."""
    try:
        full = target.name not in _cached_ledger()["baselined"]
        ids = state.ticket_ids(target) if full else [ticket_id]
        current = _target_items(target, ids, finishing=ticket_id)
        if full:  # a whole-target pass resolves rows it does not list: the memory, login and lead rows are listed too
            current[MEMORY] = memory_current(target)
            current[QUOTA] = quota_current(target, _config_targets())
            current[LEAD] = lead_current(target)
        _commit([(target.name, current, full, {ticket_id})])
    except Exception as exc:  # noqa: BLE001 — never fail a run over the inbox
        _log(exc)


def raise_start(target: Target, ticket_id: str) -> None:
    """stack.wake found a controlled target's waiting ticket ready to start: record (and notify) its `start` item now
    rather than at the next reconcile. The panel wakes on every tick, so an item already in the ledger is left alone.
    Never raises."""
    try:
        key = start_key(state.load_ticket(target, ticket_id))
        if (target.name, ticket_id, key) not in _active_rows(_cached_ledger()):
            raise_for(target, ticket_id)
    except Exception as exc:  # noqa: BLE001 — never fail a wake over the inbox
        _log(exc)


def items(targets, *, all: bool = False) -> list[dict]:
    """Ledger rows of these targets, newest first: the open ones (not resolved, not closed); `all` adds
    the ones resolved or closed in the last 24 hours."""
    names = {t.name for t in targets}
    now = datetime.now(timezone.utc)
    out = []
    for r in _cached_ledger()["rows"]:
        if r["target"] not in names:
            continue
        ended = r.get("resolved_at") or r.get("dismissed_at")
        if ended and not (all and _age(ended, now) <= RECENT):
            continue
        out.append(dict(r))
    return sorted(out, key=lambda r: r["created_at"], reverse=True)


def unread(targets) -> int:
    names = {t.name for t in targets}
    return sum(1 for r in _cached_ledger()["rows"] if r["target"] in names and r.get("resolved_at") is None
               and r.get("dismissed_at") is None and r.get("seen_at") is None)


def seen(target: Target, ticket_id: str | None = None, *, keys=None) -> int:
    """Mark rows read: the open ones of this ticket (None: of the whole target), or with `keys` those
    keys of this ticket (resolved ones too). Returns how many changed."""
    if keys is not None and not ticket_id:
        raise ValueError("keys need a ticket")
    keys = set(keys) if keys is not None else None
    with _locked():
        data = _load()
        stamp, changed = state.now(), 0
        for r in data["rows"]:
            if r["target"] != target.name or (ticket_id and r["ticket"] != ticket_id) or r.get("seen_at"):
                continue
            wanted = r["key"] in keys if keys is not None else r.get("resolved_at") is None
            if not wanted:
                continue
            r["seen_at"] = stamp
            changed += 1
        if changed:
            _save(data)
    return changed


def dismiss(target: Target, ticket_id: str, key: str) -> dict:
    """Close the ticket's current item; only the one shown now, so a stale page cannot hide a newer one."""
    item = needs(target, ticket_id)
    if not item:
        raise Refused(f"{ticket_id}: kapatılacak bir şey yok")
    if not item["dismissable"]:
        raise Refused(f"{ticket_id}: “{LABEL.get(item['kind'], item['kind'])}” kapatılamaz, bir işlem bekliyor")
    if item["key"] != key:
        raise Refused(f"{ticket_id}: bu öğe bu arada değişti; sayfayı yenile")
    with _locked():
        state.write_json(state.run_dir(target, ticket_id) / DISMISSED, sorted(dismissed(target, ticket_id) | {key}))
        data = _load()
        row = _active_rows(data).get((target.name, ticket_id, key))
        if row is not None:
            row["dismissed_at"] = state.now()
            row["seen_at"] = row.get("seen_at") or row["dismissed_at"]
            _save(data)
    try:  # what shows next is recorded now; you are looking at it, so no notification
        _commit([(target.name, _target_items(target, [ticket_id]), False, None)], send=False)
    except Exception as exc:  # noqa: BLE001
        _log(exc)
    return {"dismissed": key, "needs_you": needs(target, ticket_id)}


# --- the home board --------------------------------------------------------------------------

def _group(row: dict) -> str | None:
    n = row.get("needs_you") or {}
    kind = n.get("kind")
    if kind in WAITING:
        return "waiting_on_you"
    if row.get("running") or kind in ("quiet", "stall"):
        return "working"
    if row.get("status") in (stack.WAITING, quota.STATUS, host.STATUS) or (row.get("status") == stack.SPLIT and row.get("split_open")):
        return "waiting_on_others"
    if kind == "publish":
        return "ready_for_review"
    if kind in LANDING or (row.get("status") == stack.SPLIT and row.get("split_landing")) \
            or (row.get("pr") and not row.get("archived") and row.get("pr_state") not in cleanup.CLEANABLE):
        return "landing"
    return None


def board_from(rows) -> dict:
    """The home board from ticket rows (target, id, status, running, run_started_at, pr, pr_state, archived,
    split_open, updated_at, needs_you): waiting_on_you (questions, approvals, failures, lost runs, job errors, a
    dependency that is stuck, a slice to restack), ready_for_review (done without a PR), working (running, quiet, a
    stalled tool call), waiting_on_others (waiting for the tickets it starts after, for its Claude login's usage limit
    to reset or for the machine to have memory again; a split ticket with open slices), landing (PR open, push,
    merged-kept; a split ticket whose slices are all done with PRs still open). Each entry: {target, id, kind, severity, key,
    unread, since}; tickets in none are left out."""
    out = {g: [] for g in GROUPS}
    for row in rows:
        group = _group(row)
        if group is None:
            continue
        n = row.get("needs_you") or {}
        out[group].append({"target": row["target"], "id": row["id"], "kind": n.get("kind"), "severity": n.get("severity"),
                           "key": n.get("key"), "unread": bool(n) and n.get("seen_at") is None,
                           "since": n.get("created_at") or row.get("updated_at") or "",
                           "_started": row.get("run_started_at") or "", "_updated": row.get("updated_at") or ""})
    out["waiting_on_you"].sort(key=lambda e: (RANK.get(e["severity"], 3), e["since"]))
    out["working"].sort(key=lambda e: (not e["_started"], e["_started"]))
    out["ready_for_review"].sort(key=lambda e: e["since"], reverse=True)
    out["waiting_on_others"].sort(key=lambda e: e["_updated"], reverse=True)
    out["landing"].sort(key=lambda e: e["_updated"], reverse=True)
    for entries in out.values():
        for e in entries:
            del e["_started"], e["_updated"]
    return out


def board(targets) -> dict:
    rows = []
    for target in targets:
        for ticket_id in state.ticket_ids(target):
            try:
                st = state.load_state(target, ticket_id)
                t = state.load_ticket(target, ticket_id)
                job = last_job(target, ticket_id)
                running = state.running(target, ticket_id) or bool(job and job["state"] == "running")
                item = needs_from(target, t, st, running, job, state.phase_dirs(target, ticket_id))
                view = stack.view(target, t, st) if st.get("status") == stack.SPLIT else {}
                split_open, split_landing = bool(view.get("split_open")), bool(view.get("split_landing"))
            except (OSError, LookupError, ValueError, TypeError) as exc:
                st, running, split_open, split_landing = {}, False, False, False
                item = need("error", UNREADABLE, key="error", details=str(exc))
            rows.append({"target": target.name, "id": ticket_id, "status": st.get("status"), "running": running,
                         "run_started_at": (st.get("run") or {}).get("started_at") if running else None,
                         "pr": st.get("pr"), "pr_state": st.get("pr_state"), "archived": st.get("archived"),
                         "split_open": split_open, "split_landing": split_landing, "updated_at": st.get("updated_at"), "needs_you": item})
    return board_from(rows)
