"""Personal configuration: the target repositories the pipeline works on, and how to prepare them.

The file lives outside this repository (default ~/.config/ziggy/config.toml) so that
no target name, path or credential is ever tracked here. config.example.toml documents it.
"""
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]  # the checkout (or, installed, site-packages)
PACKAGE = Path(__file__).resolve().parent  # what ships with the package: pipelines, prompts, templates, settings, hooks

_TARGET_KEYS = {"path", "base_branch", "remote", "plan_model", "build_model", "router_model", "retro", "retro_model", "codex_review", "timeout",
                "quiet_after", "stall_after", "answer_after", "max_active", "budget_usd", "ticket_budget_usd", "daily_budget_usd", "prepare", "env", "checks", "github_repo", "gh_user",
                "preserve", "teardown", "ports", "pr_feedback_bots", "pr_replies", "memory_dir", "memory_budget", "mode",
                "roadmap_model", "lead_model", "lead_issue_label", "issue_label", "issue_run", "best_of", "codex_stages", "codex_model", "screens", "check_sandbox"}
# How much a target runs on its own: "personal" lets dependants start themselves; "controlled" (company repositories)
# always stops for plan approval, asks before starting a dependant and never answers on GitHub.
MODES = ("controlled", "personal")
# When a done ticket gets the Codex second review on its own: never, when its plan rated it high risk or large, or always.
CODEX_REVIEW = ("off", "risky", "always")
_CHECK_KEYS = {"name", "run", "timeout"}
# Named additions to the checks' sandbox-exec profile a target opts into (check_sandbox = ["xcode"]); checks.py has the
# rules. xcode: SwiftPM's shared cache and the simulator's test-manager socket, which `xcodebuild test` needs.
CHECK_SANDBOX_PRESETS = ("xcode",)
_CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_DURATION_RE = re.compile(r"^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!s))?\s*(?:(\d+)\s*s)?$", re.I)
_SLACK_KEYS = {"app_token_cmd", "bot_token_cmd", "channel", "user", "target", "forward"}
_PORTS_KEYS = {"start", "size", "count"}
_NOTIFY_KEYS = {"enabled", "events", "min_interval", "urgent", "digest_every", "language"}
_CHAT_KEYS = {"models", "timeout"}
_MEMORY_KEYS = {"vault", "projects_map"}
_LEAD_KEYS = {"enabled", "at"}
_HOST_KEYS = {"enabled", "swap_hold"}
_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PROJECTS_MAP = ".claude/scripts/projects.json"  # [memory] projects_map's default, inside the vault
MEMORY_BUDGET = 4096  # bytes of project memory per stage prompt (memory_budget)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}$")
# The needs-you kinds a notification can be sent for (attention.py derives them).
NOTIFY_EVENTS = ("question", "approval", "failed", "lost", "job-error", "quiet", "publish", "push", "merged-kept", "pr-feedback",
                 "error", "memory", "blocked", "restack", "start", "quota", "lead", "protected", "stall", "report")
# The languages of what Ziggy writes for you outside the panel (notifications, the weekly report); the panel picks its own.
LANGUAGES = ("en", "tr")


def system_language() -> str:
    """"tr" when the locale is Turkish (LC_ALL, else LANG), else "en"."""
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "tr" if locale.lower().startswith("tr") else "en"


# With digests on, these still notify at once (a person is needed now); the rest wait for the digest.
URGENT_EVENTS = ("question", "approval", "failed", "lost", "error", "quota", "protected")
CHAT_MODELS = ("sonnet", "opus", "fable", "haiku")  # sonnet first: most chat turns are lookups, not hard reasoning
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ConfigError(Exception):
    pass


def self_argv() -> list[str]:
    """How Ziggy starts itself again (panel jobs, the launchd service): a checkout's bin/ziggy, else the installed
    package."""
    script = PIPELINE_ROOT / "bin" / "ziggy"
    return [sys.executable, str(script)] if script.is_file() else [sys.executable, "-m", "ziggy"]


