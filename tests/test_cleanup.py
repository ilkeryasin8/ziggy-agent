"""Removing worktrees of tickets whose PR is merged or closed, and keeping the ones that still
hold work (uncommitted edits, commits the PR does not have, an open PR); removing one finished
ticket's worktree on request (teardown, kept branch, released ports)."""
import json
from dataclasses import replace

import pytest
from conftest import cli, git
from test_dashboard import panel, request  # noqa: F401 - the panel fixture
from test_publish import done_ticket
from test_worktree import add_worktree, make_ticket

from ziggy import cleanup, config, state, worktree


def published(env):
    ticket_id = done_ticket(env, "Sync pins")
    assert cli("publish", ticket_id).returncode == 0
    return ticket_id


def st(env, ticket_id):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def test_a_merged_ticket_loses_its_worktree_and_local_branch(env, panel):  # noqa: F811
    ticket_id = published(env)
    wt = st(env, ticket_id)["worktree"]
    code, out, _ = request(panel, "GET", "/api/cleanup")
    assert code == 200 and out["candidates"][0]["cleanable"] and out["candidates"][0]["delete_branch"]
    code, out, _ = request(panel, "POST", "/api/cleanup", {"items": [{"target": "demo", "id": ticket_id}]})
    assert code == 200 and out["results"] == [{"ok": True, "id": ticket_id, "pr_state": "MERGED", "branch_deleted": True}]
    assert not (env["state"] / "worktrees" / ticket_id).exists() and wt not in git("worktree", "list", cwd=env["repo"])
    assert "chore/sync-pins" not in git("branch", "--list", cwd=env["repo"])
    assert git("--git-dir", str(env["origin"]), "show-ref", "refs/heads/chore/sync-pins", cwd=env["repo"]).strip()  # remote kept
    s = st(env, ticket_id)
    assert s["worktree"] is None and s["archived"]["pr_state"] == "MERGED" and s["status"] == "done"
    _, snap, _ = request(panel, "GET", "/api/status")
    row = snap["tickets"][0]
    assert row["archived"] and row["needs_you"] is None
    assert (env["state"] / "runs" / ticket_id / "ticket.json").exists()  # history stays
    assert request(panel, "GET", "/api/cleanup")[1]["candidates"] == []


def test_work_still_in_the_worktree_is_kept(env, monkeypatch):
    ticket_id = published(env)
    target = config.load_target("demo")
    wt = st(env, ticket_id)["worktree"]
    (__import__("pathlib").Path(wt) / "notes.txt").write_text("draft\n")
    [c] = cleanup.candidates(target)
    assert not c["cleanable"] and "uncommitted: notes.txt" in c["reasons"]
    git("add", "notes.txt", cwd=wt)
    git("commit", "-q", "-m", "more", cwd=wt)
    [c] = cleanup.candidates(target)
    assert not c["cleanable"] and "local commits that the PR does not have" in c["reasons"]
    assert cli("cleanup", "--yes").returncode == 0 and (env["state"] / "worktrees" / ticket_id).exists()

    git("reset", "-q", "--hard", "HEAD~1", cwd=wt)
    monkeypatch.setenv("FAKE_GH_PR_STATE", "OPEN")
    [c] = cleanup.candidates(target)
    assert not c["cleanable"] and c["reasons"] == ["PR is open"]

    monkeypatch.setenv("FAKE_GH_PR_STATE", "CLOSED")
    proc = cli("cleanup", "--yes")
    assert proc.returncode == 0 and "worktree removed" in proc.stdout and "branch deleted" not in proc.stdout
    assert "chore/sync-pins" in git("branch", "--list", cwd=env["repo"])  # closed, not merged: branch kept


def test_commits_the_pr_holds_under_other_ids_do_not_keep_the_worktree(env):
    # A restacked slice reaches its PR as rewritten commits (a real run: fc7d9bf here, 53fcdeb on GitHub,
    # same tree). The worktree's branch is rewritten the same way here: the PR has the change, not the local ids.
    ticket_id = published(env)
    target = config.load_target("demo")
    wt = st(env, ticket_id)["worktree"]
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    git("commit", "-q", "--amend", "-m", "the same change, reworded", cwd=wt)  # new id, same tree
    assert git("rev-parse", "HEAD", cwd=wt).strip() != head
    [c] = cleanup.candidates(target)
    assert c["cleanable"], c["reasons"]

    (__import__("pathlib").Path(wt) / "extra.txt").write_text("not in the PR\n")
    git("add", "extra.txt", cwd=wt)
    git("commit", "-q", "-m", "really new", cwd=wt)
    [c] = cleanup.candidates(target)
    assert not c["cleanable"] and "local commits that the PR does not have" in c["reasons"]



def test_sync_archives_merged_tickets_and_keeps_ones_with_work(env, monkeypatch):
    target = config.load_target("demo")
    merged = published(env)
    monkeypatch.setenv("FAKE_GH_PR_STATE", "OPEN")
    assert cleanup.sync(target) == []
    assert st(env, merged)["pr_state"] == "OPEN" and not st(env, merged).get("archived")

    monkeypatch.setenv("FAKE_GH_PR_STATE", "MERGED")
    [r] = cleanup.sync(target)
    assert r["id"] == merged and r["branch_deleted"] is True
    s = st(env, merged)
    assert s["archived"] and s["pr_state"] == "MERGED" and s["worktree"] is None
    assert cleanup.sync(target) == []  # archived tickets are left alone


