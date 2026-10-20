"""Project memory: what a target's tickets taught, kept as Obsidian-friendly markdown that every stage prompt reads.

Where it lives (`location`): the target's `memory_dir` when set; else, with `[memory] vault` configured, the vault's
projects map (`projects_map`, default `<vault>/.claude/scripts/projects.json`) when one of its `roots` has a
`prefix` equal to the target's checkout (expanded, resolved) and a `vault_dir` folder that exists:
`<vault>/<vault_dir>/memory`. Without one, the longest parent entry whose project is one named folder (a project
without `{leaf}`, e.g. `~/Developer/acme/field` → `Acme/Field` holding a frontend and a backend repo) gives each repo
its own subfolder, `<vault>/<vault_dir>/memory/<repo folder>`, so the repos share the project folder without mixing.
A catch-all parent (`{leaf}` in its project, one vault folder for many unrelated repos) is never used; then it is
`<state>/<target>/memory` and the panel names the fallback. Only that folder of the vault is read or written, and
only by the wrapper: sessions never read the vault (the sandbox), they get the recalled text.

Files in the memory folder, each created with vault-style frontmatter (title, created, modified, type: memory,
status: active, tags):
    context.md     free-form text the user writes (the old `targets/<name>/context.md`, migrated)
    rules.md       kural entries        decisions.md   karar entries
    pitfalls.md    tuzak entries        episodes.md    olay entries (one per done ticket, written without approval)
    roadmap.md     the target's long-term plan (roadmap.py; its scope changes are `yol` candidates here)
An entry:
    ## M-12 · tuzak · onnxruntime sandbox'ta dosya bırakıyor
    - Ne oluyor: …                      (kural: Kural, Neden · karar: Karar, Neden · tuzak: Ne oluyor, Ne yapılır)
    - Ne zaman geçerli: dosyalar `src/wardrobe/ingest/**` · konular onnxruntime, rembg
    - Kaynak: wardrobe local-3 · 05-review · 2026-10-16 · onay 2026-10-16
    - Durum: yerini aldı M-15 | soluk | unutuldu       (optional)
The markdown is the source of truth: it is parsed again on every use, so edits and deletions made in Obsidian win.
The parser is tolerant (field names in any case, separators `·`, `|` or ` - `, unknown lines kept and shown); what
it cannot read becomes a warning (panel, inbox), never an error. Writes append whole entries, add a line to an
episode (the merge), or change one entry's `Durum:` line in place; nothing else in a file is rewritten, and
`modified:` in the frontmatter follows each write. `M-<n>` ids are unique per target across the files and the
candidates (next = highest + 1).

Kept in the pipeline's state, under `<state>/<target>/memory/` (never the vault):
    candidates.json  proposals waiting for the user: {next, candidates: [{id c<n>, kind, title, fields, scope
                     {paths, topics}, sources [{ticket, ref, kind, summary, where, url}], origin (answer|retro|
                     migration), ticket, created_at, status pending|approved|rejected, similar [M-ids],
                     decided_at, decided_by, entry, edited, replaced}]}. Nothing reaches the files unapproved,
                     except the episodes (and roadmap progress). A `yol` candidate (fields İşlem, Evre, Madde,
                     Başlık, Ayrıntı, Neden) is a roadmap scope change: approving it edits roadmap.md (`entry` is
                     the R-/P- id it made or touched).
    usage.json       {M-id: {count, last_used, tickets}} from the stages' `memoryUsed` and review findings' `memory`
    migrated.json    what the one-time migration of the old context.md and rules/candidates.json did

`recall` picks what one stage prompt gets, within the target's `memory_budget` bytes: context.md (at most a third),
every active rule (most used and newest first), then the decisions and pitfalls that fit the ticket (score =
3 per scope glob matching the files in play + 1 per topic in the ticket's text + a recency/usage bonus), and for the
planning stages up to three similar episodes in one line each. Entries replaced (`yerini aldı`) or forgotten
(`unutuldu`) are left out. A decision, pitfall or episode unused for STALE_DAYS since its last use (or since it was
written) shows as `soluk` on the panel with a suggestion to forget it; nothing is removed on its own."""
import getpass
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import config, state
from .config import Target

KINDS = ("kural", "karar", "tuzak", "olay")
FILES = {"kural": "rules.md", "karar": "decisions.md", "tuzak": "pitfalls.md", "olay": "episodes.md"}
TITLES = {"kural": "Kurallar", "karar": "Kararlar", "tuzak": "Tuzaklar", "olay": "Olaylar", "context": "Bağlam", "yol": "Yol haritası"}
TAGS = {"kural": "kural", "karar": "karar", "tuzak": "tuzak", "olay": "olay", "context": "bağlam", "yol": "yol-haritası"}
INTROS = {
    "kural": "Bu repoda ajanların her zaman uyduğu kurallar. Onayladığın öneriler buraya eklenir.",
    "karar": "Bu repo için verilmiş kararlar ve nedenleri. Onayladığın öneriler buraya eklenir.",
    "tuzak": "Bu repoda ajanların düştüğü tuzaklar ve ne yapılacağı. Onayladığın öneriler buraya eklenir.",
    "olay": "Biten ticket'ların kaydı; Ziggy her ticket bitince kendisi ekler.",
    "context": "Bu repo için ajanların bilmesi gereken serbest notlar; her aşamanın istemine girer.",
    "yol": "Bu reponun uzun vadeli planı: evreler ve maddeleri. İlerlemeyi Ziggy kendisi işler; kapsam değişiklikleri "
           "(madde ekleme, çıkarma, taşıma, yeni evre) Hafıza'da onayına gelir.",
}
CONTEXT_FILE = "context.md"
MAIN = {"kural": "Kural", "karar": "Karar", "tuzak": "Ne yapılır", "yol": "Başlık"}  # the field "Düzenle ve onayla" edits
FIELDS = {"kural": ("Kural", "Neden"), "karar": ("Karar", "Neden"), "tuzak": ("Ne oluyor", "Ne yapılır"),
          "yol": ("İşlem", "Evre", "Madde", "Başlık", "Ayrıntı", "Neden")}  # yol: a roadmap scope change (roadmap.py)
