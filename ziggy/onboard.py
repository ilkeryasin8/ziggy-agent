"""Adding a target: look at a repository, propose its [targets.<name>] table, append it to the config.

`detect` only reads: it proposes name, base branch, GitHub repository, gh account, and the proposal
behind prepare, test environment and engine checks (each line with `why`, the file evidence; see
`propose`), and lists what the repository still lacks (rules file, project doc, ignore rules for the
dependency folders prepare creates). It never reads a real .env file, and of a git-ignored .npmrc only
the `@scope:registry=` lines. Trying the proposal is onboard_verify.py, the agent onboard_agent.py.
`add` appends the table to the end of the config file; existing lines are never rewritten, and a
backup is restored when the result does not load. `remove` takes a target's tables out again (after a
backup), leaving its repository and state alone.

Which gh account opens PRs, and which Claude login (CLAUDE_CONFIG_DIR) runs the agent, depend on
where a repository lives; that is personal, so it comes from the config, not from here:

    [onboard]
    default_gh_user = "personal-account"
    [onboard.gh_users]
    "~/src/work" = "work-account"      # longest matching prefix wins, case-insensitive
    [onboard.claude_config_dirs]
    "~/src/work" = "~/.claude-work"    # elsewhere: the default login (~/.claude)
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

from . import config, dispatch
from .config import ConfigError
from .project import PROJECT_DOC
from .worktree import git, ignored_paths

_GITHUB = re.compile(r"github\.com[:/](?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build"}
_ENV_LINE = re.compile(r"^(?P<indent>\s+)(?P<key>[A-Z][A-Z0-9_]*):\s*(?P<value>.+?)\s*$")


def _name(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "target"


def _remote_heads(path: Path) -> tuple[str | None, set[str] | None]:
    """The remote's default branch and its branch names, asked of the remote itself (a local
    origin/HEAD is often missing or stale). (None, None) when it cannot be reached."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "ls-remote", "--symref", "origin", "HEAD", "refs/heads/*"], cwd=path, env=env,
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, None
    if proc.returncode != 0:
        return None, None
    default, heads = None, set()
    for line in proc.stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            default = line[5:].split("\t")[0].removeprefix("refs/heads/")
        elif "\trefs/heads/" in line:
            heads.add(line.split("\trefs/heads/", 1)[1])
    return default, heads


def _base_branch(path: Path, remote_default: str | None = None) -> str:
    if remote_default:
        return remote_default
    head = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=path, check=False).stdout.strip()
    if head.startswith("origin/"):
        return head.removeprefix("origin/")
    for candidate in ("main", "master"):
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{candidate}", cwd=path, check=False).returncode == 0:
            return candidate
    return "main"


def _github_repo(path: Path) -> str | None:
    url = git("remote", "get-url", "origin", cwd=path, check=False).stdout.strip()
    match = _GITHUB.search(url)
    return match["repo"] if match else None


def _by_path(path: Path, table: str, default: str | None = None) -> str | None:
    """The value of the longest `[onboard.<table>]` prefix containing `path` (case-insensitive,
    as macOS paths are), else the default."""
    try:
        _, data = config._load()
    except ConfigError:
        return None
    rules = data.get("onboard") or {}
    best, best_len = rules.get(default) if default else None, -1
    resolved = str(path.resolve()).lower()
    for prefix, value in (rules.get(table) or {}).items():
        root = str(Path(prefix).expanduser().resolve()).lower()
        if (resolved == root or resolved.startswith(root + "/")) and len(root) > best_len:
            best, best_len = value, len(root)
    return best


def _gh_user(path: Path) -> str | None:
    return _by_path(path, "gh_users", "default_gh_user")


def _claude_config_dir(path: Path) -> str | None:
    value = _by_path(path, "claude_config_dirs")
    return str(Path(value).expanduser()) if value else None


def claude_account(config_dir: str | None) -> str | None:
    """`email (org, plan)` of the Claude login a run would use, or None when it cannot be read."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        proc = subprocess.run([dispatch.claude_bin(), "auth", "status"], capture_output=True, text=True, env=env, timeout=20)
        status = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return None
    extra = ", ".join(str(status[k]) for k in ("orgName", "subscriptionType") if status.get(k))
    return f"{status.get('email', '?')}" + (f" ({extra})" if extra else "")


def _find(path: Path, pattern: str, depth: int = 2) -> list[Path]:
    """Files matching `pattern` at most `depth` folders deep, skipping dependency and build
    folders without walking into them (node_modules can hold 100k files)."""
    found = []
    for root, dirs, files in os.walk(path):
        rel_root = Path(root).relative_to(path)
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")) if len(rel_root.parts) < depth else []
        found.extend(rel_root / f for f in sorted(files) if fnmatch.fnmatch(f, pattern))
    return sorted(found, key=lambda p: (len(p.parts), str(p)))


def _ignored(path: Path, rel: str) -> bool:
    return git("check-ignore", "--quiet", "--no-index", rel, cwd=path, check=False).returncode == 0


# A uv project's own lockfile installs it, Python version included. The run then uses that .venv
# without network: no sync, offline, and a cache inside the ignored .venv (the sandbox lets the
# agent write only in the worktree). prepare unsets those three, as [env] reaches it too.
UV_PREPARE = "env -u UV_OFFLINE -u UV_NO_SYNC -u UV_CACHE_DIR uv sync --frozen"
UV_ENV = {"UV_OFFLINE": "1", "UV_NO_SYNC": "1", "UV_CACHE_DIR": "{worktree}/.venv/.uv-cache"}
_VERSION = re.compile(r"(\d+)\.(\d+)")


def python_version(path: Path) -> str:
    """The Python a project asks for: .python-version, else the lower bound of requires-python,
    else 3.12."""
    pinned = path / ".python-version"
    if pinned.is_file():
        match = _VERSION.search(pinned.read_text(encoding="utf-8"))
        if match:
            return f"{match[1]}.{match[2]}"
    try:
        requires = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8")).get("project", {}).get("requires-python", "")
    except (OSError, tomllib.TOMLDecodeError):
        requires = ""
    match = _VERSION.search(requires)
    return f"{match[1]}.{match[2]}" if match else "3.12"


# --- the proposal: prepare, env and checks, each line with the file evidence it comes from ---------------------
#
# proposal = {"prepare": [{run, why, source}], "env": [{key, value, why, suggested, source}],
#             "checks": [{name, run, why, source, timeout?}], "notes": [str], "agents_md": str,
#             "check_sandbox": [preset]}  (config.CHECK_SANDBOX_PRESETS the checks need, e.g. "xcode")
# `source` is "detect" here; the agent (onboard_agent.py) adds "agent" lines and verification (onboard_verify.py)
# a `verify` badge per line. `spec_from` turns the lines into the target table's prepare/env/checks.

_SAFE_REL = re.compile(r"^[A-Za-z0-9_.@+/-]+$")  # a path that may sit unquoted in a shell line
_CHECK_NAME_BAD = re.compile(r"[^A-Za-z0-9 ._-]+")


def _p(run: str, why: str) -> dict:
    return {"run": run, "why": why, "source": "detect"}


def _e(key: str, value: str, why: str, suggested: bool = False) -> dict:
    return {"key": key, "value": value, "why": why, "suggested": suggested, "source": "detect"}


def _read_text(path: Path, limit: int = 400_000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tracked(repo: Path) -> list[str]:
    """Files git tracks (what a fresh clone has); never the ignored ones (.env, .npmrc, node_modules)."""
    proc = git("ls-files", "-z", cwd=repo, check=False)
    return [p for p in proc.stdout.split("\0") if p] if proc.returncode == 0 else []


def _ignored_untracked(repo: Path, rel: str) -> bool:
    """git ignores `rel` and does not track it: a fresh clone (a ticket's worktree) does not have it."""
    return bool(ignored_paths(repo, [rel, rel.rstrip("/") + "/"]))


def check_name(text: str, used: set[str]) -> str:
    """A check name config.py accepts (letters, digits, space . _ -, at most 40), unique among `used`."""
    base = " ".join(_CHECK_NAME_BAD.sub(" ", text).split()).strip(" .-_")[:36] or "check"
    if not base[0].isalnum():
        base = "c " + base
    name, n = base, 2
    while name in used:
        name, n = f"{base} {n}", n + 1
    used.add(name)
    return name


# poetry keeps its venvs in a global cache unless told otherwise; the run needs the worktree's own .venv.
POETRY_PREPARE = "POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-interaction"
_PYTEST_CONFIG = (("pytest.ini", None), ("conftest.py", None), ("pyproject.toml", "[tool.pytest"),
                  ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]"))


def _ruff_configured(path: Path) -> str | None:
    for name in ("ruff.toml", ".ruff.toml"):
        if (path / name).is_file():
            return name
    return "pyproject.toml [tool.ruff]" if "[tool.ruff" in _read_text(path / "pyproject.toml") else None


def _locked(path: Path, lockfile: str, package: str) -> bool:
    return re.search(rf'^name = "{re.escape(package)}"$', _read_text(path / lockfile, 20_000_000), re.M) is not None


def _python(path: Path) -> tuple[list[dict], list[dict], list[dict], list[str], str | None]:
    """(prepare, checks, env, notes, ignore probe) for a Python project. uv projects: `uv sync --frozen`; poetry:
    `poetry install` into an in-project .venv; others: the shared-venv pattern of config.example.toml (one venv per
    target, a real .venv per worktree that sees it through a .pth file), on the Python the project asks for."""
    ruff = _ruff_configured(path)
    notes: list[str] = []
    if (path / "uv.lock").is_file():
        prepare = [_p(UV_PREPARE, "uv.lock: the locked environment, Python included, in .venv")]
        env = [_e(k, v, "uv: runs use the synced .venv offline, cache inside it") for k, v in UV_ENV.items()]
        manager, runner = "uv", "uv run "
    elif (path / "poetry.lock").is_file():
        prepare = [_p(POETRY_PREPARE, "poetry.lock: the locked environment in an in-project .venv")]
        env, manager, runner = [], "poetry", ".venv/bin/python -m "
    else:
        if (path / "requirements.txt").is_file():
            source, why = "-r requirements.txt", "requirements.txt"
        elif (path / "pyproject.toml").is_file():
            source, why = "-e .", "pyproject.toml (no lockfile)"
        else:
            reqs = _find(path, "requirements*.txt")
            if not reqs:
                return [], [], [], [], None
            source, why = f"-r {reqs[0]}", str(reqs[0])
        py = f"python{python_version(path)}"
        extra = " ruff" if ruff else ""
        prepare = [
            _p(f"test -x {{state}}/venv/bin/python || {py} -m venv {{state}}/venv", f"{why}: one shared venv per target ({py})"),
            _p(f"{{state}}/venv/bin/pip install --quiet {source} pytest{extra}", f"{why}: dependencies into the shared venv"),
            _p(f"rm -rf .venv && {py} -m venv .venv && echo {{state}}/venv/lib/{py}/site-packages > .venv/lib/{py}/site-packages/shared-venv.pth",
               "the worktree's own .venv sees the shared one"),
        ]
        env, manager, runner = [], "venv", ".venv/bin/python -m "
    checks = []
    pytest_config = next((name + (f" {marker}" if marker else "") for name, marker in _PYTEST_CONFIG
                          if (path / name).is_file() and (marker is None or marker in _read_text(path / name))), None)
    tests = "tests/ folder" if (path / "tests").is_dir() else ("test_*.py files" if _find(path, "test_*.py", depth=3) else None)
    if tests or pytest_config:
        checks.append({"name": "pytest", "run": f"{runner}pytest -q", "why": f"pytest: {tests or pytest_config}", "source": "detect"})
    if ruff:
        if manager == "venv" or (manager == "uv" and _locked(path, "uv.lock", "ruff")) or (manager == "poetry" and _locked(path, "poetry.lock", "ruff")):
            checks.append({"name": "ruff", "run": f"{runner}ruff check .", "why": f"ruff configured: {ruff}", "source": "detect"})
        else:
            notes.append(f"ruff is configured ({ruff}) but not in the {manager} lockfile: add it as a dev dependency to check it")
    return prepare, checks, env, notes, ".venv/x"


# --- node --------------------------------------------------------------------------------------------------------

_LOCKFILES = (("package-lock.json", "npm"), ("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm"), ("bun.lock", "bun"), ("bun.lockb", "bun"))
_CHECK_SCRIPTS = ("lint", "typecheck", "type-check", "check", "test", "build")  # cheap first: checks stop at a failure
# Binaries whose package has another name (for `node_modules/.bin/<bin>`); otherwise the bin is the package name.
_BIN_PACKAGES = {"tsc": "typescript", "ng": "@angular/cli", "nuxi": "nuxt", "svelte-kit": "@sveltejs/kit", "biome": "@biomejs/biome",
                 "playwright": "@playwright/test", "vue-cli-service": "@vue/cli-service", "react-scripts": "react-scripts"}
_COMPLEX = re.compile(r"&&|\|\||[;|`]|\$\(")
_SERVER = re.compile(r"\b(?:next|nuxt|nuxi|vite|astro|gatsby|remix|react-scripts|ng|vue-cli-service)\s+(?:dev|develop|serve|preview|start)\b"
                     r"|(?<![\w:-])(?:start|nodemon|serve)(?![\w:-])")
_MUTATING = re.compile(r"(?<!\S)(?:--fix|--write|-w|--watch|--watchAll)(?=\s|$|=)")
_TELEMETRY = {"next": ("NEXT_TELEMETRY_DISABLED", "1"), "nuxt": ("NUXT_TELEMETRY_DISABLED", "1"),
              "gatsby": ("GATSBY_TELEMETRY_DISABLED", "1"), "astro": ("ASTRO_TELEMETRY_DISABLED", "1"),
              "storybook": ("STORYBOOK_DISABLE_TELEMETRY", "1"), "onnxruntime-node": ("ORT_DISABLE_TELEMETRY", "1"),
              "onnxruntime-web": ("ORT_DISABLE_TELEMETRY", "1")}
_NPMRC_SCOPE = re.compile(r"^\s*(@[A-Za-z0-9._-]+):registry\s*=")


def _package(path: Path) -> dict:
    try:
        data = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _deps(package: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        section = package.get(key)
        if isinstance(section, dict):
            out.update({str(k): str(v) for k, v in section.items()})
    return out


def _node_folders(path: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """[(folder, package manager, lockfile)]: the root's lockfile; without one, the single folder (at most two levels
    down) that has one. Several such folders are a monorepo: left to the agent or the user."""
    for lock, pm in _LOCKFILES:
        if (path / lock).is_file():
            return [(".", pm, lock)], []
    found: dict[str, tuple[str, str]] = {}
    for lock, pm in _LOCKFILES:
        for rel in _find(path, lock):
            found.setdefault(str(rel.parent), (pm, lock))
    if len(found) == 1:
        folder, (pm, lock) = next(iter(found.items()))
        return [(folder, pm, lock)], []
    if found:
        return [], [f"lockfiles in several folders ({', '.join(sorted(found))}): their prepare and checks are left to the "
                    "agent ('Ajanla doldur') or to you"]
    return [], []


def _install(path: Path, folder: str, pm: str) -> tuple[str, str]:
    """(install command, why) for a folder's package manager, locked and non-interactive."""
    if pm == "npm":
        # The offline install takes the npm cache (fast, no registry token); a package missing
        # from the cache falls back to a normal install, which runs here, outside the sandbox.
        return "(npm ci --offline --no-audit --no-fund || npm ci --no-audit --no-fund)", "package-lock.json: npm ci"
    if pm == "yarn":
        if (path / folder / ".yarnrc.yml").is_file():
            return "yarn install --immutable", "yarn.lock with .yarnrc.yml: yarn berry"
        return "yarn install --frozen-lockfile --non-interactive", "yarn.lock without .yarnrc.yml: yarn v1"
    if pm == "pnpm":
        return "pnpm install --frozen-lockfile", "pnpm-lock.yaml"
    return "bun install --frozen-lockfile", "bun lockfile"


def _yarn_pnp(path: Path, folder: str) -> bool:
    """yarn berry without `nodeLinker: node-modules` installs Plug'n'Play: no node_modules/.bin."""
    rc = path / folder / ".yarnrc.yml"
    return rc.is_file() and not re.search(r"^\s*nodeLinker:\s*['\"]?node-modules", _read_text(rc), re.M)


def _local_inputs(repo: Path, folder: str, deps: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Prepare lines copying what `file:`/`link:` dependencies point at from the main checkout, when git ignores it
    (a fresh clone has no copy); notes for what cannot be copied."""
    lines, notes, seen = [], [], set()
    for name, spec in sorted(deps.items()):
        if not spec.startswith(("file:", "link:")):
            continue
        rel = os.path.normpath(os.path.join(folder, spec.split(":", 1)[1]))
        if rel.startswith("..") or os.path.isabs(rel):
            notes.append(f"{name} comes from {spec}, outside the repository: prepare must provide it")
            continue
        parts = Path(rel).parts
        top = next(("/".join(parts[:i]) for i in range(1, len(parts) + 1) if _ignored_untracked(repo, "/".join(parts[:i]))), None)
        if top is None or top in seen:
            continue  # tracked: every worktree has it
        seen.add(top)
        if not _SAFE_REL.match(top):
            notes.append(f"{name} comes from the git-ignored {top}; copy it in prepare by hand")
            continue
        where = "package.json" if folder == "." else f"{folder}/package.json"
        why = f"{where}: {name} is {spec}; {top} is git-ignored, so it is copied from the main checkout"
        if not (repo / top).exists():
            notes.append(f"{name} needs {top}, which is git-ignored and missing in the main checkout too")
        if (repo / top).is_dir():
            lines.append(_p(f'mkdir -p {top} && cp -R "{{checkout}}/{top}/." {top}/', why))
        else:
            parent = str(Path(top).parent)
            mkdir = "" if parent == "." else f"mkdir -p {parent} && "
            lines.append(_p(f'{mkdir}cp "{{checkout}}/{top}" {top}', why))
    return lines, notes


def _npmrc(repo: Path, folder: str, deps: dict[str, str]) -> tuple[str | None, list[str], list[str]]:
    """(.npmrc path to copy in for the install, the registry scopes it serves that dependencies use, notes). Only a
    git-ignored .npmrc counts (a tracked one is in every clone). Only its `@scope:registry=` lines are looked at; no
    other line (an auth token) is kept or shown."""
    rel = ".npmrc" if folder == "." else f"{folder}/.npmrc"
    if not (repo / rel).is_file() or not _ignored_untracked(repo, rel):
        return None, [], []
    scopes = set()
    try:
        with (repo / rel).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _NPMRC_SCOPE.match(line)
                if m:
                    scopes.add(m[1])
    except OSError:
        return None, [], []
    used = sorted(s for s in scopes if any(d.startswith(s + "/") for d in deps))
    if not used:
        return None, [], [f"{rel} is git-ignored and not copied: no dependency uses a registry scope it sets"]
    return rel, used, []


def _rewrite(command: str, deps: dict[str, str], bin_prefix: str = "node_modules/.bin/",
             script: bool = True) -> tuple[str | None, list[str], str | None]:
    """(a check command for a package script or a CI line, what was changed, why it was dropped); (None, [], None)
    when the command needs nothing. A check must leave the tree as it was and end by itself: `--fix`/`--write` go
    (prettier gets `--check`), `--watch`/`-w` go, vitest gets `run`; a dev server is dropped. A simple script runs
    its local binary (`node_modules/.bin/<bin>`, `yarn <bin>` under Plug'n'Play), so the check goes through no
    package manager; a CI line only does so for a declared dependency."""
    text = command.strip()
    if _SERVER.search(text):
        return None, [], "starts a server or a watcher"
    if _COMPLEX.search(text):
        if _MUTATING.search(text):
            return None, [], "changes files or watches, in a compound command: write the check by hand"
        return text, [], None
    m = re.match(r"^((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)(\S+)(.*)$", text, re.S)
    if not m:
        return None, [], "empty"
    prefix, binary, rest = m[1], m[2], m[3]
    changed = []
    if Path(binary).name == "prettier":
        rest, n = re.subn(r"(?<!\S)(?:--write|-w)(?=\s|$)", "--check", rest)
        if n:
            changed.append("--check instead of --write")
    for flag, label in ((r"--fix", "without --fix"), (r"--write", "without --write"), (r"--watch(?:All)?(?:=\S*)?|-w", "without watch mode")):
        rest, n = re.subn(rf"\s+(?:{flag})(?=\s|$)", "", rest)
        if n:
            changed.append(label)
    if Path(binary).name == "vitest" and not re.match(r"\s+(?:run|bench|related|typecheck)\b", rest):
        rest = re.sub(r"^\s+(?:watch|dev)\b", "", rest)
        rest = " run" + rest
        changed.append("vitest run (no watch)")
    local = binary in deps or _BIN_PACKAGES.get(binary) in deps
    if "/" not in binary and binary not in ("node", "npx") and (local or (script and changed)):
        binary = bin_prefix + binary
    elif not changed:
        return None, [], None
    return f"{prefix}{binary}{rest}", changed, None


def _pm_run(pm: str, script: str) -> str:
    if pm == "npm":
        return "npm test" if script == "test" else f"npm run {script}"
    return f"{pm} run {script}"


def _node(repo: Path) -> tuple[list[dict], list[dict], list[dict], list[str], list[str], list[dict]]:
    """(prepare, checks, env, notes, ignore probes, script map) for the Node project at the root or in its one app
    folder. The script map ({folder, pm, scripts, proposed {script: check}}) lets the CI parse join a workflow's
    `npm run lint` with the check already proposed."""
    folders, notes = _node_folders(repo)
    prepare, checks, env, probes, scripts_seen = [], [], [], [], []
    for folder, pm, lock in folders:
        package = _package(repo / folder)
        deps = _deps(package)
        cd = "" if folder == "." else f"cd {folder} && "
        inputs, input_notes = _local_inputs(repo, folder, deps)
        prepare += inputs
        notes += input_notes
        install, why = _install(repo, folder, pm)
        npmrc, scopes, npmrc_notes = _npmrc(repo, folder, deps)
        notes += npmrc_notes
        if npmrc:
            # The registry token is only in the worktree while the install runs.
            name = Path(npmrc).name
            install = f'{cd}cp "{{checkout}}/{npmrc}" {name} && {install}; s=$?; rm -f {name}; exit $s'
            why += f"; {npmrc} is git-ignored and serves {', '.join(scopes)}: copied in for the install only, then deleted"
        else:
            install = cd + install
        prepare.append(_p(install, f"{folder}/{lock}: {why}" if folder != "." else why))
        pnp = pm == "yarn" and _yarn_pnp(repo, folder)
        if not pnp:
            probes.append(str(Path(folder) / "node_modules" / "x"))
        where = "package.json" if folder == "." else f"{folder}/package.json"
        for dep, (key, value) in _TELEMETRY.items():
            if dep in deps and not any(e["key"] == key for e in env):
                env.append(_e(key, value, f"{where} depends on {dep}: no telemetry from runs and checks"))
        scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        proposed = {}
        used = set()
        for name in _CHECK_SCRIPTS:
            script = scripts.get(name)
            if not isinstance(script, str) or not script.strip() or (name == "test" and "no test specified" in script):
                continue
            run, changed, dropped = _rewrite(script, deps, "yarn " if pnp else "node_modules/.bin/")
            if dropped:
                notes.append(f"{where} script `{name}` is not a check: {dropped} (`{script}`)")
                continue
            check = {"name": check_name(name if folder == "." else f"{folder} {name}", used),
                     "run": cd + (run or _pm_run(pm, name)),
                     "why": f"{where} script `{name}`: `{script}`" + (f" ({', '.join(changed)}: a check must not change files or wait)" if changed else ""),
                     "source": "detect"}
            checks.append(check)
            proposed[name] = check
        scripts_seen.append({"folder": folder, "pm": pm, "scripts": scripts, "proposed": proposed, "deps": deps})
    return prepare, checks, env, notes, probes, scripts_seen


# --- CI workflows ------------------------------------------------------------------------------------------------

_CI_CHECK = re.compile(r"\b(?:test|tests|pytest|lint|eslint|ruff|flake8|mypy|pyright|tsc|typecheck|type-check|vitest|jest|clippy|vet)\b")
_CI_SKIP = re.compile(r"\$\{\{|\b(?:install|setup|checkout|upload|download|deploy|publish|login|docker|echo|cache|curl|wget|apt|apt-get|brew|sudo|pip|codecov)\b|^npm ci\b")
_PM_SCRIPT = re.compile(r"^(?:npm(?:\s+run)?|yarn(?:\s+run)?|pnpm(?:\s+run)?|bun\s+run)\s+([\w:.-]+)\s*$")
_BLOCK = ("|", ">", "|-", ">-", "|+", ">+")


def ci_runs(path: Path) -> list[dict]:
    """Every `run:` command of the GitHub workflows, one per line ({file, line, run, wd}), with the step's or the
    file's `working-directory`. A line parse, not a YAML parser: steps are `- ` items, block scalars are followed."""
    out = []
    for workflow in sorted((path / ".github" / "workflows").glob("*.y*ml")):
        rel = str(workflow.relative_to(path))
        lines = _read_text(workflow).splitlines()
        step, default_wd, i = None, None, 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            i += 1
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            dash = re.match(r"^(\s*)-\s+(.*)$", line)
            if dash and (step is None or indent <= step["indent"]):
                step = {"indent": indent, "wd": None, "runs": []}
                body, key_indent = dash[2], indent + 2
            else:
                if step is not None and indent <= step["indent"]:
                    step = None
                body, key_indent = stripped, indent
            key = re.match(r"^(run|working-directory):\s*(.*?)\s*$", body)
            if not key:
                continue
            value = key[2]
            if key[1] == "working-directory":
                wd = value.strip("'\"")
                if step is None:
                    default_wd = wd
                else:
                    step["wd"] = wd
                    for r in out:
                        if r.get("_step") is step:
                            r["wd"] = wd
                continue
            if step is None:
                continue
            if value in _BLOCK:
                while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > key_indent):
                    text = lines[i].strip()
                    i += 1
                    if text and not text.startswith("#"):
                        out.append({"file": rel, "line": i, "run": text, "wd": step["wd"] or default_wd, "_step": step})
            elif value:
                text = value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value
                out.append({"file": rel, "line": i, "run": text, "wd": step["wd"] or default_wd, "_step": step})
    return [{k: v for k, v in r.items() if k != "_step"} for r in out]


def _ci_checks(repo: Path, checks: list[dict], scripts: list[dict], notes: list[str]) -> None:
    """Test and lint commands the workflows run, as checks: one already proposed gets the workflow as evidence; a new
    one is added (rewritten like a script, in its working directory)."""
    used = {c["name"] for c in checks}
    seen = {c["run"] for c in checks}
    for r in ci_runs(repo):
        text = r["run"].strip()
        if text.startswith("cd ") or _CI_SKIP.search(text) or not _CI_CHECK.search(text):
            continue
        where = f"{r['file']}:{r['line']}"
        folder = (r["wd"] or ".").strip("/") or "."
        script = _PM_SCRIPT.match(text)
        match = next((s for s in scripts if s["folder"] == folder), None)
        body = None
        if script and match:
            name = script[1]
            if name in match["proposed"]:
                c = match["proposed"][name]
                if "CI runs it" not in c["why"]:
                    c["why"] += f"; CI runs it too ({where})"
                continue
            if name in _CHECK_SCRIPTS:
                continue  # looked at and left out above (the note says why)
            body = match["scripts"].get(name) if isinstance(match["scripts"].get(name), str) else None
        existing = next((c for c in checks if c["name"] in ("pytest", "ruff") and c["name"] in text), None)
        if existing is None and text.split()[:2] in (["go", "test"], ["cargo", "test"]):
            existing = next((c for c in checks if c["run"].startswith(" ".join(text.split()[:2]))), None)
        if existing:
            if "CI runs it" not in existing["why"]:
                existing["why"] += f"; CI runs it too ({where})"
            continue
        deps = match["deps"] if match else {}
        if body is not None:  # a package script only the workflow names (`npm run test:unit`)
            run, changed, dropped = _rewrite(body, deps)
            run = run or _pm_run(match["pm"], script[1])
        else:
            run, changed, dropped = _rewrite(text, deps, script=False)
        if dropped:
            notes.append(f"CI command at {where} is not a check: {dropped} (`{body or text}`)")
            continue
        cd = "" if folder == "." else f"cd {folder} && "
        command = cd + (run or text)
        if command in seen:
            continue
        seen.add(command)
        label = script[1] if body is not None else " ".join(w for w in text.split()[:3] if not w.startswith("-"))
        checks.append({"name": check_name("ci " + label, used), "run": command, "source": "detect",
                       "why": f"CI: {where} runs `{text}`" + (f" in {folder}" if folder != "." else "")
                              + (f"; the script is `{body}`" if body is not None else "") + (f" ({', '.join(changed)})" if changed else "")})


def _ci_env(path: Path) -> list[dict]:
    """Plain KEY: value pairs under `env:` blocks of the GitHub workflows (no ${{ }} values)."""
    env: dict[str, dict] = {}
    for workflow in sorted((path / ".github" / "workflows").glob("*.y*ml")):
        block_indent = None
        for line in _read_text(workflow).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if stripped == "env:":
                block_indent = indent
                continue
            if block_indent is not None and indent <= block_indent:
                block_indent = None
            match = _ENV_LINE.match(line) if block_indent is not None else None
            if match and "${{" not in match["value"] and match["key"] not in env:
                env[match["key"]] = _e(match["key"], match["value"].strip("'\""), f"CI env in {workflow.relative_to(path)}")
    return list(env.values())


# --- env the code reads ------------------------------------------------------------------------------------------

_SOURCE_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".py"}
_ENV_READS = (
    re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
    re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"import\.meta\.env\.([A-Z_][A-Z0-9_]*)"),
    re.compile(r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"os\.environ\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"os\.getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
)
_SETTINGS_ALIAS = re.compile(r"\b(?:validation_alias|alias|env)\s*=\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_ENV_SKIP = {"NODE_ENV", "CI", "HOME", "PATH", "PWD", "USER", "SHELL", "TMPDIR", "TERM", "LANG", "TZ", "HOSTNAME", "NEXT_RUNTIME",
             "MODE", "DEV", "PROD", "SSR", "BASE_URL"}
_ENV_SKIP_PREFIX = ("PIPELINE_", "npm_", "BMAD_", "CLAUDE_", "GITHUB_", "RUNNER_", "VITEST", "JEST_")
_EXAMPLE_ENV = re.compile(r"^(?:.*/)?(?:\.env\.(?:example|sample|template|dist|defaults)|\.?env\.example|example\.env)$")
_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
MAX_SOURCE_FILES = 4000


