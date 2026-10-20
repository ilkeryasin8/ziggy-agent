"""The stage result protocol: the JSON a stage's agent writes, how it is validated, and the footer that
tells the agent how to write it.

A stage's result comes from, in order: the result file (validated against the stage's schema), the CLI's
structured output (`--json-schema`, `structured_output` on the result event), the last `VERDICT:` line of
the final message. With none of them the stage fails: a missing result is never a silent pass. Enum
fields are read leniently (trimmed, lowercased; an unknown value falls back), `verdict` strictly."""
import json
import os
import posixpath
import re

VERDICTS = ("READY", "NEEDS_USER", "FAILED")
RISKS = ("low", "medium", "high")
SIZES = ("trivial", "small", "medium", "large")
SEVERITIES = ("low", "medium", "high", "critical")
FINDING_KINDS = ("bug", "missing", "partial", "contradicts", "unrequested", "risk")
DISPOSITIONS = ("fix", "defer", "dismiss", "decision")
FINDING_CLASSES = ("important", "nit", "pre-existing")
OUTCOMES = ("fixed", "skipped", "no_change")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ARTIFACT_KINDS = ("image", "video", "log", "text")
MAX_ARTIFACTS = 100
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_VIDEO = {".mp4", ".webm", ".mov"}
_TEXT = {".txt", ".log", ".md", ".json", ".xml", ".csv", ".yaml", ".yml"}
_CONFIDENCE_WORDS = {"low": 0.3, "medium": 0.6, "high": 0.9}
HEADING = "## Result protocol (ziggy)"
CHECKS_FAILED = "checksFailed"

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_QUESTION = {"type": "object", "required": ["text"], "properties": {
    "id": _STR, "text": _STR, "options": _STRS, "recommendation": _STR}}
_CHECK = {"type": "object", "required": ["name", "status"], "properties": {
    "name": _STR, "status": _STR, "detail": _STR, "source": _STR}}
