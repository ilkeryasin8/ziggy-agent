## Intent gap: report it, do not patch around it

An intent gap is rare. It is a change that answers a different intent than the ticket and PLAN, or one built on a
PLAN assumption the code proved wrong, so that finishing or fixing it only goes further the wrong way. For example:
the ticket asks to hide an item for one user and the change deletes it for everyone; PLAN assumed the export runs on
the server, but it runs in the browser, so the change edits code that never runs. You must be confident: the
ticket, PLAN or the user's decisions say one thing and the change clearly does another.

It is **not** an intent gap: a bug, a missing or failing test, an edge case, a style or naming issue, the right
intent done partly or badly, or anything a fix task can correct while keeping the direction. Those follow the usual
rules. When in doubt, it is not an intent gap.

When it is one, return `NEEDS_USER` with `intentGap` (leave `askThen` unset):

- `summary`: what the change does instead of what the ticket means, in one or two sentences the user understands
  without reading code ("it went the wrong way because …").
- `evidence`: where it shows: the ticket or PLAN line, and the `path:line` that contradicts it.
- `questions`: the 1–3 decisions that set the direction, in the user's terms (the product, not the code), each with
  2–4 `options` and your `recommendation`.

The pipeline saves the change as a patch, sets the branch back to the commit before it (unless the branch is already
published), records the answers in PLAN's `## Decisions (user)` and builds again from there.