def dummy_value(key: str) -> str:
    """A non-secret stand-in for a variable: a local URL nothing listens on, a local database, else 'test'."""
    k = key.upper()
    if "MONGO" in k:
        return "mongodb://127.0.0.1:27017/test"
    if any(x in k for x in ("POSTGRES", "PG_", "DATABASE_URL", "DB_URL")):
        return "postgresql://test:test@127.0.0.1:5432/test"
    if "MYSQL" in k:
        return "mysql://test:test@127.0.0.1:3306/test"
    if "REDIS" in k:
        return "redis://127.0.0.1:6379/0"
    if "AMQP" in k or "RABBIT" in k:
        return "amqp://127.0.0.1:5672"
    if k.startswith("WS_") or "_WS_" in k or "WEBSOCKET" in k or "SOCKET_URL" in k:
        return "ws://127.0.0.1:9"
    if any(x in k for x in ("URL", "URI", "ENDPOINT", "ORIGIN")):
        return "http://127.0.0.1:9"
    if k.endswith("HOST"):
        return "127.0.0.1"
    if k.endswith("PORT"):
        return "9"
    return "test"


def _code_env(repo: Path, tracked: list[str]) -> list[dict]:
    """Variables the tracked code reads and the keys of tracked example env files, each with a dummy value and the
    first place it was seen. Real .env files are never opened."""
    seen: dict[str, str] = {}
    sources = [p for p in tracked if Path(p).suffix in _SOURCE_EXT][:MAX_SOURCE_FILES]
    for rel in sources:
        text = _read_text(repo / rel)
        if not text:
            continue
        settings = rel.endswith(".py") and "BaseSettings" in text
        for n, line in enumerate(text.splitlines(), 1):
            if "env" not in line.lower() and not settings:
                continue
            found = [m for p in _ENV_READS for m in p.findall(line)]
            if settings:
                found += _SETTINGS_ALIAS.findall(line)
            for key in found:
                if key not in seen:
                    seen[key] = f"{rel}:{n} reads it"
    for rel in tracked:
        if _EXAMPLE_ENV.match(rel):
            for line in _read_text(repo / rel).splitlines():
                m = _ENV_KEY.match(line)
                if m:
                    seen[m[1]] = (seen[m[1]] + f"; listed in {rel}") if m[1] in seen else f"listed in {rel}"
    out = []
    for key, why in seen.items():
        if key in _ENV_SKIP or key.startswith(_ENV_SKIP_PREFIX):
            continue
        out.append(_e(key, dummy_value(key), why + " (dummy value, edit if a check needs a real one)", suggested=True))
    return sorted(out, key=lambda e: e["key"])


