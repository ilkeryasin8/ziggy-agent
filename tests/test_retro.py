"""The retro: the evidence a done ticket leaves, the session (fake claude as stage `retro`), and the memory
candidates it proposes (rules, decisions, pitfalls); the panel and the inbox around them."""
import json
import threading
import time

import pytest
from conftest import calls, script

from ziggy import attention, config, dashboard, feedback, memory, pipeline, retro, state, ticket

RULE = "Run `make schema` after changing `api/models/`; the generated client is committed."


@pytest.fixture
def demo(env):
    """Ticket local-1, done, with a worktree directory (no review yet)."""
    target = config.load_target("demo")
    t = ticket.create(target, "Add the weekly report", "feat", "Weekly totals.", False, "low")
    wt = env["state"] / "worktrees" / t["id"]
    wt.mkdir(parents=True)
    st = state.load_state(target, t["id"])
    st.update(status="done", worktree=str(wt), run={"n": 1})
    state.save_state(target, t["id"], st)
    return {"target": target, "id": t["id"], "run": state.run_dir(target, t["id"]), "wt": wt}


def review_run(demo, name, findings):
    d = demo["run"] / name
    d.mkdir()
    state.write_json(d / "run.json", {"stage": "review", "state": "ok", "started_at": state.now(),
                                      "result": {"verdict": "READY", "findings": findings, "fixedIds": []}})


def finding(fid, disposition="fix", summary="", reason=None):
    return {"id": fid, "severity": "medium", "disposition": disposition, "summary": summary or f"{fid} problem",
            "location": "api/models/report.py:12", **({"reason": reason} if reason else {})}


def comments(demo, items):
    (demo["run"] / "review").mkdir(exist_ok=True)
    state.write_json(demo["run"] / "review" / "review.json", {"comments": items, "rounds": [], "viewed": {}})


def comment(cid, round_, body, **extra):
    return {"id": cid, "round": round_, "path": None, "line": None, "body": body, "finding_id": None, "finding_stage": None,
            "finding_action": None, "created_at": f"2026-10-16T10:00:0{cid[-1]}+00:00", **extra}


def with_evidence(demo):
    """A review that dismissed F1 which the user sent to fix: the smallest signal."""
    review_run(demo, "03-review", [finding("F1", "dismiss", "Totals ignore the time zone", reason="UTC everywhere")])
    comments(demo, [comment("c1", 1, "The report runs in Europe/Istanbul; this is real.", finding_id="F1",
                            finding_stage="03-review", finding_action="fix")])


def retro_result(env, items=None, verdict="READY"):
    script(env, {"retro": [{"verdict": verdict, "summary": "lessons", "memory": items if items is not None else [
        {"kind": "kural", "title": "Regenerate the client after model changes", "rule": RULE,
         "why": "The review dismissed a schema drift the user had to send back.", "paths": ["api/models/**"],
         "topics": ["schema"], "sources": [{"ticket": "local-1", "ref": "R1-1"}]}]}]})


def retro_calls(env):
    return [c for c in calls(env) if c.get("stage") == "retro"]


def test_no_signal_means_no_session(env, demo, monkeypatch):
    monkeypatch.delenv("ZIGGY_RETRO")
    review_run(demo, "03-review", [finding("F1"), finding("F2", "dismiss")])  # one round, nothing sent back
    assert retro.evidence(demo["target"], demo["id"]) == []  # no github/feedback.json: tolerated
    assert retro.schedule(demo["target"], demo["id"]) is None
    assert retro.run(demo["target"], demo["id"])["state"] == "skipped"
    assert retro_calls(env) == [] and not retro.jobs_dir(demo["target"], demo["id"]).exists()