def config_path() -> Path:
    if os.environ.get("ZIGGY_CONFIG"):
        return Path(os.environ["ZIGGY_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ziggy" / "config.toml"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ziggy"


@dataclass(frozen=True)
class Target:
    name: str
    path: Path
    base_branch: str = "main"
    remote: str = "origin"
    plan_model: str = "fable"
    build_model: str = "opus"
    router_model: str = "sonnet"  # the one-shot call that proposes a ticket type for a request
    retro: bool = True  # a done ticket with something to learn from gets a read-only retro session (retro.py)
    retro_model: str = "sonnet"  # the retro session's model
    codex_review: str = "off"  # CODEX_REVIEW: a done ticket's automatic Codex review (review.py; needs `codex`)
    roadmap_model: str = "sonnet"  # the roadmap draft session's model (roadmap.py)
    timeout: int = 5400
    quiet_after: int = 900  # seconds without agent events before the panel calls a session quiet
    stall_after: int = 600  # seconds one tool call may run before the inbox says it is taking long (stalls.py)
    # Seconds an open question round may wait before it is answered with its recommendations and the run goes on
    # (pipeline.default_answer_at); None: it waits for you. Never in controlled mode.
    answer_after: int | None = None
    max_active: int = 2  # headless runs at once in this target; more wait in line for a slot
    budget_usd: float | None = None  # per session: claude --max-budget-usd
    ticket_budget_usd: float | None = None  # all of one ticket's sessions (budget.py): over it, the run stops
    daily_budget_usd: float | None = None  # the target's sessions per local day: over it, tickets wait for tomorrow
    issue_label: str | None = None  # open GitHub issues with this label become tickets gh-<n> (issues.py)
    issue_run: bool = False  # such a ticket starts at once
    codex_stages: tuple[str, ...] = ()  # stage ids Codex runs instead of Claude Code (dispatch.run_codex)
    codex_model: str | None = None  # their model (default: Codex's own)
    screens: dict = field(default_factory=dict)  # [targets.<name>.screens]: pages to screenshot after checks (screens.py)
    best_of: str = "off"  # CODEX_REVIEW's values: a second implement attempt, the better one kept (pipeline._best_of_two)
    prepare: tuple[str, ...] = ()
    checks: tuple[dict, ...] = ()  # {name, run, timeout_s}: run by the engine after an implement stage, sandboxed
    check_sandbox: tuple[str, ...] = ()  # CHECK_SANDBOX_PRESETS the checks' sandbox profile adds (opt-in per target)
    env: dict[str, str] = field(default_factory=dict)
    github_repo: str | None = None  # owner/name the draft PRs go to
    gh_user: str | None = None  # gh account whose token opens them
    preserve: tuple[str, ...] = ()  # globs relative to the checkout: gitignored files copied into new worktrees
    teardown: tuple[str, ...] = ()  # shell commands run in the worktree before it is removed
    ports: dict | None = None  # {start, size, count}: each ticket holds one range of `size` ports out of `count`
    pr_feedback_bots: tuple[str, ...] = ()  # bot logins whose PR comments count as feedback (others are skipped)
    pr_replies: bool = False  # answer fixed PR feedback on GitHub ("Fixed in <sha>") after a GitHub round
    memory_dir: Path | None = None  # where the project memory lives, overriding the vault mapping (memory.py)
    memory_budget: int = MEMORY_BUDGET  # bytes of project memory each stage prompt gets
    mode: str = "personal"  # "controlled" or "personal" (MODES)
    lead_model: str = "sonnet"  # the morning lead's session (lead.py)
    lead_issue_label: str | None = None  # open GitHub issues with this label are lead signals too (else only assigned ones)
    warnings: tuple[str, ...] = ()  # load warnings: keys the mode overrode (shown by the CLI and the panel)

    @property
    def state_dir(self) -> Path:
        return state_home() / self.name


def expand(text: str, values: dict[str, str]) -> str:
    """Replace only the known {placeholders}; other braces (shell, JSON) pass through."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def parse_duration(value) -> int:
    """Seconds from an int or a string like "90s", "45m", "1h30m", "2h", "1d" (a bare number is seconds)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r} (use e.g. 45m, 3h, 1h30m)")
    if isinstance(value, (int, float)) and value >= 0:
        return round(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = _DURATION_RE.match(text)
    if not text or not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {value!r} (use e.g. 45m, 3h, 1h30m)")
    d, h, m, s = (int(g or 0) for g in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


def _checks(path, name: str, raw) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: [[targets.{name}.checks]] must be an array of tables")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: [[targets.{name}.checks]] entries must be tables")
        unknown = set(item) - _CHECK_KEYS
        if unknown:
            raise ConfigError(f"{path}: unknown keys in a [[targets.{name}.checks]] entry: {', '.join(sorted(unknown))}")
        check_name, run = item.get("name"), item.get("run")
        if not isinstance(check_name, str) or not _CHECK_NAME_RE.match(check_name):
            raise ConfigError(f"{path}: [[targets.{name}.checks]] name {check_name!r}: letters, digits, space, . _ - (max 40)")
        if check_name in seen:
            raise ConfigError(f"{path}: [[targets.{name}.checks]] duplicate name {check_name!r}")
        if not isinstance(run, str) or not run.strip():
            raise ConfigError(f"{path}: [[targets.{name}.checks]] {check_name!r} has an empty run")
        try:
            timeout_s = parse_duration(item.get("timeout", 600))
        except ValueError as exc:
            raise ConfigError(f"{path}: [[targets.{name}.checks]] {check_name!r}: {exc}") from None
        seen.add(check_name)
        out.append({"name": check_name, "run": run, "timeout_s": timeout_s})
    return tuple(out)


def _commands(path, name: str, key: str, raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ConfigError(f"{path}: [targets.{name}] {key} must be a list of shell commands")
    return tuple(raw)


def _preserve(path, name: str, raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(g, str) for g in raw):
        raise ConfigError(f"{path}: [targets.{name}] preserve must be a list of globs")
    for glob in raw:
        parts = glob.split("/")
        if not glob.strip() or glob.startswith(("/", "~")) or "\\" in glob or ".." in parts or ".git" in parts:
            raise ConfigError(f"{path}: [targets.{name}] preserve entry {glob!r} must be relative to the repository, without '..'")
    return tuple(raw)


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _ports(path, name: str, raw) -> dict | None:
    """{start, size, count}: `count` ranges of `size` ports from `start`, one per ticket."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: [targets.{name}] ports must be a table like {{start = 4100, size = 10}}")
    unknown = set(raw) - _PORTS_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [targets.{name}] ports: {', '.join(sorted(unknown))}")
    ports = {"start": raw.get("start", 4100), "size": raw.get("size", 10), "count": raw.get("count", 100)}
    for key, low, high in (("start", 1024, 65535), ("size", 1, 100), ("count", 1, 1000)):
        if not _int_in(ports[key], low, high):
            raise ConfigError(f"{path}: [targets.{name}] ports.{key} must be a whole number from {low} to {high}")
    if ports["start"] + ports["size"] * ports["count"] > 65536:
        raise ConfigError(f"{path}: [targets.{name}] ports start + size × count goes past port 65535")
    return ports


def port_span(ports: dict) -> tuple[int, int]:
    """First and last port a target's ranges can use."""
    return ports["start"], ports["start"] + ports["size"] * ports["count"] - 1


def _no_port_overlap(path, name: str, ports: dict | None, targets: dict) -> None:
    if ports is None:
        return
    low, high = port_span(ports)
    for other, raw in targets.items():
        if other == name or not isinstance(raw, dict) or raw.get("ports") is None:
            continue
        try:
            theirs = _ports(path, other, raw["ports"])
        except ConfigError:
            continue  # that target reports its own error when it is loaded
        o_low, o_high = port_span(theirs)
        if low <= o_high and o_low <= high:
            raise ConfigError(f"{path}: [targets.{name}] ports {low}-{high} overlap [targets.{other}] ports {o_low}-{o_high}")


@dataclass(frozen=True)
class SlackConfig:
    app_token_cmd: str  # shell command printing the xapp- token (e.g. `op read ...`)
    bot_token_cmd: str  # shell command printing the xoxb- token
    channel: str  # the one channel the listener reads
    user: str  # the one member whose messages it acts on
    target: str | None = None
    forward: bool = False  # the target's questions, approvals and failures of any ticket go to the channel (listener.py)


def _load() -> tuple[Path, dict]:
    path = config_path()
    try:
        return path, tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}; start from config.example.toml") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def load_slack() -> SlackConfig:
    path, data = _load()
    raw = data.get("slack")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: no [slack] table")
    unknown = set(raw) - _SLACK_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [slack]: {', '.join(sorted(unknown))}")
    missing = [k for k in ("app_token_cmd", "bot_token_cmd", "channel", "user") if not raw.get(k)]
    if missing:
        raise ConfigError(f"{path}: [slack] needs {', '.join(missing)}")
    if not isinstance(raw.get("forward", False), bool):
        raise ConfigError(f"{path}: [slack] forward must be true or false")
    return SlackConfig(**raw)


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool = True
    events: tuple[str, ...] = NOTIFY_EVENTS  # needs-you kinds that notify
    min_interval: int = 30  # seconds between two notifications; the ones in between are summed up
    urgent: tuple[str, ...] = URGENT_EVENTS  # kinds that notify at once when digests are on
    digest_every: int = 0  # seconds; above 0, the other kinds are collected into one digest notification this often
    language: str = "en"  # LANGUAGES: notifications and the weekly report (default: the system locale's)


@dataclass(frozen=True)
class ChatConfig:
    models: tuple[str, ...] = CHAT_MODELS  # the first is the default
    timeout: int = 1800  # seconds per chat turn


def _optional_table(key: str) -> tuple[Path, dict]:
    """(config path, the [key] table or {}): no config file at all means defaults too."""
    path = config_path()
    if not path.exists():
        return path, {}
    path, data = _load()
    raw = data.get(key, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: [{key}] must be a table")
    return path, raw


def load_notifications() -> NotificationsConfig:
    path, raw = _optional_table("notifications")
    unknown = set(raw) - _NOTIFY_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [notifications]: {', '.join(sorted(unknown))}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}: [notifications] enabled must be true or false")
    events = raw.get("events", list(NOTIFY_EVENTS))
    if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
        raise ConfigError(f"{path}: [notifications] events must be a list of names")
    wrong = [e for e in events if e not in NOTIFY_EVENTS]
    if wrong:
        renamed = " (`rules` is now `memory`)" if "rules" in wrong else ""
        raise ConfigError(f"{path}: [notifications] unknown events {', '.join(wrong)}{renamed} (known: {', '.join(NOTIFY_EVENTS)})")
    durations = {}
    for key, default in (("min_interval", 30), ("digest_every", 0)):
        try:
            durations[key] = parse_duration(raw.get(key, default))
        except ValueError as exc:
            raise ConfigError(f"{path}: [notifications] {key}: {exc}") from None
    min_interval, digest_every = durations["min_interval"], durations["digest_every"]
    urgent = raw.get("urgent", list(URGENT_EVENTS))
    if not isinstance(urgent, list) or not all(isinstance(e, str) for e in urgent) or set(urgent) - set(NOTIFY_EVENTS):
        raise ConfigError(f"{path}: [notifications] urgent must be a list of event names (known: {', '.join(NOTIFY_EVENTS)})")
    language = raw.get("language") or system_language()
    if language not in LANGUAGES:
        raise ConfigError(f"{path}: [notifications] language must be one of {', '.join(LANGUAGES)}")
    return NotificationsConfig(enabled=enabled, events=tuple(dict.fromkeys(events)), min_interval=min_interval,
                               urgent=tuple(dict.fromkeys(urgent)), digest_every=digest_every, language=language)


def load_chat() -> ChatConfig:
    path, raw = _optional_table("chat")
    unknown = set(raw) - _CHAT_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [chat]: {', '.join(sorted(unknown))}")
    models = raw.get("models", list(CHAT_MODELS))
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and _MODEL_RE.match(m) for m in models):
        raise ConfigError(f"{path}: [chat] models must be a non-empty list of model names (e.g. \"opus\")")
    try:
        timeout = parse_duration(raw.get("timeout", 1800))
    except ValueError as exc:
        raise ConfigError(f"{path}: [chat] timeout: {exc}") from None
    if timeout < 1:
        raise ConfigError(f"{path}: [chat] timeout must be at least one second")
    return ChatConfig(models=tuple(dict.fromkeys(models)), timeout=timeout)


@dataclass(frozen=True)
class SessionsConfig:
    # Oturumlar leaves out sessions whose working directory's last component starts with one of these (helper tools'
    # throwaway headless sessions, e.g. a notes app's nightly summaries); they are only counted.
    hide_cwd_prefixes: tuple[str, ...] = ()


def load_sessions() -> SessionsConfig:
    path, raw = _optional_table("sessions")
    unknown = set(raw) - {"hide_cwd_prefixes"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [sessions]: {', '.join(sorted(unknown))}")
    prefixes = raw.get("hide_cwd_prefixes", [])
    if not isinstance(prefixes, list) or not all(isinstance(p, str) and p.strip() for p in prefixes):
        raise ConfigError(f"{path}: [sessions] hide_cwd_prefixes must be a list of non-empty strings")
    return SessionsConfig(hide_cwd_prefixes=tuple(p.strip() for p in prefixes))


@dataclass(frozen=True)
class MemoryConfig:
    vault: Path | None = None  # an Obsidian vault whose projects map can hold a repository's memory folder
    projects_map: Path | None = None  # that map (default <vault>/.claude/scripts/projects.json)


def load_memory() -> MemoryConfig:
    path, raw = _optional_table("memory")
    unknown = set(raw) - _MEMORY_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [memory]: {', '.join(sorted(unknown))}")
    for key in _MEMORY_KEYS:
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ConfigError(f"{path}: [memory] {key} must be a path")
    if "projects_map" in raw and "vault" not in raw:
        raise ConfigError(f"{path}: [memory] projects_map needs vault")
    if "vault" not in raw:
        return MemoryConfig()
    vault = Path(raw["vault"]).expanduser()
    if not vault.is_absolute():
        raise ConfigError(f"{path}: [memory] vault must be an absolute path")
    mapping = Path(raw["projects_map"]).expanduser() if "projects_map" in raw else vault / PROJECTS_MAP
    return MemoryConfig(vault=vault, projects_map=mapping if mapping.is_absolute() else vault / mapping)


@dataclass(frozen=True)
class LeadConfig:
    enabled: bool = True  # the panel starts each target's lead once a day
    at: str = "08:30"  # local time from which the day's lead runs (the first panel tick after it, also after a sleep)

    @property
    def clock(self) -> tuple[int, int]:
        m = _CLOCK_RE.match(self.at)
        return int(m.group(1)), int(m.group(2))


def load_lead() -> LeadConfig:
    path, raw = _optional_table("lead")
    unknown = set(raw) - _LEAD_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [lead]: {', '.join(sorted(unknown))}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}: [lead] enabled must be true or false")
    at = raw.get("at", "08:30")
    if not isinstance(at, str) or not _CLOCK_RE.match(at.strip()):
        raise ConfigError(f"{path}: [lead] at must be a local time like \"08:30\"")
    return LeadConfig(enabled=enabled, at=at.strip())


@dataclass(frozen=True)
class HostConfig:
    enabled: bool = True  # hold new stages while the machine is short of memory (host.py)
    swap_hold: float = 0.9  # swap used / swap total from which no stage starts


def load_host() -> HostConfig:
    path, raw = _optional_table("host")
    unknown = set(raw) - _HOST_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [host]: {', '.join(sorted(unknown))}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}: [host] enabled must be true or false")
    hold = raw.get("swap_hold", 0.9)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 < hold <= 1:
        raise ConfigError(f"{path}: [host] swap_hold must be a share of the swap between 0 and 1 (e.g. 0.9)")
    return HostConfig(enabled=enabled, swap_hold=float(hold))


