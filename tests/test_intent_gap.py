"""Intent gaps: a review or implement stage that finds the change answers a different intent reports it; the
engine saves the attempt as a patch, resets the ticket branch to the commit before the run's first implement
(not when the branch is published), opens a question round, and the answers continue at the pipeline's
`[intent_gap] then` stage with the attempt in its prompt."""
import json
from pathlib import Path

import pytest
from conftest import calls, cli, git, script

from ziggy import attention, config, pipelines, plan, results

GAP = {"summary": "It deletes the item for everyone; the ticket hides it for one user.",
       "evidence": "ticket: 'hide for me' vs a.txt:1 deletes the row",
       "questions": [{"id": "Q1", "text": "Hide per user or for everyone?", "options": ["per user", "everyone"],
                      "recommendation": "per user"}]}
CONVERGED = {"verdict": "READY", "route": "converged", "converged": True}


def _state(env, ticket_id="local-1"):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def _run_dir(env, ticket_id="local-1") -> Path:
    return env["state"] / "runs" / ticket_id


def _head(st) -> str:
    return git("rev-parse", "HEAD", cwd=st["worktree"]).strip()


def test_intent_gap_result_is_validated():
    out, errors = results.normalize("review", {"verdict": "NEEDS_USER", "intentGap": {**GAP, "questions": ["Which way?"]}})
    assert not errors and out["intentGap"]["questions"][0] == {"text": "Which way?", "id": "q1", "options": [], "recommendation": ""}
    assert out["intentGap"]["summary"] == GAP["summary"] and out["intentGap"]["evidence"] == GAP["evidence"]
    for bad, message in (({"summary": "x", "questions": []}, "intentGap.questions: at least one question"),
                         ({"questions": ["q"]}, "intentGap.summary: required"),
                         ("wrong", "intentGap: expected {summary, evidence, questions}")):
        assert results.normalize("implement", {"verdict": "NEEDS_USER", "intentGap": bad}) == (None, [message])
    ready, _ = results.normalize("review", {"verdict": "READY", "route": "converged", "intentGap": GAP})
    assert "intentGap" not in ready  # only a NEEDS_USER result reports one
    assert "intentGap" in results.json_schema("implement")["properties"] and "intentGap" not in results.json_schema("plan")["properties"]


def test_intent_gap_then_is_validated_and_defaults_to_the_first_implement_stage():
    plan_ = {"id": "plan", "prompt": "plan.md", "result": "plan"}
    impl = {"id": "implement", "prompt": "implement.md", "result": "implement", "max_visits": 3}
    review = {"id": "review", "prompt": "review.md", "result": "review", "routes": {"fix": "implement", "converged": "done"}}
    assert pipelines.parse("t", {"stages": [plan_, impl, review]}).intent_gap_target() == "implement"
    assert pipelines.parse("t", {"stages": [plan_, impl, review], "intent_gap": {"then": "plan"}}).intent_gap_target() == "plan"
    assert pipelines.parse("t", {"stages": [plan_, {**review, "routes": {}}]}).intent_gap_target() is None
    with pytest.raises(pipelines.PipelineConfigError) as exc:
        pipelines.parse("t", {"stages": [plan_, impl], "intent_gap": {"then": "ghost", "after": "x"}})
    assert "[intent_gap]: allowed keys then" in str(exc.value) and "intent_gap.then: unknown stage 'ghost'" in str(exc.value)
    assert pipelines.load("bmad").intent_gap_target() == "bmad-build"
    assert pipelines.load("feat").intent_gap_target() == "implement"
    assert pipelines.load("project").intent_gap_target() is None