def test_evidence_from_reviews_answers_notes_and_github(demo):
    review_run(demo, "03-review", [finding("F1", "dismiss", "Totals ignore the time zone", reason="UTC everywhere"),
                                   finding("F2", "fix", "Missing empty-week test")])
    review_run(demo, "05-review", [finding("F2", "defer", "Still no empty-week test"), finding("F3")])
    comments(demo, [
        comment("c1", 1, "Istanbul time, this is real.", finding_id="F1", finding_stage="03-review", finding_action="fix"),
        comment("c2", 1, "Use the existing `fmt_money` helper.", path="report.py", line=40, line_text="f'{x:.2f} TL'"),
        comment("c3", 1, "", finding_id="F3", finding_stage="05-review", finding_action="fix"),  # a bare "fix it": no text
        comment("c4", None, "an unsent draft"),
    ])
    st = state.load_state(demo["target"], demo["id"])
    st["rounds"] = [{"n": 1, "stage": "plan", "questions": [
        {"id": "q1", "text": "Which week start?", "options": ["Monday", "Sunday"], "recommendation": "Monday"},
        {"id": "q2", "text": "CSV export too?", "options": ["yes", "no"], "recommendation": "no"},
        {"id": "q3", "text": "Cache it?", "options": [], "recommendation": "A) yes, 1 hour"}],
        "answer": "q1: Sunday, our customers are in the US\n\nq2: no\n\nq3: a", "by": "me"}]
    st["answers"] = [{"at": state.now(), "text": "Drop the PDF export, it is out of scope.", "by": "me", "via": "panel"}]
    state.save_state(demo["target"], demo["id"], st)
    # feedback.json as feedback.py writes it: sent items carry sent_round (and fixed_sha after the round); the rest
    # (an unsent comment, an unsent CI failure) is not evidence.
    (demo["run"] / "github").mkdir()
    state.write_json(feedback.path(demo["target"], demo["id"]), {"items": [
        {"id": "rc-991", "kind": "review_comment", "author": "ayse", "body": "N+1 query here", "path": "report.py", "line": 7,
         "url": "https://github.com/acme/demo/pull/3#discussion_r991", "created_at": "2026-10-16T09:00:00Z",
         "sent_round": 1, "sent_at": state.now(), "fixed_sha": "a" * 40},
        {"id": "ci-abc1234def56-tests", "kind": "ci", "author": "tests", "body": "FAILED test_report.py::test_week - KeyError",
         "url": "https://github.com/acme/demo/actions/runs/1/job/2", "created_at": "2026-10-16T09:01:00Z",
         "conclusion": "failure", "sha": "abc1234def56", "sent_round": 1, "sent_at": state.now()},
        {"id": "ic-992", "kind": "issue_comment", "author": "ayse", "body": "Nice work overall", "url": "u",
         "created_at": "2026-10-16T09:02:00Z"},
        {"id": "ci-abc1234def56-lint", "kind": "ci", "author": "lint", "body": "E501", "url": "u", "created_at": "2026-10-16T09:03:00Z"}],
        "last_sync": state.now(), "head_sha": "abc1234def56"})

    items = {i["ref"]: i for i in retro.evidence(demo["target"], demo["id"])}
    assert set(items) == {"R1-1", "U1-2", "R1-2", "Q1-q1", "N1", "GH-rc-991", "GH-ci-abc1234def56-tests"}
    assert items["R1-1"]["kind"] == "overruled_finding" and items["R1-1"]["review_reason"] == "UTC everywhere"
    assert items["R1-1"]["user_reply"] == "Istanbul time, this is real."
    assert items["U1-2"] == {"ref": "U1-2", "kind": "user_comment", "text": "Use the existing `fmt_money` helper.",
                             "where": "report.py:40", "line": "f'{x:.2f} TL'"}
    assert items["R1-2"]["kind"] == "recurring_finding" and items["R1-2"]["rounds"] == [1, 2]
    assert items["R1-2"]["dispositions"] == ["fix", "defer"] and items["R1-2"]["text"] == "Still no empty-week test"
    assert items["Q1-q1"]["recommendation"] == "Monday" and items["Q1-q1"]["text"].startswith("Sunday")
    assert items["N1"]["kind"] == "plan_note"
    assert items["GH-rc-991"] == {"ref": "GH-rc-991", "kind": "github", "source": "review_comment", "author": "ayse",
                                  "text": "N+1 query here", "url": "https://github.com/acme/demo/pull/3#discussion_r991",
                                  "github_round": 1, "fixed": True, "where": "report.py:7"}
    assert items["GH-ci-abc1234def56-tests"]["source"] == "ci" and items["GH-ci-abc1234def56-tests"]["fixed"] is False


