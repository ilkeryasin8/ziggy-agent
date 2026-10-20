"""Ticket contract: ticket.json + ticket.md, branch naming (<type>/<slug>, -2/-3 on collision)."""
import getpass
import re
import subprocess
import unicodedata

from . import bmad, config, state
from .config import Target
from .worktree import branch_taken

TYPES = ("feat", "fix", "refactor", "chore", "docs", "project")
PIPELINES = {"feat": "feat", "fix": "fix", "refactor": "chore", "chore": "chore", "docs": "chore", "project": "project"}
# The pipeline a ticket picks instead of its type's: "standard" (the default) or "bmad" (BMad's build-auto plans and
# builds, in a target whose base branch has BMad installed; not for project tickets).
CHOICES = ("standard", bmad.PIPELINE)
# The pipeline a slice of a split ticket runs (implement → review; the plan is the parent's). Never chosen by hand.
SLICE = "slice"
SLUG_WORDS = 5
_TURKISH = str.maketrans("ıİşŞğĞçÇöÖüÜ", "iIsSgGcCoOuU")


_TYPE_PREFIX = re.compile(r"^(?P<type>[A-Za-z]+):\s*(?P<title>.+)$")


def split_type(line: str, default: str = "feat") -> tuple[str, str]:
    """'fix: login timeout' -> ('fix', 'login timeout'); no prefix -> (default, line). A one-word prefix that is not a known type is an error, not part of the title. Quotes around
    the whole line (a title pasted from a shell command) are dropped."""
    line = " ".join(line.split())
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'`":
        line = line[1:-1].strip()
    typed = _TYPE_PREFIX.match(line)
    if typed:
        if typed["type"].lower() not in TYPES:
            raise ValueError(f"bilinmeyen tip `{typed['type']}`; geçerli tipler: {', '.join(TYPES)}")
        return typed["type"].lower(), typed["title"].strip()
    return default, line


def pipeline_for(ticket_type: str, choice: str | None = None) -> str:
    """The pipeline (pipelines/<name>.toml) a ticket of this type runs; `choice` "bmad" picks BMad's."""
    return bmad.PIPELINE if choice == bmad.PIPELINE else PIPELINES[ticket_type]


def default_choice(target: Target, ticket_type: str = "feat") -> str:
    """The pipeline choice a new ticket gets when none is asked for: "bmad" for a code ticket of a controlled target
    whose base branch has BMad installed and whose main checkout has BMad's per-developer files, else "standard"."""
    if target.mode != "controlled" or ticket_type == "project":
        return "standard"
    try:
        found = bmad.status(target)
    except (OSError, subprocess.SubprocessError):
        return "standard"
    return bmad.PIPELINE if found["installed"] and not bmad.local_ready(target) else "standard"


def branch_prefix(ticket_type: str) -> str:
    """A project ticket writes docs/PROJECT.md, so its branch is docs/project-<slug>."""
    return "docs" if ticket_type == "project" else ticket_type


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title.translate(_TURKISH)).encode("ascii", "ignore").decode()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_title.lower())[:SLUG_WORDS])


def branch_name(target: Target, ticket_type: str, title: str, prefix: str = "") -> str:
    slug = slugify(title)
    if not slug:
        raise ValueError(f"title {title!r} has no letters or digits to name a branch with")
    if ticket_type == "project":
        slug = f"project-{slug}"
    if prefix:
        slug = f"{prefix}-{slug}"
    return free_branch(target, f"{branch_prefix(ticket_type)}/{slug}")


