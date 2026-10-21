# Contributing

Thanks for looking. Ziggy is a personal tool opened as is, so the bar is: small, well-reasoned changes that keep it
simple to run on one machine.

## Setup

```bash
git clone <your fork> ziggy && cd ziggy
bin/ziggy doctor                 # what your machine is missing
python3.12 -m pip install pytest # the only dependency, and only for the tests
python3.12 -m pytest tests -q    # ~7 minutes; sandbox tests skip where sandbox-exec is missing
```

`bin/ziggy` runs from the clone without installing. The tests never call the real Claude Code or GitHub: they use the
fakes in `tests/fake_claude.py`, `tests/fake_claude_chat.py`, `tests/fake_gh.py` and `tests/fake_codex.py`.

## Ground rules

- **Standard library only** at run time (`slack_sdk` for `ziggy listen` is the one optional extra). No new
  dependencies without a strong reason.
- **The engine decides.** Routing, gates, checks and limits live in Python and the pipeline TOML, not in prompts. A
  prompt may ask; the engine enforces.
- **Every change has a test**, and the whole suite passes before a PR. A bug fix starts with a test that fails.
- **Safety first.** Changes to `ziggy/claude-settings.json`, the checks' sandbox profile, the publish guard or the
  environment scrub need a test that shows the boundary still holds.
- **Match the code around you**: comment density, naming, docstrings that say *why*. The panel is plain JavaScript
  (no build step); keep it that way.
- Update `docs/reference.md` (and `docs/how-it-works.md` where it explains the rule) with the behaviour you change, and
  add a line to `CHANGELOG.md`.

## Measuring a change to the engine or the prompts

Run `ziggy eval run --label before` on `main` and `ziggy eval run --label after` on your branch, then
`ziggy eval compare before after`. It uses real Claude sessions and costs quota; say in the PR which tasks you ran.
New tasks go under `ziggy/evals/tasks/` and must pass `ziggy eval validate`.

## Pull requests

One topic per PR, a description of why, and the test output. Draft PRs are welcome for early feedback.
