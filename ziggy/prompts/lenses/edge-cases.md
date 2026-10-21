{{! Adapted from BMAD-METHOD (MIT), .claude/skills/bmad-review/references/lens-edge-case-hunter.md }}
**Edge cases** — a pure path tracer: list only paths the diff leaves unhandled, never whether the code is good.
1. Walk every branch and boundary reachable from the changed lines: conditionals, loops, early returns, error
   handlers, and value transitions (empty, null, maximum, off-by-one, overflow, type coercion, timeouts, races,
   permissions, locale, offline). Derive the classes from the code, not from this list.
2. Implicit branches: when the diff changes how some members of a fixed set are handled (enum, status, flag,
   range), the members it does not mention are branches too.
3. Every call the diff adds or changes, tests included: read the callee's signature and check argument count,
   order, types and defaults.
4. Deletions: removed code that carried a behaviour or contract nothing re-establishes is a finding (a regression,
   an orphaned reference, newly dead code).
5. Only then read the implementer's report and commit messages, and try to falsify each claim ("same as X",
   "preserves Y") against the code you traced. A claim repeated in a comment is still only a claim.
Discard handled paths silently.
