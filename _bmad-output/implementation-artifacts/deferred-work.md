- source_spec: none
  summary: P0.1 — add _bmad/custom/bmad-build-auto.toml with persistent_facts (CI test environment recipe, one-retrieval-variable-per-commit rule, CI test-list rule, untrusted-ticket rule) and an on_complete that prints exactly one BMAD_PIPELINE_RESULT line.
  evidence: Split from the Faz 0 turn-1 intent at the multi-goal check; it is independently shippable from the P0.2 push guard and was deferred to the next turn by user choice.
- source_spec: `_bmad-output/implementation-artifacts/spec-p0-2-headless-push-guard.md`
  summary: Keep secrets the sandbox can read (nested .env files, ~/.netrc, ~/.aws, ~/.npmrc) out of commits the wrapper will push.
  evidence: The sandbox allows reads everywhere by default and Read(./.env*) is root-relative; plan P1.6 sanitize/secret policy is the natural owner.
- source_spec: `_bmad-output/implementation-artifacts/spec-p0-2-headless-push-guard.md`
  summary: P0.3 dispatch must set core.hooksPath to the main checkout's pipeline/githooks (outside the ticket worktree) and the diff policy must flag agent edits to pipeline/**, AGENTS.md and settings.
  evidence: Edit(...) denies cover Claude's file tools only; sandboxed Bash can write anywhere in the worktree, including its copy of pre-push.
