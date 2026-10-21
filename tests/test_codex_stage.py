"""Codex as a stage agent (dispatch.run_codex): the target's `codex_stages` run under `codex exec` with the same prompt
and result file, in Codex's own sandbox (writes in the worktree and the inbox), and the engine treats the result as
any other; a read-only stage that changes the repository still fails."""
import json
import os
from pathlib import Path

import pytest
from conftest import ROOT, calls, cli, script
from test_engine import _records, _review, _state

from ziggy import config, dispatch

FAKE_CODEX_EXEC = ROOT / "tests" / "fake_codex_exec.py"


@pytest.fixture
def codex(env, monkeypatch):
    monkeypatch.setenv("ZIGGY_CODEX", str(FAKE_CODEX_EXEC))
    log = env["log"].parent / "codex-exec.log"
    monkeypatch.setenv("FAKE_CODEX_EXEC_LOG", str(log))
    os.chmod(FAKE_CODEX_EXEC, 0o755)
    return log


def stages(env, value):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f"[targets.demo]\ncodex_stages = {value}\n"))


def codex_calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_codex_review_reads_the_same_prompt_and_routes_like_any_other(env, codex):
    stages(env, '["review"]')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0 and _state(env)["status"] == "done"
    assert [c.get("stage") for c in calls(env)] == ["plan", "implement"]  # Claude Code ran these two only
    [review] = codex_calls(codex)
    argv = review["argv"]
    assert argv[:2] == ["exec", "--json"] and "--ignore-user-config" in argv and argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    wt = state_worktree()
    assert Path(review["cwd"]).resolve() == wt.resolve() and argv[argv.index("--cd") + 1] == str(wt)
    assert argv[argv.index("--add-dir") + 1].endswith("/runs/local-1/inbox")
    assert "## Procedure" in review["prompt"] or "review" in review["prompt"].lower()
    rec = [r for r in _records(env) if r["stage"] == "review"][0]
    assert rec["agent"] == "codex" and rec["session_id"] == "fake-thread-1" and rec["state"] == "ok"
    assert rec["codex_usage"] == {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}
    assert rec["final_message"] == "VERDICT: READY" and rec["route"] == "converged"


def state_worktree():
    from ziggy import state
    return state.worktree_dir(config.load_target("demo"), "local-1")


def test_a_codex_implement_commits_and_the_checks_follow(env, codex):
    stages(env, '["implement"]')
    model = 'codex_model = "gpt-5.5"\n'
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f"[targets.demo]\n{model}"))
    script(env, {"implement": [{"verdict": "READY", "_commit": "b.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    [impl] = codex_calls(codex)
    assert impl["argv"][impl["argv"].index("--model") + 1] == "gpt-5.5"
    assert (state_worktree() / "b.txt").read_text() == "implement by codex\n"


def test_a_read_only_codex_stage_that_changes_the_repository_fails(env, codex):
    stages(env, '["review"]')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [{**_review("converged", [], converged=True), "_touch": "README.md"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 1
    rec = [r for r in _records(env) if r["stage"] == "review"][0]
    assert rec["state"] == "failed" and "must not change the repository" in rec["result_error"] and rec["agent"] == "codex"


def test_codex_stages_are_validated_and_parse_codex_reads_its_events(env):
    stages(env, '"review"')
    with pytest.raises(config.ConfigError, match="codex_stages must be a list of stage ids"):
        config.load_target("demo")
    events = "\n".join(json.dumps(e) for e in [{"type": "thread.started", "thread_id": "t"},
                                               {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
                                               {"type": "turn.failed"}])
    out = dispatch.parse_codex(events)
    assert out["session_id"] == "t" and out["is_error"] is True and out["codex_usage"]["input_tokens"] == 5
