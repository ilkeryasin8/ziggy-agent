"""The machine's memory (host.py): sysctl's output read into a sample, the busy rule and [host]; no stage starts while
the machine is short of memory (status host, started again after two calm panel ticks in a row; `run --now` starts
anyway); a tool call running past stall_after is an inbox item until it returns (stalls.py); a stage that ran out of
time says where the time went."""
import json
import os
import re
import subprocess
import sys
import time

import pytest
from conftest import BIN, BUSY_HOST, CALM_HOST, calls, cli, script

from ziggy import attention, config, dashboard, host, pipeline, pipelines, stalls, state


def target():
    return config.load_target("demo")


def st(ticket_id):
    return state.load_state(target(), ticket_id)


def records(ticket_id):
    return [state.read_json(d / "run.json") for d in state.phase_dirs(target(), ticket_id)]


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


# --- the sample and the rule ------------------------------------------------------------------------

def test_sysctl_output_is_read_into_a_sample():
    s = host.parse(BUSY_HOST, 16.234)  # a decimal comma, as a Turkish locale prints it
    assert (s["pressure"], s["swap_total"], s["load1"]) == ("critical", 26624 * 1024 ** 2, 16.23)
    assert round(host.swap_share(s), 3) == 0.961 and host.text(s) == "Bellek: kritik · swap %96"
    warn = host.parse("2\ntotal = 4.00G  used = 1.50G  free = 2.50G  (encrypted)\n")
    assert warn["pressure"] == "warn" and warn["swap_used"] == int(1.5 * 1024 ** 3) and host.suffix(warn) == "bellek baskı altında"
    assert host.swap_share(host.parse("1\ntotal = 0.00M  used = 0.00M  free = 0.00M\n")) is None  # no swap file yet
    unknown = host.parse("sysctl: unknown oid 'kern.memorystatus_vm_pressure_level'\n")
    assert unknown["pressure"] == "unknown" and unknown["swap_used"] is None and not host.busy(unknown)
    assert host.phrase(unknown) == "" and host.phrase(host.parse(CALM_HOST)) == "makinenin belleği rahattı"
    assert host.phrase(s) == "bellek baskısı kritikti (swap %96)"
    assert host.worst(host.parse(CALM_HOST), s) is s and host.worst(s, warn) is s


def test_a_sample_is_cached_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ZIGGY_HOST_SAMPLE", raising=False)
    monkeypatch.setattr(host, "_cache", None)
    runs = []
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: runs.append(argv) or subprocess.CompletedProcess(argv, 0, BUSY_HOST, ""))
    assert host.sample()["pressure"] == "critical" and host.sample()["pressure"] == "critical" and len(runs) == 1
    assert runs[0] == ["sysctl", "-n", "kern.memorystatus_vm_pressure_level", "vm.swapusage"]
    host.sample(fresh=True)
    assert len(runs) == 2

    def missing(*a, **kw):
        raise FileNotFoundError("sysctl")
    monkeypatch.setattr(host.subprocess, "run", missing)
    assert host.sample(fresh=True)["pressure"] == "unknown"
    monkeypatch.setenv("ZIGGY_HOST_SAMPLE", str(tmp_path / "gone"))
    assert host.sample()["pressure"] == "unknown"


def test_the_busy_rule_and_its_config(env):
    calm, busy = host.parse(CALM_HOST), host.parse(BUSY_HOST)
    assert not host.busy(calm) and host.busy(busy)
    assert host.busy(host.parse("4\n"))  # critical pressure alone
    most = host.parse("2\ntotal = 1000.00M  used = 850.00M  free = 150.00M\n")  # warn pressure, swap 85 %
    assert not host.busy(most) and host.busy(most, swap_hold=0.8)
    # Full swap at normal pressure is not strain: macOS keeps swap full long after the memory came back.
    assert not host.busy(host.parse("1\ntotal = 1000.00M  used = 990.00M  free = 10.00M\n"))
    assert config.load_host() == config.HostConfig(enabled=True, swap_hold=0.9)
    base = env["config"].read_text()
    env["config"].write_text(base + "[host]\nenabled = false\nswap_hold = 0.8\n")
    assert config.load_host() == config.HostConfig(enabled=False, swap_hold=0.8)
    for bad in ("swap_hold = 1.5", "swap_hold = 0", 'swap_hold = "90%"', "swap_hold = true", 'enabled = "yes"', "hold = 0.9"):
        env["config"].write_text(base + f"[host]\n{bad}\n")
        with pytest.raises(config.ConfigError, match=r"\[host\]"):
            config.load_host()
    env["config"].write_text(base)
    assert target().stall_after == 600
    env["config"].write_text(base.replace('gh_user = "tester"', 'gh_user = "tester"\nstall_after = "15m"'))
    assert target().stall_after == 900
    env["config"].write_text(base.replace('gh_user = "tester"', 'gh_user = "tester"\nstall_after = "soon"'))
    with pytest.raises(config.ConfigError, match="stall_after"):
        target()


