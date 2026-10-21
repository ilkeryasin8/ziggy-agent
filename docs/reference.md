# Ziggy reference

Everything Ziggy does, command by command and feature by feature. Start with the [README](../README.md) for what it is and [architecture.md](architecture.md) for the picture.

## How a ticket flows

```
ticket ──▶ worktree (fresh fetch of the base branch, `prepare` once)
       ──▶ stages: each a sandboxed `claude -p` session that writes a JSON result
              │  the result's verdict and route pick the next stage (pipelines/<name>.toml)
              │  NEEDS_USER ─▶ question round (you answer) ─▶ continues
              │  risky or large plan ─▶ approval gate (you approve, change or split)
              │  implement READY ─▶ engine checks (the target's test commands) ─▶ review
              ▼
       done: branch with local commits ──▶ review page ──▶ `publish`: push + draft PR
                                             ──▶ PR feedback (comments, failing CI) can start another round
```

A ticket's type picks its pipeline:

| Type | Pipeline | Stages | Approval gate |
|---|---|---|---|
| `feat` (default) | `feat` | plan → verify → implement ⇄ review | after plan or verify, when risk ≥ medium or size ≥ large |
| `fix` | `fix` | reproduce → implement ⇄ review | after reproduce, when risk is high |
| `refactor`, `chore`, `docs` | `chore` | plan (short) → implement ⇄ review | after plan, when risk is high |
| `project` | `project` | write `docs/PROJECT.md` ⇄ doc review | none |
| any but `project`, with `--pipeline bmad` | `bmad` | BMad plan → BMad build ⇄ review | after BMad plan, when risk is high or size large |
| a slice of a split ticket | `slice` | implement ⇄ review (the plan is the parent's) | none (approved at the parent's gate) |

Each stage has its own model (the target's `plan_model` or `build_model`), persona, prompt, time limit, tool deny list
and optional `effort`. Plan, verify, reproduce and review stages must not change the repository; one that does fails and
its changes are stashed. Every stage has a visit cap per run (`max_visits`, implement 3); a loop past it fails the run
instead of spending more. The plan lives in the pipeline's state (`runs/<id>/inbox/plan.md`), not in the repository.

A stage reports through a result file (`$PIPELINE_RESULT_FILE`): a `verdict` (`READY`, `NEEDS_USER` or `FAILED`), a
summary, questions, an optional route and stage-specific fields (risk and size, findings, the reviewer's guide,
evidence). A stage that leaves no valid result fails; a missing result is never a silent pass. Fields and validation:
`ziggy/results.py` and [ziggy/pipelines/README.md](../ziggy/pipelines/README.md).

## Quick start

### Requirements

| Needed | For |
|---|---|
| macOS | the Claude Code sandbox, `sandbox-exec` for engine checks, launchd for the panel, notifications |
| Python 3.12+ | the CLI (standard library only); installed with `pipx`, or `bin/ziggy` from a checkout |
| `claude` (Claude Code CLI, logged in) | every stage, chat, retro, roadmap draft and onboarding session (`ZIGGY_CLAUDE` overrides the binary) |
| `git` | worktrees, branches |
| `gh` (logged in to the account that opens PRs) | `publish`, PR sync and feedback, `project new`, open PRs |
| `uv` | running the tests; `listen` re-runs itself through it to get `slack_sdk` |
| `codex` (optional) | the second review (`review`) |
| `terminal-notifier` (optional) | notifications; `osascript` otherwise |

### Install

```bash
pipx install git+https://github.com/ilkeryasin8/ziggy-agent.git   # or, from a clone: pipx install .
ziggy doctor                  # Python, git, Claude Code (2.1.90+), gh login, sandbox-exec, config, state; exits 1 on a problem
ziggy target add ~/src/your-repo --verify   # writes ~/.config/ziggy/config.toml (or copy config.example.toml there)
ziggy ui --install            # the panel on http://127.0.0.1:8787/, kept running by launchd
```

A clone runs without installing: `bin/ziggy` (link it onto your `PATH` if you like). `ziggy --version` prints the
version; the launchd service and the panel's background jobs start the same Ziggy that installed them (`bin/ziggy` in
a clone, `python -m ziggy` when installed). `pip install ".[slack]"` adds `slack_sdk` for `ziggy listen`; without it,
listen re-runs itself under `uv` with it.

The config path is `~/.config/ziggy/config.toml` (`$XDG_CONFIG_HOME` honoured), or `ZIGGY_CONFIG`.
Never commit the real file; it names your repositories.

An install from before the rename (`~/.config/bmad-pipeline`, `~/.local/state/bmad-pipeline`, launchd
`local.bmad-pipeline.ui`) moves over once with `ziggy migrate` (`--dry-run` prints the plan and changes nothing). It
refuses while a run, job or chat is active or the old panel answers; it moves both directories, rewrites the old state
path inside the state's JSON files and the config, repairs every moved worktree with `git worktree repair`, renames the
Claude Code project directories of those worktrees (so `resume` and chat history keep working), swaps the launchd
service if the old one was installed, and records `migrated.json` in the new state directory. A second run does nothing.

### Add a target

A target is a repository the pipeline works on. Either use the panel (Projects → "Add an existing repo"), or:

```bash
ziggy target add ~/src/example-repo            # propose the table, ask, append it to the config
ziggy target add ~/src/example-repo --verify   # try prepare and the checks in a throwaway clone first
ziggy target add ~/src/example-repo --agent    # let a read-only agent fill the gaps (paid, ≤ $1), then verify
ziggy target remove example-repo               # take its tables out of the config (repo, tickets and memory stay)
ziggy target leftovers                         # what removed targets left in the state dir (mostly worktrees)
ziggy target purge example-repo                # remove those worktrees; branches, tickets and memory stay
```

