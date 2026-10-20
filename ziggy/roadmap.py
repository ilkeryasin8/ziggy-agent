"""A target's long-term plan: phases of items, advanced ticket by ticket.

Where it comes from (`load`), first match wins:
    bmad    the main checkout's `_bmad-output/planning-artifacts/epics.md` (phases = `## Epic N: title`, items =
            `### Story N.M: title`, id = the story key `N-M`) with the statuses of
            `_bmad-output/implementation-artifacts/sprint-status.yaml` (its `development_status:` mapping, read by
            hand: backlog / ready-for-dev → todo, in-progress, review, done). Read-only: Ziggy never writes BMad files.
    memory  `roadmap.md` in the target's memory folder (memory.location: the vault folder mapped to the repo, or
            Ziggy's state), with the memory files' frontmatter:
                ## P1 · Temel
                - Durum: aktif                                    (optional: aktif | bitti)
                - [ ] R-3 · Giriş sayfası
                  - Ticket: local-7 · PR: https://… · Durum: pr  (written by Ziggy)
                  - any note                                      (the item's detail)
                - [x] R-4 · …
            Parsed again on every use, tolerant (`P1:`/`P1 -`, `R-3:`/`R3 -`, `*` bullets); what it cannot read is
            a warning (Hafıza page, project page, inbox), never an error; lines it does not know stay in the file.
    None    no roadmap yet: the panel offers a draft (`run_draft`, a read-only session) to save as roadmap.md.

Progress is automatic (`ticket_changed`, no approval): a ticket made from an item (ticket.json `origin = {kind:
"roadmap", item}`, or `link`, which a lead proposal drafted from items uses) moves each of its items to ticket →
in-progress (running) → review (done) → pr (PR opened) → merged (PR merged, checkbox `[x]`). Only the item's own
`Ticket/PR/Durum` line and checkbox change; a phase whose items are all merged or done gets `- Durum: bitti`, and
when no unfinished phase is active the first one gets `- Durum: aktif`. A status never moves back. A BMad roadmap is
not written: its items show their linked ticket's progress over the sprint status.

Scope changes (add an item to a phase, remove one, move one, add a phase) are memory candidates of kind `yol`
(memory.py), approved on the Hafıza page; `apply_change` makes the approved edit.

Kept in the state (never the vault), under `<state>/<target>/roadmap/`:
    links.json      {item id: ticket id}: which ticket works on an item (`link`, and every ticket made from one)
    drafts/NN-roadmap/  one draft session: run.json (dispatch's record, `draft` the markdown), prompt.md, claude.jsonl
    jobs/           the background jobs that ran them
"""
import dataclasses
import re
import sys
from pathlib import Path

from . import dispatch, memory, pipelines, render, results, state
from .config import Target

FILE = "roadmap.md"
STAGE = "roadmap"
PROMPT = "roadmap.md"
BUDGET_USD = 1.0
TIMEOUT = 900
EPICS = Path("_bmad-output/planning-artifacts/epics.md")
SPRINT = Path("_bmad-output/implementation-artifacts/sprint-status.yaml")
STATUSES = ("todo", "ticket", "in-progress", "review", "pr", "merged", "done")
RANK = {s: i for i, s in enumerate(STATUSES)}
FINISHED = ("merged", "done")
PHASE_STATUSES = ("todo", "active", "done")
# Read only, like the onboarding session: no edits, no shell, no subagents, no web, no credential files.
TOOLS_DENY = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "WebFetch", "WebSearch",
              "Read(**/.env)", "Read(**/.env.*)", "Read(**/.npmrc)", "Read(**/.yarnrc.yml)", "Read(**/.pypirc)",
              "Read(**/.netrc)", "Read(**/*.pem)", "Read(**/*.key)")
DOCS = ("README.md", "docs/PROJECT.md", "AGENTS.md", "CLAUDE.md", "docs/README.md", "ROADMAP.md", "docs/ROADMAP.md")
SPEC_DIRS = ("docs", "specs", "spec", "doc")
MAX_TITLE = 160
MAX_DETAIL = 600

# What a `Durum:` value means, ASCII-folded (memory._fold).
_ITEM_WORDS = {"todo": "todo", "yapilacak": "todo", "bekliyor": "todo", "backlog": "todo", "ticket": "ticket",
               "in-progress": "in-progress", "in progress": "in-progress", "suruyor": "in-progress", "calisiyor": "in-progress",
               "review": "review", "inceleme": "review", "pr": "pr", "merged": "merged", "birlesti": "merged",
               "done": "done", "bitti": "done"}
_PHASE_WORDS = {"aktif": "active", "active": "active", "suruyor": "active", "in-progress": "active", "bitti": "done",
                "done": "done", "tamam": "done", "todo": "todo", "yapilacak": "todo", "bekliyor": "todo", "backlog": "todo"}
_PHASE_WRITE = {"active": "aktif", "done": "bitti"}
# BMad's sprint-status values; one not listed counts as todo, with a warning.
_BMAD_STORY = {"backlog": "todo", "ready-for-dev": "todo", "drafted": "todo", "contexted": "todo", "in-progress": "in-progress",
               "review": "review", "ready-for-review": "review", "done": "done"}
_BMAD_EPIC = {"backlog": "todo", "in-progress": "active", "contexted": "active", "done": "done"}

