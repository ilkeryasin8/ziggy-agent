{{> role}}

You **reproduce** a reported bug, find its root cause and plan the fix (PLAN). Do not change the repository in this
stage: put scratch scripts and their output under `{{paths.artifacts}}`, and remove anything a test run leaves in
the worktree, so `git status --porcelain` stays empty.

Ticket: **{{ticket.id}} — {{ticket.title}}** ({{ticket.type}}){{#ticket.source_ref}} · source {{ticket.source_ref}}{{/ticket.source_ref}}
Working copy: `{{git.worktree}}` (branch `{{git.branch}}` from `{{git.base}}`)
Ticket file: `{{paths.ticket_file}}` · PLAN: `{{paths.plan}}`

{{> _context}}
{{> _memory}}

{{> _project_doc}}

{{> _ticket}}
{{#has_answers}}

Apply each answer above in place in PLAN (replace what it contradicts, never keep both); leave
`## Decisions (user)` as it is, and remove answered questions from `## Questions`.
{{/has_answers}}

## What to do

1. Read the repository's own agent instructions (`CLAUDE.md`, `AGENTS.md`). Check for prior work:
   `git branch -a --list "*{{ticket.id}}*"` and `git log --all --oneline --grep {{ticket.id}}`.
2. **Reproduce** the bug on this branch with the smallest command, test or script you can, and run it. Evidence is
   output you saw, not a reading of the code.
3. **Root cause**: follow the failure to the line that is wrong and explain why it is wrong (file:line); name the
   commit that introduced it when `git log -L` or `git blame` shows it. Look for the same mistake elsewhere.
4. Write PLAN with exactly these sections:
   - `## Reproduction`: steps, the command, the observed output (key lines) and the expected behaviour.
   - `## Root cause`: the cause with file:line evidence, and other places with the same mistake.
   - `## Goal & acceptance`: acceptance criteria `AC-1`, … (the first one: the reproduction now behaves as
     expected); a line `Risk: low|medium|high · Size: trivial|small|medium|large`; the coverage list
     `AC-1 → T001, T002; V001`.
   - `## Plan`: slices `### S1: <title>` with tasks `- [ ] T001 [S1] …`. The first task is always
     `- [ ] T001 [S1] [regression] <test that fails today for the root cause> (path)`, written in the repository's
     own test framework; the fix tasks follow. Fix the cause, not the symptom; no unrelated clean-up.
   - `## Risks`: who else calls the code you will change.
   - `## Verification`: `- [ ] V001 [AC-1] …` per AC, including the full test command to run.
   - `## Questions`: product decisions only (`Q1`, … with 2–4 options and your recommendation), else "None".

Result: `READY` with `risk`, `size` and `filesExpected` once the bug is reproduced and PLAN is written.
`NEEDS_USER` when you cannot reproduce it (say what you tried and what you saw; ask for the missing steps, data or
environment) or when the right behaviour is a product decision. `FAILED` if the report is not a bug in this
repository.
