"""Push a finished ticket's branch and open a draft pull request.

The only place the pipeline pushes. It runs in the wrapper with the run-only environment
(ZIGGY_RUN, the GIT_CONFIG_* hook path) removed, pushes one fixed refspec, and opens the
pull request as a draft. Approving, marking ready and merging stay with the human.

The PR text is one composer (`draft`): the panel shows it for editing, and the user's edited title and
body (`publish(..., title=, body=)`) are used word for word, with the ticket footer appended.

A slice of a split ticket (stack.py) is stacked: publishing it publishes the slices below it first, its PR's
base is the branch of the slice below, and the footer lists the stack. `retarget` moves a PR's base after the
PR below it was merged; `push_rebased` is the one forced push: a restacked slice's own branch, with a lease.
"""
import html
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import changes, guard, issues, roadmap, stack, state, worktree
from .config import Target
from .project import PROJECT_DOC
from .worktree import git

PROTECTED = {"main", "master"}
_TRAILER = re.compile(r"^\s*co-authored-by:", re.I | re.M)
MAX_TITLE = 256
MAX_BODY = 60000  # GitHub's limit is 65536 characters; the footer goes on top
MAX_EVIDENCE = 20
MAX_ACCEPTANCE = 30


class PublishError(Exception):
    pass


def gh_bin() -> str:
    return os.environ.get("ZIGGY_GH", "gh")


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "ZIGGY_RUN" and not k.startswith("GIT_CONFIG_")}


