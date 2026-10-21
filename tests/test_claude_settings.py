"""Contract tests for the headless-run Claude Code settings (ziggy/claude-settings.json)."""
import json
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[1] / "ziggy" / "claude-settings.json"

# The full deny list from plan section 6.1; every entry is required, not a subset.
REQUIRED_DENY = [
    "Bash(git push:*)",
    "Bash(git push)",
    "Bash(git remote:*)",
    "Bash(git fetch:*)",
    "Bash(git pull:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git worktree:*)",
    "Bash(git config:*)",
    "Bash(gh:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(docker:*)",
    "Bash(npm publish:*)",
    "Bash(pip install:*)",
    "Bash(npm install:*)",
    "Bash(npm ci:*)",
    "Bash(sudo:*)",
    "Bash(launchctl:*)",
    "Bash(osascript:*)",
    "Read(./.env*)",
    "Read(**/.env)",
    "Read(**/.env.*)",
    "Read(**/.npmrc)",
    "Read(**/.pypirc)",
    "Read(~/.config/**)",
    "Read(~/.ssh/**)",
    # Credentials of other tools on the machine: cloud, containers, signing, git, Codex, the keychain and browsers.
    "Read(~/.aws/**)",
    "Read(~/.azure/**)",
    "Read(~/.kube/**)",
    "Read(~/.docker/**)",
    "Read(~/.gnupg/**)",
    "Read(~/.netrc)",
    "Read(~/.git-credentials)",
    "Read(~/.codex/**)",
    "Read(~/Library/Keychains/**)",
    "Read(~/Library/Cookies/**)",
    "Read(~/Library/Application Support/Google/Chrome/**)",
    "Read(~/Library/Application Support/Firefox/**)",
    "Read(~/Library/Safari/**)",
    "Read(~/.claude.json)",
    "Edit(./.claude/settings.json)",
    "Edit(./AGENTS.md)",
    "WebFetch",
    "WebSearch",
]

# The owner's notes and home folders, and Claude Code's per-project data (auto memory, transcripts) of the default
# login and of alternate logins (CLAUDE_CONFIG_DIR=~/.claude-<name>): agents get project knowledge only through
# Ziggy's memory layer.
PRIVATE_DENY = [
    "Read(~/Documents/**)",
    "Read(~/Desktop/**)",
    "Read(~/Downloads/**)",
    "Read(~/Library/Mobile Documents/**)",
    "Read(~/.claude/projects/**)",
    "Read(~/.claude-*/projects/**)",
]

EXPECTED_ALLOW = [
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git show:*)",
    "Bash(git rev-parse:*)",
    "Bash(uv run:*)",
    "Bash(.venv/bin/python:*)",
    "Bash(python -m pytest:*)",
    "Bash(npm test:*)",
    "Bash(npm run lint:*)",
    "Bash(npm run build:*)",
]

EXPECTED_SANDBOX = {
    "enabled": True,
    "allowUnsandboxedCommands": False,
    "failIfUnavailable": True,
    "network": {
        "allowedDomains": [],
        "allowLocalBinding": True,
    },
}


@pytest.fixture(scope="module")
def settings() -> dict:
    return json.loads(SETTINGS.read_text())


def test_sandbox_block_is_exact(settings):
    # Exact equality rules out sandbox-side widening (filesystem.allowWrite, excludedCommands,
    # allowed domains); permission-side widening is covered by the two permissions tests below.
    assert settings["sandbox"] == EXPECTED_SANDBOX


@pytest.mark.parametrize("rule", REQUIRED_DENY)
def test_required_deny_rule_present(settings, rule):
    assert rule in settings["permissions"]["deny"]


def test_permissions_have_only_allow_and_deny(settings):
    # additionalDirectories or a defaultMode would widen what the run can touch.
    assert set(settings["permissions"]) == {"allow", "deny"}


def test_allow_list_is_exact(settings):
    assert settings["permissions"]["allow"] == EXPECTED_ALLOW


def test_agents_cannot_read_the_owners_notes_or_claude_data(settings):
    deny = settings["permissions"]["deny"]
    assert [rule for rule in PRIVATE_DENY if rule not in deny] == []


def test_allow_list_grants_no_push_or_gh(settings):
    for rule in settings["permissions"]["allow"]:
        assert "push" not in rule
        assert not rule.startswith("Bash(gh")


def test_no_hooks_of_ours_and_none_of_the_target(settings):
    # A target's project hooks (e.g. a SessionStart installer) would run on every headless stage
    # and can run commands the sandbox does not see; flag settings outrank the project's.
    assert "hooks" not in settings
    assert settings["disableAllHooks"] is True


def test_commits_and_prs_carry_no_attribution(settings):
    # Empty strings switch off Claude Code's Co-Authored-By trailer and PR footer
    # (verified headless: without this the agent's commit gets the trailer).
    assert settings["attribution"] == {"commit": "", "pr": ""}


def test_an_old_claude_code_is_refused(monkeypatch, tmp_path):
    from ziggy import dispatch

    old = tmp_path / "claude"
    old.write_text("#!/bin/sh\necho '2.1.40 (Claude Code)'\n")
    old.chmod(0o755)
    monkeypatch.setenv("ZIGGY_CLAUDE", str(old))
    monkeypatch.setattr(dispatch, "_VERSION", {})
    with pytest.raises(dispatch.ClaudeTooOld, match="2.1.40 is older than 2.1.90"):
        dispatch.command("sonnet", "s", "hi", tmp_path)
    old.write_text("#!/bin/sh\necho '2.1.305 (Claude Code)'\n")
    monkeypatch.setattr(dispatch, "_VERSION", {})
    assert dispatch.command("sonnet", "s", "hi", tmp_path)[0] == str(old)
    old.write_text("#!/bin/sh\necho 'a stand-in'\n")  # no version: let through
    monkeypatch.setattr(dispatch, "_VERSION", {})
    assert dispatch.claude_version() is None and dispatch.command("sonnet", "s", "hi", tmp_path)
