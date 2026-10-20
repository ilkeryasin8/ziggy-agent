"""Agentic onboarding: a read-only `claude -p` session fills the gaps of the deterministic proposal (onboard.py),
verification (onboard_verify.py) tries the result, and one repair session gets what failed. Also the AGENTS.md draft
PR.

`suggest` is the whole flow, shared by `ziggy target add --agent/--verify` and the panel's background jobs
(`ziggy target suggest <path> --request <id>`). The agent runs only when asked (it costs money): one session
through `dispatch.run` (the stage settings, sandbox and hooks), cwd the main checkout, Read/Grep/Glob only (no edits,
no shell, no subagents, no web, no reading .env/.npmrc and other credential files), the `[onboard] onboard_model`
(default sonnet), at most BUDGET_USD, the Claude login the repository's folder gets. Its answer is the structured
output (`results.json_schema("onboard")`): complete prepare/env/checks lists, notes and an AGENTS.md draft. `merge`
takes it over the proposal; a line holding a value of the checkout's real .env/.npmrc files is dropped. When the
verification after it fails, a second session gets the failure output and returns corrected lists, which are
verified once more; there it stops.

Storage, under <state home>/onboard/<request id>/:
    input.json      the proposal the request started from (the form's lines)
    result.json     the flow's progress and outcome, rewritten as it goes (the panel polls it)
    NN-onboard/     one session: run.json (dispatch's record; stage `onboard`, counted in the stats), prompt.md,
                    claude.jsonl, claude.err, result.json
    verify-*/       one verification: verify.log (its clone is always deleted)
    jobs/           the panel job that ran the request
"""
import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, dispatch, onboard, onboard_verify, pipelines, publish, render, results, state
from .config import ConfigError
from .worktree import GitError, git

PROMPT = "onboard.md"
STAGE = "onboard"
BUDGET_USD = 1.0
TIMEOUT = 900
DEFAULT_MODEL = "sonnet"
TOOLS_DENY = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "WebFetch", "WebSearch",
              # Credentials stay unread: the session's stream (claude.jsonl) would keep what it read.
              "Read(**/.env)", "Read(**/.env.*)", "Read(**/.npmrc)", "Read(**/.yarnrc.yml)", "Read(**/.pypirc)",
              "Read(**/.netrc)", "Read(**/*.pem)", "Read(**/*.key)")
REQUEST_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
AGENTS_BRANCH = "docs/agents-md"
MAX_TREE = 600
MAX_FILE = 8000
MAX_FILES_TOTAL = 60000
MAX_AGENTS_MD = 60000
_KEY_FILES = re.compile(r"^(?:[^/]+/)?(?:package\.json|pyproject\.toml|requirements[^/]*\.txt|setup\.cfg|Makefile|justfile|"
                        r"Dockerfile|docker-compose\.ya?ml|compose\.ya?ml|go\.mod|Cargo\.toml|tsconfig\.json|\.nvmrc|"
                        r"\.python-version|\.yarnrc\.yml|\.env\.example|\.env\.sample|README\.md|CONTRIBUTING\.md)$"
                        r"|^\.github/workflows/[^/]+\.ya?ml$")
_ENV_FILE = re.compile(r"^(?:\.env(?:\..+)?|.+\.env)$")
_EXAMPLE = re.compile(r"(?:example|sample|template|dist|defaults)$", re.I)


class OnboardError(Exception):
    pass


def model() -> str:
    """`[onboard] onboard_model` of the config, else sonnet."""
    try:
        _, data = config._load()
    except ConfigError:
        return DEFAULT_MODEL
    value = (data.get("onboard") or {}).get("onboard_model")
    if value is None:
        return DEFAULT_MODEL
    if not isinstance(value, str) or not config._MODEL_RE.match(value):
        raise ConfigError(f"[onboard] onboard_model must be a model name, not {value!r}")
    return value


def new_request() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{_secrets.token_hex(3)}"


def request_dir(request_id: str) -> Path:
    if not REQUEST_ID.match(request_id or ""):
        raise LookupError(f"no onboarding request {request_id!r}")
    return onboard_verify.root() / request_id


# --- the repository as the session sees it ------------------------------------------------------------------------

