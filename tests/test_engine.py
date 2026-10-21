"""The engine's own rules: pipeline validation and routing, result resolution, the prompt renderer, and
end-to-end runs through the shipped pipelines with the fake agent."""
import json
import os

import pytest

from ziggy import checks, dispatch, pipelines, render, results
from conftest import cli, calls, script


def _pipeline(stages, **extra):
    return pipelines.parse("t", {"stages": stages, **extra})


PLAN = {"id": "plan", "prompt": "plan.md", "result": "plan"}
IMPL = {"id": "implement", "prompt": "implement.md", "result": "implement", "max_visits": 3}
REVIEW = {"id": "review", "prompt": "review.md", "result": "review", "routes": {"fix": "implement", "converged": "done"}}


def test_validation_names_every_bad_reference():
    with pytest.raises(pipelines.PipelineConfigError) as exc:
        _pipeline([{**PLAN, "next": "nowhere"}, {**IMPL, "routes": {"fix": "ghost"}}, {"id": "Bad", "prompt": "x.md"}],
                  ask_user={"then": "missing"})
    message = str(exc.value)
    for part in ("unknown target 'nowhere'", "route \"fix\": unknown target 'ghost'", "id 'Bad'", "ask_user.then: unknown stage 'missing'"):
        assert part in message


def test_backward_edge_into_single_visit_stage_warns():
    p = _pipeline([PLAN, {**IMPL, "max_visits": 1}, REVIEW])
    assert any("goes back to implement" in w for w in p.warnings)


def test_routing_precedence():
    p = _pipeline([PLAN, IMPL, REVIEW])
    review = p.stage("review")
    assert p.target(review, {"verdict": "READY", "route": "fix"})[:2] == ("implement", {"from": "review", "route": "fix"})
    target, via, warnings = p.target(review, {"verdict": "READY", "route": "sideways"})
    assert (target, via) == ("done", None) and "not declared" in warnings[0]
    assert p.target(review, {"verdict": "NEEDS_USER", "route": "fix"})[0] == "ask_user"
    assert p.target(p.stage("plan"), {"verdict": "READY"})[0] == "implement"
    assert p.target(p.stage("plan"), {"verdict": "FAILED"})[0] == "failed"
    q = _pipeline([{**PLAN, "next": {"NEEDS_USER": "implement"}}, IMPL])
    assert q.target(q.stage("plan"), {"verdict": "NEEDS_USER"})[0] == "implement"
    assert not q.can_ask(q.stage("plan"))


def test_gate_thresholds_and_ratings_never_drop():
    p = _pipeline([PLAN, IMPL], gate={"after": ["plan"], "risk": "medium", "size": "large"})
    assert p.gated("plan", "low", "small", False) is None
    assert p.gated("plan", "medium", "small", False) == "risk medium"
    assert p.gated("plan", "low", "large", False) == "size large"
    assert p.gated("plan", "low", "small", True)
    assert p.gated("implement", "high", "large", False) is None
    assert pipelines.higher("risk", "high", "low") == "high"
    assert pipelines.higher("size", None, "medium") == "medium"


def test_result_resolution_order():
    good = json.dumps({"verdict": "READY", "summary": "file", "risk": " High "})
    r = results.resolve("plan", good, {"verdict": "FAILED"}, "VERDICT: NEEDS_USER")
    assert (r["source"], r["result"]["verdict"], r["result"]["risk"]) == ("file", "READY", "high")
    assert "using the file" in r["warnings"][0]
    r = results.resolve("plan", "```json\n{\"verdict\": \"MAYBE\"}\n```", {"verdict": "READY"}, "")
    assert r["source"] == "structured" and "does not match" in r["warnings"][0]
    r = results.resolve("plan", None, None, "Looked around.\nVERDICT: **NEEDS_USER**")
    assert r["source"] == "verdict" and r["result"]["questions"][0]["text"] == "Looked around."
    r = results.resolve("plan", None, None, "done, I think")
    assert r["result"] is None and r["error"] == "the agent did not write a result file"


def test_review_findings_are_read_leniently():
    data = {"verdict": "READY", "route": "fix", "converged": "yes",
            "findings": [{"severity": "BLOCKER", "kind": "Missing", "disposition": " Defer "}, {"id": "R1-2"}]}
    out, errors = results.normalize("review", data)
    assert not errors
    assert {k: out["findings"][0][k] for k in ("id", "severity", "kind", "disposition")} == \
        {"id": "f1", "severity": "medium", "kind": "missing", "disposition": "defer"}
    assert out["findings"][1]["id"] == "R1-2" and out["converged"] is False