REQUIRED = {"kural": ("Kural",), "karar": ("Karar",), "tuzak": ("Ne oluyor", "Ne yapılır"), "yol": ("İşlem",)}
SCOPE, SOURCE, STATUS = "Ne zaman geçerli", "Kaynak", "Durum"
KEY_ALIASES = {  # a field name as written (lowercase, ASCII-folded) -> its canonical name
    "kural": "Kural", "rule": "Kural", "karar": "Karar", "decision": "Karar", "neden": "Neden", "why": "Neden",
    "ne oluyor": "Ne oluyor", "ne yapilir": "Ne yapılır", "ne zaman gecerli": SCOPE, "kapsam": SCOPE, "scope": SCOPE,
    "kaynak": SOURCE, "source": SOURCE, "durum": STATUS, "status": STATUS, "ticket": "Ticket", "sonuc": "Sonuç",
    "dokunulan": "Dokunulan", "pr": "PR", "birlesti": "Birleşti", "yeniden bitti": "Yeniden bitti",
}
REPLACED, FORGOTTEN, STALE = "yerini aldı", "unutuldu", "soluk"
STALE_DAYS = 60
MAX_TITLE = 160
MAX_FIELD = 1200
MAX_CONTEXT_SHARE = 3  # context.md takes at most 1/3 of the budget
MAX_EPISODES = 3
MAX_FULL = 20000  # the retro's copy of the whole memory
DECIDED_SHOWN = 20
CANDIDATE_ID = re.compile(r"^c\d{1,7}$")
ENTRY_ID = re.compile(r"^M-(\d{1,7})$")
_HEAD = re.compile(r"^##\s+(.*?)\s*$")
_ENTRY_HEAD = re.compile(r"^[Mm]\s*-\s*(\d{1,7})\b\s*(.*)$")
_FIELD = re.compile(r"^\s*[-*]\s+([^:`]{1,40}?)\s*:\s*(.*?)\s*$")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_GLOB_CHARS = set("*?[")
_WORD = re.compile(r"[0-9a-zçğıöşü][0-9a-zçğıöşü_.\-]{2,}")
_STOP = set("""the and for with that this from into when then than have has not are was were will would should could
what which where while about after before over under only also just your you our its their them they there here
been being does done make made more most some such very each every other another same like using use used need
bir ile için gibi ama veya daha çok olan olarak sonra önce kadar şu bu her hiç değil var yok mi mı mu mü ne nasıl
neden hangi hangisi diye ise yani evet hayır olsun olmalı mısın misin mıdır midir""".split())


class MemoryRefused(ValueError):
    pass


class Decided(MemoryRefused):
    """The candidate was approved or rejected already."""


# --- where it lives ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Location:
    dir: Path
    source: str  # override | vault | state
    vault_dir: str | None = None  # the projects map's folder, when the vault is used
    hint: str | None = None  # why the vault is not used (shown on the panel)


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _vault_folder(vault: Path, rel: str) -> Path | None:
    """`<vault>/<rel>` when it stays inside the vault."""
    try:
        folder = (vault / rel).resolve()
        return folder if folder.is_relative_to(vault.resolve()) and folder != vault.resolve() else None
    except (OSError, ValueError):
        return None


def location(target: Target) -> Location:
    state_dir = target.state_dir / "memory"
    if target.memory_dir is not None:
        return Location(target.memory_dir, "override")
    try:
        mem = config.load_memory()
    except config.ConfigError as exc:
        return Location(state_dir, "state", hint=str(exc))
    if mem.vault is None:
        return Location(state_dir, "state")
    name = mem.vault.name
    try:
        data = json.loads(mem.projects_map.read_text(encoding="utf-8"))
        roots = [r for r in data.get("roots") or [] if isinstance(r, dict) and isinstance(r.get("prefix"), str)]
    except (OSError, ValueError, AttributeError) as exc:
        return Location(state_dir, "state", hint=f"{name} eşlemesi okunamadı ({mem.projects_map.name}): {exc}")
    repo = _resolved(target.path)
    parent = None
    for r in roots:
        prefix = _resolved(r["prefix"])
        if prefix == repo:
            rel = r.get("vault_dir")
            if not isinstance(rel, str) or not rel.strip() or "{" in rel:
                return Location(state_dir, "state", hint=f"{name}'ta bu repoya özel eşleme yok (projects.json: vault_dir boş)")
            folder = _vault_folder(mem.vault, rel)
            if folder is None or not folder.is_dir():
                return Location(state_dir, "state", hint=f"{name}'ta {rel} klasörü yok (projects.json)")
            return Location(folder / "memory", "vault", vault_dir=rel)
        project = r.get("project")
        if repo.is_relative_to(prefix) and isinstance(r.get("vault_dir"), str):
            if parent is None or len(str(prefix)) > len(str(parent[0])):
                parent = (prefix, r["vault_dir"], isinstance(project, str) and "{" not in project)
    hint = f"{name}'ta bu repoya özel eşleme yok (projects.json)"
    if parent:
        prefix, rel, dedicated = parent
        folder = _vault_folder(mem.vault, rel) if dedicated and rel.strip() and "{" not in rel else None
        if folder is not None and folder.is_dir():
            return Location(folder / "memory" / repo.name, "vault", vault_dir=f"{rel}/memory/{repo.name}")
        hint += (f"; üst klasörün eşlemesi ({rel}) birden çok repoyu karıştıracağı için kullanılmıyor" if not dedicated
                 else f"; üst klasörün eşlemesindeki {rel} klasörü yok")
    return Location(state_dir, "state", hint=hint)


def _state_dir(target: Target) -> Path:
    return target.state_dir / "memory"


def _lock(target: Target):
    return state.waiting_lock(target, "memory", f"project memory of {target.name}", timeout=30, poll=0.05)


def legacy_context_path(target: Target) -> Path:
    """Where the per-target context.md lived before the memory (read once, by the migration)."""
    return config.config_path().parent / "targets" / target.name / "context.md"


# --- text helpers -----------------------------------------------------------------------------------------------

def _line(text, limit: int = MAX_FIELD) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fold(text: str) -> str:
    return (text.lower().replace("ı", "i").replace("ğ", "g").replace("ü", "u").replace("ş", "s").replace("ö", "o")
            .replace("ç", "c").replace("i̇", "i"))


def norm(text: str) -> str:
    """Text as compared for duplicates: one line, lowercase, no list mark or final period."""
    text = _fold(" ".join(str(text).split()))
    text = re.sub(r"^[-*]\s+", "", text)
    text = re.sub(r"\s*\(kaynak:[^)]*\)\s*$", "", text)
    return text.rstrip(". ")


def keywords(text: str, limit: int | None = None) -> list[str]:
    """Words worth matching on: code spans first, then words of three letters or more that are not filler."""
    text = str(text or "")
    out = [s.strip().lower() for s in re.findall(r"`([^`\n]{2,60})`", text)]
    out += [w.strip(".-") for w in _WORD.findall(text.lower()) if w.strip(".-") not in _STOP and not w.isdigit()]
    out = [w for w in dict.fromkeys(out) if len(w) >= 3]
    return out[:limit] if limit else out


def _today() -> str:
    return date.today().isoformat()


def _local_day(stamp) -> date | None:
    """The local calendar day of a state timestamp (stored in UTC) or of a plain YYYY-MM-DD; the entries' dates
    are written in local time, so a UTC stamp cut at [:10] would be a day early between local midnight and the
    UTC one."""
    text = str(stamp or "").strip()
    try:
        if len(text) > 10:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (parsed.astimezone() if parsed.tzinfo else parsed).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def scrub(target: Target, text: str, secrets: set[str] | None = None) -> str:
    """Token-shaped strings and the checkout's real env/.npmrc values out (the onboarding scrubber)."""
    from . import onboard_verify
    if secrets is None:
        secrets = _secrets(target)
    return onboard_verify.redact(str(text), secrets)


def _secrets(target: Target) -> set[str]:
    from . import onboard_agent
    try:
        return onboard_agent.secret_values(target.path)
    except OSError:
        return set()


