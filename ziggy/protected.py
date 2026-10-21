"""Protected files: the repository files no agent may write (claude-settings.json denies `Edit(./<file>)` for them, so a
poisoned ticket cannot leave lasting instructions for later agents), and the patches that change them with your consent.

A stage that needs one changed writes a unified diff to the inbox artifacts and lists it in `protectedPatches` (the
implement and generic result schemas). The engine validates each listing (`settle`): the file exists in the inbox, its
diff headers name only protected files, and `git apply --check` passes on the worktree's HEAD; a valid patch is copied
to `runs/<id>/protected/<n>.patch` and recorded in the ticket's state as `protected_patches` (status pending), and the
pipeline goes on as if the stage had nothing open. An implement stage that returned FAILED only because it could not
write a protected file, with a valid protected-only patch among its artifacts, is read as READY with that patch (a
result warning says so).

The inbox shows each pending patch as a `protected` item with the diff. A stage that records a new patch for a file
supersedes every older pending patch of the ticket touching any of the same files (status `superseded`,
`superseded_by`): only the newest pending one per file waits. Each patch records `base_head`, the worktree HEAD it was
validated against. `apply` (Uygula, `ziggy protected <id> apply <n>`) runs here, outside the sandbox: under the
ticket's lock (refused while a stage runs), on a clean worktree, it checks the patch again on the current HEAD, applies
it and commits it as `docs(<ticket>): apply approved change to <files>` (no hooks, like the records commits). When it
no longer applies because an earlier patch of the ticket was applied since `base_head` (every commit since then that
touched the files is one of the ticket's applied patch commits), the patch's own result (its files at `base_head` with
the patch applied) replaces the files and the commit says `(supersedes #<n>)`; any other change to the files since
refuses it. `reject` marks it rejected, with an optional reason. Publishing is never blocked; the PR form warns while
one is pending.
"""
import json
import os
import posixpath
import subprocess
import tempfile
from pathlib import Path

from . import results, state
from .config import PACKAGE, Target
from .worktree import GitError, git

SETTINGS = PACKAGE / "claude-settings.json"
DIR = "protected"  # runs/<id>/protected/<n>.patch: the copies the wrapper applies
STATUSES = ("pending", "applied", "rejected", "superseded")
SCHEMAS = ("implement", "generic")  # the result schemas that may list protectedPatches
DIFF_MAX = 20000  # characters of a patch the inbox card shows


class ProtectedError(Exception):
    pass


def _denied_files(path: Path = SETTINGS) -> tuple[str, ...]:
    """The repository files claude-settings.json denies Edit for (`Edit(./AGENTS.md)` → AGENTS.md); globs are left out."""
    try:
        deny = json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    out = []
    for rule in deny:
        if isinstance(rule, str) and rule.startswith("Edit(./") and rule.endswith(")"):
            rel = rule[len("Edit(./"):-1]
            if rel and not any(c in rel for c in "*?[{"):
                out.append(rel)
    return tuple(dict.fromkeys(out))


FILES = _denied_files()


def is_protected(rel: str) -> bool:
    return posixpath.normpath(rel.strip()) in FILES


def _strip_prefix(path: str) -> str | None:
    path = path.strip().split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return posixpath.normpath(path)


def patch_files(text: str) -> list[str]:
    """Every path a unified diff's headers name (`diff --git`, ---/+++, rename/copy from/to), sorted; [] for none."""
    found = set()
    for line in text.splitlines():
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            if " b/" in rest:
                a, b = rest.rsplit(" b/", 1)
                found.update(p for p in (_strip_prefix(a), _strip_prefix("b/" + b)) if p)
        elif line.startswith(("--- ", "+++ ")):
            path = _strip_prefix(line[4:])
            if path:
                found.add(path)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            found.add(posixpath.normpath(line.split(" ", 2)[2].strip()))
    return sorted(found)


def check(wt: Path, patch: Path) -> tuple[list[str], str | None]:
    """(the files the patch touches, why it is not a valid protected patch or None)."""
    if not patch.is_file():
        return [], f"{patch.name}: not found"
    text = patch.read_text(encoding="utf-8", errors="replace")
    files = patch_files(text)
    if not files:
        return [], f"{patch.name}: no diff headers (a unified diff with relative paths is expected)"
    outside = [f for f in files if f not in FILES]
    if outside:
        return files, f"{patch.name}: touches files that are not protected: {', '.join(outside)}"
    proc = subprocess.run(["git", "apply", "--check", str(patch.resolve())], cwd=wt, capture_output=True, text=True)
    if proc.returncode != 0:
        return files, f"{patch.name}: does not apply on HEAD: {(proc.stderr or proc.stdout).strip()[:400]}"
    return files, None