def test_finding_classes_counts_and_confidence_are_derived():
    data = {"verdict": "READY", "route": "fix", "askThen": "  implement ",
            "findings": [{"id": "R1-1", "severity": "high", "disposition": "fix", "confidence": "85%", "verified": True},
                         {"id": "R1-2", "severity": "low", "disposition": "defer", "confidence": 2},
                         {"id": "R1-3", "severity": "low", "confidence": "high", "verified": "yes"},
                         {"id": "R1-4", "class": " Pre_Existing ", "disposition": "fix", "confidence": "sure"},
                         {"id": "R1-5", "class": "nit", "disposition": "dismiss"}],
            "unverified": [{"location": "a.py:1"}, "junk"], "guide": [{"file": "a.py", "order": "2"}, {"summary": "no file"}]}
    out, errors = results.normalize("review", data)
    assert not errors
    by_id = {f["id"]: f for f in out["findings"]}
    assert [by_id[i]["class"] for i in ("R1-1", "R1-2", "R1-3", "R1-4", "R1-5")] == \
        ["important", "pre-existing", "nit", "pre-existing", "nit"]  # defer wins over low
    assert by_id["R1-1"]["confidence"] == 0.85 and by_id["R1-2"]["confidence"] == 0.02 and by_id["R1-3"]["confidence"] == 0.9
    assert "confidence" not in by_id["R1-4"] and by_id["R1-1"]["verified"] is True and "verified" not in by_id["R1-3"]
    assert out["counts"] == {"important": 1, "nit": 1, "pre_existing": 2, "unverified": 1}  # the dismissed nit is closed
    assert out["unverified"] == [{"location": "a.py:1", "summary": ""}] and out["askThen"] == "implement"
    assert out["guide"] == [{"file": "a.py", "order": 2, "summary": ""}]
    given, _ = results.normalize("review", {"verdict": "READY", "findings": [{"severity": "high"}],
                                            "counts": {"important": "0", "preExisting": 3, "nit": -1}})
    assert given["counts"] == {"important": 0, "nit": 0, "pre_existing": 3, "unverified": 0}
    blank, _ = results.normalize("generic", {"verdict": "NEEDS_USER", "askThen": "  "})
    assert "askThen" not in blank


def test_implement_outcomes_and_artifact_objects_are_accepted():
    out, errors = results.normalize("implement", {
        "verdict": "READY", "artifacts": ["a.png", {"path": "b.log", "caption": "log"}],
        "outcomes": [{"id": "R1-1", "outcome": "Fixed"}, {"finding_id": "R1-2", "outcome": "maybe", "note": " why "}, {"outcome": "fixed"}]})
    assert not errors
    assert out["outcomes"] == [{"finding_id": "R1-1", "outcome": "fixed"}, {"finding_id": "R1-2", "outcome": "no_change", "note": "why"}]
    assert out["artifacts"] == ["a.png", {"path": "b.log", "caption": "log"}]
    assert results.normalize("implement", {"verdict": "READY", "artifacts": "a.png"})[0] is None


def test_artifacts_are_normalized_inside_the_artifacts_dir(tmp_path):
    base = tmp_path / "inbox" / "artifacts"
    base.mkdir(parents=True)
    items = [{"path": "shots/home.png", "caption": "  Home screen  ", "kind": "VIDEO"}, "notes.log", "./diagram.svg",
             str(base / "abs" / "inside.txt"), str(tmp_path / "outside.png"), "../escape.png", "shots/../../x.png",
             {"path": "shots/home.png", "caption": "later listing"}, "data.bin", "", {"caption": "no path"}, 42,
             {"path": "clip.mov", "kind": "nonsense", "caption": "x" * 600}]
    out = results.normalize_artifacts(items, str(base))
    assert [(a["path"], a["kind"]) for a in out] == [
        ("shots/home.png", "video"), ("notes.log", "log"), ("diagram.svg", "image"), ("abs/inside.txt", "text"),
        ("data.bin", "other"), ("clip.mov", "video")]
    assert out[0]["caption"] == "Home screen" and out[1]["caption"] is None and len(out[-1]["caption"]) == 500
    assert len(results.normalize_artifacts([f"f{i}.png" for i in range(150)], str(base))) == 100
    assert results.normalize_artifacts("x.png", str(base)) == []


def test_footer_offers_ask_and_routes_only_when_allowed():
    text = results.footer("/x/result.json", "review", can_ask=False, routes=["fix", "converged"])
    assert "`/x/result.json`" in text and "`NEEDS_USER` is not available" in text and "`fix`, `converged`" in text
    assert "checksFailed" not in text and "askThen" not in text.split("```")[2] and "Evidence:" not in text