_PHASE = re.compile(r"^##\s+[Pp]\s*-?\s*(\d{1,4})\b\s*(?:[·|:—–-]\s*)?(.*?)\s*$")
_PHASE_LIKE = re.compile(r"^##\s+[Pp]\s*-?\s*\d")
_ITEM = re.compile(r"^[-*+]\s+\[([ xX])\]\s*(.*?)\s*$")
_ITEM_ID = re.compile(r"^[Rr]\s*-?\s*(\d{1,6})\b\s*(?:[·|:—–-]\s*)?(.*)$")
_TOP_FIELD = re.compile(r"^[-*+]\s+([^:\[]{1,20}?)\s*:\s*(.*?)\s*$")
_PART = re.compile(r"^([^:]{1,20}?)\s*:\s*(.*)$")
_PARTS = re.compile(r"\s+[·|]\s+")
_META_KEYS = {"ticket": "Ticket", "pr": "PR", "durum": "Durum", "status": "Durum"}
_EPIC = re.compile(r"^##\s+Epic\s+(\d+)\s*[:.·—–-]\s*(.*?)\s*$", re.I)
_STORY = re.compile(r"^###\s+Story\s+(\d+)\.(\d+)\s*[:.·—–-]\s*(.*?)\s*$", re.I)
_SPRINT_KEY = re.compile(r"^\s+['\"]?([A-Za-z0-9][\w.-]*)['\"]?\s*:\s*['\"]?([^'\"#]*?)['\"]?\s*(?:#.*)?$")
TICKET_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RoadmapError(ValueError):
    pass


def _line(text, limit: int = MAX_DETAIL) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def root(target: Target) -> Path:
    return target.state_dir / "roadmap"


def jobs_dir(target: Target) -> Path:
    return root(target) / "jobs"


def memory_path(target: Target) -> Path:
    return memory.location(target).dir / FILE


def bmad_files(target: Target) -> tuple[Path, Path]:
    return target.path / EPICS, target.path / SPRINT


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# --- the memory source: parsing ---------------------------------------------------------------------------------

def _meta(text: str) -> tuple[list[list[str]], bool]:
    """An item's sub-line as [[key, value] | [raw]] parts, and whether it is the Ticket/PR/Durum line."""
    parts, known = [], False
    for raw in _PARTS.split(text.strip()):
        m = _PART.match(raw.strip())
        key = _META_KEYS.get(memory._fold(m.group(1).strip())) if m else None
        if key:
            parts.append([key, m.group(2).strip()])
            known = True
        else:
            parts.append([raw.strip()])
    return parts, known


def parse(text: str) -> tuple[list[dict], list[str]]:
    """roadmap.md → (phases with their items and line numbers, warnings). Lines it does not know are skipped here
    and stay in the file."""
    lines = text.split("\n")
    start, problem = memory._split_frontmatter(lines)
    warnings = [f"{FILE}: {problem}"] if problem else []
    phases, seen_phase, seen_item = [], {}, {}
    phase = item = None
    for i in range(start, len(lines)):
        line = lines[i]
        where = f"{FILE}:{i + 1}"
        if re.match(r"^#{1,6}\s", line):
            item = None
            m = _PHASE.match(line)
            if m:
                pid = f"P{int(m.group(1))}"
                if not m.group(2):
                    warnings.append(f"{where}: {pid} başlıksız")
                if pid in seen_phase:
                    warnings.append(f"{where}: {pid} {seen_phase[pid]} içinde de var; ilki kullanılıyor")
                    phase = None
                    continue
                seen_phase[pid] = where
                phase = {"id": pid, "n": int(m.group(1)), "title": m.group(2), "status": "todo", "line": i, "durum": None,
                         "end": i, "items": [], "notes": []}
                phases.append(phase)
            elif line.startswith("## "):
                if _PHASE_LIKE.match(line):
                    warnings.append(f"{where}: evre başlığı okunamadı: {line[3:83]}")
                phase = None
            continue
        if not line.strip():
            continue
        indented = line[:1] in (" ", "\t")
        if phase is None:
            if not indented and _ITEM.match(line):
                warnings.append(f"{where}: madde bir evrenin (## P1 · başlık) altında değil: {line.strip()[:80]}")
            continue
        phase["end"] = i
        if not indented:
            m = _ITEM.match(line)
            if m:
                item = None
                idm = _ITEM_ID.match(m.group(2))
                if not idm:
                    warnings.append(f"{where}: maddenin kimliği yok (R-<n>): {m.group(2)[:80]}")
                    continue
                iid = f"R-{int(idm.group(1))}"
                if iid in seen_item:
                    warnings.append(f"{where}: {iid} {seen_item[iid]} içinde de var; ilki kullanılıyor")
                    continue
                seen_item[iid] = where
                title = idm.group(2).strip()
                if not title:
                    warnings.append(f"{where}: {iid} başlıksız")
                item = {"id": iid, "n": int(idm.group(1)), "title": title, "checked": m.group(1) in "xX", "line": i,
                        "meta": None, "parts": [], "notes": [], "end": i}
                phase["items"].append(item)
                continue
            item = None
            f = _TOP_FIELD.match(line)
            if f and memory._fold(f.group(1)) in ("durum", "status"):
                word = _PHASE_WORDS.get(memory._fold(f.group(2)).strip())
                if word is None:
                    warnings.append(f"{where}: {phase['id']} durumu okunamadı: {f.group(2)[:40]} (aktif ya da bitti)")
                elif phase["durum"] is None:
                    phase["status"], phase["durum"] = word, i
                continue
            phase["notes"].append(line.strip())
            continue
        if item is None:
            continue
        item["end"] = i
        body = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        parts, known = _meta(body)
        if known and item["meta"] is None:
            item["meta"], item["parts"] = i, parts
        else:
            item["notes"].append(body)
    for p in phases:
        for it in p["items"]:
            _derive(it, warnings)
    return phases, warnings