def _fence(text: str, info: str = "") -> str:
    """`text` in a code fence longer than any backtick run inside it, so quoted data cannot close it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


# --- globs and scope --------------------------------------------------------------------------------------------

def _clean_glob(pattern: str) -> str:
    p = str(pattern).strip().strip("`").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _glob_re(pattern: str) -> re.Pattern:
    p = _clean_glob(pattern)
    if not _GLOB_CHARS & set(p):  # a plain path: that file, or everything under that folder
        return re.compile(re.escape(p.rstrip("/")) + r"(?:/.*)?$")
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + r"(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(out + "$")


def glob_hits(patterns: list[str], files: list[str]) -> int:
    """How many of the patterns match at least one of the files."""
    clean = [f.lstrip("./") for f in files if f]
    hits = 0
    for p in patterns:
        if not _clean_glob(p):
            continue
        rx = _glob_re(p)
        if any(rx.match(f) for f in clean):
            hits += 1
    return hits


def _stem(pattern: str) -> str:
    p = _clean_glob(pattern)
    cut = min((p.index(c) for c in _GLOB_CHARS if c in p), default=len(p))
    return p[:cut].rsplit("/", 1)[0] if cut < len(p) else p.rstrip("/")


def _paths_overlap(a: list[str], b: list[str]) -> bool:
    for x in map(_stem, a):
        for y in map(_stem, b):
            if not x or not y or x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def _topics_overlap(a: list[str], b: list[str]) -> bool:
    return bool({_fold(t) for t in a if t} & {_fold(t) for t in b if t})


def scope_line(scope: dict) -> str:
    parts = []
    if scope.get("paths"):
        parts.append("dosyalar " + ", ".join(f"`{_clean_glob(p)}`" for p in scope["paths"]))
    if scope.get("topics"):
        parts.append("konular " + ", ".join(scope["topics"]))
    return " · ".join(parts)


def parse_scope(text: str) -> dict:
    paths, topics = [], []
    for part in re.split(r"\s+·\s+|\s*;\s*", text or ""):
        part = part.strip()
        if not part:
            continue
        low = _fold(part)
        m = re.match(r"^(dosyalar|dosya|files|paths|yollar)\s*:?\s*(.*)$", low)
        if m:
            raw = part[len(part) - len(m.group(2)):] if m.group(2) else ""
            found = re.findall(r"`([^`]+)`", raw) or [x for x in re.split(r"[,\s]+", raw) if x]
            paths += [_clean_glob(x) for x in found if _clean_glob(x)]
            continue
        m = re.match(r"^(konular|konu|topics|keywords)\s*:?\s*(.*)$", low)
        if m:
            raw = part[len(part) - len(m.group(2)):] if m.group(2) else ""
            topics += [t.strip().strip("`") for t in raw.split(",") if t.strip().strip("`")]
            continue
        code = re.findall(r"`([^`]+)`", part)
        paths += [_clean_glob(x) for x in code if _clean_glob(x)]
        rest = re.sub(r"`[^`]+`", "", part)
        topics += [t.strip() for t in rest.split(",") if t.strip()]
    return {"paths": list(dict.fromkeys(paths)), "topics": list(dict.fromkeys(topics))}


def clean_scope(scope) -> dict:
    scope = scope if isinstance(scope, dict) else {}
    paths = [_clean_glob(p) for p in scope.get("paths") or [] if isinstance(p, str) and _clean_glob(p)]
    topics = [_line(t, 60) for t in scope.get("topics") or [] if isinstance(t, str) and t.strip()]
    return {"paths": list(dict.fromkeys(paths))[:12], "topics": list(dict.fromkeys(topics))[:12]}


def folder_globs(files: list[str], limit: int = 6) -> list[str]:
    """Scope paths from concrete files: each file's folder as `<folder>/**` (a root file as itself)."""
    out = []
    for f in files:
        f = _clean_glob(f)
        if not f:
            continue
        out.append(f"{f.rsplit('/', 1)[0]}/**" if "/" in f else f)
    return list(dict.fromkeys(out))[:limit]


# --- files: frontmatter, parsing --------------------------------------------------------------------------------

def _frontmatter(target: Target, what: str) -> str:
    day = _today()
    return (f"---\ntitle: Ziggy hafızası · {target.name} · {TITLES[what]}\ncreated: {day}\nmodified: {day}\ntype: memory\n"
            f"status: active\ntags: [memory, ziggy, {TAGS[what]}]\n---\n# {TITLES[what]}\n\n{INTROS[what]}\n")


def _split_frontmatter(lines: list[str]) -> tuple[int, str | None]:
    """(index of the first body line, a warning): the frontmatter is `---` … `---` at the very top."""
    if not lines or lines[0].strip() != "---":
        return 0, None
    for i in range(1, min(len(lines), 60)):
        if lines[i].strip() == "---":
            return i + 1, None
    return 0, "frontmatter kapanmamış (--- satırı yok)"


def _touch(text: str) -> str:
    """`modified:` in the frontmatter set to today (only there; a file without one stays as it is)."""
    lines = text.split("\n")
    end, _ = _split_frontmatter(lines)
    for i in range(1, max(end - 1, 1)):
        if re.match(r"^modified\s*:", lines[i]):
            lines[i] = f"modified: {_today()}"
            break
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _key(raw: str) -> str | None:
    return KEY_ALIASES.get(_fold(" ".join(raw.split())))


def parse(text: str, kind: str, file: str) -> tuple[list[dict], list[str]]:
    """The entries of one memory file and what could not be read in it. Lines the parser does not know stay in the
    file; inside an entry they are kept as `notes`."""
    lines = text.split("\n")
    start, warning = _split_frontmatter(lines)
    warnings = [f"{file}: {warning}"] if warning else []
    entries, current = [], None
    for i in range(start, len(lines)):
        line = lines[i]
        head = _HEAD.match(line)
        if head or re.match(r"^#\s", line):
            current = None
            if not head:
                continue
            m = _ENTRY_HEAD.match(head.group(1))
            if not m:
                if re.match(r"^[Mm]\s*-", head.group(1)):
                    warnings.append(f"{file}:{i + 1}: başlık okunamadı: {head.group(1)[:80]}")
                continue
            rest = re.sub(r"^(?:·|\||-|—|–|:)\s*", "", m.group(2).strip())
            parts = [p.strip() for p in re.split(r"\s+[·|]\s+", rest) if p.strip()] if rest else []
            if len(parts) == 1:  # "M-3 - kural - title" or "M-3 — title"
                dashed = [p.strip() for p in re.split(r"\s+[-—–]\s+", parts[0], maxsplit=1)]
                if len(dashed) == 2 and _fold(dashed[0]) in KINDS:
                    parts = dashed
            written_kind = None
            if parts and _fold(parts[0]) in KINDS:
                written_kind = _fold(parts[0])
                parts = parts[1:]
            title = " · ".join(parts)
            if written_kind and written_kind != kind:
                warnings.append(f"{file}:{i + 1}: M-{int(m.group(1))} '{written_kind}' yazıyor ama {file} {kind} dosyası; "
                                f"{kind} sayıldı")
            if not title:
                warnings.append(f"{file}:{i + 1}: M-{int(m.group(1))} başlıksız")
            current = {"id": f"M-{int(m.group(1))}", "n": int(m.group(1)), "kind": kind, "title": title, "fields": {},
                       "notes": [], "file": file, "line": i + 1, "end": i + 1}
            entries.append(current)
            continue
        if current is None:
            continue
        if line.strip():
            current["end"] = i + 1
        f = _FIELD.match(line)
        name = _key(f.group(1)) if f else None
        if name:
            value = f.group(2)
            current["fields"][name] = (current["fields"][name] + " " + value).strip() if name in current["fields"] else value
            current["_last"] = name
        elif line.strip():
            last = current.get("_last")
            if last and line[:1] in (" ", "\t") and not re.match(r"^\s*[-*]\s", line):
                current["fields"][last] = (current["fields"][last] + " " + line.strip()).strip()
            else:
                current["notes"].append(line.strip())
    for e in entries:
        e.pop("_last", None)
        _derive(e)
    return entries, warnings