def secret_values(repo: Path) -> set[str]:
    """The values of the checkout's real env files (not the examples) and of its .npmrc, at the root and one level
    down. Held only to keep them out of the proposal and the output tails; never written anywhere."""
    found: set[str] = set()
    folders = [repo] + [p for p in sorted(repo.iterdir()) if p.is_dir() and p.name not in onboard._SKIP_DIRS and not p.name.startswith(".")]
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = folder / name
            if not path.is_file() or path.is_symlink():
                continue
            if name == ".npmrc" or (_ENV_FILE.match(name) and not _EXAMPLE.search(name)):
                for line in onboard._read_text(path, 200_000).splitlines():
                    if "=" not in line or line.lstrip().startswith("#"):
                        continue
                    value = line.split("=", 1)[1].strip().strip("'\"")
                    if len(value) >= 8:
                        found.add(value)
    return found


def _fence(text: str, info: str = "") -> str:
    """`text` in a code fence longer than any backtick run inside it, so quoted data cannot close it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def _ignored_present(repo: Path) -> list[str]:
    proc = git("ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", cwd=repo, check=False)
    return sorted(p for p in proc.stdout.split("\0") if p)[:100] if proc.returncode == 0 else []


def _key_files(repo: Path, tracked: list[str]) -> str:
    parts, total = [], 0
    for rel in tracked:
        if not _KEY_FILES.match(rel) or "node_modules/" in rel:
            continue
        text = onboard._read_text(repo / rel, 2_000_000)
        if rel.endswith((".env.example", ".env.sample")):  # keys only
            text = "\n".join(m[1] + "=" for line in text.splitlines() if (m := onboard._ENV_KEY.match(line)))
        if len(text) > MAX_FILE:
            text = text[:MAX_FILE] + "\n… (cut)"
        block = f"`{rel}`:\n\n" + _fence(text)
        if total + len(block) > MAX_FILES_TOTAL:
            parts.append(f"(more key files left out: {rel} …)")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts) or "(none)"


def _lines_for_prompt(proposal: dict) -> dict:
    return {kind: [{k: v for k, v in item.items() if k not in ("verify", "source", "suggested")} for item in proposal.get(kind) or []]
            for kind in ("prepare", "env", "checks")} | {"notes": proposal.get("notes") or []}


def prompt(repo: Path, proposal: dict, failures: list[dict] | None = None) -> str:
    tracked = onboard._tracked(repo)
    rules = next((f for f in ("AGENTS.md", "CLAUDE.md") if (repo / f).is_file()), None)
    ctx = {
        "project": {"name": repo.name, "path": str(repo)},
        "proposal": _fence(json.dumps(_lines_for_prompt(proposal), indent=2, ensure_ascii=False), "json"),
        "failures": _fence(json.dumps(failures, indent=2, ensure_ascii=False), "json") if failures else "",
        "tree": _fence("\n".join(tracked[:MAX_TREE])),
        "tree_cut": len(tracked) > MAX_TREE,
        "ignored": _fence("\n".join(_ignored_present(repo)) or "(none)"),
        "files": _key_files(repo, tracked),
        "has_rules": rules is not None,
        "rules_file": rules or "",
    }
    return render.render(pipelines.prompt_text(PROMPT), ctx, pipelines.partial_text).rstrip() + "\n"


# --- one session --------------------------------------------------------------------------------------------------

def _json_in(message: str | None) -> str | None:
    text = (message or "").strip()
    return text if text.startswith(("{", "```")) else None


def session(repo: Path, request_id: str, proposal: dict, env: dict[str, str], failures: list[dict] | None = None,
            target_name: str | None = None, secrets: set[str] = frozenset()) -> dict:
    """One onboarding session (a repair one with `failures`); returns its record (`result` the normalized answer,
    or None). What it stored (stream, result, run.json) is scrubbed of `secrets` and token-shaped strings; the
    returned record is not, so `merge` can drop such a line."""
    target = onboard_verify.work_target(repo, request_id, env, budget_usd=BUDGET_USD)
    target.state_dir.mkdir(parents=True, exist_ok=True)
    count = max((int(m.group(1)) for p in target.state_dir.iterdir() if (m := state.PHASE_DIR.match(p.name))), default=0)
    session_dir = target.state_dir / f"{count + 1:02d}-{STAGE}"
    session_dir.mkdir()
    text = prompt(repo, proposal, failures)
    (session_dir / "prompt.md").write_text(text, encoding="utf-8")
    result_file = session_dir / "result.json"
    record = {"stage": STAGE, "phase": STAGE, "title": "Onboarding" + (" (repair)" if failures else ""), "ticket": request_id,
              "target": target_name or onboard._name(repo), "path": str(repo), "repair": bool(failures)}
    extra = {"PIPELINE_STAGE": STAGE + ("-repair" if failures else ""), "PIPELINE_TICKET": request_id,
             "PIPELINE_INBOX": str(session_dir), "PIPELINE_RESULT_FILE": str(result_file), "GIT_OPTIONAL_LOCKS": "0"}
    dispatch.run(target, onboard_verify.TICKET, repo, session_dir, session_dir, model(), text, record=record, timeout=TIMEOUT,
                 tools_deny=TOOLS_DENY, json_schema=results.json_schema(STAGE), extra_env=extra)
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
    for name in (dispatch.OUTPUT, "claude.err", "result.json"):
        path = session_dir / name
        if path.is_file():
            path.write_text(onboard_verify.redact(path.read_text(encoding="utf-8", errors="replace"), secrets), encoding="utf-8")
    try:
        stored = json.loads(onboard_verify.redact(json.dumps(record, ensure_ascii=False), secrets))
    except ValueError:  # a secret with JSON syntax in it: keep the record without the answer
        stored = {**record, "result": None, "structured_output": None, "final_message": ""}
    state.write_json(session_dir / "run.json", stored)
    return record


# --- taking the answer over -----------------------------------------------------------------------------------------

_FIELDS = {"prepare": ("run",), "env": ("key", "value"), "checks": ("name", "run")}
_KEY = {"prepare": "run", "env": "key", "checks": "name"}


def merge(proposal: dict, answer: dict, secrets: set[str] = frozenset(), has_rules: bool = False) -> dict:
    """The proposal with the agent's lists taken over: a line equal to one already there keeps its source, a new or
    changed one is `agent` (env values the agent gives are suggestions). An empty list in the answer keeps the
    current one; a line the agent left out is named in the notes. A line holding a value of the checkout's real
    .env/.npmrc files is dropped."""
    out = {"notes": list(proposal.get("notes") or []), "agents_md": "" if has_rules else (answer.get("agents_md") or proposal.get("agents_md") or "")}
    for kind, fields in _FIELDS.items():
        current = [dict(i) for i in proposal.get(kind) or []]
        given = answer.get(kind) or []
        if not given:
            out[kind] = current
            continue
        by_key = {str(i.get(_KEY[kind]) or ""): i for i in current}
        merged, used_names = [], set()
        for item in given:
            if any(value and value in json.dumps(item, ensure_ascii=False) for value in secrets):
                out["notes"].append(f"dropped an agent {kind} line: it held a value from a real .env/.npmrc file")
                continue
            if kind == "checks":
                item = {**item, "name": onboard.check_name(item["name"], used_names)}
            prev = by_key.get(str(item[_KEY[kind]]))
            same = prev is not None and all(prev.get(f) == item.get(f) for f in fields)
            entry = {f: item[f] for f in fields}
            entry["why"] = item.get("why") or (prev or {}).get("why", "")
            entry["source"] = prev.get("source", "detect") if same else "agent"
            if kind == "env":
                entry["suggested"] = prev.get("suggested", False) if same else True
            if kind == "checks" and same and prev.get("timeout"):
                entry["timeout"] = prev["timeout"]
            merged.append(entry)
        kept = {str(i[_KEY[kind]]) for i in merged}
        for key in by_key:
            if key and key not in kept:
                out["notes"].append(f"the agent left out the {kind} line `{key}`")
        out[kind] = merged
    out["notes"] += [n for n in answer.get("notes") or [] if n not in out["notes"]]
    out["check_sandbox"] = list(proposal.get("check_sandbox") or [])
    return {k: out[k] for k in ("prepare", "env", "checks", "notes", "agents_md", "check_sandbox")}


# --- the flow -------------------------------------------------------------------------------------------------------

def _cost(record: dict) -> float:
    return float(record.get("total_cost_usd") or 0)


def suggest(path: str | Path, proposal: dict | None = None, *, agent: bool = False, verify: bool = False,
            request_id: str | None = None, progress=None, target_name: str | None = None) -> dict:
    """The onboarding flow for a repository: the agent (when asked), then verification (after the agent always),
    then at most one repair session and one more verification. `proposal` is where it starts (the form's lines;
    default: what detect proposes). `progress(state)` gets the outcome so far after every step. Returns
    {request, phase: done, proposal, summary, sessions, cost_usd, error}."""
    repo = onboard._repo_path(path)
    request_id = request_id or new_request()
    found = onboard.detect(repo) if proposal is None else None
    proposal = proposal if proposal is not None else found["proposal"]
    has_rules = any((repo / f).is_file() for f in ("AGENTS.md", "CLAUDE.md"))
    name = target_name or (found["spec"]["name"] if found else onboard._name(repo))
    secrets = secret_values(repo)
    out = {"request": request_id, "phase": "start", "message": "", "proposal": proposal, "summary": None, "sessions": [],
           "cost_usd": 0.0, "error": None, "agent": agent}

    def step(phase: str, message: str = "") -> None:
        out.update(phase=phase, message=message)
        if progress:
            progress(out)

    def run_agent(failures=None) -> dict | None:
        env = onboard.spec_from(out["proposal"])["env"]
        step("repair" if failures else "agent", "ajan onarıyor" if failures else "ajan inceliyor")
        record = session(repo, request_id, out["proposal"], env, failures, target_name=name, secrets=secrets)
        out["sessions"].append({"dir": record.get("dir"), "state": record.get("state"), "cost_usd": _cost(record),
                                "repair": bool(failures), "error": record.get("result_error")})
        out["cost_usd"] = round(out["cost_usd"] + _cost(record), 4)
        result = record.get("result")
        if record.get("state") != "ok" or not result or result.get("verdict") != "READY":
            why = record.get("result_error") or (result or {}).get("summary") or record.get("state")
            out["proposal"] = {**out["proposal"], "notes": [*out["proposal"].get("notes", []), f"the agent session ended without an answer: {why}"]}
            return None
        out["proposal"] = merge(out["proposal"], result, secrets, has_rules)
        return result

    def run_verify(phase: str) -> None:
        step(phase, "deneniyor")
        out["proposal"], out["summary"] = onboard_verify.verify(
            repo, out["proposal"], request_id, secrets, progress=lambda message: step(phase, message))

    try:
        answered = run_agent() if agent else None
        if agent or verify:
            run_verify("verify")
            summary = out["summary"]
            if answered is not None and not summary["ok"] and not summary.get("error"):
                failures = summary["failures"][:10]
                if run_agent(failures) is not None:
                    run_verify("verify-again")
    except (OSError, subprocess.SubprocessError, ConfigError, GitError) as exc:
        out["error"] = str(exc)
    step("done")
    return out


# --- the panel's background job -------------------------------------------------------------------------------------

def start_request(path: str, proposal: dict | None, agent: bool, name: str | None = None) -> dict:
    """Write the request's input ({proposal, name}: the form's lines and target name) and start `target suggest` as a
    detached panel job; returns {request, job}."""
    from .dashboard import spawn_job  # the panel's detached jobs; dashboard imports this module
    repo = onboard._repo_path(path)
    proposal = onboard.clean_proposal(proposal) if proposal is not None else None
    request_id = new_request()
    rdir = request_dir(request_id)
    rdir.mkdir(parents=True)
    state.write_json(rdir / "input.json", {"proposal": proposal, "name": name or None})
    state.write_json(rdir / "result.json", {"request": request_id, "phase": "queued", "message": "", "proposal": proposal,
                                            "summary": None, "sessions": [], "cost_usd": 0.0, "error": None, "agent": agent})
    args = ["target", "suggest", str(repo), "--request", request_id] + (["--agent"] if agent else [])
    return {"request": request_id, "job": spawn_job(rdir / "jobs", args)}


def run_request(path: str, request_id: str, agent: bool) -> dict:
    """The job's body: the flow on the request's input, its progress written to result.json."""
    rdir = request_dir(request_id)
    rdir.mkdir(parents=True, exist_ok=True)
    given = state.read_json(rdir / "input.json") if (rdir / "input.json").is_file() else {}
    return suggest(path, given.get("proposal"), agent=agent, verify=True, request_id=request_id, target_name=given.get("name"),
                   progress=lambda out: state.write_json(rdir / "result.json", out))


def request_view(request_id: str) -> dict:
    """result.json of a request, its job's state, and the live lines of the session running now."""
    from .dashboard import _last_lines, read_jobs
    rdir = request_dir(request_id)
    if not rdir.is_dir():
        raise LookupError(f"no onboarding request {request_id}")
    try:
        out = state.read_json(rdir / "result.json")
    except (OSError, ValueError):
        out = {}
    jobs = read_jobs(rdir / "jobs")
    job = jobs[-1] if jobs else None
    live = []
    if out.get("phase") in ("agent", "repair"):
        sessions = sorted(p for p in rdir.iterdir() if state.PHASE_DIR.match(p.name))
        if sessions:
            live = _last_lines(sessions[-1], 3)
    finished = out.get("phase") == "done"
    return {**out, "live_lines": live, "job": {k: job[k] for k in ("state", "exit", "tail")} if job else None,
            "running": bool(job and job["state"] == "running") and not finished,
            "lost": bool(job and job["state"] in ("lost", "error")) and not finished}


