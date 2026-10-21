"""The user's review of a ticket's branch before a PR.

What changed comes from read-only git in the ticket worktree; draft comments, sent rounds and viewed
marks live in `runs/<id>/review/review.json`; AI findings, the reviewer's guide, implementer outcomes and
evidence are read live from the stage runs' `run.json` results. `submit` turns the drafts into a
`## Review (user, round N)` plan section; the caller (`ziggy request-changes`) hands it to
`pipeline.request_changes`, which starts the run.
"""
import getpass
import os
import re
import secrets
import subprocess
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import guard, plan, results, state, worktree
from .config import Target

SHA = re.compile(r"^[0-9a-f]{7,64}$")
BLOB_ID = re.compile(r"^[0-9a-f]{40,64}$|^deleted$")
STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied", "T": "typechange"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml"}
GENERATED = [re.compile(p) for p in (
    r"(^|/)(package-lock\.json|bun\.lockb?|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|Podfile\.lock|gradle\.lockfile|composer\.lock|uv\.lock|poetry\.lock)$",
    r"\.min\.(js|css)$",
    r"(^|/)(build|dist|generated|__generated__)/",
    r"\.(pb|g)\.\w+$",
    r"\.snap$",
)]
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
MAX_DIFF_LINES = 20000
MAX_BLOB = 10 * 1024 * 1024
MAX_BODY = 20000
MAX_COMMITS = 500
MAX_FIX = 200
GIT_TIMEOUT = 60
FINDING_CLASSES = ("important", "nit", "pre-existing")
OUTCOMES = ("fixed", "skipped", "no_change")
# What /api/files serves from inbox/artifacts, and the largest file of each kind it shows.
ARTIFACT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp", ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm",
                  ".mov": "video/quicktime", ".log": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                  ".md": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8"}