def _derive(e: dict) -> None:
    fields = e["fields"]
    e["scope"] = parse_scope(fields.get(SCOPE, ""))
    source = fields.get(SOURCE, "")
    approved = re.search(r"onay\w*\s+(\d{4}-\d{2}-\d{2})", source)
    created = _DATE.search(source)
    e["created"] = created.group(1) if created else None
    e["approved"] = approved.group(1) if approved else None
    durum = _fold(fields.get(STATUS, "")).strip()
    e["status"] = "active"
    e["replaced_by"] = None
    if durum.startswith("yerini aldi") or durum.startswith("replaced"):
        e["status"] = "replaced"
        m = re.search(r"m\s*-\s*(\d+)", durum)
        e["replaced_by"] = f"M-{int(m.group(1))}" if m else None
    elif durum.startswith("unutuldu") or durum.startswith("forgotten"):
        e["status"] = "forgotten"
    e["marked_stale"] = durum.startswith("soluk")


def _file_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def entries(target: Target, loc: Location | None = None) -> tuple[list[dict], list[str]]:
    """Every entry in the memory folder (the first of a duplicated id wins) and the warnings reading it gave."""
    loc = loc or location(target)
    out, warnings, seen = [], [], {}
    for kind in KINDS:
        name = FILES[kind]
        try:
            text = _file_text(loc.dir / name)
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"{name}: okunamadı: {exc}")
            continue
        if text is None:
            continue
        found, problems = parse(text, kind, name)
        warnings += problems
        for e in found:
            if e["id"] in seen:
                warnings.append(f"{name}:{e['line']}: {e['id']} {seen[e['id']]} içinde de var; ilki kullanılıyor")
                continue
            seen[e["id"]] = f"{name}:{e['line']}"
            out.append(e)
    return out, warnings


def context_text(target: Target, loc: Location | None = None) -> str:
    """context.md without its frontmatter, heading and intro line ("" when there is none)."""
    loc = loc or location(target)
    try:
        text = _file_text(loc.dir / CONTEXT_FILE) or ""
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.split("\n")
    start, _ = _split_frontmatter(lines)
    body = lines[start:]
    if body and body[0].strip() == f"# {TITLES['context']}":
        body = body[1:]
    text = "\n".join(body).strip()
    if text.startswith(INTROS["context"]):
        text = text[len(INTROS["context"]):].strip()
    return text


# --- writing ----------------------------------------------------------------------------------------------------

def entry_block(e: dict) -> str:
    lines = [f"## {e['id']} · {e['kind']} · {_line(e['title'], MAX_TITLE)}"]
    for name, value in e["fields"].items():
        if str(value or "").strip():
            lines.append(f"- {name}: {_line(value)}")
    scope = scope_line(e.get("scope") or {})
    if scope:
        lines.append(f"- {SCOPE}: {scope}")
    if e.get("source"):
        lines.append(f"- {SOURCE}: {_line(e['source'])}")
    return "\n".join(lines) + "\n"


def _append(target: Target, loc: Location, kind: str, block: str) -> None:
    path = loc.dir / FILES[kind]
    text = _file_text(path)
    if text is None:
        text = _frontmatter(target, kind)
    text = _touch(text)
    if not text.endswith("\n"):
        text += "\n"
    _write(path, text + "\n" + block)


def _edit_entry(target: Target, loc: Location, mid: str, change) -> bool:
    """Apply `change(lines, entry)` (it edits the list in place) to the file holding `mid`; False when missing."""
    found, _ = entries(target, loc)
    e = next((x for x in found if x["id"] == mid), None)
    if e is None:
        return False
    path = loc.dir / e["file"]
    lines = (_file_text(path) or "").split("\n")
    change(lines, e)
    _write(path, _touch("\n".join(lines)))
    return True


def _set_status(target: Target, loc: Location, mid: str, value: str) -> bool:
    """The entry's `Durum:` line set to `value`: the line rewritten when there is one, else added at its end."""
    def change(lines, e):
        for i in range(e["line"], e["end"]):
            f = _FIELD.match(lines[i])
            if f and _key(f.group(1)) == STATUS:
                lines[i] = f"- {STATUS}: {value}"
                return
        lines.insert(e["end"], f"- {STATUS}: {value}")
    return _edit_entry(target, loc, mid, change)


def _add_line(target: Target, loc: Location, mid: str, line: str) -> bool:
    def change(lines, e):
        lines.insert(e["end"], line)
    return _edit_entry(target, loc, mid, change)


# --- candidates -------------------------------------------------------------------------------------------------

def _candidates_path(target: Target) -> Path:
    return _state_dir(target) / "candidates.json"


def load_candidates(target: Target) -> dict:
    try:
        data = state.read_json(_candidates_path(target))
    except (OSError, ValueError):
        data = {}
    items = [c for c in (data.get("candidates") or []) if isinstance(c, dict) and CANDIDATE_ID.match(str(c.get("id")))]
    nxt = data.get("next") if isinstance(data.get("next"), int) else 1 + max((int(c["id"][1:]) for c in items), default=0)
    return {"next": nxt, "candidates": items}


def _save_candidates(target: Target, data: dict) -> None:
    _state_dir(target).mkdir(parents=True, exist_ok=True)
    state.write_json(_candidates_path(target), data)


def pending(target: Target) -> list[dict]:
    return [c for c in load_candidates(target)["candidates"] if c.get("status") == "pending"]


def _next_id(found: list[dict], candidates: list[dict]) -> int:
    used = [e["n"] for e in found]
    used += [int(m.group(1)) for c in candidates for m in [ENTRY_ID.match(str(c.get("entry") or ""))] if m]
    return max(used, default=0) + 1


def _main(c: dict) -> str:
    return str((c.get("fields") or {}).get(MAIN.get(c.get("kind"), ""), ""))


def _similar(kind: str, scope: dict, found: list[dict]) -> list[str]:
    return [e["id"] for e in found if e["kind"] == kind and e["status"] == "active"
            and ((scope["paths"] and e["scope"]["paths"] and _paths_overlap(scope["paths"], e["scope"]["paths"]))
                 or _topics_overlap(scope["topics"], e["scope"]["topics"]))]