_BASE = {
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "summary": _STR,
    "notes": _STR,
    "questions": {"type": "array", "items": _QUESTION},
    "route": {"type": "string", "description": "Only for steps that list routes: the route to take, together with READY"},
    "askThen": {"type": "string", "description": "Only with NEEDS_USER, and only when this step's instructions ask for it: "
                                                 "the step that continues with the answers"},
}
_RATING = {"risk": {"type": "string", "enum": list(RISKS)}, "size": {"type": "string", "enum": list(SIZES)}}
_FINDING = {"type": "object", "properties": {
    "id": _STR, "severity": {"type": "string", "enum": list(SEVERITIES)},
    "kind": {"type": "string", "enum": list(FINDING_KINDS)}, "location": _STR, "evidence": _STR, "summary": _STR,
    "disposition": {"type": "string", "enum": list(DISPOSITIONS)}, "reason": _STR,
    "class": {"type": "string", "enum": list(FINDING_CLASSES),
              "description": "important = must be fixed in this change; nit = worth a mention only; "
                             "pre-existing = not introduced by this change"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "verified": {"type": "boolean"},
    "memory": {"type": "string", "description": "The project memory rule or decision (M-<n>) this finding violates"}}}
_MEMORY_USED = {"type": "array", "items": _STR,
                "description": "Ids (M-<n>) of the project memory entries you relied on; empty when none applied"}
_ARTIFACT = {"anyOf": [_STR, {"type": "object", "required": ["path"], "properties": {
    "path": _STR, "kind": {"type": "string", "enum": list(ARTIFACT_KINDS)}, "caption": _STR}}]}
_ARTIFACTS = {"type": "array", "items": _ARTIFACT}
_GUIDE = {"type": "object", "required": ["file"], "properties": {
    "file": _STR, "summary": _STR, "howToVerify": _STR, "order": {"type": "number"}}}
_UNVERIFIED = {"type": "object", "properties": {"location": _STR, "summary": _STR, "reason": _STR},
               "description": "A candidate the verification pass could not confirm (kept out of the plan, listed for the record)"}
_COUNT = {"type": "integer", "minimum": 0}
_PROTECTED_PATCHES = {"type": "array", "description": (
    "Changes to protected files (you cannot write them): each a unified diff you saved under the artifacts dir, with "
    "paths relative to the repository root, that `git apply` applies on the branch HEAD. The user applies it after "
    "review; listing one is not a failure."), "items": {"type": "object", "required": ["path", "files"], "properties": {
        "path": {"type": "string", "description": "artifacts/<name>.patch"}, "files": _STRS, "summary": _STR}}}
_OUTCOME = {"type": "object", "required": ["finding_id", "outcome"], "properties": {
    "finding_id": _STR, "outcome": {"type": "string", "enum": list(OUTCOMES)}, "note": _STR}}
_INTENT_GAP = {"type": "object", "required": ["summary", "questions"], "properties": {
    "summary": _STR, "evidence": _STR, "questions": {"type": "array", "items": _QUESTION}},
    "description": "Only with NEEDS_USER, and only when the step's instructions describe it: the change answers a "
                   "different intent than the ticket and PLAN (not a bug)"}
_MEMORY_SOURCE = {"type": "object", "required": ["ticket", "ref"], "properties": {"ticket": _STR, "ref": _STR}}
MEMORY_KINDS = ("kural", "karar", "tuzak")
_MEMORY_ITEM = {"type": "object", "required": ["kind", "title", "sources"], "properties": {
    "kind": {"type": "string", "enum": list(MEMORY_KINDS),
             "description": "kural = a standing rule; karar = a decision and its reason; tuzak = a pitfall and what to do"},
    "title": {"type": "string", "description": "One line naming it"},
    "rule": {"type": "string", "description": "kural: one imperative sentence, specific to this repository"},
    "decision": {"type": "string", "description": "karar: what was decided"},
    "why": {"type": "string", "description": "kural, karar: why"},
    "happens": {"type": "string", "description": "tuzak: what goes wrong, and when"},
    "todo": {"type": "string", "description": "tuzak: what to do instead"},
    "paths": {"type": "array", "items": _STR, "description": "Globs relative to the repository root where it applies"},
    "topics": {"type": "array", "items": _STR, "description": "A few keywords it is about"},
    "sources": {"type": "array", "items": _MEMORY_SOURCE}}}
MAX_MEMORY = 5
_ONBOARD_PREPARE = {"type": "object", "required": ["run", "why"], "properties": {
    "run": {"type": "string", "description": "One shell command, run in a fresh checkout before every run (network on)"},
    "why": _STR}}
_ONBOARD_ENV = {"type": "object", "required": ["key", "value", "why"], "properties": {
    "key": _STR, "value": {"type": "string", "description": "A dummy, non-secret value"}, "why": _STR}}
_ONBOARD_CHECK = {"type": "object", "required": ["name", "run", "why"], "properties": {
    "name": {"type": "string", "description": "Letters, digits, space, . _ - (max 40)"},
    "run": {"type": "string", "description": "Read-only, non-interactive, ends by itself, no network"}, "why": _STR}}
MAX_ONBOARD_ITEMS = 20
MAX_ROADMAP_PHASES = 8
MAX_ROADMAP_ITEMS = 12  # per phase
_ROADMAP_ITEM = {"type": "object", "required": ["title"], "properties": {
    "title": {"type": "string", "description": "One line: a piece of work one ticket can deliver"},
    "detail": {"type": "string", "description": "One or two sentences: what done looks like, which docs or code it rests on"}}}
_ROADMAP_PHASE = {"type": "object", "required": ["title", "items"], "properties": {
    "title": {"type": "string", "description": "A short name for the phase"},
    "goal": {"type": "string", "description": "One sentence: what the project can do once the phase is done"},
    "items": {"type": "array", "maxItems": MAX_ROADMAP_ITEMS, "items": _ROADMAP_ITEM}}}
LEAD_TYPES = ("feat", "fix", "refactor", "chore", "docs")  # ticket types a lead draft may have (no project tickets)
MAX_LEAD = 5
_LEAD_DRAFT = {"type": "object", "required": ["title", "type", "body", "why", "sources"], "properties": {
    "title": {"type": "string", "description": "The ticket's title: one line, what gets done"},
    "type": {"type": "string", "enum": list(LEAD_TYPES)},
    "body": {"type": "string", "description": "The ticket text (markdown): what and why, where, what not to touch, how to verify"},
    "why": {"type": "string", "description": "One sentence: why this is the next thing to do"},
    "sources": {"type": "array", "items": _STR, "description": "Keys of the signals this draft comes from"},
    "size": {"type": "string", "enum": list(SIZES)}}}

_PROPS = {
    "generic": {"protectedPatches": _PROTECTED_PATCHES},
    "plan": {"planPath": _STR, "filesExpected": _STRS, **_RATING},
    "verify": {"changesMade": _STRS, **_RATING},
    "implement": {
        "commit": {"type": "object", "required": ["hash"], "properties": {"hash": _STR, "message": _STR, "branch": _STR}},
        "filesChanged": {"type": "array", "items": {"anyOf": [_STR, {"type": "object", "required": ["path"],
                                                                     "properties": {"path": _STR, "why": _STR}}]}},
        "checks": {"type": "array", "items": _CHECK},
        "artifacts": _ARTIFACTS,
        "openItems": _STRS,
        "outcomes": {"type": "array", "items": _OUTCOME},
        "intentGap": _INTENT_GAP,
        "protectedPatches": _PROTECTED_PATCHES,
    },
    "review": {
        "findings": {"type": "array", "items": _FINDING},
        "converged": {"type": "boolean"},
        "fixedIds": _STRS,
        "deferredIds": _STRS,
        "unverified": {"type": "array", "items": _UNVERIFIED},
        "counts": {"type": "object", "description": "Open findings per class (filled from findings when missing)",
                   "properties": {"important": _COUNT, "nit": _COUNT, "pre_existing": _COUNT, "unverified": _COUNT}},
        "guide": {"type": "array", "items": _GUIDE},
        "checks": {"type": "array", "items": _CHECK},
        "artifacts": _ARTIFACTS,
        "intentGap": _INTENT_GAP,
    },
    "doc": {"docPath": _STR, **_RATING},
    # The retro session (retro.py) after a ticket is done: not a pipeline stage's schema.
    "retro": {"memory": {"type": "array", "maxItems": MAX_MEMORY, "items": _MEMORY_ITEM}},
    # The onboarding session (onboard_agent.py) before a target exists: not a pipeline stage's schema either.
    "onboard": {
        "prepare": {"type": "array", "items": _ONBOARD_PREPARE},
        "env": {"type": "array", "items": _ONBOARD_ENV},
        "checks": {"type": "array", "items": _ONBOARD_CHECK},
        "notes": _STRS,
        "agents_md": {"type": "string", "description": "A draft AGENTS.md when the repository has none, else empty"},
    },
    # The roadmap draft session (roadmap.py): phases and items for a repository without a roadmap; not a stage either.
    "roadmap": {
        "phases": {"type": "array", "maxItems": MAX_ROADMAP_PHASES, "items": _ROADMAP_PHASE},
        "notes": _STRS,
    },
    # The morning lead (lead.py): ticket drafts from the target's signals; not a pipeline stage's schema either.
    "lead": {"drafts": {"type": "array", "maxItems": MAX_LEAD, "items": _LEAD_DRAFT}},
}
ENGINE_ONLY = ("retro", "onboard", "roadmap", "lead")
NAMES = tuple(n for n in _PROPS if n not in ENGINE_ONLY)  # what a pipeline stage's `result` may name


def json_schema(name: str) -> dict:
    extra = {} if name in ENGINE_ONLY else {"memoryUsed": _MEMORY_USED}
    return {"type": "object", "required": ["verdict"], "properties": {**_BASE, **extra, **_PROPS[name]}}


def _lenient(value, allowed: tuple, fallback):
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return fallback


def _strings(value, where: str, errors: list) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: expected a list of strings")
        return []
    return value


def _checks(value, errors: list) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("checks: expected a list")
        return []
    out = []
    for i, c in enumerate(value):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("status"), str):
            errors.append(f"checks.{i}: needs name and status strings")
            continue
        out.append(c)
    return out