def _derive(it: dict, warnings: list[str]) -> None:
    fields = {p[0]: p[1] for p in it["parts"] if len(p) == 2}
    it["ticket"] = fields.get("Ticket", "").split(" ")[0].strip() or None
    it["pr"] = fields.get("PR") or None
    durum = None
    if fields.get("Durum"):
        durum = _ITEM_WORDS.get(memory._fold(fields["Durum"]).strip())
        if durum is None:
            warnings.append(f"{FILE}:{it['meta'] + 1}: {it['id']} durumu okunamadı: {fields['Durum'][:40]}")
    if it["checked"]:
        it["status"] = "merged" if durum == "merged" else "done"
    else:
        it["status"] = durum or ("ticket" if it["ticket"] else "todo")
    it["detail"] = " ".join(it["notes"])


def _public_phase(p: dict) -> dict:
    return {"id": p["id"], "title": p["title"], "status": p["status"],
            "items": [{k: it[k] for k in ("id", "title", "status", "ticket", "pr", "detail")} for it in p["items"]]}


# --- the bmad source --------------------------------------------------------------------------------------------

def sprint_status(text: str) -> tuple[dict[str, str], list[str]]:
    """The `development_status:` mapping of sprint-status.yaml as {key: value}, read by hand (no YAML library):
    indented `key: value` lines under that key, up to the next unindented line."""
    out, warnings, inside = {}, [], False
    for i, line in enumerate(text.split("\n")):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            inside = line.split("#", 1)[0].strip() == "development_status:"
            continue
        if not inside:
            continue
        m = _SPRINT_KEY.match(line)
        if not m:
            warnings.append(f"sprint-status.yaml:{i + 1}: okunamadı: {line.strip()[:80]}")
            continue
        out[m.group(1)] = m.group(2).strip().lower()
    return out, warnings


def _story_detail(lines: list[str], start: int) -> str:
    """The story's user-story lines (As a … / I want … / So that …), up to its first bold label or heading."""
    out = []
    for line in lines[start:]:
        if line.startswith("#") or line.strip().startswith("**"):
            break
        if line.strip():
            out.append(line.strip())
    return _line(" ".join(out))


def parse_bmad(epics: str, sprint: str | None) -> tuple[list[dict], list[str]]:
    lines = epics.split("\n")
    body, _ = memory._split_frontmatter(lines)
    phases, warnings, phase = [], [], None
    for i in range(body, len(lines)):
        e = _EPIC.match(lines[i])
        if e:
            phase = {"id": f"E{int(e.group(1))}", "n": int(e.group(1)), "title": e.group(2), "status": "todo", "items": []}
            phases.append(phase)
            continue
        if lines[i].startswith("## "):
            phase = None
            continue
        s = _STORY.match(lines[i])
        if s:
            if phase is None:
                continue  # the Epic List's summaries (### Epic N) and stories outside an epic section
            if int(s.group(1)) != phase["n"]:
                warnings.append(f"epics.md:{i + 1}: Story {s.group(1)}.{s.group(2)} Epic {phase['n']} altında")
            phase["items"].append({"id": f"{int(s.group(1))}-{int(s.group(2))}", "title": s.group(3), "status": "todo",
                                   "ticket": None, "pr": None, "detail": _story_detail(lines, i + 1)})
    statuses, problems = sprint_status(sprint) if sprint is not None else ({}, [])
    warnings += problems
    by_story = {}
    for key, value in statuses.items():
        m = re.match(r"^(\d+)-(\d+)(?:-|$)", key)
        if m:
            by_story[f"{int(m.group(1))}-{int(m.group(2))}"] = (key, value)
    known = {it["id"] for p in phases for it in p["items"]}
    for sid, (key, value) in by_story.items():
        if sid not in known:
            warnings.append(f"sprint-status.yaml: {key} epics.md'de yok")
    for p in phases:
        for it in p["items"]:
            key, value = by_story.get(it["id"], (None, None))
            if value is None:
                continue
            if value not in _BMAD_STORY:
                warnings.append(f"sprint-status.yaml: {key}: bilinmeyen durum '{value}'; yapılacak sayıldı")
            it["status"] = _BMAD_STORY.get(value, "todo")
        epic = statuses.get(f"epic-{p['n']}")
        if epic is not None and epic not in _BMAD_EPIC:
            warnings.append(f"sprint-status.yaml: epic-{p['n']}: bilinmeyen durum '{epic}'")
        p["status"] = _BMAD_EPIC.get(epic or "", "todo")
    for p in phases:
        p.pop("n", None)
    return phases, warnings


def _bmad_phase_status(p: dict) -> str:
    items = p["items"]
    if items and all(it["status"] in FINISHED for it in items):
        return "done"
    if p["status"] == "done" and not items:
        return "done"
    if p["status"] == "active" or any(it["status"] != "todo" for it in items):
        return "active"
    return "todo"


# --- links and tickets ------------------------------------------------------------------------------------------

def _links_path(target: Target) -> Path:
    return root(target) / "links.json"


def links(target: Target) -> dict[str, str]:
    try:
        data = state.read_json(_links_path(target))
    except (OSError, ValueError):
        return {}
    raw = data.get("links") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (raw or {}).items() if isinstance(v, str)}


def _save_links(target: Target, data: dict[str, str]) -> None:
    root(target).mkdir(parents=True, exist_ok=True)
    state.write_json(_links_path(target), {"links": data})


