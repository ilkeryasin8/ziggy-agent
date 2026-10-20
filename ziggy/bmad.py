"""BMad Method in a target repository: whether it is installed, and whether this machine can run it.

BMad counts as installed when the target's base branch on the remote tracks the `bmad-build-auto` skill and
`_bmad/`. Its six per-developer files are gitignored, so they come from the main checkout (`bmad-setup`
there writes them); a `bmad` ticket's worktree gets copies of them. Read-only: nothing here fetches or
changes a checkout."""
import re
import time

from .config import Target
from .worktree import git

PIPELINE = "bmad"
SKILL = ".claude/skills/bmad-build-auto/SKILL.md"
ROOT = "_bmad"
MANIFEST = "_bmad/_config/manifest.yaml"
RECORDS = "_bmad-output"
LOCAL_FILES = (
    "_bmad/config.user.toml",
    "_bmad/core/config.yaml",
    "_bmad/bmm/config.yaml",
    "_bmad/_config/manifest.yaml",
    "_bmad/_config/files-manifest.csv",
    "_bmad/custom/config.user.toml",
)
CACHE_SECONDS = 60
_CACHE: dict[str, tuple[float, dict]] = {}
_VERSION = re.compile(r"^\s*version:\s*['\"]?([0-9][^\s'\"]*)", re.M)


def _version(text: str) -> str | None:
    found = _VERSION.search(text or "")
    return found[1] if found else None


def _read(target: Target) -> dict:
    ref = f"{target.remote}/{target.base_branch}"
    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=target.path, check=False).returncode != 0:
        return {"installed": False, "version": None, "reason": f"{ref} is not fetched"}
    missing = [p for p, kind in ((SKILL, "blob"), (ROOT, "tree"))
               if git("cat-file", "-t", f"{ref}:{p}", cwd=target.path, check=False).stdout.strip() != kind]
    if missing:
        return {"installed": False, "version": None, "reason": f"{ref} has no {' or '.join(missing)}"}
    shown = git("show", f"{ref}:{MANIFEST}", cwd=target.path, check=False)
    version = _version(shown.stdout) if shown.returncode == 0 else None
    if version is None:
        try:
            version = _version((target.path / MANIFEST).read_text(encoding="utf-8"))
        except OSError:
            version = None
    return {"installed": True, "version": version, "reason": f"{ref} has {SKILL} and {ROOT}/"}


def status(target: Target, fresh: bool = False) -> dict:
    """{"installed", "version", "reason"} for the target's remote base branch; cached for a minute."""
    cached = _CACHE.get(target.name)
    if cached and not fresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    result = _read(target)
    _CACHE[target.name] = (time.monotonic(), result)
    return result


def local_ready(target: Target) -> list[str]:
    """The per-developer BMad files missing in the main checkout (empty: this machine can run BMad)."""
    return [rel for rel in LOCAL_FILES if not (target.path / rel).is_file()]


def check(target: Target) -> None:
    """Raise ValueError unless a ticket of this target can run the bmad pipeline."""
    found = status(target, fresh=True)
    if not found["installed"]:
        raise ValueError(f"BMad is not installed in {target.name} ({found['reason']}); use the standard pipeline")
    missing = local_ready(target)
    if missing:
        raise ValueError(f"BMad's per-developer files are missing in {target.path}: {', '.join(missing)} "
                         "(run BMad's setup in the main checkout first)")