def test_review_intent_gap_saves_the_attempt_resets_the_branch_and_rebuilds_with_it(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}, {"verdict": "READY", "_commit": "b.txt"}],
                 "review": [{"verdict": "NEEDS_USER", "summary": "Wrong direction.", "intentGap": GAP}, CONVERGED]})
    cli("new", "chore: hide the item")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    pre = st["run"]["pre_implement"]
    assert st["status"] == "needs-user" and st["next"]["stage"] == "implement"
    r = st["rounds"][-1]
    gap = r["intent_gap"]
    assert r["questions"][0]["id"] == "Q1" and r["continue_at"] == "implement" and r["context"] == "Wrong direction."
    assert (gap["attempt"], gap["stage"], gap["reset"], gap["reset_to"], gap["kept"], gap["commits"]) == (1, "review", True, pre, None, 1)
    assert _head(st) == pre and not (Path(st["worktree"]) / "a.txt").exists()  # the branch is back before the implement
    patch = (_run_dir(env) / "attempts" / "attempt-1.patch").read_text()
    assert f"# Range: {pre}..{gap['head']}" in patch and "feat(local-1): implement 1" in patch
    assert "+++ b/a.txt" in patch and "+implement visit 1" in patch
    git("apply", "--check", str(_run_dir(env) / "attempts" / "attempt-1.patch"), cwd=st["worktree"])  # it applies again
    meta = json.loads((_run_dir(env) / "attempts" / "attempt-1.json").read_text())
    assert meta["summary"] == GAP["summary"] and meta["head"] == gap["head"] and meta["stage"] == "review"
    assert meta["commit_list"][0]["subject"] == "feat(local-1): implement 1"

    item = attention.needs(config.load_target("demo"), "local-1")
    assert item["kind"] == "question" and item["title"] == "Niyet sapması: review 1 soru soruyor"
    assert item["round"]["intent_gap"]["attempt"] == 1
    status = cli("status", "local-1").stdout
    assert f"intent gap: {GAP['summary']}" in status and f"attempts/attempt-1.patch; the branch was reset to {pre[:10]}" in status
    assert cli("answer", "local-1", "--text", "Q1: per user").returncode == 0
    st = _state(env)
    assert st["status"] == "done" and st["run"]["n"] == 1
    impl = [c for c in calls(env) if c.get("stage") == "implement"]
    assert len(impl) == 2
    prompt = impl[1]["prompt"]
    assert "Routed here from `review` by route `intentGap`" in prompt
    assert "## The previous attempt went the wrong way" in prompt and GAP["summary"] in prompt
    assert f"{_run_dir(env) / 'inbox' / 'attempts' / 'attempt-1.patch'}" in prompt and f"reset to `{pre}`" in prompt
    assert "## The previous attempt went the wrong way" not in impl[0]["prompt"]
    assert (_run_dir(env) / "inbox" / "attempts" / "attempt-1.patch").read_text() == patch
    plan_md = (_run_dir(env) / "inbox" / "plan.md").read_text()
    assert "Intent gap (found by review): " + GAP["summary"] in plan_md and "> Q1: per user" in plan_md
    review = [c for c in calls(env) if c.get("stage") == "review"][1]
    assert "Intent gap (found by review): " + GAP["summary"] in review["prompt"]  # the answered round, with the gap
    # The second review reads the whole branch: the first one's HEAD left the history with the reset.
    rec = json.loads(sorted(_run_dir(env).glob("*-review/run.json"))[-1].read_text())
    assert rec["review"]["full"] and "no longer in the branch's history" in rec["review"]["full_reason"]


def test_published_branch_keeps_its_code_and_only_asks(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}, {"verdict": "READY", "_commit": "b.txt"}],
                 "review": [CONVERGED, {"verdict": "NEEDS_USER", "intentGap": GAP}]})
    cli("new", "chore: hide the item")
    assert cli("run", "local-1").returncode == 0
    st = _state(env)
    published = _head(st)
    st.update(status="failed", next={"stage": "implement", "via": None, "resume": None},
              pr="https://github.example.test/acme/demo/pull/1", published_head=published)
    (_run_dir(env) / "state.json").write_text(json.dumps(st))
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    gap = st["rounds"][-1]["intent_gap"]
    assert st["run"]["n"] == 2 and st["run"]["pre_implement"] == published  # this run's first implement
    assert (gap["reset"], gap["reset_to"], gap["kept"], gap["commits"]) == (False, None, "published", 1)
    assert _head(st) == gap["head"] != published and (Path(st["worktree"]) / "b.txt").exists()
    patch = (_run_dir(env) / "attempts" / "attempt-1.patch").read_text()
    assert "+++ b/b.txt" in patch and "a.txt" not in patch
    assert "the branch kept its code" in plan.round_markdown({**st["rounds"][-1], "answer": "x"})

    assert cli("answer", "local-1", "--text", "per user").returncode == 3  # the review asks again (script repeats)
    prompt = [c for c in calls(env) if c.get("stage") == "implement"][-1]["prompt"]
    assert "the branch is already published, so its code was kept as it is (not reset)" in prompt
    assert "never rewrite history" in prompt


def test_implement_reported_gap_resets_and_comes_back_to_implement(env):
    script(env, {"implement": [{"verdict": "NEEDS_USER", "_commit": "a.txt", "_dirty": "half.txt", "intentGap": GAP},
                               {"verdict": "READY", "_commit": "b.txt"}],
                 "review": [CONVERGED]})
    cli("new", "chore: hide the item")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    gap = st["rounds"][-1]["intent_gap"]
    assert gap["stage"] == "implement" and gap["reset"] and gap["stash"]  # the uncommitted edit is stashed, not lost
    assert _head(st) == st["run"]["pre_implement"] and not git("status", "--porcelain", cwd=st["worktree"]).strip()
    assert "+++ b/a.txt" in (_run_dir(env) / "attempts" / "attempt-1.patch").read_text()
    assert cli("answer", "local-1", "--text", "per user").returncode == 0
    assert _state(env)["status"] == "done"
    second = [c for c in calls(env) if c.get("stage") == "implement"][1]["prompt"]
    assert "`implement` found that the previous attempt answered a different intent" in second


def test_intent_gap_without_an_implement_stage_is_an_ordinary_question(env):
    script(env, {"plan": [{"verdict": "NEEDS_USER", "intentGap": GAP, "questions": [{"text": "Plain question?"}]}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 3
    r = _state(env)["rounds"][-1]
    assert r["intent_gap"] is None and r["questions"][0]["text"] == "Plain question?"  # only implement/review report gaps
    assert not (_run_dir(env) / "attempts").exists()