def gh_as(user: str, *args: str, cwd: Path) -> str:
    """Run gh with the token of one configured account, never the active one."""
    env = _clean_env()
    token = subprocess.run([gh_bin(), "auth", "token", "--user", user], capture_output=True, text=True, env=env)
    if token.returncode != 0 or not token.stdout.strip():
        raise PublishError(f"no gh token for {user}: {token.stderr.strip()}")
    env["GH_TOKEN"] = token.stdout.strip()
    proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True, env=env, cwd=cwd)
    if proc.returncode != 0:
        raise PublishError(f"gh {args[0]} {args[1]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _gh(target: Target, *args: str) -> str:
    return gh_as(target.gh_user, *args, cwd=target.path)


def pending_findings(target: Target, ticket_id: str, title: str | None = None, body: str | None = None) -> list[dict]:
    """What a publish of the ticket would stop on now (the panel asks before it starts the publish job). An adopted PR
    is checked by the job itself, after it fetched the PR."""
    ticket, st = state.load_ticket(target, ticket_id), state.load_state(target, ticket_id)
    if ticket.get("pull") or not st.get("worktree"):
        return []
    text = guard.clean_text(_with_footer(target, body, ticket) if body is not None else pr_body(target, ticket, st))
    found = guard.findings(Path(st["worktree"]), worktree.base_ref(target, ticket), ticket["branch"],
                           guard.clean_text(title) if title is not None else _title(ticket), text)
    return guard.unapproved(st, found)


def _guard(target: Target, ticket_id: str, st: dict, wt: Path, base: str, head: str, title: str = "", body: str = "",
           approve: bool = False, by: str | None = None) -> None:
    """guard.py before a push: refuse while a finding is not approved; `approve` records the current ones."""
    found = guard.unapproved(st, guard.findings(wt, base, head, title, body))
    if not found:
        return
    if not approve:
        raise PublishError(guard.message(found))
    guard.approve(target, ticket_id, found, by or "panel")
    fresh = state.load_state(target, ticket_id)  # publish saves its own copy of the state later: carry the record over
    st.update(guard_approved=fresh["guard_approved"], guard_log=fresh["guard_log"])


def _sections(text: str, heading: str) -> str:
    """The body of every `## <heading>` section, joined (the wrapper may append one twice)."""
    found = re.findall(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return "\n\n".join(b.strip() for b in found if b.strip())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _html(text: str) -> str:
    """One line, safe inside the raw HTML of a `<details>` block (a stray `</details>` cannot close it)."""
    return html.escape(_one_line(text), quote=False)


def acceptance_lines(plan_text: str) -> list[str]:
    """The `AC-n` lines of the plan's `## Goal & acceptance` section, without list markers and without the
    coverage lines that map criteria to tasks (`AC-1 → T003`)."""
    m = re.search(r"^##\s+Goal\s*&\s*acceptance[ \t]*$(.*?)(?=^## |\Z)", plan_text, re.I | re.M | re.S)
    out = []
    for line in (m[1] if m else "").splitlines():
        text = line.strip()
        if not re.search(r"AC-\d+", text) or re.search(r"(→|->)\s*[TV]\d{3}", text):
            continue
        out.append(re.sub(r"^[-*]\s*(\[[ xX]\]\s*)?", "", text))
        if len(out) >= MAX_ACCEPTANCE:
            break
    return out


def _last_review(target: Target, ticket_id: str) -> dict | None:
    """The newest review stage's verdict and finding counts, from its run.json."""
    reviews = [d for d in state.phase_dirs(target, ticket_id) if state.PHASE_DIR.match(d.name)[2].startswith("review")]
    for d in reversed(reviews):
        try:
            record = state.read_json(d / "run.json")
        except (OSError, ValueError):
            continue
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        counts: dict[str, int] = {}
        for finding in result.get("findings") or []:
            counts[finding.get("disposition") or "fix"] = counts.get(finding.get("disposition") or "fix", 0) + 1
        return {"verdict": result.get("verdict") or record.get("verdict"), "converged": bool(result.get("converged")),
                "summary": (result.get("summary") or "").strip(), "counts": counts}
    return None


def _footer(target: Target, ticket: dict) -> list[str]:
    """What every PR body ends with, edited by the user or not: a slice's stack, the ticket's source, the issue it
    closes and its id."""
    stacked = stack.pr_stack_section(target, ticket)
    parts = ([stacked] if stacked else []) + ([f"Ticket: {ticket['source_ref']}"] if ticket.get("source_ref") else [])
    closing = issues.closes(target, ticket)  # a ticket from one of the repository's issues closes it on merge
    return parts + ([closing] if closing else []) + [f"`ziggy ticket {ticket['id']}`"]


def _code_parts(target: Target, ticket: dict, st: dict) -> list[str]:
    """A code ticket's PR sections: what the implementer did, the reviewer's guide (changes, how to verify), the
    plan's acceptance criteria, the user's decisions, the last review and the findings it dismissed (collapsed),
    deferred items, the engine's checks and the evidence files. A v1 ticket (a spec, no plan, no stage results)
    gets none."""
    plan = _read(state.plan_path(target, ticket["id"]))
    inputs = changes.pr_inputs(target, ticket["id"])
    parts = []
    if inputs["work_summary"]:
        parts.append(f"## Summary\n\n{inputs['work_summary']}")
    guide = inputs["guide"]
    if guide:
        parts.append("## Changes\n\n" + "\n".join(
            f"- `{e['file']}`" + (f" — {_one_line(e['summary'])}" if e["summary"] else "") for e in guide))
    verify = [e for e in guide if e["how_to_verify"]]
    if verify:
        parts.append("## How to verify\n\n" + "\n".join(f"- `{e['file']}`: {_one_line(e['how_to_verify'])}" for e in verify))
    criteria = acceptance_lines(plan)
    goal = _sections(plan, "Goal & acceptance")
    if criteria:
        parts.append("## Acceptance criteria\n\n" + "\n".join(f"- {c}" for c in criteria))
    elif goal:  # criteria written as prose: the whole section
        parts.append(f"## Goal & acceptance\n\n{goal}")
    decisions = _sections(plan, "Decisions (user)")
    if decisions:
        parts.append(f"## Decisions\n\n{decisions}")
    review = _last_review(target, ticket["id"])
    if review:
        counts = ", ".join(f"{n} {kind}" for kind, n in sorted(review["counts"].items())) or "no findings"
        line = f"**{review['verdict'] or '?'}**" + (" · converged" if review["converged"] else "") + f" · {counts}"
        parts.append("## Review\n\n" + line + (f"\n\n{review['summary']}" if review["summary"] else ""))
    dismissed = inputs["dismissed"]
    if dismissed:  # collapsed, so a reader can still check what the review chose not to act on
        parts.append(f"<details>\n<summary>Review'un elediği bulgular ({len(dismissed)})</summary>\n\n" + "\n".join(
            f"- `{d['id']}` {_html(d['summary'])}" + (f" — {_html(d['reason'])}" if d["reason"] else "") for d in dismissed)
            + "\n\n</details>")
    deferred = _read(state.inbox_dir(target, ticket["id"]) / "deferred.md")
    items = [m.strip() for m in re.findall(r"^#{2,6}\s+(.+)$", deferred, re.M)]
    if items:
        parts.append("## Deferred\n\n" + "\n".join(f"- {i}" for i in items))
    checks = st.get("checks") or []
    if checks:
        parts.append("## Checks\n\n" + "\n".join(
            f"- {c.get('name', '?')}: {c.get('status', '?')}" + (f" ({c['detail']})" if c.get("detail") else "") for c in checks))
    shown = [a for a in inputs["evidence"] if a["exists"] and a["kind"] != "log"][:MAX_EVIDENCE]
    if shown:  # named, not uploaded: GitHub cannot see the local files
        parts.append("## Evidence\n\n" + "\n".join(f"- {_one_line(a['caption'] or a['path'])} (`{a['path']}`)" for a in shown))
    return parts


def _project_parts(target: Target, ticket: dict, st: dict) -> list[str]:
    """A project ticket's PR sections: what the project doc says (its Product section), the user's decisions,
    and the questions the doc still lists as open."""
    doc = _read(Path(st["worktree"]) / PROJECT_DOC) if st.get("worktree") else ""
    plan = _read(state.plan_path(target, ticket["id"]))
    parts = []
    product = _sections(doc, "Product")
    if product:
        parts.append(f"## Summary\n\n{product}")
    decisions = _sections(plan, "Decisions (user)")
    if decisions:
        parts.append(f"## Decisions\n\n{decisions}")
    open_questions = _sections(doc, "Open questions")
    if open_questions and open_questions.rstrip(".").lower() not in ("none", "none yet"):
        parts.append(f"## Still open\n\n{open_questions}")
    return parts


def _parts(target: Target, ticket: dict, st: dict) -> list[str]:
    return (_project_parts if ticket["type"] == "project" else _code_parts)(target, ticket, st)


def pr_body(target: Target, ticket: dict, st: dict) -> str:
    """The PR description `publish` writes without a user-edited body: the draft plus the footer."""
    return "\n\n".join(_parts(target, ticket, st) + _footer(target, ticket)) + "\n"


def draft(target: Target, ticket_id: str) -> dict:
    """{title, body} for the panel's PR form; the body leaves out the footer, which publish always appends."""
    ticket = state.load_ticket(target, ticket_id)
    parts = _parts(target, ticket, state.load_state(target, ticket_id))
    return {"title": _title(ticket)[:MAX_TITLE], "body": "\n\n".join(parts) + "\n" if parts else ""}


def _with_footer(target: Target, body: str, ticket: dict) -> str:
    return "\n\n".join(([body.rstrip()] if body.strip() else []) + _footer(target, ticket)) + "\n"


def _title(ticket: dict) -> str:
    if ticket["type"] == "project":
        return f"docs: Project doc — {ticket['title']}"
    return f"{ticket['type']}: {ticket['title']}"


def _overrides(title, body) -> tuple[str | None, str | None]:
    """The user's title and body, checked: a one-line title of at most MAX_TITLE characters, a body of at most
    MAX_BODY, and no Co-Authored-By line in either (a squash merge would turn it into a trailer)."""
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise PublishError("the PR title must not be empty")
        title = title.strip()
        if "\n" in title or "\r" in title:
            raise PublishError("the PR title must be one line")
        if len(title) > MAX_TITLE:
            raise PublishError(f"the PR title is too long (max {MAX_TITLE} characters)")
    if body is not None:
        if not isinstance(body, str):
            raise PublishError("the PR body must be text")
        if len(body) > MAX_BODY:
            raise PublishError(f"the PR body is too long (max {MAX_BODY} characters)")
    if any(text and _TRAILER.search(text) for text in (title, body)):
        raise PublishError("the PR title or body has a Co-Authored-By line; remove it before publishing")
    return title, body


def publish(target: Target, ticket_id: str, foreign_ok: bool = False, title: str | None = None,
            body: str | None = None, approve_findings: bool = False, by: str | None = None) -> str:
    """Push the ticket's branch and return the URL of its draft PR (an existing one is reused).
    `title`/`body` are the user's edited PR text (the body gets the footer appended); on an existing PR they
    replace its title/description, which is otherwise left alone (a project PR's body follows its doc).
    An adopted PR (ticket["pull"]) gets the new commits on its own branch instead. Before any push guard.py looks
    at the branch and the PR text: unapproved findings refuse it unless `approve_findings`."""
    title, body = _overrides(title, body)
    title = guard.clean_text(title) if title is not None else None
    body = guard.clean_text(body) if body is not None else None
    if not target.github_repo or not target.gh_user:
        raise PublishError(f"[targets.{target.name}] needs github_repo and gh_user to open pull requests")
    ticket = state.load_ticket(target, ticket_id)
    st = state.load_state(target, ticket_id)
    if st["status"] != "done":
        raise PublishError(f"{ticket_id} is {st['status']}; only a done ticket is published")
    if ticket.get("pull"):
        if title is not None or body is not None:
            raise PublishError("an adopted PR keeps its own title and description")
        return _publish_pull(target, ticket, st, foreign_ok, approve_findings, by)
    branch, base = ticket["branch"], ticket["base_branch"]
    if branch in PROTECTED | {base}:
        raise PublishError(f"refusing to push {branch}: it is a protected or base branch")

    wt = Path(st["worktree"])
    base_ref = worktree.base_ref(target, ticket)
    messages = git("log", "--format=%B%x00", f"{base_ref}..{branch}", cwd=wt).stdout
    if not messages.strip():
        raise PublishError(f"{branch} has no commits over {base_ref}")
    if _TRAILER.search(messages):
        raise PublishError(f"{branch} has commits with a Co-Authored-By trailer; reword them before publishing")
    text = guard.clean_text(_with_footer(target, body, ticket) if body is not None else pr_body(target, ticket, st))
    _guard(target, ticket_id, st, wt, base_ref, branch, title or _title(ticket), text, approve_findings, by)
    stack.publish_below(target, ticket)  # a stacked slice: its PR's base branch goes up first
    if body is None:  # the stack footer names the PRs publish_below just opened
        text = guard.clean_text(pr_body(target, ticket, state.load_state(target, ticket_id)))

    push = subprocess.run(
        ["git", "push", target.remote, f"refs/heads/{branch}:refs/heads/{branch}"],
        cwd=target.path, env=_clean_env(), capture_output=True, text=True, timeout=300,
    )
    if push.returncode != 0:
        raise PublishError(f"push of {branch} failed: {push.stderr.strip()}")

    existing = _gh(target, "pr", "list", "--repo", target.github_repo, "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url")
    is_project = ticket["type"] == "project"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    try:
        if existing:
            url = existing
            fields = ["-f", f"title={title}"] if title is not None else []
            if body is not None or is_project:  # a project PR's summary, decisions and open items move with its doc
                fields += ["-F", f"body=@{fh.name}"]  # -F reads the file verbatim: the markdown arrives intact
            if fields:
                # REST, not `gh pr edit`: its GraphQL query still asks for classic Projects and fails since their
                # deprecation (cli/cli#11983).
                _gh(target, "api", "--method", "PATCH", f"repos/{target.github_repo}/pulls/{_pr_number(url)}", *fields, "--silent")
        else:
            url = _gh(
                target, "pr", "create", "--draft", "--repo", target.github_repo, "--base", base, "--head", branch,
                "--title", title or guard.clean_text(_title(ticket))[:MAX_TITLE], "--body-file", fh.name,
            ).splitlines()[-1]
    finally:
        os.unlink(fh.name)
    st["pr"] = url
    if not existing:
        st["pr_opened_at"] = state.now()  # the delivery metrics count change rounds after this
    st["published_head"] = git("rev-parse", branch, cwd=wt).stdout.strip()  # what the PR shows now
    state.save_state(target, ticket_id, st)
    roadmap.ticket_changed(target, ticket_id)  # its roadmap item, if any, moves to pr (best effort)
    return url


def _pr_number(url: str) -> str:
    number = re.search(r"/pull/(\d+)/?$", url or "")
    if not number:
        raise PublishError(f"cannot tell the PR number from {url}")
    return number[1]


def retarget(target: Target, url: str, base: str) -> None:
    """Change an open PR's base branch (REST, like the update path above): a slice whose PR below was merged."""
    if not target.github_repo or not target.gh_user:
        raise PublishError(f"[targets.{target.name}] needs github_repo and gh_user to change a PR")
    _gh(target, "api", "--method", "PATCH", f"repos/{target.github_repo}/pulls/{_pr_number(url)}", "-f", f"base={base}", "--silent")


def pr_closed(target: Target, url: str) -> bool:
    """The PR is merged or closed on GitHub now (a failed retarget asks: its state may be newer than the ticket's)."""
    try:
        return _gh(target, "api", f"repos/{target.github_repo}/pulls/{_pr_number(url)}", "--jq", ".state").strip() == "closed"
    except (PublishError, ValueError):
        return False


def push_rebased(target: Target, ticket: dict, st: dict) -> str:
    """Push a restacked slice's branch to its own PR: forced, but with a lease on the commit last pushed
    (st["published_head"]), so a push by anyone else since stops it; never another branch, a protected or base
    branch, or an adopted PR's. Returns the commit pushed."""
    branch = ticket["branch"]
    if ticket.get("pull"):
        raise PublishError(f"{ticket['id']} is an adopted PR; its branch is never force-pushed")
    if branch in PROTECTED | {ticket["base_branch"], target.base_branch}:
        raise PublishError(f"refusing to push {branch}: it is a protected or base branch")
    if not st.get("pr") or not st.get("published_head"):
        raise PublishError(f"{ticket['id']} has no PR to update; nothing to push")
    wt = Path(st["worktree"])
    messages = git("log", "--format=%B%x00", f"{worktree.base_ref(target, ticket)}..{branch}", cwd=wt).stdout
    if _TRAILER.search(messages):
        raise PublishError(f"{branch} has commits with a Co-Authored-By trailer; reword them before publishing")
    # A rebase moves lines, not findings: what was approved before stays approved; anything new asks.
    _guard(target, ticket["id"], st, wt, worktree.base_ref(target, ticket), branch)
    push = subprocess.run(
        ["git", "push", f"--force-with-lease=refs/heads/{branch}:{st['published_head']}", target.remote,
         f"refs/heads/{branch}:refs/heads/{branch}"],
        cwd=target.path, env=_clean_env(), capture_output=True, text=True, timeout=300,
    )
    if push.returncode != 0:
        raise PublishError(f"push of the rebased {branch} failed (nothing changed on GitHub): {push.stderr.strip()}")
    return git("rev-parse", branch, cwd=wt).stdout.strip()


def _publish_pull(target: Target, ticket: dict, st: dict, foreign_ok: bool, approve_findings: bool = False,
                  by: str | None = None) -> str:
    """Push an adopted PR's fix commits to the PR's own branch: fast-forward only, never to a
    fork, and to someone else's PR only when the human said so (`foreign_ok`)."""
    pull = ticket["pull"]
    if pull.get("fork"):
        raise PublishError(f"PR #{pull['number']} comes from a fork; the pipeline cannot push to it")
    if pull["head"] in PROTECTED | {pull["base"]}:
        raise PublishError(f"refusing to push to {pull['head']}: it is a protected or base branch")
    if pull["author"] != target.gh_user and not foreign_ok:
        raise PublishError(f"PR #{pull['number']} is {pull['author']}'s; confirm pushing to their branch (--foreign-ok)")
    wt = Path(st["worktree"])
    remote = worktree.fetch_pull(target, pull["number"])
    if not worktree.is_ancestor(wt, remote, "HEAD"):
        raise PublishError(f"PR #{pull['number']} moved on to {remote[:7]}; review or fix again to pick it up, then push")
    messages = git("log", "--format=%B%x00", f"{remote}..HEAD", cwd=wt).stdout
    if not messages.strip():
        raise PublishError(f"{ticket['branch']} has no commits over PR #{pull['number']}")
    if _TRAILER.search(messages):  # only the pipeline's own commits; the PR's are not ours to judge
        raise PublishError(f"{ticket['branch']} has commits with a Co-Authored-By trailer; reword them before publishing")
    _guard(target, ticket["id"], st, wt, remote, "HEAD", approve=approve_findings, by=by)  # only what we add to it
    push = subprocess.run(
        ["git", "push", target.remote, f"refs/heads/{ticket['branch']}:refs/heads/{pull['head']}"],
        cwd=target.path, env=_clean_env(), capture_output=True, text=True, timeout=300,
    )
    if push.returncode != 0:
        raise PublishError(f"push to {pull['head']} failed: {push.stderr.strip()}")
    st["published_head"] = worktree.head(wt)
    state.save_state(target, ticket["id"], st)
    return st["pr"]
