"""Checks against the base: a failing check tried again on the code before the change (pre-existing or not), a fix's
regression test that must fail without the fix, and checks.at_base itself."""
import os

import pytest
from conftest import calls, cli, git, script
from test_engine import _records, _review, _state

from ziggy import checks, results

needs_sandbox = pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")


@needs_sandbox
def test_a_check_that_fails_on_the_base_too_is_pre_existing_and_the_next_checks_still_run(env):
    env["config"].write_text(env["config"].read_text()
                             + '[[targets.demo.checks]]\nname = "old"\nrun = "grep -q healthy README.md"\n'
                             + '[[targets.demo.checks]]\nname = "ok"\nrun = "true"\n')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    assert [c.get("stage") for c in calls(env)].count("implement") == 1  # not sent back
    old, ok = next(r for r in _records(env) if r["stage"] == "implement")["checks"]
    assert old["status"] == "fail" and old["preexisting"] is True and "pre-existing, not sent back" in old["detail"]
    assert ok["name"] == "ok" and ok["status"] == "pass"
    assert list(_state(env)["run"]["baselines"].values()) == ["fail"] and _state(env)["status"] == "done"


@needs_sandbox
def test_a_check_the_change_broke_passes_on_the_base_and_goes_back(env):
    env["config"].write_text(env["config"].read_text() + '[[targets.demo.checks]]\nname = "no-a"\nrun = "test ! -f a.txt"\n')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 1  # every visit breaks it again: the run fails at the cap
    first = next(r for r in _records(env) if r["stage"] == "implement")
    assert first["checks"][0]["status"] == "fail" and not first["checks"][0].get("preexisting")
    assert first["route"] == results.CHECKS_FAILED and list(_state(env)["run"]["baselines"].values()) == ["pass"]


@needs_sandbox
def test_a_fix_must_bring_a_regression_test_that_fails_without_it(env):
    proves = {"path": "test_fix.sh", "run": "test -f fix.txt && test -f test_fix.sh"}
    script(env, {"implement": [{"verdict": "READY", "_commit": ["fix.txt", "test_fix.sh"]},
                               {"verdict": "READY", "_commit": ["fix.txt", "test_fix.sh"],
                                "regressionTest": {"path": "test_fix.sh", "run": "test -f test_fix.sh"}},
                               {"verdict": "READY", "_commit": ["fix.txt", "test_fix.sh"], "regressionTest": proves}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "fix: crash on empty cart")
    assert cli("run", "local-1").returncode == 0
    missing, passes, good = [r["checks"][-1] for r in _records(env) if r["stage"] == "implement"]
    assert missing["name"] == "regression-test" and "READY without `regressionTest`" in missing["detail"]
    assert passes["status"] == "fail" and "does not reproduce the bug" in passes["detail"]
    assert good["status"] == "pass" and "without the fix, passes with it" in good["detail"]
    impl = [c["prompt"] for c in calls(env) if c.get("stage") == "implement"][0]
    assert "Report it in `regressionTest`" in impl
    assert _state(env)["status"] == "done"


def test_at_base_shows_the_code_before_the_change_and_puts_the_branch_back(env):
    repo = env["repo"]
    base = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "keep.txt").write_text("kept\n")
    git("add", "-A", cwd=repo)
    git("rm", "-q", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "change", cwd=repo)
    with checks.at_base(repo, base, keep=("keep.txt",)):
        assert (repo / "README.md").read_text() == "target\n" and not (repo / "new.txt").exists()
        assert (repo / ".gitignore").exists() and (repo / "keep.txt").read_text() == "kept\n"
    assert (repo / "README.md").read_text() == "changed\n" and (repo / "new.txt").exists()
    assert not (repo / ".gitignore").exists() and git("status", "--porcelain", cwd=repo) == ""
    git("reset", "--keep", "--quiet", base, cwd=repo)  # the index is fresh: a reset after it is not refused
    assert (repo / "README.md").read_text() == "target\n"
