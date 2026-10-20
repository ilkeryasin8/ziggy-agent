"""CLI tests: ticket creation, the stage engine end to end (questions, approval gate, review loop,
checks, stop/retry, request-changes), exit codes, slots and the status output.

Runs the real CLI against a throwaway repository with a bare origin; `claude` is tests/fake_claude.py,
which writes each stage's result from FAKE_RESULTS.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import BIN, ROOT, calls, cli, gh_calls, git, script

from ziggy import cli as cli_mod
from ziggy import checks, ticket

STAGE_DIR = re.compile(r"^\d{2,}-([a-z][a-z0-9-]*)$")


def new(title="Sync pymongo pins", *extra):
    proc = cli("new", title, "--type", "chore", *extra)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("\t")[0]


def results(env, monkeypatch, data: dict):
    script(env, data)


def run_root(env, ticket_id) -> Path:
    return env["state"] / "runs" / ticket_id


def state_of(env, ticket_id) -> dict:
    return json.loads((run_root(env, ticket_id) / "state.json").read_text())


def stages_ran(env, ticket_id) -> list[str]:
    dirs = [d.name for d in run_root(env, ticket_id).iterdir() if STAGE_DIR.match(d.name)]
    return [STAGE_DIR.match(n)[1] for n in sorted(dirs, key=lambda n: int(n.split("-", 1)[0]))]


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# --- tickets ---------------------------------------------------------------------------------


def test_slug_is_ascii_and_short():
    assert ticket.slugify("Login timeout'u 30 sn yap, çok uzun") == "login-timeout-u-30-sn"
    assert ticket.slugify("Şirket ığdır güncelleme") == "sirket-igdir-guncelleme"


def test_types_map_to_pipelines():
    assert ticket.TYPES == ("feat", "fix", "refactor", "chore", "docs", "project")
    assert {t: ticket.pipeline_for(t) for t in ticket.TYPES} == {
        "feat": "feat", "fix": "fix", "refactor": "chore", "chore": "chore", "docs": "chore", "project": "project"}


def test_new_writes_contract_and_avoids_branch_collisions(env):
    git("branch", "chore/sync-pymongo-pins", cwd=env["repo"])
    first = cli("new", "Sync pymongo pins", "--type", "chore", "--body-file", "-", stdin="Worker and backend differ.\n")
    second = cli("new", "Sync pymongo pins", "--type", "chore")
    assert first.stdout.split("\t")[:2] == ["local-1", "chore/sync-pymongo-pins-2"]
    assert second.stdout.split("\t")[:2] == ["local-2", "chore/sync-pymongo-pins-3"]
    assert first.stdout.strip().endswith(f"demo ({env['repo']})")

    run = run_root(env, "local-1")
    t = json.loads((run / "ticket.json").read_text())
    assert t["source"] == "local" and t["base_branch"] == "main" and t["plan_checkpoint"] is False
    assert "stage" not in t and "story" not in t
    md = (run / "ticket.md").read_text()
    assert md.index("## Pipeline") < md.index("## Ticket (untrusted)") < md.index("Worker and backend differ.") < md.index("## Decisions (user)")
    assert "- Pipeline: chore" in md and "BMad" not in md
    st = state_of(env, "local-1")
    assert st["status"] == "new" and st["pipeline"] == "chore" and st["rounds"] == []


def test_a_project_ticket_gets_a_docs_branch(env):
    proc = cli("new", "project: Meal planner")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\t")[1] == "docs/project-meal-planner"
    assert state_of(env, "local-1")["pipeline"] == "project"


def test_new_takes_the_type_from_the_title(env):
    proc = cli("new", "fix: Login timeout'u 30 sn yap")
    assert proc.returncode == 0, proc.stderr
    t = json.loads((run_root(env, "local-1") / "ticket.json").read_text())
    assert (t["type"], t["title"], t["branch"]) == ("fix", "Login timeout'u 30 sn yap", "fix/login-timeout-u-30-sn")
    bad = cli("new", "perf: hızlandır")
    assert bad.returncode == 2 and "bilinmeyen tip `perf`" in bad.stderr
    assert cli("new", "prd: meal planning").returncode == 2  # BMad stages are gone
    clash = cli("new", "fix: x", "--type", "chore")
    assert clash.returncode == 2 and "disagree" in clash.stderr
    assert cli("new", "x", "--type", "perf").returncode == 2


@pytest.mark.parametrize("line", ['"chore: Remove langchain"', "'chore: Remove langchain'", "  `chore: Remove langchain`  "])
def test_quoted_title_keeps_its_type(line):
    assert ticket.split_type(line) == ("chore", "Remove langchain")


def test_new_in_editor(env, tmp_path):
    editor = tmp_path / "editor.sh"
    editor.write_text(
        "#!/bin/sh\n"
        "body=$(cat \"$1\")\n"
        "printf 'chore: Remove langchain\\n\\n## Context\\nUnused.\\n%s' \"$body\" > \"$1\"\n"
    )
    editor.chmod(0o755)
    proc = cli("new", env={**os.environ, "VISUAL": str(editor), "EDITOR": str(editor)})
    assert proc.returncode == 0, proc.stderr
    run = run_root(env, "local-1")
    t = json.loads((run / "ticket.json").read_text())
    assert (t["type"], t["title"]) == ("chore", "Remove langchain")
    md = (run / "ticket.md").read_text()
    assert "## Context\nUnused." in md and "scissors" not in md and ">8" not in md


def test_new_in_editor_with_empty_title_cancels(env):
    proc = cli("new", env={**os.environ, "VISUAL": "true", "EDITOR": "true"})
    assert proc.returncode == 1 and "no ticket created" in proc.stderr
    assert not (env["state"] / "runs").exists()


def test_target_follows_the_current_directory(env, tmp_path):
    other = tmp_path / "other"
    git("clone", "-q", str(env["origin"]), str(other), cwd=tmp_path)
    (other / "sub").mkdir()
    env["config"].write_text(env["config"].read_text() + f'[targets.other]\npath = "{other}"\n')
    inside = cli("new", "chore: In other", cwd=other / "sub")  # any subdirectory counts
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout.strip().endswith(f"other ({other})")
    outside = cli("new", "chore: Elsewhere", cwd=tmp_path)
    assert outside.stdout.strip().endswith(f"demo ({env['repo']})")
    explicit = cli("--target", "demo", "new", "chore: Explicit", cwd=other)
    assert explicit.stdout.strip().endswith(f"demo ({env['repo']})")


def test_a_ticket_can_ask_for_turkish_output(env):
    ticket_id = cli("new", "chore: Sync pins", "--lang", "tr").stdout.split("\t")[0]
    md = (run_root(env, ticket_id) / "ticket.md").read_text()
    assert "- Output language: Turkish." in md and "commit messages stay in English" in md
    plain = cli("new", "chore: Other").stdout.split("\t")[0]
    assert "Output language" not in (run_root(env, plain) / "ticket.md").read_text()
    assert cli("new", "chore: Bad", "--lang", "de").returncode == 2


def test_every_command_parses():
    p = cli_mod.parser()
    for argv in (["new", "x"], ["run", "a-1"], ["run", "a-1", "--full-review"], ["retry", "a-1", "--full-review"],
                 ["answer", "a-1", "--text", "x"], ["approve", "a-1"], ["split", "a-1", "--no-run"],
                 ["new", "x", "--after", "a-1", "--after", "a-2"], ["after", "a-1", "a-2"], ["after", "a-1"],
                 ["restack", "a-1"], ["restack", "a-1", "--push"],
                 ["request-changes", "a-1"], ["fix", "a-1", "--review", "r"], ["resume", "a-1"], ["stop", "a-1"],
                 ["nudge", "a-1", "--text", "x"], ["nudge", "a-1", "--file", "-"], ["chat", "a-1", "--text", "x", "--new"],
                 ["chat-stop", "a-1"], ["inbox", "--all", "--seen"],
                 ["status"], ["watch", "a-1"], ["log", "a-1"], ["open", "a-1", "--plan"], ["publish", "a-1"],
                 ["publish", "a-1", "--title", "t", "--body-file", "-"],
                 ["review", "a-1"], ["prs"], ["adopt", "3"], ["sync"], ["cleanup"], ["cleanup", "a-1", "--force"], ["route", "x"],
                 ["target", "add", "."], ["project", "new", "x"], ["sessions", "--all"], ["ui"], ["listen"]):
        assert p.parse_args(argv).func
    with pytest.raises(SystemExit):
        p.parse_args(["chat", "a-1"])  # a question is required
    for gone in ("plan", "build", "stages", "bmad-setup", "review-setup", "decisions"):
        with pytest.raises(SystemExit):
            p.parse_args([gone, "a-1"])


# --- steering, chat, cleanup, inbox, publishing ------------------------------------------------


def test_nudge_refuses_what_it_cannot_deliver(env):
    ticket_id = new()
    proc = cli("nudge", ticket_id, "--text", "use the v2 API")
    assert proc.returncode == 2 and proc.stderr.startswith("ziggy: ") and ticket_id in proc.stderr
    proc = cli("nudge", ticket_id, stdin="   \n")  # stdin by default, and empty
    assert proc.returncode == 2 and "no message text" in proc.stderr


def test_retry_full_review_starts_a_new_run(env):
    ticket_id = new()
    proc = cli("retry", ticket_id, "--full-review")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "(reviews read the whole branch)" in proc.stdout
    assert state_of(env, ticket_id)["run"]["full_review"] is True
    assert cli("retry", ticket_id, "--full-review").returncode == 2  # a done ticket has no new run to start


def test_cleanup_one_ticket_keeps_its_branch(env):
    ticket_id = new()
    assert cli("cleanup", ticket_id).returncode == 2  # not finished: nothing to remove yet
    assert cli("run", ticket_id).returncode == 0
    st = state_of(env, ticket_id)
    assert st["worktree"] and Path(st["worktree"]).is_dir()
    assert cli("cleanup", "--force").returncode == 2  # --force needs a ticket
    proc = cli("cleanup", ticket_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    branch = json.loads((run_root(env, ticket_id) / "ticket.json").read_text())["branch"]
    assert "kept" in proc.stdout and branch in proc.stdout
    assert not Path(st["worktree"]).exists() and state_of(env, ticket_id)["worktree"] is None
    assert git("branch", "--list", branch, cwd=env["repo"]).strip()


def test_inbox_lists_what_needs_you_and_marks_it_read(env, monkeypatch):
    proc = cli("inbox")
    assert proc.returncode == 0 and proc.stdout.strip() == "nothing needs you"
    results(env, monkeypatch, {"plan": [{"verdict": "FAILED", "summary": "cannot plan"}]})
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 1
    proc = cli("inbox", "--seen")
    assert proc.returncode == 0, proc.stderr
    line = next(x for x in proc.stdout.splitlines() if f"{ticket_id}:" in x)
    assert line.startswith("new\tfailed\t") and "\tdemo/failed:" in line
    items = json.loads(cli("inbox", "--json").stdout)
    mine = [i for i in items if i["ticket"] == ticket_id]
    assert mine and all(i["seen_at"] for i in mine)
    assert cli("inbox").stdout.startswith("seen\t")


def test_chat_refuses_empty_questions_and_has_nothing_to_stop(env):
    ticket_id = new()
    proc = cli("chat", ticket_id, "--text", "   ")
    assert proc.returncode == 2 and proc.stderr.startswith("ziggy: ")
    assert cli("chat-stop", ticket_id).returncode == 2


def test_publish_uses_the_given_title_and_description(env, monkeypatch, tmp_path):
    results(env, monkeypatch, {"implement": [{"verdict": "READY", "summary": "pinned", "_commit": "requirements.txt"}]})
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    body = tmp_path / "body.md"
    body.write_text("Written by hand.\n\n## Verify\n- run the tests\n")
    proc = cli("publish", ticket_id, "--title", "chore: Pin pymongo for CI", "--body-file", str(body))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    created = next(c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"])
    assert created["argv"][created["argv"].index("--title") + 1] == "chore: Pin pymongo for CI"
    assert "--draft" in created["argv"] and created["body"].startswith("Written by hand.\n\n## Verify\n")


# --- running ---------------------------------------------------------------------------------


def test_run_goes_through_the_pipeline_without_pushing(env):
    ticket_id = new()
    refs_before = git("for-each-ref", cwd=env["origin"])
    proc = cli("run", ticket_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{ticket_id}: done" in proc.stdout

    st = state_of(env, ticket_id)
    wt = Path(st["worktree"])
    assert st["status"] == "done" and wt == env["state"] / "worktrees" / ticket_id
    assert stages_ran(env, ticket_id) == ["plan", "implement", "review"]
    assert git("for-each-ref", cwd=env["origin"]) == refs_before
    assert git("branch", "--show-current", cwd=wt).strip() == "chore/sync-pymongo-pins"
    assert (env["state"] / f"prepared-{ticket_id}").is_file()

    for call in calls(env):
        argv = call["argv"]
        assert argv[argv.index("--settings") + 1] == str(ROOT / "claude-settings.json")
        assert argv[argv.index("--setting-sources") + 1] == "project"
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--add-dir") + 1] == str(run_root(env, ticket_id) / "inbox")
        assert call["env"]["ZIGGY_RUN"] == "1"
        assert call["env"]["TEST_DB_URI"] == f"mongodb://127.0.0.1:27017/demo_{ticket_id}"
        assert call["push_exit"] != 0
    assert "hooksPath" not in (env["repo"] / ".git" / "config").read_text()


def test_new_with_run(env):
    proc = cli("new", "chore: Sync pins", "--run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "local-1: done" in proc.stdout and "cost=$" in proc.stdout


def test_questions_wait_for_an_answer_then_continue(env, monkeypatch):
    results(env, monkeypatch, {"plan": [
        {"verdict": "NEEDS_USER", "summary": "which file?",
         "questions": [{"id": "q1", "text": "Which pins file?", "options": ["worker", "both"], "recommendation": "both"}]},
        {"verdict": "READY", "summary": "planned"},
    ]})
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    st = state_of(env, ticket_id)
    assert st["status"] == "needs-user" and st["rounds"][-1]["answer"] is None

    status = cli("status", ticket_id).stdout
    assert "Which pins file?" in status and "both (recommended)" in status
    assert f"ziggy answer {ticket_id}" in status
    assert cli("approve", ticket_id).returncode == 2  # not at the gate

    proc = cli("answer", ticket_id, "--text", "Both files.")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    st = state_of(env, ticket_id)
    assert st["status"] == "done" and st["rounds"][-1]["answer"]
    assert stages_ran(env, ticket_id) == ["plan", "plan", "implement", "review"]
    assert "Both files." in (run_root(env, ticket_id) / "answers.md").read_text()
    assert "## Decisions (user)" in (run_root(env, ticket_id) / "inbox" / "plan.md").read_text()


def test_answer_refuses_a_new_ticket(env):
    ticket_id = new()
    assert cli("answer", ticket_id, "--text", "x").returncode == 2
    assert cli("answer", ticket_id, "--text", "   ").returncode == 2


def test_a_risky_plan_waits_at_the_gate(env, monkeypatch):
    results(env, monkeypatch, {"plan": [{"verdict": "READY", "summary": "big", "risk": "high", "size": "large"}]})
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    st = state_of(env, ticket_id)
    assert st["status"] == "awaiting-approval" and st["gate"]["stage"] == "plan" and st["risk"] == "high"
    status = cli("status", ticket_id).stdout
    assert "waiting for your approval after plan" in status and f"ziggy approve {ticket_id}" in status
    assert "risk: high  size: large" in status
    assert cli("run", ticket_id).returncode == 2  # run refuses a gated ticket

    proc = cli("approve", ticket_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state_of(env, ticket_id)["status"] == "done"
    assert stages_ran(env, ticket_id) == ["plan", "implement", "review"]


def test_plan_checkpoint_forces_the_gate(env):
    ticket_id = new("Small change", "--plan-checkpoint")
    assert cli("run", ticket_id).returncode == 3
    assert state_of(env, ticket_id)["status"] == "awaiting-approval"
    assert len(calls(env)) == 1
    assert cli("approve", ticket_id).returncode == 0


def test_review_fix_loop_goes_back_to_implement(env, monkeypatch):
    results(env, monkeypatch, {"review": [{"verdict": "READY", "route": "fix", "summary": "one bug"},
                                          {"verdict": "READY", "route": "converged", "summary": "clean"}]})
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    assert stages_ran(env, ticket_id) == ["plan", "implement", "review", "implement", "review"]


def test_review_loop_cap_fails_the_run(env, monkeypatch):
    results(env, monkeypatch, {"review": [{"verdict": "READY", "route": "fix", "summary": "still a bug"}]})
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    st = state_of(env, ticket_id)
    assert st["status"] == "failed" and "loop cap reached" in st["error"]
    assert "loop cap reached" in cli("status", ticket_id).stdout


@pytest.mark.skipif(not os.access(checks.SANDBOX_EXEC, os.X_OK), reason="checks only run under macOS sandbox-exec")
def test_failed_checks_route_back_to_implement(env, monkeypatch):
    env["config"].write_text(env["config"].read_text() + '[[targets.demo.checks]]\nname = "ok-file"\nrun = "test -f ok.txt"\n')
    results(env, monkeypatch, {"implement": [{"verdict": "READY", "summary": "first"},
                                             {"verdict": "READY", "summary": "fixed", "_commit": "ok.txt"}]})
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert stages_ran(env, ticket_id) == ["plan", "implement", "implement", "review"]
    st = state_of(env, ticket_id)
    assert [(c["name"], c["status"]) for c in st["checks"]] == [("ok-file", "pass")]


def test_a_failed_stage_exits_1_and_run_retries_it(env, monkeypatch):
    results(env, monkeypatch, {"implement": [{"verdict": "FAILED", "summary": "cannot build"}, {"verdict": "READY"}]})
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 1
    st = state_of(env, ticket_id)
    assert st["status"] == "failed" and "implement" in st["error"]
    assert f"ziggy run {ticket_id}" in cli("status", ticket_id).stdout
    assert cli("run", ticket_id).returncode == 0
    assert stages_ran(env, ticket_id) == ["plan", "implement", "implement", "review"]


def test_no_result_file_fails_the_stage(env, monkeypatch):
    results(env, monkeypatch, {"plan": [{"_no_result": True}]})
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 1
    assert "result file" in state_of(env, ticket_id)["error"]


def test_a_verdict_line_is_enough(env, monkeypatch):
    results(env, monkeypatch, {"plan": [{"_verdict_line": "READY"}]})
    assert cli("run", new()).returncode == 0


def test_stop_fails_the_run_and_run_retries_the_stage(env, monkeypatch):
    results(env, monkeypatch, {"implement": [{"verdict": "READY", "_sleep": 60}, {"verdict": "READY"}]})
    ticket_id = new()
    running = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    root = run_root(env, ticket_id)
    assert wait_for(lambda: any((d / "pid").exists() for d in root.glob("*-implement")))
    stop = cli("stop", ticket_id)
    assert stop.returncode == 0 and "stop requested" in stop.stdout
    running.communicate(timeout=60)
    assert running.returncode == 1
    st = state_of(env, ticket_id)
    assert st["status"] == "failed" and st["next"]["stage"] == "implement"
    assert cli("run", ticket_id).returncode == 0
    assert stages_ran(env, ticket_id) == ["plan", "implement", "implement", "review"]


def test_codex_findings_start_a_new_run_at_implement(env):
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    proc = cli("fix", ticket_id, "--text", "- [P1] Pins differ in worker.py\n- [P2] Missing test for the bump\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Review (codex, round 1) (2 finding(s))" in proc.stdout
    assert stages_ran(env, ticket_id) == ["plan", "implement", "review", "implement", "review"]
    plan = (run_root(env, ticket_id) / "inbox" / "plan.md").read_text()
    section = plan[plan.index("## Review (codex, round 1)"):]
    assert len(re.findall(r"^- \[ \] T\d{3,} Fix: ", section, re.M)) == 2
    st = state_of(env, ticket_id)
    assert st["user_reviews"][-1]["source"] == "codex" and st["user_reviews"][-1]["comments"] == 2
    again = cli("fix", ticket_id, "--text", "- one more", "--no-run")
    assert again.returncode == 0 and "Review (codex, round 2)" in again.stdout


def test_request_changes_without_drafts_refuses(env):
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    proc = cli("request-changes", ticket_id)
    assert proc.returncode == 2 and proc.stderr.startswith("ziggy: ")


def test_fix_without_a_review_refuses(env):
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    assert cli("fix", ticket_id).returncode == 2


# --- slots -----------------------------------------------------------------------------------


def hold_slot(env, name, pid=None):
    lock = env["state"] / "locks" / name
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{pid or os.getpid()} active run (local-9)\n")
    return lock


def test_a_free_slot_runs_and_no_wait_refuses_when_all_are_busy(env):
    ticket_id = new()
    hold_slot(env, "active")  # one of the two default slots
    assert cli("run", ticket_id).returncode == 0
    sessions = len(calls(env))
    hold_slot(env, "active-2")
    other = new("Other pins")
    proc = cli("--no-wait", "run", other)
    assert proc.returncode == 2 and "all 2 run slots of demo are busy" in proc.stderr
    assert len(calls(env)) == sessions


def test_a_run_waits_in_line_for_a_slot(env):
    ticket_id = new()
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for name in ("active", "active-2"):
            hold_slot(env, name, holder.pid)
        waiting = subprocess.Popen([sys.executable, str(BIN), "run", ticket_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        state_file = run_root(env, ticket_id) / "state.json"
        assert wait_for(lambda: "waiting_since" in json.loads(state_file.read_text()))
        assert calls(env) == []
    finally:
        holder.kill()
        holder.wait()
    _, err = waiting.communicate(timeout=60)
    assert waiting.returncode == 0, err
    assert "waiting for a run slot (2/2 busy)" in err
    st = json.loads(state_file.read_text())
    assert st["status"] == "done" and "waiting_since" not in st


def test_stale_lock_is_taken_over(env):
    ticket_id = new()
    lock = env["state"] / "locks" / "active"
    lock.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (lock / "owner").write_text(f"{dead.pid} active run (local-9)\n")
    assert cli("run", ticket_id).returncode == 0
    assert not lock.exists()


def test_failed_prepare_stops_before_dispatch(env):
    env["config"].write_text(env["config"].read_text().replace('prepare = ["echo prepared > {state}/prepared-{run_id}"]', 'prepare = ["exit 7"]'))
    ticket_id = new()
    proc = cli("run", ticket_id)
    assert proc.returncode == 2 and "exit 7" in proc.stderr
    assert calls(env) == []
    assert state_of(env, ticket_id)["prepared"] is False


def test_session_without_result_event_marks_failed(env, tmp_path, monkeypatch):
    silent = tmp_path / "silent-claude"
    silent.write_text("#!/bin/sh\necho '{\"type\": \"system\", \"subtype\": \"init\"}'\nexit 1\n")
    silent.chmod(0o755)
    monkeypatch.setenv("ZIGGY_CLAUDE", str(silent))
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 1
    assert state_of(env, ticket_id)["status"] == "failed"


# --- status, log, open -----------------------------------------------------------------------


def test_status_log_and_open(env, monkeypatch):
    results(env, monkeypatch, {"plan": [{"verdict": "READY", "summary": "planned",
                                         "_plan": "# Plan\n\n- [x] T001 Bump the worker pin\n- [ ] T002 Bump the backend pin\n"}]})
    ticket_id = new()
    assert cli("run", ticket_id).returncode == 0
    listing = cli("status").stdout
    assert re.search(rf"^{ticket_id}\tdone\treview\tchore/sync-pymongo-pins\t\$\d+\.\d\d\t", listing, re.M)
    detail = cli("status", ticket_id).stdout
    assert "pipeline: chore" in detail
    assert "stages: plan ✓ → implement ✓ → review ✓" in detail
    assert re.search(r"tasks: \d+/\d+", detail)
    assert f"ziggy publish {ticket_id}" in detail
    log = cli("log", ticket_id, "--message").stdout
    assert log.count("record:") == 3 and "--- final message" in log
    assert cli("open", ticket_id).stdout.strip().endswith(f"worktrees/{ticket_id}")
    assert cli("open", ticket_id, "--plan").stdout.strip().endswith("inbox/plan.md")
    other = new("Other")
    assert cli("open", other, "--plan").returncode == 1
    assert f"next: ziggy run {other}" in cli("status", other).stdout