It reads the repository and proposes the `[targets.<name>]` table: base branch, GitHub repository, gh account and Claude
login (from the config's `[onboard]` rules), the `mode` (`controlled` for a company repository, the folders those rules
give their own login or gh account; `personal` elsewhere; `--mode` or the form's "mod" select changes it), `prepare`
commands by lockfile, test `env`, and `checks` from package scripts, CI workflows and test config, each line with the
file it came from. It also says what the repository lacks (an `AGENTS.md`/`CLAUDE.md`, `docs/PROJECT.md`, ignore rules
for folders `prepare` creates, …).

On the panel: **Inspect** fills the form and, when it has prepare or check lines, verifies them at once (free);
**Verify** verifies the edited lines again; **Fill in with agent** first runs the agent. Verification clones the checkout
under the state directory, runs the prepare lines like a run would, runs each check exactly like the engine, and deletes
the clone; each line shows `tried · N s`, `failed: <reason>` or `not tried`. The agent session reads only (Read,
Grep, Glob; credential files unreadable), costs at most $1, and when the repository has no `AGENTS.md` it drafts one,
which **Open draft PR** opens as a draft PR. Nothing is saved until **Add to config**; the table is appended to the end of
the config with a backup kept. Details: [how-it-works.md](how-it-works.md), "Adding a target".

A new repository from nothing: `ziggy project new <name> --description "…"` (or "New project from scratch" on the
Projects page) creates it with README, `AGENTS.md`, `.gitignore` and a `docs/PROJECT.md` stub, makes one first commit
on `main`, creates the GitHub repository (private unless `--public`), pushes, and adds the target.

### First ticket

```bash
cd ~/src/example-repo                  # the target is the repository you stand in
ziggy new "fix: login times out after 10 s" --run
ziggy status local-1           # stage bar, tasks, risk/size, open questions, what to type next
ziggy watch local-1 --brief    # follow it live
ziggy answer local-1 --text "Keep the Windows path out of scope."   # when it asks
ziggy approve local-1          # when it stops at the approval gate
ziggy publish local-1          # when it is done: push and open a draft PR
```

Or use the panel's composer on the home page: project, title, type, text, "Ask for plan approval", output language,
"Start after", then "Create" or "Create and start".

The target is the configured one whose checkout (or ticket worktree) contains the current directory; elsewhere
`default_target`; `--target <name>` (before the command) overrides both. `new` without a title opens `$VISUAL`/`$EDITOR`
on a template (first line `[<type>:] <title>`, then the body). The branch is `<type>/<slug of the title>`
(`docs/project-<slug>` for a `project` ticket), with `-2`, `-3` when taken.

Exit codes: 0 done (or only recorded, or waiting for other tickets, for a usage limit to reset or for the machine's
memory), 3 waiting for you (questions or the approval gate), 1 the run failed, 2 an error before or around a run
(refused command, bad config, git or GitHub failure).

## The panel

`ziggy ui` serves the panel on `http://127.0.0.1:8787/` for every configured target (`--port`, `--open`;
`--install`/`--uninstall` for launchd, logs in `~/.local/state/ziggy/ui.{out,err}.log`). The panel speaks English or
Turkish: it follows the browser's language and remembers the "EN · TR" switch at the bottom of the sidebar
(`web/i18n.js`; the code's texts are Turkish keys with English in `web/i18n/*.en.js`, and `tests/test_i18n.py` fails
on a text left untranslated). Texts the server builds go through the same dictionaries, with patterns for their
variable parts; a failed run's time report ("timeout (45 min): …") is assembled from too many pieces and may keep
Turkish words in English. The quick guide below names the Turkish labels. It runs the same commands as the terminal: buttons start `ziggy <command>` as detached jobs
(`runs/<id>/jobs/`), so a run outlives a panel restart.

The panel is built to stay open: a sidebar with the pages, their counts, "Yeni ticket" (N, or ⌘K for the command bar),
the inbox ("Inbox"), the run slots per target, the quota of each Claude login ("Quota": `5 sa %18 · 7 gün %11`,
orange from 80 %, red with the reset time while the limit is reached) and the health line. The look ("Volt") keeps its
lime accent for what waits on you; running work is blue and moves, finished work goes quiet. v1 is dark only and laid
out for the desktop.

| Page | What it is for |
|---|---|
| "Command" | the command bar (new ticket: project, type, line, body, plan gate, language, "Start after"), today's and the week's cost, "Today" (what finished or merged yesterday, what waits on you by kind, the lead's proposals, each Claude login's 5-hour and weekly quota), "N waiting on you" (the board), "Up next" (the lead's proposals per project, right below what waits on you), today's events from the inbox, the last finished tickets |
| "Ticket'lar" | every ticket, status tabs, search and project filter, "Temizlik" (worktrees of merged or closed PRs) |
| "Projects" | the targets as cards (open tickets, cost, sessions, run slots), "Mevcut repoyu ekle", "New project from scratch"; a project's page: its roadmap ("Roadmap", see Roadmap), `docs/PROJECT.md` on the base branch, "Suggest type", "Open PRs", its tickets |
| "Sessions" | the other Claude Code sessions on this machine (desktop, terminal, VS Code, headless): open or recently ended, the resume command, the transcript |
| "Stats" | cost today, 7 and 30 days, total; "Teslim" (merged PRs per project over 30 days: how many, the share merged without a change request after the PR opened, change rounds per PR, quota per PR); daily cost by stage; cost per project; per-stage cost and averages; models; outcomes; most expensive tickets; "Weekly report" (the latest weekly report) |
| "Memory" | the project memory per target: proposed entries and roadmap changes to approve, edit or reject, the rules, decisions, pitfalls and episodes with their use counts, "Forget it?" on faded ones (see Project memory) |
| ticket page | `#ticket/<target>/<id>`: the ticket's head and stage bar, "Your move" when it waits on you, the tabs, the chat beside them |

The board on "Command" has five groups, each card with its next step first (j/k move, Enter takes the step); the first
card waiting on you is drawn lime. Cards and ticket rows carry the plan's risk when it is not low and "large" for a
large change; among equally urgent items the riskier ticket comes first (`attention.triage_weight`), and "Ready for
review" lists the riskiest done ticket first:

| Group | Holds |
|---|---|
| "Seni bekliyor" | a question round, a plan at the approval gate, a failed or lost run, a panel job that failed, new PR feedback, a stuck dependency, a slice to restack |
| "Ready for review" | a done ticket without a PR |
| "Live" | running agents as live lanes (stage bar, clock, cost, the last activity lines), including quiet ones |
| "Queued" | tickets waiting for others ("Bekliyor: local-3 bitince"), tickets waiting for a usage limit to reset ("Quota full · resumes at 15:05") or for the machine's memory ("Machine is short on memory · waiting"), split tickets with their slices |
| "Teslimde" | open PRs, a branch ahead of its PR, a closed PR whose worktree still holds work |

Failures, lost runs, job errors, PR feedback and reminders can be closed with "Kapat" (that occurrence only);
questions, approvals and quiet sessions need an action. A ticket's page has the tabs "Plan", "Sorular",
"Changes" (the review page: diff, findings, comments, PR feedback, guide, evidence, PR draft), "Review" (Codex
reviews), "Log" ("Etkinlik" live activity, "Stages" every stage session with prompt, result, transcript, checks
log, cost, and "Job'lar") and "Ticket metni"; the chat ("Talk to the agent") sits beside them. Esc goes back. Old
`#review/<target>/<id>` and `#needs` links still open the right place.

The panel listens on 127.0.0.1 only, answers only to a loopback `Host`, takes only same-origin JSON POSTs and sends a
strict Content-Security-Policy. Nothing on it marks a PR ready, approves or merges.

### Driving Ziggy from another Claude session (MCP)

`ziggy mcp [--read-only] [--port 8787]` is a Model Context Protocol server on stdio. It talks to the running panel
(start it first: `ziggy ui`), so a tool does what the panel's button does. Read tools: `ziggy_status`,
`ziggy_list_tickets`, `ziggy_get_ticket`. Write tools (`ziggy_create_ticket`, `ziggy_run`, `ziggy_answer`,
`ziggy_approve`, `ziggy_nudge`, `ziggy_stop`, `ziggy_publish`) need `confirm: true` in the call, which the model sets
after you agreed; `--read-only` leaves them out. Nothing merges.

```sh
claude mcp add --scope user ziggy -- "$(command -v ziggy)" mcp     # or /path/to/clone/bin/ziggy
claude mcp add --scope user ziggy-ro -- "$(command -v ziggy)" mcp --read-only
# a second Claude account:
CLAUDE_CONFIG_DIR=~/.claude-work claude mcp add --scope user ziggy -- "$(command -v ziggy)" mcp
```

## CLI reference

`ziggy [--target NAME] [--no-wait] <command>`; `--no-wait` fails instead of waiting in line when every run slot
of the target is busy. `<command> --help` lists each command's flags.

**Tickets**

| Command | What |
|---|---|
| `new [title]` | create a ticket (`--type`, `--body-file`, `--run`, `--plan-checkpoint`, `--lang tr\|en`, `--pipeline standard\|bmad` (default `bmad` in a controlled target with BMad ready, else `standard`), `--after ID`, `--source`, `--source-url`, `--risk`) |
| `run <id>` (alias `retry`) | run the pipeline; on a failed ticket, retry the stage it failed in (`--full-review`: every review of this run reads the whole branch; `--now`: start even while the target's Claude login is over its usage limit or the machine is short of memory, with a warning) |
| `resume <id>` | continue a session that was killed, crashed or timed out (`claude --resume`) |
| `recheck <id>` | a failed ticket whose READY visit failed an engine check (its environment fixed since): run only the checks again on the branch as it is, no agent, then continue |
| `answer <id>` | answer the open questions, or change course after a stop (`--text`/`--file`, `--no-run`) |
| `approve <id>` | continue past the approval gate (`--no-run`) |
| `split <id>` | at the gate: one stacked ticket and draft PR per slice of the plan (`--no-run`) |
| `after <id> [ID …]` | set the tickets this one starts after; no ids clears them |
| `request-changes <id>` | send the review page's draft comments back as a new run (`--github ITEM_ID …`: picked PR feedback instead; `--no-run`) |
| `fix <id>` | fix the Codex findings you kept, in a new run (`--review`, `--text`/`--file`, `--no-run`) |
| `route "<request>"` | propose a ticket type, title and reason with one tool-less call (`--create`, `--lang`) |

**Following and steering**

| Command | What |
|---|---|
| `status [id]` | list tickets (after one `# quota` line per Claude login and a `# host` line for the machine's memory), or show one |
| `watch <id>` | follow the run as readable lines (`--brief`, `--all`) |
| `log <id>` | one line per stage session (`--message`: the last final message) |
| `open <id>` | print the worktree path (`--plan`: the plan file) |
| `stop <id>` | end the running session (or leave the line for a run slot) |
| `nudge <id>` | steer the running stage with a message (`--text`/`--file`/stdin) |
| `chat <id> --text "…"` | ask about a ticket in a read-only session (`--new`, `--model`) |
| `chat-stop <id>` | stop the reply being written |
| `talk new\|send\|list\|stop\|delete` | Chat: free chats about the target, read-only or in a worktree of their own (`new --write`; `send <id> --text`, `--model`; `delete <id> --force`) |
| `inbox` | what needs you across every target (`--all`, `--seen`, `--json`) |
| `stats` | cost (today, 7 and 30 days, per project) and delivery: merged PRs over 30 days, share without change requests, change rounds and quota per PR (`--json`) |
| `report` | the weekly report as Markdown, up to yesterday (`--save`: also to `<state>/reports/`) |
| `issues sync` | labelled GitHub issues → tickets `gh-<n>` (`--dry-run`; see GitHub issues) |
| `eval list\|validate\|run\|show\|compare` | the fixed tasks with hidden tests (see Measuring changes) |
| `sessions` | other Claude Code sessions on this machine (`--all`, `--json`) |

**Shipping and GitHub**

| Command | What |
|---|---|
| `publish <id>` | push a done ticket's branch and open a draft PR (`--title`, `--body-file`, `--foreign-ok`) |
| `restack <id>` | rebase a slice whose PR below was squash-merged (`--push`: force-with-lease it to its PR) |
| `review <id>` | second review of the branch by Codex (`--post REVIEW_ID`: add it to the draft PR as a comment) |
| `prs` | the target's open PRs and the ticket following each |
| `adopt <number>` | make an open PR a ticket `pr-<n>` (`--review`: start a Codex review) |
| `sync` | one PR-sync pass: archive and clean merged or closed tickets, read new PR feedback |
| `cleanup [id]` | no id: remove worktrees of tickets whose PR is merged or closed; with an id: one finished ticket's worktree (`--force`, `--yes`) |

**Memory, targets, projects, services**

| Command | What |
|---|---|
| `retro <id>` | look back at a ticket now and propose memory entries (rules, decisions, pitfalls) |
| `protected <id> [list\|apply <n>\|reject <n>]` | the ticket's patches of protected files (`AGENTS.md`, `.claude/settings.json`): list them with their diffs, apply one on the ticket branch as a commit, or reject it (`--reason`) |
| `memory [list\|candidates\|approve\|reject\|forget] [id]` | the project memory and its candidates (`--target`; with approve: `--text` your wording, `--title`, `--replace` to supersede similar entries) |
| `roadmap [show\|draft\|link <item> <ticket>]` | the target's roadmap (`--target`): show it with each item's status, draft one (read-only session; the panel's "Draft a roadmap" job), or link a ticket to an item (`R-3`, or a BMad story key `1-2`) |
| `lead [run\|list\|start\|reject] [id]` | the morning lead: `run` (the default) runs today's when its time came (`--now`: at once) and lists the proposals; `start p<n>` creates and runs the ticket (`--lang`; `--no-run` only creates it, the only way in a controlled target); `reject p<n>` (`--reason`); every target unless `--target` |
| `target add <path>` | propose and append a target table (`--name`, `--mode controlled\|personal`, `--yes`, `--verify`, `--agent`) |
| `target suggest <path>` | the panel's onboarding job (agent and verification of a request); not for direct use |
| `project new <name>` | new repository on GitHub, added as a target (`--dir`, `--description`, `--owner`, `--public`, `--gh-user`, `--claude-config-dir`, `--yes`) |
| `source map <name> <project>` | the target a tracker project maps to (`[sources.<name>.projects]`) |
| `ui` | the web panel (`--port`, `--install`, `--uninstall`, `--open`) |
| `listen` | serve the Slack channel (Socket Mode) |
| `migrate` | one-time move of an install from before the rename (bmad-pipeline) to Ziggy's paths (`--dry-run`) |

`new --risk` is only recorded in `ticket.json`; the approval gate uses the risk and size the stages report.

## Features

### Questions and the approval gate

A `NEEDS_USER` result opens a question round: the ticket waits as `needs-user` with questions, options and a recommended
answer. In `feat`, the plan's questions go to the verify stage first, which settles what the code can answer. `answer`
closes the round, appends it to `runs/<id>/answers.md` and the plan's `## Decisions (user)`, and continues at the stage
the pipeline names. After `max_rounds` (5) rounds the run fails. On the panel, "Ask for help" on a question round opens
the chat about it.

A target's `answer_after` (a duration, e.g. `"4h"`; unset by default) lets a round that waited that long go on with
its recommendations: the panel's minute tick answers it (`by` "ziggy", `via` "timeout", the text says so) and starts
the run, and the question card says when ("If no answer comes, it continues at 15:05 with the suggested answers"). Only when every
question has a recommendation; never for an intent gap or a last visit's settling, and never in controlled mode
(the key is ignored there with a warning).

The approval gate stops a run as `awaiting-approval` after a gated stage when the highest reported risk or size reaches
the pipeline's threshold, or always with `--plan-checkpoint` ("Ask for plan approval"). Risk and size are never lowered once
reported. The approval card has "Onayla → devam" (`approve`), "Request changes" (`answer`: your text becomes a decision
and goes back to the stage that takes answers), "Read the plan", and "Split into slices" when the plan can be split (the approve
button then reads "Onayla → tek PR").

### Review rounds and dispositions

Review follows `prompts/_review-policy.md`. Each finding has a class (`important`, `nit`, `pre-existing`) and a
disposition: a real problem the change introduced, in scope and cheap to fix, is `fix` whatever its class; out of scope
or costly is `defer` (to `inbox/deferred.md`); `dismiss` only for a finding that is wrong, with evidence. The engine
enforces the route: a `READY` review with an open `fix` finding goes back to implement; on the last round, open
non-important findings are deferred and the review converges. A candidate becomes a finding only after a verification
pass confirmed it; low-confidence ones are listed as unverified. The engine holds the bar too: an open `fix` finding
without `evidence`, whose location and evidence name no file, or with a confidence under 0.6 is moved to unverified
(`results.hold_unproven`) and never sent back; a review whose every fix was held converges. A branch that changed
manifests or lockfiles (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Podfile`, … and their locks) gets a
dependencies step in the review prompt: each added or re-versioned dependency is needed, the real package from the usual
registry, pinned like the others, and its lockfile changed with it. Round 2 and later read only what changed since the last
reviewed HEAD plus the previous open findings (`--full-review` reads the whole branch). On its last allowed visit the
review may put the open important findings to you instead of failing at the cap; your answer grants one more implement
and review visit. Dismissed findings stay visible ("Review N bulguyu eledi") and are listed in the PR draft. Details:
[docs/how-it-works.md](how-it-works.md), "Review rounds".

### Engine checks

After implement reports `READY`, the engine runs the target's `[[targets.<name>.checks]]` (`name`, `run`, `timeout`,
default 10 minutes) in the worktree, in order, stopping at the first failure that is not pre-existing (below). A failure sends the work back to implement
with the output in `checks.log`. Checks run code the agent wrote outside Claude's sandbox, so each runs under macOS
`sandbox-exec` with a generated profile: no network but localhost, writes only in the worktree, the temp directories,
`/dev` and the target's state directory. Without `sandbox-exec` a check fails; it never runs unsandboxed. Untracked
files a check leaves behind are deleted and named as a warning; changed tracked files fail it. Prompts get the check
commands and their latest results, so implement runs only targeted tests and review does not rerun the suite.

A check that fails on a branch that changed something is run once more on the run's base commit (`checks.at_base`: the tracked files as the base has
them for the duration, the index untouched, the branch's files back afterwards). Failing there too, it is
**pre-existing**: marked in the result and on the panel (amber, "(pre-existing)"), not sent back, and the next checks still
run; the base's status is remembered per run (`run.baselines`). Passing there, the change broke it and it goes back.

A `fix` proves itself (`regression = true` on its implement stage): after the checks pass, the `regressionTest`
(`path`, `run`) the implementer reports must pass on the branch and fail on the code from before the implement with
only that test file taken from the branch. A missing one, or one that passes without the fix (it does not reproduce
the bug), goes back to implement like a failed check.

`[targets.<name>.screens]` adds screenshots once an implement's checks pass: `pages` (paths, required), `url`
(default `http://127.0.0.1:{port}`), `start` (the app's server command, run in the background), `wait` (60 s for the
app to answer), `sizes` (default `[[1440, 900], [390, 844]]`) and `browser`. Inside the checks' sandbox the engine
starts the app, waits for `url`, and takes each page at each size with a headless Chromium; the images are the
implement's evidence (`inbox/artifacts/screens/`, on the review page) and the step is a check named `screens`: a page
that cannot be taken goes back to implement. The browser is `browser`, `$ZIGGY_BROWSER`, Playwright's
`chrome-headless-shell` (`npx playwright install chromium-headless-shell`) or a Chromium on the `PATH`; Google Chrome
itself does not start inside the macOS sandbox. No browser at all stops the run as an environment problem.

On Linux the checks run under bubblewrap (`bwrap`, `checks.bwrap_argv`) instead of `sandbox-exec`: the filesystem
read-only except the worktree, the target's state directory and `/tmp`; a network namespace of its own (its own
loopback: unlike on macOS, a database listening on the host's `localhost` is out of reach); the credential directories
under an empty tmpfs and the credential files replaced by an unreadable one. The `check_sandbox` presets are macOS
rules and do nothing there. bwrap needs unprivileged user namespaces (on Ubuntu 24.04:
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, or an AppArmor profile for it); a check that cannot
start them fails as an environment problem, not a code one. `ziggy doctor` names the backend it found.

`best_of` (`"off"` by default, `"risky"`: the plan rated the ticket high risk or large, or `"always"`) gives the first
implement of a fresh branch a second attempt: once the first attempt's checks have run, the branch goes back to where
it started and a new session does the same step; its checks run too, and the better attempt stays (fewer failing
checks that send work back, then READY, then the smaller change). The other is saved as
`attempts/best-of-<stage dir>.patch` and the choice is in `run.best_of`. It is one visit; it doubles that stage's
cost. Never for a resumed session or a branch already published or stacked on.

Toolchains that need more than that profile opt in per target with named presets, `check_sandbox = ["xcode"]` (an
unknown name is a config error); targets without one get the profile unchanged. `xcode` adds exactly two rules: writes
to SwiftPM's shared cache (`~/Library/Caches/org.swift.swiftpm`, resolved at run time) and outbound connections to the
simulator's test-manager socket under `/private/var/tmp`. Onboarding proposes it for a repository with an `*.xcodeproj`
or a `Package.swift`, with `swift test --disable-sandbox --package-path <pkg> --scratch-path .build/<pkg>` per local
package with tests and `xcodebuild test -project … -scheme … -destination 'platform=iOS Simulator,name=<newest
iPhone>' -derivedDataPath .build/DerivedData -IDEPackageSupportDisableManifestSandbox=YES
-IDEPackageSupportDisablePluginExecutionSandbox=YES 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' -quiet` (timeout
30m); a nested sandbox (SwiftPM's, the compiler's macro plugin server's) cannot start inside the checks' sandbox, hence
the flags. `.build/` should be git-ignored, or each check's build output is removed.

A check that fails in its environment rather than in the code (today: a tool refused a sandbox inside the check's,
`sandbox_apply: Operation not permitted`) fails the ticket at once with that line and the fix, instead of sending the
code back to the implementer. Once the command or preset is fixed, "Re-run only the checks" (`recheck <id>`)
runs only the checks on the branch as it is, with the earlier READY result carried over (no session, no cost), and
the pipeline continues from there; a code failure then goes back to the implementer as usual.

### Protected files

`claude-settings.json` denies `Edit` of `AGENTS.md` and `.claude/settings.json` (the list is read from those deny
rules) so a poisoned ticket cannot leave lasting instructions for later agents. When a ticket needs one changed, the
plan gives a "prepare a patch" task and implement saves a unified diff under the inbox artifacts and lists it in
`protectedPatches`. The engine checks it (the file exists, its headers name only protected files, `git apply --check`
passes on the branch HEAD), stores it as `runs/<id>/protected/<n>.patch` and goes on: the ticket does not fail (an
implement that still returned `FAILED` only for that, with such a patch among its artifacts, is read as `READY` with a
warning). A newer patch of the same file supersedes the older pending one, so one card per file waits. The review
sees the pending patches and does not send that change back. The inbox shows "Protected file
change: AGENTS.md" with the diff: **Apply** checks the patch again on the current HEAD, applies it and commits
`docs(<id>): apply approved change to AGENTS.md` on the ticket branch (refused while a stage runs, on a dirty worktree
or when it no longer applies); a published ticket then shows "Update PR". A patch prepared without an earlier one
you applied ("replaces earlier #1") replaces it: its own result is committed with "(supersedes #1)", unless
someone else changed the file since. **Reddet** records the refusal with an
optional reason. Publishing never waits for it; the PR form says one is pending. CLI: `ziggy protected <id>` (with
each patch's status).

### Review page, PR draft and publish

"Review changes" shows the branch's diff (all changes, since one of your reviews or a pipeline review round, or
one commit), with the pipeline's findings on their lines (class, severity, confidence, verified, disposition,
implement's outcome). Each file has a risk score (`changes.file_risk`: its open findings by severity, +6 for a file
that runs in CI or steers agents, +4 for a dependency manifest or lockfile, up to +4 for the size; a test or
generated file counts a quarter), shown as "risk N" / "riskli · N" with its reasons; "Risk order" lists the files
by it instead of the tree ("Tree"), remembered per browser. "Seen" ticks stay per file content. Press **+** on a line for a draft comment, or answer a finding with "Fix" / "Dismiss";
"Select open important" and "Fix selected" send the last round's open important findings back. "Request changes"
(`request-changes`) sends the drafts as a `## Review (user, round N)` section of the plan and starts a new run.
"Reviewer rehberi" is the latest review's per-file guide; "Evidence" lists the screenshots, recordings and logs the stages
saved under `inbox/artifacts/`.

"Onayla → Draft PR" opens the PR draft (title and description composed from the plan, the guide and the evidence,
both editable); "Push and open draft PR" runs `publish --title … --body-file -`. `publish` refuses a protected or base
branch and a title, body or commit with a `Co-Authored-By` line, pushes one branch, opens the PR as a draft, and reuses
an open PR for the branch.

### Codex as a stage agent

`codex_stages` (a list of stage ids, e.g. `["review"]`) runs those stages with `codex exec` instead of Claude Code:
the same prompt on stdin and the same result file, in Codex's own sandbox (`--sandbox workspace-write`: writes in the
worktree and, through `--add-dir`, the ticket's inbox; no network), your Codex config and rules left out
(`--ignore-user-config`), the environment scrubbed but for `OPENAI_API_KEY`/`CODEX_API_KEY`, and `codex_model` as
its model when set. Stop, nudge and the stage's timeout work as for Claude; a Codex stage is never resumed (each
attempt is a fresh session) and reports tokens (`codex_usage`), not cost. The engine judges it as any stage: a
read-only stage that changes the repository fails, its checks and routes are the same.

### GitHub issues

With `issue_label` (and `github_repo`, `gh_user`), the target's open GitHub issues carrying that label become tickets
`gh-<n>`, once each, on the panel's PR sync (every 3 minutes) or with `ziggy issues sync` (`--dry-run` lists them).
The type comes from the labels (`bug`/`fix`/`regression` → fix, `docs`/`documentation` → docs, `refactor`, `chore`/
`maintenance`/`dependencies` → chore, else feat); the body is the issue's, with a line linking back. `issue_run = true`
starts each new one at once. Their PR bodies end with `Closes #<n>`, so GitHub closes the issue when the PR merges;
Ziggy writes nothing on the issue itself.

### Measuring changes: `ziggy eval`

`ziggy eval` runs Ziggy on 15 small fixed tasks (`ziggy/evals/tasks/`: Python bug fixes and features, each with a seed
repository, visible tests, **hidden** tests the agents never see, and a reference solution) so a change to the engine,
a prompt or a model is measured rather than guessed. `eval run [ids…] [--label X] [--plan-model M] [--build-model M]`
gives each task a throwaway repository with a local origin and its own config and state (yours are not read or
written), runs the ticket with real Claude Code sessions (it costs quota) as `run --now` (the machine-memory and
usage-limit holds do not park it), answers question rounds with their recommendations and passes the approval gate, then runs the hidden tests on the branch under the checks' sandbox.
Results (pass, status, cost, time, sessions, implement visits, review rounds, interventions) go to
`~/.local/state/ziggy/evals/<run>/results.json`; `eval show [run]` prints them, `eval compare A B` puts two runs side
by side (`latest`, a name, a prefix or a path). `eval validate` checks the tasks themselves: visible tests pass on the
seed, hidden tests fail there and pass with the solution. `eval list` lists them.

### Codex review and open PRs

`review <id>` (**Codex review**) runs `codex review --base <remote>/<base>` in the worktree, in Codex's read-only
sandbox. "PR'a yorum ekle" posts a review to the draft PR; "Have the findings fixed" → "Send and run" (`fix`) turns the
findings you kept into a `## Review (codex, round N)` section and a new run. The project page lists the repository's
open PRs (`prs`); any PR not opened by the pipeline can be adopted (`adopt <n>`) as ticket `pr-<n>` on a local branch at
the PR's head, reviewed and fixed, then pushed back to the PR's own branch with "Push to PR": fast-forward only, never
to a fork, and to someone else's PR only after a confirm (`publish --foreign-ok`).

`codex_review` (per target) starts the Codex review on its own when a run ends done: `"off"` (default), `"risky"` (the
plan rated the ticket high risk or large) or `"always"`. It runs as a background job like the panel's button and needs
`codex` on the `PATH`; the findings wait on the review page.

### GitHub feedback loop

Every PR sync (the panel every 3 minutes, or `sync`) reads each ticket's open PR as the target's `gh_user`, never from an
agent: line comments, conversation comments, reviews with a body and failing checks (an Actions check with the last 200
lines of its job log up to the last error). Bot comments are skipped unless listed in `pr_feedback_bots`. The inbox shows
"PR'da N yeni yorum" / "CI is red on the PR". On the review page, "GitHub geri bildirimi" lists the items ("Refresh now"
reads now); "Fix selected" (`request-changes <id> --github <item id> …`) adds a `## Review (github, round N)`
section, the text quoted as untrusted data, and starts a run. When it ends done the branch is pushed so the PR updates.
With `pr_replies = true` (off by default) the pipeline also replies on GitHub ("Fixed in <sha>: …").

### Project memory

Each target has a project memory: plain markdown files the wrapper reads into every stage prompt. Where it lives:
the target's `memory_dir` if set; else, with `[memory] vault` configured, the entry of the vault's `projects.json`
(`projects_map`, default `<vault>/.claude/scripts/projects.json`) whose `roots[].prefix` is exactly the target path and
whose `vault_dir` exists, giving `<vault>/<vault_dir>/memory`. Without one, the nearest parent entry that names one
project (no `{leaf}` in it, e.g. `~/Developer/acme/field` → a folder for a frontend and a backend repo) gives each repo
its own `<vault>/<vault_dir>/memory/<repo folder>`, so repos sharing a project folder never mix. A catch-all parent
(`{leaf}`) is never used; then it is `~/.local/state/ziggy/<target>/memory` and "Memory" says why
("… has no mapping of its own for this repo (projects.json)").

The folder holds `context.md` (free-form standing notes), `rules.md` (kural), `decisions.md` (karar), `pitfalls.md`
(tuzak) and `episodes.md` (olay). Each entry is a `## M-12 · tuzak · <title>` heading with a few `- Field: value`
lines (the main field, `Ne zaman geçerli:` file globs and topics, `Kaynak:` ticket, stage, date and approval, an
optional `Durum:`). The markdown is the source of truth: edit it by hand, it is re-read every time; Ziggy only appends
entries (and lines to an episode) and changes one entry's `Durum:` line.

Candidates come from three places: every answered question of a question round becomes a decision candidate; when a run ends done
with new evidence of what went wrong (your sent review comments, dismissed findings you sent to fix anyway, findings
several rounds repeated, answers against the recommendation, your notes, PR feedback, checks that were fixed, recovered
runs, intent gaps), a background `retro` session proposes rules, decisions and pitfalls: one read-only session with
the target's `retro_model` (default `sonnet`), at most $1 and 10 minutes, no edit, shell or subagent tools; and the
migration below. Candidates wait on "Memory" and in the inbox ("N new memory proposals"), with similar existing entries
shown. "Onayla" appends the entry, "Edit and approve" with your wording (optionally replacing the similar ones, which
get `Durum: yerini aldı M-<new>`), "Reject" drops it. Nothing but episodes enters memory without your approval;
`retro = false` turns the automatic pass off. An episode (ticket, outcome, cost, touched paths, PR, later its merge)
is appended when a ticket is done.

Recall: every stage prompt gets `## Proje hafızası`, within `memory_budget` bytes (default 4096): part of `context.md`,
every active rule, then the decisions and pitfalls whose scope matches the files in play (the branch's changes, or
the plan's expected files) or whose topics appear in the ticket, and for plan-type stages up to 3 episodes. Agents
list the ids they relied on in `memoryUsed` (review findings may cite one in `memory`); the counts go to
`<target>/memory/usage.json` and the ticket page shows what each session saw and used. Decisions, pitfalls and
episodes unused for 60 days are marked faded ("soluk") with "Forget it?"; nothing is removed automatically
("Unut" / `memory forget` sets `Durum: unutuldu`). Each entry also learns how the tickets that used it ended
(`usage.json` `outcomes`): "clean" when the PR merged with no change asked, "rework" once a person or GitHub asked for
changes. An entry most of whose tickets (at least 3, two thirds) needed changes and none merged clean, or a decision,
pitfall or episode unused for 120 days, is suggested for pruning ("budanabilir" on the panel; `ziggy memory prune`
lists them, `--apply` forgets them).

Migration runs once per target: the old per-target `context.md` in the config directory minus its learned-rules section becomes the memory
`context.md`, each learned rule a `rules.md` entry, pending and rejected old rule candidates memory candidates; the old
files stay in place, unread.

### Roadmap

A project's long-term plan, shown on its page as "Roadmap": phases with a progress bar (merged and done items
over all), each item with its status, linked ticket and PR. Where it comes from:

- **BMad** (the main checkout has `_bmad-output/planning-artifacts/epics.md`): phases are its `## Epic N: …` sections
  (`E1`, `E2`…), items its `### Story N.M: …` (id = the story key `1-2`, detail = the user-story lines), statuses from
  `_bmad-output/implementation-artifacts/sprint-status.yaml`'s `development_status` (backlog and ready-for-dev → to do,
  in-progress, review, done; an unknown value counts as to do, with a warning). Ziggy never writes these files; a
  linked ticket's progress shows over the sprint status. When a `roadmap.md` exists too, BMad wins and the page says so.
- **Memory**: `roadmap.md` in the project memory folder (the vault folder mapped to the repo, or Ziggy's state), with
  the memory files' frontmatter: `## P1 · title` phases (optional `- Durum: aktif|bitti`), `- [ ] R-3 · title` items
  (`- [x]` done), an indented `- Ticket: local-7 · PR: <url> · Durum: pr` line Ziggy keeps, and indented notes (the
  item's detail). Edits in Obsidian win; what it cannot read is a warning (project page, "Memory", inbox).

Progress is automatic, no approval: a ticket made from an item (`origin = {kind: roadmap, item}` in its ticket.json, or
`ziggy roadmap link`) moves it to ticket → in-progress → review (done) → pr → merged (PR merged: `[x]`); only that
item's line and checkbox change, a status never goes back, a phase whose items are all merged or done becomes `bitti`
and the next unfinished phase `aktif`. Scope changes (add an item to a phase, remove or move one, add a phase) are
memory candidates of kind `yol`: they wait on "Memory" with the others, and "Onayla" makes the edit in `roadmap.md`.

"Open ticket" on an item that has not started fills the command bar (title, body = the item, its phase, detail and
`Kaynak: yol haritası R-3`) with the item as origin; the ticket page then shows "Roadmap: P2 · R-3". A project
without a roadmap gets "Draft a roadmap": one read-only session (the target's `roadmap_model`, default `sonnet`, at
most $1, no edit, shell, subagent or web tools, cwd the main checkout) reads the README, `docs/PROJECT.md`, the specs
and the memory's context and decisions, and proposes phases and items. The draft opens in an editor; nothing is
written until "Taslak olarak kaydet". A BMad project without epics gets a note to create them with BMad instead.

### Dependencies and stacked slices

`new --after local-3` (repeatable; "Start after" in the composer) makes a ticket wait (status `waiting`) until those
tickets of the same target are done, then start on its own (in a controlled target it asks first, see Controlled
mode); `after <id> …` changes the list before it runs. Cycles are refused. A dependency that failed, was archived
undone or had its PR closed unmerged leaves an inbox item ("Bekliyor ama …").

At the approval gate, a plan with two or more `### S<n>:` slices rated size `large` or risk `high` can be split
(`split`, "Split into slices"): each slice becomes a ticket on branch `<parent branch>-s<n>`, stacked on the slice below,
running the `slice` pipeline; the first starts at once, each next one when the one below is done. Publishing a slice
publishes the ones below first; each PR's base is the branch below and its body ends with a `## Stack` section. When a
slice's PR merges, the sync moves the next PR's base. After a squash or rebase merge the next slice needs a rebase:
`restack <id>` ("Rebase"), then `restack <id> --push` (force-with-lease of the ticket's own branch: the only
forced push the pipeline makes).

### Intent gaps

When implement or review finds that the change answers a different intent than the ticket (or rests on a plan
assumption the code proved wrong), it returns `NEEDS_USER` with `intentGap` instead of patching around it. The engine
saves what the run built as `runs/<id>/attempts/attempt-<n>.patch`, resets the ticket branch to before the first
implement (never a published branch, nor a slice another slice is built on), and opens a question round; the panel
shows "Intent gap". Your answers go into the plan and the run continues at the pipeline's `[intent_gap] then` stage.

### Deferred findings to tickets

Each deferred finding in the ticket page's Plan tab has "Make a ticket": it fills the composer (type `fix` for a bug
finding, else `chore`; the finding as title and text) and nothing runs until you create it. The new ticket records
where it came from, and the finding then links to it instead of offering a second ticket.

### BMad pipeline per ticket

In a target whose base branch tracks BMad Method (`.claude/skills/bmad-build-auto/SKILL.md` and `_bmad/`), a ticket
may opt into the `bmad` pipeline: `new --pipeline bmad`, or "Hat: BMad" in the composer (remembered per target). BMad's
`bmad-build-auto` plans to a spec under `_bmad-output/implementation-artifacts/` and builds from it; engine checks,
review, the review page, chat and the draft PR stay the same. The engine commits `_bmad-output/` on the ticket branch.
BMad's six gitignored per-developer files must exist in the main checkout (run BMad's setup there); each worktree gets
copies, and creating a BMad ticket is refused without them. The panel marks each project "BMad kurulu (<version>)",
"BMad kurulu · yerel dosya eksik" or "No BMad".

### Controlled mode

A target's `mode` is `personal` (the default) or `controlled`, for repositories where nothing should move without you
(a company's). A controlled target:

- defaults new tickets to the `bmad` pipeline when BMad is installed and ready there (`new` without `--pipeline`, and
  the composer's "Hat" until you pick one for the target), else the standard one; still switchable per ticket;
- stops every ticket at the approval gate after planning, whatever its risk: `ticket.create` sets `plan_checkpoint` on
  every ticket (terminal, panel, Slack, sources, deferred findings, split slices, adopted PRs) and the engine gates
  tickets made before the switch too. A slice runs the `slice` pipeline, which has no gate: its plan was approved at
  the parent's;
- never starts a dependant on its own: when the last ticket it waits for is done, the inbox asks
  ("local-3 done; start local-4?") with "Start" (`run`); the same holds for the next slice of a stack;
- never answers on GitHub: `pr_replies = true` is ignored, with a load warning (`uyarı: …` in the terminal, a warning
  chip on the panel's health line).

The panel marks its project and tickets "Controlled". The lead's proposals stay proposals in a controlled target: only
"Open as ticket" (the composer, prefilled), never "Start" (see The lead).

### The lead (Up next)

Every morning the lead looks at each project and proposes what to work on next (`ziggy/lead.py`):

- **Signals** (no LLM, free), each with a key that stays the same while the thing it names does: red CI on the base
  branch (the last Actions runs there, read as the target's `gh_user`), the roadmap's next items (the current phase's
  first items not started yet, see Roadmap), open issues assigned to `gh_user` or labelled `lead_issue_label`, and
  deferred review findings no ticket was made from. Memory entries, faded ones included, are context, never work.
- **Proposals**: one cheap read-only session (`lead_model`, default `sonnet`, at most $0.50, no edit, shell or subagent
  tools, in the main checkout) turns the signals, the project memory and the open tickets into 0–5 ticket drafts (title,
  type, text, why, the signal keys it came from, size). A key that a pending or rejected proposal holds, or that an
  open ticket made from a proposal works on, is not proposed again; a pending proposal expires after 7 days. When the
  login is blocked or its 5-hour window is at 80 % or more (or the session gives no answer), the top signals become
  drafts as they are, marked "agentless".
- **Schedule**: `[lead] at` (local time, default `08:30`) and `enabled`: the panel's 60 s tick starts each project's lead
  once a day at or after that time; a machine that slept through it runs it on the first tick after waking
  (`~/.local/state/ziggy/lead/last_run.json`). "Yenile" and `ziggy lead run --now` run it at once.
- **Actions** on "Command" → "Up next", each proposal with its why and its sources: in a personal project
  "Start" creates the ticket (`origin = {kind: lead, proposal}`) and runs it, "Edit" opens the composer prefilled; in
  a controlled project only "Open as ticket" (the composer; you create it). "Reddet" takes an optional reason. A
  ticket made from a proposal is linked to the proposal's roadmap items, which then move with it (ticket → in-progress
  → review → pr → merged). The inbox shows "N new work proposals" per project.

Proposals live in `~/.local/state/ziggy/<target>/lead/proposals.json`, sessions in `<target>/lead/NN-lead/` (stage
`lead` in the stats). `ziggy lead [run|list|start|reject] [id] [--target NAME] [--now] [--reason …]` does the same
from the terminal.

### Usage limits (quota)

Every session (stage, chat, retro, roadmap draft, onboarding) reports the Claude subscription's limits in its stream
(`rate_limit_event`: status `allowed`, `allowed_warning` or `rejected`, the 5-hour and weekly windows' utilization and
reset times). Ziggy keeps the latest per Claude login (the target's resolved `CLAUDE_CONFIG_DIR`, else `default`;
labelled with the login's email) in `~/.local/state/ziggy/quota.json`. The sidebar's "Quota" and `status` show it.

When a stage's session hits the limit (its last event is `rejected`, or it ended on a usage-limit error such as
"You've hit your session limit · resets 8pm (Europe/Istanbul)"), the stage does not fail: the login is marked blocked
until the window resets, the ticket waits with status `quota` ("Quota full · resumes at 15:05") and, a minute after the
reset (30 minutes when no reset time is known), the panel's tick resumes the same session with a short note on why it
stopped. The error text is kept in the session's `run.json` (`quota_error`). The inbox shows "Usage limit reached: <login> ·
HH:MM" once per login until the reset. No stage starts on a blocked login: the ticket waits the same way ("Quota full ·
starts at HH:MM") while other logins' tickets run on. A held ticket's "Start now" (`run --now`) starts it anyway,
after a warning. The hold lives in the ticket's state, so a panel restart picks it up on its first tick.

### A busy machine (host)

A Mac that swaps hard turns a `cat` into minutes and a stage into a timeout. Before every stage Ziggy samples the
machine (`sysctl` memory pressure and `vm.swapusage`, the load average; cached a few seconds): while the pressure is
critical, or it is warn with swap used at `[host] swap_hold` (0.9) of its total or more, the stage does not start and the ticket
waits with status `host` ("Machine is short on memory · waiting"). The panel's tick starts it once the machine has
been calm for two ticks in a row (one ticket per tick, the longest held first); "Start now" (`run --now`) starts it
anyway after a warning. Running stages are never stopped. The health line shows a chip ("Bellek: kritik · swap %96")
while the pressure is warn or critical (a full swap alone is normal on macOS and shows nothing); `status` prints a `# host` line; every stage's `run.json`
keeps `host` samples (start, end, the worst seen). `[host] enabled = false` turns the hold off.

### Steering, retries and stalled runs

`nudge <id>` ("Steer") interrupts a live stage and resumes its own session with your message; with no session live
the message goes into the next stage's prompt. A stage's `[stages.retry]` policy resumes lost, timed-out and quiet
sessions on its own (at most 2 per run, with backoff); "quiet" means no output for `quiet_after` seconds while no tool
is running (a tool's heartbeats count as output). One tool call running past the target's `stall_after` (10 m) is an
inbox item, "A command has been running for 12 min: grep -ril … · memory critical", with "Steer" and "Durdur"; it goes away when
the call returns or the stage ends, and nothing is stopped for it. A stage that ends by its timeout, by going quiet or
by losing its process without a result says where its time went, first in the failure's text: "zaman aşımı (45 dk):
sürenin 34 dk’sı (%76) en yavaş 3 komutu beklerken geçti (grep -ril … 25 dk, …); bellek baskısı kritikti" (its
`run.json` keeps `time_report` and the slowest `tool_calls`). A ticket whose process is gone gets a "lost" card.
`resume` ("Resume where it left off") continues a killed, crashed or timed-out session; a failed card also offers
"Re-run the stage" and "Change direction". A writing stage that resumes (after a retry, a quota wait, a nudge or
`resume`) gets PLAN's `## Goal & acceptance` and its open tasks again ahead of the stage's instruction
(`plan.reminder`), so a long session does not lose the bar for done with its context.

### Ticket chat

"Chat" (`chat <id> --text …`) answers questions about one ticket, one headless turn per message in the ticket's
worktree (else the main checkout), with a system prompt rebuilt from the ticket's state. Follow-ups continue the
conversation; "Yeni sohbet" (`--new`) starts over. Models come from `[chat] models` (the first is the default; `sonnet`, `opus`, `fable`, `haiku` without one), each
reply stops after `[chat] timeout` (30 min) or with "Durdur" (`chat-stop`). It is read-only: editing tools and
branch-moving git commands are denied, files a turn changed in the worktree are stashed, and in the main checkout it gets
no Bash.

### Chat (free chat)

The panel's Chat page (`talk …` on the command line) is a conversation with Claude about a project, not tied to a
ticket: each message is one headless turn that resumes the chat's session, like typing into a Claude Code session.
A chat is fixed to one mode when it starts:

- **okuyan** (read): the main checkout, with no editing tools and no Bash.
- **yazan** (write): a worktree of its own on `chat/<id>` from `<remote>/<base>`, prepared like a ticket's; it edits,
  runs commands and commits there. Branch-moving git commands are denied and the pre-push hook refuses a push.

Unlike the stages, a chat runs with MCP on, so the Claude login's claude.ai connectors (YouTrack, …) and the
repository's MCP servers are there: "list the open issues assigned to me" works in a project whose login has YouTrack.
"Make a ticket" puts the chat's title and turns in the composer; "Sil" removes the chat, and for a write chat its
worktree and branch (asking again when the branch still holds commits or changes). Chats share `[chat]`'s models,
timeout and the four live-reply slots with ticket chats.

```bash
ziggy talk new [--write]                       # prints the chat id
ziggy talk send <id> --text "…" [--model sonnet]
ziggy talk list | stop <id> | delete <id> [--force]
```

### Sources

- **Terminal and panel**: tickets `local-<n>`.
- **Claude Code skill**: `skills/ziggy/SKILL.md`; link it with
  `ln -s "$PWD/skills/ziggy" ~/.claude/skills/ziggy`. `/ziggy <what you want>` has Claude write
  the ticket (shown to you first), run it, relay progress, bring questions and the gate to you, and publish only after
  you agree. The work still happens in the pipeline's sessions.
- **YouTrack**, through the Claude session's YouTrack connector: the skill can make a ticket from an issue with
  `new "<type>: <title>" --source youtrack:PROJ-12 --source-url <url>` (ticket `proj-12`, target from
  `[sources.youtrack.projects]`), and comment on or move the issue (`[sources.youtrack.states]`) after asking. The
  pipeline itself never talks to YouTrack and holds no tracker credentials.
- **GitHub PRs**: `adopt` (above).
- **Slack** (optional): `listen` serves one private channel over Socket Mode. `bmad: [!plan] [<type>:] <title>` makes
  ticket `sl-<n>` and runs it; thread replies `approve`, `retry`, `nudge: <message>`, or anything else as the answer to
  open questions. Only the configured member's messages act. Tokens come from `[slack] app_token_cmd`/`bot_token_cmd`
  at start and stay in memory. The Slack app manifest is below. With `[slack] forward = true` the listener also
  follows the target's other tickets (panel, CLI, issues): every 30 s it posts their new questions (with options and
  the recommendation), plan approvals, failures, lost runs and protected-file patches to the channel, one thread per
  ticket (`slack-threads.json`), and the same replies (`approve`, `retry`, `nudge:`, an answer) act on them. Such a
  ticket is not published from Slack when it is done; its draft PR stays a click on the panel (or `publish`).

### Inbox and notifications

Every needs-you item is one row in a ledger shared by all targets (`~/.local/state/ziggy/attention.json`); the
engine adds rows as it saves state, the panel reconciles every 60 s. "Gelen kutusu" and `inbox` list them; opening a
ticket marks its items read. A new row is a macOS notification (sent by the wrapper, never from inside a session), at
most one per `[notifications] min_interval` (30 s), the rest summed up; `[notifications] enabled` and `events` choose
what is sent, `language` (`en` or `tr`; the system locale's by default) the language notifications and the weekly report are
written in. `digest_every` (off by default) splits them in two tiers: the `urgent` kinds (question, approval, failed,
lost, error, quota, protected) still notify at once, the rest are collected and shown as one "Digest · N items" notification
naming the first three, once that long has passed since the first (at most 50 kept, the rest counted).

The weekly report (`ziggy/weekly.py`) covers the seven days before a Monday: tickets done, PRs merged and runs failed,
cost and sessions, the delivery numbers, what waits on you and what the project memory has to decide. The panel's
minute tick writes it on Monday from 09:00 (or the first tick after) to `~/.local/state/ziggy/reports/<YYYY>-W<ww>.md`,
once, with a notification of kind `report`; "Stats" shows the latest under "Weekly report" (this week so far
before the first one); `ziggy report` prints it up to yesterday (`--save` writes it).

### Worktree lifecycle

A ticket's worktree is created from a fresh fetch of the base branch when it first runs; the target's `prepare`
commands run once there (outside the sandbox, network allowed). `preserve` globs copy gitignored files (`.env`, local
settings) from the main checkout. With `ports = { start, size, count }` each ticket holds its own port range
(`PIPELINE_PORT`/`PIPELINE_PORT_END`, `{port}`/`{port_end}`) until its worktree is removed. `cleanup <id>` ("Temizle")
runs the target's `teardown` commands (180 s each) and removes a finished ticket's worktree; uncommitted files or a
failing teardown keep it unless `--force`. The branch stays. When a ticket's PR is merged or closed, the PR sync
archives it and removes its worktree (never forced; a merged PR's local branch goes too); one that still holds work is
kept with a card saying why.

### Sessions on this machine

`sessions` and "Sessions" list the other Claude Code sessions on this machine, read-only: live ones from
`~/.claude` and every target's `CLAUDE_CONFIG_DIR`, and those that ended in the last 3 days, with where they run, the
last prompt and a resume command; "Transcript" opens a transcript. The pipeline's own sessions are left out, and so are
helper sessions whose working directory name starts with a prefix in `[sessions] hide_cwd_prefixes` (only counted).

### Cost and stats

Every session records its cost, turns and tokens in its `run.json`; "Stats" adds them up (retro sessions as stage
`retro`, lead sessions as `lead`, onboarding sessions as `onboard`, roadmap drafts as `roadmap`). A target's
`budget_usd` caps each session with `claude --max-budget-usd`. Two caps are checked before every stage starts
(`ziggy/budget.py`, costs as the sessions report them): `ticket_budget_usd`, all of one ticket's sessions — over it
the run fails between stages with the numbers, and `ziggy run <id> --now` goes on; `daily_budget_usd`, the target's
sessions that started today — over it a ticket waits as `budget` ("Daily budget used" on the board, "Start
now" goes on now) and the panel's tick starts it the next day. "Delivery" (and `ziggy stats`) counts the last 30 days'
merged PRs per project and overall: the share merged without a change round after the PR opened (no review-page or
GitHub feedback round), change rounds per PR, and the sessions' cost per merged PR, labelled "kota" for a Claude
subscription login (its sessions report rate limits): no dollars are paid there, the figure is the quota used at API
prices.

## Safety model

Layers, from [docs/how-it-works.md](how-it-works.md) "Safety layers":

- **Claude Code sandbox.** Every session gets `--settings claude-settings.json --setting-sources project
  --strict-mcp-config --permission-mode auto`: OS sandbox with no network egress (localhost binding allowed for test
  databases), no unsandboxed fallback, writes limited to the worktree and the inbox (`--add-dir`), deny rules for
  `git push`, `gh`, `curl`, package installs, `.env*` reads and more, no reads of the owner's notes and home folders
  (`~/Documents`, `~/Desktop`, `~/Downloads`, iCloud Drive) or of Claude Code's per-project data
  (`~/.claude/projects`, `~/.claude-*/projects`: project knowledge comes from Ziggy's memory), no reads of other
  tools' credentials (`~/.aws`, `~/.kube`, `~/.docker`, `~/.gnupg`, `~/.netrc`, `~/.codex`, the keychain, browser
  profiles, any `.env`/`.npmrc`/`.pypirc`; verified to stop even a Python `open()` from Bash), and no attribution
  trailers. Ziggy refuses to start a session on a Claude Code older than 2.1.90 (earlier sandbox network bypasses).
- **No credentials in the environment.** Sessions, checks and the Codex review get the environment without variables
  whose names look like credentials (`*TOKEN*`, `*SECRET*`, `*API_KEY*`, `AWS_*`, `GH_*`, …); the target's own `[env]`
  entries stay, Claude's own login only reaches claude sessions. `prepare` keeps the full environment (private
  registries) and runs outside the sandbox.
- **Guarded pushes.** Before any push (`publish`, an adopted PR, `restack --push`) the branch and its PR text are
  checked for files that run in CI or steer agents (workflows and other CI configs, `AGENTS.md`/`CLAUDE.md` anywhere,
  editor-agent rules, devcontainers, MCP configs, `.claude/`, git hooks, install-time `package.json` scripts),
  credentials (in added lines, commit messages, the PR text) and invisible Unicode. Findings stop the push until you
  approve them (the panel asks; `publish --accept-findings`); approval holds for those findings, a new one asks
  again, so an automatic push after a feedback round never carries one unseen. PR text and posted Codex reviews always
  go out with invisible characters dropped and images turned into links (an image URL is fetched when the PR is
  viewed).
- **No network in runs.** Dependencies must be in place after `prepare`; runs cannot install packages.
- **Target hooks off.** `disableAllHooks`; `--setting-sources project` keeps user settings out.
- **Tools denied.** Always `AskUserQuestion`, `ScheduleWakeup`, `CronCreate`, plus each stage's deny list.
- **pre-push hook.** `ziggy/githooks/pre-push` rejects any push while `ZIGGY_RUN` is set; sessions get it through
  `GIT_CONFIG_*` environment variables, so the target's `.git/config` is never edited.
- **Wrapper-only push.** Sessions never push; the wrapper does, with the run-only environment removed: `publish` (also
  run after a GitHub feedback round; one ticket branch, never a protected or base branch), `restack --push`, the
  onboarding AGENTS.md draft PR (its own `docs/agents-md` branch) and `project new`'s first push of `main`.
- **Draft-only PRs.** Nothing approves, marks ready or merges; `tests/test_publish.py` greps the code for it.
- **Checks sandboxed.** Engine checks run under `sandbox-exec` (see Engine checks); presets add named rules per target.
- **Protected files.** Agents cannot write `AGENTS.md` or `.claude/settings.json`; a change to them is a patch you
  apply from the inbox (see Protected files).
- **Secrets.** No token in the config (Slack tokens via commands, tracker access via Claude connectors); ticket text,
  PR feedback and retro evidence are quoted to agents as untrusted data; onboarding never reads real `.env` values into
  the config and scrubs them from logs.
- **Memory as data.** Only the wrapper reads or writes the memory folder (sessions never see the vault); recalled
  entries are fenced as data, not instructions, and secrets are scrubbed from candidates.

`scripts/sandbox-smoke.sh <target-repo>` re-verifies the Claude Code sandbox after a Claude Code upgrade (needs
`claude` logged in, `uv`, `python3` and MongoDB on `127.0.0.1:27017`); `scripts/checks-sandbox-smoke.sh <dir>` verifies
the `sandbox-exec` profile of engine checks.

## Configuration

[config.example.toml](../config.example.toml) documents every key; unknown keys are refused. The few everyone sets:

| Key | What |
|---|---|
| `default_target` | the target used outside any target's checkout |
| `[targets.<name>] path`, `base_branch`, `remote` | the main checkout and where worktrees come from (defaults `main`, `origin`) |
| `github_repo`, `gh_user` | where `publish` opens draft PRs, and as whom |
| `prepare` | commands run once per new worktree, outside the sandbox |
| `[[targets.<name>.checks]]` | engine checks (`name`, `run`, `timeout`) |
| `[targets.<name>.env]` | environment for prepare, sessions and checks (e.g. `CLAUDE_CONFIG_DIR` for another Claude login) |
| `plan_model`, `build_model` | stage models (defaults `fable`, `opus`) |
| `max_active` | runs at once per target (default 2) |

Other target keys: `mode` (`personal` or `controlled`, see Controlled mode), `router_model`, `retro`, `retro_model`,
`codex_review` (see Codex review),
`roadmap_model`, `lead_model`, `lead_issue_label` (see The lead), `memory_dir`, `memory_budget` (4096 bytes), `timeout`
(5400 s), `quiet_after` (900 s), `stall_after` (10 m), `budget_usd`, `ticket_budget_usd`, `daily_budget_usd`, `issue_label`, `issue_run`, `best_of`, `codex_stages`, `codex_model`, `preserve`, `teardown`, `ports`, `pr_feedback_bots`,
`pr_replies`. Other tables: `[notifications]`, `[chat]`, `[lead]` (`at`, `enabled`), `[host]` (`enabled`, `swap_hold`;
see A busy machine), `[memory]` (`vault`,
`projects_map`; see Project memory), `[onboard]` (gh accounts and Claude logins by path, `onboard_model`),
`[sources.<name>]`, `[slack]`. Placeholders in `prepare` and `env`: `{run_id}`, `{state}`, `{worktree}`, `{target}`,
`{checkout}`, `{port}`, `{port_end}`.

Per-target standing notes for agents go in the memory folder's `context.md` (see Project memory), recalled with the
memory into every stage after `prompts/_context.md`. A target's `docs/PROJECT.md` (written by `project` tickets) is
included in every stage prompt when present.

### Slack app

Create from a manifest at api.slack.com/apps:

```yaml
display_information:
  name: Ziggy
features:
  bot_user:
    display_name: Ziggy
oauth_config:
  scopes:
    bot: [chat:write, groups:history, reactions:write]
settings:
  event_subscriptions:
    bot_events: [message.groups]
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

Then create an app-level token with `connections:write`, install the app, invite the bot to the private channel, and
point `[slack]` at both tokens.

## Repository layout

| Path | What |
|---|---|
| `bin/ziggy` | entry point from a checkout (no install); `pyproject.toml` makes the `ziggy` command of an install |
| `ziggy/__init__.py`, `__main__.py` | the version; `python -m ziggy` |
| `ziggy/doctor.py` | `ziggy doctor`: the machine's prerequisites |
| `ziggy/cli.py` | argument parsing and every command |
| `ziggy/config.py` | loads and validates the personal config |
| `ziggy/ticket.py` | ticket contract (`ticket.json`, `ticket.md`), types, branch naming |
| `ziggy/state.py` | the state directory layout, `state.json`, locks and run slots |
| `ziggy/pipeline.py` | the engine: stages, routing, question rounds, the gate, review scope, intent gaps, nudges, retries, resume |
| `ziggy/pipelines.py` | loads and validates `pipelines/<name>.toml` |
| `ziggy/results.py` | the result protocol: per-stage schemas, validation, the prompt footer |
| `ziggy/render.py` | the Mustache subset the prompts use |
| `ziggy/plan.py` | the ticket plan: task counts, appended decisions and review rounds |
| `ziggy/dispatch.py` | one headless `claude -p` session: flags, environment, `run.json`, the watch loop, stop |
| `ziggy/progress.py` | readable progress from a session's event stream (`watch`, the panel's activity) |
| `ziggy/checks.py` | engine checks under `sandbox-exec`, the `check_sandbox` presets |
| `ziggy/protected.py` | protected files: patch validation, pending patches, Uygula / Reddet |
| `ziggy/worktree.py` | worktrees, `preserve`, ports, prepare and teardown, remote checks |
| `ziggy/changes.py` | the review page: diffs, comments, findings, guide, evidence, PR draft |
| `ziggy/feedback.py` | PR feedback from GitHub, the github review round, replies |
| `ziggy/publish.py` | push and draft PR, stacked PR bases, the restack push |
| `ziggy/pulls.py` | open PRs and adopting one |
| `ziggy/review.py` | Codex review |
| `ziggy/cleanup.py` | PR sync for merged or closed PRs, archiving, worktree removal |
| `ziggy/stack.py` | dependencies (`after`), split, stacked slices, restack |
| `ziggy/quota.py` | the usage-limit ledger per Claude login, holding and resuming tickets at a limit |
| `ziggy/host.py`, `stalls.py` | the machine's memory and the host hold; tool calls that take long, where a stage's time went |
| `ziggy/deferred.py` | deferred findings as entries, "Make a ticket" |
| `ziggy/chat.py` | ticket chat |
| `ziggy/attention.py` | what needs you, the inbox ledger |
| `ziggy/notify.py` | macOS notifications |
| `ziggy/retro.py`, `memory.py` | retro sessions; project memory (files, candidates, recall, usage) |
| `ziggy/roadmap.py` | the roadmap: roadmap.md or BMad's epics, automatic progress, `yol` changes, the draft session |
| `ziggy/lead.py` | the morning lead: signals, proposals, the daily schedule, Today's "finished yesterday" |
| `ziggy/sessions.py` | other Claude Code sessions on this machine, read-only |
| `ziggy/bmad.py` | BMad Method status of a target |
| `ziggy/project.py` | the project page's state and `route` |
| `ziggy/new_project.py` | `project new` |
| `ziggy/onboard.py`, `onboard_verify.py`, `onboard_agent.py` | `target add`: proposal, verification in a throwaway clone, agent session and AGENTS.md draft PR |
| `ziggy/listener.py`, `slack_socket.py` | Slack intake and its Socket Mode transport (the only `slack_sdk` import) |
| `ziggy/dashboard.py`, `ziggy/web/` | the panel: standard-library HTTP server and the static page (`app.js`, `review.js`, `chat.js`, `sessions.js`, `md.js`) |
| `ziggy/pipelines/` | one TOML file per pipeline; format in [ziggy/pipelines/README.md](../ziggy/pipelines/README.md) |
| `ziggy/prompts/` | stage prompts, shared partials (`_*.md`), personas (`roles/`), review lenses (`lenses/`), the retro, roadmap draft, lead and onboarding prompts |
| `ziggy/claude-settings.json` | the settings every headless session gets |
| `ziggy/githooks/pre-push` | rejects pushes while `ZIGGY_RUN` is set |
| `ziggy/templates/` | `AGENTS.md`, `PROJECT.md` and `gitignore-base` for `project new` |
| `config.example.toml` | template for the personal config |
| `skills/ziggy/` | the Claude Code skill |
| `scripts/sandbox-smoke.sh`, `scripts/checks-sandbox-smoke.sh` | sandbox smoke tests (above) |
| `scripts/bmad-setup.sh`, `_bmad-output/` | BMad Method for developing this repository: pinned setup (run it by hand, or from a SessionStart hook in your own `.claude/settings.local.json`), build records |
| `docs/` | `architecture.md` (diagrams), `how-it-works.md`, `reference.md` (this page) |
| `tests/` | pytest suite |

## Development

```bash
uv run --no-project --python 3.12 --with pytest==8.4.2 python -m pytest tests -q
```

The tests build a throwaway target repository with a bare origin (`tests/conftest.py`) and drive the CLI, engine and
panel against fakes: `fake_claude.py` (stage sessions, scripted per test), `fake_claude_chat.py`, `fake_codex.py` and
`fake_gh.py`, selected through `ZIGGY_CLAUDE`, `ZIGGY_CODEX` and `ZIGGY_GH`. Contract tests cover
`claude-settings.json` and the `pre-push` hook. Tests that need macOS `sandbox-exec` skip elsewhere. CI
(`.github/workflows/ci.yml`) runs the suite with Python 3.12 on every push and pull request.