def _norm_class(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = re.sub(r"[\s_]+", "-", value.strip().lower())
    name = "pre-existing" if name == "preexisting" else name
    return name if name in FINDING_CLASSES else None


def derive_class(finding: dict) -> str:
    """An explicit class wins; else a deferred finding is pre-existing, a low one a nit, the rest important."""
    explicit = _norm_class(finding.get("class"))
    if explicit:
        return explicit
    if finding.get("disposition") == "defer":
        return "pre-existing"
    if finding.get("severity") == "low":
        return "nit"
    return "important"


def _confidence(value) -> float | None:
    """0..1; "85%", 85 and "high" read leniently; anything else is dropped."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        try:
            value = float(text.removesuffix("%").strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or value != value:
        return None
    if 1 < value <= 100:
        value = value / 100
    return float(value) if 0 <= value <= 1 else None


def _count(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value == value and value >= 0:
        return int(value)
    return None


def _counts(given, findings: list[dict], unverified: int) -> dict:
    """Open findings (not dismissed) per class; each field the agent gave wins over the derived one."""
    open_ = [f for f in findings if f.get("disposition") != "dismiss"]
    derived = {"important": sum(f["class"] == "important" for f in open_), "nit": sum(f["class"] == "nit" for f in open_),
               "pre_existing": sum(f["class"] == "pre-existing" for f in open_), "unverified": unverified}
    given = given if isinstance(given, dict) else {}
    out = {}
    for key, value in derived.items():
        raw = given.get(key, given.get("preExisting")) if key == "pre_existing" else given.get(key)
        own = _count(raw)
        out[key] = value if own is None else own
    return out


def settle_review_route(result: dict, last_round: bool) -> list[str]:
    """The review prompt's routing rule, applied to a READY result of a review stage with `fix` and `converged`
    routes: an open `fix` finding (not in `fixedIds`) sends the work back whatever its class or severity. On the
    last round (`last_round`: no review or fix visit left in this run) the open ones that are not important are
    deferred instead, and with no important one left the review converges. Changes `result` in place (route,
    converged, the deferred findings' disposition, deferredIds); returns warnings naming what it changed."""
    if result.get("verdict") != "READY":
        return []
    fixed = set(result.get("fixedIds") or [])
    open_fix = [f for f in result.get("findings") or [] if f.get("disposition") == "fix" and f.get("id") not in fixed]
    route, warnings = result.get("route"), []
    if last_round:
        small = [f for f in open_fix if f.get("class") != "important"]
        if small:
            for f in small:
                f["disposition"] = "defer"
            ids = [f["id"] for f in small]
            result["deferredIds"] = list(dict.fromkeys([*(result.get("deferredIds") or []), *ids]))
            warnings.append(f"last round: {', '.join(ids)} deferred instead of sent back")
            open_fix = [f for f in open_fix if f.get("class") == "important"]
            if not open_fix and route != "converged":
                warnings.append(f'route "{route or "-"}" → "converged": nothing important is open on the last round')
                result.update(route="converged", converged=True)
                return warnings
    elif open_fix and route != "fix":
        ids = ", ".join(f["id"] for f in open_fix)
        warnings.append(f'route "{route or "-"}" → "fix": {ids} still open with disposition fix')
        result.update(route="fix", converged=False)
    if not open_fix and result.get("route") == "fix":
        warnings.append('route "fix" with no open finding to fix')
    return warnings


def _order(value) -> float | int | None:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return int(value) if float(value).is_integer() else value


def _guide(value) -> list[dict]:
    out = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not entry["file"].strip():
            continue
        item = {**entry, "summary": entry.get("summary") if isinstance(entry.get("summary"), str) else ""}
        order = _order(entry.get("order"))
        if order is None:
            item.pop("order", None)
        else:
            item["order"] = order
        out.append(item)
    return out


def _outcomes(value, errors: list) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("outcomes: expected a list")
        return []
    out = []
    for o in value:
        if not isinstance(o, dict):
            continue
        fid = o.get("finding_id", o.get("id"))
        if not isinstance(fid, str) or not fid.strip():
            continue
        item = {"finding_id": fid.strip(), "outcome": _lenient(o.get("outcome"), OUTCOMES, "no_change")}
        if isinstance(o.get("note"), str) and o["note"].strip():
            item["note"] = o["note"].strip()
        out.append(item)
    return out


def memory_id(value) -> str | None:
    """`M-<n>` from what an agent wrote ("m-7", " M - 7 "), else None."""
    m = re.fullmatch(r"\s*[Mm]\s*-\s*(\d{1,7})\s*", value) if isinstance(value, str) else None
    return f"M-{int(m.group(1))}" if m else None


def _memory_items(value, errors: list) -> list[dict]:
    """The retro's memory proposals: {kind, title, rule|decision|happens+todo, why, paths, topics, sources
    [{ticket, ref}]}, one-line strings, at most MAX_MEMORY; an entry without a kind or title is dropped (retro.py
    checks the fields each kind needs and the sources against the evidence it sent)."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("memory: expected a list")
        return []
    out = []
    for r in value:
        if not isinstance(r, dict) or not isinstance(r.get("title"), str) or not r["title"].strip():
            continue
        kind = _lenient(r.get("kind"), MEMORY_KINDS, None)
        if kind is None:
            continue
        sources = [{"ticket": str(s.get("ticket") or "").strip(), "ref": str(s.get("ref") or "").strip()}
                   for s in r.get("sources") or [] if isinstance(s, dict)] if isinstance(r.get("sources"), list) else []
        item = {"kind": kind, "title": " ".join(r["title"].split()), "sources": [s for s in sources if s["ref"]]}
        for key in ("rule", "decision", "why", "happens", "todo"):
            item[key] = " ".join(r[key].split()) if isinstance(r.get(key), str) else ""
        for key in ("paths", "topics"):
            item[key] = [" ".join(x.split()) for x in r.get(key) or [] if isinstance(x, str) and x.strip()] \
                if isinstance(r.get(key), list) else []
        out.append(item)
    return out[:MAX_MEMORY]


def _onboard_items(value, key: str, fields: tuple[str, ...], errors: list) -> list[dict]:
    """The onboarding session's lines of one kind: each with its `fields` as one-line strings (an env value may be
    empty, the others may not) and a `why`; malformed entries are dropped, at most MAX_ONBOARD_ITEMS."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{key}: expected a list")
        return []
    out = []
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(item.get(f), str) for f in fields):
            continue
        entry = {f: item[f].strip() for f in fields}
        if any("\n" in v for v in entry.values()) or not all(v for f, v in entry.items() if f != "value"):
            continue
        entry["why"] = " ".join(str(item.get("why") or "").split())
        out.append(entry)
    return out[:MAX_ONBOARD_ITEMS]