_MIB = 1024 * 1024
ARTIFACT_LIMITS = {**{e: 20 * _MIB for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")},
                   **{e: 200 * _MIB for e in (".mp4", ".webm", ".mov")},
                   **{e: 5 * _MIB for e in (".log", ".txt", ".md", ".json")}}
ARTIFACT_SEGMENT = re.compile(r"^[^/\x00-\x1f\x7f.][^/\x00-\x1f\x7f]{0,254}$")  # no dotfiles, no `.`/`..`
MAX_ARTIFACT_DEPTH = 8
MAX_WORK_SUMMARY = 4000


class ChangesError(ValueError):
    """A review request the ticket or its review state does not allow; shown to the user as is."""


class ArtifactTooLarge(ChangesError):
    """An evidence file over its kind's size cap (the panel answers 413)."""


# --- git -----------------------------------------------------------------------------------------

def _git(wt: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", "--literal-pathspecs", "-C", str(wt), *args], capture_output=True,
                          timeout=GIT_TIMEOUT, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if check and proc.returncode != 0:
        raise ChangesError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout if proc.returncode == 0 else b""


def _text(wt: Path, *args: str, check: bool = True) -> str:
    return _git(wt, *args, check=check).decode("utf-8", errors="replace")


def _rev(wt: Path, rev: str) -> str | None:
    out = _text(wt, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False).strip()
    return out if re.fullmatch(r"[0-9a-f]{40,64}", out) else None


# --- pure parsers --------------------------------------------------------------------------------

def parse_raw(out: str) -> list[dict]:
    """`git diff --raw -z --no-abbrev`: `:<mode> <mode> <sha> <sha> <status>\\0<path>\\0[<path>\\0]`."""
    parts = out.split("\0")
    entries, i = [], 0
    while i < len(parts):
        head = parts[i]
        i += 1
        if not head.startswith(":"):
            continue
        fields = head[1:].split(" ")
        old_sha, new_sha, code = (fields + ["", "", "", "", ""])[2:5]
        letter = code[:1]
        two = letter in ("R", "C")
        first = parts[i] if i < len(parts) else ""
        i += 1
        second = None
        if two:
            second = parts[i] if i < len(parts) else ""
            i += 1
        if not first:
            continue
        entries.append({
            "status": STATUS.get(letter, "modified"),
            "path": second if two else first,
            "old_path": first if two else None,
            "old_blob": old_sha if old_sha.strip("0") else None,
            "new_blob": new_sha if new_sha.strip("0") else None,
        })
    return entries


def parse_numstat(out: str) -> dict[str, tuple[int | None, int | None]]:
    """`git diff --numstat -z`: `<add>\\t<del>\\t<path>\\0`, renames `<add>\\t<del>\\t\\0<old>\\0<new>\\0`; `-` = binary."""
    parts = out.split("\0")
    stats, i = {}, 0
    while i < len(parts):
        m = re.match(r"^(-|\d+)\t(-|\d+)\t(.*)$", parts[i], re.S)
        i += 1
        if not m:
            continue
        path = m[3]
        if not path:
            i += 1  # old path
            path = parts[i] if i < len(parts) else ""
            i += 1
        if path:
            stats[path] = (None if m[1] == "-" else int(m[1]), None if m[2] == "-" else int(m[2]))
    return stats


def ext_of(path: str) -> str:
    return Path(path).suffix.lower() if "." in Path(path).name[1:] else ""


def is_image(path: str | None) -> bool:
    return bool(path) and ext_of(path) in IMAGE_TYPES


def is_generated(path: str) -> bool:
    return any(r.search(path) for r in GENERATED)


TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|specs?|e2e)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$|Tests?\.swift$")
SEVERITY_WEIGHT = {"critical": 8, "high": 5, "medium": 3, "low": 1}


def file_risk(f: dict, findings: list[dict]) -> tuple[int, list[str]]:
    """Where to look first, as (score, reasons): the file's open findings by severity, a file that runs in CI or steers
    agents (guard.SENSITIVE), a dependency manifest or lockfile, the size of the change; a test or generated file counts
    for a quarter."""
    from .pipeline import DEPENDENCY_FILE  # the engine imports this module's callers, not this module
    path, score, why = f["path"], 0, []
    open_ = [x for x in findings if x.get("path") == path and x.get("latest") and not x.get("fixed")
             and not x.get("dismissed") and x.get("disposition") not in ("dismiss", "defer")]
    if open_:
        score += sum(SEVERITY_WEIGHT.get(x.get("severity"), 3) for x in open_)
        why.append(f"{len(open_)} açık bulgu")
    if guard.sensitive(path):
        score += 6
        why.append("CI ya da ajan dosyası")
    if DEPENDENCY_FILE.search(path):
        score += 4
        why.append("bağımlılık")
    lines = (f.get("additions") or 0) + (f.get("deletions") or 0)
    if lines >= 50:
        score += min(lines // 50, 4)
        why.append(f"{lines} satır")
    if f.get("generated") or TEST_FILE.search(path):
        score //= 4
        why.append("üretilmiş" if f.get("generated") else "test")
    return score, why


def viewed_state(mark: str | None, new_blob: str | None) -> str:
    """"no" without a mark, "viewed" while the mark is the file's current blob, else "changed"."""
    if not mark:
        return "no"
    return "viewed" if mark == (new_blob or "deleted") else "changed"


def change_files(raw: list[dict], numstat: dict, viewed: dict) -> list[dict]:
    files = []
    for r in raw:
        additions, deletions = numstat.get(r["path"], (0, 0))
        files.append({
            **r,
            "additions": additions,
            "deletions": deletions,
            "binary": r["path"] in numstat and additions is None,
            "image": is_image(r["path"]) or is_image(r["old_path"]),
            "generated": is_generated(r["path"]),
            "viewed": viewed_state(viewed.get(r["path"]), r["new_blob"]),
        })
    return files


def parse_status(out: str) -> list[dict]:
    """`git status --porcelain=v1 -z`: XY, a space, the path; renames carry the old path next."""
    parts = out.split("\0")
    files, i = [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy = entry[:2]
        files.append({"path": entry[3:], "status": "untracked" if xy == "??" else (xy.strip() or "modified")})
        if xy[0] in ("R", "C"):
            i += 1
    return files


def parse_unified(text: str, max_lines: int = MAX_DIFF_LINES) -> dict:
    """Hunks of a one-file unified diff; stops after `max_lines` diff lines (`truncated`)."""
    hunks, cur, o, n, count, binary = [], None, 0, 0, 0, False
    for line in text.split("\n"):
        h = HUNK.match(line)
        if h:
            o, n = int(h[1]), int(h[3])
            cur = {"header": line, "old_start": o, "old_lines": 1 if h[2] is None else int(h[2]),
                   "new_start": n, "new_lines": 1 if h[4] is None else int(h[4]), "section": h[5] or "", "lines": []}
            hunks.append(cur)
            continue
        if cur is None:
            if line.startswith("Binary files ") or line == "GIT binary patch":
                binary = True
            continue
        if count >= max_lines:
            return {"hunks": hunks, "binary": binary, "truncated": True}
        c = line[:1]
        if c == " ":
            dl = {"t": "ctx", "o": o, "n": n, "s": line[1:]}
            o, n = o + 1, n + 1
        elif c == "+":
            dl = {"t": "add", "o": None, "n": n, "s": line[1:]}
            n += 1
        elif c == "-":
            dl = {"t": "del", "o": o, "n": None, "s": line[1:]}
            o += 1
        elif c == "\\":
            dl = {"t": "note", "o": None, "n": None, "s": line[2:]}
        elif line.startswith("diff --git "):
            cur = None
            continue
        else:
            continue
        cur["lines"].append(dl)
        count += 1
    return {"hunks": hunks, "binary": binary, "truncated": False}


_LOCATIONS = (
    re.compile(r"^([^\s:#]+):(\d+)(?:\s*[-–]\s*(\d+))?"),
    re.compile(r"^([^\s:#]+)#L(\d+)(?:-L?(\d+))?"),
    re.compile(r"^([^\s:#]+)\s*\(?\s*lines?\s+(\d+)(?:\s*[-–]\s*(\d+))?", re.I),
)


def parse_location(location, worktree: str | Path | None = None) -> dict | None:
    """A finding's `location` (`path:12`, `path:12-18`, `path#L12`, `path (line 12)`, a bare path), worktree-relative.
    Absolute paths under the worktree are made relative; any other absolute path gives None."""
    if not isinstance(location, str):
        return None
    text = location.strip().replace("`", "").strip()
    if not text:
        return None
    m = next((m for m in (r.match(text) for r in _LOCATIONS) if m), None)
    path = m[1] if m else text.split()[0]
    line = int(m[2]) if m else None
    end = int(m[3]) if m and m[3] else None
    path = re.sub(r"[),;]+$", "", path)
    if path.startswith("./"):
        path = path[2:]
    if worktree and path.startswith("/"):
        root = str(worktree).rstrip("/") + "/"
        if not path.startswith(root):
            return None
        path = path[len(root):]
    if not path or path.startswith("/") or "\0" in path:
        return None
    line = line if line and line > 0 else None
    return {"path": path, "line": line, "end_line": end if end and line and end >= line else None}


def match_changed_path(path: str, files: list[str]) -> str | None:
    """The changed file a location names: the exact path, else the one path ending in `/<path>`."""
    if path in files:
        return path
    hits = [f for f in files if f.endswith("/" + path)]
    return hits[0] if len(hits) == 1 else None


# --- ticket context ------------------------------------------------------------------------------

def _ticket(target: Target, ticket_id: str) -> tuple[dict, dict, Path | None]:
    ticket = state.load_ticket(target, ticket_id)  # LookupError for an unknown ticket
    st = state.load_state(target, ticket_id)
    wt = Path(st["worktree"]) if st.get("worktree") else None
    return ticket, st, wt


def _base_commit(target: Target, ticket: dict, st: dict, wt: Path) -> str | None:
    """Fork point of the branch: merge-base with <remote>/<base>, else the run's recorded base commit."""
    sha = _text(wt, "merge-base", "HEAD", worktree.base_ref(target, ticket), check=False).strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", sha):
        return sha
    recorded = (st.get("run") or {}).get("base_commit")
    return _rev(wt, recorded) if isinstance(recorded, str) and SHA.match(recorded) else None


def _records(target: Target, ticket_id: str) -> list[tuple[str, dict]]:
    """(dir name, run.json) of every stage run with a readable record, oldest first."""
    found = []
    for d in state.phase_dirs(target, ticket_id):
        try:
            rec = state.read_json(d / "run.json")
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            found.append((d.name, rec))
    return found


def _stage_name(dir_name: str) -> str:
    return state.PHASE_DIR.match(dir_name)[2]


def _ok_result(rec: dict) -> dict | None:
    result = rec.get("result")
    return result if rec.get("state", "ok") == "ok" and isinstance(result, dict) else None


def _reviews(records: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """The finished review stages with a findings list, oldest first."""
    return [(name, rec) for name, rec in records if _stage_name(name).startswith("review")
            and (result := _ok_result(rec)) is not None and isinstance(result.get("findings"), list)]


def _review_runs(target: Target, ticket_id: str) -> list[tuple[str, dict]]:
    return _reviews(_records(target, ticket_id))


def finding_class(f: dict) -> str:
    """important | nit | pre-existing: the finding's own class, else derived (defer → pre-existing, low → nit)."""
    raw = f.get("class")
    if isinstance(raw, str):
        c = re.sub(r"[\s_]+", "-", raw.strip().lower())
        c = "pre-existing" if c == "preexisting" else c
        if c in FINDING_CLASSES:
            return c
    if f.get("disposition") == "defer":
        return "pre-existing"
    return "nit" if f.get("severity") == "low" else "important"


def _confidence(value):
    """A 0–1 number (`80`, `"80%"` → 0.8) or low/medium/high; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("low", "medium", "high"):
            return text
        try:
            value = float(text.rstrip("%").strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        if 0 <= value <= 1:
            return float(value)
        if 1 < value <= 100:
            return value / 100
    return None


def outcome_for(records: list[tuple[str, dict]], review_dir: str, fid: str) -> dict | None:
    """What the implementer reported for a finding: the newest ok stage run after the review whose `outcomes`
    names it, as {outcome, note, stage_dir}."""
    names = [name for name, _ in records]
    start = names.index(review_dir) + 1 if review_dir in names else len(records)
    for name, rec in reversed(records[start:]):
        result = _ok_result(rec)
        for o in _list(result.get("outcomes")) if result else []:
            if isinstance(o, dict) and (o.get("finding_id") or o.get("id")) == fid:
                outcome = o.get("outcome").strip().lower() if isinstance(o.get("outcome"), str) else ""
                note = o.get("note").strip() if isinstance(o.get("note"), str) else ""
                return {"outcome": outcome if outcome in OUTCOMES else "no_change", "note": note or None, "stage_dir": name}
    return None


def _settled_later(reviews: list[tuple[str, dict]], idx: int) -> set[str]:
    """Finding ids a review after `reviews[idx]` lists again (any disposition) or as fixed."""
    out = set()
    for j, (_, r) in enumerate(reviews[idx + 1:], idx + 1):
        out |= {str(x) for x in _list(r["result"].get("fixedIds"))}
        out |= {_finding_id(f, j, k) for k, f in enumerate(r["result"]["findings"]) if isinstance(f, dict)}
    return out


def dismissed_findings(reviews: list[tuple[str, dict]], records: list[tuple[str, dict]], comments: list[dict]) -> list[dict]:
    """What the review dismissed and is still dismissed, oldest round first: {key, id, stage_dir, summary, reason,
    location}. A dismissal ends when a later review lists the id again or as fixed, when the user sent the finding
    to fix (a sent review comment with `finding_action` fix), or when the implementer reports it `fixed`. The one
    rule behind the review page's card, the `dismissed` flag, the done card's count and the PR draft."""
    records = list(records)
    out = []
    for idx, (name, rec) in enumerate(reviews):
        settled = _settled_later(reviews, idx)
        for k, f in enumerate(rec["result"]["findings"]):
            if not isinstance(f, dict) or f.get("disposition") != "dismiss":
                continue
            fid = _finding_id(f, idx, k)
            if fid in settled or (outcome_for(records, name, fid) or {}).get("outcome") == "fixed" or any(
                    c.get("round") is not None and c.get("finding_action") == "fix" and c.get("finding_id") == fid
                    and c.get("finding_stage") in (name, None) for c in comments):
                continue
            out.append({"key": f"{name}#{fid}", "id": fid, "stage_dir": name, "summary": str(f.get("summary") or ""),
                        "reason": f.get("reason") if isinstance(f.get("reason"), str) else None,
                        "location": f.get("location") if isinstance(f.get("location"), str) else None})
    return out


def dismissed_count(target: Target, ticket_id: str) -> int:
    records = _records(target, ticket_id)
    return len(dismissed_findings(_reviews(records), records, load_review(target, ticket_id)["comments"]))


def review_history(target: Target, ticket_id: str) -> tuple[list[tuple[str, dict]], list[dict]]:
    """(the finished review runs with findings, oldest first; the review page's comments) for retro.py."""
    return _review_runs(target, ticket_id), load_review(target, ticket_id)["comments"]


def overruled_findings(reviews: list[tuple[str, dict]], comments: list[dict]) -> list[dict]:
    """Findings the AI review dismissed that the user then sent to fix (a sent review comment with `finding_action`
    fix): the review was wrong about them. {ref (`R<round>-<n>`), id, stage_dir, round, summary, reason, location,
    comment (the user's reply text)}."""
    out = []
    for idx, (name, rec) in enumerate(reviews):
        for k, f in enumerate(rec["result"]["findings"]):
            if not isinstance(f, dict) or f.get("disposition") != "dismiss":
                continue
            fid = _finding_id(f, idx, k)
            reply = next((c for c in comments if c.get("round") is not None and c.get("finding_action") == "fix"
                          and c.get("finding_id") == fid and c.get("finding_stage") in (name, None)), None)
            if reply is None:
                continue
            out.append({"ref": f"R{idx + 1}-{k + 1}", "id": fid, "stage_dir": name, "round": idx + 1,
                        "summary": str(f.get("summary") or ""), "reason": _str(f.get("reason")),
                        "location": _str(f.get("location")), "comment": _str(reply.get("body")), "comment_id": reply.get("id")})
    return out


def recurring_findings(reviews: list[tuple[str, dict]]) -> list[dict]:
    """Finding ids two or more review rounds listed (the implementer did not settle them when first asked), each
    once, by its first round: {ref (`R<round>-<n>` of the first listing), id, rounds, summary (the newest), location,
    dispositions}. Positional ids (`R1-2`, given when a finding had none) never recur."""
    seen: dict[str, dict] = {}
    for idx, (name, rec) in enumerate(reviews):
        for k, f in enumerate(rec["result"]["findings"]):
            if not isinstance(f, dict) or not (isinstance(f.get("id"), str) and f["id"]):
                continue
            item = seen.setdefault(f["id"], {"ref": f"R{idx + 1}-{k + 1}", "id": f["id"], "rounds": [], "dispositions": []})
            if idx + 1 not in item["rounds"]:
                item["rounds"].append(idx + 1)
                item["dispositions"].append(str(f.get("disposition") or "fix"))
            item.update(summary=str(f.get("summary") or ""), location=_str(f.get("location")))
    return [item for item in seen.values() if len(item["rounds"]) > 1]


def collect_findings(reviews: list[tuple[str, dict]], files: list[str], worktree,
                     records: list[tuple[str, dict]] = (), comments: list[dict] = ()) -> list[dict]:
    """Findings of every review round, newest round first, placed on changed files where their location allows.
    `dismissed`: still dismissed by the review (`dismissed_findings`);
    `selectable`: the newest round's finding not fixed or dismissed, or a dismissed one ("Fix selected" may pick it);
    `open_important`: selectable, important, not a product decision, and the user has not replied yet."""
    records = list(records)
    dismissed = {d["key"] for d in dismissed_findings(reviews, records, comments)}
    out = []
    for idx, (name, rec) in enumerate(reviews):
        later = {str(x) for _, r in reviews[idx + 1:] for x in _list(r["result"].get("fixedIds"))}
        for k, f in enumerate(rec["result"]["findings"]):
            if not isinstance(f, dict):
                continue
            fid = _finding_id(f, idx, k)
            location = f.get("location") if isinstance(f.get("location"), str) else None
            loc = parse_location(location, worktree)
            path = match_changed_path(loc["path"], files) if loc else None
            disposition = str(f.get("disposition") or "fix")
            cls = finding_class(f)
            latest, fixed = idx == len(reviews) - 1, fid in later
            was_dismissed = f"{name}#{fid}" in dismissed
            selectable = (latest and not fixed and disposition != "dismiss") or was_dismissed
            replied = any(c.get("finding_id") == fid and c.get("finding_stage") in (name, None) for c in comments)
            out.append({
                "key": f"{name}#{fid}", "id": fid, "stage_dir": name, "round": idx + 1,
                "severity": str(f.get("severity") or "medium"), "kind": str(f.get("kind") or "bug"),
                "disposition": disposition, "summary": str(f.get("summary") or ""),
                "class": cls, "confidence": _confidence(f.get("confidence")),
                "verified": f["verified"] if isinstance(f.get("verified"), bool) else None,
                "outcome": outcome_for(records, name, fid),
                "evidence": f.get("evidence") if isinstance(f.get("evidence"), str) else None,
                "reason": f.get("reason") if isinstance(f.get("reason"), str) else None,
                "location": location, "path": path,
                "line": loc["line"] if path else None, "end_line": loc["end_line"] if path else None,
                "fixed": fixed, "latest": latest, "selectable": selectable, "dismissed": was_dismissed,
                "open_important": selectable and disposition not in ("decision", "dismiss") and cls == "important" and not replied,
            })
    return sorted(out, key=lambda f: -f["round"])


def unverified(reviews: list[tuple[str, dict]]) -> list[dict]:
    """The newest review's candidates its verification pass could not confirm (kept out of the findings)."""
    if not reviews:
        return []
    name, rec = reviews[-1]
    out = []
    for u in _list(rec["result"].get("unverified")):
        if isinstance(u, str) and u.strip():
            u = {"summary": u}
        if isinstance(u, dict):
            out.append({"stage_dir": name, "location": _str(u.get("location")), "summary": str(u.get("summary") or "").strip(),
                        "reason": _str(u.get("reason"))})
    return out


def guide_view(reviews: list[tuple[str, dict]], files: list[str], worktree) -> dict | None:
    """The newest review's reviewer's guide in reading order: {stage_dir, summary, entries[{file, path, line,
    summary, how_to_verify, order}]}; `path` is the changed file the entry names, when there is one."""
    for name, rec in reversed(reviews):
        raw = [g for g in _list(rec["result"].get("guide")) if isinstance(g, dict) and _str(g.get("file"))]
        if not raw:
            continue
        entries = []
        for i, g in enumerate(raw):
            loc = parse_location(g["file"], worktree)
            path = match_changed_path(loc["path"], files) if loc else None
            order = g.get("order")
            if isinstance(order, str):
                try:
                    order = float(order.strip())
                except ValueError:
                    order = None
            if not isinstance(order, (int, float)) or isinstance(order, bool):
                order = 1000 + i
            entries.append(((order, i), {
                "file": g["file"].strip(), "path": path, "line": loc["line"] if path else None,
                "summary": " ".join(str(g.get("summary") or "").split()),
                "how_to_verify": _str(g.get("howToVerify", g.get("how_to_verify"))), "order": order}))
        return {"stage_dir": name, "summary": str(rec["result"].get("summary") or "").strip(),
                "entries": [e for _, e in sorted(entries, key=lambda it: it[0])]}
    return None


def _str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def work_summary(records: list[tuple[str, dict]]) -> str:
    """What the newest successful implement stage said it did."""
    for name, rec in reversed(records):
        result = _ok_result(rec)
        if _stage_name(name).startswith("implement") and result and _str(result.get("summary")):
            return result["summary"].strip()[:MAX_WORK_SUMMARY]
    return ""


def attribute_commits(commits: list[dict], records: list[tuple[str, dict]], base: str | None) -> list[dict | None]:
    """The stage run that made each commit (newest first, as `git log` lists them), from the HEADs recorded
    around each run: a run owns the commits in head_before..head_after; the first run to claim one keeps it.
    Hand-made commits stay None."""
    index = {c["hash"]: i for i, c in enumerate(commits)}
    out: list[dict | None] = [None] * len(commits)
    for name, rec in records:
        before, after = rec.get("head_before"), rec.get("head_after")
        if not before or not after or before == after:
            continue
        lo = index.get(after)
        hi = len(commits) if before == base else index.get(before)
        if lo is None or hi is None or lo >= hi:
            continue
        ref = {"stage_dir": name, "stage": rec.get("stage") or _stage_name(name),
               "visit": rec.get("ticket_visit") or rec.get("visit")}
        for i in range(lo, hi):
            if out[i] is None:
                out[i] = ref
    return out


def _attribute(commits: list[dict], records: list[tuple[str, dict]], base: str | None) -> None:
    for c, ref in zip(commits, attribute_commits(commits, records, base)):
        c.update(ref or {"stage_dir": None, "stage": None, "visit": None})


# --- evidence ------------------------------------------------------------------------------------

def artifacts_dir(target: Target, ticket_id: str) -> Path:
    return state.inbox_dir(target, ticket_id) / "artifacts"


def file_url(target_name: str, ticket_id: str, rel: str) -> str:
    return f"/api/files/{quote(target_name, safe='')}/{quote(ticket_id, safe='')}/" + "/".join(
        quote(seg, safe="") for seg in rel.split("/"))


def _artifact_path(target: Target, ticket_id: str, rel) -> Path | None:
    """The regular file `rel` names inside the ticket's artifacts dir after resolving symlinks, else None."""
    if not isinstance(rel, str) or not rel or "\0" in rel or rel.startswith("/") or "\\" in rel:
        return None
    segments = rel.split("/")
    if len(segments) > MAX_ARTIFACT_DEPTH or not all(ARTIFACT_SEGMENT.match(s) for s in segments):
        return None
    root = artifacts_dir(target, ticket_id)
    try:
        base = root.resolve(strict=True)
        path = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if base not in path.parents or not path.is_file():
        return None
    return path


def artifact(target: Target, ticket_id: str, rel: str) -> tuple[Path, str]:
    """An evidence file for /api/files: a regular file inside `inbox/artifacts` (realpath check, so `..` and
    symlinks out are refused) with an allowed extension. LookupError when not servable, ArtifactTooLarge over
    its kind's size cap."""
    state.load_ticket(target, ticket_id)
    path = _artifact_path(target, ticket_id, rel) if ext_of(rel or "") in ARTIFACT_TYPES else None
    ext = ext_of(path.name) if path else ""
    if ext not in ARTIFACT_TYPES:  # a link inside the dir must point at an allowed kind too
        raise LookupError("not found")
    if path.stat().st_size > ARTIFACT_LIMITS[ext]:
        raise ArtifactTooLarge(f"{rel} is too large to show here")
    return path, ARTIFACT_TYPES[ext]


def evidence(target: Target, ticket_id: str, records: list[tuple[str, dict]]) -> list[dict]:
    """Evidence groups, newest stage run first: {stage_dir, stage, visit, at, artifacts[{path, kind, caption, url,
    size, exists, stage_dir, stage}]}. A file listed by several runs stays with the first one that listed it."""
    root = str(artifacts_dir(target, ticket_id))
    groups, seen = [], set()
    for name, rec in records:
        result = _ok_result(rec)
        if not result or not isinstance(result.get("artifacts"), list):
            continue
        stage = rec.get("stage") or _stage_name(name)
        items = []
        for a in results.normalize_artifacts(result["artifacts"], root):
            url = file_url(target.name, ticket_id, a["path"])
            if url in seen:
                continue
            seen.add(url)
            path = _artifact_path(target, ticket_id, a["path"])
            items.append({**a, "url": url, "size": path.stat().st_size if path else None, "exists": path is not None,
                          "stage_dir": name, "stage": stage})
        if items:
            groups.append({"stage_dir": name, "stage": stage, "visit": rec.get("ticket_visit") or rec.get("visit"),
                           "at": rec.get("finished_at") or rec.get("started_at"), "artifacts": items})
    return groups[::-1]


def pr_inputs(target: Target, ticket_id: str) -> dict:
    """What the PR draft takes from the stage runs: the implementer's summary, the guide, the evidence files and
    the findings the review dismissed."""
    records = _records(target, ticket_id)
    reviews = _reviews(records)
    guide = guide_view(reviews, [], None)
    return {"work_summary": work_summary(records), "guide": guide["entries"] if guide else [],
            "evidence": [a for g in reversed(evidence(target, ticket_id, records)) for a in g["artifacts"]],
            "dismissed": dismissed_findings(reviews, records, load_review(target, ticket_id)["comments"])}


def pr_draft(target: Target, ticket_id: str) -> dict:
    """{title, body} the panel prefills its PR form with; the same text `publish` uses without overrides
    (minus the footer, which publish always appends)."""
    from . import protected, publish  # publish reads pr_inputs from this module; importing it here keeps the import one-way
    pending = protected.pending(state.load_state(target, ticket_id))
    return {**publish.draft(target, ticket_id), "protected_pending": sorted({f for p in pending for f in p["files"]})}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _finding_id(f: dict, idx: int, k: int) -> str:
    """The finding's own id, else `R<round>-<n>` (round and position among the review runs)."""
    return f["id"] if isinstance(f.get("id"), str) and f["id"] else f"R{idx + 1}-{k + 1}"


def _finding_ref(reviews: list[tuple[str, dict]], stage_dir: str | None, fid: str) -> dict | None:
    """What a reply refers to, copied from the newest review run holding that finding id."""
    for idx in range(len(reviews) - 1, -1, -1):
        name, rec = reviews[idx]
        if stage_dir and name != stage_dir:
            continue
        for k, f in enumerate(rec["result"]["findings"]):
            if isinstance(f, dict) and _finding_id(f, idx, k) == fid:
                return {"id": fid, "stage_dir": name, "severity": str(f.get("severity") or "medium"),
                        "kind": str(f.get("kind") or "bug"), "disposition": str(f.get("disposition") or "fix"),
                        "summary": str(f.get("summary") or ""),
                        "location": f.get("location") if isinstance(f.get("location"), str) else None}
    return None


def _points(target: Target, ticket_id: str, st: dict, reviews: list[tuple[str, dict]]) -> list[dict]:
    """Heads the range picker can diff from: each AI review's starting HEAD, each sent review round's HEAD."""
    out = []
    for i, (_, rec) in enumerate(reviews, 1):
        if rec.get("head_before"):
            out.append({"kind": "ai", "n": i, "spec": f"since:ai:{i}", "label": f"AI review, tur {i}",
                        "head": rec["head_before"], "at": rec.get("started_at") or ""})
    for i, r in enumerate(st.get("user_reviews") or [], 1):
        if isinstance(r, dict) and r.get("head"):
            who = {"codex": "Codex review", "github": "GitHub geri bildirimi"}.get(r.get("source"), "senin review'in")
            out.append({"kind": "user", "n": i, "spec": f"since:user:{i}", "label": f"{who}, tur {r.get('round', i)}",
                        "head": r["head"], "at": r.get("at") or ""})
    return sorted(out, key=lambda p: p["at"])


# --- review.json ---------------------------------------------------------------------------------

def review_dir(target: Target, ticket_id: str) -> Path:
    return state.run_dir(target, ticket_id) / "review"


def load_review(target: Target, ticket_id: str) -> dict:
    path = review_dir(target, ticket_id) / "review.json"
    data = state.read_json(path) if path.is_file() else {}
    return {"comments": list(data.get("comments") or []), "rounds": list(data.get("rounds") or []),
            "viewed": dict(data.get("viewed") or {})}


def _save_review(target: Target, ticket_id: str, data: dict) -> None:
    root = review_dir(target, ticket_id)
    root.mkdir(parents=True, exist_ok=True)
    state.write_json(root / "review.json", data)


def _review_lock(target: Target, ticket_id: str):
    return state.waiting_lock(target, f"review-{ticket_id}", f"review comments of {ticket_id}", timeout=15, poll=0.05)


def draft_count(target: Target, ticket_id: str) -> int:
    path = review_dir(target, ticket_id) / "review.json"
    if not path.is_file():
        return 0
    try:
        return sum(1 for c in state.read_json(path).get("comments") or [] if c.get("round") is None)
    except ValueError:
        return 0


def _blocked(target: Target, ticket_id: str, st: dict, wt: Path | None) -> str | None:
    """Why `submit` cannot send the drafts now (None: it can)."""
    if state.running(target, ticket_id):
        return f"{ticket_id} has an active run; wait for it to finish"
    status = st.get("status")
    if status == "queued":
        return f"{ticket_id} is waiting for a run slot; wait for it to finish"
    if status not in ("done", "failed"):
        return f"{ticket_id} is {status}; changes can be requested once it is done or failed"
    if not wt or not wt.is_dir():
        return f"{ticket_id} has no worktree"
    return None


# --- interface -----------------------------------------------------------------------------------

def _valid_path(p) -> bool:
    return isinstance(p, str) and 0 < len(p) <= 4096 and "\0" not in p


def summary(target: Target, ticket_id: str, range_spec: str = "all", to: str | None = None) -> dict:
    """What the branch changed for the range, plus the review state (drafts, rounds, viewed marks, findings,
    unverified candidates, the reviewer's guide, evidence, and which stage run made each commit)."""
    ticket, st, wt = _ticket(target, ticket_id)
    review = load_review(target, ticket_id)
    records = _records(target, ticket_id)
    reviews = _reviews(records)
    alive = state.running(target, ticket_id)
    blocked = _blocked(target, ticket_id, st, wt)
    drafts = sum(1 for c in review["comments"] if c.get("round") is None)
    out = {
        "ticket_id": ticket_id, "title": ticket.get("title"), "branch": ticket.get("branch"),
        "base": worktree.base_ref(target, ticket), "status": st.get("status"), "stage": st.get("stage"),
        "running": alive, "pr": st.get("pr"), "worktree": bool(wt and wt.is_dir()),
        "head": None, "base_commit": None,
        "range": {"kind": "all", "spec": "all", "from": "", "to": "", "label": "Tüm değişiklikler"},
        "new_commits": 0, "files": [], "totals": {"files": 0, "additions": 0, "deletions": 0},
        "commits": [], "points": _points(target, ticket_id, st, reviews), "uncommitted": [],
        "findings": [], "comments": review["comments"], "rounds": review["rounds"], "viewed": review["viewed"],
        "drafts": drafts, "submit_blocked": blocked or (None if drafts else "no draft comments to send"),
        "send_blocked": blocked,  # "Fix selected" drafts its own replies, so only the ticket's state blocks it
        "approve": st.get("status") == "done" and not alive,
        "unverified": unverified(reviews), "guide": guide_view(reviews, [], None),
        "evidence": evidence(target, ticket_id, records),
        "error": None,
    }
    if not out["worktree"]:
        out["findings"] = collect_findings(reviews, [], None, records, review["comments"])
        out["error"] = "the worktree is gone" if wt else "this ticket has no worktree yet"
        return out
    try:
        head = _rev(wt, "HEAD")
        if not head:
            raise ChangesError("cannot read HEAD of the worktree")
        base = _base_commit(target, ticket, st, wt)
        out["base_commit"] = base
        if base:
            log = _text(wt, "log", f"--max-count={MAX_COMMITS}", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1e", f"{base}..{head}")
            for rec in filter(None, (r.strip() for r in log.split("\x1e"))):
                h, short, subject, author, at = (rec.split("\x1f") + [""] * 5)[:5]
                out["commits"].append({"hash": h, "short": short, "subject": subject, "author": author, "at": at})
            _attribute(out["commits"], records, base)
        out["head"] = next((c for c in out["commits"] if c["hash"] == head),
                           {"hash": head, "short": head[:9], "subject": "", "author": "", "at": ""})
        frm, end, kind, label, spec = _resolve(wt, (range_spec or "all").strip(), head, base, out["points"], out["commits"], ticket)
        if kind != "commit" and to and SHA.match(to):
            pinned = _rev(wt, to)
            if pinned and pinned != head:
                end = pinned
                out["new_commits"] = int(_text(wt, "rev-list", "--count", f"{pinned}..{head}", check=False).strip() or 0)
        out["range"] = {"kind": kind, "spec": spec if kind != "commit" else f"commit:{end}", "from": frm, "to": end, "label": label}
        raw = _text(wt, "diff", "--no-color", "--no-ext-diff", "-M", "-z", "--raw", "--no-abbrev", frm, end)
        numstat = _text(wt, "diff", "--no-color", "--no-ext-diff", "-M", "-z", "--numstat", frm, end)
        out["files"] = change_files(parse_raw(raw), parse_numstat(numstat), review["viewed"])
        out["totals"] = {"files": len(out["files"]),
                         "additions": sum(f["additions"] or 0 for f in out["files"]),
                         "deletions": sum(f["deletions"] or 0 for f in out["files"])}
        out["uncommitted"] = parse_status(_text(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=False))
    except (ChangesError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)
    files = [f["path"] for f in out["files"]]
    out["findings"] = collect_findings(reviews, files, wt, records, review["comments"])
    for f in out["files"]:
        f["risk"], f["risk_why"] = file_risk(f, out["findings"])
    out["guide"] = guide_view(reviews, files, wt)
    return out


def _resolve(wt: Path, spec: str, head: str, base: str | None, points: list[dict], commits: list[dict], ticket: dict):
    """(from, to, kind, label, normalized spec) for `all`, `commit:<sha>`, `since:ai:<n>`, `since:user:<n>`."""
    kind, _, arg = spec.partition(":")
    if kind == "commit" and SHA.match(arg):
        sha = _rev(wt, arg)
        if not sha:
            raise ChangesError(f"unknown commit {arg}")
        parent = _rev(wt, f"{sha}^")
        if not parent:
            raise ChangesError(f"commit {arg[:10]} has no parent")
        c = next((c for c in commits if c["hash"] == sha), None)
        return parent, sha, "commit", f"Commit {sha[:9]}" + (f": {c['subject']}" if c else ""), f"commit:{sha}"
    if kind == "since":
        point = next((p for p in points if p["spec"] == spec), None)
        if not point:
            raise ChangesError(f"unknown review point {arg}")
        frm = _rev(wt, point["head"])
        if not frm:
            raise ChangesError(f"the head of {point['label']} ({point['head'][:9]}) is no longer in the repository")
        return frm, head, "since", f"{point['label']} sonrası", spec
    if not base:
        raise ChangesError(f"cannot find where {ticket.get('branch') or 'the branch'} forks from its base")
    return base, head, "all", "Tüm değişiklikler", "all"


_DIFFS: "OrderedDict[tuple, dict]" = OrderedDict()
_DIFFS_LOCK = threading.Lock()
_DIFFS_MAX = 300


def file_diff(target: Target, ticket_id: str, path: str, frm: str, to: str, old_path: str | None = None,
              full: bool = False) -> dict:
    """One file's hunks between two commits (`to` may be "worktree": against the working tree, not cached)."""
    _, _, wt = _ticket(target, ticket_id)
    if not wt or not wt.is_dir():
        raise LookupError(f"{ticket_id} has no worktree")
    if not (isinstance(frm, str) and SHA.match(frm)) or not (to == "worktree" or (isinstance(to, str) and SHA.match(to))):
        raise ChangesError("from/to must be commit ids")
    if not _valid_path(path) or (old_path is not None and not _valid_path(old_path)):
        raise ChangesError("bad path")
    paths = [old_path, path] if old_path and old_path != path else [path]
    context = 100000 if full else 3
    key = (str(wt), frm, to, tuple(paths), context)
    if to != "worktree":
        with _DIFFS_LOCK:
            if key in _DIFFS:
                _DIFFS.move_to_end(key)
                return _DIFFS[key]
    revs = [frm] if to == "worktree" else [frm, to]
    text = _text(wt, "diff", "--no-color", "--no-ext-diff", "-M", f"-U{context}", *revs, "--", *paths)
    res = {"path": path, "old_path": old_path, "from": frm, "to": to, **parse_unified(text)}
    if to != "worktree":
        with _DIFFS_LOCK:
            _DIFFS[key] = res
            if len(_DIFFS) > _DIFFS_MAX:
                _DIFFS.popitem(last=False)
    return res


def blob(target: Target, ticket_id: str, rev: str, path: str) -> tuple[bytes, str]:
    """An image file at a commit (before/after views). LookupError for anything else."""
    _, _, wt = _ticket(target, ticket_id)
    ctype = IMAGE_TYPES.get(ext_of(path)) if _valid_path(path) else None
    if not wt or not wt.is_dir() or not (isinstance(rev, str) and SHA.match(rev)) or not ctype:
        raise LookupError("not found")
    size = _text(wt, "cat-file", "-s", f"{rev}:{path}", check=False).strip()
    if not size.isdigit() or int(size) > MAX_BLOB:
        raise LookupError("not found")
    return _git(wt, "cat-file", "blob", f"{rev}:{path}"), ctype


def review_post(target: Target, ticket_id: str, payload: dict) -> dict:
    """Draft comments (`comment.add|edit|delete`), viewed marks (`viewed.set`) and "fix it" replies to findings
    (`findings.fix`, ids = finding keys `<stage_dir>#<id>` or bare ids). Sent comments are frozen."""
    state.load_ticket(target, ticket_id)
    op = payload.get("op")
    if not isinstance(op, str):
        raise ChangesError("op must be a string")
    reviews = _review_runs(target, ticket_id) if op == "findings.fix" else []
    with _review_lock(target, ticket_id):
        data = load_review(target, ticket_id)
        result = _fix_findings(data, reviews, payload.get("ids")) if op == "findings.fix" else _apply(data, op, payload)
        _save_review(target, ticket_id, data)
    return result


def _fix_findings(data: dict, reviews: list[tuple[str, dict]], ids) -> dict:
    """A draft "fix it" reply per selected finding. A finding that already has a fix reply, or any unsent reply,
    is left alone, so sending the same selection twice adds nothing."""
    if not isinstance(ids, list):
        raise ChangesError("ids must be a list of finding ids")
    keys = [k for k in ids[:MAX_FIX] if isinstance(k, str) and k]
    if not keys:
        raise ChangesError("no findings selected")
    found, added = 0, 0
    for key in keys:
        stage_dir, _, fid = key.rpartition("#")
        ref = _finding_ref(reviews, stage_dir or None, fid)
        if ref is None:
            continue
        found += 1
        if any(c.get("finding_id") == fid and c.get("finding_stage") in (ref["stage_dir"], None)
               and (c.get("finding_action") == "fix" or c.get("round") is None) for c in data["comments"]):
            continue
        stamp = state.now()
        data["comments"].append({
            "id": secrets.token_hex(8), "round": None, "path": None, "line": None, "side": "new", "commit_sha": None,
            "line_text": None, "finding_id": fid, "finding_stage": ref["stage_dir"], "finding_action": "fix", "body": "",
            "author": getpass.getuser(), "created_at": stamp, "updated_at": stamp,
        })
        added += 1
    if not found:
        raise ChangesError("none of the selected findings exists")
    message = f'{added} "fix it" repl{"y" if added == 1 else "ies"} drafted' if added else "those findings already have replies"
    return {"ok": True, "message": message, "added": added}


def _action(value) -> str | None:
    return value if value in ("fix", "dismiss") else None


def _body(value, action: str | None, finding: bool) -> str:
    body = str(value or "").strip()
    if not body and not (finding and action):
        raise ChangesError("empty comment")
    if len(body) > MAX_BODY:
        raise ChangesError(f"comment too long (max {MAX_BODY} characters)")
    return body


def _apply(data: dict, op: str, p: dict) -> dict:
    stamp = state.now()
    if op == "comment.add":
        finding = p["finding_id"][:100] if isinstance(p.get("finding_id"), str) and p["finding_id"] else None
        action = _action(p.get("finding_action")) if finding else None
        body = _body(p.get("body"), action, bool(finding))
        path = p.get("path")
        if path is not None and not _valid_path(path):
            raise ChangesError("bad path")
        if not path and not finding:
            raise ChangesError("a comment needs a file or a finding")
        line = p.get("line")
        line = line if isinstance(line, int) and not isinstance(line, bool) and line > 0 and path else None
        c = {
            "id": secrets.token_hex(8), "round": None, "path": path or None, "line": line,
            "side": "old" if p.get("side") == "old" else "new",
            "commit_sha": p["commit_sha"] if isinstance(p.get("commit_sha"), str) and SHA.match(p["commit_sha"]) else None,
            "line_text": p["line_text"][:2000] if isinstance(p.get("line_text"), str) else None,
            "finding_id": finding,
            "finding_stage": p["finding_stage"][:64] if finding and isinstance(p.get("finding_stage"), str) else None,
            "finding_action": action, "body": body, "author": getpass.getuser(),
            "created_at": stamp, "updated_at": stamp,
        }
        data["comments"].append(c)
        return {"ok": True, "message": "draft saved", "comment": c}
    if op in ("comment.edit", "comment.delete"):
        c = next((c for c in data["comments"] if c.get("id") == p.get("id")), None)
        if c is None:
            raise ChangesError("unknown comment")
        if c.get("round") is not None:
            raise ChangesError(f"this comment was sent with round {c['round']}; it can no longer change")
        if op == "comment.delete":
            data["comments"].remove(c)
            return {"ok": True, "message": "draft deleted"}
        action = c.get("finding_action") if "finding_action" not in p else _action(p.get("finding_action"))
        body = _body(p.get("body"), action, bool(c.get("finding_id")))
        c.update(body=body, finding_action=action if c.get("finding_id") else None, updated_at=stamp)
        return {"ok": True, "message": "draft saved", "comment": c}
    if op == "viewed.set":
        if not _valid_path(p.get("path")):
            raise ChangesError("bad path")
        mark = p.get("blob")
        if mark is not None and not (isinstance(mark, str) and BLOB_ID.match(mark)):
            raise ChangesError("bad blob id")
        if mark is None:
            data["viewed"].pop(p["path"], None)
        else:
            data["viewed"][p["path"]] = mark
        return {"ok": True, "message": "marked viewed" if mark else "unmarked"}
    raise ChangesError(f"unknown op {op}")


# --- request changes -----------------------------------------------------------------------------

def heading(round_: int) -> str:
    return f"Review (user, round {round_})"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().split("\n"))


def _inline_code(text: str) -> str:
    t = text.rstrip()[:300]
    return f"`` {t} ``" if "`" in t else f"`{t}`"


def _where(c: dict) -> str:
    if not c.get("path"):
        return ""
    return f"`{c['path']}{':' + str(c['line']) if c.get('line') else ''}`"


def _order(items: list[tuple[dict, dict | None]]) -> list[tuple[dict, dict | None]]:
    """Line comments by file and line, replies to findings last."""
    return sorted(items, key=lambda it: (1 if it[0].get("finding_id") else 0, it[0].get("path") or "",
                                         it[0].get("line") or 0, it[0].get("created_at") or ""))


def section_markdown(round_: int, by: str, date: str, frm: str | None, to: str | None,
                     items: list[tuple[dict, dict | None]], first_task: int) -> tuple[str, int]:
    """The plan section for one round: every comment verbatim with where it was made, then a fix task per comment
    (a reply that dismisses a finding is recorded but gets no task). Returns (markdown, task count)."""
    items = _order(items)
    n = len(items)
    diff = f" · diff `{frm[:10]}..{to[:10]}`" if frm and to else ""
    out = [
        f"## {heading(round_)}",
        "",
        f"Requested by {by} on {date}{diff} · {n} comment{'' if n == 1 else 's'}. The comments are the user's own words",
        "(verbatim) and outrank the plan where they disagree. Fix tasks are at the end; their ids continue the plan's.",
    ]
    tasks, notes, nxt = [], [], first_task
    for k, (c, f) in enumerate(items, 1):
        cid = f"U{round_}-{k}"
        at = _where(c)
        out.append("")
        if c.get("finding_id"):
            loc = f"`{f['location']}`" if f and f.get("location") else at
            out.append(f"### {cid} · reviewer finding {c['finding_id']}{' · ' + loc if loc else ''}")
            if f:
                out.append(f"Finding ({f['severity']} {f['kind']}, {f['disposition']}): {' '.join(f['summary'].split())}")
            if c.get("finding_action"):
                out.append(f"The user: **{'fix it' if c['finding_action'] == 'fix' else 'dismiss it'}**")
        else:
            out.append(f"### {cid}{' · ' + at if at else ''}")
            if c.get("line_text") is not None and c.get("line"):
                side = "old side, removed" if c.get("side") == "old" else "new side"
                out.append(f"Line ({side}): {_inline_code(c['line_text'])}")
        if (c.get("body") or "").strip():
            out += ["", _quote(c["body"])]
        task = f"T{nxt:03d}"
        if c.get("finding_id") and c.get("finding_action") == "dismiss":
            notes.append(f"Not a task: {cid}, the user dismisses reviewer finding {c['finding_id']}; leave that code as it is.")
        elif c.get("finding_id"):
            loc = f"`{f['location']}`" if f and f.get("location") else at
            tasks.append(f"- [ ] {task} [{cid}] Fix reviewer finding {c['finding_id']} as the user asks{f' ({loc})' if loc else ''}")
            nxt += 1
        else:
            tasks.append(f"- [ ] {task} [{cid}] Address {cid}{' at ' + at if at else ''}")
            nxt += 1
    out += ["", "### Fix tasks", *(tasks or ["(none)"]), *notes]
    return "\n".join(out) + "\n", len(tasks)


def submit(target: Target, ticket_id: str, by: str) -> dict:
    """Send the drafts as round N: builds the plan section, marks the drafts sent, records the round. The caller
    passes the section (and `findings`, the ids the user asked to fix) to `pipeline.request_changes`, which appends
    it to the plan and starts the run."""
    ticket, st, wt = _ticket(target, ticket_id)
    blocked = _blocked(target, ticket_id, st, wt)
    if blocked:
        raise ChangesError(blocked)
    head = _rev(wt, "HEAD")
    base = _base_commit(target, ticket, st, wt)
    reviews = _review_runs(target, ticket_id)
    with _review_lock(target, ticket_id):
        data = load_review(target, ticket_id)
        drafts = [c for c in data["comments"] if c.get("round") is None]
        if not drafts:
            raise ChangesError(f"{ticket_id} has no draft comments to send")
        again = _blocked(target, ticket_id, state.load_state(target, ticket_id), wt)  # a run may have started meanwhile
        if again:
            raise ChangesError(again)
        round_ = max((r.get("round", 0) for r in data["rounds"]), default=0) + 1
        items = [(c, _finding_ref(reviews, c.get("finding_stage"), c["finding_id"]) if c.get("finding_id") else None)
                 for c in drafts]
        md, _ = section_markdown(round_, by, datetime.now().astimezone().date().isoformat(), base, head, items,
                                 plan.next_task_number(plan.read(state.plan_path(target, ticket_id))))
        stamp = state.now()
        for c in drafts:
            c["round"] = round_
            c["updated_at"] = stamp
        data["rounds"].append({"round": round_, "head": head, "base_commit": base, "comments": len(drafts),
                               "by": by, "at": stamp})
        _save_review(target, ticket_id, data)
        (review_dir(target, ticket_id) / f"round-{round_}.md").write_text(md, encoding="utf-8")
    fixes = list(dict.fromkeys(c["finding_id"] for c in drafts if c.get("finding_action") == "fix" and c.get("finding_id")))
    return {"round": round_, "heading": heading(round_), "comments": len(drafts), "section_md": md, "findings": fixes}
