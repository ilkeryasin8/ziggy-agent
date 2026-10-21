"""GitHub Issues as a work source: the target's open issues labelled `issue_label` become tickets (`gh-<n>`), once
each, with the type their labels say. Their PRs close the issue on merge (`Closes #<n>` in the PR body, publish.py).
Nothing is written to the issue itself. The panel's PR sync runs `sync`; `ziggy issues sync` does it by hand
(`--dry-run` lists what it would create). With `issue_run`, a new ticket starts at once (a panel job)."""
import json
import sys
from urllib.parse import quote

from . import publish, state, ticket
from .config import Target

NAME = "github"
MAX_ISSUES = 50
# Issue labels → ticket type (the first that matches; anything else is a feature).
TYPES_BY_LABEL = (("bug", "fix"), ("fix", "fix"), ("defect", "fix"), ("regression", "fix"), ("docs", "docs"),
                  ("documentation", "docs"), ("refactor", "refactor"), ("chore", "chore"), ("maintenance", "chore"),
                  ("dependencies", "chore"))
MAX_BODY = 20000


class IssuesError(Exception):
    pass


def ref(number: int) -> str:
    return f"GH-{number}"


def ticket_id(number: int) -> str:
    return ref(number).lower()


def type_of(labels: list[str]) -> str:
    names = [label.lower() for label in labels]
    for name, kind in TYPES_BY_LABEL:
        if any(name == n or n.startswith(f"{name}:") or n.endswith(f":{name}") for n in names):
            return kind
    return "feat"


def open_issues(target: Target) -> list[dict]:
    """The target's open issues (not PRs) labelled `issue_label`: {number, title, body, url, author, labels}."""
    if not target.issue_label:
        return []
    if not (target.github_repo and target.gh_user):
        raise IssuesError(f"{target.name}: issue_label needs github_repo and gh_user")
    out = publish.gh_as(target.gh_user, "api",
                        f"repos/{target.github_repo}/issues?state=open&per_page={MAX_ISSUES}&labels={quote(target.issue_label, safe='')}",
                        cwd=target.path)
    found = json.loads(out or "[]")
    return [{"number": i["number"], "title": i.get("title") or f"Issue #{i['number']}", "body": i.get("body") or "",
             "url": i.get("html_url") or f"https://github.com/{target.github_repo}/issues/{i['number']}",
             "author": (i.get("user") or {}).get("login") or "",
             "labels": [lb.get("name", "") for lb in i.get("labels") or [] if isinstance(lb, dict)]}
            for i in found if isinstance(i, dict) and "pull_request" not in i]


def _body(issue: dict) -> str:
    text = issue["body"].strip()[:MAX_BODY] or "(The issue has no description.)"
    return f"{text}\n\n---\nFrom GitHub issue #{issue['number']}" + (f" by @{issue['author']}" if issue["author"] else "") + \
        f": {issue['url']}\n"


def sync(target: Target, dry_run: bool = False) -> list[dict]:
    """Create a ticket for each labelled open issue that has none yet; returns {id, number, title, type, created}."""
    out = []
    for issue in open_issues(target):
        tid = ticket_id(issue["number"])
        if (state.run_dir(target, tid) / "ticket.json").exists():
            continue
        kind = type_of(issue["labels"])
        row = {"id": tid, "number": issue["number"], "title": issue["title"], "type": kind, "created": False}
        if not dry_run:
            ticket.create(target, issue["title"], kind, _body(issue), plan_checkpoint=False, risk="medium",
                          requester=issue["author"] or None, origin={"name": NAME, "ref": ref(issue["number"]), "url": issue["url"]})
            row["created"] = True
            if target.issue_run:
                from .dashboard import start_job  # the panel's jobs; dashboard imports the engine
                start_job(target, tid, "run", tid)
            print(f"{tid}: ticket from GitHub issue #{issue['number']}: {issue['title']}", file=sys.stderr, flush=True)
        out.append(row)
    return out


def closes(target: Target, t: dict) -> str | None:
    """`Closes #<n>` for a ticket made from an issue of the target's own repository (GitHub closes it on merge)."""
    origin = t.get("origin") or {}
    if origin.get("name") != NAME or not str(origin.get("ref") or "").startswith("GH-"):
        return None
    url = origin.get("url") or ""
    if target.github_repo and f"github.com/{target.github_repo}/issues/" not in url:
        return None
    return f"Closes #{origin['ref'][3:]}"