def _lead_drafts(value, errors: list) -> list[dict]:
    """The lead's ticket drafts: {title, type, body, why, sources, size}, at most MAX_LEAD; one without a title or with
    an unknown type is dropped (lead.py keeps only the drafts whose sources name signals it sent)."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("drafts: expected a list")
        return []
    out = []
    for d in value:
        if not isinstance(d, dict) or not isinstance(d.get("title"), str) or not d["title"].strip():
            continue
        kind = _lenient(d.get("type"), LEAD_TYPES, None)
        if kind is None:
            continue
        sources = [s.strip() for s in d.get("sources") or [] if isinstance(s, str) and s.strip()] \
            if isinstance(d.get("sources"), list) else []
        out.append({"title": " ".join(d["title"].split()), "type": kind,
                    "body": d["body"].strip() if isinstance(d.get("body"), str) else "",
                    "why": " ".join(d["why"].split()) if isinstance(d.get("why"), str) else "",
                    "sources": list(dict.fromkeys(sources)), "size": _lenient(d.get("size"), SIZES, None)})
    return out[:MAX_LEAD]


def _artifact_list(value, errors: list) -> list:
    """The raw listing (strings or {path, kind, caption}); paths are normalized where the stage settles."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("artifacts: expected a list")
        return []
    return [a for a in value if isinstance(a, (str, dict))]


