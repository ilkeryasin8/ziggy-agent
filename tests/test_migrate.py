"""`ziggy migrate` on a fake HOME/XDG tree: an old-named config and state with a real git repository whose worktree
lives under the old state, Claude Code project directories named after it, and a target with its own
CLAUDE_CONFIG_DIR. Processes and launchd are stubbed; nothing here touches the machine's real install."""
import json
import os
import plistlib
import subprocess

import pytest

from conftest import git
from ziggy import migrate


def _tree(entries: dict) -> dict:
    return {str(p): (p.read_bytes() if p.is_file() else None) for p in sorted(entries)}


@pytest.fixture
def old(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_base, state_base = home / ".config", home / ".local" / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_base))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_base))
    monkeypatch.delenv("ZIGGY_CONFIG", raising=False)
    monkeypatch.setattr(migrate, "processes", lambda: [])
    monkeypatch.setattr(migrate, "launchd_pid", lambda label: None)
    launchctl = []
    monkeypatch.setattr(migrate, "launchctl", lambda *a: launchctl.append(a))

    old_state = state_base / "bmad-pipeline"
    repo = tmp_path / "repo"
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    git("-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    wt = old_state / "demo" / "worktrees" / "local-1"
    wt.parent.mkdir(parents=True)
    git("worktree", "add", "-q", "-b", "ticket/local-1", str(wt), cwd=repo)

    run = old_state / "demo" / "runs" / "local-1"
    (run / "01-plan").mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"worktree": str(wt), "status": "done"}))
    (run / "01-plan" / "run.json").write_text(json.dumps({"cwd": str(wt), "other": str(old_state) + "-elsewhere/x"}))
    (old_state / "attention.json").write_text(json.dumps({"items": [{"path": str(old_state / "demo")}]}))
    venv = old_state / "demo" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\ncommand = python3 -m venv {venv}\n")
    (venv / "bin" / "pytest").write_text(f"#!{venv}/bin/python\nimport pytest\n")
    os.chmod(venv / "bin" / "pytest", 0o755)

    claude_alt = home / ".claude-alt"
    config = cfg_base / "bmad-pipeline" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[targets.demo]\npath = "{repo}"\nnote = "{old_state}/demo/venv"\n'
                      f'[targets.demo.env]\nCLAUDE_CONFIG_DIR = "{claude_alt}"\n')

    enc = migrate.claude_encode(wt)
    (home / ".claude" / "projects" / enc).mkdir(parents=True)
    (home / ".claude" / "projects" / enc / "s.jsonl").write_text("{}\n")
    (home / ".claude" / "projects" / "-Users-someone-else").mkdir()
    (claude_alt / "projects" / enc).mkdir(parents=True)
    return {"home": home, "repo": repo, "wt": wt, "old_state": old_state, "new_state": state_base / "ziggy",
            "old_config": cfg_base / "bmad-pipeline", "new_config": cfg_base / "ziggy", "claude_alt": claude_alt,
            "launchctl": launchctl}


def _run(dry_run=False, installed=None):
    lines = []
    code = migrate.run(dry_run, (installed if installed is not None else []).append, out=lines.append)
    return code, lines


def test_claude_encoding_matches_claude_code():
    assert (migrate.claude_encode("/Users/u/.local/state/bmad-pipeline/wardrobe/worktrees/local-3")
            == "-Users-u--local-state-bmad-pipeline-wardrobe-worktrees-local-3")


