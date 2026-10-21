"""Best of two (pipeline._best_of_two): a risky or large ticket's first implement runs twice from the same start, both
attempts are checked, the better one stays on the branch and the other is saved as a patch."""
import os

import pytest
from conftest import calls, cli, git, script
from test_engine import _records, _review, _stages, _state

from ziggy import checks, config, pipeline, state

needs_sandbox = pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")


def setup(env, best_of="always", check='test ! -f bad.txt'):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f'[targets.demo]\nbest_of = "{best_of}"\n')
                             + f'[[targets.demo.checks]]\nname = "no-bad"\nrun = "{check}"\n')


def branch_files(env, ticket_id="local-1"):
    wt = state.worktree_dir(config.load_target("demo"), ticket_id)
    return sorted(git("ls-files", cwd=wt).split())


@needs_sandbox
def test_the_second_attempt_wins_when_the_first_fails_a_check(env):
    setup(env)
    script(env, {"implement": [{"verdict": "READY", "_commit": "bad.txt"}, {"verdict": "READY", "_commit": "good.txt"}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 0
    assert _stages(env) == ["plan", "verify", "implement", "implement", "review"]  # one visit, two sessions
    assert "good.txt" in branch_files(env) and "bad.txt" not in branch_files(env)
    st = _state(env)
    assert st["status"] == "done" and st["run"]["best_of"]["why"] == "second: fewer failing checks"
    assert st["run"]["visits"]["implement"] == 1
    patch = state.run_dir(config.load_target("demo"), "local-1") / st["run"]["best_of"]["patch"]
    assert "bad.txt" in patch.read_text() and patch.read_text().startswith("# ziggy: the implement attempt that lost")
    winner = [r for r in _records(env) if r["stage"] == "implement"][1]
    assert winner["best_of"]["chosen"] == st["run"]["best_of"]["second"]


@needs_sandbox
def test_with_both_passing_the_smaller_change_stays(env):
    setup(env)
    script(env, {"implement": [{"verdict": "READY", "_commit": ["a.txt", "b.txt"]}, {"verdict": "READY", "_commit": "c.txt"}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 0
    assert "c.txt" in branch_files(env) and "a.txt" not in branch_files(env)
    assert _state(env)["run"]["best_of"]["why"] == "second: the smaller change"


@needs_sandbox
def test_a_second_attempt_that_is_not_ready_leaves_the_first(env):
    setup(env)
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}, {"verdict": "FAILED", "summary": "gave up"}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 0
    assert "a.txt" in branch_files(env) and _state(env)["run"]["best_of"]["why"].startswith("first: the second attempt was not usable")


@needs_sandbox
def test_off_by_default_and_only_for_risky_tickets_when_risky(env):
    env["config"].write_text(env["config"].read_text() + '[[targets.demo.checks]]\nname = "ok"\nrun = "true"\n')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 0
    assert [c.get("stage") for c in calls(env)].count("implement") == 1 and "best_of" not in _state(env)["run"]
    target = config.load_target("demo")
    stage = pipeline.pipelines.Stage(id="implement", title="Implement", prompt="implement.md", result="implement", checks=True)
    t = {"id": "local-9"}
    risky = target.__class__(**{**target.__dict__, "best_of": "risky"})
    run = {"visits": {"implement": 1}}
    assert pipeline._best_of_wanted(risky, t, {"risk": "high"}, stage, {}, run)
    assert pipeline._best_of_wanted(risky, t, {"size": "large"}, stage, {}, run)
    assert not pipeline._best_of_wanted(risky, t, {"risk": "medium", "size": "small"}, stage, {}, run)
    assert not pipeline._best_of_wanted(risky, t, {"risk": "high"}, stage, {"resume": {"x": 1}}, run)
    assert not pipeline._best_of_wanted(risky, t, {"risk": "high"}, stage, {}, {"visits": {"implement": 2}})


def test_best_of_is_validated(env):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", '[targets.demo]\nbest_of = "twice"\n'))
    with pytest.raises(config.ConfigError, match="best_of must be one of 'off', 'risky', 'always'"):
        config.load_target("demo")