def artifact_kind(path: str) -> str:
    ext = posixpath.splitext(path.lower())[1]
    if ext in _IMAGE:
        return "image"
    if ext in _VIDEO:
        return "video"
    if ext == ".log":
        return "log"
    return "text" if ext in _TEXT else "other"


def normalize_artifacts(items, artifacts_dir: str) -> list[dict]:
    """Evidence listings as [{path, kind, caption}], `path` relative to `artifacts_dir`: absolute paths inside it
    are made relative, anything escaping it is dropped, the first listing of a path wins, at most 100.
    Pure path arithmetic; the file itself is checked where it is served."""
    if not isinstance(items, list):
        return []
    bases = list(dict.fromkeys([artifacts_dir, os.path.realpath(artifacts_dir)]))
    out, seen = [], set()
    for item in items:
        raw = item if isinstance(item, str) else item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            continue
        rel = raw.strip().replace("\\", "/")
        if os.path.isabs(rel):
            inside = [os.path.relpath(rel, b) for b in bases]
            rel = next((r for r in inside if r != ".." and not r.startswith("../")), inside[0])
        rel = posixpath.normpath(rel.replace("\\", "/"))
        while rel.startswith("./"):
            rel = rel[2:]
        if rel in ("", ".", "..") or rel.startswith("../") or posixpath.isabs(rel) or rel in seen:
            continue
        seen.add(rel)
        given = item.get("kind") if isinstance(item, dict) else None
        kind = given.strip().lower() if isinstance(given, str) and given.strip().lower() in ARTIFACT_KINDS else artifact_kind(rel)
        caption = item.get("caption") if isinstance(item, dict) else None
        caption = caption.strip()[:500] if isinstance(caption, str) and caption.strip() else None
        out.append({"path": rel, "kind": kind, "caption": caption})
        if len(out) >= MAX_ARTIFACTS:
            break
    return out