def _artifact_rel(raw, artifacts_dir: str) -> str | None:
    """A listing's path relative to the artifacts dir ('artifacts/x.patch', 'x.patch' or an absolute path inside it)."""
    if not isinstance(raw, str):
        return None
    rel = raw.strip().replace("\\", "/")
    if rel.startswith("artifacts/"):
        rel = rel[len("artifacts/"):]
    found = results.normalize_artifacts([rel], artifacts_dir)
    return found[0]["path"] if found else None


def _validated(listings, artifacts_dir: str, wt: Path) -> tuple[list[dict], list[str]]:
    valid, warnings = [], []
    for item in listings:
        raw = item.get("path") if isinstance(item, dict) else item
        rel = _artifact_rel(raw, artifacts_dir)
        if rel is None:
            warnings.append(f"protectedPatches: {raw!r} is not a file under the inbox artifacts; ignored")
            continue
        if any(v["artifact"] == rel for v in valid):
            continue
        files, error = check(wt, Path(artifacts_dir) / rel)
        if error:
            warnings.append(f"protectedPatches: {error}; ignored")
            continue
        summary = item.get("summary") if isinstance(item, dict) else None
        valid.append({"artifact": rel, "files": files, "summary": " ".join(summary.split())[:500] if isinstance(summary, str) else ""})
    return valid, warnings


def settle(schema: str, result: dict, artifacts_dir: str, wt: Path) -> list[str]:
    """Validates the result's protectedPatches in place (each {artifact, files, summary}); the FAILED shape of an
    implement stage whose only blocker was a protected file becomes READY with its patch. Returns result warnings."""
    if schema not in SCHEMAS:
        result.pop("protectedPatches", None)
        return []
    listed = result.get("protectedPatches")
    if listed is not None and not isinstance(listed, list):
        result["protectedPatches"] = []
        return ["protectedPatches: expected a list; ignored"]
    valid, warnings = _validated(listed or [], artifacts_dir, wt)
    result["protectedPatches"] = valid
    if listed or schema != "implement" or result.get("verdict") != "FAILED":
        return warnings
    # The shape before protectedPatches existed: FAILED, the summary or open items naming a protected file, and a
    # clean patch of only protected files among the artifacts.
    said = " ".join([result.get("summary") or "", *[i for i in result.get("openItems") or [] if isinstance(i, str)]])
    if not any(name in said for name in FILES):
        return warnings
    patches = [{"path": a["path"], "summary": a.get("caption") or ""} for a in result.get("artifacts") or []
               if isinstance(a, dict) and str(a.get("path", "")).endswith((".patch", ".diff"))]
    found, _ = _validated(patches, artifacts_dir, wt)
    if not found:
        return warnings
    result["protectedPatches"] = found
    result["verdict"] = "READY"
    names = ", ".join(sorted({f for p in found for f in p["files"]}))
    return warnings + [f"FAILED read as READY: the only blocker was a protected file ({names}); its patch "
                       f"({', '.join(p['artifact'] for p in found)}) waits for your approval"]


def pending(st: dict) -> list[dict]:
    return [p for p in st.get("protected_patches") or [] if p.get("status") == "pending"]


def record(target: Target, ticket_id: str, st: dict, found: list[dict], artifacts_dir: str, stage: str, phase_dir: str,
           *, base_head: str) -> list[dict]:
    """Copies each validated patch to runs/<id>/protected/<n>.patch and adds it to st["protected_patches"] as pending,
    with base_head (the worktree HEAD it was validated against); a patch identical to one still pending is not added
    again. Older pending patches touching any of the same files become superseded by the new one. Returns the new entries."""
    root = state.run_dir(target, ticket_id) / DIR
    entries = st.setdefault("protected_patches", [])
    added = []
    for p in found:
        data = (Path(artifacts_dir) / p["artifact"]).read_bytes()
        if any((root / f"{e['n']}.patch").is_file() and (root / f"{e['n']}.patch").read_bytes() == data for e in pending(st)):
            continue
        n = 1 + max((e["n"] for e in entries), default=0)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{n}.patch").write_bytes(data)
        for old in pending(st):
            if set(old["files"]) & set(p["files"]):
                old.update(status="superseded", superseded_by=n, superseded_at=state.now())
        entry = {"n": n, "path": f"{DIR}/{n}.patch", "files": p["files"], "summary": p["summary"], "artifact": p["artifact"],
                 "stage": stage, "phase_dir": phase_dir, "run": (st.get("run") or {}).get("n"), "status": "pending",
                 "created_at": state.now(), "base_head": base_head}
        entries.append(entry)
        added.append(entry)
    return added