def _origins(target: Target) -> dict[str, str]:
    """{item: ticket} from the tickets made from an item (the oldest wins)."""
    out = {}
    for tid in state.ticket_ids(target):
        try:
            o = state.load_ticket(target, tid).get("origin") or {}
        except (OSError, ValueError, LookupError):
            continue
        if o.get("kind") == "roadmap" and o.get("item"):
            out.setdefault(str(o["item"]), tid)
    return out


def items_of(target: Target, ticket_id: str, ticket: dict | None = None) -> list[str]:
    """The items a ticket (its ticket.json when given) works on: its origin's item first, then the items linked to it
    (a lead proposal drafted from several items links them all)."""
    if ticket is None:
        try:
            ticket = state.load_ticket(target, ticket_id)
        except (OSError, ValueError, LookupError):
            ticket = {}
    o = ticket.get("origin") or {}
    found = [str(o["item"])] if o.get("kind") == "roadmap" and o.get("item") else []
    found += [item for item, tid in links(target).items() if tid == ticket_id]
    return list(dict.fromkeys(found))


def item_of(target: Target, ticket_id: str, ticket: dict | None = None) -> str | None:
    """The item a ticket (its ticket.json when given) works on: its origin, else the first link."""
    return next(iter(items_of(target, ticket_id, ticket)), None)


def ticket_status(target: Target, ticket_id: str) -> str:
    """Where a ticket is, as an item status: ticket (not started), in-progress, review (done), pr, merged."""
    st = state.load_state(target, ticket_id)
    if st.get("pr_state") == "MERGED":
        return "merged"
    if st.get("pr"):
        return "pr"
    if st.get("status") == "done":
        return "review"
    if st.get("status") == "new" and not state.running(target, ticket_id):
        return "ticket"
    return "in-progress"


def _ticket_exists(target: Target, ticket_id: str) -> bool:
    return bool(TICKET_ID.match(ticket_id or "")) and (state.run_dir(target, ticket_id) / "ticket.json").is_file()


# --- loading ----------------------------------------------------------------------------------------------------

def load(target: Target) -> dict:
    """{source: memory | bmad | None, path, warnings, phases: [{id, title, status, items: [{id, title, status, ticket,
    pr, detail}]}], shadowed: the roadmap.md that BMad's epics hide (or None)}. Never raises on what the files hold."""
    epics_path, sprint_path = bmad_files(target)
    mem = memory_path(target)
    try:
        epics = _read(epics_path)
    except (OSError, UnicodeDecodeError) as exc:
        epics = None
        bmad_warning = [f"epics.md: okunamadı: {exc}"]
    else:
        bmad_warning = []
    if epics is not None:
        try:
            sprint = _read(sprint_path)
        except (OSError, UnicodeDecodeError) as exc:
            sprint, bmad_warning = None, [f"sprint-status.yaml: okunamadı: {exc}"]
        phases, warnings = parse_bmad(epics, sprint)
        known = {**_origins(target), **links(target)}
        for p in phases:
            for it in p["items"]:
                tid = known.get(it["id"])
                if tid and _ticket_exists(target, tid):
                    it["ticket"] = tid
                    try:
                        live = ticket_status(target, tid)
                        it["pr"] = state.load_state(target, tid).get("pr")
                    except (OSError, ValueError, LookupError):
                        live = "ticket"
                    if RANK[live] > RANK[it["status"]] and it["status"] not in FINISHED:
                        it["status"] = live
            p["status"] = _bmad_phase_status(p)
        return {"source": "bmad", "path": str(epics_path), "warnings": bmad_warning + warnings, "phases": phases,
                "shadowed": str(mem) if mem.is_file() else None}
    try:
        text = _read(mem)
    except (OSError, UnicodeDecodeError) as exc:
        return {"source": "memory", "path": str(mem), "warnings": bmad_warning + [f"{FILE}: okunamadı: {exc}"], "phases": [],
                "shadowed": None}
    if text is None:
        return {"source": None, "path": None, "warnings": bmad_warning, "phases": [], "shadowed": None}
    phases, warnings = parse(text)
    known = links(target)
    origins = _origins(target)
    for p in phases:
        for it in p["items"]:
            it["ticket"] = it["ticket"] or known.get(it["id"]) or origins.get(it["id"])
    if not phases:
        warnings.append(f"{FILE}: evre yok (## P1 · başlık)")
    return {"source": "memory", "path": str(mem), "warnings": bmad_warning + warnings,
            "phases": [_public_phase(p) for p in phases], "shadowed": None}


def warnings(target: Target) -> list[str]:
    """What reading the roadmap gave (the inbox's memory row and the Hafıza page show them)."""
    try:
        return load(target)["warnings"]
    except Exception as exc:  # noqa: BLE001 - a warning about the warnings, never a crash
        return [f"{FILE}: okunamadı: {exc}"]


def current_phase(phases: list[dict]) -> dict | None:
    """The active phase, else the first unfinished one."""
    return next((p for p in phases if p["status"] == "active"), None) or next((p for p in phases if p["status"] != "done"), None)


def next_items(target: Target, n: int = 3) -> list[dict]:
    """The first not-yet-started items (status todo, no ticket) of the current phase, in order, each with `phase`."""
    phase = current_phase(load(target)["phases"])
    if phase is None:
        return []
    return [{**it, "phase": phase["id"]} for it in phase["items"] if it["status"] == "todo" and not it["ticket"]][:n]


def find(data: dict, item_id: str) -> tuple[dict, dict] | None:
    for p in data["phases"]:
        for it in p["items"]:
            if it["id"] == item_id:
                return p, it
    return None