def _questions(items: list, where: str, errors: list) -> list[dict]:
    qs = []
    for i, q in enumerate(items):
        if isinstance(q, str) and q.strip():
            q = {"text": q}
        if not isinstance(q, dict) or not isinstance(q.get("text"), str) or not q["text"].strip():
            errors.append(f"{where}.{i}.text: required")
            continue
        qid = q.get("id")
        qs.append({**q, "id": str(qid).strip() if qid is not None and str(qid).strip() else f"q{i + 1}",
                   "options": [str(o) for o in (q.get("options") or []) if str(o).strip()][:6],
                   "recommendation": q.get("recommendation") if isinstance(q.get("recommendation"), str) else ""})
    return qs


def _intent_gap(value, errors: list) -> dict | None:
    """{summary, evidence, questions}: what the change answers instead of the ticket's intent, and the questions
    that settle it (at least one)."""
    if not isinstance(value, dict):
        errors.append("intentGap: expected {summary, evidence, questions}")
        return None
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("intentGap.summary: required")
    questions = value.get("questions")
    qs = _questions(questions, "intentGap.questions", errors) if isinstance(questions, list) else []
    if not qs:
        errors.append("intentGap.questions: at least one question")
    evidence = value.get("evidence")
    return {"summary": summary.strip() if isinstance(summary, str) else "",
            "evidence": evidence.strip() if isinstance(evidence, str) else "", "questions": qs}