def clean_proposal(kind: str, title, fields: dict) -> tuple[str, dict]:
    if kind not in MAIN:
        raise MemoryRefused(f"unknown kind {kind!r} (kural, karar, tuzak or yol)")
    title = _line(title, MAX_TITLE)
    out = {name: _line(fields.get(name)) for name in FIELDS[kind] if _line(fields.get(name))}
    if not title:
        raise MemoryRefused("the title is empty")
    missing = [name for name in REQUIRED[kind] if not out.get(name)]
    if missing:
        raise MemoryRefused(f"a {kind} needs {', '.join(missing)}")
    if kind == "yol":
        from . import roadmap  # roadmap reads the memory folder through this module
        roadmap.check_change(out)
    return title, out


def _add(target: Target, data: dict, found: list[dict], proposed: list[dict], origin: str, ticket: str | None,
         secrets: set[str], extra: dict) -> list[dict]:
    """add_candidates' body, under the memory lock: `data` (the candidates) is changed in place."""
    known = {norm(e["title"]) for e in found if e["status"] == "active"}
    known |= {norm(e["fields"].get(MAIN.get(e["kind"], ""), "")) for e in found if e["status"] == "active"}
    known |= {norm(c["title"]) for c in data["candidates"] if c.get("status") in ("pending", "approved")}
    known |= {norm(_main(c)) for c in data["candidates"] if c.get("status") in ("pending", "approved")}
    known.discard("")
    added = []
    for p in proposed:
        try:
            title, fields = clean_proposal(p["kind"], scrub(target, p["title"], secrets),
                                           {k: scrub(target, v, secrets) for k, v in (p.get("fields") or {}).items()})
        except MemoryRefused:
            continue
        main = norm(fields.get(MAIN[p["kind"]], ""))
        if norm(title) in known or (main and main in known):  # a yol removal or move has no main text
            continue
        known |= {norm(title), main} - {""}
        scope = clean_scope(p.get("scope"))
        sources = [{k: scrub(target, v, secrets) if isinstance(v, str) else v for k, v in s.items()}
                   for s in p.get("sources") or [] if isinstance(s, dict)]
        c = {"id": f"c{data['next']}", "kind": p["kind"], "title": title, "fields": fields, "scope": scope,
             "sources": sources, "origin": origin, "ticket": p.get("ticket") or ticket,
             "created_at": p.get("created_at") or state.now(), "status": p.get("status") or "pending",
             "similar": _similar(p["kind"], scope, found), "decided_at": p.get("decided_at"),
             "decided_by": p.get("decided_by"), "entry": None, "edited": False, "replaced": [], **extra}
        data["next"] += 1
        data["candidates"].append(c)
        added.append(c)
    return added


def add_candidates(target: Target, proposed: list[dict], origin: str, ticket: str | None = None, **extra) -> list[dict]:
    """Store proposals ({kind, title, fields, scope, sources[, created_at, status]}) as pending candidates, scrubbed
    of secrets; one whose title or main text an active entry or an open or approved candidate has already is
    skipped. Returns the ones added."""
    secrets = _secrets(target)
    loc = location(target)
    with _lock(target):
        data = load_candidates(target)
        added = _add(target, data, entries(target, loc)[0], proposed, origin, ticket, secrets, extra)
        if added:
            _save_candidates(target, data)
    if any(c["status"] == "pending" for c in added):
        _changed(target, send=True)
    return added


def source_line(target: Target, c: dict) -> str:
    sources = c.get("sources") or []
    tickets = list(dict.fromkeys(str(s.get("ticket") or "") for s in sources if s.get("ticket"))) or ([c["ticket"]] if c.get("ticket") else [])
    refs = list(dict.fromkeys(" ".join(x for x in (s.get("where"), s.get("ref")) if x) for s in sources))
    day = _local_day(c.get("created_at"))
    created = day.isoformat() if day else _today()
    parts = [" ".join([target.name, ", ".join(tickets)]) if tickets else target.name]
    if any(refs):
        parts.append(", ".join(r for r in refs if r))
    parts += [created, f"onay {_today()}"]
    return " · ".join(parts)


def decide(target: Target, candidate_id: str, action: str, text: str | None = None, title: str | None = None,
           replace: bool = False, by: str | None = None) -> dict:
    """approve (with `text`: the main field as edited, `title`: the title as edited; `replace`: the similar entries
    get `Durum: yerini aldı <new id>`) or reject one pending candidate; returns it as saved."""
    if action not in ("approve", "reject"):
        raise MemoryRefused(f"unknown decision {action!r}")
    if not CANDIDATE_ID.match(candidate_id or ""):
        raise LookupError(f"no memory candidate {candidate_id!r}")
    migrate(target)
    loc = location(target)
    with _lock(target):
        data = load_candidates(target)
        c = next((x for x in data["candidates"] if x["id"] == candidate_id), None)
        if c is None:
            raise LookupError(f"no memory candidate {candidate_id}")
        if c.get("status") != "pending":
            raise Decided(f"{candidate_id} is already {c.get('status')}")
        if action == "reject":
            c.update(status="rejected", decided_at=state.now(), decided_by=by or getpass.getuser())
        else:
            fields = dict(c["fields"])
            if text is not None:
                fields[MAIN[c["kind"]]] = text
            new_title, fields = clean_proposal(c["kind"], c["title"] if title is None else title, fields)
            secrets = _secrets(target)
            new_title = scrub(target, new_title, secrets)
            fields = {k: scrub(target, v, secrets) for k, v in fields.items()}
            if c["kind"] == "yol":  # a roadmap scope change: the edit goes into roadmap.md, not an entry
                from . import roadmap
                entry_id, replaced = roadmap.apply_change(target, fields), []
            else:
                found, _ = entries(target, loc)
                n = _next_id(found, data["candidates"])
                entry_id = f"M-{n}"
                entry = {"id": entry_id, "kind": c["kind"], "title": new_title, "fields": fields, "scope": c.get("scope") or {},
                         "source": source_line(target, c)}
                _append(target, loc, c["kind"], entry_block(entry))
                replaced = []
                if replace:
                    for old in c.get("similar") or []:
                        if any(e["id"] == old and e["status"] == "active" for e in found) and \
                                _set_status(target, loc, old, f"{REPLACED} M-{n}"):
                            replaced.append(old)
            c.update(status="approved", entry=entry_id, edited=new_title != c["title"] or fields != c["fields"],
                     replaced=replaced, approved_title=new_title, approved_fields=fields,
                     decided_at=state.now(), decided_by=by or getpass.getuser())
        _save_candidates(target, data)
    _changed(target, send=False)
    return c


def forget(target: Target, mid: str) -> dict:
    """`Durum: unutuldu` on an entry: recall leaves it out; the text stays in the file."""
    mid = (mid or "").strip().upper()
    if not ENTRY_ID.match(mid):
        raise LookupError(f"no memory entry {mid!r}")
    loc = location(target)
    with _lock(target):
        found, _ = entries(target, loc)
        e = next((x for x in found if x["id"] == mid), None)
        if e is None:
            raise LookupError(f"no memory entry {mid}")
        if e["status"] == "forgotten":
            raise Decided(f"{mid} is already forgotten")
        _set_status(target, loc, mid, FORGOTTEN)
    return {"id": mid, "status": "forgotten", "file": str(loc.dir / e["file"])}