def test_migrate_moves_rewrites_repairs_and_renames(old):
    code, lines = _run()
    assert code == 0, lines
    new_state, new_wt = old["new_state"], old["new_state"] / "demo" / "worktrees" / "local-1"
    assert not old["old_state"].exists() and not old["old_config"].exists()
    assert (old["new_config"] / "config.toml").is_file()

    st = json.loads((new_state / "demo" / "runs" / "local-1" / "state.json").read_text())
    assert st["worktree"] == str(new_wt)
    run = json.loads((new_state / "demo" / "runs" / "local-1" / "01-plan" / "run.json").read_text())
    assert run["cwd"] == str(new_wt)
    assert run["other"] == str(old["old_state"]) + "-elsewhere/x"  # only the whole path component is rewritten
    assert str(new_state / "demo") in (new_state / "attention.json").read_text()
    config_text = (old["new_config"] / "config.toml").read_text()
    assert f'"{new_state}/demo/venv"' in config_text and "bmad-pipeline" not in config_text
    script = new_state / "demo" / "venv" / "bin" / "pytest"
    assert script.read_text().startswith(f"#!{new_state}/demo/venv/bin/python") and os.access(script, os.X_OK)
    assert str(old["old_state"]) not in (new_state / "demo" / "venv" / "pyvenv.cfg").read_text()

    listed = git("worktree", "list", "--porcelain", cwd=old["repo"])
    assert f"worktree {os.path.realpath(new_wt)}" in listed and "bmad-pipeline" not in listed
    assert git("status", "--porcelain", cwd=new_wt) == ""  # the moved worktree works

    new_enc = migrate.claude_encode(new_wt)
    for projects in (old["home"] / ".claude" / "projects", old["claude_alt"] / "projects"):
        names = {p.name for p in projects.iterdir()}
        assert new_enc in names and migrate.claude_encode(old["wt"]) not in names
    assert (old["home"] / ".claude" / "projects" / new_enc / "s.jsonl").is_file()
    assert (old["home"] / ".claude" / "projects" / "-Users-someone-else").is_dir()

    record = json.loads((new_state / "migrated.json").read_text())
    assert record["runs"][0]["from"]["state"] == str(old["old_state"]) and record["runs"][0]["actions"]
    assert old["launchctl"] == []  # no old panel was installed

    before = (new_state / "migrated.json").read_text()
    code, lines = _run()  # a second run has nothing left to do
    assert code == 0 and lines == ["nothing to migrate"]
    assert (new_state / "migrated.json").read_text() == before


def test_dry_run_prints_the_plan_and_writes_nothing(old):
    tracked = [p for root in (old["home"], old["repo"] / ".git") for p in root.rglob("*")]
    before = _tree(tracked)
    code, lines = _run(dry_run=True)
    assert code == 0
    assert all(line.startswith("would ") for line in lines[:-1]) and "nothing changed" in lines[-1]
    text = "\n".join(lines)
    assert f"would move {old['old_state']} → {old['new_state']}" in text
    assert "worktree repair" in text and str(old["new_state"] / "demo" / "worktrees" / "local-1") in text
    assert f"rename {old['claude_alt'] / 'projects' / migrate.claude_encode(old['wt'])}" in text
    assert _tree([p for root in (old["home"], old["repo"] / ".git") for p in root.rglob("*")]) == before
    assert not old["new_state"].exists() and not old["new_config"].exists()


def test_refuses_while_something_runs(old, monkeypatch):
    lock = old["old_state"] / "demo" / "locks" / "ticket-local-1"
    lock.mkdir(parents=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (lock / "owner").write_text(f"{sleeper.pid} run local-1 now\n")
        code, lines = _run()
        assert code == 1 and any("locks/ticket-local-1" in line for line in lines)
        assert old["old_state"].is_dir() and not old["new_state"].exists()
    finally:
        sleeper.kill()
        sleeper.wait()
    (lock / "owner").write_text("999999 run local-1 then\n")  # a dead owner does not block

    monkeypatch.setattr(migrate, "processes", lambda: [(4242, "/usr/bin/python3 /x/bin/bmad-pipeline ui --port 8787")])
    code, lines = _run()
    assert code == 1 and lines == ["refused: the old panel is serving: pid 4242 /usr/bin/python3 /x/bin/bmad-pipeline ui --port 8787"]
    monkeypatch.setattr(migrate, "launchd_pid", lambda label: 4242)
    old_plist = migrate.old_plist()
    old_plist.parent.mkdir(parents=True)
    with old_plist.open("wb") as f:
        plistlib.dump({"Label": migrate.OLD_LABEL, "ProgramArguments": ["py", "bin/bmad-pipeline", "ui", "--port", "8899"]}, f)
    installed = []
    code, lines = _run(installed=installed)  # the launchd-managed panel is the one migrate stops and reinstalls
    assert code == 0, lines
    assert old["launchctl"] == [("bootout", f"gui/{os.getuid()}/{migrate.OLD_LABEL}")]
    assert installed == [8899] and not old_plist.exists()


def test_refuses_when_the_new_directory_is_taken(old):
    (old["new_state"] / "x").mkdir(parents=True)
    code, lines = _run()
    assert code == 1 and lines == [f"refused: {old['new_state']} exists and is not empty; move or remove it first"]
    assert old["old_state"].is_dir()
