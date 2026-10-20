"""A project's state on the base branch (docs/PROJECT.md and open tickets) and the router's proposal.
`claude` is the fake (router answers from FAKE_ROUTE)."""
import json

import pytest
from conftest import calls, cli, git

from ziggy import config, project


def target():
    return config.load_target("demo")


def push_doc(env, text="# demo\n\n## Product\n\nA planner.\n"):
    path = env["repo"] / "docs" / "PROJECT.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    git("add", "-A", cwd=env["repo"])
    git("commit", "-q", "-m", "docs: project doc", cwd=env["repo"])
    git("push", "-q", "origin", "main", cwd=env["repo"])


def test_status_reads_the_project_doc_and_open_tickets_from_the_base(env):
    s = project.status(target(), fresh=True)
    assert s["base"] == "origin/main" and s["project_doc"]["exists"] is False and s["tickets"] == {}
    push_doc(env)
    ticket_id = cli("new", "fix: Login").stdout.split("\t")[0]
    s = project.status(target(), fresh=True)
    doc = s["project_doc"]
    assert doc["exists"] and doc["lines"] == 5 and doc["subject"] == "docs: project doc" and len(doc["sha"]) == 40
    assert "A planner." in doc["text"] and doc["date"]
    assert [t["id"] for t in s["tickets"]["fix"]] == [ticket_id]


def test_route_asks_a_tool_less_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_ROUTE", json.dumps({"type": "project", "title": "  Gardırop  planlayıcı ", "reason": "PROJECT.md yok."}))
    answer = project.route(target(), "Gardırop planlayan bir uygulama yapalım")
    assert answer["type"] == "project" and answer["title"] == "Gardırop planlayıcı" and answer["reason"] == "PROJECT.md yok."
    [call] = [c for c in calls(env) if c.get("router")]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--model") + 1] == "sonnet"
    assert "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1])["properties"]["type"]["enum"] == ["feat", "fix", "chore", "project"]
    prompt = argv[argv.index("-p") + 1]
    assert "- docs/PROJECT.md: missing" in prompt and "Gardırop planlayan" in prompt


def test_route_refuses_an_unusable_answer(env, monkeypatch):
    monkeypatch.setenv("FAKE_ROUTE", json.dumps({"type": "deploy", "title": "x", "reason": ""}))
    with pytest.raises(project.RouteError, match="no usable answer"):
        project.route(target(), "ship it")
    with pytest.raises(project.RouteError):
        project.route(target(), "  ")
