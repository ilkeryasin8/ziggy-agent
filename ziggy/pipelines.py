"""Pipeline definitions: `pipelines/<name>.toml`, an ordered list of stages whose results route to the
next stage, a question round, done or failed. See pipelines/README.md for the file format."""
import re
import tomllib
from dataclasses import dataclass, field, replace

from . import results
from .config import PACKAGE, parse_duration

PIPELINES_DIR = PACKAGE / "pipelines"
PROMPTS_DIR = PACKAGE / "prompts"
TERMINALS = ("done", "failed", "ask_user")
_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_ROUTE = re.compile(r"^[a-z][a-zA-Z0-9_]{0,29}$")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_PIPELINE_KEYS = {"name", "title", "ask_user", "user_review", "intent_gap", "gate", "stages"}
_STAGE_KEYS = {"id", "title", "role", "model", "prompt", "result", "writes", "tools_deny", "timeout", "quiet_after",
               "max_visits", "checks", "next", "routes", "ask_then", "effort", "visits", "incremental", "retry",
               "commit_paths", "commit_message", "regression"}
# What a later visit may change (`[stages.visits.<N>]`); locks, routes and checks stay the stage's.
VISIT_KEYS = ("model", "effort", "timeout", "quiet_after", "tools_deny", "prompt")
_VISIT = re.compile(r"^([2-9]|[1-9][0-9]+)$")
_RETRY_KEYS = {"on_lost", "on_quiet", "on_timeout", "max", "backoff", "max_backoff"}
_ORDER = {"risk": results.RISKS, "size": results.SIZES}


class PipelineConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Retry:
    """Automatic retries in the stage's own session, at most `max` per run (one count for every cause),
    waiting `backoff` seconds doubled each time (capped at `max_backoff`); a lost process is retried at once."""
    on_lost: bool = False
    on_quiet: bool = False
    on_timeout: bool = False
    max: int = 2
    backoff: int = 30
    max_backoff: int = 600


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    prompt: str
    role: str | None = None
    model: str = "build"
    result: str = "generic"
    writes: bool = True
    tools_deny: tuple[str, ...] = ()
    timeout: int | None = None
    quiet_after: int | None = None
    max_visits: int = 1
    checks: bool = False
    next: str | dict | None = None
    routes: dict = field(default_factory=dict)
    ask_then: str | None = None
    effort: str | None = None
    visits: dict = field(default_factory=dict)  # {visit number (2+): {key: value}}, keys from VISIT_KEYS
    incremental: bool = True
    retry: Retry = Retry()
    commit_paths: tuple[str, ...] = ()  # repository paths the engine commits after the stage when they changed
    commit_message: str | None = None
    # After its checks pass, the engine proves the fix: the `regressionTest` the stage reports passes on the branch
    # and fails on the code from before the implement (pipeline._regression_test). Needs checks = true.
    regression: bool = False

    def route_names(self) -> list[str]:
        return [r for r in self.routes if r != results.CHECKS_FAILED]


def effective(stage: Stage, visit: int) -> Stage:
    """The stage as it runs on `visit`: every `visits` override up to it applied in ascending order
    (later visits replace a key, lists included)."""
    changes: dict = {}
    for n in sorted(stage.visits):
        if n <= visit:
            changes.update(stage.visits[n])
    return replace(stage, **changes) if changes else stage


def override_keys(stage: Stage, visit: int) -> list[str]:
    keys: dict = {}
    for n in sorted(stage.visits):
        if n <= visit:
            keys.update(dict.fromkeys(stage.visits[n]))
    return list(keys)


