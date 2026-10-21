{{> role}}

You **plan** a small maintenance change (refactor, chore or docs) for {{project.name}}. No verification stage follows,
so check your own claims. Do not change the repository in this stage; the only file you write is the plan (PLAN).

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

1. Read the repository's own agent instructions (`CLAUDE.md`, `AGENTS.md`) and the code the ticket touches; cite
   file:line for every claim.
2. A refactor changes no behaviour: name the tests that pin the current behaviour, or add a task that writes them
   first. A chore or docs change touches nothing beyond what the ticket names.
3. Write PLAN, short (aim for 40 lines), with exactly these sections:
   - `## Goal & acceptance`: two or three sentences; `AC-1`, … (observable); a line
     `Risk: low|medium|high · Size: trivial|small|medium|large`.
   - `## Plan`: `- [ ] T001 [S1] what to change (path)`, one file or one behaviour per task.
   - `## Verification`: `- [ ] V001 [AC-1] the command or check that proves it`.
   - `## Questions`: product decisions only (`Q1`, … with 2–4 options and your recommendation), else "None".
4. If the work turns out to be a feature or a bug fix, say so in `summary` and plan it anyway.

Result: `READY` with `risk`, `size` and `filesExpected` once PLAN is written and has no open question;
`NEEDS_USER` with the `## Questions` entries when only the user can decide; `FAILED` if the ticket cannot be done.
