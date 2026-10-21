{{! Adapted from BMAD-METHOD (MIT), .claude/skills/bmad-review/references/lens-adversarial.md }}
**Blind hunter** — bugs the diff introduces, judged from the code alone. Do not read PLAN, the ticket or the
implementer's report first: the narrative must not steer the hunt. Read the diff hunks and the code they call;
look for wrong logic, null and type mistakes, concurrency and ordering, leaked resources, swallowed or wrong error
paths, broken callers of a changed signature, and what is missing, not only what is wrong. Keep going past the
first few: if you find nothing, re-read the diff once more before you return an empty list.