def _changed(target: Target, *, send: bool) -> None:
    """The inbox's memory row follows what is pending (best effort)."""
    try:
        from . import attention
        attention.memory_changed(target, send=send)
    except Exception as exc:  # noqa: BLE001 - the inbox must never undo a decision
        print(f"memory: inbox update failed: {exc}", file=sys.stderr)


# --- answers become decision candidates -------------------------------------------------------------------------

AGREE = {"ok", "okay", "yes", "evet", "tamam", "kabul", "agree", "agreed", "önerilen", "recommended", "+1", "👍"}


def _resolve_answer(q: dict, answer: str) -> str:
    """An "ok" takes the recommendation; an option letter or number takes that option."""
    ans = answer.strip()
    simple = ans.lower().strip(" .!")
    if simple in AGREE and q.get("recommendation"):
        return str(q["recommendation"])
    options = [str(o) for o in q.get("options") or []]
    m = re.fullmatch(r"([a-z]|\d)\)?", simple)
    if m and options:
        key = m.group(1)
        for i, o in enumerate(options):
            if key == str(i + 1) or re.match(rf"^{re.escape(key)}[).:]\s", o.strip().lower()) or (key.isalpha() and ord(key) - 97 == i):
                return o
    return ans


def from_round(target: Target, ticket: dict, round_: dict, files: list[str]) -> list[dict]:
    """Every answered question of a closed question round as a decision candidate: the question is the title, the
    answer the `Karar`, scope from the files in play and the question's keywords."""
    from .retro import answers_by_question  # retro imports the engine's helpers; this keeps the import one-way
    questions = [q for q in round_.get("questions") or [] if isinstance(q, dict) and q.get("id") and q.get("text")]
    parts = answers_by_question(str(round_.get("answer") or ""), [str(q["id"]) for q in questions])
    proposed = []
    for q in questions:
        answer = parts.get(str(q["id"]), "").strip()
        if not answer:
            continue
        decision = _resolve_answer(q, answer)
        why = f"{ticket['id']} {round_.get('stage') or ''} aşamasında soruldu (tur {round_.get('n')})".replace("  ", " ")
        rec = str(q.get("recommendation") or "").strip()
        if rec and norm(rec) != norm(decision):
            why += f"; önerilen: {_line(rec, 300)}"
        proposed.append({
            "kind": "karar", "title": _line(q["text"], MAX_TITLE), "fields": {"Karar": decision, "Neden": why},
            "scope": {"paths": folder_globs(files), "topics": keywords(q["text"], 5)},
            "sources": [{"ticket": ticket["id"], "ref": f"Q{round_.get('n')}-{q['id']}", "kind": "answer",
                         "where": round_.get("phase_dir"), "summary": _line(q["text"], 300)}]})
    return add_candidates(target, proposed, "answer", ticket["id"]) if proposed else []


# --- episodes ---------------------------------------------------------------------------------------------------

def _episode_of(found: list[dict], ticket_id: str) -> dict | None:
    return next((e for e in found if e["kind"] == "olay" and str(e["fields"].get("Ticket", "")).split(" ")[0] == ticket_id), None)


def _top_paths(files: list[str]) -> list[str]:
    out = []
    for f in files:
        f = f.strip()
        if f:
            out.append(f.split("/", 1)[0] + "/" if "/" in f else f)
    return list(dict.fromkeys(out))


def episode_done(target: Target, ticket: dict, st: dict, files: list[str], cost: float, review_rounds: int) -> str | None:
    """A ticket reached done: its episode (no approval). A ticket done again gets a line on its entry instead."""
    secrets = _secrets(target)
    loc = location(target)
    top = _top_paths(files)
    pr = st.get("pr") or ""
    summary = f"bitti · maliyet ${cost:.2f} · review turu {review_rounds}"
    with _lock(target):
        found, _ = entries(target, loc)
        old = _episode_of(found, ticket["id"])
        if old is not None:
            _add_line(target, loc, old["id"], scrub(target, f"- Yeniden bitti: {_today()} · {summary}", secrets))
            return old["id"]
        n = _next_id(found, load_candidates(target)["candidates"])
        fields = {"Ticket": f"{ticket['id']} · {ticket.get('type') or ''} · hat {st.get('pipeline') or '?'}",
                  "Sonuç": summary}
        if top:
            fields["Dokunulan"] = ", ".join(f"`{p}`" for p in top[:12])
        if pr:
            fields["PR"] = pr
        entry = {"id": f"M-{n}", "kind": "olay", "title": f"{ticket['id']} · {_line(ticket.get('title'), 120)}",
                 "fields": fields, "source": f"{target.name} {ticket['id']} · done · {_today()}",
                 "scope": {"paths": [p + "**" if p.endswith("/") else p for p in top[:8]],
                           "topics": keywords(ticket.get("title") or "", 6)}}
        block = entry_block(entry)
        _append(target, loc, "olay", scrub(target, block, secrets))
        return entry["id"]


def episode_merged(target: Target, ticket_id: str, url: str | None = None) -> bool:
    """Its PR merged: one `Birleşti:` line on the ticket's episode (once)."""
    loc = location(target)
    with _lock(target):
        found, _ = entries(target, loc)
        e = _episode_of(found, ticket_id)
        if e is None or "Birleşti" in e["fields"]:
            return False
        return _add_line(target, loc, e["id"], scrub(target, f"- Birleşti: {_today()}" + (f" · {url}" if url else "")))


# --- usage and staleness ----------------------------------------------------------------------------------------

def _usage_path(target: Target) -> Path:
    return _state_dir(target) / "usage.json"


def usage(target: Target) -> dict:
    try:
        data = state.read_json(_usage_path(target))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and ENTRY_ID.match(k)} if isinstance(data, dict) else {}


def used_ids(result: dict | None) -> list[str]:
    """The ids a stage says it relied on (`memoryUsed`) and the ones its review findings name (`memory`)."""
    result = result or {}
    ids = list(result.get("memoryUsed") or [])
    ids += [f["memory"] for f in result.get("findings") or [] if isinstance(f, dict) and isinstance(f.get("memory"), str)]
    out = []
    for i in ids:
        m = re.fullmatch(r"\s*[Mm]\s*-\s*(\d{1,7})\s*", str(i))
        if m:
            out.append(f"M-{int(m.group(1))}")
    return list(dict.fromkeys(out))


def record_usage(target: Target, ticket_id: str, ids: list[str]) -> list[str]:
    """Count one use of each id that names an entry; returns the ones counted."""
    if not ids:
        return []
    known = {e["id"] for e in entries(target)[0]}
    counted = [i for i in ids if i in known]
    if not counted:
        return []
    with _lock(target):
        data = usage(target)
        for i in counted:
            u = data.setdefault(i, {"count": 0, "last_used": None, "tickets": []})
            u["count"] = int(u.get("count") or 0) + 1
            u["last_used"] = state.now()
            u["tickets"] = list(dict.fromkeys([*(u.get("tickets") or []), ticket_id]))[-20:]
        _state_dir(target).mkdir(parents=True, exist_ok=True)
        state.write_json(_usage_path(target), data)
    return counted