def _python_switches(repo: Path) -> list[dict]:
    text = " ".join(_read_text(repo / f, 20_000_000) for f in ("requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock"))
    if re.search(r"(?<![\w-])onnxruntime(?![\w-]*[a-z])", text, re.I):
        return [_e("ORT_DISABLE_TELEMETRY", "1", "the Python dependencies include onnxruntime: no telemetry from runs and checks")]
    return []


# Xcode projects and Swift packages: their checks need the `xcode` check_sandbox preset (checks.py). A nested sandbox
# cannot start inside the checks' sandbox, so each one the toolchain would start is switched off: SwiftPM's
# (--disable-sandbox, and the two -IDEPackageSupport flags for xcodebuild) and the compiler's macro plugin server's
# (-disable-sandbox in OTHER_SWIFT_FLAGS; without it the first @Model, @Query or @Observable fails the build with
# `sandbox_apply: Operation not permitted`). The checks' own sandbox still holds the plugin. Build output stays in the
# worktree's .build/, which must be git-ignored.
XCODE_PRESET = "xcode"
XCODEBUILD_TIMEOUT = "30m"
XCODEBUILD_FLAGS = ("-derivedDataPath .build/DerivedData -IDEPackageSupportDisableManifestSandbox=YES "
                    "-IDEPackageSupportDisablePluginExecutionSandbox=YES 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' "
                    "-quiet")