def test_footer_names_ask_targets_and_evidence():
    text = results.footer("/r.json", "review", True, ["fix", "converged"], ["review", "implement"], "/in/artifacts")
    assert "Answers continue at `review`" in text and "continue at one of: `review`, `implement`" in text
    assert "under `/in/artifacts` (also\n`PIPELINE_ARTIFACTS_DIR`)" in text
    one = results.footer("/r.json", "implement", True, [], ["implement"], "/in/artifacts")
    assert "Answers continue at" not in one and "Evidence:" in one
    assert "Evidence:" not in results.footer("/r.json", "plan", True, [], [], "/in/artifacts")


def test_visit_overrides_are_cumulative_and_validated():
    stage = {**REVIEW, "model": "build", "max_visits": 3, "visits": {"2": {"model": "opus", "tools_deny": ["Bash"]},
                                                                     "3": {"effort": "max", "tools_deny": ["Edit"], "timeout": "5m"}}}
    s = _pipeline([PLAN, IMPL, stage]).stage("review")
    assert pipelines.effective(s, 1) == s
    v2, v3 = pipelines.effective(s, 2), pipelines.effective(s, 3)
    assert (v2.model, v2.effort, v2.tools_deny) == ("opus", None, ("Bash",))
    assert (v3.model, v3.effort, v3.tools_deny, v3.timeout) == ("opus", "max", ("Edit",), 300)
    assert pipelines.override_keys(s, 3) == ["model", "tools_deny", "effort", "timeout"]
    assert pipelines.effective(s, 9).routes == s.routes
    late = _pipeline([PLAN, IMPL, {**REVIEW, "max_visits": 1, "visits": {"3": {"effort": "high"}}}])
    assert any("visits.3 never applies (max_visits 1)" in w for w in late.warnings)
    for bad, part in (({"1": {"effort": "high"}}, "keys are visit numbers from 2"),
                      ({"2": {"routes": {}}}, "unknown keys routes"), ({"2": {"effort": "huge"}}, "effort must be one of"),
                      ({"2": {"prompt": "nope.md"}}, "prompt file not found")):
        with pytest.raises(pipelines.PipelineConfigError, match=part):
            _pipeline([PLAN, IMPL, {**REVIEW, "visits": bad}])


def test_retry_policy_and_ask_targets_parse():
    p = _pipeline([PLAN, {**IMPL, "retry": {"on_quiet": True, "backoff": "1m"}}, {**REVIEW, "ask_then": "review"}])
    r = p.stage("implement").retry
    assert (r.on_quiet, r.on_lost, r.max, r.backoff, r.max_backoff) == (True, False, 2, 60, 600)
    assert p.stage("plan").retry == pipelines.Retry()
    assert p.ask_targets(p.stage("review")) == ["review", "implement"]
    for bad, part in (({"max": 11}, "retry.max"), ({"max": True}, "retry.max"), ({"on_lost": "yes"}, "retry.on_lost"),
                      ({"often": 1}, "retry: allowed keys"), ({"backoff": "soon"}, "retry.backoff")):
        with pytest.raises(pipelines.PipelineConfigError, match=part):
            _pipeline([PLAN, {**IMPL, "retry": bad}])


def test_tool_running_ignores_startup_children_and_other_groups():
    rows = dispatch.parse_ps(" 100     1   100 Fri Oct  2 10:00:00 2026     claude -p\n"
                             " 101   100   100 Fri Oct  2 10:00:02 2026     mcp-server\n"
                             " 102   100   100 Fri Oct  2 10:05:00 2026     npm test\n"
                             " 103     1   555 Fri Oct  2 10:06:00 2026     other\n"
                             "garbage line\n")
    assert [r["pid"] for r in rows] == [100, 101, 102, 103]
    leader, mcp, build, other = rows
    assert build["started"] - leader["started"] == 300
    assert dispatch.tool_running(rows, leader, build["started"] - 60)  # a build started after the last output
    assert not dispatch.tool_running([leader, mcp], leader, leader["started"])  # an MCP server from startup
    assert not dispatch.tool_running([leader, other], leader, leader["started"])  # another process group
    assert not dispatch.tool_running(rows, leader, build["started"] + 60)  # started long before the last output
    assert dispatch.fmt_duration(59) == "0m" and dispatch.fmt_duration(3900) == "1h 05m"


