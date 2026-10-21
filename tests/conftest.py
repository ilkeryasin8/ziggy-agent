"""Shared fixture: a throwaway target repository with a bare origin, a personal config pointing at
it, and fakes for `claude` (tests/fake_claude.py, scripted per stage through FAKE_RESULTS) and `gh`
(tests/fake_gh.py)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BIN = ROOT / "bin" / "ziggy"
FAKE = ROOT / "tests" / "fake_claude.py"
FAKE_GH = ROOT / "tests" / "fake_gh.py"
RESULTS = "results.json"
CALM_HOST = "1\ntotal = 2048.00M  used = 0.00M  free = 2048.00M  (encrypted)\n"
BUSY_HOST = "4\ntotal = 26624,00M  used = 25576,44M  free = 1047,56M  (encrypted)\n"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=repo)
    (repo / ".gitignore").write_text(".venv\n")
    (repo / "README.md").write_text("target\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)

    config = tmp_path / "config.toml"
    config.write_text(
        'default_target = "demo"\n'
        "[targets.demo]\n"
        f'path = "{repo}"\n'
        'github_repo = "acme/demo"\n'
        'gh_user = "tester"\n'
        'prepare = ["echo prepared > {state}/prepared-{run_id}"]\n'
        "[targets.demo.env]\n"
        'TEST_DB_URI = "mongodb://127.0.0.1:27017/demo_{run_id}"\n'
    )
    log = tmp_path / "claude.log"
    monkeypatch.setenv("ZIGGY_CONFIG", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ZIGGY_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_RESULTS", str(tmp_path / RESULTS))
    monkeypatch.setenv("ZIGGY_GH", str(FAKE_GH))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_GH_ORIGIN", str(origin))
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")  # fake github.com remotes fail at once, offline
    monkeypatch.delenv("ZIGGY_RUN", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setenv("ZIGGY_NOTIFY", "off")  # notify.send never reaches osascript in tests
    monkeypatch.setenv("LANG", "tr_TR.UTF-8")  # notifications and the weekly report in Turkish, whatever the machine's
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("ZIGGY_RETRO", "off")  # no background retro sessions after done (test_retro.py turns it on)
    monkeypatch.setenv("ZIGGY_LEAD", "off")  # the panel's tick never starts a lead job (test_lead.py turns it on)
    # The machine as `sysctl` would describe it (host.py): calm, whatever the real one does; tests write another.
    host = tmp_path / "sysctl.txt"
    host.write_text(CALM_HOST)
    monkeypatch.setenv("ZIGGY_HOST_SAMPLE", str(host))
    return {
        "repo": repo, "origin": origin, "config": config, "log": log, "gh_log": tmp_path / "gh.log",
        "state": tmp_path / "xdg" / "ziggy" / "demo", "results": tmp_path / RESULTS, "host": host,
    }


def cli(*args, stdin=None, cwd=None, env=None):
    return subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, input=stdin, cwd=cwd, env=env)


def calls(env):
    return [json.loads(line) for line in env["log"].read_text().splitlines()] if env["log"].exists() else []


def script(env, stages: dict) -> None:
    """What the fake agent answers, per stage and visit: {"plan": [{...}, {...}], "review": [...]}."""
    env["results"].write_text(json.dumps(stages))



def gh_calls(env):
    return [json.loads(line) for line in env["gh_log"].read_text().splitlines()] if env["gh_log"].exists() else []
