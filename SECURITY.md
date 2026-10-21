# Security

Ziggy runs code an AI agent wrote, on your machine, with your Claude Code login and your GitHub account. Its safety
model is described in [docs/reference.md](docs/reference.md#safety-model); this page says what it is meant to stop,
what it does not, and how to report a hole.

## Reporting a vulnerability

Please report privately through GitHub's **Security → Report a vulnerability** on this repository (a private security
advisory), not in a public issue. Include the Ziggy version (`ziggy --version`), the Claude Code version
(`claude --version`), macOS version, and the smallest ticket, repository or config that shows the problem. You will
get an answer within a week; fixes land on `main` and are listed in [CHANGELOG.md](CHANGELOG.md).

## What Ziggy is meant to stop

- An agent writing outside the ticket's git worktree, reaching the network from the commands it runs, pushing, or
  reading credential stores (`~/.ssh`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.gnupg`, keychains, browser profiles,
  `.env` files) — through Claude Code's sandbox and deny rules.
- Your test commands (engine checks) doing the same — they run under macOS `sandbox-exec` with no network but
  localhost and the same credential paths denied.
- Secret-looking environment variables reaching sessions, checks or the Codex review.
- A branch leaving the machine with changes to CI workflows, agent instruction files (`AGENTS.md`, `CLAUDE.md`,
  `.cursorrules`, …), editor/devcontainer/MCP/hook config or package install scripts, with secrets, or with invisible
  Unicode, without your explicit approval; PR text carrying images that exfiltrate on render.
- A read-only chat using a connector to write (MCP write verbs are denied; read chats run on an allowlist).
- Running on a Claude Code version with known sandbox bypasses (older than 2.1.90 is refused).

## What it does not stop

- A malicious ticket or repository persuading the agent to write harmful code **inside** the worktree. The draft PR
  and your review are the last line; Ziggy never merges.
- Anything Claude Code's own sandbox fails to enforce. `scripts/sandbox-smoke.sh` re-checks it after an upgrade.
- Linux: engine checks run under bubblewrap, which needs unprivileged user namespaces; the Claude Code sessions
  rely on Claude Code's own Linux sandbox. The Linux backend is tested in CI, not in daily use.
- What you approve. An approved guard finding is pushed as it is.

## Supported versions

Only the latest release on `main` gets fixes.
