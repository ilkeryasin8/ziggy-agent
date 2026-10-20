"""Codex review of a done ticket: running it, posting it to the PR, splitting it into findings, and
sending the kept findings back as fix tasks (`fix`). `codex`, `gh` and `claude` are fakes."""
import json

import pytest
from conftest import script, ROOT, cli, gh_calls, git
from test_publish import done_ticket as _done

from ziggy import config, review, state

FAKE_CODEX = ROOT / "tests" / "fake_codex.py"


@pytest.fixture
def codex(env, monkeypatch, tmp_path):
    monkeypatch.setenv("ZIGGY_CODEX", str(FAKE_CODEX))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    return tmp_path / "codex.log"


def done_ticket(env):
    return _done(env, "Sync pins")


def st(env, ticket_id):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def test_review_runs_codex_on_the_branch_against_its_base(env, codex):
    ticket_id = cli("new", "chore: Not yet").stdout.split("\t")[0]
    assert cli("review", ticket_id).returncode == 2  # not done yet
    ticket_id = done_ticket(env)
    proc = cli("review", ticket_id)
    assert proc.returncode == 0, proc.stderr
    call = json.loads(codex.read_text().splitlines()[-1])
    assert call["argv"][:3] == ["review", "--base", "origin/main"] and 'sandbox_mode="read-only"' in call["argv"]
    assert call["cwd"] == st(env, ticket_id)["worktree"] and call["run_env"] is None
    target = config.load_target("demo")
    [r] = review.reviews(target, ticket_id)
    assert r["state"] == "ok" and "[P1] Old cursor API" in r["text"] and not r["stale"]
    assert git("status", "--porcelain", cwd=st(env, ticket_id)["worktree"]) == ""


def test_a_failed_review_is_recorded_as_an_error(env, codex, monkeypatch):
    ticket_id = done_ticket(env)
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    proc = cli("review", ticket_id)
    assert proc.returncode == 2 and "not logged in" in proc.stderr
    assert review.reviews(config.load_target("demo"), ticket_id)[0]["state"] == "error"


def test_post_needs_the_pr_to_show_the_reviewed_commit(env, codex):
    ticket_id = done_ticket(env)
    assert cli("review", ticket_id).returncode == 0
    target = config.load_target("demo")
    rid = review.reviews(target, ticket_id)[0]["id"]
    with pytest.raises(review.ReviewError, match="no pull request"):
        review.post(target, ticket_id, rid)
    assert cli("publish", ticket_id).returncode == 0
    assert review.post(target, ticket_id, rid).endswith("/pull/1")
    comment = [c["argv"] for c in gh_calls(env) if c["argv"][:2] == ["pr", "comment"]]
    assert len(comment) == 1 and "--body-file" in comment[0]
    with pytest.raises(review.ReviewError, match="already on the PR"):
        review.post(target, ticket_id, rid)


def test_findings_split_a_review_into_items():
    text = ("Summary line.\n\nFull review comments:\n\n"
            "- [P1] Old cursor API still used — feature.txt:1-3\n  The worker calls `find().count()`.\n  Use count_documents.\n\n"
            "- [P3] No location here\n  Just a note.\n\nTrailing text.\n")
    first, second = review.findings(text)
    assert first == {"id": 1, "priority": "P1", "title": "Old cursor API still used", "location": "feature.txt:1-3",
                     "body": "The worker calls `find().count()`.\nUse count_documents.",
                     "text": "- [P1] Old cursor API still used — feature.txt:1-3\n  The worker calls `find().count()`.\n  Use count_documents."}
    assert second["id"] == 2 and second["location"] is None and second["body"] == "Just a note."
    assert review.findings("No issues found.\n") == []


def test_fix_turns_kept_findings_into_tasks_of_a_new_run(env, codex, monkeypatch, tmp_path):
    script(env, {"implement": [{"verdict": "READY", "summary": "fixed", "_commit": "fix.txt"}]})
    ticket_id = done_ticket(env)
    assert cli("publish", ticket_id).returncode == 0
    published = st(env, ticket_id)["published_head"]
    assert cli("review", ticket_id).returncode == 0
    target = config.load_target("demo")
    kept = review.reviews(target, ticket_id)[0]["text"].split("- [P3]")[0]

    proc = cli("fix", ticket_id, "--text", kept)
    assert proc.returncode == 0, proc.stderr
    s = st(env, ticket_id)
    assert s["status"] == "done" and s["user_reviews"][-1]["source"] == "codex"
    plan = state.plan_path(target, ticket_id).read_text()
    assert "Review (codex, round 1)" in plan and "Old cursor API" in plan and "Missing test" not in plan
    head = git("rev-parse", "HEAD", cwd=s["worktree"]).strip()
    assert head != published and review.reviews(target, ticket_id)[0]["stale"]
    assert cli("publish", ticket_id).returncode == 0
    assert git("--git-dir", str(env["origin"]), "rev-parse", "chore/sync-pins", cwd=env["repo"]).strip() == head
