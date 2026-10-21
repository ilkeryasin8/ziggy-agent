# Changelog

All notable changes. Ziggy follows [semantic versioning](https://semver.org) from 0.1.0 on.

## Unreleased

- `[targets.<name>.screens]`: screenshots of the app's pages at desktop and phone sizes after the checks.
- `[slack] forward`: answer, approve, retry and steer any ticket from its Slack thread.
- Engine checks on Linux, under bubblewrap.
- `codex_stages`: chosen stages run with Codex (`codex exec`) instead of Claude Code.
- `best_of`: a risky ticket's first implement runs twice and the better attempt stays.
- Fix: after a check was tried on the base commit, the index's stat data is refreshed (a later `reset --keep` refused).
- GitHub issues labelled `issue_label` become tickets; their PRs close them on merge.
- Spending caps per target: `ticket_budget_usd` and `daily_budget_usd`, checked before every stage.
- Panel jobs write their exit code atomically, so a finished job is never read as failed while the file is written.

## 0.1.0 — 2026-10-21

The first public version.

### Safety
- A publish guard stops branches touching CI workflows, agent instruction files, editor/devcontainer/MCP/hook config
  or package install scripts, or carrying secrets or invisible Unicode, until approved; PR text is cleaned of images
  and media tags.
- Secret-looking environment variables are dropped for sessions, checks and the Codex review; credential stores and
  `.env` files are denied to sessions and to the checks' sandbox.
- Read-only chats run on an allowlist of the login's MCP read tools; Claude Code older than 2.1.90 is refused.

### Engine
- A failing check is tried on the base commit and marked pre-existing when it fails there too; a `fix` must report a
  regression test that passes on the branch and fails without the fix.
- Review findings below the evidence bar are held as unverified; dependency changes get their own review step;
  `codex_review` starts the Codex second review for risky or large tickets.
- `answer_after` answers a waiting question round with its recommendations; resumed sessions are reminded of the
  plan's acceptance criteria and open tasks; project memory learns from how tickets ended and suggests pruning.

### Review and notifications
- Per-file risk scores and a risk-ordered review page; the board puts the riskier of equally urgent tickets first.
- Notification tiers with digests; a weekly report.

### Packaging and tooling
- `pipx install`, `ziggy --version`, `ziggy doctor`; everything the package reads ships inside it.
- `ziggy eval`: 15 tasks with hidden tests to measure changes to the engine, prompts or models.
