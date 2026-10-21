{{! Adapted from BMAD-METHOD (MIT), .claude/skills/bmad-review/references/lens-verification-gap.md }}
**Verification gaps** — ask one question per changed behaviour: if it broke where it is actually used, would a test
or check fail? Skip non-behavioural changes (formatting, comments, pure renames, type-only).
1. Name each changed behaviour: output, side effect, branch, error path, schema or event shape, default, rule.
2. Trace it to the nearest place that observes it (callers, routes, commands, schema readers; one to three hops).
3. Name the smallest realistic regression there (invert the branch, drop the default, omit the field): the
   demonstration. Read the relevant test and decide whether the demonstration would make an assertion fail.
4. Report a **regression gap** (no test runs that path or nothing asserts the result), a **broken-verification
   gap** (the test is skipped, flaky, not in the normal run, mocks the integration away, or only checks "no
   error"), or a **missing-adoption gap** (a sibling site the change clearly supersedes still does it the old way,
   and no test notices).
5. Also check each claim in the implementer's report and each `[x]` task against evidence: engine checks
   (`source: engine`), test files in the diff and saved artifacts count; a check without output, or an AC nothing
   exercises, is a gap.
Evidence rules: read a test before saying what it covers; before saying no test exists, search the repo for the
symbol and its imports and say how far you looked; drop any finding you cannot ground.
