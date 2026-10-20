"""Starting a new project: a repository from nothing, on GitHub, added as a target.

`create_project` writes README, AGENTS.md, .gitignore and a `docs/PROJECT.md` stub (the project
doc a `project` ticket fills in), makes one first commit on main, creates the GitHub repository
under the chosen gh account, pushes main and adds the target to the config.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, onboard, publish
from .config import PIPELINE_ROOT, ConfigError
from .project import PROJECT_DOC
from .publish import PublishError, _clean_env, gh_as
from .worktree import GitError, git

TEMPLATES = PIPELINE_ROOT / "templates"
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class SetupError(Exception):
    pass


def project_parents() -> list[str]:
    """Folders a new project may go in: ~/Developer and the existing [onboard] rule prefixes."""
    roots = [Path.home() / "Developer"]
    try:
        _, data = config._load()
        rules = data.get("onboard") or {}
        for table in ("gh_users", "claude_config_dirs"):
            roots += [Path(p).expanduser() for p in (rules.get(table) or {})]
    except ConfigError:
        pass
    out = []
    for root in roots:
        if root.is_dir() and str(root) not in out:
            out.append(str(root))
    return out


_CHOICES: dict[str, tuple[float, dict]] = {}


def gh_accounts() -> list[str]:
    """The accounts `gh` is logged in to (its JSON status output is not in every version)."""
    proc = subprocess.run([publish.gh_bin(), "auth", "status"], capture_output=True, text=True, env=_clean_env(), timeout=30)
    return list(dict.fromkeys(re.findall(r"account (\S+)", proc.stdout + proc.stderr)))


def account_choices() -> dict:
    """Who can own a new repository (each gh account and its organizations, with the account
    that acts for it) and which Claude logins exist (the default and the [onboard] ones), each
    with the account it is logged in to. Cached for a few minutes: it asks GitHub and claude."""
    cached = _CHOICES.get("all")
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    owners = []
    for account in gh_accounts():
        owners.append({"owner": account, "gh_user": account, "kind": "user"})
        try:
            orgs = json.loads(gh_as(account, "api", "user/orgs", "--jq", "[.[].login]", cwd=Path.home()) or "[]")
        except (PublishError, ValueError):
            orgs = []
        owners += [{"owner": org, "gh_user": account, "kind": "org"} for org in orgs]
    dirs = [None]
    try:
        _, data = config._load()
        dirs += [str(Path(v).expanduser()) for v in ((data.get("onboard") or {}).get("claude_config_dirs") or {}).values()]
    except ConfigError:
        pass
    logins = [{"dir": d, "account": onboard.claude_account(d)} for d in dict.fromkeys(dirs)]
    result = {"owners": owners, "claude_logins": logins}
    _CHOICES["all"] = (time.time(), result)
    return result


def project_defaults(parent: str, name: str = "x") -> dict:
    """The gh account and Claude login a new project at parent/name would get by its folder,
    and the choices the form offers instead."""
    path = Path(parent).expanduser() / (name or "x")
    claude_dir = onboard._claude_config_dir(path)
    return {"gh_user": onboard._gh_user(path), "claude_config_dir": claude_dir,
            "claude_account": onboard.claude_account(claude_dir), **account_choices()}


def create_project(name: str, parent: str, description: str = "", owner: str | None = None, private: bool = True,
                   gh_user: str | None = None, claude_config_dir: str | None = None) -> str:
    """Start a repository at parent/name; returns the new target's name."""
    name = name.strip()
    if not _PROJECT_NAME.match(name):
        raise SetupError("project name: letters, digits, '.', '_' and '-' only")
    root = Path(parent).expanduser().resolve()
    if str(root) not in [str(Path(p).resolve()) for p in project_parents()]:
        raise SetupError(f"{root} is not one of the project folders: {', '.join(project_parents())}")
    path = root / name
    if path.exists():
        raise SetupError(f"{path} already exists")
    target_name = onboard._name(path)
    if target_name in onboard._names():
        raise SetupError(f"a target named {target_name} already exists")
    gh_user = (gh_user or "").strip() or onboard._gh_user(path)
    if not gh_user:
        raise SetupError("no gh account for this folder: set [onboard] default_gh_user in the config")
    owner = (owner or gh_user).strip()
    description = " ".join(description.split())

    path.mkdir()
    try:
        git("init", "--quiet", "-b", "main", cwd=path)
        (path / "README.md").write_text(f"# {name}\n\n{description}\n" if description else f"# {name}\n", encoding="utf-8")
        agents = (TEMPLATES / "AGENTS.md").read_text(encoding="utf-8")
        (path / "AGENTS.md").write_text(agents.replace("{name}", name).replace("{description}", description or "What this project is for: write it here."), encoding="utf-8")
        shutil.copy2(TEMPLATES / "gitignore-base", path / ".gitignore")
        doc = (TEMPLATES / "PROJECT.md").read_text(encoding="utf-8")
        (path / "docs").mkdir()
        (path / PROJECT_DOC).write_text(doc.replace("{name}", name).replace("{description}", description or "Not written yet."), encoding="utf-8")
        git("add", "--all", cwd=path)
        # The one commit a new repository has to make on main; later work comes in by PR.
        git("commit", "--quiet", "--no-verify", "-m", f"chore: start {name}\n\n"
            "README, AGENTS.md, ignore rules and a docs/PROJECT.md stub, so the first tickets can run.", cwd=path)
    except (GitError, SetupError, OSError):
        shutil.rmtree(path, ignore_errors=True)
        raise

    gh_as(gh_user, "repo", "create", f"{owner}/{name}", "--private" if private else "--public",
          *(["--description", description] if description else []), cwd=path)
    host = os.environ.get("ZIGGY_GITHUB_URL", "https://github.com")  # a local path in tests
    git("remote", "add", "origin", f"{host}/{owner}/{name}.git", cwd=path)
    push = subprocess.run(["git", "push", "--quiet", "-u", "origin", "main"], cwd=path, env=_clean_env(),
                          capture_output=True, text=True, timeout=300)
    if push.returncode != 0:
        raise SetupError(f"repository {owner}/{name} created, but the push failed: {push.stderr.strip()}")

    spec = onboard.detect(path)["spec"]
    spec["name"] = target_name
    spec["github_repo"] = f"{owner}/{name}"
    spec["gh_user"] = gh_user
    if claude_config_dir is not None:  # the form's choice; "" is the default login
        spec["env"].pop("CLAUDE_CONFIG_DIR", None)
        if claude_config_dir:
            spec["env"]["CLAUDE_CONFIG_DIR"] = str(Path(claude_config_dir).expanduser())
    onboard.add(spec)
    return target_name