def normalize_item(item_id: str) -> str:
    raw = str(item_id or "").strip()
    m = _ITEM_ID.match(raw)
    if m and not m.group(2):
        return f"R-{int(m.group(1))}"
    m = re.fullmatch(r"(\d{1,4})[-.](\d{1,4})(?:-.*)?", raw)
    return f"{int(m.group(1))}-{int(m.group(2))}" if m else raw


# --- writing progress -------------------------------------------------------------------------------------------

def _write(target: Target, lines: list[str]) -> None:
    memory._write(memory_path(target), memory._touch("\n".join(lines)))


def _find_raw(phases: list[dict], item_id: str) -> tuple[dict, dict] | None:
    for p in phases:
        for it in p["items"]:
            if it["id"] == item_id:
                return p, it
    return None


def _set_item(lines: list[str], it: dict, ticket: str | None, pr: str | None, status: str) -> None:
    """The item's `Ticket · PR · Durum` line (rewritten in place, its other parts kept; added under the item when
    missing) and, once merged, its checkbox."""
    parts = [list(p) for p in it["parts"]]
    keyed = {p[0]: p for p in parts if len(p) == 2}
    for key, value in (("Ticket", ticket), ("PR", pr), ("Durum", status)):
        if not value:
            continue
        if key in keyed:
            keyed[key][1] = value
        else:
            parts.append([key, value])
    order = {"Ticket": 0, "PR": 1, "Durum": 2}
    parts.sort(key=lambda p: order.get(p[0], 3) if len(p) == 2 else 3)
    text = " · ".join(f"{p[0]}: {p[1]}" if len(p) == 2 else p[0] for p in parts)
    if it["meta"] is not None:
        indent = re.match(r"^\s*", lines[it["meta"]]).group(0) or "  "
        lines[it["meta"]] = f"{indent}- {text}"
    else:
        lines.insert(it["line"] + 1, f"  - {text}")
    if status in FINISHED and not it["checked"]:
        lines[it["line"]] = lines[it["line"]].replace("[ ]", "[x]", 1)


def _set_phase(lines: list[str], p: dict, status: str) -> None:
    line = f"- Durum: {_PHASE_WRITE[status]}"
    if p["durum"] is not None:
        lines[p["durum"]] = line
    else:
        lines.insert(p["line"] + 1, line)


def _rollup(lines: list[str]) -> list[str]:
    """A phase whose items are all merged or done becomes `bitti`; with no unfinished phase active, the first
    unfinished one with items becomes `aktif`. Returns the lines (edited in place)."""
    phases, _ = parse("\n".join(lines))
    for p in reversed(phases):  # bottom up: an inserted line never moves a phase still to edit
        if p["items"] and p["status"] != "done" and all(it["status"] in FINISHED for it in p["items"]):
            _set_phase(lines, p, "done")
    phases, _ = parse("\n".join(lines))
    if not any(p["status"] == "active" for p in phases):
        first = next((p for p in phases if p["status"] != "done" and p["items"]), None)
        if first is not None:
            _set_phase(lines, first, "active")
    return lines


def _progress(target: Target, item_id: str, ticket_id: str, status: str, pr: str | None) -> bool:
    """Under the memory lock: write one item's progress; False when nothing changed."""
    text = _read(memory_path(target))
    if text is None:
        return False
    lines = text.split("\n")
    phases, _ = parse(text)
    found = _find_raw(phases, item_id)
    if found is None:
        return False
    _, it = found
    if it["status"] == "done":
        status = "done"  # checked by hand: finished, whatever the ticket says
    elif RANK[status] < RANK[it["status"]]:
        status = it["status"]  # never back
    if it["ticket"] == ticket_id and it["pr"] == (pr or it["pr"]) and it["status"] == status \
            and any(len(p) == 2 and p[0] == "Durum" for p in it["parts"]):
        return False
    _set_item(lines, it, ticket_id, pr, status)
    _write(target, _rollup(lines))
    return True


def ticket_changed(target: Target, ticket_id: str) -> bool:
    """A ticket was created, started, finished, published or merged: for each item of the target's roadmap.md it works
    on, that item's status follows (no approval). Never raises (a hook of the ticket's lifecycle)."""
    try:
        item_ids = [i for i in items_of(target, ticket_id) if i.startswith("R-")]
        if not item_ids:
            return False
        if bmad_files(target)[0].is_file():
            return False  # BMad's files are read-only; load() shows the ticket's progress over them
        status = ticket_status(target, ticket_id)
        pr = state.load_state(target, ticket_id).get("pr")
        with memory._lock(target):
            changed = [_progress(target, item_id, ticket_id, status, pr) for item_id in item_ids]
        return any(changed)
    except Exception as exc:  # noqa: BLE001 - a missed status line never holds up the ticket
        print(f"{ticket_id}: roadmap not updated: {exc}", file=sys.stderr)
        return False


def link(target: Target, item_id: str, ticket_id: str) -> None:
    """Remember that `ticket_id` works on `item_id` (links.json in the state); a roadmap.md item shows it at once."""
    item_id = normalize_item(item_id)
    if not _ticket_exists(target, ticket_id):
        raise LookupError(f"no ticket {ticket_id} in {target.name}")
    data = load(target)
    if find(data, item_id) is None:
        raise LookupError(f"no roadmap item {item_id} in {target.name}")
    with memory._lock(target):
        saved = links(target)
        saved[item_id] = ticket_id
        _save_links(target, saved)
    ticket_changed(target, ticket_id)


