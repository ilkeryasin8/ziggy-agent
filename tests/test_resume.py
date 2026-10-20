"""Continuing an interrupted stage session with `claude --resume`: a run killed mid-stage, an implement
session that timed out with half-done edits (stashed, then handed back to the resumed session), a nudge
that interrupts a running stage, and the stage retry policy (quiet, timeout) with its backoff."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import BIN, calls, cli, script

from ziggy import config, pipeline, pipelines


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def results(env, data: dict) -> str:
    script(env, data)
    return str(env["results"])


def stage_dirs(env, ticket_id, stage):
    return sorted((env["state"] / "runs" / ticket_id).glob(f"*-{stage}"))


def test_a_killed_run_resumes_its_own_session(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    fake = results(env, {"plan": [{"verdict": "READY", "_sleep": 60}, {"verdict": "READY"}]})
    run = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], env={**os.environ, "FAKE_RESULTS": fake},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    assert wait_for(lambda: stage_dirs(env, ticket_id, "plan") and (stage_dirs(env, ticket_id, "plan")[0] / "pid").exists())
    pid_file = stage_dirs(env, ticket_id, "plan")[0] / "pid"
    claude_pid = int(pid_file.read_text())
    os.killpg(run.pid, signal.SIGKILL)  # the wrapper dies with no chance to record anything
    os.killpg(claude_pid, signal.SIGKILL)
    run.wait()
    pid_file.unlink(missing_ok=True)  # what a killed wrapper leaves behind, minus a stale pid

    target = config.load_target("demo")
    assert pipeline.resumable(target, ticket_id)
    session = json.loads((stage_dirs(env, ticket_id, "plan")[0] / "run.json").read_text())["session_id"]

    proc = cli("resume", ticket_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The killed session may die before it logs itself, so pick the calls by what they are, not by position.
    resumed = [c["argv"] for c in calls(env) if "--resume" in c["argv"]]
    assert len(resumed) == 1 and resumed[0][resumed[0].index("--resume") + 1] == session and "--session-id" not in resumed[0]
    after = [c["stage"] for c in calls(env) if c.get("stage") and "--resume" not in c["argv"] and c["stage"] != "plan"]
    assert after == ["implement", "review"]  # the following stages are fresh sessions
    st = json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())
    assert st["status"] == "done"
    assert pipeline.resumable(target, ticket_id) is None
    assert cli("resume", ticket_id).returncode == 2


def test_a_killed_implement_resumes_with_its_half_done_edits(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    fake = results(env, {"implement": [{"verdict": "READY", "_dirty": "half-done.txt", "_sleep": 60}, {"verdict": "READY", "_commit": "half-done.txt"}]})
    run = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], env={**os.environ, "FAKE_RESULTS": fake},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    root = env["state"] / "runs" / ticket_id
    assert wait_for(lambda: stage_dirs(env, ticket_id, "implement") and (stage_dirs(env, ticket_id, "implement")[0] / "pid").exists())
    wt = json.loads((root / "state.json").read_text())["worktree"]
    assert wait_for(lambda: os.path.exists(os.path.join(wt, "half-done.txt")))
    pid_file = stage_dirs(env, ticket_id, "implement")[0] / "pid"
    claude_pid = int(pid_file.read_text())
    os.killpg(run.pid, signal.SIGKILL)
    os.killpg(claude_pid, signal.SIGKILL)
    run.wait()
    pid_file.unlink(missing_ok=True)

    assert pipeline.resumable(config.load_target("demo"), ticket_id)
    proc = cli("resume", ticket_id)  # the half-done edits do not block a resume
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads((root / "state.json").read_text())["status"] == "done"
    assert sum("--resume" in c["argv"] for c in calls(env)) == 1


def test_a_stopped_session_is_not_resumed(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    fake = results(env, {"plan": [{"verdict": "READY", "_sleep": 60}]})
    run = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], env={**os.environ, "FAKE_RESULTS": fake},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert wait_for(lambda: stage_dirs(env, ticket_id, "plan") and (stage_dirs(env, ticket_id, "plan")[0] / "pid").exists())
    assert cli("stop", ticket_id).returncode == 0
    run.wait(timeout=30)
    assert pipeline.resumable(config.load_target("demo"), ticket_id) is None
    assert "resume" not in cli("status", ticket_id).stdout.split("next:")[-1]


def records(env, ticket_id, stage):
    return [json.loads((d / "run.json").read_text()) for d in stage_dirs(env, ticket_id, stage)]


def load_state(env, ticket_id):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def test_a_nudge_interrupts_the_stage_and_resumes_its_session_with_the_message(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    fake = results(env, {"implement": [{"verdict": "READY", "_sleep": 60, "_commit": "pins.txt"}]})
    run = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], env={**os.environ, "FAKE_RESULTS": fake},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert wait_for(lambda: stage_dirs(env, ticket_id, "implement") and (stage_dirs(env, ticket_id, "implement")[0] / "pid").exists())
    assert wait_for(lambda: any(c.get("stage") == "implement" for c in calls(env)))  # the session is up
    target = config.load_target("demo")
    out = pipeline.nudge(target, ticket_id, "  use the v2 API  ", by="dev")
    assert out == {"message": "interrupting implement; it resumes with your message", "stage": "implement", "delivery": "interrupt"}
    run.wait(timeout=60)
    first, second = records(env, ticket_id, "implement")
    assert first["state"] == "killed" and first["kill_reason"] == "nudge:user" and first["interrupt"] == "nudge:user"
    assert first["nudged"]["text"] == "use the v2 API" and first["retry"] == {"cause": "nudge", "n": None, "max": None, "delay_s": 0}
    assert second["state"] == "ok" and second["resumed"] and second["session_id"] == first["session_id"]
    assert second["retry_of"] == stage_dirs(env, ticket_id, "implement")[0].name
    assert (second["visit"], second["ticket_visit"]) == (first["visit"], first["ticket_visit"]) == (1, 1)
    resumed = [c for c in calls(env) if c.get("stage") == "implement" and c["resumed"]]
    assert len(resumed) == 1 and "dev interrupted you to steer this step" in resumed[0]["prompt"]
    assert "verbatim:\n\nuse the v2 API\n\nFollow it" in resumed[0]["prompt"]
    st = load_state(env, ticket_id)
    assert st["status"] == "done" and st["run"]["visits"]["implement"] == 1
    assert [(n["text"], n["delivery"], n["stage"]) for n in st["nudges"]] == [("use the v2 API", "interrupt", "implement")]
    assert "stash" not in first
    views = {v["id"]: v for v in pipeline.stages_view(target, ticket_id)}
    assert views["implement"]["visits"] == 1


def test_a_nudge_before_the_stage_starts_goes_into_its_prompt_and_is_refused_while_waiting(env):
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    script(env, {"plan": [{"verdict": "NEEDS_USER", "questions": [{"text": "Which pins?"}]}, {"verdict": "READY"}]})
    assert cli("run", ticket_id).returncode == 3
    target = config.load_target("demo")
    with pytest.raises(pipeline.PipelineError, match="waits for answers; answer its questions instead"):
        pipeline.nudge(target, ticket_id, "hurry", by="dev")
    with pytest.raises(pipeline.PipelineError, match="empty message"):
        pipeline.nudge(target, ticket_id, "   ")
    assert cli("answer", ticket_id, "--text", "all of them", "--no-run").returncode == 0
    out = pipeline.nudge(target, ticket_id, "keep it small", by="dev")
    assert out["message"] == "plan gets your message when it starts" and out["delivery"] == "queued"
    assert cli("run", ticket_id).returncode == 0
    plans = [c for c in calls(env) if c.get("stage") == "plan"]
    assert "## A message from dev\n\nThey want this step steered as follows (verbatim):\n\nkeep it small" in plans[1]["prompt"]
    assert plans[1]["prompt"].index("keep it small") < plans[1]["prompt"].index("## Result protocol (ziggy)")
    assert not (env["state"] / "runs" / ticket_id / "nudge.json").exists()
    assert records(env, ticket_id, "plan")[1]["nudge"]["text"] == "keep it small"
    with pytest.raises(pipeline.PipelineError, match="has no active run"):
        pipeline.nudge(target, ticket_id, "too late")


def _one_stage(tmp_path, monkeypatch, **stage):
    """A one-stage `chore` pipeline (implement only) with the given stage keys, loaded in this process."""
    folder = tmp_path / "pipelines"
    folder.mkdir()
    keys = "\n".join(f"{k} = {json.dumps(v) if not isinstance(v, dict) else '{ ' + ', '.join(f'{a} = {json.dumps(b)}' for a, b in v.items()) + ' }'}"
                     for k, v in stage.items())
    (folder / "chore.toml").write_text('name = "chore"\n[[stages]]\nid = "implement"\nprompt = "implement.md"\n'
                                       f'result = "implement"\n{keys}\n')
    monkeypatch.setattr(pipelines, "PIPELINES_DIR", folder)


def test_a_quiet_stage_is_retried_in_its_session_with_backoff_until_retries_run_out(env, tmp_path, monkeypatch):
    _one_stage(tmp_path, monkeypatch, quiet_after="1s", retry={"on_quiet": True, "max": 1, "backoff": "1s"})
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    script(env, {"implement": [{"verdict": "READY", "_sleep": 60, "_sleep_resumed": True}]})
    target = config.load_target("demo")
    started = time.monotonic()
    pipeline.run(target, ticket_id)
    first, second = records(env, ticket_id, "implement")
    assert first["state"] == "killed" and first["kill_reason"] == "nudge:quiet"
    assert first["nudged"]["kind"] == "quiet" and first["nudged"]["text"].startswith("it produced no output for")
    assert first["retry"] == {"cause": "quiet", "n": 1, "max": 1, "delay_s": 1}
    assert second["resumed"] and second["session_id"] == first["session_id"] and second["visit"] == first["visit"] == 1
    assert second["state"] == "killed" and "retry" not in second
    assert second["retry_exhausted"] == "implement went quiet; no retries left (1 per run)"
    prompt = [c for c in calls(env) if c.get("resumed")][0]["prompt"]
    assert "Ziggy stopped you because it produced no output for" in prompt and "run it differently" in prompt
    st = load_state(env, ticket_id)
    assert st["status"] == "failed" and st["error"].startswith("implement: sessiz kaldı (")  # where the time went, then:
    assert st["error"].split("\n")[1] == "implement went quiet; no retries left (1 per run)"
    assert st["run"]["retries"] == {"implement": 1} and "retry_at" not in st
    assert time.monotonic() - started < 50  # interrupted, not waited out
    assert pipeline.resumable(target, ticket_id)["reason"] == "nudge"


@pytest.mark.parametrize("on_timeout", [True, False])
def test_a_stage_past_its_timeout_is_interrupted_and_retried_when_the_policy_says_so(env, tmp_path, monkeypatch, on_timeout):
    _one_stage(tmp_path, monkeypatch, timeout="2s", retry={"on_timeout": on_timeout, "max": 1, "backoff": "1s"})
    ticket_id = cli("new", "chore: Sync pins").stdout.split("\t")[0]
    script(env, {"implement": [{"verdict": "READY", "_sleep": 60}]})
    target = config.load_target("demo")
    pipeline.run(target, ticket_id)
    recs = records(env, ticket_id, "implement")
    first = recs[0]
    assert first["state"] == "timeout" and first["timed_out"] and first["interrupt"] == "timeout" and first["interrupted_at"]
    st = load_state(env, ticket_id)
    if on_timeout:
        assert first["retry"] == {"cause": "timeout", "n": 1, "max": 1, "delay_s": 1}
        assert len(recs) == 2 and recs[1]["state"] == "ok" and st["status"] == "done"
        prompt = [c for c in calls(env) if c.get("resumed")][0]["prompt"]
        assert "Ziggy stopped you because it ran past its timeout (0m)" in prompt
    else:
        assert len(recs) == 1 and "retry" not in first
        # First line: where the time went (stalls.explain); then the engine's own line.
        assert st["status"] == "failed" and st["error"].startswith("implement: zaman aşımı (")
        assert st["error"].split("\n")[1].startswith("implement timeout")