def normalize(name: str, data) -> tuple[dict | None, list[str]]:
    """The validated result with defaults filled in, or None and the problems (at most a few)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["(root): expected a JSON object"]
    out = dict(data)  # extra keys are kept
    if data.get("verdict") not in VERDICTS:
        errors.append(f"verdict: expected one of {', '.join(VERDICTS)}")
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        errors.append("summary: expected a string")
    out["summary"] = summary if isinstance(summary, str) else ""
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        errors.append("questions: expected a list")
        questions = []
    out["questions"] = _questions(questions, "questions", errors)
    route = data.get("route")
    if isinstance(route, str) and route.strip():
        out["route"] = route.strip()
    else:
        out.pop("route", None)
    ask_then = data.get("askThen")
    if isinstance(ask_then, str) and ask_then.strip():
        out["askThen"] = ask_then.strip()
    else:
        out.pop("askThen", None)
    if name in ("plan", "verify", "doc"):
        out["risk"] = _lenient(data.get("risk"), RISKS, None)
        out["size"] = _lenient(data.get("size"), SIZES, None)
    if name == "plan":
        out["filesExpected"] = _strings(data.get("filesExpected"), "filesExpected", errors)
    if name == "verify":
        out["changesMade"] = _strings(data.get("changesMade"), "changesMade", errors)
    if name == "implement":
        commit = data.get("commit")
        if commit is not None and not (isinstance(commit, dict) and isinstance(commit.get("hash"), str)):
            errors.append("commit: expected {hash, message}")
        files = data.get("filesChanged") or []
        if not isinstance(files, list):
            errors.append("filesChanged: expected a list")
            files = []
        out["filesChanged"] = [f if isinstance(f, str) else f.get("path", "") for f in files
                               if isinstance(f, str) or (isinstance(f, dict) and isinstance(f.get("path"), str))]
        out["checks"] = _checks(data.get("checks"), errors)
        out["artifacts"] = _artifact_list(data.get("artifacts"), errors)
        out["openItems"] = _strings(data.get("openItems"), "openItems", errors)
        out["outcomes"] = _outcomes(data.get("outcomes"), errors)
    if name in ("implement", "generic"):
        patches = data.get("protectedPatches")
        if patches is not None and not isinstance(patches, list):
            errors.append("protectedPatches: expected a list")
        if patches is not None:
            out["protectedPatches"] = [p for p in patches if isinstance(p, (dict, str))] if isinstance(patches, list) else []
    if name == "review":
        findings = data.get("findings") or []
        if not isinstance(findings, list):
            errors.append("findings: expected a list")
            findings = []
        fs = []
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                errors.append(f"findings.{i}: expected an object")
                continue
            fid = f.get("id")
            finding = {**f, "id": str(fid).strip() if fid is not None and str(fid).strip() else f"f{i + 1}",
                       "severity": _lenient(f.get("severity"), SEVERITIES, "medium"),
                       "kind": _lenient(f.get("kind"), FINDING_KINDS, "bug"),
                       "disposition": _lenient(f.get("disposition"), DISPOSITIONS, "fix"),
                       "summary": f.get("summary") if isinstance(f.get("summary"), str) else ""}
            finding["class"] = derive_class(finding)
            confidence = _confidence(f.get("confidence"))
            if confidence is None:
                finding.pop("confidence", None)
            else:
                finding["confidence"] = confidence
            if not isinstance(f.get("verified"), bool):
                finding.pop("verified", None)
            if memory_id(f.get("memory")):
                finding["memory"] = memory_id(f["memory"])
            else:
                finding.pop("memory", None)
            fs.append(finding)
        out["findings"] = fs
        out["converged"] = data.get("converged") is True
        out["fixedIds"] = _strings(data.get("fixedIds"), "fixedIds", errors)
        out["deferredIds"] = _strings(data.get("deferredIds"), "deferredIds", errors)
        out["checks"] = _checks(data.get("checks"), errors)
        unverified = data.get("unverified") or []
        if not isinstance(unverified, list):
            errors.append("unverified: expected a list")
            unverified = []
        out["unverified"] = [{**u, "summary": u.get("summary") if isinstance(u.get("summary"), str) else ""}
                             for u in unverified if isinstance(u, dict)]
        out["counts"] = _counts(data.get("counts"), fs, len(out["unverified"]))
        out["guide"] = _guide(data.get("guide"))
        out["artifacts"] = _artifact_list(data.get("artifacts"), errors)
    if name in ("implement", "review"):
        out.pop("intentGap", None)  # only a NEEDS_USER result reports one
        if data.get("intentGap") is not None and data.get("verdict") == "NEEDS_USER":
            out["intentGap"] = _intent_gap(data["intentGap"], errors)
    if name not in ENGINE_ONLY:
        used = data.get("memoryUsed")
        out["memoryUsed"] = list(dict.fromkeys(i for i in map(memory_id, used) if i)) if isinstance(used, list) else []
    if name == "retro":
        out["memory"] = _memory_items(data.get("memory"), errors)
    if name == "onboard":
        for key, fields in (("prepare", ("run",)), ("env", ("key", "value")), ("checks", ("name", "run"))):
            out[key] = _onboard_items(data.get(key), key, fields, errors)
        out["notes"] = [" ".join(n.split()) for n in _strings(data.get("notes"), "notes", errors) if n.strip()][:MAX_ONBOARD_ITEMS]
        agents = data.get("agents_md")
        out["agents_md"] = agents.strip() if isinstance(agents, str) else ""
    if name == "roadmap":
        out["phases"] = _roadmap_phases(data.get("phases"), errors)
        out["notes"] = [" ".join(n.split()) for n in _strings(data.get("notes"), "notes", errors) if n.strip()][:20]
    if name == "lead":
        out["drafts"] = _lead_drafts(data.get("drafts"), errors)
    return (None, errors[:3]) if errors else (out, [])


def _one(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _roadmap_phases(value, errors: list) -> list[dict]:
    """The draft session's phases: {title, goal, items [{title, detail}]}, one-line strings; a phase or item without
    a title is dropped, and so is a phase left without items; at most MAX_ROADMAP_PHASES × MAX_ROADMAP_ITEMS."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("phases: expected a list")
        return []
    out = []
    for p in value:
        if not isinstance(p, dict) or not _one(p.get("title")):
            continue
        raw = p.get("items") if isinstance(p.get("items"), list) else []
        items = [{"title": _one(i.get("title")), "detail": _one(i.get("detail"))} for i in raw if isinstance(i, dict) and _one(i.get("title"))]
        if items:
            out.append({"title": _one(p["title"]), "goal": _one(p.get("goal")), "items": items[:MAX_ROADMAP_ITEMS]})
    return out[:MAX_ROADMAP_PHASES]