def on_created(target: Target, ticket: dict) -> None:
    """ticket.create made a ticket from an item (origin kind roadmap): link it and mark the item. Never raises."""
    origin = ticket.get("origin") or {}
    if origin.get("kind") != "roadmap" or not origin.get("item"):
        return
    try:
        with memory._lock(target):
            saved = links(target)
            saved[str(origin["item"])] = ticket["id"]
            _save_links(target, saved)
    except Exception as exc:  # noqa: BLE001 - the ticket exists; its origin still links it
        print(f"{ticket['id']}: roadmap link not saved: {exc}", file=sys.stderr)
    ticket_changed(target, ticket["id"])


def origin(target: Target, body) -> dict:
    """A new ticket's origin from the composer ({kind: roadmap, item}): the item must be in the roadmap and not
    started yet. Returns {kind, item, phase}."""
    if not isinstance(body, dict) or body.get("kind") != "roadmap":
        raise ValueError("origin must be {kind: roadmap, item}")
    item_id = normalize_item(body.get("item"))
    found = find(load(target), item_id)
    if found is None:
        raise LookupError(f"no roadmap item {item_id} in {target.name}")
    phase, it = found
    if it["ticket"] and _ticket_exists(target, it["ticket"]):
        raise ValueError(f"roadmap item {item_id} is already ticket {it['ticket']}")
    if it["status"] != "todo":
        raise ValueError(f"roadmap item {item_id} is {it['status']}, not to do")
    return {"kind": "roadmap", "item": item_id, "phase": phase["id"]}


_STRUCTURE: dict[tuple, tuple[tuple, dict]] = {}  # (target, files) -> (the files' mtimes, {item: (phase, title)})


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _structure(target: Target) -> dict[str, tuple[str, str]]:
    """{item: (phase, title)}, read again only when roadmap.md, epics.md or sprint-status.yaml changed."""
    paths = (memory_path(target), *bmad_files(target))
    key, sig = (target.name, *map(str, paths)), tuple(_mtime(p) for p in paths)
    hit = _STRUCTURE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    out = {it["id"]: (p["id"], it["title"]) for p in load(target)["phases"] for it in p["items"]}
    _STRUCTURE[key] = (sig, out)
    return out


def ticket_ref(target: Target, ticket_id: str, ticket: dict | None = None) -> dict | None:
    """{item, phase, title} of the item a ticket works on (the ticket page's 'Yol haritası: P2 · R-5'); None when it
    works on none. Never raises."""
    try:
        item_id = item_of(target, ticket_id, ticket)
        if not item_id:
            return None
        phase, title = _structure(target).get(item_id, (None, None))
    except Exception:  # noqa: BLE001 - the ticket row must render whatever the roadmap holds
        return None
    return {"item": item_id, "phase": phase, "title": title}


# --- scope changes: memory candidates of kind `yol` -------------------------------------------------------------

OPS = {"add": "ekle", "remove": "çıkar", "move": "taşı", "phase": "evre ekle"}
_OP_OF = {memory._fold(v): k for k, v in OPS.items()} | {k: k for k in OPS} | {"cikar": "remove", "tasi": "move", "evre": "phase"}
OP, PHASE, ITEM, TITLE, DETAIL, WHY = "İşlem", "Evre", "Madde", "Başlık", "Ayrıntı", "Neden"


def check_change(fields: dict) -> None:
    """A `yol` candidate's fields, checked and normalized in place (memory.clean_proposal calls it)."""
    op = _OP_OF.get(memory._fold(fields.get(OP, "")).strip())
    if op is None:
        raise memory.MemoryRefused(f"{OP} must be one of {', '.join(OPS.values())}")
    fields[OP] = OPS[op]
    if fields.get(PHASE):
        m = re.fullmatch(r"\s*[Pp]\s*-?\s*(\d{1,4})\s*", fields[PHASE])
        if not m:
            raise memory.MemoryRefused(f"{PHASE} is a phase id such as P2")
        fields[PHASE] = f"P{int(m.group(1))}"
    if fields.get(ITEM):
        fields[ITEM] = normalize_item(fields[ITEM])
        if not re.fullmatch(r"R-\d{1,6}", fields[ITEM]):
            raise memory.MemoryRefused(f"{ITEM} is an item id such as R-3")
    need = {"add": (PHASE, TITLE), "remove": (ITEM,), "move": (ITEM, PHASE), "phase": (TITLE,)}[op]
    missing = [k for k in need if not fields.get(k)]
    if missing:
        raise memory.MemoryRefused(f"{OPS[op]} needs {', '.join(missing)}")


def _summary(op: str, change: dict) -> str:
    title = _line(change.get("title"), 120)
    return {"add": f"{change.get('phase')} evresine ekle: {title}", "remove": f"{change.get('item')} çıkar",
            "move": f"{change.get('item')} → {change.get('phase')}", "phase": f"Yeni evre: {title}"}[op]


def propose(target: Target, changes: list[dict], origin: str, ticket: str | None = None) -> list[dict]:
    """Scope changes ({op: add|remove|move|phase, phase, item, title, detail, why}) as pending memory candidates of
    kind `yol` (approved on the Hafıza page). Returns the ones added; malformed ones are skipped."""
    proposed = []
    for ch in changes:
        op = ch.get("op")
        if op not in OPS:
            continue
        fields = {OP: OPS[op], PHASE: ch.get("phase"), ITEM: ch.get("item"), TITLE: ch.get("title"),
                  DETAIL: ch.get("detail"), WHY: ch.get("why")}
        proposed.append({"kind": "yol", "title": _summary(op, ch), "fields": {k: str(v) for k, v in fields.items() if v},
                         "scope": {}, "sources": []})
    return memory.add_candidates(target, proposed, origin, ticket) if proposed else []


