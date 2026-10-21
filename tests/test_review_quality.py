"""Review quality: the verification bar the engine holds findings to, the dependency changes a review is told about,
and the automatic Codex review of risky or large tickets."""
import pytest
from conftest import FAKE, ROOT, calls, cli, script
from test_engine import _records, _review, _state

from ziggy import config, dashboard, results, review

FAKE_CODEX = ROOT / "tests" / "fake_codex.py"


def finding(fid, **extra):
    return {"id": fid, "disposition": "fix", "summary": f"{fid} problem", **extra}


def test_findings_below_the_verification_bar_are_held_as_unverified():
    result = {"verdict": "READY", "route": "fix", "fixedIds": ["R0"], "findings": [
        finding("R1", location="app.py:12", evidence="app.py:12 divides by zero"),
        finding("R2", location="app.py:20", evidence=""),
        finding("R3", location="the checkout flow", evidence="it looks wrong"),
        finding("R4", location="app.py:30", evidence="app.py:30 skips it", confidence=0.4),
        finding("R0"),  # fixed this round: nothing to prove
        {**finding("R5"), "disposition": "dismiss"}]}
    warnings = results.hold_unproven(result)
    assert [f["id"] for f in result["findings"]] == ["R1", "R0", "R5"]
    held = {u["id"]: u["reason"] for u in result["unverified"]}
    assert held == {"R2": "held by the engine: no evidence", "R3": "held by the engine: its location and evidence name no file",
                    "R4": "held by the engine: confidence 0.40 is under 0.6"}
    assert result["route"] == "fix" and result["counts"]["unverified"] == 3 and "R2, R3, R4 held" in warnings[0]


def test_a_review_whose_every_fix_was_held_converges_instead_of_sending_work_back(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [_review("fix", [finding("R1-1", location="somewhere", evidence="")])]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    assert [c.get("stage") for c in calls(env)].count("implement") == 1
    [rec] = [r for r in _records(env) if r["stage"] == "review"]
    assert rec["route"] == "converged" and rec["result"]["unverified"][0]["id"] == "R1-1"
    assert any("every finding to fix was held" in w for w in rec["result_warnings"])


def test_the_review_is_told_which_manifests_and_lockfiles_changed(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": ["package.json", "web/yarn.lock", "src/app.py"]}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: add a date library")
    assert cli("run", "local-1").returncode == 0
    prompt = next(c["prompt"] for c in calls(env) if c.get("stage") == "review")
    assert "**Dependencies changed** on this branch: `package.json` (added); `web/yarn.lock` (added); diff the" in prompt
    assert "src/app.py" not in prompt.split("**Dependencies changed**")[1].split("\n")[0]


def test_a_branch_without_dependency_changes_gets_no_dependency_step(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    assert "Dependencies changed" not in next(c["prompt"] for c in calls(env) if c.get("stage") == "review")


@pytest.mark.parametrize("setting, risk, size, expected", [
    ("off", "high", "large", False), ("risky", "low", "small", False), ("risky", "high", "small", True),
    ("risky", "medium", "large", True), ("always", "low", "trivial", True)])
def test_codex_review_is_wanted_by_the_setting_and_the_plans_rating(env, setting, risk, size, expected):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f'[targets.demo]\ncodex_review = "{setting}"\n'))
    assert review.wanted(config.load_target("demo"), {"risk": risk, "size": size}) is expected


def test_an_unknown_codex_review_setting_is_a_config_error(env):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", '[targets.demo]\ncodex_review = "maybe"\n'))
    with pytest.raises(config.ConfigError, match="codex_review must be one of 'off', 'risky', 'always'"):
        config.load_target("demo")


def test_done_starts_the_codex_review_when_the_target_asks_for_it(env, monkeypatch):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", '[targets.demo]\ncodex_review = "always"\n'))
    monkeypatch.setenv("ZIGGY_CODEX", str(FAKE_CODEX))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(env["log"].parent / "codex.log"))
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    proc = cli("run", "local-1")
    assert proc.returncode == 0 and "Codex review started in the background" in proc.stderr
    target = config.load_target("demo")
    [job] = dashboard.jobs(target, "local-1")
    assert job["command"] == "review local-1" and _state(env)["status"] == "done"


def test_no_codex_review_without_the_setting_or_the_binary(env, monkeypatch):
    monkeypatch.setenv("ZIGGY_CODEX", str(FAKE))  # installed, but the target did not ask
    target = config.load_target("demo")
    assert review.schedule(target, "local-1", {"risk": "high"}) is None
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", '[targets.demo]\ncodex_review = "always"\n'))
    monkeypatch.setenv("ZIGGY_CODEX", str(env["log"].parent / "no-such-codex"))
    assert review.schedule(config.load_target("demo"), "local-1", {}) is None