def _day(stamp) -> date | None:
    return _local_day(stamp)


def stale(e: dict, use: dict | None, today: date | None = None) -> bool:
    """A decision, pitfall or episode unused for STALE_DAYS since its last use, or since it was written."""
    if e["kind"] == "kural" or e["status"] != "active":
        return False
    if e.get("marked_stale"):
        return True
    last = _day((use or {}).get("last_used")) or _day(e.get("approved")) or _day(e.get("created"))
    return last is not None and ((today or date.today()) - last).days >= STALE_DAYS


# --- recall -----------------------------------------------------------------------------------------------------

def _recent(e: dict, use: dict | None, today: date) -> float:
    last = _day((use or {}).get("last_used")) or _day(e.get("approved")) or _day(e.get("created"))
    bonus = 1.0 if last and (today - last).days <= 30 else 0.0
    return bonus + 0.5 * min(int((use or {}).get("count") or 0), 4)


def _relevance(e: dict, files: list[str], words: set[str]) -> float:
    paths, topics = e["scope"]["paths"], e["scope"]["topics"]
    if not paths and not topics:
        return 0.5  # no scope: it holds everywhere, after the ones that match this ticket
    hits = 3 * glob_hits(paths, files)
    hits += sum(1 for t in topics if _topic_in(t, words))
    return float(hits)


def _topic_in(topic: str, words: set[str]) -> bool:
    parts = [_fold(w) for w in keywords(topic)] or [_fold(topic.strip().lower())]
    return bool(parts) and all(p in words for p in parts)


def _render(e: dict) -> str:
    lines = [f"- {e['id']} · {e['kind']} · {e['title']}"]
    lines += [f"  {k}: {v}" for k, v in e["fields"].items() if k not in (SOURCE, STATUS, SCOPE) and v]
    scope = scope_line(e["scope"])
    if scope:
        lines.append(f"  Geçerli: {scope}")
    lines += [f"  {n}" for n in e.get("notes") or []]
    return "\n".join(lines)


def _episode_line(e: dict) -> str:
    f = e["fields"]
    bits = [f"{e['id']} · {e['title']}"] + [x for x in (f.get("Dokunulan"), f.get("Sonuç"), "birleşti" if f.get("Birleşti") else None) if x]
    return "- " + " · ".join(bits)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def recall(target: Target, ticket: dict, stage, files: list[str]) -> dict:
    """What one stage prompt gets: {exists, text (fenced), ids, left_out, warnings}; `stage` is the pipeline stage
    (its `result` tells a planning stage), `files` the files in play (the plan's before implement, the branch's
    changed files after)."""
    migrate(target)
    loc = location(target)
    found, warnings = entries(target, loc)
    uses = usage(target)
    today = date.today()
    budget = target.memory_budget
    words = {_fold(w) for w in keywords(f"{ticket.get('title') or ''}\n{ticket.get('body') or ''}")}
    active = [e for e in found if e["status"] == "active"]
    blocks: list[str] = []
    ids, left, used = [], 0, 0

    def fits(text: str) -> bool:
        nonlocal used
        size = _size(text) + 2
        if used + size > budget:
            return False
        blocks.append(text)
        used += size
        return True

    def section(heading: str, items: list[tuple[dict, str]]) -> int:
        """Entries under a heading, each while it fits (the heading only with its first entry); returns how many
        did not fit."""
        missed, first = 0, True
        for e, text in items:
            if fits(f"{heading}\n{text}" if first else text):
                ids.append(e["id"])
                first = False
            else:
                missed += 1
        return missed

    context = context_text(target, loc)
    if context:
        head = "### Bağlam (context.md)\n"
        cap = budget // MAX_CONTEXT_SHARE
        text = context
        if _size(head + text) > cap:
            text = text.encode("utf-8")[: max(cap - _size(head) - 24, 0)].decode("utf-8", errors="ignore")
            text = (text.rsplit("\n", 1)[0] if "\n" in text else text) + "\n… (kısaltıldı)"
        if not fits(head + text):
            context = ""

    def last_day(e: dict) -> date:
        return _day((uses.get(e["id"]) or {}).get("last_used")) or _day(e.get("approved")) or _day(e.get("created")) or date.min

    rules = sorted((e for e in active if e["kind"] == "kural"),
                   key=lambda e: (-int((uses.get(e["id"]) or {}).get("count") or 0), -last_day(e).toordinal(), -e["n"]))
    left += section("### Kurallar (her zaman geçerli)", [(e, _render(e)) for e in rules])
    ranked = [(_relevance(e, files, words), e) for e in active if e["kind"] in ("karar", "tuzak")]
    ranked = sorted(((rel + _recent(e, uses.get(e["id"]), today), e) for rel, e in ranked if rel > 0),
                    key=lambda x: (-x[0], -x[1]["n"]))
    left += section("### Bu işe uyan kararlar ve tuzaklar", [(e, _render(e)) for _, e in ranked])
    if getattr(stage, "result", None) in ("plan", "verify"):
        episodes = [(_relevance(e, files, words), e) for e in active if e["kind"] == "olay"
                    and (e["scope"]["paths"] or e["scope"]["topics"])
                    and str(e["fields"].get("Ticket", "")).split(" ")[0] != ticket.get("id")]
        episodes = sorted(((rel + _recent(e, uses.get(e["id"]), today), e) for rel, e in episodes if rel > 0),
                          key=lambda x: (-x[0], -x[1]["n"]))
        section("### Benzer eski işler", [(e, _episode_line(e)) for _, e in episodes[:MAX_EPISODES]])
    if not ids and not context:
        return {"exists": False, "text": "", "ids": [], "left_out": left, "warnings": warnings}
    if left:
        blocks.append(f"({left} kayıt yer kalmadığı için gösterilmedi.)")
    return {"exists": True, "text": _fence("\n\n".join(blocks), "markdown"), "ids": ids, "left_out": left,
            "warnings": warnings}


def full_text(target: Target) -> str:
    """The whole active memory (context.md and every active entry with its id), for the retro; "" when empty."""
    migrate(target)
    loc = location(target)
    found, _ = entries(target, loc)
    blocks = []
    context = context_text(target, loc)
    if context:
        blocks.append("### Bağlam (context.md)\n" + context)
    for kind in KINDS:
        items = [e for e in found if e["kind"] == kind and e["status"] == "active"]
        if items:
            blocks.append(f"### {TITLES[kind]}\n" + "\n".join(_episode_line(e) if kind == "olay" else _render(e) for e in items))
    text = "\n\n".join(blocks)
    return text if len(text) <= MAX_FULL else text[:MAX_FULL] + "\n… (kısaltıldı)"


# --- migration --------------------------------------------------------------------------------------------------

_LEARNED = "## Learned rules"
_RULE_LINE = re.compile(r"^\s*[-*]\s+(.*?)\s*(?:\(kaynak:\s*([^)]*)\))?\s*$")