# --- admission ---------------------------------------------------------------------------------------

def test_no_stage_starts_while_the_machine_is_short_of_memory_and_two_calm_ticks_start_it(env, monkeypatch):
    env["host"].write_text(BUSY_HOST)
    cli("new", "chore: held one")
    proc = cli("run", "local-1")
    assert proc.returncode == 0, proc.stderr
    assert "local-1: the machine is short of memory (memory critical · swap 96%" in proc.stderr
    s = st("local-1")
    assert s["status"] == host.STATUS and s["next"]["stage"] == "plan" and s["host"]["stage"] == "plan"
    assert s["host"]["sample"]["pressure"] == "critical" and not s.get("worktree")
    assert [c for c in calls(env) if c.get("stage")] == []
    row = dashboard.ticket_row(target(), "local-1")
    assert row["host"]["stage"] == "plan" and [x["state"] for x in row["stages"]][0] == "current"
    assert attention.board_from([row])["waiting_on_others"][0]["id"] == "local-1"
    assert "starts on its own once the machine has memory again" in cli("status", "local-1").stdout
    status = cli("status").stdout
    assert "# host: memory critical · swap 96% (25.0 of 26.0 GB)" in status and "new stages wait" in status
    panel = host.view()
    assert panel["show"] and panel["busy"] and panel["tone"] == "bad" and panel["text"] == "Bellek: kritik · swap %96"

    # The panel's tick: two calm ticks in a row, then one ticket per tick.
    started = []
    monkeypatch.setattr(dashboard, "start_job", lambda t, i, *a: started.append((i, *a)) or {"id": "x"})
    monkeypatch.setattr(host, "_calm", 0)
    assert host.wake([target()]) == []  # still busy
    env["host"].write_text(CALM_HOST)
    assert host.wake([target()]) == [] and started == []  # one calm tick is not enough
    env["host"].write_text(BUSY_HOST)
    assert host.wake([target()]) == []  # busy again: the count starts over
    env["host"].write_text(CALM_HOST)
    assert host.wake([target()]) == [] and started == []
    assert host.wake([target()]) == ["local-1"] and started == [("local-1", "run", "local-1")]
    assert not host.view()["show"]

    proc = cli("run", "local-1")  # what the started job runs
    assert proc.returncode == 0, proc.stderr
    s = st("local-1")
    assert s["status"] == "done" and "host" not in s
    plan = records("local-1")[0]
    assert plan["host"]["start"]["pressure"] == plan["host"]["end"]["pressure"] == "normal" and plan["host"]["worst"]


def test_run_now_starts_anyway_with_a_warning(env):
    env["host"].write_text(BUSY_HOST)
    cli("new", "chore: urgent")
    cli("run", "local-1")
    assert st("local-1")["status"] == host.STATUS
    proc = cli("run", "local-1", "--now")
    assert proc.returncode == 0, proc.stderr
    assert "local-1: warning: the machine is short of memory (memory critical" in proc.stderr
    assert "starting plan anyway (--now)" in proc.stderr and "starting review anyway (--now)" in proc.stderr
    s = st("local-1")
    assert s["status"] == "done" and "host" not in s
    assert [r["host"]["worst"]["pressure"] for r in records("local-1")] == ["critical"] * 3
    env["config"].write_text(env["config"].read_text() + "[host]\nenabled = false\n")
    cli("new", "chore: not held when switched off")
    assert cli("run", "local-2").returncode == 0 and st("local-2")["status"] == "done"


# --- a tool call that takes long -----------------------------------------------------------------------

