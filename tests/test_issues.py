"""GitHub Issues as a work source (issues.py): labelled open issues become gh-<n> tickets once, typed by their
labels; their PRs say `Closes #<n>`; the CLI and the panel's PR sync run it."""
import json

import pytest
from conftest import cli

from ziggy import config, issues, publish, state


def labelled(env, monkeypatch, found, **extra):
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", '[targets.demo]\nissue_label = "ziggy"\n'
                                                               + "".join(f"{k} = {v}\n" for k, v in extra.items())))
    monkeypatch.setenv("FAKE_GH_LEAD", json.dumps({"issues": found}))


def issue(n, title, labels=("ziggy",), body="It breaks.", **extra):
    return {"number": n, "title": title, "body": body, "html_url": f"https://github.com/acme/demo/issues/{n}",
            "user": {"login": "ada"}, "labels": [{"name": x} for x in labels], **extra}


def test_labelled_issues_become_typed_tickets_once(env, monkeypatch):
    labelled(env, monkeypatch, [issue(12, "Crash on empty cart", ("ziggy", "bug")), issue(13, "Dark mode"),
                                issue(14, "A PR, not an issue", pull_request={"url": "x"})])
    target = config.load_target("demo")
    rows = issues.sync(target)
    assert [(r["id"], r["type"], r["created"]) for r in rows] == [("gh-12", "fix", True), ("gh-13", "feat", True)]
    t = state.load_ticket(target, "gh-12")
    assert t["title"] == "Crash on empty cart" and t["type"] == "fix" and t["requester"] == "ada"
    assert t["origin"] == {"name": "github", "ref": "GH-12", "url": "https://github.com/acme/demo/issues/12"}
    assert (state.run_dir(target, "gh-12") / "ticket.md").read_text().count("From GitHub issue #12 by @ada") == 1
    assert issues.sync(target) == []  # once each


def test_dry_run_and_the_cli(env, monkeypatch):
    labelled(env, monkeypatch, [issue(7, "Docs for the API", ("ziggy", "documentation"))])
    out = cli("issues", "sync", "--dry-run")
    assert out.returncode == 0 and out.stdout.strip() == "gh-7\tdocs\t#7 Docs for the API\t(dry run)"
    assert not state.run_dir(config.load_target("demo"), "gh-7").exists()
    out = cli("issues", "sync")
    assert out.stdout.strip() == "gh-7\tdocs\t#7 Docs for the API" and cli("issues", "sync").stdout.startswith("demo: no new issue")


def test_without_a_label_nothing_is_read(env, monkeypatch):
    monkeypatch.setenv("FAKE_GH_LEAD", json.dumps({"error": "must not be called"}))
    assert issues.sync(config.load_target("demo")) == []
    out = cli("issues", "sync")
    assert out.returncode == 2 and "has no issue_label" in out.stderr


@pytest.mark.parametrize("labels, kind", [(("bug",), "fix"), (("type:bug",), "fix"), (("chore",), "chore"),
                                          (("refactor",), "refactor"), (("enhancement",), "feat"), ((), "feat")])
def test_labels_choose_the_type(labels, kind):
    assert issues.type_of(list(labels)) == kind


def test_the_pr_of_an_issue_ticket_closes_it(env, monkeypatch):
    labelled(env, monkeypatch, [issue(12, "Crash on empty cart", ("ziggy", "bug"))])
    target = config.load_target("demo")
    issues.sync(target)
    t = state.load_ticket(target, "gh-12")
    assert issues.closes(target, t) == "Closes #12"
    assert "Closes #12" in publish.pr_body(target, t, state.load_state(target, "gh-12"))
    other = {**t, "origin": {"name": "github", "ref": "GH-3", "url": "https://github.com/someone/else/issues/3"}}
    assert issues.closes(target, other) is None and issues.closes(target, {"origin": None}) is None


def test_issue_run_starts_the_new_ticket(env, monkeypatch):
    from ziggy import dashboard
    labelled(env, monkeypatch, [issue(21, "Speed up search")], issue_run="true")
    target = config.load_target("demo")
    issues.sync(target)
    [job] = dashboard.jobs(target, "gh-21")
    assert job["command"] == "run gh-21"