@dataclass(frozen=True)
class Pipeline:
    name: str
    title: str
    stages: tuple[Stage, ...]
    ask_user_then: str | None = None
    max_rounds: int = 5
    user_review_then: str | None = None
    intent_gap_then: str | None = None
    gate_after: tuple[str, ...] = ()
    gate_risk: str | None = None
    gate_size: str | None = None
    warnings: tuple[str, ...] = ()

    def stage(self, stage_id: str) -> Stage:
        for s in self.stages:
            if s.id == stage_id:
                return s
        raise KeyError(stage_id)

    def ids(self) -> list[str]:
        return [s.id for s in self.stages]

    def following(self, stage_id: str) -> str:
        ids = self.ids()
        i = ids.index(stage_id)
        return ids[i + 1] if i + 1 < len(ids) else "done"

    def default_target(self, stage: Stage, verdict: str) -> str:
        return {"READY": self.following(stage.id), "NEEDS_USER": "ask_user", "FAILED": "failed"}[verdict]

    def target(self, stage: Stage, result: dict) -> tuple[str, dict | None, list[str]]:
        """(next target, via {from, route} when a named route chose it, warnings)."""
        verdict, route, warnings = result["verdict"], result.get("route") or "", []
        if route:
            if verdict != "READY":
                warnings.append(f'route "{route}" ignored with verdict {verdict}')
            elif route in stage.routes:
                return stage.routes[route], {"from": stage.id, "route": route}, warnings
            elif stage.route_names():
                warnings.append(f'route "{route}" is not declared (declared: {", ".join(stage.route_names())}); using next')
            else:
                warnings.append(f'route "{route}" ignored: the stage declares no routes')
        elif verdict == "READY" and stage.route_names():
            warnings.append(f"READY without a route (declared: {', '.join(stage.route_names())}); using next")
        nxt = stage.next
        if nxt is None:
            return self.default_target(stage, verdict), None, warnings
        if isinstance(nxt, str):
            return (nxt if verdict == "READY" else self.default_target(stage, verdict)), None, warnings
        return nxt.get(verdict) or self.default_target(stage, verdict), None, warnings

    def can_ask(self, stage: Stage) -> bool:
        if any(t == "ask_user" for t in stage.routes.values()):
            return True
        if stage.next is None or isinstance(stage.next, str):
            return True
        return (stage.next.get("NEEDS_USER") or "ask_user") == "ask_user"

    def answers_continue_at(self, stage_id: str) -> str:
        return self.stage(stage_id).ask_then or self.ask_user_then or stage_id

    def ask_targets(self, stage: Stage) -> list[str]:
        """Where answers to this stage's questions may continue: the default first, then the stages its
        routes lead to (`askThen` picks one)."""
        ids = self.ids()
        found = [self.answers_continue_at(stage.id), *(t for r, t in stage.routes.items() if r != results.CHECKS_FAILED)]
        return [t for t in dict.fromkeys(found) if t in ids]

    def intent_gap_target(self) -> str | None:
        """Where the answers to an intent-gap round continue: `[intent_gap] then`, else the first implement stage
        (None: the pipeline has nowhere to build again, so an intent gap is an ordinary question round)."""
        return self.intent_gap_then or next((s.id for s in self.stages if s.result == "implement"), None)

    def gated(self, stage_id: str, risk: str | None, size: str | None, forced: bool) -> str | None:
        """The reason to stop for approval after this stage, or None."""
        if stage_id not in self.gate_after:
            return None
        if forced:
            return "the ticket asks for a plan checkpoint"
        reasons = []
        if self.gate_risk and risk and _rank("risk", risk) >= _rank("risk", self.gate_risk):
            reasons.append(f"risk {risk}")
        if self.gate_size and size and _rank("size", size) >= _rank("size", self.gate_size):
            reasons.append(f"size {size}")
        return " · ".join(reasons) or None


def _rank(kind: str, value: str) -> int:
    return _ORDER[kind].index(value)