def _target_for_cwd(targets: dict) -> str | None:
    """The target whose checkout (or one of its ticket worktrees) contains the current directory."""
    cwd = Path.cwd().resolve()
    best, best_len = None, -1
    for name, raw in targets.items():
        if not isinstance(raw, dict) or "path" not in raw:
            continue
        for root in (Path(raw["path"]).expanduser().resolve(), (state_home() / name / "worktrees").resolve()):
            if cwd.is_relative_to(root) and len(root.parts) > best_len:
                best, best_len = name, len(root.parts)
    return best


_SOURCE_REF = re.compile(r"^(?P<project>[A-Za-z][A-Za-z0-9_]*)-(?P<number>\d+)$")


def source_settings(name: str) -> dict:
    """A work source's mappings from [sources.<name>]: projects (project -> target), states
    (event -> state name) and types (issue type -> ticket type). Empty when not configured; the
    source's own credentials live in its Claude connector, never here."""
    try:
        _, data = _load()
    except ConfigError:
        return {"projects": {}, "states": {}, "types": {}}
    raw = (data.get("sources") or {}).get(name) or {}
    return {key: dict(raw.get(key) or {}) for key in ("projects", "states", "types")}


def parse_source(spec: str) -> tuple[str, str, str]:
    """`youtrack:PROJ-12` -> ("youtrack", "PROJ-12", "PROJ")."""
    name, _, ref = spec.partition(":")
    match = _SOURCE_REF.match(ref.strip())
    if not name.strip() or not match:
        raise ConfigError(f"source must look like <name>:<PROJECT>-<number>, e.g. youtrack:PROJ-12 (got {spec!r})")
    return name.strip().lower(), ref.strip().upper(), match["project"].upper()


