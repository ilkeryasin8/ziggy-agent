"""Contract tests for the pipeline pre-push git hook (githooks/pre-push)."""
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1] / "githooks"


def git_env(env_value: str | None) -> dict[str, str]:
    # Isolate from the developer's global/system git config (signing, hooks, templates) and from
    # inherited GIT_* variables (GIT_DIR, GIT_INDEX_FILE, ...) set when pytest runs inside a git hook.
    env = {k: v for k, v in os.environ.items() if k != "ZIGGY_RUN" and not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if env_value is not None:
        env["ZIGGY_RUN"] = env_value
    return env


def git(*args: str, cwd: Path, env_value: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=git_env(env_value), timeout=60
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    assert git("init", "--bare", "-q", str(remote), cwd=tmp_path).returncode == 0
    assert git("init", "-q", str(work), cwd=tmp_path).returncode == 0
    for key, value in [
        ("user.name", "Pipeline Test"),
        ("user.email", "pipeline-test@example.test"),
        ("core.hooksPath", str(HOOKS_DIR)),
    ]:
        assert git("config", key, value, cwd=work).returncode == 0
    assert git("remote", "add", "origin", str(remote), cwd=work).returncode == 0
    (work / "file.txt").write_text("content\n")
    assert git("add", "file.txt", cwd=work).returncode == 0
    assert git("commit", "-q", "-m", "initial", cwd=work).returncode == 0
    return work, remote


def remote_refs(remote: Path) -> str:
    result = git("ls-remote", str(remote), cwd=remote)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("env_value", ["1", "true"])
def test_push_rejected_while_pipeline_run_is_set(repo, env_value):
    work, remote = repo
    before = remote_refs(remote)
    result = git("push", "origin", "HEAD:refs/heads/x", cwd=work, env_value=env_value)
    assert result.returncode != 0
    assert "ZIGGY_RUN" in result.stderr
    assert remote_refs(remote) == before


@pytest.mark.parametrize("env_value", [None, ""], ids=["unset", "empty"])
def test_push_proceeds_when_pipeline_run_is_unset(repo, env_value):
    work, remote = repo
    result = git("push", "origin", "HEAD:refs/heads/x", cwd=work, env_value=env_value)
    assert result.returncode == 0, result.stderr
    assert "refs/heads/x" in remote_refs(remote)
