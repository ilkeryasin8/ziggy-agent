"""Worktree lifecycle helpers: gitignored files preserved into a worktree, per-ticket port ranges,
recreating a worktree from a kept branch."""
import json
import os
from dataclasses import replace

import pytest
from conftest import git

from ziggy import config, state, worktree


def make_ticket(env, ticket_id, branch=None, status="new", **extra):
    run = env["state"] / "runs" / ticket_id
    run.mkdir(parents=True, exist_ok=True)
    ticket = {"id": ticket_id, "title": ticket_id, "branch": branch or f"chore/{ticket_id}", "base_branch": "main"}
    (run / "ticket.json").write_text(json.dumps(ticket))
    (run / "state.json").write_text(json.dumps({"status": status, "worktree": None, **extra}))
    return ticket


def add_worktree(env, ticket):
    wt = env["state"] / "worktrees" / ticket["id"]
    git("worktree", "add", "-q", "-b", ticket["branch"], str(wt), "HEAD", cwd=env["repo"])
    return wt


@pytest.fixture
def repo(env, tmp_path):
    """.env* ignored by .gitignore, .claude/skills by .git/info/exclude; a force-added .env.example,
    an unignored notes.txt, a tracked file the main checkout has uncommitted edits to."""
    r = env["repo"]
    (r / ".gitignore").write_text(".venv\n.env*\nlinked*\ntracked.txt\n")
    (r / ".env.example").write_text("EXAMPLE=1\n")
    (r / "tracked.txt").write_text("v1\n")
    git("add", ".gitignore", cwd=r)
    git("add", "-f", ".env.example", "tracked.txt", cwd=r)
    git("commit", "-q", "-m", "ignores", cwd=r)
    (r / ".git" / "info" / "exclude").write_text(".claude/skills/\n")
    (r / ".env").write_text("SECRET=main\n")
    (r / "notes.txt").write_text("not ignored\n")
    (r / "tracked.txt").write_text("v2 uncommitted\n")
    (r / ".claude" / "skills" / "a").mkdir(parents=True)
    (r / ".claude" / "skills" / "a" / "SKILL.md").write_text("skill\n")
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret.txt").write_text("outside\n")
    (outside / "dir" / "inner.txt").write_text("outside dir\n")
    os.symlink(outside / "secret.txt", r / ".env.link")  # ignored, but a symlink out of the repo
    os.symlink(outside / "dir", r / "linked")  # an ignored directory symlink
    return r


def test_preserve_copies_only_missing_gitignored_files(env, repo):
    target = replace(config.load_target("demo"), preserve=(".env*", "notes.txt", "tracked.txt", ".claude/skills/**", "linked/*"))
    wt = add_worktree(env, make_ticket(env, "local-1"))
    assert worktree.preserve(target, wt) == [".claude/skills/a/SKILL.md", ".env"]
    assert (wt / ".env").read_text() == "SECRET=main\n"
    assert (wt / "tracked.txt").read_text() == "v1\n"  # tracked: the branch's version stays
    assert not (wt / "notes.txt").exists() and not os.path.lexists(wt / ".env.link") and not os.path.lexists(wt / "linked")
    assert worktree.preserve(target, wt) == []  # idempotent
    (wt / ".env").write_text("SECRET=worktree\n")
    assert worktree.preserve(target, wt) == [] and (wt / ".env").read_text() == "SECRET=worktree\n"  # never overwrites


def test_preserve_never_writes_through_a_symlink_in_the_worktree(env, repo, tmp_path):
    target = replace(config.load_target("demo"), preserve=(".claude/**",))
    wt = add_worktree(env, make_ticket(env, "local-1"))
    escape = tmp_path / "escape"
    escape.mkdir()
    os.symlink(escape, wt / ".claude")
    assert worktree.preserve(target, wt) == []
    assert list(escape.iterdir()) == []


def test_preserve_without_globs_is_a_no_op(env, repo):
    wt = add_worktree(env, make_ticket(env, "local-1"))
    assert worktree.preserve(config.load_target("demo"), wt) == []


def test_glob_star_stays_in_one_directory():
    rx = worktree._glob_regex("config/*.env")
    assert rx.match("config/a.env") and not rx.match("config/sub/a.env")
    rx = worktree._glob_regex("**/.env")
    assert rx.match(".env") and rx.match("a/b/.env") and not rx.match("a/b/x.env")


def test_ports_are_allocated_per_ticket_kept_and_exhausted(env, capsys):
    target = replace(config.load_target("demo"), ports={"start": 4100, "size": 10, "count": 3})
    for n in range(1, 5):
        make_ticket(env, f"local-{n}")
    sts = {n: state.load_state(target, f"local-{n}") for n in range(1, 5)}
    assert worktree.ports(target, "local-1", sts[1]) == {"PIPELINE_PORT": "4100", "PIPELINE_PORT_END": "4109"}
    assert state.load_state(target, "local-1")["port_base"] == 4100  # persisted
    assert worktree.ports(target, "local-2", sts[2])["PIPELINE_PORT"] == "4110"
    assert worktree.ports(target, "local-1", sts[1])["PIPELINE_PORT"] == "4100"  # kept across runs
    assert worktree.ports(target, "local-3", sts[3])["PIPELINE_PORT"] == "4120"
    assert worktree.ports(target, "local-4", sts[4]) == {}
    assert "no free port range" in capsys.readouterr().err and state.load_state(target, "local-4").get("port_base") is None
    env_, values = worktree.target_env(target, "local-2", env["state"] / "worktrees" / "local-2")
    assert env_["PIPELINE_PORT"] == "4110" and env_["PIPELINE_PORT_END"] == "4119" and values["port"] == "4110"
    # released (cleanup sets port_base to None): the next ticket gets that range
    st1 = state.load_state(target, "local-1")
    st1["port_base"] = None
    state.save_state(target, "local-1", st1)
    assert worktree.ports(target, "local-4", sts[4])["PIPELINE_PORT"] == "4100"


def test_ports_reallocate_a_range_the_config_no_longer_has(env):
    target = replace(config.load_target("demo"), ports={"start": 4100, "size": 10, "count": 3})
    make_ticket(env, "local-1", port_base=5000)
    st = state.load_state(target, "local-1")
    assert worktree.ports(target, "local-1", st)["PIPELINE_PORT"] == "4100" and st["port_base"] == 4100


def test_no_ports_without_config(env):
    target = config.load_target("demo")
    make_ticket(env, "local-1", port_base=4100)
    st = state.load_state(target, "local-1")
    assert worktree.ports(target, "local-1", st) == {}
    assert "PIPELINE_PORT" not in worktree.target_env(target, "local-1", env["state"])[0]


def test_create_reuses_the_branch_only_after_cleanup_removed_the_worktree(env):
    target = config.load_target("demo")
    ticket = make_ticket(env, "local-1")
    wt = add_worktree(env, ticket)
    (wt / "kept.txt").write_text("work\n")
    git("add", "kept.txt", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    git("worktree", "remove", str(wt), cwd=env["repo"])
    with pytest.raises(worktree.GitError, match="already exists"):  # a slug collision stays an error
        worktree.create(target, ticket, wt)
    st = state.load_state(target, "local-1")
    st["worktree_removed"] = {"at": state.now(), "path": str(wt)}
    state.save_state(target, "local-1", st)
    worktree.create(target, ticket, wt)
    assert (wt / "kept.txt").read_text() == "work\n" and worktree.is_worktree(target, wt)
    assert git("branch", "--show-current", cwd=wt).strip() == ticket["branch"]