# --- AGENTS.md as a draft PR ----------------------------------------------------------------------------------------

def agents_md_pr(path: str | Path, text: str, *, gh_user: str, github_repo: str, base_branch: str,
                 company_confirmed: bool = False) -> str:
    """Commit `text` as AGENTS.md on a new branch `docs/agents-md` made in a temporary worktree (off the remote base;
    the user's checkout is not touched), push it the wrapper's way (ZIGGY_RUN unset, the pre-push hook in
    place) and open a draft PR; returns its URL. A company repository needs `company_confirmed`."""
    repo = onboard._repo_path(path)
    if onboard.company(repo) and not company_confirmed:
        raise OnboardError(f"{repo} is a company repository: opening a PR there needs an explicit confirmation")
    text = (text or "").strip()
    if not text:
        raise OnboardError("AGENTS.md is empty")
    if len(text) > MAX_AGENTS_MD:
        raise OnboardError(f"AGENTS.md is too long (max {MAX_AGENTS_MD} characters)")
    if publish._TRAILER.search(text):
        raise OnboardError("AGENTS.md has a Co-Authored-By line; remove it first")
    if not gh_user or not github_repo or not re.match(r"^[\w.-]+/[\w.-]+$", github_repo):
        raise OnboardError("a gh account and the GitHub repository (owner/name) are needed to open the PR")
    if not re.match(r"^[\w./-]+$", base_branch or "") or ".." in base_branch:
        raise OnboardError(f"bad base branch {base_branch!r}")
    env = publish._clean_env()
    remote_ref = f"refs/remotes/origin/{base_branch}"
    fetch = subprocess.run(["git", "fetch", "--quiet", "origin", f"refs/heads/{base_branch}:{remote_ref}"], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=300)
    if fetch.returncode != 0:
        raise OnboardError(f"fetch of {base_branch} failed: {fetch.stderr.strip()}")
    if git("cat-file", "-e", f"{remote_ref}:AGENTS.md", cwd=repo, check=False).returncode == 0:
        raise OnboardError(f"origin/{base_branch} already has an AGENTS.md")
    taken = subprocess.run(["git", "ls-remote", "--heads", "origin", AGENTS_BRANCH], cwd=repo, env=env, capture_output=True,
                           text=True, timeout=120)
    if taken.stdout.strip():
        raise OnboardError(f"origin already has a {AGENTS_BRANCH} branch; delete it or merge its PR first")
    root = onboard_verify.root()
    root.mkdir(parents=True, exist_ok=True)
    wt = Path(tempfile.mkdtemp(prefix="agents-md-", dir=root))
    shutil.rmtree(wt)  # git worktree add makes it
    try:
        git("worktree", "add", "--quiet", "--detach", str(wt), remote_ref, cwd=repo)
        (wt / "AGENTS.md").write_text(text + "\n", encoding="utf-8")
        git("add", "AGENTS.md", cwd=wt)
        git("commit", "--quiet", "--no-verify", "-m", "docs: add AGENTS.md\n\n"
            "Rules for the AI agents that work on this repository: stack, how to install, test and lint, what not to touch.",
            cwd=wt)
        push = subprocess.run(["git", "-c", f"core.hooksPath={dispatch.GITHOOKS}", "push", "origin", f"HEAD:refs/heads/{AGENTS_BRANCH}"],
                              cwd=wt, env=env, capture_output=True, text=True, timeout=300)
        if push.returncode != 0:
            raise OnboardError(f"push of {AGENTS_BRANCH} failed: {push.stderr.strip()}")
        body = ("Adds `AGENTS.md`: the rules AI agents read before they work on this repository (stack, how to install, "
                "test and lint, what not to touch). Drafted by Ziggy's onboarding from the repository's own files "
                "and reviewed before this PR.\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(body)
        try:
            url = publish.gh_as(gh_user, "pr", "create", "--draft", "--repo", github_repo, "--base", base_branch,
                                "--head", AGENTS_BRANCH, "--title", "docs: add AGENTS.md", "--body-file", fh.name, cwd=repo)
        except publish.PublishError:
            # The branch this call just pushed goes again, so "Draft PR aç" can be retried.
            subprocess.run(["git", "push", "--quiet", "origin", "--delete", AGENTS_BRANCH], cwd=wt, env=env,
                           capture_output=True, timeout=300)
            raise
        finally:
            os.unlink(fh.name)
        return url.splitlines()[-1]
    finally:
        git("worktree", "remove", "--force", str(wt), cwd=repo, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        git("worktree", "prune", cwd=repo, check=False)
