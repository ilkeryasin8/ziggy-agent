# Architecture

Ziggy turns a ticket into a reviewed branch by running a fixed pipeline of headless Claude Code sessions, one per
stage, each sandboxed in a git worktree of the target repository. The orchestration is a deterministic engine
(`ziggy/pipeline.py`), not an agent: agents only do the work of one stage and report through a result file; the engine
decides what happens next. This page draws the main pieces; [how-it-works.md](how-it-works.md) has every rule and
`ziggy/*.py` the rest (each module starts with a docstring that says what it owns).

## The system

```mermaid
flowchart LR
    you([You])

    subgraph fronts[Ways in]
        panel["Web panel<br/>ziggy ui · 127.0.0.1"]
        cli["CLI<br/>ziggy new / run / answer …"]
        mcp["MCP server<br/>ziggy mcp"]
        skill["Claude Code skill<br/>/ziggy"]
        slack["Slack listener<br/>(optional)"]
    end

    subgraph core[Ziggy]
        engine["Engine<br/>pipeline.py · pipelines/*.toml"]
        checks["Engine checks<br/>checks.py · sandbox-exec"]
        state[("State<br/>~/.local/state/ziggy")]
        memory[("Project memory<br/>markdown")]
        attention["Attention · inbox<br/>quota · host watch"]
    end

    subgraph run[One stage]
        agent["claude -p<br/>sandboxed, no network,<br/>no MCP, no push"]
        wt[("git worktree<br/>of the target")]
    end

    gh[(GitHub)]
    config[/"~/.config/ziggy/config.toml"/]

    you --> panel & cli & skill & slack
    skill & mcp --> panel
    panel & cli & slack --> engine
    config --> engine
    engine -- "prompt + context" --> agent
    agent -- "edits, commits" --> wt
    agent -- "result.json" --> engine
    engine --> checks --> wt
    engine <--> state
    engine -- "recalled entries" --> memory
    engine --> attention --> panel
    panel -- "publish: push + draft PR" --> gh
    gh -- "PR feedback, merges" --> engine
```