XCODE_PROBE_TIMEOUT = 60  # seconds for `xcodebuild -list` / `xcrun simctl list`
_IPHONE_NUMBER = re.compile(r"^iPhone (\d+)")
_RUNTIME_VERSION = re.compile(r"iOS-(\d+)-(\d+)")


def _quoted(rel: str) -> str:
    return rel if _SAFE_REL.match(rel) else "'" + rel.replace("'", "'\\''") + "'"


def _swift_packages(tracked: list[str]) -> list[str]:
    """Folders (relative, "." for the root, at most two deep) whose tracked Package.swift has tracked files under Tests/."""
    out = []
    for rel in tracked:
        if Path(rel).name != "Package.swift" or len(Path(rel).parts) > 3:
            continue
        folder = str(Path(rel).parent)
        prefix = "" if folder == "." else folder + "/"
        if any(p.startswith(prefix + "Tests/") for p in tracked):
            out.append(folder)
    return sorted(out, key=lambda f: (f != ".", f))


def _probe(argv: list[str]) -> str | None:
    """A read-only Xcode tool's stdout, or None (missing, failed, timed out)."""
    if not shutil.which(argv[0]):
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=XCODE_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _xcode_scheme(repo: Path, project: str) -> tuple[str, str]:
    """(scheme, where it comes from): the project's own scheme from `xcodebuild -list -json` (the one named like the
    project, else the first), else the project's name."""
    stem = Path(project).stem
    out = _probe(["xcodebuild", "-list", "-json", "-project", str(repo / project)])
    try:
        schemes = json.loads(out)["project"]["schemes"] if out else []
    except (ValueError, KeyError, TypeError):
        schemes = []
    schemes = [s for s in schemes if isinstance(s, str)]
    if not schemes:
        return stem, "the project's name (xcodebuild -list gave no scheme)"
    return (stem if stem in schemes else schemes[0]), "xcodebuild -list"


