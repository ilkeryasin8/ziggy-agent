"""What leaves the machine when Ziggy pushes: checked before every push, cleaned before every PR text.

An agent's output is untrusted (a ticket, a PR comment or a file it read may have steered it), and a push is the one
step that hands it to the outside: GitHub runs the branch's CI with the repository's secrets, renders its PR text, and
other people's agents read its instruction files. So before a push (`publish`, the adopted-PR push, the restack push)
`findings` looks at the branch against its base and at the PR title and body:

    sensitive  a changed file that runs in CI or steers agents: workflows and other CI configs, AGENTS.md / CLAUDE.md
               anywhere, editor-agent rules, devcontainers, MCP configs, .claude/, git hooks, and lifecycle scripts
               (preinstall, postinstall, prepare, …) added or changed in a package.json
    secret     a token or key (GitHub, Slack, AWS, Anthropic, OpenAI, Google, Stripe, npm, private keys) in an added
               line, a commit message or the PR text; the match is shown redacted
    unicode    invisible or direction-changing characters (zero-width, bidi controls, Unicode tags) in the same places

A push with findings is refused until you approve exactly those findings (`approve`, stored as their fingerprints in
the ticket's state); a later push with nothing new goes through, a new finding asks again. `clean_text` is applied to
every PR title and body Ziggy sends, approved or not: invisible characters are dropped, and images and embedded media
become plain links, so viewing the PR fetches nothing an attacker chose (the Camo image proxy fetches any image URL).
"""
import fnmatch
import hashlib
import json
import re
from pathlib import Path

from . import state
from .worktree import git

MARKER = "publish needs approval:"  # how the panel recognises the refusal it can turn into a question
SENSITIVE = (
    ".github/workflows/*", ".github/actions/*", ".gitlab-ci.yml", ".gitlab/*", ".circleci/*", "Jenkinsfile",
    ".buildkite/*", "azure-pipelines.yml", "bitbucket-pipelines.yml", ".travis.yml",
    "AGENTS.md", "*/AGENTS.md", "CLAUDE.md", "*/CLAUDE.md", ".cursorrules", "*/.cursorrules", ".cursor/*",
    ".github/copilot-instructions.md", ".github/instructions/*", ".windsurfrules", ".clinerules", ".clinerules/*",
    ".devcontainer/*", ".mcp.json", "*/.mcp.json", ".claude/*", "*/.claude/*", ".husky/*", ".githooks/*",
    ".pre-commit-config.yaml", ".envrc", ".vscode/tasks.json",
)
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack")
SECRETS = (
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-\d-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
_MEDIA_TAG = re.compile(r"<(img|picture|source|video|audio|iframe|object|embed|svg)\b", re.I)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_FINDINGS = 50


def _redact(match: str) -> str:
    return match[:6] + "…" if len(match) > 8 else "…"


def _scan_text(text: str, where: str) -> list[dict]:
    out = [{"kind": "secret", "where": where, "detail": f"{name}: {_redact(m.group(0))}"}
           for name, pattern in SECRETS for m in pattern.finditer(text)]
    if INVISIBLE.search(text):
        codes = sorted({f"U+{ord(c):04X}" for c in INVISIBLE.findall(text)})
        out.append({"kind": "unicode", "where": where, "detail": "invisible characters " + ", ".join(codes[:6])})
    return out


def sensitive(path: str) -> bool:
    """The path runs in CI or steers agents (SENSITIVE)."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in SENSITIVE)


def _scripts(wt: Path, ref: str, path: str) -> dict:
    proc = git("show", f"{ref}:{path}", cwd=wt, check=False)
    try:
        scripts = json.loads(proc.stdout).get("scripts") if proc.returncode == 0 else {}
    except (ValueError, AttributeError):
        return {}
    return scripts if isinstance(scripts, dict) else {}


def findings(wt: Path, base: str, head: str = "HEAD", title: str = "", body: str = "") -> list[dict]:
    """Everything on the branch (base..head) and in the PR text that needs your eyes before a push."""
    out: list[dict] = []
    changed = [p for p in git("diff", "--name-only", f"{base}...{head}", cwd=wt).stdout.splitlines() if p]
    for path in changed:
        if sensitive(path):
            out.append({"kind": "sensitive", "where": path, "detail": "runs in CI or instructs agents"})
        elif path == "package.json" or path.endswith("/package.json"):
            before, after = _scripts(wt, base, path), _scripts(wt, head, path)
            touched = [k for k in LIFECYCLE if after.get(k) != before.get(k)]
            if touched:
                out.append({"kind": "sensitive", "where": path, "detail": "install-time scripts changed: " + ", ".join(touched)})
    current, line_no = None, 0
    for line in git("diff", "-U0", "--no-color", f"{base}...{head}", cwd=wt).stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif (hunk := _HUNK.match(line)):
            line_no = int(hunk.group(1))
        elif line.startswith("+") and current:
            out += _scan_text(line[1:], f"{current}:{line_no}")
            line_no += 1
    messages = git("log", "--format=%H%x00%B%x01", f"{base}..{head}", cwd=wt).stdout
    for entry in filter(str.strip, messages.split("\x01")):
        sha, _, text = entry.strip().partition("\x00")
        out += _scan_text(text, f"commit {sha[:10]}")
    out += _scan_text(title, "PR title") + _scan_text(body, "PR body")
    return out[:MAX_FINDINGS]


def fingerprint(finding: dict) -> str:
    """A finding without its line number (a rebase moves lines; the same file and match stay approved)."""
    where = re.sub(r":\d+$", "", finding["where"])
    return hashlib.sha256(f"{finding['kind']}|{where}|{finding['detail']}".encode()).hexdigest()[:16]


def unapproved(st: dict, found: list[dict]) -> list[dict]:
    approved = set(st.get("guard_approved") or [])
    return [f for f in found if fingerprint(f) not in approved]


def approve(target, ticket_id: str, found: list[dict], by: str) -> None:
    st = state.load_state(target, ticket_id)
    st["guard_approved"] = sorted(set(st.get("guard_approved") or []) | {fingerprint(f) for f in found})
    st.setdefault("guard_log", []).append({"at": state.now(), "by": by, "findings": found[:MAX_FINDINGS]})
    state.save_state(target, ticket_id, st)


def message(found: list[dict]) -> str:
    lines = [f"- {f['kind']}: {f['where']} ({f['detail']})" for f in found[:15]]
    more = f"\n- … and {len(found) - 15} more" if len(found) > 15 else ""
    return (f"{MARKER} the branch or its PR text has {len(found)} finding(s) to look at before it leaves this machine:\n"
            + "\n".join(lines) + more + "\nReview them (the review page shows the diff), then publish again approving them.")


def clean_text(text: str) -> str:
    """PR text as Ziggy sends it: no invisible characters, and images or embedded media turned into plain links."""
    text = INVISIBLE.sub("", text)
    text = text.replace("![", "[image: ")  # a markdown image becomes a link: nothing is fetched until clicked
    return _MEDIA_TAG.sub(lambda m: "&lt;" + m.group(1), text)
