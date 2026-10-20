"""Subscription quota: the sessions' rate_limit_events feed a ledger per Claude login; a stage whose session hits the
limit does not fail but waits (status `quota`) and resumes the same session when the window resets, also after a
panel restart; no stage starts on a login that is over its limit (other logins run on; `run --now` starts anyway);
a usage-limit error without the event is caught from its text; `status`, the panel and the inbox show it."""
import json
import time

import pytest
from conftest import calls, cli, script

from ziggy import attention, config, dashboard, dispatch, pipeline, quota, state

SOON, LATER = int(time.time()) + 3 * 3600, int(time.time()) + 5 * 86400  # windows that have not reset yet
ALLOWED = {"status": "allowed", "resetsAt": SOON, "rateLimitType": "five_hour", "overageStatus": "rejected",
           "overageDisabledReason": "out_of_credits", "isUsingOverage": False,
           "unifiedWindows": {"five_hour": {"utilization": 0.18, "resetsAt": SOON},
                              "seven_day": {"utilization": 0.11, "resetsAt": LATER}}}


@pytest.fixture
def logins(env, tmp_path, monkeypatch):
    """Two Claude logins: the default one (demo) and ~/.claude-work (a second target, `work`, on the same repo)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.test"}}))
    work = tmp_path / "claude-work"
    work.mkdir()
    (work / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "work@example.test"}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    env["config"].write_text(env["config"].read_text() + f'[targets.work]\npath = "{env["repo"]}"\n'
                             f'[targets.work.env]\nCLAUDE_CONFIG_DIR = "{work}"\n')
    return {"default": quota.DEFAULT, "work": str(work.resolve())}


def target(name="demo"):
    return config.load_target(name)


def st(ticket_id, name="demo"):
    return state.load_state(target(name), ticket_id)


def stage_calls(env, ticket_id):
    return [c for c in calls(env) if c.get("stage") and c["env"]["PIPELINE_TICKET"] == ticket_id]


def wait_until(check, timeout=120, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


# --- the ledger ---------------------------------------------------------------------------------------

def test_the_ledger_keeps_the_latest_event_per_login(logins, tmp_path):
    assert quota.account_key(target()) == "default" and quota.account_key(target("work")) == logins["work"]
    quota.record(logins["default"], ALLOWED)
    quota.record(logins["work"], {**ALLOWED, "status": "allowed_warning", "utilization": 0.91,
                                  "unifiedWindows": {"five_hour": {"utilization": 0.91, "resetsAt": SOON}}})
    data = json.loads((tmp_path / "xdg" / "ziggy" / "quota.json").read_text())
    me, work = data["accounts"]["default"], data["accounts"][logins["work"]]
    assert me["label"] == "me@example.test" and me["status"] == "allowed" and me["type"] == "five_hour"
    assert me["windows"] == {"five_hour": {"utilization": 0.18, "resets_at": SOON},
                             "seven_day": {"utilization": 0.11, "resets_at": LATER}}
    assert me["updated_at"] and "blocked_until" not in me
    assert work["label"] == "work@example.test" and work["status"] == "allowed_warning"

    # A stream with other events around them: the feed records the latest and notes a rate-limit turn error.
    stream = tmp_path / "claude.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init"},
        {"type": "rate_limit_event", "rate_limit_info": {**ALLOWED, "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": SOON}}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "rate_limit is a word here"}]}},
        {"type": "assistant", "error": "rate_limit", "message": {"content": []}})) + "\n")
    feed = quota.Feed(logins["default"], stream)
    feed.poll()
    assert feed.last["unifiedWindows"]["five_hour"]["utilization"] == 0.5 and feed.rate_limited
    assert quota.entry("default")["windows"]["five_hour"]["utilization"] == 0.5
    assert quota.entry("default")["windows"]["seven_day"]["utilization"] == 0.11  # windows the event left out stay


def test_a_stage_feeds_the_ledger_and_status_and_the_panel_show_it(logins, env):
    script(env, {"plan": [{"verdict": "READY", "_rate_limit": ALLOWED}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    record = json.loads((env["state"] / "runs" / "local-1" / "01-plan" / "run.json").read_text())
    assert record["account"] == "default" and record["rate_limit"]["status"] == "allowed"
    assert quota.entry("default")["windows"]["five_hour"]["utilization"] == 0.18

    out = cli("status").stdout
    assert "# quota me@example.test (demo): 5h 18% · 7d 11%" in out
    assert "# quota work@example.test (work): no rate-limit event seen yet" in out

    rows = {r["key"]: r for r in dashboard.snapshot(time.time())["quota"]}
    me = rows["default"]
    assert me["targets"] == ["demo"] and me["tone"] == "ok" and not me["blocked"]
    assert [(w["label"], w["percent"], w["tone"]) for w in me["windows"]] == [("5 sa", 18, "ok"), ("7 gün", 11, "ok")]
    quota.record("default", {**ALLOWED, "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": SOON}}})
    me = {r["key"]: r for r in dashboard.snapshot(time.time())["quota"]}["default"]
    assert me["tone"] == "warn" and me["windows"][0]["tone"] == "warn"
    assert rows[logins["work"]]["tone"] == "muted" and rows[logins["work"]]["windows"] == []


# --- a session that hits the limit -----------------------------------------------------------------------

def test_a_rejected_stage_waits_and_resumes_its_session_when_the_window_resets(logins, env, monkeypatch):
    # An hour away, so the real clock cannot reach the reset while the first half asserts the block; the second
    # half moves quota.now itself.
    script(env, {"implement": [{"verdict": "READY", "_rejected_in": 3600, "_commit": "{ticket}.txt"}]})
    cli("new", "chore: tidy")
    proc = cli("run", "local-1")
    assert proc.returncode == 0, proc.stderr
    assert "hit me@example.test's usage limit; it continues in its session at" in proc.stderr
    s = st("local-1")
    q = s["quota"]
    assert s["status"] == "quota" and s["run"]["outcome"] == "quota" and not s.get("error")
    assert q["reason"] == "rejected" and q["stage"] == "implement" and q["account"] == "default" and q["window"] == "five_hour"
    assert q["label"] == "me@example.test" and q["phase_dir"] == "02-implement"
    assert quota.epoch_of(q["resume_at"]) == q["resets_at"] + quota.GRACE_S
    first = stage_calls(env, "local-1")
    assert [c["stage"] for c in first] == ["plan", "implement"]
    session = first[1]["argv"][first[1]["argv"].index("--session-id") + 1]
    assert s["next"]["resume"]["session_id"] == session and s["next"]["stage"] == "implement"
    record = json.loads((env["state"] / "runs" / "local-1" / "02-implement" / "run.json").read_text())
    assert record["state"] == "quota" and record["quota_error"].startswith("You've hit your session limit")
    assert record["quota_hit"]["source"] == "event" and "stash" not in record  # a half-done edit stays in the tree
    assert not pipeline.counts_as_visit(record)
    assert [v["state"] for v in pipeline.stages_view(target(), "local-1")][:2] == ["done", "current"]

    # The login is blocked until the window resets; the inbox says so once for the login.
    blocked = quota.blocked("default")
    assert blocked and blocked["blocked_until"] == q["resets_at"] and blocked["blocked_window"] == "five_hour"
    item = next(r for r in attention.items([target(), target("work")]) if r["kind"] == "quota")
    assert item["title"] == f"Kota doldu: me@example.test · {quota.clock(q['resets_at'])}" and item["ticket"] == "quota"
    assert item["target"] == "demo"
    assert attention.needs(target(), "local-1") is None  # nothing for a person to do on the ticket
    assert dashboard.ticket_row(target(), "local-1")["quota"]["reason"] == "rejected"
    assert dashboard.ticket_row(target(), "local-1")["needs_you"] is None
    assert "continues on its own at" in cli("status", "local-1").stdout

    # The panel restarts; its first ticks find the hold on disk. Not before resume_at, then once.
    started = []
    monkeypatch.setattr(dashboard, "start_job", lambda t, i, *a: started.append((i, *a)) or {"id": "x"})
    resume_at = quota.epoch_of(q["resume_at"])
    monkeypatch.setattr(quota, "now", lambda: resume_at - 5)
    assert quota.wake(target()) == [] and started == []
    monkeypatch.setattr(quota, "now", lambda: resume_at + 1)
    assert quota.wake(target()) == ["local-1"] and started == [("local-1", "run", "local-1")]

    # The job the tick starts: `run` continues the same session, as the same visit, with a short note.
    pipeline.run(target(), "local-1")
    second = stage_calls(env, "local-1")[2]
    assert second["resumed"] and second["argv"][second["argv"].index("--resume") + 1] == session
    assert second["prompt"].startswith("Ziggy paused you because your Claude account's usage limit was reached; "
                                       "the limit has reset now. Your last turn was interrupted")
    s = st("local-1")
    assert s["status"] == "done" and "quota" not in s and s["run"]["visits"]["implement"] == 1
    attention.reconcile([target(), target("work")])
    assert not [r for r in attention.items([target(), target("work")]) if r["kind"] == "quota"]  # resolved after reset


def test_a_usage_limit_message_without_the_event_is_caught_from_its_text(logins, env):
    reset = int(time.time()) + 7200
    script(env, {"plan": [{"verdict": "READY", "_limit_text": f"Claude AI usage limit reached|{reset}"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    s = st("local-1")
    assert s["status"] == "quota" and s["quota"]["resets_at"] == reset and s["quota"]["window"] is None
    record = json.loads((env["state"] / "runs" / "local-1" / "01-plan" / "run.json").read_text())
    assert record["quota_error"] == f"Claude AI usage limit reached|{reset}" and record["quota_hit"]["source"] == "text"
    assert quota.blocked("default")["blocked_until"] == reset

    # Another error is a failure as before.
    script(env, {"plan": [{"verdict": "READY", "_limit_text": "Internal server error"}]})
    cli("new", "chore: other")
    assert cli("run", "local-2", "--now").returncode == 1
    assert st("local-2")["status"] == "failed" and "quota" not in st("local-2")


def test_reset_times_are_read_from_the_messages():
    at = 1792022400  # 2026-10-15 00:00 UTC
    assert quota.parse_reset("Claude AI usage limit reached|1792112400", at) == 1792112400
    # 8pm Istanbul (UTC+3) is 17:00 UTC the same day.
    assert quota.parse_reset("You've hit your session limit · resets 8pm (Europe/Istanbul)", at) == at + 17 * 3600
    assert quota.parse_reset("You've hit your weekly limit · resets Oct 20, 3:30pm (UTC)", at) == at + 5 * 86400 + 15.5 * 3600
    assert quota.parse_reset("something else", at) is None
    assert quota.rejection({"is_error": True, "final_message": "You've hit your session limit · resets 8pm (Europe/Istanbul)"})
    assert quota.rejection({"is_error": True, "final_message": "Tests failed"}) is None
    assert quota.rejection({"is_error": False, "final_message": "I added rate limiting to the API"}) is None
    assert quota.rejection({"is_error": True, "api_error_status": 429, "final_message": "Too many requests"})["source"] == "text"
    # The inbox says when the window resets with the Turkish locative, read by the time's last word.
    assert [quota.locative(t) for t in ("15:05", "15:30", "15:00", "20:00", "17.10 01:39", "09:40")] == \
        ["15:05'te", "15:30'da", "15:00'te", "20:00'de", "17.10 01:39'da", "09:40'ta"]


# --- admission ---------------------------------------------------------------------------------------

def test_no_stage_starts_on_a_blocked_login_while_another_login_runs(logins, env):
    until = int(time.time()) + 3600
    ok = [{"verdict": "READY", "summary": "ok", "_rate_limit": ALLOWED}]
    script(env, {"plan": ok, "implement": ok, "review": ok})
    quota.block("default", until, "five_hour", "rejected")
    cli("new", "chore: blocked one")
    proc = cli("run", "local-1")
    assert proc.returncode == 0
    assert "me@example.test is over its usage limit; plan starts at" in proc.stderr
    s = st("local-1")
    assert s["status"] == "quota" and s["quota"]["reason"] == "admission" and s["quota"]["stage"] == "plan"
    assert quota.epoch_of(s["quota"]["resume_at"]) == until + quota.GRACE_S and s["next"]["stage"] == "plan"
    assert stage_calls(env, "local-1") == [] and not s.get("worktree")
    assert dashboard.ticket_row(target(), "local-1")["quota"]["reason"] == "admission"
    assert "starts on its own at" in cli("status", "local-1").stdout
    assert quota.wake(target()) == []  # not due yet

    # The other login is not held.
    assert cli("--target", "work", "new", "chore: other login", "--run").returncode == 0
    assert st("local-1", "work")["status"] == "done"

    # A manual start goes ahead, with a warning.
    proc = cli("run", "local-1", "--now")
    assert proc.returncode == 0, proc.stderr
    assert "warning: me@example.test is over its usage limit until" in proc.stderr and "starting anyway" in proc.stderr
    assert st("local-1")["status"] == "done" and "quota" not in st("local-1")


def test_a_held_ticket_starts_when_an_allowed_session_shows_the_block_is_over(logins, env, monkeypatch):
    until = int(time.time()) + 3600
    quota.block("default", until, "five_hour", "rejected")
    cli("new", "chore: held")
    cli("run", "local-1")
    assert st("local-1")["status"] == "quota" and not quota.due(st("local-1"))
    # An event from a request already in flight when the block came does not clear it; a newer session's does, and
    # so does one that reports a new window.
    same_window = {**ALLOWED, "unifiedWindows": {"five_hour": {"utilization": 0.99, "resetsAt": until}}}
    quota.record("default", same_window, since=time.time() - 3600)
    assert quota.blocked("default")
    quota.record("default", same_window, since=time.time() + 1)
    assert not quota.blocked("default") and quota.due(st("local-1"))
    quota.block("default", until, "five_hour", "rejected")
    quota.record("default", {**ALLOWED, "unifiedWindows": {"five_hour": {"utilization": 0.01, "resetsAt": until + 18000}}})
    assert not quota.blocked("default")


def test_the_panel_offers_run_now_on_a_held_ticket(logins, env, monkeypatch):
    quota.block("default", int(time.time()) + 3600, "five_hour", "rejected")
    cli("new", "chore: held")
    cli("run", "local-1")
    started = []
    monkeypatch.setattr(dashboard, "start_job", lambda t, i, *a: started.append(a) or {"id": "x"})
    dashboard.do_action(target(), "local-1", "run", opts={"now": True})
    assert started == [("run", "local-1", "--now")]
    assert dispatch.nudge_resume_prompt({"kind": "quota", "text": "the limit"}, "do it").startswith("Ziggy paused you because the limit")