def newest_iphone(devices: dict) -> str | None:
    """The newest available iPhone simulator in `xcrun simctl list devices available -j` output: on the newest iOS
    runtime, the highest model number, the plain model before its Pro/Max/Plus variants."""
    best = None
    for runtime, items in (devices.get("devices") or {}).items():
        version = _RUNTIME_VERSION.search(runtime)
        if not version or not isinstance(items, list):
            continue
        for d in items:
            name = d.get("name") if isinstance(d, dict) else None
            if not isinstance(name, str) or not name.startswith("iPhone") or d.get("isAvailable") is False:
                continue
            number = _IPHONE_NUMBER.match(name)
            key = (int(version[1]), int(version[2]), int(number[1]) if number else 0, -len(name))
            if best is None or key > best[0]:
                best = (key, name)
    return best[1] if best else None


def _xcode(repo: Path, tracked: list[str], used: set[str]) -> tuple[list[dict], list[str], list[str]]:
    """(checks, notes, ignore probes) for an Xcode project at the root and the local Swift packages with tests."""
    projects = sorted(p.name for p in repo.glob("*.xcodeproj") if p.is_dir())
    packages = _swift_packages(tracked)
    checks, notes = [], []
    for folder in packages:
        label = Path(repo.name if folder == "." else folder).name
        checks.append({"name": check_name(f"swift test {label}", used), "source": "detect",
                       "run": f"swift test --disable-sandbox --package-path {_quoted(folder)} --scratch-path {_quoted('.build/' + label)}",
                       "why": f"{'Package.swift' if folder == '.' else folder + '/Package.swift'} with Tests/ (SwiftPM's own "
                              "sandbox cannot nest in the checks' sandbox: --disable-sandbox)"})
    if projects:
        project = projects[0]
        scheme, scheme_why = _xcode_scheme(repo, project)
        raw = _probe(["xcrun", "simctl", "list", "devices", "available", "-j"])
        try:
            phone = newest_iphone(json.loads(raw)) if raw else None
        except ValueError:
            phone = None
        if phone:
            checks.append({"name": check_name("xcodebuild test", used), "timeout": XCODEBUILD_TIMEOUT, "source": "detect",
                           "run": f"xcodebuild test -project {_quoted(project)} -scheme {_quoted(scheme)} "
                                  f"-destination {_quoted(f'platform=iOS Simulator,name={phone}')} {XCODEBUILD_FLAGS}",
                           "why": f"{project}: scheme from {scheme_why}, newest available iPhone simulator ({phone})"})
        else:
            notes.append(f"{project}: no available iPhone simulator found (xcrun simctl list devices available); add an "
                         "`xcodebuild test` check by hand with a -destination that exists")
        if len(projects) > 1:
            notes.append(f"several Xcode projects ({', '.join(projects)}); the xcodebuild check uses {project}")
    if checks:
        notes.append(f'Xcode/SwiftPM checks need check_sandbox = ["{XCODE_PRESET}"] (SwiftPM\'s cache and the simulator\'s '
                     "test socket); build output goes to .build/")
    return checks, notes, ([".build/x"] if checks else [])