def test_render_sections_lists_partials_and_standalone_lines():
    template = "A\n{{#items}}\n- {{name}}{{#tag}} ({{tag}}){{/tag}}\n{{/items}}\n{{^none}}\nempty\n{{/none}}\n{{> p}}\nn={{n}}{{#zero}}!{{/zero}}\n"
    out = render.render(template, {"items": [{"name": "a", "tag": "x"}, {"name": "b"}], "none": [], "n": 0, "zero": 0,
                                   "outer": "o"}, lambda name: "p:{{outer}}\n")
    assert out == "A\n- a (x)\n- b\nempty\np:o\nn=0\n"
    assert render.render("{{#obj}}{{inner}}/{{outer}}{{/obj}}", {"obj": {"inner": 1}, "outer": 2}) == "1/2"
    assert render.render("[{{missing.deep}}][{{d}}]", {"d": {"k": 1}}) == "[][]"
    with pytest.raises(render.RenderError):
        render.render("{{#a}}x", {})


@pytest.mark.parametrize("name", pipelines.names())
def test_shipped_prompts_render_clean(name):
    """Every stage prompt renders with a full context: no tags left, no Python reprs."""
    p = pipelines.load(name)
    ctx = {"ticket": {"id": "local-1", "title": "T", "type": "feat", "body": "b"}, "project": {"name": "demo"},
           "git": {"worktree": "/w", "branch": "feat/t", "base": "origin/main", "base_commit": "abc"},
           "paths": {k: f"/p/{k}" for k in ("ticket_file", "plan", "deferred", "answers", "inbox", "artifacts", "result_file", "project_doc", "run_dir")},
           "project_doc": {"exists": True, "path": "/w/docs/PROJECT.md"},
           "run": {"n": 2, "visit": 2, "ticket_visit": 3, "via": {"from": "review", "route": "fix"}, "round": 1,
                   "max_visits": 3, "last_visit": True, "adjudicated": False},
           "user_review": {"round": 1, "comments": 2, "heading": "Review (user, round 1)", "source": "user",
                           "findings": ["R1-1"], "has_findings": True},
           "review": {"round": 3, "full": False, "since": "def", "full_reason": "", "has_carried": True, "last_round": True,
                      "carried": [{"id": "R1-1", "class": "important", "severity": "high", "kind": "bug",
                                   "location": "a.py:3", "summary": "s", "disposition": "fix"}]},
           "ports": {"base": 4100, "end": 4109},
           "answers_markdown": "### Round 1", "has_answers": True, "today": "2026-10-14", "language": "Turkish",
           "memory": {"exists": True, "text": "```markdown\n### Kurallar (her zaman geçerli)\n- M-1 · kural · Use make\n```", "ids": ["M-1"]},
           "attempt": {"attempt": 1, "stage": "review", "summary": "it deletes for everyone", "evidence": "a.py:3",
                       "patch": "/p/inbox/attempts/attempt-1.patch", "reset": True, "reset_to": "abc", "kept_note": ""},
           "prev": {"plan": {"risk": "medium", "size": "small"}, "bmad-plan": {"planPath": "_bmad-output/spec.md"},
                    "bmad-build": {"checks": [{"name": "tests", "status": "fail", "source": "engine", "detail": "1 failed"}]},
                    "implement": {"commit": {"hash": "abc", "message": "m"},
                    "summary": "s", "checks": [{"name": "tests", "status": "pass", "source": "engine"}], "openItems": ["x"],
                    "outcomes": [{"finding_id": "R1-1", "outcome": "fixed", "note": "n"}]}}}
    with_checks = {"commands": [{"name": "tests", "run": "make test"}], "has_results": True,
                   "results": [{"name": "tests", "status": "pass", "detail": None}, {"name": "lint", "status": "fail", "detail": "E1"}]}
    for stage in p.stages:
        def partial(n, stage=stage):
            return pipelines.partial_text(f"roles/{stage.role}" if n == "role" else n) if n != "role" or stage.role else ""
        for visit in range(1, stage.max_visits + 2):
            eff = pipelines.effective(stage, visit)
            for adjudicated, full, checks in ((False, False, None), (True, True, with_checks),
                                              (False, True, {**with_checks, "results": [], "has_results": False})):
                ctx["run"]["adjudicated"], ctx["review"]["full"], ctx["engine_checks"] = adjudicated, full, checks
                out = render.render(pipelines.prompt_text(eff.prompt), ctx, partial)
                assert "{{" not in out and "}}" not in out and "{'" not in out and "[object" not in out, (name, stage.id)
                assert "## Proje hafızası (project memory, data)" in out and "- M-1 · kural · Use make" in out, (name, stage.id)
                assert "`memoryUsed`" in out, (name, stage.id)
                bare = render.render(pipelines.prompt_text(eff.prompt), {**ctx, "memory": {"exists": False}}, partial)
                assert "Proje hafızası" not in bare and "\n\n\n" not in bare, (name, stage.id)
                if eff.prompt in ("implement.md", "bmad-build.md"):
                    assert ("do not run the whole suite" in out.lower()) == bool(checks), (name, stage.id)
                    assert ("`tests` (`make test`)" in out) == bool(checks)
                    assert "`/p/inbox/attempts/attempt-1.patch`" in out and "reset to `abc`" in out
                if eff.prompt == "review.md":
                    assert ("Do not re-run the full suite" in out) == bool(checks and checks["has_results"])
                    assert ("`tests`=pass; `lint`=fail (E1);" in out) == bool(checks and checks["has_results"])
                    assert ("No engine check result matches" in out) == bool(checks and not checks["has_results"])


