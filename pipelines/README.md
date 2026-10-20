# Pipelines

A ticket runs through one pipeline, `pipelines/<name>.toml`: an ordered list of stages. Each `[[stages]]` entry is
one sandboxed headless `claude -p` session in the ticket's worktree; its prompt is rendered from `prompts/` and the
agent ends by writing a result file. The result's `verdict` (and, with `READY`, its `route`) picks the next stage, a
question round, `done` or `failed`. The loader is `ziggy/pipelines.py`; the engine is
`ziggy/pipeline.py`.

## Shipped pipelines

| Pipeline | Used for | Stages | Gate (after · threshold) |
|---|---|---|---|
| `feat` | type `feat` | plan → verify → implement ⇄ review | plan, verify · risk medium or size large |
| `fix` | type `fix` | reproduce → implement ⇄ review | reproduce · risk high |
| `chore` | types `refactor`, `chore`, `docs` | plan (`plan-lite.md`) → implement ⇄ review | plan · risk high |
| `project` | type `project` (writes `docs/PROJECT.md`) | write ⇄ doc-review | none |
| `bmad` | `new --pipeline bmad` (panel: "Hat: BMad"), a code ticket in a target whose base branch has BMad installed | bmad-plan → bmad-build ⇄ review | bmad-plan · risk high or size large |
| `slice` | never chosen by hand: each slice `ziggy split` makes | implement ⇄ review | none (the parent's plan was approved) |

`⇄`: review sends work back (`fix` route to implement, `revise` to write) until it converges (`converged` /
`accept` → `done`). Every implement stage (`implement`, `bmad-build`) runs the target's checks after `READY`; a
failure goes back to the same stage (`checksFailed`). The type → pipeline map is `ticket.PIPELINES`; `bmad` is the
one per-ticket choice (`ticket.CHOICES`: `standard` or `bmad`); `slice` is set only by a split.

- `feat`: the plan stage's `NEEDS_USER` goes to verify, which decides which open questions reach the user.
- `fix`: reproduce writes the root cause and a plan with a failing regression test first; a bug it cannot
  reproduce goes to the user.
- `bmad`: BMad Method's `bmad-build-auto` plans (halting at ready-for-dev) and builds; our checks and review follow.
  Both BMad stages commit `_bmad-output/` (`commit_paths`); answers go back to bmad-plan for a re-plan.
- `slice`: no plan stage; the split seeds PLAN with the parent's goal, acceptance criteria and decisions plus the
  slice's tasks. The branch is stacked on the slice below, and the review reads the slice's own diff.
- A plan stage that writes `## Plan` as `### S<n>: <title>` slices makes a large or risky plan splittable at the
  gate (`stack.slices`).

## File format

```toml
name = "feat"                # default: the file name
title = "Feature"            # default: the name

[ask_user]                   # NEEDS_USER opens a question round
then = "verify"              # where the answers continue (default: the asking stage; see ask_then)
max_rounds = 5               # question rounds per run (default 5); one more fails the run

[user_review]                # request-changes, Codex fixes, picked PR feedback start a new run here
then = "implement"           # default: the first stage with result = "implement", else the first stage

[intent_gap]                 # optional: where an intent gap's answers continue
then = "implement"           # default: the first stage with result = "implement"

[gate]                       # optional: stop for approval after these stages
after = ["plan"]             # a stage id or a list
risk = "medium"              # low | medium | high: stop when the rated risk is at least this
size = "large"               # trivial | small | medium | large: or when the rated size is at least this

[[stages]]
id = "plan"
role = "architect"
model = "plan"
prompt = "plan.md"
result = "plan"
writes = false
next = { READY = "verify", NEEDS_USER = "verify" }
```

Top-level keys: `name`, `title`, `ask_user`, `user_review`, `intent_gap`, `gate`, `stages` (at least one). The gate
also stops when the ticket asks for a plan checkpoint (`new --plan-checkpoint`); risk and size come from the plan or
verify result and are never lowered during a run.

### Stage keys

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase letters, digits, dashes; starts with a letter, at most 40 characters. |
| `title` | the id | Shown in the panel and `status`. |
| `prompt` | required | File under `prompts/`. |
| `role` | none | `prompts/roles/<role>.md`, rendered where the prompt says `{{> role}}`. |
| `model` | `"build"` | `"plan"` / `"build"`: the target's `plan_model` / `build_model`; anything else is a model id. |
| `result` | `"generic"` | Result schema: `generic`, `plan`, `verify`, `implement`, `review`, `doc`. |
| `writes` | `true` | `false`: the stage must not change the repository; changes it leaves or commits are stashed and fail the stage. PLAN and `deferred.md` live in the inbox, so a read-only stage can still write them. |
| `tools_deny` | `[]` | Tool names the session may not use (e.g. `["Edit", "NotebookEdit"]`). |
| `timeout` | target `timeout` | Duration (`"45m"`, `"1h30m"`). |
| `quiet_after` | target `quiet_after` | Duration without output before the session counts as quiet. |
| `effort` | none | `low`, `medium`, `high`, `xhigh`, `max`: passed as `claude --effort` when the installed CLI knows the flag. |
| `max_visits` | `1` | How often the stage may run in one run; a route past it fails the run. Raise it on every stage a route goes back to. |
| `checks` | `false` | Run the target's `[[targets.<name>.checks]]` after `READY`. |
| `next` | the following stage | A stage id (for `READY`; `NEEDS_USER` → `ask_user`, `FAILED` → `failed`), or a table `{ READY = …, NEEDS_USER = …, FAILED = … }`. |
| `routes` | `{}` | Route names the agent may pick with `READY` → targets, e.g. `{ fix = "implement", converged = "done" }`. `checksFailed` is set by the engine when checks fail. Names: lowercase start, letters, digits, `_`. |
| `ask_then` | `[ask_user] then` | Where answers to this stage's questions continue. |
| `visits` | `{}` | Overrides for later visits in the same run (below). |
| `incremental` | `true` | Review stages only: later rounds read only what changed since the last reviewed HEAD. |
| `retry` | off | Automatic continuation of a lost, quiet or timed-out session (below). |
| `commit_paths` | `[]` | Repository paths the engine commits after the stage (below). |
| `commit_message` | `docs({ticket}): BMad {stage} records` | Message for that commit; `{ticket}` and `{stage}` are filled in. |

Targets (`next`, `routes`, `ask_then`, `then`) are stage ids or `done`, `failed`, `ask_user`. A `READY` with a route
the stage does not declare, or without a route where routes are declared, falls back to `next` with a warning in the
run record.

**`visits`** — `visits = { 2 = { effort = "high" } }` or `[stages.visits.2]`. Keys are visit numbers from 2 (visit 1
is the stage itself); overrides are cumulative (visit 3 gets 2's and 3's, later keys win) and may set only `model`,
`effort`, `timeout`, `quiet_after`, `tools_deny` and `prompt`.

**`retry`** — `retry = { on_lost = true, on_quiet = true, on_timeout = true, max = 2, backoff = "30s",
max_backoff = "10m" }` (defaults: all `false`, `max = 2` (0–10), `backoff = 30s`, `max_backoff = 10m`). The stage
continues in its own session (the same visit) when its process is lost, when it goes quiet (no output for
`quiet_after` while no tool it started is running: it is interrupted with SIGINT), or past its `timeout`. At most
`max` per run for all causes together, waiting `backoff` doubled each time (a lost process at once). A nudge
(`ziggy nudge <id> --text …`) interrupts the running stage the same way and resumes it with the message,
whatever the policy.

**`commit_paths`** — `commit_paths = ["_bmad-output"]`: repository-relative, no `..` or `.git`. After the stage the
engine commits only these paths when they changed (`git add -A` and `git commit --no-verify --only` on them), before
it judges the tree, and records the commit as `records_commit` in the stage's `run.json`. On a `writes = false`
stage, changes under these paths do not count as writes. Not after a session that resumes on its own (its edits stay
in the tree).

**`incremental`** — by default review round 2 and later read only what changed since the newest reviewed HEAD (the
previous review or the user's), plus the previous round's open findings; the whole branch again when that HEAD left
the branch's history, with `incremental = false`, or with `ziggy run <id> --full-review`.

### Load checks

Unknown keys (top level, stage, `[ask_user]`, `[user_review]`, `[intent_gap]`, `[gate]`, `retry`, `visits.<n>`),
a missing prompt or role file, an unknown result schema, invalid durations or values, duplicate stage ids and
unknown targets fail the load with a message naming the file (the first eight errors). Warnings, which do not fail
it: a route back to a stage whose `max_visits` is 1, a `visits.<n>` that can never apply, `incremental` on a
non-review stage, `checksFailed` without `checks = true`.

## Review rounds

A review stage with `fix` and `converged` routes: any open finding with disposition `fix` sends work back, nit or
important; `dismiss` is only for a finding that is wrong. The engine enforces it (`results.settle_review_route`):
`READY` with an open `fix` finding is routed `fix`; on the last round (the review's cap, or no visit left on the
stage `fix` goes to) open findings that are not important become `defer` and the review converges. With important
ones open on that last visit it may ask the user (`NEEDS_USER` with `askThen = "implement"`, one of its route
targets): the answers then grant one more implement and one more review in that run (`run.adjudicated`); asking
again on that last visit fails the run.

## Intent gaps

An implement or review stage that finds the work answers a different intent returns `NEEDS_USER` with `intentGap`.
The engine saves the attempt as `runs/<id>/attempts/attempt-<n>.patch`, resets the branch to the commit before the
run's first implement, opens a question round with the gap's questions and continues at `[intent_gap] then` with
`via.route = "intentGap"`; `askThen` is ignored. The branch keeps its code instead when it is already published,
when a slice above is built on it, when that commit left the branch's history, or when nothing was built. A pipeline
without an implement stage and no `then` treats the result as an ordinary question round.

## Protected files

Agents cannot write the files `claude-settings.json` denies `Edit` for (`AGENTS.md`, `.claude/settings.json`;
`ziggy/protected.py` reads the list from there). Stages with the `implement` or `generic` result schema may list
`protectedPatches` (`[{path: "artifacts/<name>.patch", files, summary}]`): unified diffs under the inbox artifacts.
After the session the engine keeps each one that exists, names only protected files in its diff headers and passes
`git apply --check` on the worktree's HEAD (the rest become result warnings), copies it to
`runs/<id>/protected/<n>.patch` and records it in `state.json` `protected_patches` (`pending`, then `applied` with the
commit's `sha`, or `rejected` with a `reason`). The run goes on as usual; nothing lands on the branch until the user
applies it (inbox card, `ziggy protected <id> apply <n>`). An implement stage that returns `FAILED` naming a protected
file in its summary or open items, with a valid protected-only patch among its `artifacts`, is read as `READY` with
that patch (a result warning says so). Prompts get `protected.files` and `protected.pending` (`n`, `files`,
`summary`, `path`); `_context.md` tells every stage to plan "prepare a patch" tasks instead of edits, and the review
dismisses a finding a pending patch covers.

## Prompts

Syntax: a Mustache subset (`ziggy/render.py`): `{{a.b}}`, `{{#x}}…{{/x}}` (a dict pushes it as context, a
list repeats per item, anything else shows when truthy), `{{^x}}…{{/x}}`, `{{.}}`, `{{! comment}}`,
`{{> partial}}`. No HTML escaping; missing values render empty; a tag alone on its line takes the line with it.

### Variables

| Variable | Contents |
|---|---|
| `ticket` | `id`, `title`, `type`, `body`, `slug`, `source_ref`, `language`, `language_name` |
| `project` | `name`, `path`, `github_repo` (the target) |
| `git` | `main` (the checkout), `worktree`, `branch`, `base`, `base_commit` |
| `paths` | `ticket_file`, `plan`, `deferred`, `answers`, `inbox`, `artifacts`, `result_file`, `project_doc`, `run_dir` |
| `project_doc` | `exists`, `path` (`docs/PROJECT.md` in the worktree) |
| `run` | `n`, `stage`, `visit`, `ticket_visit` (visits across all runs), `max_visits`, `last_visit`, `round`, `adjudicated`, `via` (`{from, route}` when a route led here) |
| `user_review` | when a review round started the run: `round`, `source` (`user`, `codex`, `github`), `heading`, `comments`, `findings`, `has_findings`, `github` (PR feedback: its quoted text is untrusted), `prefix` (`G` for GitHub, else `U`: the item ids' letter) |
| `review` | review stages only: `round`, `prev_round`, `prev_head`, `user_head`, `since`, `full`, `full_reason`, `carried`, `has_carried`, `last_round` (no fix round left in this run) |
| `engine_checks` | when the target has checks and a stage of the pipeline runs them: `commands` [{`name`, `run`}], `results` [{`name`, `status`, `detail`}] (the newest outcomes, when taken on the current HEAD), `has_results` |
| `ports` | `base`, `end`, when the target allocates ports |
| `memory` | the target's recalled project memory: `exists`, `text` (the fenced entries within `memory_budget`), `ids`, `left_out`, `warnings` |
| `prev.<stage>` | the latest successful result per stage id (this run's override earlier runs') |
| `attempt` | only on the visit an intent gap's answers continue at: `attempt`, `stage`, `summary`, `evidence`, `patch` (its copy under the inbox), `reset`, `reset_to`, `kept`, `kept_note`, … |
| `answers_markdown`, `has_answers` | the user's answers so far |
| `language`, `today`, `now` | output language name (empty for the default), date, timestamp |

The session's environment adds `PIPELINE_RESULT_FILE`, `PIPELINE_STAGE`, `PIPELINE_TICKET`, `PIPELINE_INBOX`,
`PIPELINE_PLAN`, `PIPELINE_WORKTREE`, `PIPELINE_BASE_COMMIT`, `PIPELINE_ARTIFACTS_DIR`, `PIPELINE_VISIT`,
`PIPELINE_LAST_VISIT` (`1`/`0`), the port range (`PIPELINE_PORT`, `PIPELINE_PORT_END`) and the target's `[env]`.

### Partials

| Partial | What it renders |
|---|---|
| `{{> role}}` | the stage's `prompts/roles/<role>.md` (empty without a role) |
| `{{> _context}}` | standing rules for every stage (`prompts/_context.md`) |
| `{{> _memory}}` | the target's project memory as data (`## Proje hafızası`: its `context.md`, rules, matching decisions and pitfalls, episodes for plan-type stages) and the instruction to list relied-on ids in `memoryUsed`; right after `{{> _context}}` |
| `{{> _ticket}}` | the untrusted ticket text and the user's answers |
| `{{> _project_doc}}` | the instruction to read `docs/PROJECT.md` when it exists (not in the `project` pipeline, whose subject is that doc) |
| `{{> _round}}` | the round number: the stage's visit count across all runs of the ticket |
| `{{> _review-policy}}` | how strict reviews are (classes `important` / `nit` / `pre-existing`, the verification bar); edit it to tune the reviewer |
| `{{> _intent-gap}}` | what an intent gap is and how to report it (in `implement.md` and `review.md`; `bmad-build.md` inlines its own) |
| `{{> lenses/<name>}}` | review lenses: `blind-hunter`, `edge-cases`, `verification-gap`, `convergence` in `review.md`; `adversarial` in `doc-review.md` |

Roles: `analyst`, `architect`, `dev`, `pm`, `product-architect`, `reviewer`, `ux-designer`.

### Writing a prompt

- Do not write the result protocol: the engine appends it (the schema, the verdicts, the stage's routes, where
  answers may continue, and for `implement`/`review` the evidence rule: files under `paths.artifacts` listed in
  `artifacts`). Every stage schema accepts `memoryUsed` (the memory ids the session relied on) and a review finding
  may carry `memory` (the `M-<n>` it breaks). End the prompt with a "Result:" paragraph that says when to use each
  verdict and what the result fields should contain.
- PLAN (`{{paths.plan}}`) and `{{paths.deferred}}` live in the inbox, outside the repository; the engine appends the
  user's decisions and reviews to PLAN.
- The BMad prompts (`bmad-plan.md`, `bmad-build.md`) are the exception to the partials: their first line is BMad's
  slash command, so they inline their own rules and include `{{> _memory}}` after that rules list.
- Not stages: `retro.md` is the read-only retro session after a ticket is done (`retro.py`; context `project`,
  `ticket`, `cwd`, `evidence`, `memory` {`exists`, `text`}, `max_items`, `language`), `onboard.md` the onboarding
  agent session (`onboard_agent.py`), `roadmap.md` the roadmap draft session (`roadmap.py`; context `project`, `docs`
  [{`path`}], `context` and `decisions` {`exists`, `text`}, `max_phases`, `max_items`). Their schemas, `retro`,
  `onboard` and `roadmap`, cannot be a stage's `result`.

## Adding a pipeline

1. Copy the closest file, rename `name`, and change the stages.
2. Reuse prompts where you can; a new prompt needs `{{> role}}`, `{{> _context}}`, `{{> _memory}}`,
   `{{> _project_doc}}`, `{{> _ticket}}` and a Result paragraph matching its `result` schema.
3. Point ticket types at it in `ticket.PIPELINES` (`ziggy/ticket.py`), or make it a per-ticket choice in
   `ticket.CHOICES` as `bmad` is.