def propose(repo: Path) -> tuple[dict, list[str]]:
    """The deterministic proposal for a repository (see the top of this section) and the ignore probes: paths
    prepare creates that must be git-ignored, or a run's tree is dirty."""
    py_prepare, py_checks, py_env, notes, venv_probe = _python(repo)
    node_prepare, node_checks, node_env, node_notes, node_probes, scripts = _node(repo)
    notes += node_notes
    prepare = py_prepare + node_prepare
    checks = py_checks + node_checks
    if (repo / "go.mod").is_file():
        prepare.append(_p("go mod download", "go.mod"))
        checks.append({"name": "go test", "run": "go test ./...", "why": "go.mod", "source": "detect"})
    if (repo / "Cargo.toml").is_file():
        if (repo / "Cargo.lock").is_file():
            prepare.append(_p("cargo fetch", "Cargo.lock"))
        checks.append({"name": "cargo test", "run": "cargo test", "why": "Cargo.toml", "source": "detect"})
    tracked = _tracked(repo)
    xcode_checks, xcode_notes, xcode_probes = _xcode(repo, tracked, {c["name"] for c in checks})
    checks += xcode_checks
    notes += xcode_notes
    _ci_checks(repo, checks, scripts, notes)
    env: list[dict] = []
    for item in _ci_env(repo) + node_env + _python_switches(repo) + _code_env(repo, tracked) + py_env:
        if not any(e["key"] == item["key"] for e in env):
            env.append(item)
    claude_dir = _claude_config_dir(repo)
    if claude_dir:
        env = [e for e in env if e["key"] != "CLAUDE_CONFIG_DIR"]
        env.append(_e("CLAUDE_CONFIG_DIR", claude_dir, "the Claude login for this folder ([onboard.claude_config_dirs] in the config)"))
    proposal = {"prepare": prepare, "env": env, "checks": checks, "notes": notes, "agents_md": "",
                "check_sandbox": [XCODE_PRESET] if xcode_checks else []}
    return proposal, ([venv_probe] if venv_probe else []) + node_probes + xcode_probes


_SOURCES = ("detect", "agent", "user")
MAX_LINES = 50


def clean_proposal(raw) -> dict:
    """A proposal that comes from outside (the panel's form): only the known fields, one-line strings, at most
    MAX_LINES lines of each kind; verification badges are dropped (the lines may have been edited)."""
    if not isinstance(raw, dict):
        raise ValueError("proposal must be an object")

    def lines(kind: str, fields: tuple[str, ...]) -> list[dict]:
        items = raw.get(kind) or []
        if not isinstance(items, list):
            raise ValueError(f"proposal {kind} must be a list")
        out = []
        for item in items[:MAX_LINES]:
            if not isinstance(item, dict):
                continue
            entry = {f: str(item.get(f) if item.get(f) is not None else "").strip() for f in fields}
            if any("\n" in v or "\r" in v for v in entry.values()):
                raise ValueError(f"proposal {kind}: one line per entry")
            if kind == "checks" and item.get("timeout"):
                try:
                    config.parse_duration(item["timeout"])
                except ValueError as exc:
                    raise ValueError(f"proposal check {entry['name']!r}: {exc}") from None
                entry["timeout"] = item["timeout"]
            entry["why"] = " ".join(str(item.get("why") or "").split())[:1000]
            entry["source"] = item.get("source") if item.get("source") in _SOURCES else "user"
            if kind == "env":
                entry["suggested"] = item.get("suggested") is True
            out.append(entry)
        return out

    notes = raw.get("notes") or []
    presets = raw.get("check_sandbox") or []
    if not isinstance(presets, list) or any(p not in config.CHECK_SANDBOX_PRESETS for p in presets):
        raise ValueError(f"proposal check_sandbox: known presets are {', '.join(config.CHECK_SANDBOX_PRESETS)}")
    return {"check_sandbox": list(dict.fromkeys(presets)), "prepare": lines("prepare", ("run",)), "env": lines("env", ("key", "value")), "checks": lines("checks", ("name", "run")),
            "notes": [" ".join(n.split())[:1000] for n in notes if isinstance(n, str)][:MAX_LINES] if isinstance(notes, list) else [],
            "agents_md": str(raw.get("agents_md") or "")[:60000]}


def spec_from(proposal: dict) -> dict:
    """The target table's prepare, env, checks and check_sandbox from a proposal's lines (empty lines dropped)."""
    checks = []
    for c in proposal.get("checks") or []:
        if str(c.get("name") or "").strip() and str(c.get("run") or "").strip():
            check = {"name": str(c["name"]).strip(), "run": str(c["run"]).strip()}
            if c.get("timeout"):
                check["timeout"] = c["timeout"]
            checks.append(check)
    return {
        "prepare": [str(p["run"]).strip() for p in proposal.get("prepare") or [] if str(p.get("run") or "").strip()],
        "env": {str(e["key"]).strip(): str(e.get("value") or "") for e in proposal.get("env") or [] if str(e.get("key") or "").strip()},
        "checks": checks,
        "check_sandbox": [p for p in proposal.get("check_sandbox") or [] if p in config.CHECK_SANDBOX_PRESETS],
    }


def company(path: str | Path) -> bool:
    """A repository under a folder with its own Claude login or gh account in the [onboard] rules (the split that
    separates work from personal repositories): opening a PR there needs an explicit confirmation."""
    repo = Path(str(path).strip().strip("'\"")).expanduser()
    return bool(_by_path(repo, "claude_config_dirs") or _by_path(repo, "gh_users"))


def _repo_path(path: str | Path) -> Path:
    """A pasted path, tolerant of surrounding blanks and quotes; must be a git checkout."""
    text = str(path).strip().strip("'\"").strip()
    if not text:
        raise ConfigError("path is required")
    repo = Path(text).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ConfigError(f"{repo} is not a git checkout")
    return repo


def _origin(repo: Path) -> str:
    return git("remote", "get-url", "origin", cwd=repo, check=False).stdout.strip()


