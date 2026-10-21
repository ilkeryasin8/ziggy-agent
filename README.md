# Ziggy

**A personal agent that turns tickets into reviewed branches, by running headless Claude Code in a sandbox, stage by
stage, while you do something else.**

You write a ticket (in a web panel, the terminal, or from a Claude Code session). Ziggy runs it through a fixed
pipeline (plan → verify → implement ⇄ review), each stage a fresh, sandboxed `claude -p` session in a git worktree of
your repository. It asks when it needs you, stops at an approval gate when a plan is risky, runs your test commands
itself, and ends with a branch of local commits you review and publish as a draft PR. You approve and merge; Ziggy
never does.

![The Komuta board: what waits on you, what is ready for review, what is on its way](docs/images/home.png)

## Why

Coding agents are good at a well-scoped task and bad at knowing when to stop, ask, or prove their work. Ziggy keeps
the agent to one stage at a time and puts a deterministic engine around it:

- **The engine decides, not the agent.** Each stage writes a JSON result (`READY`, `NEEDS_USER`, `FAILED`, a route);
  `pipelines/*.toml` says where that goes. Visit caps stop loops; a missing result is a failure, never a pass.
- **Questions and an approval gate.** An agent that needs a decision asks a question round and waits. A plan rated
  risky or large stops for you to approve, change, or split into stacked slices (one draft PR each).
- **Proof, not claims.** After an implement stage says READY, Ziggy runs your checks itself under `sandbox-exec`; a
  failure goes back to the implementer, a broken environment stops the ticket and says what to fix.
- **Sandboxed by default.** No network, no push, no package installs, no MCP, no reading your home folders; writes only
  in the worktree. Draft PRs only.
- **It learns.** Answered questions and review lessons become project memory (markdown you approve) that later stages
  read; a morning "lead" proposes the next tickets from your roadmap, red CI and deferred findings.

## How it differs

Most coding agents put a model in charge of the loop, in the cloud or in your editor. Ziggy keeps the loop in a
small, deterministic engine on your machine and gives the model one bounded stage at a time:

- **The engine routes and verifies.** Stages report a result; TOML pipelines decide what happens next. Your test
  commands run in the engine, under an OS sandbox, and a failing check is tried on the base branch so the agent is
  never sent to fix what was already broken. A bug fix must bring a regression test that fails without it.
- **You stay the approver.** Questions with recommendations, an approval gate for risky plans, a split of large plans
  into stacked slices with one draft PR each, and a review page ranked by risk. Ziggy never merges, and nothing is
  pushed past a guard that stops CI files, agent instructions, secrets and hidden characters until you look.
- **Local and inspectable.** Your repository, your Claude Code login, plain files for state and memory. No server, no
  account, no telemetry.

It is not a cloud agent fleet, an IDE assistant or a general autonomous developer: it is a careful pipeline for
tickets you would hand to a colleague, sized for one person running a few repositories.

## What it looks like