def test_sync_keeps_a_merged_ticket_with_local_work(env, panel):  # noqa: F811
    ticket_id = published(env)
    (__import__("pathlib").Path(st(env, ticket_id)["worktree"]) / "wip.txt").write_text("x\n")
    [r] = cleanup.sync(config.load_target("demo"))
    assert r["kept"] == ["uncommitted: wip.txt"]
    _, snap, _ = request(panel, "GET", "/api/status")
    row = snap["tickets"][0]
    assert row["pr_state"] == "MERGED" and not row["archived"] and row["needs_you"]["kind"] == "merged-kept"
    assert "wip.txt" in row["needs_you"]["details"]


def finished(env, ticket_id="local-1", status="done", **extra):
    """A finished ticket with a worktree whose branch has one commit, plus a port range."""
    ticket = make_ticket(env, ticket_id, status=status, port_base=4100, prepared=True, **extra)
    wt = add_worktree(env, ticket)
    (wt / "work.txt").write_text("work\n")
    git("add", "work.txt", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    s = st(env, ticket_id)
    s["worktree"] = str(wt)
    (env["state"] / "runs" / ticket_id / "state.json").write_text(json.dumps(s))
    return ticket, wt


def ported(**changes):
    return replace(config.load_target("demo"), ports={"start": 4100, "size": 10, "count": 5}, **changes)


def test_remove_keeps_the_branch_releases_the_ports_and_a_new_run_recreates_it(env):
    target = ported(teardown=("echo $PIPELINE_PORT {run_id} > {state}/torn-{run_id}",))
    ticket, wt = finished(env)
    r = cleanup.remove(target, "local-1")
    assert r["message"] == "removed local-1's worktree; branch chore/local-1 is kept" and r["port_released"] == 4100
    assert r["teardown"] == [] and (env["state"] / "torn-local-1").read_text() == "4100 local-1\n"  # ran before removal
    assert "$ echo" in (env["state"] / "runs" / "local-1" / "teardown.log").read_text()
    assert not wt.exists() and "chore/local-1" in git("branch", "--list", cwd=env["repo"])
    s = st(env, "local-1")
    assert s["worktree"] is None and s["prepared"] is False and s["port_base"] is None and s["worktree_removed"]["path"] == str(wt)
    with pytest.raises(cleanup.CleanupError, match="has no worktree"):
        cleanup.remove(target, "local-1")
    worktree.create(target, ticket, wt)  # what the next run's _ready_worktree does
    assert (wt / "work.txt").read_text() == "work\n"


def test_remove_refuses_a_running_or_unfinished_ticket(env):
    target = config.load_target("demo")
    _, wt = finished(env)
    with state.lock(target, "ticket-local-1", "a run"):
        with pytest.raises(cleanup.CleanupError, match="has an active run"):
            cleanup.remove(target, "local-1", force=True)
    finished(env, "local-2", status="needs-user")
    with pytest.raises(cleanup.CleanupError, match="only a done or failed"):
        cleanup.remove(target, "local-2", force=True)
    assert wt.exists() and (env["state"] / "worktrees" / "local-2").exists()


def test_uncommitted_files_need_force(env):
    target = config.load_target("demo")
    _, wt = finished(env, status="failed")
    (wt / "wip.txt").write_text("x\n")
    with pytest.raises(cleanup.CleanupError, match="1 uncommitted file"):
        cleanup.remove(target, "local-1")
    assert wt.exists() and st(env, "local-1")["worktree"] == str(wt)
    assert cleanup.remove(target, "local-1", force=True)["branch"] == "chore/local-1"
    assert not wt.exists() and st(env, "local-1")["worktree_removed"]["forced"] is True


def test_a_failing_teardown_keeps_the_worktree_unless_forced(env):
    target = ported(teardown=("echo stopping; echo boom >&2; exit 3", "touch {state}/second"))
    _, wt = finished(env)
    with pytest.raises(cleanup.CleanupError, match=r"teardown failed \(exit 3\): stopping \| boom; pass --force"):
        cleanup.remove(target, "local-1")
    assert wt.exists() and st(env, "local-1")["port_base"] == 4100 and not (env["state"] / "second").exists()
    r = cleanup.remove(target, "local-1", force=True)
    assert r["teardown"] and "teardown failed" in r["message"] and not wt.exists() and st(env, "local-1")["port_base"] is None


def test_merged_cleanup_runs_teardown_and_releases_ports(env):
    pr = "https://github.com/acme/demo/pull/1"
    _, wt = finished(env, pr=pr)
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    failing = ported(teardown=("exit 1",))
    with pytest.raises(cleanup.CleanupError, match="teardown failed"):
        cleanup.clean(failing, "local-1", pr={"state": "MERGED", "headRefOid": head})
    assert wt.exists()
    r = cleanup.clean(ported(teardown=("true",)), "local-1", pr={"state": "MERGED", "headRefOid": head})
    assert r["branch_deleted"] and not wt.exists()
    s = st(env, "local-1")
    assert s["archived"] and s["port_base"] is None