def test_evidence_from_checks_failed_runs_and_intent_gaps(demo):
    def record(name, **data):
        d = demo["run"] / name
        d.mkdir()
        state.write_json(d / "run.json", {"state": "ok", "verdict": "READY", **data})
    record("02-implement", stage="implement", run=1, head_after="aaa", checks=[{"name": "tests", "status": "fail", "detail": "1 failed"}],
           finished_at="2026-10-16T10:00:00+00:00")
    record("03-implement", stage="implement", run=1, head_after="bbb", checks=[{"name": "tests", "status": "pass"}])
    record("04-review", stage="review", run=1, state="timeout", result_error="review ran past its timeout",
           finished_at="2026-10-16T11:00:00+00:00")
    record("05-review", stage="review", run=2, started_at="2026-10-16T12:00:00+00:00")
    st = state.load_state(demo["target"], demo["id"])
    st["answers"] = [{"at": "2026-10-16T11:30:00+00:00", "text": "Review only the API folder.", "by": "me", "via": "panel"}]
    st["rounds"] = [{"n": 1, "stage": "review", "questions": [{"id": "q1", "text": "Deletes for everyone?"}],
                     "answer": "Only the owner's rows.", "intent_gap": {"attempt": 1, "stage": "review",
                                                                       "summary": "It deletes every user's report", "evidence": "api.py:3"}}]
    state.save_state(demo["target"], demo["id"], st)
    items = {i["ref"]: i for i in retro.evidence(demo["target"], demo["id"])}
    assert items["C-02-implement-tests"] == {"ref": "C-02-implement-tests", "kind": "check_fixed", "text": "tests fail: 1 failed",
                                             "failed_in": "02-implement", "passed_in": "03-implement"}
    assert items["F1"]["kind"] == "run_recovered" and items["F1"]["text"] == "review: review ran past its timeout"
    assert items["F1"]["notes"] == ["Review only the API folder."]
    assert items["I1"] == {"ref": "I1", "kind": "intent_gap", "stage": "review", "text": "It deletes every user's report",
                           "evidence": "api.py:3", "answer": "Only the owner's rows."}


def test_answers_split_per_question():
    assert retro.answers_by_question("q1: Sunday\n\nQ2) no", ["q1", "q2"]) == {"q1": "Sunday", "q2": "no"}
    assert retro.answers_by_question("Keep Windows out.", ["q1"]) == {"q1": "Keep Windows out."}
    assert retro.against_recommendation("Monday", "Sunday please")
    assert not retro.against_recommendation("Monday", "monday.") and not retro.against_recommendation("Monday", "ok")
    assert not retro.against_recommendation("B) keep it", "b") and not retro.against_recommendation("", "anything")


def test_retro_proposes_rules_decisions_and_pitfalls(env, demo):
    with_evidence(demo)
    mem = demo["target"].state_dir / "memory"
    mem.mkdir(parents=True)
    (mem / "rules.md").write_text("# Kurallar\n\n## M-4 · kural · Vendor is read-only\n- Kural: Never touch `vendor/`.\n")
    src = [{"ticket": "local-1", "ref": "R1-1"}, {"ticket": "local-9", "ref": "R1-1"}]
    retro_result(env, [
        {"kind": "kural", "title": "Regenerate the client", "rule": RULE, "why": "Schema drift was sent back.",
         "paths": ["api/models/**"], "topics": ["schema"], "sources": src},
        {"kind": "karar", "title": "Reports use Europe/Istanbul", "decision": "Compute weekly totals in Europe/Istanbul.",
         "why": "The customers are there.", "paths": ["api/report/**"], "topics": ["timezone"], "sources": src[:1]},
        {"kind": "tuzak", "title": "UTC totals look right in tests", "happens": "Tests run at noon UTC, so the day edge never shows.",
         "todo": "Pin a test at 23:30 Istanbul time.", "topics": ["timezone", "tests"], "sources": src[:1]},
        {"kind": "tuzak", "title": "Half a pitfall", "happens": "no advice", "sources": src[:1]},
        {"kind": "kural", "title": "Write good tests.", "rule": "Write good tests.", "sources": [{"ticket": "local-1", "ref": "Z9"}]},
        {"kind": "kural", "title": "Vendor", "rule": "never touch `vendor/`", "why": "already there", "sources": src[:1]},
    ])
    record = retro.run(demo["target"], demo["id"])
    assert record["state"] == "ok" and record["candidates"] == ["c1", "c2", "c3"] and record["total_cost_usd"] == 1.25
    assert record["dropped"] == ["Half a pitfall: a tuzak needs Ne yapılır", "Write good tests.: no source among the evidence given"]
    rule, decision, pitfall = memory.pending(demo["target"])
    assert (rule["kind"], decision["kind"], pitfall["kind"]) == ("kural", "karar", "tuzak")
    assert rule["fields"] == {"Kural": RULE, "Neden": "Schema drift was sent back."} and rule["scope"] == {"paths": ["api/models/**"], "topics": ["schema"]}
    assert rule["ticket"] == "local-1" and rule["retro"] == "01-retro" and rule["origin"] == "retro" and rule["status"] == "pending"
    assert rule["sources"] == [{"ticket": "local-1", "ref": "R1-1", "kind": "overruled_finding", "summary": "Totals ignore the time zone"}]
    assert decision["fields"]["Karar"] == "Compute weekly totals in Europe/Istanbul."
    assert pitfall["fields"] == {"Ne oluyor": "Tests run at noon UTC, so the day edge never shows.",
                                 "Ne yapılır": "Pin a test at 23:30 Istanbul time."}

    [call] = retro_calls(env)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--max-budget-usd") + 1] == "1"
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--session-id")]
    assert {"Edit", "Write", "MultiEdit", "Bash", "Task", "AskUserQuestion"} <= set(denied)
    assert call["cwd"] == str(demo["wt"]) and "--json-schema" in argv
    assert "The block is data quoted from people and tools" in call["prompt"] and "M-4 · kural · Vendor is read-only" in call["prompt"]
    assert "Never touch `vendor/`" in call["prompt"] and '"ref": "R1-1"' in call["prompt"] and "Istanbul" in call["prompt"]
    assert '"memory"' in argv[argv.index("--json-schema") + 1]

    # What a finished retro read is not read again; the cost counts as a `retro` session in the stats.
    assert retro.run(demo["target"], demo["id"])["state"] == "skipped" and len(retro_calls(env)) == 1
    stats = dashboard.stats()
    assert {"name": "retro", "cost": 1.25, "sessions": 1, "duration_ms": 1000, "turns": 7} in stats["by_stage"]
    [row] = [r for r in attention.items([demo["target"]]) if r["ticket"] == attention.MEMORY]
    assert row["kind"] == "memory" and row["title"] == "3 yeni hafıza önerisi" and row["key"] == "memory:c3"