def _new_file(target: Target) -> list[str]:
    return memory._frontmatter(target, "yol").split("\n")


def _insert_at(lines: list[str], at: int, block: list[str]) -> None:
    lines[at:at] = block


def apply_change(target: Target, fields: dict) -> str:
    """Make an approved `yol` candidate's edit in roadmap.md; returns the item or phase id it made or touched.
    The caller holds the memory lock (memory.decide)."""
    if bmad_files(target)[0].is_file():
        raise memory.MemoryRefused("bu projenin yol haritası BMad'in epics.md'sinden okunuyor; değişiklik orada yapılır")
    check_change(fields)
    op = _OP_OF[memory._fold(fields[OP])]
    path = memory_path(target)
    text = _read(path)
    if text is None and op != "phase":
        raise memory.MemoryRefused(f"{FILE} yok; önce bir yol haritası kaydet")
    lines = (text if text is not None else "\n".join(_new_file(target))).split("\n")
    phases, _ = parse("\n".join(lines))
    by_phase = {p["id"]: p for p in phases}
    detail = _line(fields.get(DETAIL))
    if op == "phase":
        n = max((p["n"] for p in phases), default=0) + 1
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"## P{n} · {_line(fields[TITLE], MAX_TITLE)}"] + ([detail] if detail else []) + [""]
        made = f"P{n}"
    elif op == "add":
        p = by_phase.get(fields[PHASE])
        if p is None:
            raise memory.MemoryRefused(f"{fields[PHASE]} yol haritasında yok")
        used = [it["n"] for q in phases for it in q["items"]]
        used += [int(k[2:]) for k in links(target) if re.fullmatch(r"R-\d+", k)]
        made = f"R-{max(used, default=0) + 1}"
        _insert_at(lines, p["end"] + 1, [f"- [ ] {made} · {_line(fields[TITLE], MAX_TITLE)}"] + ([f"  - {detail}"] if detail else []))
    else:
        found = _find_raw(phases, fields[ITEM])
        if found is None:
            raise memory.MemoryRefused(f"{fields[ITEM]} yol haritasında yok")
        _, it = found
        made = it["id"]
        if op == "move" and fields[PHASE] not in by_phase:
            raise memory.MemoryRefused(f"{fields[PHASE]} yol haritasında yok")
        block = lines[it["line"]: it["end"] + 1]
        del lines[it["line"]: it["end"] + 1]
        if op == "move":
            target_phase = {p["id"]: p for p in parse("\n".join(lines))[0]}[fields[PHASE]]
            _insert_at(lines, target_phase["end"] + 1, block)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(target, _rollup(lines))
    return made


# --- the draft: a read-only session proposes phases and items ---------------------------------------------------

def session_dirs(target: Target) -> list[Path]:
    base = root(target) / "drafts"
    if not base.is_dir():
        return []
    found = [(int(m.group(1)), p) for p in base.iterdir() if p.is_dir() and (m := state.PHASE_DIR.match(p.name))]
    return [p for _, p in sorted(found)]