def candidates(root: Path | None = None, depth: int = 3) -> list[dict]:
    """Git checkouts under ~/Developer (not inside another checkout, not in _archive), with
    whether each is already a target and whether it has a project doc."""
    root = (root or Path.home() / "Developer").expanduser()
    try:
        _, data = config._load()
        configured = {str(Path(raw["path"]).expanduser().resolve()): name for name, raw in (data.get("targets") or {}).items() if isinstance(raw, dict) and raw.get("path")}
    except ConfigError:
        configured = {}
    found = []
    for current, dirs, _files in os.walk(root):
        here = Path(current)
        level = len(here.relative_to(root).parts)
        if (here / ".git").exists() and here != root:
            resolved = str(here.resolve())
            found.append({"path": resolved, "name": str(here.relative_to(root)), "target": configured.get(resolved),
                          "project_doc": (here / PROJECT_DOC).is_file(), "remote": bool(_origin(here))})
            dirs[:] = []  # a checkout's own folders are not separate projects
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")) and d not in _SKIP_DIRS) if level < depth else []
    return sorted(found, key=lambda c: c["name"].lower())


def detect(path: str | Path) -> dict:
    """A proposed target table, the proposal behind its prepare/env/checks (each line with its evidence), the
    readiness checks for the repository at `path`, and whether it is a company repository (whose table proposes
    mode = "controlled"; others "personal")."""
    repo = _repo_path(path)
    proposal, probes = propose(repo)
    remote_default, remote_heads = _remote_heads(repo) if _origin(repo) else (None, None)
    work = company(repo)
    spec = {
        "name": _name(repo),
        "path": str(repo),
        "base_branch": _base_branch(repo, remote_default),
        "remote": "origin",
        "github_repo": _github_repo(repo),
        "gh_user": _gh_user(repo),
        "mode": "controlled" if work else "personal",
        **spec_from(proposal),
    }
    claude_dir = _claude_config_dir(repo)

    checks = []

    def check(key: str, ok: bool, message: str, fix: str = "", blocking: bool = False) -> None:
        checks.append({"key": key, "ok": ok, "blocking": blocking and not ok, "message": message, "fix": "" if ok else fix})

    rules = next((f for f in ("AGENTS.md", "CLAUDE.md") if (repo / f).is_file()), None)
    check("rules", rules is not None, f"rules for the agent: {rules or 'none'}",
          "add an AGENTS.md (stack, how to test, what not to touch); 'Ajanla doldur' drafts one to open as a draft PR")
    check("project_doc", (repo / PROJECT_DOC).is_file(), f"project doc: {PROJECT_DOC}" + ("" if (repo / PROJECT_DOC).is_file() else " (none)"),
          f"optional: open a `project` ticket; it writes {PROJECT_DOC} (goals, scope, architecture) that every plan reads")
    dirty = git("status", "--porcelain", cwd=repo, check=False).stdout.strip()
    check("clean", not dirty, "the checkout is clean", "commit or stash the local changes; worktrees branch off the remote base anyway")
    origin = _origin(repo)
    check("remote", bool(origin), f"origin remote: {origin or 'none'}",
          "push the repository to GitHub first (e.g. `gh repo create <name> --private --source . --push`): "
          "ticket worktrees branch off origin/<base> and draft PRs are pushed there", blocking=True)
    if origin and remote_heads is None:
        check("base", False, f"base branch on origin: {spec['base_branch']} (origin not reachable, not checked)",
              "check the remote URL and your access; the first run fetches it")
    elif origin:
        base = spec["base_branch"]
        work_branch = base.split("/", 1)[0] in ("feat", "fix", "chore", "refactor", "docs")
        check("base", base in remote_heads and not work_branch,
              f"base branch on origin: {base}" + ("" if base in remote_heads else " (missing)"),
              f"origin has no `{base}` branch; branches there: {', '.join(sorted(remote_heads)) or 'none'}. "
              "Create the base on GitHub (e.g. `git push origin <commit>:refs/heads/main`, then make it the default branch) "
              "or pick an existing branch as base" if base not in remote_heads else
              f"`{base}` looks like a work branch; ticket branches and PRs would stack on it. Create `main` from it on GitHub "
              "and make that the default branch",
              blocking=base not in remote_heads)
    check("github", bool(spec["github_repo"]), f"GitHub repository: {spec['github_repo'] or 'not found'}", "set github_repo (owner/name) to open draft PRs")
    check("gh_user", bool(spec["gh_user"]), f"gh account for PRs: {spec['gh_user'] or 'unknown'}", "set gh_user, or add a rule under [onboard.gh_users] in the config")
    account = claude_account(claude_dir)
    where = f"CLAUDE_CONFIG_DIR={claude_dir}" if claude_dir else "the default login"
    check("claude", account is not None, f"Claude account for runs: {account or 'unknown'} ({where})",
          f"log in once: CLAUDE_CONFIG_DIR={claude_dir} claude /login" if claude_dir else "log in once: claude /login")
    runs = " ".join(spec["prepare"])
    if UV_PREPARE in spec["prepare"]:
        check("tool", shutil.which("uv") is not None, "uv project: installed with `uv sync --frozen`, run offline",
              "install uv (https://docs.astral.sh/uv/)")
    elif "-m venv" in runs:
        py = f"python{python_version(repo)}"
        check("tool", shutil.which(py) is not None, f"Python the project asks for: {py}", f"install {py} (e.g. `brew install python@{py[6:]}`)")
    for tool in ("poetry", "yarn", "pnpm", "bun", "go", "cargo"):
        if re.search(rf"(?<![\w/-]){tool} (?:install|mod|fetch)\b", runs):
            check(f"tool:{tool}", shutil.which(tool) is not None, f"{tool} is installed (prepare uses it)", f"install {tool}")
    check("prepare", bool(spec["prepare"]), "dependency install found (prepare)",
          "no lockfile or Python project found; write prepare by hand or let the agent look ('Ajanla doldur')")
    if XCODE_PRESET in spec["check_sandbox"]:
        check("tool:xcodebuild", shutil.which("xcodebuild") is not None,
              f'Xcode checks run with check_sandbox = ["{XCODE_PRESET}"] (SwiftPM cache, simulator test socket)',
              "install Xcode and select it (xcode-select -s /Applications/Xcode.app)")
    check("checks", bool(spec["checks"]), "engine checks after implement: " + (", ".join(c["run"] for c in spec["checks"]) or "none"),
          "no test command found; add checks (name, command) by hand so implement stages are verified")
    for probe in probes:
        folder = str(Path(probe).parent)
        check(f"ignored:{folder}", _ignored(repo, probe), f"{folder}/ is git-ignored", f"add {folder}/ to .gitignore; prepare creates it and a dirty tree halts the run")
    if spec["name"] in _names():
        check("name", False, f"target name {spec['name']} is free", "choose another name", blocking=True)
    return {"spec": spec, "checks": checks, "can_add": not any(c["blocking"] for c in checks), "proposal": proposal,
            "company": work, "has_rules": rules is not None}


def _names() -> list[str]:
    try:
        return config.target_names()
    except ConfigError:
        return []


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def render(spec: dict) -> str:
    name = spec["name"]
    lines = [f"[targets.{name}]", f"path = {_toml_string(spec['path'])}"]
    for key in ("base_branch", "remote", "github_repo", "gh_user", "mode", "plan_model", "build_model"):
        if spec.get(key):
            lines.append(f"{key} = {_toml_string(spec[key])}")
    if spec.get("timeout"):
        lines.append(f"timeout = {int(spec['timeout'])}")
    if spec.get("check_sandbox"):
        lines.append(f"check_sandbox = [{', '.join(_toml_string(p) for p in spec['check_sandbox'])}]")
    if spec.get("prepare"):
        lines.append("prepare = [")
        lines.extend(f"    {_toml_string(c)}," for c in spec["prepare"])
        lines.append("]")
    for check in spec.get("checks") or []:
        lines.extend(["", f"[[targets.{name}.checks]]", f"name = {_toml_string(check['name'])}", f"run = {_toml_string(check['run'])}"])
        if check.get("timeout"):
            timeout = check["timeout"]
            lines.append(f"timeout = {timeout}" if isinstance(timeout, int) else f"timeout = {_toml_string(timeout)}")
    if spec.get("env"):
        lines.extend(["", f"[targets.{name}.env]"])
        lines.extend(f"{_toml_string(k)} = {_toml_string(str(v))}" for k, v in spec["env"].items())
    return "\n".join(lines) + "\n"


