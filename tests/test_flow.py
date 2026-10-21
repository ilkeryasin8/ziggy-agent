"""Run flow: question rounds answered with their recommendations after `answer_after`, PLAN's goal and open tasks
reminded to a resumed session, and project memory judged by how the tickets that used it ended."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import calls, cli, script
from test_engine import _state
from test_memory import seed, target

from ziggy import config, dashboard, dispatch, memory, pipeline, plan, state

QUESTION = {"id": "Q1", "text": "Keep the old API?", "options": ["yes", "no"], "recommendation": "yes"}


def asked(env, *, answer_after="2h", questions=(QUESTION,), mode=None):
    extra = f'answer_after = "{answer_after}"\n' + (f'mode = "{mode}"\n' if mode else "")
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f"[targets.demo]\n{extra}"))
    script(env, {"verify": [{"verdict": "NEEDS_USER", "questions": list(questions)}, {"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 3


def backdate(hours):
    t = config.load_target("demo")
    st = state.load_state(t, "local-1")
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    st["rounds"][-1]["asked_at"] = when.isoformat(timespec="seconds")
    state.save_state(t, "local-1", st)
    return t


def test_a_round_left_past_answer_after_is_answered_with_its_recommendations(env):
    asked(env)
    t = config.load_target("demo")
    at = pipeline.default_answer_at(t, _state(env))
    assert at and datetime.fromisoformat(at) > datetime.now(timezone.utc)
    assert not pipeline.answer_by_default(t, "local-1")  # not due yet
    assert dashboard.ticket_row(t, "local-1")["auto_answer_at"] == at
    t = backdate(3)
    assert pipeline.answer_by_default(t, "local-1")
    r = _state(env)["rounds"][0]
    assert r["by"] == "ziggy" and r["via"] == "timeout" and r["answer"].startswith("Q1: yes\n\n(No answer came in 2h 00m")
    assert not pipeline.answer_by_default(t, "local-1")  # answered once
    pipeline.run(t, "local-1")
    assert _state(env)["status"] == "done" and _state(env)["run"]["visits"]["verify"] == 2


def test_the_panel_tick_answers_due_rounds_and_starts_the_run(env):
    asked(env)
    t = backdate(3)
    assert dashboard.answer_due(t) == ["local-1"]
    [job] = dashboard.jobs(t, "local-1")
    assert job["command"] == "run local-1"


@pytest.mark.parametrize("case", ["no recommendation", "controlled", "unset"])
def test_no_default_answer_without_every_recommendation_in_controlled_mode_or_unset(env, case):
    if case == "no recommendation":
        asked(env, questions=(QUESTION, {"id": "Q2", "text": "Which name?", "options": []}))
    elif case == "controlled":  # it stops for plan approval anyway; the setting itself is dropped
        env["config"].write_text(env["config"].read_text().replace(
            "[targets.demo]\n", '[targets.demo]\nanswer_after = "2h"\nmode = "controlled"\n'))
        t = config.load_target("demo")
        assert t.answer_after is None and any("answer_after is ignored" in w for w in t.warnings)
        return
    else:
        script(env, {"verify": [{"verdict": "NEEDS_USER", "questions": [QUESTION]}]})
        cli("new", "feat: thing")
        cli("run", "local-1")
    t = backdate(30)
    assert pipeline.default_answer_at(t, _state(env)) is None and not pipeline.answer_by_default(t, "local-1")


def test_answering_a_given_round_refuses_another_one(env):
    asked(env)
    t = config.load_target("demo")
    with pytest.raises(pipeline.PipelineError, match="no open question round 2"):
        pipeline.answer(t, "local-1", "Q1: yes", by="ziggy", via="timeout", run_after=False, round_n=2)
    assert _state(env)["rounds"][0]["answer"] is None


def test_plan_reminder_names_the_goal_and_the_open_tasks():
    text = ("## Goal & acceptance\n\nTotals round once.\n\n- AC-1: 3 x 0.10 shows 0.30\n\n## Plan\n\n"
            "- [x] T001 [S1] test (t.py)\n- [ ] T002 [S1] fix (cart.py)\n\n## Verification\n\n- [ ] V001 run it\n")
    out = plan.reminder(text)
    assert out.startswith("## Where this step stands (from PLAN)") and "AC-1: 3 x 0.10 shows 0.30" in out
    assert "- [ ] T002 [S1] fix (cart.py)\n- [ ] V001 run it" in out and "T001" not in out
    assert plan.reminder("") == "" and plan.reminder("## Notes\n\nnothing\n") == ""
    prompt = dispatch.resume_prompt("ORIGINAL", None, out)
    assert prompt.index("## Where this step stands") < prompt.index(dispatch.RESUME_MARK)
    assert dispatch.nudge_resume_prompt({"kind": "user", "text": "go"}, "ORIGINAL", out).count("Where this step stands") == 1


def test_memory_entries_learn_from_how_their_tickets_ended(env):
    seed(env)
    t = target()
    data = memory.usage(t)
    data["M-2"]["tickets"] = ["local-0", "local-1", "local-2"]
    state.write_json(env["state"] / "memory" / "usage.json", data)
    assert memory.record_outcome(t, "local-0", "rework") == ["M-2"]
    memory.record_outcome(t, "local-1", "rework")
    assert memory.prune_candidates(t) == [] or "M-2" not in [c["id"] for c in memory.prune_candidates(t)]  # 2 < 3
    memory.record_outcome(t, "local-2", "rework")
    memory.record_outcome(t, "local-2", "clean")  # rework sticks
    [c] = [c for c in memory.prune_candidates(t) if c["id"] == "M-2"]
    assert c["why"] == "3 of the 3 tickets that used it needed changes; none merged clean"
    assert memory.record_outcome(t, "local-9", "clean") == []
    assert next(e for e in memory.view(t)["entries"]["kural"] if e["id"] == "M-2")["prune"] == c["why"]
    out = cli("memory", "prune")
    assert "M-2\tkural" in out.stdout and "--apply" in out.stdout
    assert "unutuldu" in cli("memory", "prune", "--apply").stdout
    assert "M-2" not in [c["id"] for c in memory.prune_candidates(t)]


def test_an_entry_unused_for_long_is_suggested_but_a_rule_is_not(env):
    seed(env)
    t = target()
    long_ago = (datetime.now(timezone.utc) - timedelta(days=memory.PRUNE_UNUSED_DAYS + 5)).date()
    e = {"id": "M-50", "kind": "karar", "status": "active", "created": long_ago.isoformat()}
    assert memory.prune_reason(e, None).startswith("unused for ")
    assert memory.prune_reason({**e, "kind": "kural"}, None) is None
    assert memory.prune_reason({**e, "status": "forgotten"}, None) is None
    fresh = {**e, "created": datetime.now(timezone.utc).date().isoformat()}
    assert memory.prune_reason(fresh, None) is None
    assert t.name == "demo"


def test_changes_asked_by_a_person_count_as_rework_for_the_memory_it_used(env):
    seed(env)
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt", "memoryUsed": ["M-2"]}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    t = target()
    pipeline.request_changes(t, "local-1", "### Change\n\n- rename it", heading="Review (user)", comments=1, run_after=False)
    assert memory.usage(t)["M-2"]["outcomes"] == {"local-1": "rework"}