def _finding(fid, disposition="fix", severity="low", cls=None):
    finding = {"id": fid, "disposition": disposition, "severity": severity}
    finding["class"] = cls or results.derive_class(finding)
    return finding


def test_review_route_sends_open_fix_findings_back_whatever_their_class():
    nit = {"verdict": "READY", "route": "converged", "converged": True,
           "findings": [_finding("R1-1"), _finding("R1-2", "dismiss"), _finding("R1-3", "defer")]}
    warnings = results.settle_review_route(nit, last_round=False)
    assert (nit["route"], nit["converged"]) == ("fix", False)
    assert warnings == ['route "converged" → "fix": R1-1 still open with disposition fix']
    dismissed = {"verdict": "READY", "route": "converged", "converged": True, "fixedIds": ["R1-1"],
                 "findings": [_finding("R1-1"), _finding("R2-1", "dismiss", "high"), _finding("R2-2", "defer")]}
    assert results.settle_review_route(dismissed, last_round=False) == [] and dismissed["route"] == "converged"
    nothing = {"verdict": "READY", "route": "fix", "findings": [_finding("R2-1", "dismiss")]}
    assert results.settle_review_route(nothing, last_round=False) == ['route "fix" with no open finding to fix']
    assert nothing["route"] == "fix"  # left as the agent set it: the loop cap still applies
    asking = {"verdict": "NEEDS_USER", "findings": [_finding("R1-1")]}
    assert results.settle_review_route(asking, last_round=False) == [] and "route" not in asking


def test_review_route_on_the_last_round_defers_nits_and_converges():
    last = {"verdict": "READY", "route": "fix", "deferredIds": ["R3-9"],
            "findings": [_finding("R3-1"), _finding("R3-2", "fix", "medium", "nit"), _finding("R3-3", "dismiss")]}
    warnings = results.settle_review_route(last, last_round=True)
    assert (last["route"], last["converged"], last["deferredIds"]) == ("converged", True, ["R3-9", "R3-1", "R3-2"])
    assert [f["disposition"] for f in last["findings"]] == ["defer", "defer", "dismiss"]
    assert warnings[0] == "last round: R3-1, R3-2 deferred instead of sent back"
    important = {"verdict": "READY", "route": "fix", "findings": [_finding("R3-1"), _finding("R3-4", "fix", "high")]}
    results.settle_review_route(important, last_round=True)
    assert important["route"] == "fix" and [f["disposition"] for f in important["findings"]] == ["defer", "fix"]


# --- end to end through the CLI, with the fake agent ---------------------------------------------

def _state(env, ticket_id="local-1"):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def _stages(env):
    return [c["stage"] for c in calls(env) if c.get("stage")]


def test_feat_runs_every_stage_and_records_the_plan(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "feature.txt"}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True}]})
    assert cli("new", "feat: add a thing").returncode == 0
    proc = cli("run", "local-1")
    assert proc.returncode == 0, proc.stderr
    st = _state(env)
    assert _stages(env) == ["plan", "verify", "implement", "review"]
    assert st["status"] == "done" and st["tasks"] == {"done": 1, "total": 2}
    first = calls(env)[0]
    assert "## Result protocol (ziggy)" in first["prompt"] and first["env"]["PIPELINE_RESULT_FILE"].endswith("inbox/result.json")
    assert all(c["push_exit"] != 0 for c in calls(env) if c.get("stage"))  # the pre-push hook held