def add(spec: dict) -> Path:
    """Append the target table to the config; returns the backup path. Restores the backup when
    the new file does not parse or the target does not load."""
    name = spec.get("name") or ""
    if not config._NAME_RE.match(name):
        raise ConfigError(f"target name {name!r} must be lowercase letters, digits and dashes")
    spec = {**spec, "path": str(_repo_path(spec.get("path") or ""))}
    if not _origin(Path(spec["path"])):
        raise ConfigError(f"{spec['path']} has no origin remote; push it to GitHub first")
    if name in _names():
        raise ConfigError(f"a target named {name} already exists")
    unknown = set(spec) - config._TARGET_KEYS - {"name"}
    if unknown:
        raise ConfigError(f"unknown target keys: {', '.join(sorted(unknown))}")
    if spec.get("mode") not in (None, "", *config.MODES):
        raise ConfigError(f"mode must be \"controlled\" or \"personal\", not {spec['mode']!r}")

    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    if path.exists():
        shutil.copy2(path, backup)
    separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + separator + render(spec), encoding="utf-8")
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
        config.load_target(name)
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        if path.exists() and backup.exists():
            shutil.copy2(backup, path)
        elif not existing:
            path.unlink(missing_ok=True)
        raise ConfigError(f"the new table did not load, config restored: {exc}") from None
    return backup


_HEADER = re.compile(r"^\s*\[\[?\s*(?P<key>[^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _header_target(line: str) -> str | None:
    """The target a table header belongs to: `[targets.shop]`, `[[targets.shop.checks]]` -> "shop";
    None for any other header, "" for a line that is not a header."""
    match = _HEADER.match(line)
    if not match:
        return ""
    parts = [p.strip().strip("\"'") for p in match["key"].split(".")]
    return parts[1] if len(parts) > 1 and parts[0] == "targets" else None


def remove(name: str, busy=None) -> Path:
    """Take the target's tables ([targets.<name>] and its .env, .checks, … sub-tables) out of the config;
    returns the backup path. Every other line stays as it is, comments included; the repository, its
    worktrees and the target's state (tickets, memory) are not touched, so adding it again brings them back.
    `busy(target)` lists its tickets under way; a target with one is refused, as is one that default_target
    or a [sources.*.projects] mapping still names. Nothing is written unless the result parses to the old
    config less exactly this target."""
    path = config.config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = tomllib.loads(text)
    if name not in (data.get("targets") or {}):
        raise ConfigError(f"no target named {name}")
    if data.get("default_target") == name:
        raise ConfigError(f"{name} is default_target; point default_target at another target first")
    for source, raw in (data.get("sources") or {}).items():
        mapped = [project for project, target in ((raw or {}).get("projects") or {}).items() if target == name]
        if mapped:
            raise ConfigError(f"[sources.{source}.projects] maps {', '.join(mapped)} to {name}; remove that mapping first")
    if busy:
        try:
            target = config.load_target(name)
        except ConfigError:
            target = None  # a table that does not load has no runs to wait for
        running = busy(target) if target else []
        if running:
            raise ConfigError(f"{name} has tickets under way ({', '.join(running)}); stop them or let them finish first")

    # A comment right above a header belongs to that table: it goes with the target's tables and stays
    # with the next table after them.
    kept, dropped, inside = [], [], False
    for line in text.splitlines(keepends=True):
        owner = _header_target(line)
        if owner != "":
            if owner == name and not inside:
                while kept and kept[-1].lstrip().startswith("#"):
                    kept.pop()
            elif owner != name and inside:
                tail = []
                while dropped and dropped[-1].lstrip().startswith("#"):
                    tail.insert(0, dropped.pop())
                kept.extend(tail)
            inside = owner == name
            dropped = []
        (dropped if inside else kept).append(line)
    result = "".join(kept)
    if inside:  # the target's tables ended the file: so does the table before them now
        result = result.rstrip("\n") + "\n" if result.strip() else ""
    expected = {**data, "targets": {k: v for k, v in data["targets"].items() if k != name}}
    try:
        parsed = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"removing {name} would break the config ({exc}); edit {path} by hand") from None
    if parsed != expected:
        raise ConfigError(f"could not take {name} out of {path} cleanly; edit it by hand")

    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_text(result, encoding="utf-8")
    return backup


def _bytes(path: Path) -> int:
    """Disk use of a directory tree (`du`: a worktree with its node_modules is too many files to stat in Python)."""
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    try:
        return int(out.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def _common_dir(wt: Path) -> Path | None:
    """The .git of the repository a worktree belongs to, or None when it is not a working worktree any more."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=wt,
                         capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def leftovers() -> list[dict]:
    """What targets no longer in the config left in the state dir ("Çıkar" keeps it, so adding the repository again
    brings its tickets back): per name, its size and its worktrees, which are most of it."""
    try:
        names = set(config.target_names())
    except ConfigError:
        names = set()
    home = config.state_home()
    out = []
    for d in sorted(home.iterdir()) if home.is_dir() else []:
        if not d.is_dir() or d.name in names or not config._NAME_RE.match(d.name):
            continue
        if not ((d / "runs").is_dir() or (d / "worktrees").is_dir()):
            continue  # the panel's own folders (jobs, lead, locks, …)
        trees = []
        for wt in sorted((d / "worktrees").iterdir()) if (d / "worktrees").is_dir() else []:
            if not wt.is_dir():
                continue
            status = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True)
            trees.append({"name": wt.name, "path": str(wt), "bytes": _bytes(wt),
                          "dirty": status.returncode == 0 and bool(status.stdout.strip())})
        out.append({"name": d.name, "path": str(d), "bytes": _bytes(d), "worktrees": trees,
                    "worktree_bytes": sum(t["bytes"] for t in trees)})
    return out


def purge(name: str, force: bool = False) -> dict:
    """Remove the worktrees a removed target left behind. Their branches stay in the repository (only the checkouts
    go), and so do the tickets and memory. A worktree with uncommitted changes is refused unless `force`."""
    found = next((x for x in leftovers() if x["name"] == name), None)
    if found is None:
        raise ConfigError(f"no leftovers of a removed target named {name}")
    dirty = [t["name"] for t in found["worktrees"] if t["dirty"]]
    if dirty and not force:
        raise ConfigError(f"{name}: {', '.join(dirty)} still hold uncommitted changes; remove them anyway to lose those")
    removed, freed = [], 0
    for t in found["worktrees"]:
        wt = Path(t["path"])
        common = _common_dir(wt)
        if common:
            subprocess.run(["git", "--git-dir", str(common), "worktree", "remove", "--force", str(wt)], capture_output=True)
        if wt.exists():  # not a worktree any more (its repository moved or is gone): the files only
            shutil.rmtree(wt, ignore_errors=True)
        if common:
            subprocess.run(["git", "--git-dir", str(common), "worktree", "prune"], capture_output=True)
        if not wt.exists():
            removed.append(t["name"])
            freed += t["bytes"]
    return {"name": name, "removed": removed, "freed_bytes": freed}
