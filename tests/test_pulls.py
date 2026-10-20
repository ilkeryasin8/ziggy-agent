"""Open PRs of a target adopted as tickets: listed with the ticket following each, reviewed by
Codex at the PR's current head, fixed by a pipeline run on top of the PR (`fix`), and pushed to the PR's own branch
(fast-forward only; never to a fork; someone else's only when confirmed). `gh`, `codex` and
`claude` are fakes; the PR lives on the bare origin as a branch plus refs/pull/<n>/head."""
import json
from pathlib import Path

import pytest
from conftest import script, cli, gh_calls, git
from test_dashboard import panel, request, settle  # noqa: F401 - the panel fixture
from test_review import codex  # noqa: F401 - the codex fixture

from ziggy import config, pulls, state


def target():
    return config.load_target("demo")


def push_pr(env, number=7, branch="feature/units", text="metric\n"):
    """A PR branch with one commit on origin, and GitHub's refs/pull/<n>/head pointing at it."""
    repo = env["repo"]
    git("checkout", "-q", "-B", branch, "origin/main" if not _on_origin(env, branch) else f"origin/{branch}", cwd=repo)
    (repo / "units.txt").write_text(text)
    git("add", "units.txt", cwd=repo)
    git("commit", "-q", "-m", f"feat: units {text.strip()}", cwd=repo)
    git("push", "-q", "origin", f"{branch}:{branch}", cwd=repo)
    sha = git("rev-parse", "HEAD", cwd=repo).strip()
    git("--git-dir", str(env["origin"]), "update-ref", f"refs/pull/{number}/head", sha, cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return sha


def _on_origin(env, branch):
    return git("--git-dir", str(env["origin"]), "branch", "--list", branch, cwd=env["repo"]).strip() != ""


@pytest.fixture
def pr(env, monkeypatch):
    sha = push_pr(env)
    listed = [{"number": 7, "title": "feat: Show units", "url": "https://github.example.test/acme/demo/pull/7",
               "author": {"login": "tester"}, "headRefName": "feature/units", "baseRefName": "main", "isDraft": False,
               "isCrossRepository": False, "headRefOid": sha, "updatedAt": "2026-10-13T10:00:00Z", "body": "Adds units."}]
    monkeypatch.setenv("FAKE_GH_PULLS", json.dumps(listed))
    pulls._cache.clear()
    return {"sha": sha, "listed": listed}


def set_pulls(monkeypatch, pr, **changes):
    pr["listed"][0].update(changes)
    monkeypatch.setenv("FAKE_GH_PULLS", json.dumps(pr["listed"]))
    pulls._cache.clear()


def st(ticket_id):
    return state.load_state(target(), ticket_id)


def test_adopt_makes_a_done_ticket_at_the_pr_head(env, pr):
    [item] = pulls.listing(target())
    assert item["number"] == 7 and item["ticket"] is None and item["mine"] and item["head"] == "feature/units"
    proc = cli("adopt", "7")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("pr-7\tpr/7-show-units\t")
    t = state.load_ticket(target(), "pr-7")
    assert t["pull"]["head"] == "feature/units" and t["base_branch"] == "main" and t["type"] == "feat"
    s = st("pr-7")
    assert s["status"] == "done" and s["pr"].endswith("/pull/7") and s["published_head"] == pr["sha"]
    assert git("rev-parse", "HEAD", cwd=s["worktree"]).strip() == pr["sha"]
    md = (env["state"] / "runs" / "pr-7" / "ticket.md").read_text()
    assert "- Pull request: #7" in md and "git diff origin/main...HEAD" in md and "Adds units." in md
    assert pulls.listing(target())[0]["ticket"] == "pr-7"
    assert pulls.adopt(target(), 7)["id"] == "pr-7"  # again: the same ticket
    assert "#7\tpr-7\ttester" in cli("prs").stdout


def test_review_follows_the_pr_and_fixes_go_to_its_own_branch(env, pr, codex, monkeypatch, tmp_path):  # noqa: F811
    script(env, {"implement": [{"verdict": "READY", "summary": "fixed", "_commit": "fix.txt"}]})
    assert cli("adopt", "7").returncode == 0
    newer = push_pr(env, text="metric and imperial\n")  # the author pushed after the adoption
    proc = cli("review", "pr-7")
    assert proc.returncode == 0, proc.stderr
    wt = st("pr-7")["worktree"]
    assert git("rev-parse", "HEAD", cwd=wt).strip() == newer and st("pr-7")["published_head"] == newer
    call = json.loads(codex.read_text().splitlines()[-1])
    assert call["argv"][:3] == ["review", "--base", "origin/main"]

    assert cli("publish", "pr-7").returncode == 2  # nothing new over the PR yet
    proc = cli("fix", "pr-7", "--text", "- [P1] Old cursor API — feature.txt:1\n  Use count_documents.")
    assert proc.returncode == 0, proc.stderr
    s = st("pr-7")
    assert s["status"] == "done" and git("merge-base", "--is-ancestor", newer, "HEAD", cwd=wt) == ""
    assert git("rev-parse", "HEAD", cwd=wt).strip() != newer and (Path(wt) / "units.txt").read_text() == "metric and imperial\n"
    assert "Old cursor API" in state.plan_path(target(), "pr-7").read_text()
    proc = cli("publish", "pr-7")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("/pull/7")
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    assert git("--git-dir", str(env["origin"]), "rev-parse", "feature/units", cwd=env["repo"]).strip() == head
    assert st("pr-7")["published_head"] == head
    assert not [c for c in gh_calls(env) if c["argv"][:2] == ["pr", "create"]]


def test_someone_elses_pr_needs_a_confirmation_and_a_fork_is_never_pushed(env, pr, monkeypatch):
    set_pulls(monkeypatch, pr, author={"login": "app/dependabot", "is_bot": True})
    assert cli("adopt", "7").returncode == 0
    wt = st("pr-7")["worktree"]
    open(f"{wt}/fix.txt", "w").write("fix\n")
    git("add", "fix.txt", cwd=wt)
    git("commit", "-q", "-m", "fix: review finding", cwd=wt)
    proc = cli("publish", "pr-7")
    assert proc.returncode == 2 and "app/dependabot's" in proc.stderr
    assert cli("publish", "pr-7", "--foreign-ok").returncode == 0

    t = state.load_ticket(target(), "pr-7")
    t["pull"]["fork"] = True
    state.write_json(env["state"] / "runs" / "pr-7" / "ticket.json", t)
    proc = cli("publish", "pr-7", "--foreign-ok")
    assert proc.returncode == 2 and "fork" in proc.stderr


def test_a_diverged_branch_stops_instead_of_merging(env, pr):
    assert cli("adopt", "7").returncode == 0
    wt = st("pr-7")["worktree"]
    open(f"{wt}/local.txt", "w").write("local\n")
    git("add", "local.txt", cwd=wt)
    git("commit", "-q", "-m", "fix: local", cwd=wt)
    push_pr(env, text="force-pushed\n")
    with pytest.raises(pulls.PullError, match="diverged"):
        pulls.sync_ticket(target(), "pr-7")
    proc = cli("publish", "pr-7")
    assert proc.returncode == 2 and "moved on" in proc.stderr


def test_panel_lists_adopts_and_guards_the_push(panel, env, pr, codex, monkeypatch):  # noqa: F811
    set_pulls(monkeypatch, pr, author={"login": "teammate"})
    code, out, _ = request(panel, "GET", "/api/targets/demo/pulls?fresh=1")
    assert code == 200 and out["pulls"][0]["ticket"] is None and not out["pulls"][0]["mine"]
    code, out, _ = request(panel, "POST", "/api/targets/demo/pulls", {"number": 7, "review": True})
    assert code == 200, out
    assert out["ticket"]["id"] == "pr-7" and out["job"]["command"] == "review pr-7"
    r = settle(panel, "pr-7")
    assert r["review"]["state"] == "ok" and r["pull_foreign"] and r["pull"]["number"] == 7
    assert request(panel, "GET", "/api/targets/demo/pulls")[1]["pulls"][0]["ticket"] == "pr-7"

    wt = st("pr-7")["worktree"]
    open(f"{wt}/fix.txt", "w").write("fix\n")
    git("add", "fix.txt", cwd=wt)
    git("commit", "-q", "-m", "fix: review finding", cwd=wt)
    assert settle(panel, "pr-7")["needs_you"]["kind"] == "push"
    act = {"target": "demo", "id": "pr-7", "action": "publish"}
    code, out, _ = request(panel, "POST", "/api/action", act)
    assert code == 409 and "teammate's" in out["error"]
    code, out, _ = request(panel, "POST", "/api/action", {**act, "text": "foreign-ok"})
    assert code == 200 and out["job"]["command"] == "publish pr-7"
    assert settle(panel, "pr-7")["needs_you"] is None
    assert request(panel, "POST", "/api/targets/demo/pulls", {"number": "x"})[0] == 400
