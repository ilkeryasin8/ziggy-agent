"""watch, stop and the status after a stop: following a run, ending one, changing course."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from conftest import BIN, cli, script

from ziggy import dispatch

HEADER = re.compile(r"^== (\d{2,})-([a-z][a-z0-9-]*) · .+ ==$")


def new(title="chore: Sync pins"):
    proc = cli("new", title)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("\t")[0]


def results(env, data: dict) -> dict:
    script(env, data)
    return {}


def start_run(ticket_id, **env):
    return subprocess.Popen(
        [sys.executable, str(BIN), "run", ticket_id], env={**os.environ, **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def stage_dir(env, ticket_id, stage):
    found = sorted((env["state"] / "runs" / ticket_id).glob(f"*-{stage}"))
    return found[-1] if found else None


def test_watch_replays_every_stage_session(env):
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    out = cli("watch", ticket_id, "--all").stdout.splitlines()
    headers = [HEADER.match(line)[2] for line in out if HEADER.match(line)]
    assert headers == ["plan", "implement", "review"]
    assert out[-1] == f"{ticket_id}: done"


def test_watch_brief_shows_the_newest_session(env):
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    out = cli("watch", ticket_id, "--brief").stdout.splitlines()
    assert HEADER.match(out[0])[2] == "review"


def test_watch_follows_a_live_run_through_its_stages(env):
    ticket_id = new()
    run = start_run(ticket_id, **results(env, {"plan": [{"verdict": "READY", "_sleep": 1.5}]}))
    try:
        assert wait_for(lambda: stage_dir(env, ticket_id, "plan") is not None)
        out = cli("watch", ticket_id, "--brief").stdout.splitlines()
    finally:
        run.wait(timeout=60)
    print(out)
    headers = [HEADER.match(line)[2] for line in out if HEADER.match(line)]
    assert headers[0] == "plan" and "review" in headers
    assert out[-1] == f"{ticket_id}: done"


def test_stop_ends_the_session_and_retry_finishes(env):
    ticket_id = new()
    run = start_run(ticket_id, **results(env, {"plan": [{"verdict": "READY", "_sleep": 60}, {"verdict": "READY"}]}))
    assert wait_for(lambda: stage_dir(env, ticket_id, "plan") is not None and (stage_dir(env, ticket_id, "plan") / dispatch.PID_FILE).exists())
    plan_dir = stage_dir(env, ticket_id, "plan")
    started = time.monotonic()
    proc = cli("stop", ticket_id)
    assert proc.returncode == 0, proc.stderr
    assert f"stop requested for {plan_dir.name}" in proc.stdout
    run.wait(timeout=30)
    assert time.monotonic() - started < 20
    assert run.returncode == 1, run.stderr.read() + run.stdout.read()

    st = json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())
    assert st["status"] == "failed" and st["next"]["stage"] == "plan"
    record = json.loads((plan_dir / "run.json").read_text())
    assert record["stopped"] is True and record["state"] == "killed" and not (plan_dir / dispatch.PID_FILE).exists()
    assert cli("stop", ticket_id).returncode == 2  # nothing running any more
    status = cli("status", ticket_id).stdout
    assert "running: no" in status and f"ziggy run {ticket_id}" in status

    assert cli("run", ticket_id).returncode == 0


def test_stopped_run_takes_a_new_direction(env):
    ticket_id = new()
    run = start_run(ticket_id, **results(env, {"implement": [{"verdict": "READY", "_sleep": 60}, {"verdict": "READY"}]}))
    assert wait_for(lambda: stage_dir(env, ticket_id, "implement") is not None
                    and (stage_dir(env, ticket_id, "implement") / dispatch.PID_FILE).exists())
    assert cli("stop", ticket_id).returncode == 0
    run.wait(timeout=30)

    proc = cli("answer", ticket_id, "--text", "Keep the Windows file; bump only the other three.")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = env["state"] / "runs" / ticket_id
    assert json.loads((root / "state.json").read_text())["status"] == "done"
    assert "Keep the Windows file" in (root / "inbox" / "plan.md").read_text()


def test_stop_mid_edit_stashes_the_half_done_work(env):
    ticket_id = new()
    run = start_run(ticket_id, **results(env, {"implement": [{"verdict": "READY", "_dirty": "half-done.txt", "_sleep": 60},
                                                             {"verdict": "READY"}]}))
    st_path = env["state"] / "runs" / ticket_id / "state.json"
    assert wait_for(lambda: (json.loads(st_path.read_text()).get("worktree") or "")
                    and (Path(json.loads(st_path.read_text())["worktree"]) / "half-done.txt").exists())
    wt = Path(json.loads(st_path.read_text())["worktree"])
    assert cli("stop", ticket_id).returncode == 0
    run.wait(timeout=30)

    record = json.loads((stage_dir(env, ticket_id, "implement") / "run.json").read_text())
    assert record["stopped"] is True and record["stash"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "stash", "list"], cwd=wt, capture_output=True, text=True).stdout.strip()
    assert cli("run", ticket_id).returncode == 0