def higher(kind: str, a: str | None, b: str | None) -> str | None:
    """The higher of two ratings: risk and size are never lowered."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _rank(kind, a) >= _rank(kind, b) else b


def _stage(raw, label: str, errors: list) -> Stage | None:
    if not isinstance(raw, dict):
        errors.append(f"{label}: expected a table")
        return None
    unknown = set(raw) - _STAGE_KEYS
    if unknown:
        errors.append(f"{label}: unknown keys {', '.join(sorted(unknown))}")
    sid = raw.get("id")
    if not isinstance(sid, str) or not _ID.match(sid):
        errors.append(f"{label}: id {sid!r}: lowercase letters, digits and dashes, starting with a letter")
        return None
    label = f"stage {sid}"
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not (PROMPTS_DIR / prompt).is_file():
        errors.append(f"{label}: prompt file not found: {prompt}")
    role = raw.get("role")
    if role is not None and not (isinstance(role, str) and (PROMPTS_DIR / "roles" / f"{role}.md").is_file()):
        errors.append(f"{label}: role file not found: prompts/roles/{role}.md")
    result = raw.get("result", "generic")
    if result not in results.NAMES:
        errors.append(f"{label}: unknown result schema {result!r}")
    durations = {}
    for key in ("timeout", "quiet_after"):
        if key in raw:
            try:
                durations[key] = parse_duration(raw[key])
            except ValueError as exc:
                errors.append(f"{label}: {key}: {exc}")
    max_visits = raw.get("max_visits", 1)
    if isinstance(max_visits, bool) or not isinstance(max_visits, int) or max_visits < 1:
        errors.append(f"{label}: max_visits must be a whole number of at least 1")
        max_visits = 1
    nxt = raw.get("next")
    if nxt is not None and not isinstance(nxt, str):
        if not isinstance(nxt, dict) or set(nxt) - set(results.VERDICTS) or not all(isinstance(v, str) for v in nxt.values()):
            errors.append(f"{label}: next must be a stage id or a table of READY / NEEDS_USER / FAILED targets")
            nxt = None
    routes = raw.get("routes") or {}
    if not isinstance(routes, dict) or not all(isinstance(k, str) and _ROUTE.match(k) and isinstance(v, str) for k, v in routes.items()):
        errors.append(f"{label}: routes must map route names (lowercase start, letters/digits/_) to targets")
        routes = {}
    tools = raw.get("tools_deny", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        errors.append(f"{label}: tools_deny must be a list of tool names")
        tools = []
    for key in ("writes", "checks", "incremental", "regression"):
        if key in raw and not isinstance(raw[key], bool):
            errors.append(f"{label}: {key} must be true or false")
    effort = raw.get("effort")
    if effort is not None and effort not in results.EFFORTS:
        errors.append(f"{label}: effort must be one of {', '.join(results.EFFORTS)}")
        effort = None
    commit_paths = raw.get("commit_paths", [])
    if not isinstance(commit_paths, list) or not all(
            isinstance(p, str) and p.strip() and not p.startswith(("/", "-")) and ".." not in p.split("/") and ".git" not in p.split("/")
            for p in commit_paths):
        errors.append(f"{label}: commit_paths must be a list of repository-relative paths")
        commit_paths = []
    commit_message = raw.get("commit_message")
    if commit_message is not None and not (isinstance(commit_message, str) and commit_message.strip()):
        errors.append(f"{label}: commit_message must be a non-empty string")
        commit_message = None
    return Stage(id=sid, title=str(raw.get("title") or sid), prompt=str(prompt), role=role, model=str(raw.get("model", "build")),
                 result=result if result in results.NAMES else "generic", writes=raw.get("writes", True) is True,
                 tools_deny=tuple(tools), timeout=durations.get("timeout"), quiet_after=durations.get("quiet_after"),
                 max_visits=max_visits, checks=raw.get("checks", False) is True, next=nxt, routes=dict(routes),
                 ask_then=raw.get("ask_then"), effort=effort, visits=_visits(raw.get("visits"), label, errors),
                 incremental=raw.get("incremental", True) is not False, retry=_retry(raw.get("retry"), label, errors),
                 commit_paths=tuple(p.strip().rstrip("/") for p in commit_paths), commit_message=commit_message,
                 regression=raw.get("regression", False) is True)


def _visits(raw, label: str, errors: list) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{label}: visits must be a table of visit numbers")
        return {}
    out = {}
    for key, table in raw.items():
        where = f"{label}: visits.{key}"
        if not _VISIT.match(str(key)):
            errors.append(f"{label}: visits: keys are visit numbers from 2 (visit 1 is the stage itself)")
            continue
        if not isinstance(table, dict):
            errors.append(f"{where}: expected a table")
            continue
        unknown = set(table) - set(VISIT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(sorted(unknown))} (allowed: {', '.join(VISIT_KEYS)})")
        change = {}
        for k, v in table.items():
            if k in ("timeout", "quiet_after"):
                try:
                    change[k] = parse_duration(v)
                except ValueError as exc:
                    errors.append(f"{where}: {k}: {exc}")
            elif k == "effort":
                if v not in results.EFFORTS:
                    errors.append(f"{where}: effort must be one of {', '.join(results.EFFORTS)}")
                else:
                    change[k] = v
            elif k == "tools_deny":
                if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
                    errors.append(f"{where}: tools_deny must be a list of tool names")
                else:
                    change[k] = tuple(v)
            elif k == "prompt":
                if not isinstance(v, str) or not (PROMPTS_DIR / v).is_file():
                    errors.append(f"{where}: prompt file not found: {v}")
                else:
                    change[k] = v
            elif k == "model":
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{where}: model must be a model id, \"plan\" or \"build\"")
                else:
                    change[k] = v
        out[int(key)] = change
    return dict(sorted(out.items()))


def _retry(raw, label: str, errors: list) -> Retry:
    if raw is None:
        return Retry()
    if not isinstance(raw, dict):
        errors.append(f"{label}: retry must be a table")
        return Retry()
    unknown = set(raw) - _RETRY_KEYS
    if unknown:
        errors.append(f"{label}: retry: allowed keys {', '.join(sorted(_RETRY_KEYS))}")
    values = {}
    for key in ("on_lost", "on_quiet", "on_timeout"):
        if key in raw:
            if isinstance(raw[key], bool):
                values[key] = raw[key]
            else:
                errors.append(f"{label}: retry.{key} must be true or false")
    if "max" in raw:
        if isinstance(raw["max"], bool) or not isinstance(raw["max"], int) or not 0 <= raw["max"] <= 10:
            errors.append(f"{label}: retry.max must be a whole number from 0 to 10")
        else:
            values["max"] = raw["max"]
    for key in ("backoff", "max_backoff"):
        if key in raw:
            try:
                values[key] = parse_duration(raw[key])
            except ValueError as exc:
                errors.append(f"{label}: retry.{key}: {exc}")
    return Retry(**values)


def parse(name: str, data: dict) -> Pipeline:
    label = f"pipelines/{name}.toml"
    errors: list[str] = []
    warnings: list[str] = []
    unknown = set(data) - _PIPELINE_KEYS
    if unknown:
        errors.append(f"unknown keys {', '.join(sorted(unknown))}")
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise PipelineConfigError(f"{label}: needs at least one [[stages]] table")
    stages = [s for i, raw in enumerate(raw_stages) if (s := _stage(raw, f"stages[{i}]", errors))]
    ids = [s.id for s in stages]
    for sid in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"duplicate stage id {sid!r}")
    valid = set(ids) | set(TERMINALS)

    def known_stage(value, where):
        if value is not None and value not in ids:
            errors.append(f"{where}: unknown stage {value!r}")

    ask_user = data.get("ask_user") or {}
    user_review = data.get("user_review") or {}
    intent_gap = data.get("intent_gap") or {}
    gate = data.get("gate") or {}
    for table, keys, tname in ((ask_user, {"then", "max_rounds"}, "ask_user"), (user_review, {"then"}, "user_review"),
                               (intent_gap, {"then"}, "intent_gap"), (gate, {"after", "risk", "size"}, "gate")):
        if not isinstance(table, dict) or set(table) - keys:
            errors.append(f"[{tname}]: allowed keys {', '.join(sorted(keys))}")
    known_stage(ask_user.get("then"), "ask_user.then")
    known_stage(user_review.get("then"), "user_review.then")
    known_stage(intent_gap.get("then"), "intent_gap.then")
    gate_after = gate.get("after", [])
    gate_after = [gate_after] if isinstance(gate_after, str) else gate_after
    if not isinstance(gate_after, list):
        errors.append("gate.after must be a stage id or a list of them")
        gate_after = []
    for g in gate_after:
        known_stage(g, "gate.after")
    for key, allowed in (("risk", results.RISKS), ("size", results.SIZES)):
        if key in gate and gate[key] not in allowed:
            errors.append(f"gate.{key} must be one of {', '.join(allowed)}")
    max_rounds = ask_user.get("max_rounds", 5)
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
        errors.append("ask_user.max_rounds must be a whole number of at least 1")

    by_id = {s.id: s for s in stages}
    for idx, s in enumerate(stages):
        edges = []
        if s.next is None:
            edges.append(("next", ids[idx + 1] if idx + 1 < len(ids) else "done"))
        elif isinstance(s.next, str):
            edges.append(("next", s.next))
        else:
            edges += [(f"next.{k}", v) for k, v in s.next.items()]
        edges += [(f'route "{k}"', v) for k, v in s.routes.items()]
        for where, t in edges:
            if t not in valid:
                errors.append(f"stage {s.id}: {where}: unknown target {t!r}")
            elif t in by_id and ids.index(t) <= idx and by_id[t].max_visits <= 1:
                warnings.append(f"stage {s.id}: {where} goes back to {t}, whose max_visits is 1, so taking it fails the run")
        known_stage(s.ask_then, f"stage {s.id}: ask_then")
        for n in s.visits:
            if n > s.max_visits + 1:  # +1: the visit an adjudication grants
                warnings.append(f"stage {s.id}: visits.{n} never applies (max_visits {s.max_visits})")
        if s.incremental is False and s.result != "review":
            warnings.append(f"stage {s.id}: incremental applies only to review stages")
        if results.CHECKS_FAILED in s.routes and not s.checks:
            warnings.append(f"stage {s.id}: routes.{results.CHECKS_FAILED} without checks = true")
        if s.regression and not s.checks:
            warnings.append(f"stage {s.id}: regression = true without checks = true (the test runs with the checks)")
    if errors:
        raise PipelineConfigError(f"{label}: " + "; ".join(errors[:8]))
    return Pipeline(name=str(data.get("name") or name), title=str(data.get("title") or name), stages=tuple(stages),
                    ask_user_then=ask_user.get("then"), max_rounds=max_rounds, user_review_then=user_review.get("then"),
                    intent_gap_then=intent_gap.get("then"),
                    gate_after=tuple(gate_after), gate_risk=gate.get("risk"), gate_size=gate.get("size"),
                    warnings=tuple(warnings))


def load(name: str) -> Pipeline:
    if not _NAME.match(name or ""):
        raise PipelineConfigError(f"pipeline name {name!r} is not valid")
    path = PIPELINES_DIR / f"{name}.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineConfigError(f"pipelines/{name}.toml does not exist") from None
    except tomllib.TOMLDecodeError as exc:
        raise PipelineConfigError(f"pipelines/{name}.toml: {exc}") from None
    return parse(name, data)


def names() -> list[str]:
    return sorted(p.stem for p in PIPELINES_DIR.glob("*.toml"))


def prompt_text(rel: str) -> str:
    return (PROMPTS_DIR / rel).read_text(encoding="utf-8")


def partial_text(name: str) -> str:
    """`prompts/<name>.md`; missing partials render as nothing."""
    rel = name if name.endswith(".md") else f"{name}.md"
    path = (PROMPTS_DIR / rel).resolve()
    if not path.is_relative_to(PROMPTS_DIR.resolve()) or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")