def free_branch(target: Target, base: str) -> str:
    """`base`, or `base-2`, `base-3`, … when a ticket or the repository already has that name."""
    reserved = {state.load_ticket(target, i)["branch"] for i in state.ticket_ids(target)}
    candidate, n = base, 1
    while candidate in reserved or branch_taken(target, candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def check_after(target: Target, ticket_id: str | None, after: list[str]) -> list[str]:
    """The dependency list, cleaned (order kept, duplicates dropped): every id must be a ticket of this target, and
    following `after` from them must never come back to `ticket_id` (None: a ticket not created yet). Raises
    ValueError naming a ticket of another target, an unknown one, or the cycle."""
    out = []
    for raw in after:
        dep = str(raw).strip().lower()
        if not dep or dep in out:
            continue
        if ":" in dep or "/" in dep:
            raise ValueError(f"{raw}: a dependency must be a ticket of {target.name} (a bare id such as local-3)")
        if not _ID.match(dep):
            raise ValueError(f"{raw} is not a ticket id")
        if not (state.run_dir(target, dep) / "ticket.json").is_file():
            elsewhere = [name for name in config.target_names() if name != target.name
                         and (config.state_home() / name / "runs" / dep / "ticket.json").is_file()]
            if elsewhere:
                raise ValueError(f"{dep} is a ticket of {', '.join(elsewhere)}, not {target.name}; "
                                 "a ticket can only wait for tickets of its own target")
            raise ValueError(f"no ticket {dep} in {target.name}")
        out.append(dep)
    if ticket_id is not None:
        for dep in out:
            path = _path_to(target, dep, ticket_id, [ticket_id])
            if path:
                raise ValueError(f"dependency cycle: {' → '.join(path)}")
    return out


def _path_to(target: Target, start: str, goal: str, trail: list[str]) -> list[str] | None:
    """trail + the chain of `after` links from start to goal, or None when goal is not reachable."""
    if start == goal:
        return [*trail, start]
    if start in trail:
        return None
    try:
        deps = state.load_ticket(target, start).get("after") or []
    except LookupError:
        return None
    for dep in deps:
        found = _path_to(target, dep, goal, [*trail, start])
        if found:
            return found
    return None


LANGUAGES = {"tr": "Turkish", "en": "English"}


def render_markdown(ticket: dict, body: str) -> str:
    checkpoint = "yes" if ticket["plan_checkpoint"] else "no"
    language = LANGUAGES.get(ticket.get("language") or "")
    language_line = (f"- Output language: {language}. Write the plan, notes and questions in {language}; "
                     "code, identifiers, file names and commit messages stay in English.\n") if language else ""
    pull = ticket.get("pull")
    if pull:
        language_line += (f"- Pull request: #{pull['number']} ({pull['url']}) by {pull['author']}, branch `{pull['head']}` into "
                          f"`{pull['base']}`. This worktree already holds the PR's commits; `git diff {pull['base_ref']}...HEAD` "
                          "is its change. The work is the newest review section of the plan (review findings to fix "
                          "on top of the PR): keep the PR's own change, and do not revert or redo it beyond what that entry asks.\n")
    origin = ticket.get("origin") or {}
    if origin.get("kind") == "deferred":
        source_line = (f"- Source: deferred review finding {origin['entry']} of ticket {origin['ticket']} "
                       "(its inbox/deferred.md). The ticket text below is that finding, as the user edited it.\n")
    elif origin.get("kind") == "roadmap":
        source_line = (f"- Source: roadmap item {origin['item']}" + (f" (phase {origin['phase']})" if origin.get("phase") else "")
                       + " of this repository's long-term plan. The ticket text below is that item, as the user edited it.\n")
    elif origin.get("kind") == "lead":
        source_line = (f"- Source: Ziggy's lead proposal {origin['proposal']} (drafted from the project's roadmap, CI, issues "
                       "and deferred findings). The ticket text below is that proposal, as the user accepted it.\n")
    elif origin:
        source_line = (f"- Source: {origin['name']} issue {origin['ref']}" + (f" ({origin['url']})" if origin.get("url") else "")
                       + ". The ticket text below is that issue's text.\n")
    else:
        source_line = ""
    if ticket.get("after"):
        source_line += f"- Starts after: {', '.join(ticket['after'])} (those tickets were done before this one ran)\n"
    if ticket.get("parent"):
        source_line += (f"- Slice: {ticket['slice']} of {ticket['parent']}. PLAN holds the parent's goal, acceptance criteria "
                        f"and decisions, and only this slice's tasks: implement {ticket['slice']} alone. The slices below it "
                        "are already in the base; the ones above follow in their own tickets, stacked on this branch.\n")
    return (
        f"# {ticket['id']} — {ticket['title']}\n\n"
        "## Pipeline\n\n"
        "Written by the pipeline wrapper; trusted.\n\n"
        f"- Type: {ticket['type']}\n"
        f"- Pipeline: {ticket.get('pipeline') or pipeline_for(ticket['type'])}\n"
        f"- Branch: {ticket['branch']} (already checked out in this worktree)\n"
        f"{base_line(ticket)}\n"
        f"- Plan checkpoint: {checkpoint}\n"
        f"{language_line}{source_line}\n"
        "## Ticket (untrusted)\n\n"
        f"{ticket['title']}\n\n"
        f"{body.strip()}\n\n"
        "## Decisions (user)\n\n"
        "None yet.\n"
    )


def base_line(ticket: dict) -> str:
    """The `- Base:` line of ticket.md: a stacked slice names the slice below it (its branch is local)."""
    if ticket.get("stack_base"):
        return f"- Base: {ticket['base_branch']} (local branch of {ticket['stack_base']}, the slice below; stacked)"
    return f"- Base: {ticket['base_branch']}"


def create(
    target: Target,
    title: str,
    ticket_type: str,
    body: str,
    plan_checkpoint: bool,
    risk: str,
    *,
    slack: dict | None = None,
    source_ref: str | None = None,
    requester: str | None = None,
    language: str | None = None,
    origin: dict | None = None,
    pull: dict | None = None,
    pipeline: str | None = None,
    after: list[str] | None = None,
    stack: dict | None = None,
) -> dict:
    """A local ticket (local-<n>), or with `slack` ({channel, thread_ts}) a Slack one (sl-<n>).
    `plan_checkpoint` stops the run at the approval gate after planning whatever the risk (always on in a
    controlled target, whoever creates the ticket). `pipeline`
    "bmad" runs BMad's pipeline (the target must have BMad installed and set up); None or "standard" the type's.
    `origin` is an issue ({name, ref, url}: the ticket takes the issue's id), a deferred finding
    ({kind: "deferred", ticket, entry}), a roadmap item ({kind: "roadmap", item, phase}: the item is linked and its
    status follows the ticket, roadmap.py) or a lead proposal ({kind: "lead", proposal}: lead.accepted links the
    proposal's roadmap items); an origin with a kind is a local ticket that records where it came from.
    `after`: tickets of the same target this one starts after (check_after). `stack` ({parent, slice, branch,
    base_branch, stack_base}): a slice of a split ticket (stack.split), which runs the slice pipeline."""
    if ticket_type not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if pipeline not in (None, *CHOICES) and not (stack and pipeline == SLICE):
        raise ValueError(f"pipeline must be one of {', '.join(CHOICES)}")
    if pipeline == bmad.PIPELINE:
        if ticket_type == "project":
            raise ValueError("a project ticket runs the project pipeline; the BMad pipeline is for code tickets")
        bmad.check(target)
    if language and language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    title = " ".join(title.split())
    if not title:
        raise ValueError("title is empty")
    issue = origin if origin and not origin.get("kind") else None
    with state.lock(target, "new", "ticket creation"):
        after = check_after(target, None, after or [])
        source = issue["name"] if issue else "github" if pull else "slack" if slack else "local"
        if pull:
            # An open pull request becomes pr-<n>, one ticket per PR, on its own base branch.
            ticket_id = f"pr-{pull['number']}"
            if (state.run_dir(target, ticket_id) / "ticket.json").exists():
                raise ValueError(f"PR #{pull['number']} is already ticket {ticket_id} in {target.name}")
            source_ref = pull["url"]
        elif issue:
            # An issue becomes the ticket of the same name (proj-12): one ticket per issue.
            ticket_id = issue["ref"].lower()
            if (state.run_dir(target, ticket_id) / "ticket.json").exists():
                raise ValueError(f"{issue['ref']} is already ticket {ticket_id} in {target.name}")
            source_ref = issue["ref"]
        else:
            ticket_id = state.next_id(target, "sl" if slack else "local")
        ticket = {
            "id": ticket_id,
            "source": source,
            "source_ref": source_ref,
            "title": title,
            "type": ticket_type,
            "pipeline": SLICE if stack else pipeline_for(ticket_type, pipeline),
            "branch": (branch_name(target, "pr", title, prefix=str(pull["number"])) if pull else
                       free_branch(target, stack["branch"]) if stack else
                       branch_name(target, ticket_type, title, prefix=ticket_id if issue else "")),
            "pull": pull,  # {"number", "url", "head", "base", "base_ref", "author", "fork"}: an open PR this ticket fixes
            "base_branch": pull["base"] if pull else stack["base_branch"] if stack else target.base_branch,
            "plan_checkpoint": plan_checkpoint or target.mode == "controlled",
            "risk": risk,
            "requester": requester or getpass.getuser(),
            "slack": slack,
            "language": language,
            "origin": origin,
            "after": after,  # ticket ids this one starts after (all done first)
            **({"parent": stack["parent"], "slice": stack["slice"], "stack_base": stack.get("stack_base")} if stack else {}),
            "created_at": state.now(),
        }
        root = state.run_dir(target, ticket_id)
        root.mkdir(parents=True)
        (root / "ticket.md").write_text(render_markdown(ticket, body), encoding="utf-8")
        state.write_json(root / "ticket.json", ticket)
        state.save_state(target, ticket_id, {
            "status": "new", "pipeline": ticket["pipeline"], "stage": None, "worktree": None, "prepared": False,
            "run": None, "next": None, "rounds": [], "gate": None, "risk": None, "size": None,
            "tasks": {"done": 0, "total": 0}, "checks": [], "user_reviews": [], "error": None,
        })
    if origin and origin.get("kind") == "roadmap":
        from . import roadmap  # roadmap reads tickets through state; it never imports this module
        roadmap.on_created(target, ticket)
    return ticket