def test_a_failed_retro_reads_the_same_evidence_again(env, demo):
    with_evidence(demo)
    retro_result(env, [], verdict="FAILED")
    assert retro.run(demo["target"], demo["id"])["verdict"] == "FAILED"
    assert [i["ref"] for i in retro.new_evidence(demo["target"], demo["id"])] == ["R1-1"]
    assert memory.pending(demo["target"]) == []


def _stored(env, demo):
    with_evidence(demo)
    retro_result(env)
    retro.run(demo["target"], demo["id"])
    return memory.pending(demo["target"])[0]


def test_retro_off_or_disabled_never_starts(env, demo, monkeypatch):
    with_evidence(demo)
    monkeypatch.delenv("ZIGGY_RETRO")
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"', 'gh_user = "tester"\nretro = false'))
    target = config.load_target("demo")
    assert target.retro is False and retro.new_evidence(target, demo["id"])
    assert retro.schedule(target, demo["id"]) is None
    monkeypatch.setenv("ZIGGY_RETRO", "off")
    assert retro.schedule(demo["target"], demo["id"]) is None
    for bad in ('retro = "yes"', 'retro_model = "bad model!"'):
        env["config"].write_text(env["config"].read_text().replace("retro = false", bad))
        with pytest.raises(config.ConfigError):
            config.load_target("demo")
        env["config"].write_text(env["config"].read_text().replace(bad, "retro = false"))


def test_done_starts_the_retro_in_the_background(env, monkeypatch):
    monkeypatch.delenv("ZIGGY_RETRO")
    script(env, {
        "implement": [{"verdict": "READY", "_commit": "a.txt"}, {"verdict": "READY", "_commit": "b.txt"}],
        "review": [{"verdict": "READY", "route": "fix", "findings": [finding("F1", summary="No index on report_date")]},
                   {"verdict": "READY", "route": "converged", "converged": True,
                    "findings": [finding("F1", "defer", summary="Still no index on report_date")]}],
        "retro": [{"verdict": "READY", "summary": "one rule", "memory": [
            {"kind": "kural", "title": "Index report_date queries", "rule": "Add an index with every new query on `report_date`.",
             "why": "Two rounds missed it.", "sources": [{"ticket": "local-1", "ref": "R1-1"}]}]}],
    })
    target = config.load_target("demo")
    t = ticket.create(target, "Weekly report", "chore", "", False, "low")
    pipeline.run(target, t["id"])
    assert state.load_state(target, t["id"])["status"] == "done"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not memory.pending(target):
        time.sleep(0.2)
    [c] = memory.pending(target)
    assert c["sources"][0]["ref"] == "R1-1" and c["sources"][0]["kind"] == "recurring_finding"
    [job] = dashboard.read_jobs(retro.jobs_dir(target, t["id"]))
    assert job["command"] == f"retro {t['id']}"
    assert dashboard.jobs(target, t["id"]) == []  # never a ticket job: publish is not held up