def _split_learned(text: str) -> tuple[str, list[str]]:
    """(the text without its `## Learned rules` section, that section's list items)."""
    lines = text.split("\n")
    at = next((i for i, x in enumerate(lines) if x.strip() == _LEARNED), None)
    if at is None:
        return text, []
    end = next((i for i in range(at + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])), len(lines))
    items = [x for x in lines[at + 1:end] if re.match(r"^\s*[-*]\s+\S", x)]
    return "\n".join(lines[:at] + lines[end:]).strip(), items


def _title_of(text: str) -> str:
    text = _line(text, 400)
    if len(text) <= 90:
        return text
    cut = text[:90].rsplit(" ", 1)[0]
    return cut + "…"


def migrate(target: Target) -> dict | None:
    """Once per target: the old `targets/<name>/context.md` becomes the memory's context.md (without its
    `## Learned rules`), each learned rule a rules.md entry, and `rules/candidates.json`'s open candidates memory
    candidates (decided ones are kept as history). Records `migrated.json`; a second call does nothing. The old
    files are left where they are, unread from then on."""
    marker = _state_dir(target) / "migrated.json"
    if marker.exists():
        return None
    loc = location(target)
    done = {"at": state.now(), "dir": str(loc.dir), "context": False, "rules": [], "candidates": []}
    with _lock(target):
        if marker.exists():
            return None
        old = legacy_context_path(target)
        try:
            old_text = old.read_text(encoding="utf-8")
        except OSError:
            old_text = None
        found, _ = entries(target, loc)
        if old_text is not None:
            rest, items = _split_learned(old_text)
            if rest and not (loc.dir / CONTEXT_FILE).exists():
                _write(loc.dir / CONTEXT_FILE, _frontmatter(target, "context") + "\n" + rest.rstrip() + "\n")
                done["context"] = True
            have = {norm(e["fields"].get("Kural", "")) for e in found if e["kind"] == "kural"} | {norm(e["title"]) for e in found if e["kind"] == "kural"}
            n = _next_id(found, load_candidates(target)["candidates"])
            for item in items:
                m = _RULE_LINE.match(item)
                rule, refs = (m.group(1), m.group(2) or "") if m else (item.strip("-* "), "")
                if not rule or norm(rule) in have:
                    continue
                have.add(norm(rule))
                day = (_DATE.search(refs).group(1) if _DATE.search(refs) else _today())
                where = re.sub(r",?\s*\d{4}-\d{2}-\d{2}\s*$", "", refs).strip(" ,")
                entry = {"id": f"M-{n}", "kind": "kural", "title": _title_of(rule), "fields": {"Kural": rule},
                         "source": " · ".join(x for x in (" ".join(filter(None, (target.name, where))), day, f"onay {day}",
                                                          "context.md'den taşındı") if x)}
                _append(target, loc, "kural", scrub(target, entry_block(entry)))
                done["rules"].append(entry["id"])
                n += 1
        legacy = target.state_dir / "rules" / "candidates.json"
        try:
            old_candidates = state.read_json(legacy).get("candidates") or []
        except (OSError, ValueError, AttributeError):
            old_candidates = []
        proposed = []
        for c in old_candidates:
            if not isinstance(c, dict) or c.get("status") not in ("pending", "rejected") or not str(c.get("rule") or "").strip():
                continue  # an approved one is in context.md's Learned rules (migrated above, unless deleted there)
            proposed.append({"kind": "kural", "title": _title_of(c["rule"]), "fields": {"Kural": c["rule"], "Neden": c.get("why") or ""},
                             "scope": {}, "sources": [s for s in c.get("sources") or [] if isinstance(s, dict)],
                             "created_at": c.get("created_at"), "status": c["status"], "decided_at": c.get("decided_at"),
                             "decided_by": c.get("decided_by"), "ticket": c.get("ticket")})
        data = load_candidates(target)
        added = _add(target, data, entries(target, loc)[0], proposed, "migration", None, _secrets(target), {}) if proposed else []
        if added:
            _save_candidates(target, data)
        done["candidates"] = [c["id"] for c in added]
        _state_dir(target).mkdir(parents=True, exist_ok=True)
        state.write_json(marker, done)
    if any(c["status"] == "pending" for c in added):
        _changed(target, send=False)
    return done


# --- the panel --------------------------------------------------------------------------------------------------

def view(target: Target) -> dict:
    """The Hafıza page's section for one target: where the memory lives, the pending candidates, the entries per kind
    with their use, and what could not be read (roadmap.md's problems too)."""
    from . import roadmap  # roadmap reads the memory folder through this module
    migrate(target)
    loc = location(target)
    found, warnings = entries(target, loc)
    warnings = warnings + roadmap.warnings(target)
    uses = usage(target)
    today = date.today()
    items = load_candidates(target)["candidates"]
    by_id = {e["id"]: e for e in found}
    pending_ = []
    for c in items:
        if c.get("status") == "pending":
            similar = [{"id": i, "title": by_id[i]["title"]} for i in c.get("similar") or [] if i in by_id and by_id[i]["status"] == "active"]
            pending_.append({**c, "similar": similar})
    decided = sorted((c for c in items if c.get("status") != "pending"), key=lambda c: c.get("decided_at") or "", reverse=True)
    out_entries = {k: [] for k in KINDS}
    for e in found:
        use = uses.get(e["id"]) or {}
        out_entries[e["kind"]].append({
            **{k: e[k] for k in ("id", "kind", "title", "fields", "scope", "status", "replaced_by", "file", "line", "created", "approved", "notes")},
            "uses": int(use.get("count") or 0), "last_used": use.get("last_used"), "tickets": use.get("tickets") or [],
            "stale": stale(e, use, today)})
    for k in KINDS:
        out_entries[k].sort(key=lambda e: (e["status"] != "active", -int(e["id"][2:])))
    return {"target": target.name, "dir": str(loc.dir), "source": loc.source, "vault_dir": loc.vault_dir, "hint": loc.hint,
            "context_exists": (loc.dir / CONTEXT_FILE).is_file(), "context_path": str(loc.dir / CONTEXT_FILE),
            "pending": pending_, "decided": decided[:DECIDED_SHOWN], "entries": out_entries, "warnings": warnings,
            "retro": target.retro, "retro_model": target.retro_model, "stale_days": STALE_DAYS}


def ticket_view(target: Target, ticket_id: str, records: list[dict]) -> dict:
    """The drawer's Hafıza block: per stage session, the entries it was given and the ones it said it used."""
    try:
        by_id = {e["id"]: e for e in entries(target)[0]}
    except Exception:  # noqa: BLE001 - the drawer must open whatever the memory folder holds
        by_id = {}
    sessions = []
    for r in records:
        mem = r.get("memory") if isinstance(r.get("memory"), dict) else None
        if not mem:
            continue
        used = [{"id": i, "title": (by_id.get(i) or {}).get("title"), "kind": (by_id.get(i) or {}).get("kind")} for i in mem.get("used") or []]
        sessions.append({"dir": r.get("dir"), "stage": r.get("stage"), "shown": len(mem.get("shown") or []), "used": used})
    return {"sessions": sessions, "used": list(dict.fromkeys(u["id"] for s in sessions for u in s["used"]))}
