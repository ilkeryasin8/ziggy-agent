"""guard.py: what a push would take off the machine (files that run in CI or steer agents, credentials, invisible
characters), the approval that lets it through, and the cleaning of PR text."""
import json

import pytest
from conftest import git
from test_dashboard import panel, request  # noqa: F401 - the panel fixture
from test_publish import created, done_ticket, state_of, target

from ziggy import guard, publish, state

GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def kinds(found):
    return sorted({(f["kind"], f["where"].split(":")[0]) for f in found})


def test_findings_name_ci_files_agent_rules_install_scripts_secrets_and_invisible_text(env):
    ticket_id = done_ticket(env, files=(".github/workflows/ci.yml", "pkg/AGENTS.md", "app.py", "package.json"))
    wt = state.worktree_dir(target(), ticket_id)
    (wt / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "postinstall": "node x.js"}}))
    (wt / "app.py").write_text(f"TOKEN = '{GH_TOKEN}'\nname = 'pay​pal'\n")
    git("commit", "-q", "-am", "more", cwd=wt)
    found = guard.findings(wt, "origin/main", "HEAD", "Fix it", "See ![x](https://evil.example/?d=1)")
    assert kinds(found) == [("secret", "app.py"), ("sensitive", ".github/workflows/ci.yml"), ("sensitive", "package.json"),
                            ("sensitive", "pkg/AGENTS.md"), ("unicode", "app.py")]
    secret = next(f for f in found if f["kind"] == "secret")
    assert secret["where"] == "app.py:1" and GH_TOKEN not in secret["detail"] and "GitHub token" in secret["detail"]
    assert "postinstall" in next(f["detail"] for f in found if f["where"] == "package.json")
    assert guard.findings(wt, "origin/main", "HEAD", "Leaks " + GH_TOKEN, "") [-1]["where"] == "PR title"


def test_clean_text_drops_invisible_characters_and_disarms_images():
    text = "ok‮evil ![shot](https://x.example/a.png?d=s) <img src=https://x.example/b.png> <video src=x>"
    out = guard.clean_text(text)
    assert "‮" not in out and "![" not in out and "[image: shot](https://x.example/a.png?d=s)" in out
    assert "<img" not in out and "&lt;img" in out and "&lt;video" in out


def test_publish_stops_on_findings_until_they_are_approved(env, panel):  # noqa: F811
    ticket_id = done_ticket(env, files=(".github/workflows/deploy.yml",))
    with pytest.raises(publish.PublishError, match=r"(?s)publish needs approval:.*deploy.yml"):
        publish.publish(target(), ticket_id)
    assert not created(env)
    code, out, _ = request(panel, "POST", "/api/action", {"target": "demo", "id": ticket_id, "action": "publish"})
    assert code == 409 and out["error"].startswith(guard.MARKER)
    url = publish.publish(target(), ticket_id, approve_findings=True, by="dev")
    assert url and created(env)
    st = state_of(env, ticket_id)
    assert len(st["guard_approved"]) == 1 and st["guard_log"][0]["by"] == "dev"
    assert publish.pending_findings(target(), ticket_id) == []  # approved once: a later push asks nothing new
    wt = state.worktree_dir(target(), ticket_id)
    (wt / "CLAUDE.md").write_text("new rules\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "rules", cwd=wt)
    assert [f["where"] for f in publish.pending_findings(target(), ticket_id)] == ["CLAUDE.md"]  # something new asks again


def test_a_plain_branch_publishes_without_a_question(env):
    ticket_id = done_ticket(env, files=("src/util.py",))
    assert publish.pending_findings(target(), ticket_id) == []
    assert publish.publish(target(), ticket_id)