def test_question_round_continues_the_same_run_at_ask_user_then(env):
    script(env, {"verify": [{"verdict": "NEEDS_USER", "questions": [{"id": "Q1", "text": "Keep the old API?",
                                                                     "options": ["yes", "no"], "recommendation": "yes"}]},
                            {"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    assert st["status"] == "needs-user" and st["rounds"][0]["questions"][0]["id"] == "Q1"
    assert cli("answer", "local-1", "--text", "yes, keep it").returncode == 0
    st = _state(env)
    assert st["status"] == "done" and st["run"]["n"] == 1 and st["run"]["visits"]["verify"] == 2
    plan_md = (env["state"] / "runs" / "local-1" / "inbox" / "plan.md").read_text()
    assert "## Decisions (user)" in plan_md and "> yes, keep it" in plan_md
    assert _stages(env) == ["plan", "verify", "verify", "implement", "review"]


def test_gate_stops_on_risk_and_approve_continues(env):
    script(env, {"plan": [{"verdict": "READY", "risk": "medium"}], "verify": [{"verdict": "READY", "risk": "low"}],
                 "implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: risky")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    assert st["status"] == "awaiting-approval" and st["risk"] == "medium"  # verify could not lower it
    assert st["gate"]["stage"] == "plan" and st["next"]["stage"] == "verify"
    assert cli("approve", "local-1").returncode in (0, 3)


def test_review_loop_hits_the_cap_and_names_the_edge(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [{"verdict": "READY", "route": "fix"}]})
    cli("new", "feat: tidy")
    assert cli("run", "local-1").returncode == 1
    st = _state(env)
    assert st["status"] == "failed" and "review → implement (route fix): loop cap reached (max_visits 3)" in st["error"]
    assert _stages(env) == ["plan", "verify", "implement", "review", "implement", "review", "implement", "review"]
    assert st["next"]["stage"] == "implement"


def test_uncommitted_work_fails_the_clean_tree_check_and_goes_back(env):
    script(env, {"implement": [{"verdict": "READY", "_dirty": "left.txt"}, {"verdict": "READY", "_commit": "left.txt"}]})
    cli("new", "chore: tidy")
    cli("run", "local-1")
    st = _state(env)
    assert _stages(env)[:3] == ["plan", "implement", "implement"], st.get("error")


def test_read_only_stage_that_edits_fails_and_keeps_the_edit_in_a_stash(env):
    script(env, {"plan": [{"verdict": "READY", "_commit": "sneaky.txt"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 1
    st = _state(env)
    assert st["status"] == "failed" and "must not change the repository" in st["error"]


def test_missing_result_fails_the_stage_and_run_retries_it_in_a_new_run(env):
    script(env, {"plan": [{"_no_result": True}, {"verdict": "READY"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 1
    st = _state(env)
    assert "did not write a result file" in st["error"] and st["next"]["stage"] == "plan"
    cli("run", "local-1")
    assert _state(env)["run"]["n"] == 2


def _records(env, ticket_id="local-1"):
    root = env["state"] / "runs" / ticket_id
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and d.name[:2].isdigit()), key=lambda d: int(d.name.split("-")[0]))
    return [json.loads((d / "run.json").read_text()) for d in dirs if (d / "run.json").exists()]


def _review(route, findings, **extra):
    """A READY review result; findings get a location and evidence (the verification bar) unless they bring their own."""
    findings = [{"location": "a.txt:1", "evidence": "a.txt:1 shows it", **f} for f in findings]
    return {"verdict": "READY", "route": route, "findings": findings, **extra}


def test_later_reviews_are_incremental_think_harder_and_carry_open_findings(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt", "artifacts": ["shot.png", "../x.png"]}],
                 "review": [_review("fix", [{"id": "R1-1", "severity": "high", "disposition": "fix", "summary": "broken"},
                                            {"id": "R1-2", "severity": "low", "disposition": "defer", "summary": "nit"}]),
                            _review("fix", [{"id": "R2-1", "severity": "low", "disposition": "defer"}], fixedIds=["R1-1"]),
                            _review("converged", [], converged=True)]})
    cli("new", "feat: add a thing")
    assert cli("run", "local-1").returncode == 0
    reviews = [c for c in calls(env) if c.get("stage") == "review"]
    assert len(reviews) == 3
    assert "--effort" not in reviews[0]["argv"]
    assert reviews[1]["argv"][reviews[1]["argv"].index("--effort") + 1] == "high"  # visit 2 override
    assert reviews[2]["argv"][reviews[2]["argv"].index("--effort") + 1] == "high"  # cumulative into visit 3
    assert [c["env"]["PIPELINE_VISIT"] for c in reviews] == ["1", "2", "3"]
    assert [c["env"]["PIPELINE_LAST_VISIT"] for c in reviews] == ["0", "0", "1"]
    recs = [r for r in _records(env) if r["stage"] == "review"]
    assert recs[0]["review"]["full"] and recs[0]["review"]["full_reason"] == "first review"
    second = recs[1]["review"]
    assert not second["full"] and second["since"] == recs[0]["head_before"] and second["since"] != recs[1]["head_before"]
    assert [c["id"] for c in second["carried"]] == ["R1-1"]  # the deferred one is not carried
    assert "Diff under review: `" + second["since"] + "..HEAD`" in reviews[1]["prompt"]
    assert "Open findings from the previous review" in reviews[1]["prompt"] and "`R1-1` [important/high/bug]" in reviews[1]["prompt"]
    assert recs[2]["review"]["carried"] == []  # R1-1 fixed, R2-1 deferred
    assert recs[1]["effort"] == "high" and recs[1]["overrides"] == ["effort"] and recs[2]["last_visit"] is True
    assert 'route "fix" with no open finding to fix' in recs[1]["result_warnings"]
    assert "This is the last review this run allows" in reviews[2]["prompt"]
    assert "set `askThen` with `NEEDS_USER` to continue at one of: `review`, `implement`" in reviews[0]["prompt"]
    impl = next(r for r in _records(env) if r["stage"] == "implement")
    assert impl["result"]["artifacts"] == [{"path": "shot.png", "kind": "image", "caption": None}]
    first_impl = next(c for c in calls(env) if c.get("stage") == "implement")
    assert first_impl["env"]["PIPELINE_ARTIFACTS_DIR"].endswith("inbox/artifacts") and "Evidence: save screenshots" in first_impl["prompt"]


def test_full_review_reads_the_whole_branch_but_still_carries(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [_review("fix", [{"id": "R1-1", "severity": "high", "disposition": "fix"}]),
                            _review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    cli("run", "local-1")  # chore: review max_visits 2 → fix, then converged
    assert _state(env)["status"] == "done"
    assert cli("run", "local-1", "--full-review").returncode == 2  # only a new run can be a full one
    st = _state(env)
    st["status"], st["next"] = "failed", {"stage": "review", "via": None, "resume": None}
    (env["state"] / "runs" / "local-1" / "state.json").write_text(json.dumps(st))
    assert cli("run", "local-1", "--full-review").returncode == 0
    last = [r for r in _records(env) if r["stage"] == "review"][-1]
    assert last["run"] == 2 and last["review"]["full"] and last["review"]["full_reason"] == "a full review was requested"
    assert last["review"]["carried"] == []  # the previous ok review converged with nothing open


def test_a_nit_to_fix_goes_back_and_the_last_round_defers_it(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [_review("converged", [{"id": "R1-1", "severity": "low", "disposition": "fix"},
                                                  {"id": "R1-2", "severity": "low", "disposition": "dismiss"}], converged=True),
                            _review("fix", [{"id": "R2-1", "severity": "low", "disposition": "fix"}], fixedIds=["R1-1"])]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0  # chore: review max_visits 2
    assert _stages(env) == ["plan", "implement", "review", "implement", "review"]
    first, last = [r for r in _records(env) if r["stage"] == "review"]
    assert first["route"] == "fix" and first["next"] == "implement" and first["result"]["converged"] is False
    assert 'route "converged" → "fix": R1-1 still open with disposition fix' in first["result_warnings"]
    assert last["route"] == "converged" and last["next"] == "done" and last["review"]["last_round"] is True
    assert last["result"]["findings"][0]["disposition"] == "defer" and last["result"]["deferredIds"] == ["R2-1"]
    reviews = [c for c in calls(env) if c.get("stage") == "review"]
    assert "This is the last review this run allows" in reviews[1]["prompt"] and "last review" not in reviews[0]["prompt"]


@pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")
def test_engine_checks_reach_the_implement_and_review_prompts(env):
    env["config"].write_text(env["config"].read_text() + '[[targets.demo.checks]]\nname = "ok"\nrun = "true"\n')
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    impl, review = [c["prompt"] for c in calls(env) if c.get("stage") in ("implement", "review")]
    assert "Do not run the whole suite: after you report `READY` the engine runs\n   `ok` (`true`); a failure" in impl
    assert "The engine ran the target's checks on the HEAD under review: `ok`=pass;" in review
    assert "Do not re-run the full suite" in review


@pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")
def test_a_check_failing_in_its_environment_stops_without_burning_implement_visits(env):
    env["config"].write_text(env["config"].read_text() + '[[targets.demo.checks]]\nname = "app"\n'
                             "run = \"echo 'sandbox-exec: sandbox_apply: Operation not permitted'; exit 65\"\n")
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 1
    st = _state(env)
    assert st["status"] == "failed" and 'check "app" failed in its environment, not in the code' in st["error"]
    assert "The code was not sent back" in st["error"]
    assert [c.get("stage") for c in calls(env)].count("implement") == 1


@pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")
def test_recheck_runs_only_the_checks_once_the_environment_is_fixed(env):
    from ziggy import config, pipeline, state

    broken = '[[targets.demo.checks]]\nname = "app"\nrun = "echo \'sandbox-exec: sandbox_apply: Operation not permitted\'; exit 65"\n'
    base = env["config"].read_text()
    env["config"].write_text(base + broken)
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "feat: thing")
    assert cli("recheck", "local-1").returncode == 2  # new: nothing to re-check yet
    assert cli("run", "local-1").returncode == 1
    target = config.load_target("demo")
    assert pipeline.recheck_source(target, "local-1", _state(env)) is not None

    env["config"].write_text(base + '[[targets.demo.checks]]\nname = "app"\nrun = "true"\n')  # the fix
    assert cli("recheck", "local-1").returncode == 0
    st = _state(env)
    assert st["status"] == "done" and [c.get("stage") for c in calls(env)].count("implement") == 1  # no second session
    again = [r for _, r in pipeline._records(target, "local-1") if r.get("checks_only")]
    assert len(again) == 1 and again[0]["verdict"] == "READY" and again[0]["checks"][0]["status"] == "pass"
    assert again[0]["cost_usd"] == 0.0 and again[0]["next"] == "review"
    assert state.load_state(target, "local-1")["run"]["n"] == 2


def _adjudicating_script(env, review_after):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt", "outcomes": [{"id": "R3-1", "outcome": "fixed"}]}],
                 "review": [_review("fix", [{"id": "R1-1", "severity": "high", "disposition": "fix"}]),
                            _review("fix", [{"id": "R2-1", "severity": "high", "disposition": "fix"}]),
                            {"verdict": "NEEDS_USER", "askThen": "implement",
                             "questions": [{"id": "q1", "text": "R3-1: still broken", "options": ["patch: x", "defer: y"]}]},
                            review_after]})


def test_adjudication_on_the_last_visit_grants_one_more_round(env):
    _adjudicating_script(env, _review("converged", [], converged=True))
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    assert st["status"] == "needs-user" and st["next"]["stage"] == "implement"
    assert st["rounds"][-1]["continue_at"] == "implement" and st["rounds"][-1]["adjudicate"] is True
    assert cli("answer", "local-1", "--text", "patch it").returncode == 0
    st = _state(env)
    assert st["status"] == "done" and st["run"]["extra_visits"] == {"implement": 1, "review": 1} and st["run"]["adjudicated"] == 1
    impl = [c for c in calls(env) if c.get("stage") == "implement"]
    assert len(impl) == 4 and "**Adjudicated**" in impl[-1]["prompt"] and "**Adjudicated**" not in impl[-2]["prompt"]
    last = [r for r in _records(env) if r["stage"] == "implement"][-1]
    assert last["via"] == {"from": "review", "route": "adjudicated"} and last["max_visits"] == 4
    assert last["result"]["outcomes"] == [{"finding_id": "R3-1", "outcome": "fixed"}]
    review4 = [c for c in calls(env) if c.get("stage") == "review"][-1]
    assert "After the user's adjudication" in review4["prompt"] and "This is the last review" not in review4["prompt"]
    assert [s["visit_max"] for s in _stages_view(env) if s["id"] in ("implement", "review")] == [4, 4]


def test_a_second_adjudication_in_a_run_fails_it(env):
    _adjudicating_script(env, {"verdict": "NEEDS_USER", "askThen": "implement", "questions": [{"text": "again?"}]})
    cli("new", "feat: thing")
    cli("run", "local-1")
    assert cli("answer", "local-1", "--text", "patch it").returncode == 1
    st = _state(env)
    assert st["status"] == "failed" and "review: still not settled after the user's adjudication" in st["error"]


def test_unknown_ask_then_is_ignored_with_a_warning(env):
    script(env, {"verify": [{"verdict": "NEEDS_USER", "askThen": "review", "questions": [{"text": "?"}]}]})
    cli("new", "feat: thing")
    assert cli("run", "local-1").returncode == 3
    st = _state(env)
    assert st["next"]["stage"] == "verify" and st["rounds"][-1]["continue_at"] is None
    rec = [r for r in _records(env) if r["stage"] == "verify"][-1]
    assert any('askThen "review" ignored (allowed: verify' in w for w in rec["result_warnings"])


def _stages_view(env):
    from ziggy import config, pipeline
    return pipeline.stages_view(config.load_target("demo"), "local-1")