Everything a person does goes through the panel, the CLI or something that drives them (the MCP server and the skill
call the panel's HTTP API; the panel starts the same `ziggy` commands the CLI runs, as background jobs). Only the
engine (and `publish`) touches the target repository from outside a sandbox.

## One stage, end to end

```mermaid
sequenceDiagram
    autonumber
    participant E as Engine (pipeline.py)
    participant D as dispatch.py
    participant A as claude -p (stage)
    participant W as Worktree
    participant C as Engine checks

    E->>E: render the stage prompt (prompts/*.md):<br/>ticket, plan, answers, memory, review context
    E->>D: model, session id, deny list, budget, env (PIPELINE_*)
    D->>A: --settings claude-settings.json --strict-mcp-config<br/>--permission-mode auto --output-format stream-json
    A->>W: read, edit, run tests, commit (implement)
    A-->>D: stream events (watched for quota, quiet, stalls)
    A->>E: $PIPELINE_RESULT_FILE: verdict, summary, questions, route, findings
    alt implement READY
        E->>C: target's checks under sandbox-exec
        C-->>E: pass / fail / failed in its environment
    end
    E->>E: route: next stage, question round, approval gate, done or failed
```

A stage that writes no valid result fails; there is no silent pass. Plan, verify, reproduce and review stages must not
change the repository (their changes are stashed and the stage fails). Each stage has a visit cap per run.

## Ticket states

```mermaid
stateDiagram-v2
    [*] --> new
    new --> running: run
    running --> needs_user: NEEDS_USER (question round)
    running --> awaiting_approval: risky or large plan (gate)
    running --> waiting: depends on another ticket
    running --> quota: Claude login over its limit
    running --> host: machine short of memory
    waiting --> running: dependency done
    quota --> running: window resets
    host --> running: pressure eases
    needs_user --> running: answer
    awaiting_approval --> running: approve / change
    awaiting_approval --> split: split into stacked slices
    running --> failed: no result, loop cap, check failed
    failed --> running: run (retry the stage) / resume / recheck
    running --> done: review converged
    done --> published: publish (push + draft PR)
    published --> running: PR feedback round
    published --> archived: PR merged or closed
    archived --> [*]
```

## Routing: the `feat` pipeline

Pipelines are TOML files (`pipelines/`): an ordered list of stages whose results route to the next. `feat` is the
default; `fix`, `chore`, `project`, `slice` and the optional `bmad` follow the same shape.

```mermaid
flowchart TD
    plan[plan] -->|READY / NEEDS_USER| verify[verify]
    verify -->|READY| gate{risk ≥ medium<br/>or size ≥ large?}
    verify -->|NEEDS_USER| ask[(question round)]
    ask -->|answers| verify
    gate -->|yes| approve[(approval gate)]
    gate -->|no| implement[implement]
    approve -->|approve| implement
    implement -->|READY| checks{engine checks}
    checks -->|pass| review[review]
    checks -->|code failure| implement
    checks -->|environment failure| failed([failed: fix the check, then recheck])
    failed -.->|recheck: checks only, no session| checks
    review -->|fix| implement
    review -->|converged| done([done])
    implement -->|past max_visits| capped([failed: loop cap])
```

## Safety layers

```mermaid
flowchart TB
    subgraph L1[Claude Code sandbox, every stage session]
        s1["OS sandbox: writes only in the worktree and the inbox;<br/>no network egress (localhost allowed)"]
        s2["deny rules: git push, gh, curl, package installs,<br/>.env / .npmrc reads, home folders, other projects' transcripts"]
        s3["no MCP, no user settings or hooks, no attribution;<br/>AskUserQuestion, ScheduleWakeup, CronCreate denied"]
    end
    subgraph L2[Outside the session]
        g1["pre-push git hook refuses any push during a run"]
        g2["engine checks under sandbox-exec: loopback only,<br/>writes in the worktree, temp and state dirs"]
        g3["protected files (AGENTS.md, .claude/settings.json):<br/>agents leave a patch, you apply it"]
    end
    subgraph L3[People decide]
        p1["question rounds and the approval gate"]
        p2["publish opens draft PRs only;<br/>nothing approves, marks ready or merges"]
    end
    L1 --> L2 --> L3
```

## Sohbet and the MCP server

Two ways to talk to Claude about a project outside a ticket run.

```mermaid
sequenceDiagram
    actor You
    participant P as Panel (Sohbet page)
    participant T as talk.py
    participant A as claude -p (chat turn)
    participant K as Claude login's connectors<br/>(e.g. the issue tracker)

    You->>P: message (read chat or write chat)
    P->>T: ziggy talk send (a background job)
    T->>A: --resume the chat's session, MCP on<br/>read: no edits, no shell, no connector writes<br/>write: its own worktree on chat/ID, no push
    A->>K: look up issues, …
    A-->>P: streamed answer, steps, commits
    You->>P: "Ticket'a çevir": a ticket draft from the chat
```

```mermaid
sequenceDiagram
    participant S as Another Claude session
    participant M as ziggy mcp (stdio)
    participant P as Panel HTTP API

    S->>M: ziggy_status / ziggy_get_ticket
    M->>P: GET /api/status …
    S->>M: ziggy_create_ticket {…, confirm: true}
    M->>P: POST /api/new (the panel starts the run)
    Note over S,M: writing tools refuse without confirm: true,<br/>and are hidden with --read-only
```

## Where state lives

```text
~/.config/ziggy/config.toml          targets, checks, models, sources … (unknown keys refused)
~/.local/state/ziggy/
  <target>/
    runs/<ticket>/
      ticket.json, ticket.md         the ticket (trusted header, untrusted body)
      state.json                     status, run, rounds, gate, checks
      inbox/                         plan.md, answers.md, deferred.md, artifacts/ (the agents' scratch)
      NN-<stage>/                    one dir per stage visit: prompt.md, claude.jsonl, run.json, checks.log
      chat/                          the ticket's chat turns
    worktrees/<ticket>/              the ticket's git worktree
    talk/<id>/                       Sohbet chats (and a write chat's worktree)
    memory/ (or a notes vault)       project memory, markdown
  attention.json, quota.json, …      the inbox ledger and machine-wide state
```