def test_a_tool_call_past_stall_after_is_an_inbox_item_until_it_returns(env):
    script(env, {"plan": [{"verdict": "READY", "summary": "ok", "_sleep": 4,
                           "_tools": [{"command": "cd /tmp && grep -ril needle ~/Documents", "heartbeats": [320, 650, 700],
                                       "hold": 4}]}]})
    cli("new", "chore: slow grep")
    run = subprocess.Popen([sys.executable, str(BIN), "run", "local-1"], env=os.environ.copy(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, start_new_session=True)
    try:
        wait_for(lambda: state.phase_dirs(target(), "local-1"))
        env["host"].write_text(BUSY_HOST)  # the machine starts swapping while the stage runs
        item = wait_for(lambda: (n := attention.needs(target(), "local-1")) and n["kind"] == "stall" and n)
        assert re.fullmatch(r"Komut 1[0-2] dk’dır sürüyor: grep -ril needle ~/Documents · bellek kritik · swap %96", item["title"])
        assert item["key"].endswith(":toolu_fake_0") and item["severity"] == "medium" and not item["dismissable"]
        assert "Ziggy komutu durdurmaz" in item["details"] and "Makine: Bellek: kritik · swap %96" in item["details"]
        stalled = records("local-1")[0]["stall"]
        assert stalled["tool"] == "Bash" and stalled["seconds"] >= 600 and stalled["host"]["pressure"] == "critical"
        attention.reconcile([target()])
        rows = [r for r in attention._load()["rows"] if r["kind"] == "stall"]
        assert len(rows) == 1 and rows[0]["resolved_at"] is None
        # The call returns while the stage goes on: the item is resolved.
        wait_for(lambda: "stall" not in records("local-1")[0])
        assert (attention.needs(target(), "local-1") or {}).get("kind") != "stall"
        attention.reconcile([target()])
        assert [r for r in attention._load()["rows"] if r["kind"] == "stall"][0]["resolved_at"]
    finally:
        run.wait(timeout=60)
    s = st("local-1")
    assert s["status"] == host.STATUS and s["next"]["stage"] == "implement"  # the next stage waits for memory
    plan = records("local-1")[0]
    assert "stall" not in plan and plan["host"]["worst"]["pressure"] == "critical"
    assert plan["tool_calls"][0]["command"] == "grep -ril needle ~/Documents" and plan["tool_calls"][0]["seconds"] >= 700
    assert not plan["tool_calls"][0]["open"]


# --- where the time went -------------------------------------------------------------------------------

def _use(i, command):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": command}}]}}


def _result(i):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "x"}]}}


def _beat(i, elapsed):
    return {"type": "tool_progress", "tool_use_id": f"{i}-heartbeat-0", "tool_name": "Bash", "parent_tool_use_id": i,
            "elapsed_time_seconds": elapsed, "heartbeat": True}


# A 45-minute stage (seconds from its start, event): a grep that took 25 minutes beside a quick cat, a subagent, an ls.
FIXTURE = [(0, _use("t1", "cd /work && grep -ril needle ~")), (10, _use("t2", "cat a.md")), (40, _result("t2")),
           (330, _beat("t1", 320)), (1300, _beat("t1", 1299)), (1500, _result("t1")),
           (1600, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Task", "input": {}}]}}),
           (1600, _use("t3", "ls")), (1720, _result("t3"))]


def test_a_stage_that_ran_out_of_time_says_where_the_time_went(tmp_path):
    stream = tmp_path / "claude.jsonl"
    clock = [0.0]
    tracked = stalls.Calls(stream, clock=lambda: clock[0])
    seen = {}
    with stream.open("w") as out:
        for at, event in FIXTURE:
            out.write(json.dumps(event) + "\n")
            out.flush()
            clock[0] = at
            tracked.poll()
            seen[at] = tracked.stalled(600)
    assert seen[330] is None and seen[1300]["command"] == "grep -ril needle ~" and seen[1300]["seconds"] == 1300
    assert tracked.stalled(600, now=2700) is None  # every call returned
    slow = tracked.slowest(now=2700)
    assert [(c["command"], c["seconds"]) for c in slow] == [("grep -ril needle ~", 1500), ("ls", 120), ("cat a.md", 30)]
    assert stalls.explain("timeout", 2700, slow, host.parse(BUSY_HOST)) == (
        "zaman aşımı (45 dk): sürenin 27 dk’sı (%60) en yavaş 3 komutu beklerken geçti "
        "(grep -ril needle ~ 25 dk, ls 2 dk, cat a.md 30 sn); bellek baskısı kritikti (swap %96)")
    assert stalls.explain("quiet", 600, [], None) == "sessiz kaldı (10 dk): hiçbir komut uzun sürmedi"


def test_a_timed_out_stage_fails_with_where_its_time_went(env, tmp_path, monkeypatch):
    folder = tmp_path / "pipelines"
    folder.mkdir()
    (folder / "chore.toml").write_text('name = "chore"\n[[stages]]\nid = "implement"\nprompt = "implement.md"\n'
                                       'result = "implement"\ntimeout = "3s"\n')
    monkeypatch.setattr(pipelines, "PIPELINES_DIR", folder)
    cli("new", "chore: Sync pins")
    script(env, {"implement": [{"verdict": "READY", "_sleep": 60,
                                "_tools": [{"command": "grep -ril needle /", "heartbeats": [1], "every": 1, "returns": False}]}]})
    pipeline.run(target(), "local-1")
    s = st("local-1")
    first, engine = s["error"].split("\n")
    assert re.fullmatch(r"implement: zaman aşımı \(\d sn\): sürenin \d sn’si \(%\d+\) bir komutu beklerken geçti "
                        r"\(grep -ril needle / \d sn\); makinenin belleği rahattı", first), first
    assert engine.startswith("implement timeout")
    r = records("local-1")[0]
    assert r["state"] == "timeout" and r["time_report"] == first.split(": ", 1)[1]
    assert r["tool_calls"][0]["open"] and r["host"]["end"]["pressure"] == "normal"
    assert attention.needs(target(), "local-1")["details"].split("\n")[0] == first  # the panel's next step