def test_panel_lists_decides_and_forgets(env, demo):
    from test_dashboard import request
    c = _stored(env, demo)
    server = dashboard.Panel(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, out, _ = request(server, "GET", "/api/memory")
        [view] = out["targets"]
        assert code == 200 and [p["id"] for p in view["pending"]] == [c["id"]] and view["source"] == "state"
        assert view["dir"] == str(demo["target"].state_dir / "memory") and view["hint"] is None
        _, snap, _ = request(server, "GET", "/api/status")
        assert snap["memory_pending"] == 1 and snap["targets"][0]["memory_pending"] == 1
        assert request(server, "POST", "/api/attention/seen", {"target": "demo", "memory": True})[1]["changed"] == 1
        for bad in ({"action": "reject", "text": "x"}, {"action": "approve", "replace": "yes"}, {"action": "approve", "title": 3}):
            assert request(server, "POST", f"/api/memory/demo/candidates/{c['id']}", bad)[0] == 400
        code, out, _ = request(server, "POST", f"/api/memory/demo/candidates/{c['id']}",
                               {"action": "approve", "text": "Edited rule.", "title": "Edited title"})
        assert code == 200 and out["candidate"]["status"] == "approved" and out["candidate"]["entry"] == "M-1"
        assert out["candidate"]["approved_fields"]["Kural"] == "Edited rule." and out["candidate"]["edited"]
        again = request(server, "POST", f"/api/memory/demo/candidates/{c['id']}", {"action": "approve"})
        assert again[0] == 409 and "already approved" in again[1]["error"]
        assert request(server, "POST", "/api/memory/demo/candidates/../x", {"action": "approve"})[0] == 404
        assert request(server, "POST", "/api/memory/demo/candidates/c99", {"action": "approve"})[0] == 404
        rules_md = (demo["target"].state_dir / "memory" / "rules.md").read_text()
        assert rules_md.count("## M-1 · kural · Edited title\n- Kural: Edited rule.\n") == 1
        _, out, _ = request(server, "GET", "/api/memory")
        [entry] = out["targets"][0]["entries"]["kural"]
        assert entry["id"] == "M-1" and entry["uses"] == 0 and not entry["stale"] and out["targets"][0]["pending"] == []
        assert request(server, "POST", "/api/memory/demo/entries/M-1", {"action": "delete"})[0] == 400
        assert request(server, "POST", "/api/memory/demo/entries/M-1", {"action": "forget"})[1]["entry"]["status"] == "forgotten"
        assert request(server, "POST", "/api/memory/demo/entries/M-1", {"action": "forget"})[0] == 409
        assert request(server, "POST", "/api/memory/demo/entries/M-9", {"action": "forget"})[0] == 404
        assert "- Durum: unutuldu" in (demo["target"].state_dir / "memory" / "rules.md").read_text()
        assert not [r for r in attention.items([demo["target"]]) if r["ticket"] == attention.MEMORY]  # the inbox row resolved
    finally:
        server.shutdown()
        server.server_close()


def test_results_schema_for_retro_is_engine_only():
    from ziggy import results
    assert "retro" not in results.NAMES
    out, errors = results.normalize("retro", {"verdict": "READY", "memory": [
        {"kind": " Kural ", "title": "  Keep\n `legacy/` in UTC ", "rule": "Keep `legacy/` in UTC.", "why": "x",
         "paths": ["legacy/**", 3], "sources": [{"ticket": "local-1", "ref": "U1-1"}, {"ref": ""}]},
        {"kind": "nonsense", "title": "dropped"}, {"why": "no title"},
        *[{"kind": "karar", "title": f"d{i}", "sources": []} for i in range(6)]]})
    assert not errors and len(out["memory"]) == 5
    assert out["memory"][0] == {"kind": "kural", "title": "Keep `legacy/` in UTC", "rule": "Keep `legacy/` in UTC.", "why": "x",
                                "decision": "", "happens": "", "todo": "", "paths": ["legacy/**"], "topics": [],
                                "sources": [{"ticket": "local-1", "ref": "U1-1"}]}
    assert "memoryUsed" not in results.json_schema("retro")["properties"]
    assert "memoryUsed" in results.json_schema("implement")["properties"]
    assert json.dumps(results.json_schema("retro"))  # passed to --json-schema