| | |
|---|---|
| ![A question round on a ticket page](docs/images/ticket.png) | ![The review page with the branch's diff](docs/images/review.png) |
| A ticket asking a question round, with options and a recommendation | Reviewing the branch before the draft PR |
| ![Sohbet: a free chat about a project](docs/images/sohbet.png) | ![Projects](docs/images/projects.png) |
| Sohbet: talk to Claude about a project, outside any ticket | Projects, their run slots and cost |

The panel speaks English or Turkish (its author's language): it follows your browser and remembers the "EN · TR"
switch in its sidebar. The CLI, the code, the prompts and the docs are in English, and agents answer in the language
you pick per ticket.

## Features

- **Pipelines per ticket type** (`feat`, `fix`, `chore`, `project`, stacked `slice`s, optional BMad), each stage with its
  own model, prompt, persona, time limit and tool deny list.
- **Web panel** (`ziggy ui`, local only): a board of what waits on you, live agent activity, question rounds and the
  approval gate, a review page with the diff, PR drafting, cost and quota, project memory, roadmap.
- **Steering**: nudge a running stage with a message, stop it, resume a crashed session, retry a stage, or re-run only
  the checks after fixing an environment.
- **GitHub loop**: draft PRs, PR comments and failing CI brought back as a new round, merged PRs archived and their
  worktrees cleaned.
- **Sohbet**: free chats about a project, read-only or in a worktree of their own, with your Claude login's connectors
  (e.g. an issue tracker) available read-only.
- **MCP server** (`ziggy mcp`): drive Ziggy from any Claude Code or Claude Desktop session ("make this a ticket").
- **Guided onboarding** of a repository: detects how to install and test it (Python, Node, Go, Rust, Swift/Xcode),
  verifies that in a throwaway clone, and proposes its config.
- **Machine-aware**: waits for a Claude login's usage window to reset, and for memory pressure to ease, instead of
  failing.

## Architecture

```mermaid
flowchart LR
    you([You]) --> fronts["Panel · CLI · MCP · Claude Code skill"]
    fronts --> engine["Engine<br/>pipelines/*.toml"]
    engine -- "one stage" --> agent["claude -p<br/>sandboxed, in a git worktree"]
    agent -- "result.json" --> engine
    engine --> checks["Your checks<br/>under sandbox-exec"]
    engine --> pr["Draft PR<br/>(you merge)"]
```

[docs/architecture.md](docs/architecture.md) draws the whole system: a stage end to end, ticket states, routing, the
safety layers, Sohbet and MCP, and where state lives.

## Quick start

Requirements: **macOS** (the sandboxes and launchd), **Python 3.12+** (standard library only), **Claude Code** logged
in, **git**, and **gh** logged in to the account that opens PRs. Optional: `codex` for a second review, `uv` for the
tests.

```bash
pipx install git+https://github.com/ilkeryasin8/ziggy-agent.git
ziggy doctor                                # what this machine still needs, each with the fix
ziggy target add ~/src/your-repo --verify   # look at the repo, try its install and tests, write its config
ziggy ui --install                          # the panel at http://127.0.0.1:8787, kept running by launchd
ziggy new "feat: add a dark mode toggle" --run
```

From a clone, `bin/ziggy` runs the same commands without installing anything.

Or open the panel and use "Mevcut repoyu ekle" (add an existing repository) and the command bar.

Ziggy's own state lives in `~/.local/state/ziggy/` and its config in `~/.config/ziggy/config.toml`
([config.example.toml](config.example.toml) documents every key). Nothing is committed to your repository except the
work a ticket asks for.

A minimal config is one target:

```toml
[targets.shop]
path = "~/src/shop"

[[targets.shop.checks]]
name = "tests"
run = "python3 -m pytest -q"
```

## What it costs

Every stage is a Claude Code session on your own login. On a subscription it counts against your usage limits (Ziggy
waits for a limit to reset instead of failing, and shows each login's windows); with an API key it is billed. In the
author's use the default models (`fable` plans, `opus` builds) have come to roughly $5–10 of usage per merged PR; a
small bug fix on `sonnet` took $0.57 in `ziggy eval`. These are a handful of tickets, not a benchmark: measure your own
with `ziggy stats` and `ziggy eval`. `plan_model`, `build_model` and `budget_usd` (a per-session cap) are set per
target.

## Safety at a glance

- Sessions run in Claude Code's sandbox: writes only in the ticket's worktree, no network for the commands the agent
  runs (Claude Code itself still reaches its API), no push, no MCP servers, no hooks, your global settings not loaded;
  credential stores and `.env` files are denied to them, and secret-looking environment variables are dropped.
- Your checks run under `sandbox-exec` with no network but localhost and no access to `~/.ssh`, `~/.aws` and the like.
- Before anything leaves the machine, a guard stops branches touching CI workflows, agent instruction files, editor or
  devcontainer config, package install scripts, or carrying secrets or invisible Unicode, until you approve them; PR
  text is cleaned of images that could exfiltrate on render.
- Draft PRs only; Ziggy never merges. Claude Code older than 2.1.90 is refused.

Details and the limits of each layer: [docs/reference.md](docs/reference.md#safety-model) and
[SECURITY.md](SECURITY.md).

## Documentation

- [docs/architecture.md](docs/architecture.md): the picture, with diagrams.
- [docs/reference.md](docs/reference.md): every command and feature.
- [docs/how-it-works.md](docs/how-it-works.md): the rules behind each part, in depth.
- [ziggy/pipelines/README.md](ziggy/pipelines/README.md): the pipeline file format and the prompts.
- `ziggy eval`: 15 tasks with hidden tests to measure a change to the engine, a prompt or a model (reference: "Measuring changes").

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md); changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Status

A personal tool in daily use by its author on macOS, open-sourced as is. Expect sharp edges outside that setup:
Linux runs the checks under bubblewrap and is tested in CI, not in daily use. Issues and ideas are welcome.

## Credits

The role personas and review lenses under `ziggy/prompts/roles/` and `ziggy/prompts/lenses/` are adapted from
[BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) (MIT). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

[MIT](LICENSE)