def replaces(target: Target, ticket_id: str, st: dict, entry: dict) -> list[int]:
    """The applied patches of the same files a pending one was prepared without (their commit is not in its base_head):
    applying it replaces their content and supersedes them."""
    applied = [e for e in st.get("protected_patches") or [] if e.get("status") == "applied" and e.get("sha")
               and not e.get("superseded_by") and set(e.get("files") or []) & set(entry["files"])]
    wt = Path(st.get("worktree") or "")
    base = base_head(target, ticket_id, st, entry) if applied and st.get("worktree") and wt.is_dir() else None
    if not base:
        return []
    return [e["n"] for e in applied if git("merge-base", "--is-ancestor", e["sha"], base, cwd=wt, check=False).returncode == 1]


def diff_text(target: Target, ticket_id: str, entry: dict, limit: int = DIFF_MAX) -> str:
    try:
        text = (state.run_dir(target, ticket_id) / entry["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)\n"


def listing(target: Target, ticket_id: str) -> list[dict]:
    """Every protected patch of the ticket with its diff text (and, pending, the applied ones it replaces), oldest first."""
    st = state.load_state(target, ticket_id)
    return [{**e, "diff": diff_text(target, ticket_id, e),
             **({"replaces": replaces(target, ticket_id, st, e)} if e.get("status") == "pending" else {})}
            for e in st.get("protected_patches") or []]


def _entry(st: dict, n: int) -> dict:
    entry = next((e for e in st.get("protected_patches") or [] if e.get("n") == n), None)
    if entry is None:
        raise LookupError(f"no protected patch #{n}")
    if entry.get("status") == "superseded":
        raise ProtectedError(f"protected patch #{n} is superseded by #{entry.get('superseded_by')}")
    if entry.get("status") != "pending":
        raise ProtectedError(f"protected patch #{n} is {entry.get('status')} already")
    return entry


def base_head(target: Target, ticket_id: str, st: dict, entry: dict) -> str | None:
    """The worktree HEAD a patch was validated against (a revision, maybe `<sha>^`). Patches recorded before base_head
    existed fall back to the stage's run.json `head_after` (the HEAD at the stage's end, where settle checked it), else
    to the parent of the first applied protected commit of the ticket touching the same files."""
    if entry.get("base_head"):
        return entry["base_head"]
    try:
        run = json.loads((state.run_dir(target, ticket_id) / entry["phase_dir"] / "run.json").read_text())
        if isinstance(run, dict) and run.get("head_after"):
            return run["head_after"]
    except (KeyError, OSError, TypeError, ValueError):
        pass
    applied = sorted((e for e in st.get("protected_patches") or [] if e.get("sha") and e.get("applied_at")
                      and set(e.get("files") or []) & set(entry["files"])), key=lambda e: e["applied_at"])
    return f"{applied[0]['sha']}^" if applied else None


def _intended_tree(wt: Path, base: str, patch: Path) -> str | None:
    """The tree of `base` with the patch applied, built in a temporary index (the worktree and its index untouched);
    None when it does not apply there."""
    with tempfile.TemporaryDirectory(prefix="ziggy-protected-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for args in (["read-tree", base], ["apply", "--cached", str(patch.resolve())]):
            if subprocess.run(["git", *args], cwd=wt, env=env, capture_output=True, text=True).returncode != 0:
                return None
        proc = subprocess.run(["git", "write-tree"], cwd=wt, env=env, capture_output=True, text=True)
        return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def _over_earlier(target: Target, ticket_id: str, st: dict, entry: dict, wt: Path, patch: Path,
                  files: list[str]) -> tuple[str, list[dict]] | None:
    """For a patch that no longer applies on HEAD: (its intended tree, the ticket's applied patches it replaces) when
    every commit since its base_head that touched its files is one of the ticket's applied protected commits; None
    when that is not the reason (no base, base not on the branch, no such commit, the patch broken at its base).
    Raises when someone else changed the files since."""
    if not files or not all(is_protected(f) for f in files):
        return None
    rev = base_head(target, ticket_id, st, entry)
    proc = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", cwd=wt, check=False) if rev else None
    base = proc.stdout.strip() if proc and proc.returncode == 0 else ""
    if not base or git("merge-base", "--is-ancestor", base, "HEAD", cwd=wt, check=False).returncode != 0:
        return None
    touched = git("rev-list", "--reverse", f"{base}..HEAD", "--", *files, cwd=wt).stdout.split()
    if not touched:
        return None
    ours = {e["sha"]: e for e in st.get("protected_patches") or [] if e.get("sha") and e.get("applied_at")}
    if any(sha not in ours for sha in touched):
        raise ProtectedError(f"{', '.join(files)} bu yama hazırlandıktan sonra başka bir değişiklikle değişti; yama "
                             "uygulanamıyor. Reddet ve ajanın yeni bir yama hazırlamasını iste.")
    tree = _intended_tree(wt, base, patch)
    return (tree, [ours[sha] for sha in touched]) if tree else None


def _notify(target: Target, ticket_id: str) -> None:
    from . import attention  # attention imports the engine, which imports this module
    attention.raise_for(target, ticket_id)


def apply(target: Target, ticket_id: str, n: int, by: str | None = None) -> dict:
    """Uygula: applies pending patch #n on the ticket branch and commits it (the wrapper, outside the sandbox).
    Refused while a stage runs (the ticket's lock), on a dirty worktree, or when the patch no longer applies — unless
    only the ticket's earlier applied patches changed its files since its base_head: then its own result replaces
    theirs (`_over_earlier`) and they are marked superseded_by #n."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} (a stage is running)"):
        st = state.load_state(target, ticket_id)
        entry = _entry(st, n)
        wt = Path(st.get("worktree") or "")
        if not st.get("worktree") or not wt.is_dir():
            raise ProtectedError(f"{ticket_id} has no worktree to apply the patch in")
        dirty = git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout.strip()
        if dirty:
            raise ProtectedError(f"{ticket_id}: the worktree has uncommitted changes; the patch was not applied")
        patch = state.run_dir(target, ticket_id) / entry["path"]
        files, error = check(wt, patch)
        over = _over_earlier(target, ticket_id, st, entry, wt, patch, files) if error else None
        if error and over is None:
            raise ProtectedError(f"protected patch #{n} was not applied: {error}")
        message = f"docs({ticket_id}): apply approved change to {', '.join(files)}"
        try:
            if over is None:
                git("apply", "--index", str(patch.resolve()), cwd=wt)
            else:
                tree, replaced = over
                for f in files:
                    if git("cat-file", "-e", f"{tree}:{f}", cwd=wt, check=False).returncode == 0:
                        git("checkout", tree, "--", f, cwd=wt)
                    else:
                        git("rm", "--quiet", "--ignore-unmatch", "--", f, cwd=wt)
                if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
                    raise ProtectedError(f"protected patch #{n} was not applied: {', '.join(files)} already has its content "
                                         f"(#{', #'.join(str(e['n']) for e in replaced)}); reject it")
                message += f" (supersedes #{', #'.join(str(e['n']) for e in replaced)})"
            git("commit", "--no-verify", "--quiet", "--message", message, cwd=wt)
        except (GitError, ProtectedError):
            git("reset", "--quiet", "--hard", "HEAD", cwd=wt, check=False)  # the tree was clean before
            raise
        entry.update(status="applied", applied_at=state.now(), sha=git("rev-parse", "HEAD", cwd=wt).stdout.strip(),
                     by=by, files=files)
        if over is not None:
            for old in over[1]:
                old["superseded_by"] = n
        state.save_state(target, ticket_id, st)
    _notify(target, ticket_id)
    return entry


def reject(target: Target, ticket_id: str, n: int, reason: str = "", by: str | None = None) -> dict:
    """Reddet: marks pending patch #n rejected (refused while a stage runs: the run rewrites the state)."""
    with state.lock(target, f"ticket-{ticket_id}", f"ticket {ticket_id} (a stage is running)"):
        st = state.load_state(target, ticket_id)
        entry = _entry(st, n)
        entry.update(status="rejected", rejected_at=state.now(), reason=" ".join(reason.split())[:1000], by=by)
        state.save_state(target, ticket_id, st)
    _notify(target, ticket_id)
    return entry


def prompt_view(target: Target, ticket_id: str, st: dict) -> dict:
    """What stage prompts see: the protected files, and the pending patches (with the path of each stored copy)."""
    root = state.run_dir(target, ticket_id)
    waiting = [{"n": p["n"], "files": ", ".join(p["files"]), "summary": p.get("summary") or "", "path": str(root / p["path"])}
               for p in pending(st)]
    return {"files": ", ".join(f"`{f}`" for f in FILES), "pending": waiting, "has_pending": bool(waiting)}
