{{! How strict the review stage is. The review prompt includes this file; edit it freely (prompts are read on every
    run). Keep each rule one line and concrete: the reviewer applies it literally. }}
## Review policy

- **Important**: an acceptance criterion is broken, data can be lost or corrupted, a crash or a wrong result on a
  real path, a security hole, or code that contradicts a user decision.
- **Nit**: real but small (a missing test for a stated behaviour, a missing escape, a false claim in PLAN or the
  evidence, a silently skipping test helper, naming). At most 5 nit rows; mention the rest as a count in `summary`.
- **Fix, defer, dismiss**: a real problem this change introduced, in scope and cheap to fix, is `fix` whatever its
  class and sends work back; out of scope or costly is `defer`; `dismiss` only a wrong finding, with the evidence.
- **Pre-existing**: the problem was there before this change (`git show {{git.base_commit}}:<path>`). Record it in
  `{{paths.deferred}}`; it never sends work back.
- **Verification bar**: a finding is reported only after its location was opened and the consequence confirmed on
  this diff. `evidence` quotes the line (`path:line`) and says what goes wrong; `confidence` below 0.6 means
  `unverified`, not a table row.
- **Skip**: generated files, lockfiles, images and vendored code; formatting the project's formatter owns.
- **Later rounds** (round 2 and on): report Important findings and the status of the carried ones only; do not open
  new nits on code an earlier round already read.
- **Summary**: one line, `F to fix, D deferred, K pre-existing, X dismissed, U unverified · <route>`.