def target_names() -> list[str]:
    """Every configured target, in config order."""
    _, data = _load()
    return [name for name, raw in (data.get("targets") or {}).items() if isinstance(raw, dict)]


def _max_active(path, name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{path}: [targets.{name}] max_active must be a whole number of at least 1")
    return value


def _logins(path, name: str, raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ConfigError(f"{path}: [targets.{name}] pr_feedback_bots must be a list of GitHub logins")
    return tuple(x.strip() for x in raw)


def _check_sandbox(path, name: str, raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{path}: [targets.{name}] check_sandbox must be a list of preset names ({', '.join(CHECK_SANDBOX_PRESETS)})")
    unknown = [x for x in raw if x not in CHECK_SANDBOX_PRESETS]
    if unknown:
        raise ConfigError(f"{path}: [targets.{name}] check_sandbox: unknown preset {unknown[0]!r} "
                          f"(known: {', '.join(CHECK_SANDBOX_PRESETS)})")
    return tuple(dict.fromkeys(raw))


def _flag(path, name: str, key: str, value) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: [targets.{name}] {key} must be true or false")
    return value


def _duration(path, name: str, key: str, value) -> int:
    try:
        seconds = parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"{path}: [targets.{name}] {key}: {exc}") from None
    if seconds < 1:
        raise ConfigError(f"{path}: [targets.{name}] {key} must be at least one second")
    return seconds


def _model(path, name: str, key: str, value) -> str:
    if not isinstance(value, str) or not _MODEL_RE.match(value):
        raise ConfigError(f"{path}: [targets.{name}] {key} must be a model name (e.g. \"sonnet\")")
    return value


def _memory_dir(path, name: str, value) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
        raise ConfigError(f"{path}: [targets.{name}] memory_dir must be an absolute path")
    return Path(value).expanduser()


def _memory_budget(path, name: str, value) -> int:
    if not _int_in(value, 256, 65536):
        raise ConfigError(f"{path}: [targets.{name}] memory_budget must be a whole number of bytes from 256 to 65536")
    return value


def _dollars(path, name: str, key: str, value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{path}: [targets.{name}] {key} must be a positive number of dollars")
    return float(value)


def _codex_review(path, name: str, value) -> str:
    return _choice(path, name, "codex_review", value)


_SCREENS_KEYS = {"pages", "url", "start", "wait", "sizes", "browser"}


def _screens(path, name: str, raw) -> dict:
    """[targets.<name>.screens]: `pages` (paths, required), `url` (default http://127.0.0.1:{port}), `start`, `wait`,
    `sizes` ([[w, h], …]) and `browser`; {} when absent."""
    if raw is None:
        return {}
    where = f"{path}: [targets.{name}.screens]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(raw) - _SCREENS_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
    pages = raw.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(p, str) and p.startswith("/") for p in pages):
        raise ConfigError(f"{where} pages must be a list of paths starting with / (e.g. [\"/\", \"/cart\"])")
    sizes = raw.get("sizes", [[1440, 900], [390, 844]])
    if not isinstance(sizes, list) or not all(isinstance(s, list) and len(s) == 2 and all(_int_in(v, 200, 4000) for v in s)
                                              for s in sizes):
        raise ConfigError(f"{where} sizes must be a list of [width, height] pairs (200..4000)")
    for key in ("url", "start", "browser"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ConfigError(f"{where} {key} must be a non-empty string")
    try:
        wait = parse_duration(raw.get("wait", 60))
    except ValueError as exc:
        raise ConfigError(f"{where} wait: {exc}") from None
    return {"pages": list(dict.fromkeys(pages)), "url": raw.get("url", "http://127.0.0.1:{port}"), "start": raw.get("start"),
            "wait": wait, "sizes": [list(s) for s in sizes], "browser": raw.get("browser")}


def _stage_ids(path, name: str, raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and re.match(r"^[a-z][a-z0-9-]{0,39}$", x) for x in raw):
        raise ConfigError(f"{path}: [targets.{name}] codex_stages must be a list of stage ids (e.g. [\"review\"])")
    return tuple(dict.fromkeys(raw))


def _choice(path, name: str, key: str, value) -> str:
    """One of CODEX_REVIEW (\"off\", \"risky\", \"always\")."""
    if value not in CODEX_REVIEW:
        raise ConfigError(f"{path}: [targets.{name}] {key} must be one of {', '.join(map(repr, CODEX_REVIEW))}")
    return value


def _mode(path, name: str, value) -> str:
    if value not in MODES:
        raise ConfigError(f"{path}: [targets.{name}] mode must be \"controlled\" or \"personal\"")
    return value


def load_target(name: str | None = None) -> Target:
    """--target wins; else the target whose repository you are standing in; else default_target;
    else the only target."""
    path, data = _load()
    targets = data.get("targets") or {}
    if name is None:
        name = _target_for_cwd(targets)
    if name is None:
        name = data.get("default_target")
    if name is None and len(targets) == 1:
        name = next(iter(targets))
    if name is None:
        raise ConfigError(f"{path}: run inside a target repository, set default_target or pass --target")
    if name not in targets:
        raise ConfigError(f"{path}: no [targets.{name}] table")
    if not _NAME_RE.match(name):
        raise ConfigError(f"{path}: target name {name!r} must be lowercase letters, digits and dashes")

    raw = targets[name]
    unknown = set(raw) - _TARGET_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in [targets.{name}]: {', '.join(sorted(unknown))}")
    if "path" not in raw:
        raise ConfigError(f"{path}: [targets.{name}] needs path")

    repo = Path(raw["path"]).expanduser()
    if not (repo / ".git").exists():
        raise ConfigError(f"{path}: [targets.{name}] path {repo} is not a git checkout")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ConfigError(f"{path}: [targets.{name}.env] values must be strings")

    ports = _ports(path, name, raw.get("ports"))
    _no_port_overlap(path, name, ports, targets)
    retro = _flag(path, name, "retro", raw.get("retro", True))
    mode = _mode(path, name, raw.get("mode", "personal"))
    pr_replies = _flag(path, name, "pr_replies", raw.get("pr_replies", False))
    warnings = []
    if mode == "controlled" and pr_replies:
        pr_replies = False
        warnings.append(f"[targets.{name}] pr_replies = true is ignored: mode = \"controlled\" never answers on GitHub")
    answer_after = _duration(path, name, "answer_after", raw["answer_after"]) if raw.get("answer_after") is not None else None
    if mode == "controlled" and answer_after is not None:
        answer_after = None
        warnings.append(f"[targets.{name}] answer_after is ignored: mode = \"controlled\" always waits for your answers")
    label = raw.get("lead_issue_label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise ConfigError(f"{path}: [targets.{name}] lead_issue_label must be a GitHub label name")
    issue_label = raw.get("issue_label")
    if issue_label is not None and (not isinstance(issue_label, str) or not issue_label.strip()):
        raise ConfigError(f"{path}: [targets.{name}] issue_label must be a GitHub label name")
    return Target(
        name=name,
        path=repo,
        base_branch=raw.get("base_branch", "main"),
        remote=raw.get("remote", "origin"),
        plan_model=raw.get("plan_model", "fable"),
        build_model=raw.get("build_model", "opus"),
        router_model=raw.get("router_model", "sonnet"),
        retro=retro,
        retro_model=_model(path, name, "retro_model", raw.get("retro_model", "sonnet")),
        codex_review=_codex_review(path, name, raw.get("codex_review", "off")),
        roadmap_model=_model(path, name, "roadmap_model", raw.get("roadmap_model", "sonnet")),
        timeout=int(raw.get("timeout", 5400)),
        quiet_after=int(raw.get("quiet_after", 900)),
        stall_after=_duration(path, name, "stall_after", raw.get("stall_after", 600)),
        answer_after=answer_after,
        max_active=_max_active(path, name, raw.get("max_active", 2)),
        budget_usd=float(raw["budget_usd"]) if raw.get("budget_usd") else None,
        ticket_budget_usd=_dollars(path, name, "ticket_budget_usd", raw.get("ticket_budget_usd")),
        daily_budget_usd=_dollars(path, name, "daily_budget_usd", raw.get("daily_budget_usd")),
        issue_label=issue_label.strip() if issue_label else None,
        issue_run=_flag(path, name, "issue_run", raw.get("issue_run", False)),
        best_of=_choice(path, name, "best_of", raw.get("best_of", "off")),
        codex_stages=_stage_ids(path, name, raw.get("codex_stages", [])),
        screens=_screens(path, name, raw.get("screens")),
        codex_model=_model(path, name, "codex_model", raw["codex_model"]) if raw.get("codex_model") else None,
        prepare=_commands(path, name, "prepare", raw.get("prepare", [])),
        checks=_checks(path, name, raw.get("checks", [])),
        check_sandbox=_check_sandbox(path, name, raw.get("check_sandbox", [])),
        env=dict(env),
        github_repo=raw.get("github_repo"),
        gh_user=raw.get("gh_user"),
        preserve=_preserve(path, name, raw.get("preserve", [])),
        teardown=_commands(path, name, "teardown", raw.get("teardown", [])),
        ports=ports,
        pr_feedback_bots=_logins(path, name, raw.get("pr_feedback_bots", [])),
        pr_replies=pr_replies,
        memory_dir=_memory_dir(path, name, raw.get("memory_dir")),
        memory_budget=_memory_budget(path, name, raw.get("memory_budget", MEMORY_BUDGET)),
        mode=mode,
        lead_model=_model(path, name, "lead_model", raw.get("lead_model", "sonnet")),
        lead_issue_label=label.strip() if label else None,
        warnings=tuple(warnings),
    )