_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```$", re.S)
_VERDICT = re.compile(r"VERDICT:\s*\**\s*(READY|NEEDS_USER|FAILED)\b")


def resolve(name: str, file_text: str | None, structured, final_message: str) -> dict:
    """{result, source ("file"|"structured"|"verdict"|None), warnings[], error|None}."""
    warnings: list[str] = []
    from_file = None
    if file_text is not None:
        text = file_text.strip()
        if not text:
            warnings.append("result file is empty")
        else:
            fenced = _FENCE.match(text)
            try:
                data = json.loads(fenced.group(1) if fenced else text)
            except ValueError as exc:
                warnings.append(f"result file is not valid JSON: {exc}")
            else:
                from_file, errors = normalize(name, data)
                if from_file is None:
                    warnings.append(f"result file does not match the {name} schema: {'; '.join(errors)}")
    from_structured = None
    if structured is not None:
        from_structured, errors = normalize(name, structured)
        if from_structured is None:
            warnings.append("structured output does not match the result schema")
    if from_file is not None:
        if from_structured is not None and from_structured["verdict"] != from_file["verdict"]:
            warnings.append(f"structured output says {from_structured['verdict']} but the result file says "
                            f"{from_file['verdict']}; using the file")
        return {"result": from_file, "source": "file", "warnings": warnings, "error": None}
    if from_structured is not None:
        return {"result": from_structured, "source": "structured", "warnings": warnings, "error": None}
    matches = list(_VERDICT.finditer(final_message or ""))
    if matches:
        last = matches[-1]
        verdict = last.group(1)
        summary = (final_message[:last.start()].strip())[-2000:]
        result, _ = normalize(name, {"verdict": verdict, "summary": summary})
        if verdict == "NEEDS_USER":
            result["questions"] = [{"id": "q1", "text": summary or "The agent needs a decision (see its last message).",
                                    "options": [], "recommendation": ""}]
        warnings.append("no result file; fell back to the VERDICT line in the final message")
        return {"result": result, "source": "verdict", "warnings": warnings, "error": None}
    error = "the result file is invalid" if file_text is not None else "the agent did not write a result file"
    return {"result": None, "source": None, "warnings": warnings, "error": error}


def footer(result_file: str, name: str, can_ask: bool, routes: list[str], ask_targets: list[str] = (),
           artifacts_dir: str | None = None) -> str:
    """Appended to every stage prompt `ask_targets`: where answers to
    this step's questions may continue, the default first; `artifacts_dir`: where evidence goes."""
    ask = ("- `NEEDS_USER`: you need a decision only the user can make. Put each question in `questions` "
           "(`text`, 2-4 `options`, and your `recommendation`). Do not ask anything you can settle from the code or docs."
           if can_ask else
           "- `NEEDS_USER` is not available in this step; decide yourself and explain in `summary`.")
    lines = [
        "",
        "---",
        HEADING,
        "",
        f"When you are done, write your result as JSON to `{result_file}`",
        "(the same path is in the environment variable `PIPELINE_RESULT_FILE`). Write it as your very last action,",
        "then reply with a short plain-text summary. The file must match this JSON Schema:",
        "",
        "```json",
        json.dumps(json_schema(name), indent=2),
        "```",
        "",
        "Verdicts:",
        "- `READY`: this step is complete.",
        ask,
        "- `FAILED`: you could not complete the step; say why in `summary`.",
    ]
    if routes:
        lines += [
            "",
            f"Routes (this step): with `READY`, also set `route` to one of: {', '.join(f'`{r}`' for r in routes)}.",
            "The route decides the next step. Leave `route` unset with `NEEDS_USER` or `FAILED`.",
        ]
    if can_ask and len(ask_targets) > 1:
        lines += [
            "",
            f"Answers continue at `{ask_targets[0]}`. Only when this step's instructions say so, set `askThen` with "
            f"`NEEDS_USER` to continue at one of: {', '.join(f'`{t}`' for t in ask_targets)}.",
        ]
    if artifacts_dir and "artifacts" in _PROPS[name]:
        lines += [
            "",
            f"Evidence: save screenshots, recordings and logs that show the work under `{artifacts_dir}` (also",
            "`PIPELINE_ARTIFACTS_DIR`) and list them in `artifacts` as `{ path, kind, caption }`: `path` relative to that",
            "directory, `kind` image, video, log or text, and a one-line `caption` saying what it proves.",
        ]
    lines += ["", "Never ask the user questions in chat: nobody is watching this session. Questions only go into the result file.", ""]
    return "\n".join(lines)