def _record(d: Path) -> dict:
    try:
        data = state.read_json(d / "run.json")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fence(text: str, info: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def _docs(repo: Path) -> list[str]:
    found = [d for d in DOCS if (repo / d).is_file()]
    for folder in SPEC_DIRS:
        base = repo / folder
        if base.is_dir():
            found += sorted(str(p.relative_to(repo)) for p in base.rglob("*.md") if ".git" not in p.parts)[:30]
    return list(dict.fromkeys(found))[:40]


def prompt(target: Target) -> str:
    found, _ = memory.entries(target)
    decisions = [f"- {e['id']} · {e['title']}: {e['fields'].get('Karar', '')}" for e in found
                 if e["kind"] == "karar" and e["status"] == "active"]
    context = memory.context_text(target)
    ctx = {
        "project": {"name": target.name, "path": str(target.path)},
        "docs": [{"path": d} for d in _docs(target.path)],
        "context": {"exists": bool(context.strip()), "text": _fence(context, "markdown")},
        "decisions": {"exists": bool(decisions), "text": _fence("\n".join(decisions), "markdown")},
        "max_phases": results.MAX_ROADMAP_PHASES, "max_items": results.MAX_ROADMAP_ITEMS,
    }
    return render.render(pipelines.prompt_text(PROMPT), ctx, pipelines.partial_text).rstrip() + "\n"


def draft_markdown(phases: list[dict]) -> str:
    """The session's phases as roadmap.md's body (no frontmatter): the first phase active, ids R-1… in order."""
    out, n = [], 0
    for i, p in enumerate(phases, 1):
        out += [f"## P{i} · {_line(p.get('title'), MAX_TITLE)}"] + (["- Durum: aktif"] if i == 1 else [])
        if p.get("goal"):
            out.append(_line(p["goal"]))
        for it in p.get("items") or []:
            n += 1
            out.append(f"- [ ] R-{n} · {_line(it.get('title'), MAX_TITLE)}")
            if it.get("detail"):
                out.append(f"  - {_line(it['detail'])}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _json_in(message: str | None) -> str | None:
    text = (message or "").strip()
    return text if text.startswith(("{", "```")) else None


def run_draft(target: Target) -> dict:
    """One draft session (the job's body): reads the repository, its memory's context and decisions, and answers
    with phases and items; the record keeps them as markdown (`draft`) for the panel to edit and save."""
    base = root(target) / "drafts"
    base.mkdir(parents=True, exist_ok=True)
    with state.waiting_lock(target, "roadmap-draft", f"roadmap draft of {target.name}", timeout=TIMEOUT + 300, poll=1):
        count = max((int(state.PHASE_DIR.match(p.name).group(1)) for p in session_dirs(target)), default=0)
        session = base / f"{count + 1:02d}-{STAGE}"
        session.mkdir()
        text = prompt(target)
        (session / "prompt.md").write_text(text, encoding="utf-8")
        result_file = session / "result.json"
        record = {"stage": STAGE, "phase": STAGE, "title": "Yol haritası taslağı", "target": target.name}
        env = {"PIPELINE_STAGE": STAGE, "PIPELINE_TICKET": STAGE, "PIPELINE_INBOX": str(session),
               "PIPELINE_RESULT_FILE": str(result_file), "GIT_OPTIONAL_LOCKS": "0"}
        budget = min(target.budget_usd, BUDGET_USD) if target.budget_usd else BUDGET_USD
        dispatch.run(dataclasses.replace(target, budget_usd=budget), STAGE, target.path, session, session, target.roadmap_model, text,
                     record=record, timeout=TIMEOUT, tools_deny=TOOLS_DENY, json_schema=results.json_schema(STAGE), extra_env=env)
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
        phases = (result or {}).get("phases") or []
        record["draft"] = draft_markdown(phases) if record["state"] == "ok" and record["verdict"] == "READY" and phases else None
        state.write_json(session / "run.json", record)
    return record


def draft_running(target: Target) -> bool:
    from .dashboard import read_jobs  # the panel's job records; dashboard imports this module
    return any(j["state"] == "running" for j in read_jobs(jobs_dir(target)))


def start_draft(target: Target) -> dict:
    """'Yol haritası çıkar': the draft session as a background job (only without a roadmap, one at a time)."""
    if load(target)["source"] is not None:
        raise RoadmapError(f"{target.name} already has a roadmap")
    if draft_running(target):
        raise RoadmapError("a roadmap draft is already being written")
    from .dashboard import spawn_job
    return spawn_job(jobs_dir(target), ["--target", target.name, "roadmap", "draft"])


def draft_view(target: Target) -> dict | None:
    """The newest draft: {running, state, verdict, summary, notes, draft, error, cost, started_at, dir}; None before
    the first one."""
    from .dashboard import read_jobs
    job = (read_jobs(jobs_dir(target)) or [None])[-1]
    dirs = session_dirs(target)
    if not dirs and not job:
        return None
    r = _record(dirs[-1]) if dirs else {}
    result = r.get("result") or {}
    running = bool(job and job["state"] == "running")
    return {"running": running, "state": "running" if running else r.get("state") or (job or {}).get("state"),
            "verdict": r.get("verdict"), "summary": result.get("summary"), "notes": result.get("notes") or [],
            "draft": r.get("draft"), "error": r.get("result_error") or ((job or {}).get("tail") if job and job["state"] == "error" else None),
            "cost": r.get("total_cost_usd"), "started_at": r.get("started_at") or (job or {}).get("started_at"),
            "dir": str(dirs[-1]) if dirs else None}


def save(target: Target, text: str) -> dict:
    """'Taslak olarak kaydet': the edited draft becomes roadmap.md (with the memory files' frontmatter when it has
    none). Only when the target has no roadmap; it must hold at least one phase. Returns load()."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the roadmap is empty")
    if len(text) > 200_000:
        raise ValueError("the roadmap is too long")
    current = load(target)
    if current["source"] == "bmad":
        raise RoadmapError("this project's roadmap is BMad's epics.md (read-only here)")
    body = text.replace("\r\n", "\n").strip() + "\n"
    if not body.startswith("---"):
        body = memory._frontmatter(target, "yol") + "\n" + body
    phases, _ = parse(body)
    if not phases:
        raise ValueError("the draft has no phase (## P1 · title)")
    path = memory_path(target)
    with memory._lock(target):
        if path.exists():
            raise RoadmapError(f"{path} already exists; edit it in Obsidian")
        path.parent.mkdir(parents=True, exist_ok=True)
        memory._write(path, memory.scrub(target, body))
    return load(target)


# --- the panel --------------------------------------------------------------------------------------------------

def view(target: Target) -> dict:
    """The project page's Yol haritası section: the roadmap with each phase's progress, where it lives, the next
    items, and (without one) the newest draft."""
    data = load(target)
    loc = memory.location(target)
    phases = []
    for p in data["phases"]:
        done = sum(1 for it in p["items"] if it["status"] in FINISHED)
        phases.append({**p, "done": done, "total": len(p["items"])})
    current = current_phase(data["phases"])
    nxt = [it["id"] for it in (current or {}).get("items", []) if it["status"] == "todo" and not it["ticket"]][:3]
    return {**data, "phases": phases, "where": "bmad" if data["source"] == "bmad" else loc.source, "vault_dir": loc.vault_dir,
            "memory_path": str(loc.dir / FILE), "current": (current or {}).get("id"), "next": nxt,
            "draft": draft_view(target) if data["source"] is None else None, "model": target.roadmap_model}


def cli_lines(data: dict) -> list[str]:
    out = []
    for p in data["phases"]:
        done = sum(1 for it in p["items"] if it["status"] in FINISHED)
        out.append(f"{p['id']}\t{p['status']}\t{done}/{len(p['items'])}\t{p['title']}")
        for it in p["items"]:
            extra = " ".join(x for x in (it.get("ticket"), it.get("pr")) if x)
            out.append(f"  {it['id']}\t{it['status']}\t{it['title']}" + (f"\t({extra})" if extra else ""))
    return out
