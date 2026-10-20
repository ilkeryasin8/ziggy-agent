---
title: 'P0.2: Push guard for headless pipeline runs'
type: 'feature'
created: '2026-10-06'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: '96ac09ade73f6b6739ffa146302dda7711fa4d7c'
context: ['{project-root}/AGENTS.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The BMAD ticket pipeline will run `claude -p` unattended in per-ticket worktrees. Nothing in the repository yet stops such a run from pushing or reaching the network; the only push point must be the pipeline wrapper.

**Approach:** Decision (human, 2026-10-07, after review pass 1 showed command-string layers can be bypassed from inside the agent's process): the Claude Code OS sandbox is the primary layer — no network egress, writes limited to the worktree, no unsandboxed fallback. Settings deny rules and a git `pre-push` hook remain as cheap secondary layers. No command-string guard script.

## Boundaries & Constraints

**Always:** Sandbox enabled with `allowUnsandboxedCommands: false` and `failIfUnavailable: true`; `network.allowedDomains` empty; `network.allowLocalBinding: true` (tests need the local MongoDB). The `pre-push` hook is inert unless `BMAD_PIPELINE_RUN` is non-empty. Tracked files contain no absolute local paths, hostnames, or company/customer names.

**Never:** No command-string guard (`guard-bash.sh`) or PreToolUse hook. No `filesystem.allowWrite`, `excludedCommands`, or allowed network domains. No pipeline CLI/dispatch code (P0.3). No `_bmad/custom/*` changes (P0.1). No change to `.claude/settings.json`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Settings contract | `pipeline/claude-settings.json` | sandbox keys exactly as in Always; every deny entry from plan §6.1 present; no `hooks` key | N/A |
| pre-push active | `BMAD_PIPELINE_RUN=1`, hooksPath set, `git push` | push rejected, remote refs unchanged | N/A |
| pre-push inactive | env unset or empty | push proceeds | N/A |
| Sandboxed push bypasses | headless run in a worktree outside `~/.claude`, bare origin outside the worktree: plain push, `python subprocess git push --no-verify`, `git -c alias.p=push -c core.hooksPath=/dev/null p`, `git send-pack` | origin refs byte-identical | N/A |
| Sandboxed normal work | same run: `git commit`, MongoDB ping on 127.0.0.1:27017, `uv run --no-cache _bmad/scripts/render_skill.py …` | commit lands; ping ok; render prints a workflow path | N/A |
| Sandboxed egress / tamper | same run: HTTPS to github.com; write to main repo `.git/config` | both fail | N/A |

</frozen-after-approval>

## Code Map

- `.github/workflows/ci.yml` -- jobs `backend`, `llm-worker`, `frontend`; every Python test file must be listed (AGENTS.md). Add a lean `pipeline` job.
- `README.md` "## Tests" (~line 161) -- "four independent test surfaces" becomes five.
- `backup/p02-attempt1` (commit `eed6ec7`) -- review pass 1 code. KEEP and reuse: `pipeline/githooks/pre-push`, `pipeline/tests/test_pre_push.py`, the deny/allow lists in `pipeline/claude-settings.json`, the CI job shape. DROP: `pipeline/hooks/guard-bash.sh`, `pipeline/tests/test_guard_bash.py`, the `hooks` block.
- Claude Code sandbox docs (code.claude.com/docs/en/sandboxing) -- linked worktrees may write the main repo's shared `.git` except `hooks/` and `config`; everything under `~/.claude` is a protected path no setting can open; on macOS `allowLocalBinding` is the only way to reach host localhost.

## Tasks & Acceptance

**Execution:**
- [x] `pipeline/claude-settings.json` -- pass-1 deny/allow lists plus the `sandbox` block from Always; remove `hooks` -- primary + permission layers in one file loaded via `--settings`.
- [x] `pipeline/githooks/pre-push` -- restore from `backup/p02-attempt1` unchanged.
- [x] `pipeline/tests/test_pre_push.py` -- restore from `backup/p02-attempt1` unchanged.
- [x] `pipeline/tests/test_claude_settings.py` -- assert the settings-contract row: exact sandbox values, every plan §6.1 deny entry (not a subset), no `hooks`, no `allowWrite`/`excludedCommands`/allowed domains.
- [x] `pipeline/scripts/sandbox-smoke.sh` -- build a temp bare origin + clone + linked worktree outside `~/.claude` (`mktemp -d` under `/tmp`), run one cheap headless `claude -p` with `--settings pipeline/claude-settings.json` that executes the matrix commands, then assert ground truth: origin refs unchanged, the commit landed, main `.git/config` unchanged; print the rest; exit non-zero on any failed assertion -- re-verifies the sandbox after Claude Code upgrades; not run in CI (needs the CLI and an account).
- [x] `.github/workflows/ci.yml` -- `pipeline` job: Python 3.12, `pytest==8.4.2`, run `test_pre_push.py` and `test_claude_settings.py` explicitly.
- [x] `README.md` -- Tests: five surfaces; one line each for `pipeline/tests` and `pipeline/scripts/sandbox-smoke.sh`.

**Acceptance Criteria:**
- Given a clean checkout, when `bash pipeline/scripts/sandbox-smoke.sh` runs, then it exits 0 and every matrix "Sandboxed …" row holds.
- Given CI on the branch, when the `pipeline` job runs, then both test files run and pass.

## Implementation Notes

- Pass 2 (2026-10-07): settings = pass-1 deny/allow lists + sandbox block, `hooks` removed; `pre-push` and `test_pre_push.py` restored byte-identical from `eed6ec7`; settings test asserts the sandbox block by exact equality and all 33 deny entries; `sandbox-smoke.sh` runs every matrix command inside one headless haiku run via a probe script and checks refs, commit, and `.git/config` from outside. Negative control by the implementer: the same probe outside the sandbox pushed all four refs.
- Verified: `pytest pipeline/tests` 40 passed; `sandbox-smoke.sh` 13/13 PASS.
- Review pass 2 patches deliberately changed the restored files: `pre-push` comment only (non-comment lines identical to `eed6ec7`), and `test_pre_push.py` now drops inherited `GIT_*` variables. Final: 42 passed; smoke 14/14 with `--setting-sources project`.

## Spec Change Log

- 2026-10-07, review pass 1 → intent_gap resolved by the human (option A). Trigger: verified pushes reached a bare remote via inline alias, interpreter subprocess with `--no-verify`, and `send-pack`, past deny rules, `guard-bash.sh`, and `pre-push`. Amended: Approach, Boundaries, Matrix, Code Map, Tasks. Known-bad state avoided: a push guard that matches command strings inside the agent's own process. KEEP: `pre-push` and its tests, the plan §6.1 deny/allow lists, the lean CI job. Sandbox behaviour in the matrix was verified on this machine on 2026-10-07 before this rewrite.

## Review Triage Log

Pass 1 (blind-hunter BH, edge-case-hunter EC, verification-gap VG). Verified by probing the real guard and by real pushes to a bare remote with `BMAD_PIPELINE_RUN=1` and `core.hooksPath=pipeline/githooks`.

| Finding | Verdict | Evidence |
|---|---|---|
| BH1/EC9 allowed interpreter runs `git push` in a subprocess | high | `.venv/bin/python -c "subprocess.run(['git','push','--no-verify',…])"`: guard exit 0, ref `pyverify` reached the bare remote; `Bash(.venv/bin/python:*)` is in allow |
| BH2/EC2 inline alias + `core.hooksPath=/dev/null` | high | guard exit 0; ref `alias` reached the remote; no deny prefix matches `git -c …` |
| EC3/BH9 `send-pack`, `subtree push`, `http-push` | high | guard exit 0 for both; `send-pack` ref reached the remote, pre-push not run |
| EC14/BH12 `git push --no-verify` skips pre-push | high | git semantics; reproduced through the python path above |
| BH5/EC12/EC13 guard or hooks disabled via Bash (`chmod -x`, `sed -i`, `.git/config`) | high | `Edit(./pipeline/**)` covers edit tools only; a non-executable hook exits 126, which Claude Code treats as non-blocking |
| EC-claim1/EC-claim2 "blocks pushes anywhere" / acceptance criterion | high | contradicted by the three remote refs above |
| BH7 network exfiltration through allowed interpreters | medium | denies are by command name only; no network isolation exists |
| BH6 `Read(...)` denies do not cover `cat` via Bash; `.env` only at root | medium | `Read(./.env*)` is root-relative; Bash is not a Read tool |
| BH3/EC4 quoted/escaped words (`"git" push`, `G=git; $G push`) | medium | guard exit 0 for `"git" push`; plain-push pre-push still blocks these when hooks are intact |
| EC1 backslash-newline between `git` and `push` | medium | guard exit 0; pre-push still blocks if intact |
| EC5/EC6/VG-O1 `gh` after `sudo`/`env`/`timeout`/`if`, or `\gh` | medium | `sudo gh pr create` guard exit 0 |
| EC8/BH9 `git -C . reset --hard`, `checkout -B main`, `update-ref -d`, `tag -d`, `master` | medium | guard exit 0 for reset and `checkout -B main` |
| EC7 bundled short force flags (`-qf`) | maybe-false | not probed; settle by running `git checkout -qf x` through the guard |
| EC10/EC11/BH8/VG-O2 false positives: `-m "drop -f flag"`, `git add -f`, `git log refs/heads/main` | medium | guard exit 2 for the first two; contradicts frozen "commit messages stay allowed" |
| VG1 gh chained/env-prefixed/absolute-path/newline forms untested | medium | pre-verified: four mutations keep 46 passed |
| VG2 long global options (`--git-dir`) untested | medium | pre-verified mutation |
| VG3 clustered `-dD` untested | medium | pre-verified mutation |
| BH11 settings test checks a subset of deny rules | low | 11 deny entries removable without a failing test |
| BH10 README/CI wording on which tests run | low | CI lists files per AGENTS.md; README sentence ambiguous |
| BH4 nothing installs the hooks or passes `--settings` | false | intent assigns wiring to P0.3 dispatch ("passes to every headless run") |

Routing: G1 (all `high` rows) shares one root cause inside `<frozen-after-approval>` — the Approach relies on command-string layers inside the agent's own process, which cannot stop a push from that process → **intent_gap**, loopback to the human. Other entries are moot until the code is re-derived.

Pass 2 (sandbox implementation; BH2/EC2/VG2). Pass-1 rows above concern the removed guard script and are not carried.

| Finding | Verdict | Evidence |
|---|---|---|
| VG2-1/BH2-19/EC2-9 settings test pins only `sandbox`; `permissions.additionalDirectories`, `Edit(...)`/`WebFetch(domain:)`/broad `Bash(*)` allow entries would widen the sandbox unnoticed | medium | docs: permission paths and domains merge into the sandbox config; tests check only `push`/`gh` substrings → patch: exact allow list, `permissions` keys = {allow, deny}, fix comment |
| BH2-6/EC2-7 user and project settings merge into the headless run | medium | `~/.claude/settings.json` adds hooks, `defaultMode: auto` and `env` today; no widening keys now, but a future `additionalDirectories` would apply silently → patch: `--setting-sources project` in the smoke run |
| EC2-1/EC2-2 non-zero exits from unrelated causes (126/127, missing curl, offline host) count as "blocked" | medium | `expect_fail` treats any non-zero code as pass; `curl` is not in the preflight list → patch |
| BH2-16 claude exit code printed, never checked | low | `sandbox-smoke.sh` prints `$?` only → patch (direct check) |
| EC2-3 gitignored BMad files missing → setup hook runs a full install, misleading FAILs | low | `[ -f ] && cp` silently skips → patch: fail with a message |
| EC2-4/EC2-6 `mktemp` and `git config` failures unchecked | low | no `|| exit 2` → patch |
| BH2-2 pre-push comment claims it rejects every push | low | `--no-verify` and `core.hooksPath` overrides skip it → patch: comment says advisory, sandbox is the control |
| EC2-8 `GIT_*` env leaks into `test_pre_push.py` | low | env copy drops only `BMAD_PIPELINE_RUN` → patch |
| BH2-18 README smoke prerequisites omit git, python3, curl | low | script preflight checks more tools than README lists → patch |
| BH2-10 secrets readable (`Read(./.env*)` root-only; `~/.netrc`, `~/.aws`) and could be committed, then pushed by the wrapper | medium | sandbox allows reads everywhere by default → defer: plan P1.6 sanitize/secret policy |
| BH2-11 Bash can write `pipeline/**`, `AGENTS.md`, the worktree's `pre-push` | medium | `Edit(...)` denies cover file tools only → defer: P0.3 must point `core.hooksPath` at the main checkout (outside the worktree); diff policy in P1.6 |
| BH2-1 nothing installs the hook or passes `--settings` | false | intent assigns this to P0.3 |
| BH2-3 prefix deny rules miss variants | low | true but harmless under the sandbox (Approach makes deny secondary) → rejected |
| BH2-7 relies on default `autoAllowBashIfSandboxed` | low | a change makes the smoke fail loudly (no results, now with exit check) → rejected |
| BH2-8 smoke clones HEAD, not the working tree | low | smoke verifies the sandbox, not repo code → rejected |
| BH2-9 commit and tamper checks contradict | false | docs: linked worktrees may write the common dir except `config`/`hooks`; smoke 13/13 shows both |
| BH2-12 render fails offline | false | smoke `render succeeded` and printed a workflow path |
| BH2-13/EC2-5 temp dir leaks on early exit / Ctrl-C | low | rare; a trap adds code → rejected |
| BH2-17 CI lists files while README runs the directory | false | AGENTS.md requires explicit per-file CI lists |

## Design Notes

Why the sandbox: it is enforced by the OS around every process a Bash command starts, so no spelling of a push escapes it, and `.git/hooks` and `.git/config` stay unwritable, which keeps `pre-push` and the remote config intact. Consequences for P0.3: dispatch must create worktrees outside `~/.claude` (protected path → `git commit` fails there) and pass `--settings pipeline/claude-settings.json` with `BMAD_PIPELINE_RUN=1`; the wrapper pushes outside the sandbox with the variable unset. Accepted risk: `allowLocalBinding` lets commands reach every localhost TCP service; Unix sockets stay blocked. On Linux `allowLocalBinding` has no effect (the pipeline runs on macOS, plan D19).

## Verification

**Commands:**
- `python -m pytest pipeline/tests -q` -- expected: all pass
- `sh -n pipeline/githooks/pre-push && bash -n pipeline/scripts/sandbox-smoke.sh` -- expected: no syntax errors
- `bash pipeline/scripts/sandbox-smoke.sh` -- expected: exit 0
