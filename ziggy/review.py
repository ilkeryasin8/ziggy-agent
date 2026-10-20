"""A second opinion on a finished ticket: `codex review` of its branch against the base.

The review runs in the wrapper, in the ticket's worktree, with Codex's own read-only sandbox and
without the run-only environment; it writes nothing to the branch. Each review is kept under
runs/<id>/reviews/<time>/ (review.md, meta.json). Posting one to the draft PR is a comment through
the target's gh account, and only on request. Acting on the findings goes through the CLI `fix`
command: `findings` splits a review into its items, and the ones the user keeps become fix tasks of
a new run (pipeline.request_changes) on the same branch.
"""
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from . import pulls, state
from .config import Target
from .publish import PublishError, _clean_env, gh_as
from . import worktree
from .worktree import git

TIMEOUT = 1800


class ReviewError(Exception):
    pass


def codex_bin() -> str:
    return os.environ.get("ZIGGY_CODEX", "codex")


def _dir(target: Target, ticket_id: str) -> Path:
    return state.run_dir(target, ticket_id) / "reviews"


def _head(wt: Path) -> str | None:
    proc = git("rev-parse", "HEAD", cwd=wt, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def run(target: Target, ticket_id: str) -> dict:
    st = state.load_state(target, ticket_id)
    if st["status"] != "done":
        raise ReviewError(f"{ticket_id} is {st['status']}; a review needs a done ticket")
    wt = Path(st["worktree"] or "")
    if not st["worktree"] or not wt.is_dir():
        raise ReviewError(f"{ticket_id} has no worktree to review")
    ticket = state.load_ticket(target, ticket_id)
    if ticket.get("pull"):
        pulls.sync_ticket(target, ticket_id)  # review what the PR shows now, not what was adopted
    # The ticket's own base: the one its PR targets, even if the target's setting changed since (a stacked slice:
    # the local branch of the slice below, which needs no fetch).
    branch = ticket["base_branch"]
    base = worktree.base_ref(target, ticket)
    if not ticket.get("stack_base"):
        with worktree.shared_git(target, f"fetch of {branch} for a review"):
            git("fetch", "--quiet", target.remote, f"refs/heads/{branch}:refs/remotes/{target.remote}/{branch}", cwd=target.path, timeout=120)

    root = _dir(target, ticket_id)
    root.mkdir(parents=True, exist_ok=True)
    out_dir = root / f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    out_dir.mkdir()  # unique per run; never shared by two reviews
    meta = {"started_at": state.now(), "base": base, "head": _head(wt)}
    state.write_json(out_dir / "meta.json", meta)
    # Codex's built-in review prompt: it takes no custom instructions together with --base.
    argv = [codex_bin(), "review", "--base", base,
            "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"', "-c", "notify=[]"]
    try:
        proc = subprocess.run(argv, cwd=wt, env=_clean_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=TIMEOUT)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr = None, exc.stdout or "", f"timed out after {TIMEOUT}s"
    except OSError as exc:
        code, stdout, stderr = None, "", str(exc)
    text = stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace")
    (out_dir / "review.md").write_text(text, encoding="utf-8")
    meta.update(finished_at=state.now(), exit=code, stderr_tail=(stderr or "")[-2000:] if isinstance(stderr, str) else "")
    state.write_json(out_dir / "meta.json", meta)
    if code != 0 or not text.strip():
        raise ReviewError(f"codex review failed (exit {code}): {(stderr or '').strip()[-500:] if isinstance(stderr, str) else ''}")
    return {"id": out_dir.name, "path": str(out_dir / "review.md"), **meta}


def reviews(target: Target, ticket_id: str) -> list[dict]:
    """Newest first; `stale` when the branch moved on since the reviewed commit."""
    root = _dir(target, ticket_id)
    st = state.load_state(target, ticket_id)
    head = _head(Path(st["worktree"])) if st["worktree"] and Path(st["worktree"]).is_dir() else None
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            meta = state.read_json(d / "meta.json")
        except (OSError, ValueError):
            continue
        text = (d / "review.md").read_text(encoding="utf-8") if (d / "review.md").is_file() else ""
        done = "finished_at" in meta
        out.append({
            "id": d.name, **meta, "text": text,
            "state": "running" if not done else "ok" if meta.get("exit") == 0 and text.strip() else "error",
            "stale": bool(done and head and meta.get("head") and meta["head"] != head),
        })
    return out


def post(target: Target, ticket_id: str, review_id: str) -> str:
    """Add one review to the ticket's draft PR as a comment; returns the PR URL."""
    st = state.load_state(target, ticket_id)
    if not st.get("pr"):
        raise ReviewError(f"{ticket_id} has no pull request yet; open the draft PR first")
    if not target.gh_user:
        raise ReviewError(f"[targets.{target.name}] needs gh_user to comment on the PR")
    found = [r for r in reviews(target, ticket_id) if r["id"] == review_id]
    if not found:
        raise LookupError(f"{ticket_id} has no review {review_id}")
    r = found[0]
    if r["state"] != "ok":
        raise ReviewError(f"review {review_id} did not finish cleanly")
    if r.get("posted_at"):
        raise ReviewError(f"review {review_id} is already on the PR")
    if not r.get("head") or r["head"] != st.get("published_head"):
        raise ReviewError(f"review {review_id} is of {(r.get('head') or '?')[:7]}, but the PR shows "
                          f"{(st.get('published_head') or '?')[:7]}; update the PR or review again")
    body = _dir(target, ticket_id) / review_id / "comment.md"
    sha = (r.get("head") or "")[:7]
    body.write_text(f"### Codex review · `{sha}`\n\n{r['text'].strip()}\n", encoding="utf-8")
    try:
        gh_as(target.gh_user, "pr", "comment", st["pr"], "--body-file", str(body), cwd=target.path)
    except PublishError as exc:
        raise ReviewError(str(exc)) from None
    finally:
        body.unlink(missing_ok=True)
    meta_path = _dir(target, ticket_id) / review_id / "meta.json"
    meta = state.read_json(meta_path)
    meta["posted_at"] = state.now()
    state.write_json(meta_path, meta)
    return st["pr"]


_FINDING = re.compile(r"^\s*(?:[-*]\s+)?\[(?P<priority>P\d)\]\s*(?P<head>.+?)\s*$")
_LOCATION = re.compile(r"^(?P<title>.+?)\s+(?:—|–|-)\s+(?P<location>\S+?:\d+(?:-\d+)?)$")


def findings(review_text: str) -> list[dict]:
    """The `[P1] title — file:line` items of a Codex review, each with the indented explanation
    under it: [{id (1-based), priority, title, location|None, body, text}]."""
    out: list[dict] = []
    current: dict | None = None
    for line in review_text.splitlines():
        match = _FINDING.match(line)
        if match:
            head = match["head"]
            where = _LOCATION.match(head)
            current = {"id": len(out) + 1, "priority": match["priority"], "title": where["title"] if where else head,
                       "location": where["location"] if where else None, "body": [], "head": line.strip()}
            out.append(current)
        elif current is not None:
            if line.strip() and not line[:1].isspace():
                current = None  # unindented text after an item ends it
            else:
                current["body"].append(line.strip())
    for item in out:
        body = "\n".join(item.pop("body")).strip()
        head = item.pop("head")
        item["body"] = body
        item["text"] = head + ("\n" + "\n".join("  " + b if b else "" for b in body.splitlines()) if body else "")
    return out
