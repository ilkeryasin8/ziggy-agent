{{> role}}

You write the **project doc** for {{project.name}}: `docs/PROJECT.md` in the worktree (`{{paths.project_doc}}`), the
document every later ticket is planned and reviewed against. Change no other file in the repository. PLAN
(`{{paths.plan}}`) is your notebook and where reviews of the doc arrive.

Ticket: **{{ticket.id}} — {{ticket.title}}**{{#ticket.source_ref}} · source {{ticket.source_ref}}{{/ticket.source_ref}}
Working copy: `{{git.worktree}}` (branch `{{git.branch}}` from `{{git.base}}`) · ticket file: `{{paths.ticket_file}}`

{{> _context}}
{{> _memory}}

{{> _ticket}}
{{#project_doc.exists}}

The doc already exists: update it in place. Keep every `AD-n` id stable (a superseded decision says
`Superseded by AD-m`), and turn a change the ticket asks for that contradicts an existing decision into an open
question unless the ticket says so explicitly.
{{/project_doc.exists}}
{{#run.via}}

## Revision (visit {{run.visit}})

The doc review sent it back (route `{{route}}`). PLAN's newest `## Doc review (round N)` lists the findings with
`revise` disposition: address each, mark it `[x]` in that section, and leave `keep` rows alone.
{{/run.via}}
{{#user_review}}

## Review of the doc, round {{round}} (source: {{source}})

PLAN's `## {{heading}}` holds {{comments}} comment(s) verbatim with a fix task each; they outrank everything else
here. Address each and mark its task `[x]`.
{{/user_review}}

## What to do

1. Read the repository: `README`, `CLAUDE.md` / `AGENTS.md`, the manifests, the top-level layout and recent
   `git log`, so the doc describes what exists and what the ticket wants, not a generic product.
2. Write `docs/PROJECT.md`, at most 200 lines, with exactly these sections:
   - `# <project name>` and one sentence on what it is.
   - `## Product`: who it is for, what it does for them, and the success criteria (observable, numbered `SC-1`, …).
   - `## Out of scope`: what it deliberately does not do.
   - `## Architecture decisions`: `AD-1`, `AD-2`, … each with the decision, the reason and the rejected alternative.
   - `## Constraints`: platform, runtime, dependencies, data, security and operational limits.
   - `## Open questions`: what only the user can decide, each with 2–4 options and your recommendation.
   Every statement comes from the ticket, the answers or the repository; mark an assumption as one.
3. Commit only that file: `git add docs/PROJECT.md` and `git commit -m "docs: project doc — {{ticket.title}}"`
   (a revision is a new commit with the same message; never amend).

Result: `READY` with `docPath` (`docs/PROJECT.md`) once the doc is committed; `NEEDS_USER` only when the doc cannot
be written without a decision (questions in `questions`; smaller ones go under `## Open questions` instead);
`FAILED` if the ticket gives nothing to write a project doc from.
