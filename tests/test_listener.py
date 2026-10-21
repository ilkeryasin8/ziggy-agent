"""Slack listener: command parsing, filters, dedupe, thread routing, and the full run -> PR path.

Drives Listener in-process with a fake Slack API; runs use the fake claude and fake gh."""
import json

import pytest
from conftest import calls, gh_calls, git, script

from ziggy import listener as listener_mod
from ziggy.config import SlackConfig, load_target

CHANNEL, USER = "C0TEST", "U0DEV"


class FakeSlack:
    def __init__(self):
        self.posts, self.reactions = [], []

    def post(self, channel, thread_ts, text):
        self.posts.append((channel, thread_ts, text))

    def react(self, channel, ts, name):
        self.reactions.append((channel, ts, name))

    def permalink(self, channel, ts):
        return f"https://slack.example.test/archives/{channel}/p{ts.replace('.', '')}"

    def post_top(self, channel, text):
        ts = f"9{len(self.posts):03d}.000"
        self.posts.append((channel, ts, text))
        return ts

    def texts(self, thread_ts):
        return [text for _, ts, text in self.posts if ts == thread_ts]


@pytest.fixture
def slack(env):
    api = FakeSlack()
    fake_results(env, {})
    lst = listener_mod.Listener(load_target(), SlackConfig("true", "true", CHANNEL, USER), api)
    return lst, api


def message(text, ts, event_id=None, **extra):
    event = {"type": "message", "channel": CHANNEL, "user": USER, "text": text, "ts": ts, "client_msg_id": f"m-{ts}"}
    event.update(extra)
    return (event_id or f"E-{ts}", event)


COMMIT = {"verdict": "READY", "summary": "bumped", "_commit": "pins.txt"}  # a publishable branch


def fake_results(env, data):
    script(env, {"implement": [COMMIT], **data})


QUESTION = {"verdict": "NEEDS_USER", "summary": "which file?",
            "questions": [{"id": "q1", "text": "Windows dosyası da mı?", "options": ["evet", "hayır"], "recommendation": "hayır"}]}


def state_of(env, ticket_id):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


@pytest.mark.parametrize(
    "text, expected",
    [
        ("bmad: fix: login timeout'u 30 sn yap", {"type": "fix", "title": "login timeout'u 30 sn yap", "body": "", "plan_checkpoint": False}),
        ("BMAD: README'yi güncelle\nAyrıntı satırı", {"type": "feat", "title": "README'yi güncelle", "body": "Ayrıntı satırı", "plan_checkpoint": False}),
        ("bmad: !plan chore: langchain'i kaldır", {"type": "chore", "title": "langchain'i kaldır", "body": "", "plan_checkpoint": True}),
        ("bmad: fix: a &amp; b", {"type": "fix", "title": "a & b", "body": "", "plan_checkpoint": False}),
        ("merhaba", None),
    ],
)
def test_parse_command(text, expected):
    assert listener_mod.parse_command(text) == expected


@pytest.mark.parametrize("text", ["bmad: perf: hızlandır", "bmad:", "bmad: !plan"])
def test_parse_command_rejects(text):
    with pytest.raises(listener_mod.CommandError):
        listener_mod.parse_command(text)


def test_command_runs_to_a_draft_pr_in_the_thread(env, slack):
    lst, api = slack
    lst.handle_event(*message("bmad: chore: Sync pymongo pins\nWorker and backend differ.", "100.1"))
    assert api.reactions == [(CHANNEL, "100.1", "eyes")]
    assert api.texts("100.1")[0] == "`sl-1` alındı · dal `chore/sync-pymongo-pins` · sıraya girdi"

    lst.drain()
    texts = api.texts("100.1")
    assert [t.split(":", 1)[0] for t in texts[1:4]] == ["plan", "implement", "review"]
    assert texts[1].startswith("plan: *READY* · ")
    assert texts[4].startswith("Draft PR: https://github.example.test/acme/demo/pull/1")

    t = json.loads((env["state"] / "runs" / "sl-1" / "ticket.json").read_text())
    assert t["source"] == "slack" and t["requester"] == USER
    assert t["slack"] == {"channel": CHANNEL, "thread_ts": "100.1"}
    assert t["source_ref"] == "https://slack.example.test/archives/C0TEST/p1001"
    assert "Worker and backend differ." in (env["state"] / "runs" / "sl-1" / "ticket.md").read_text()
    assert git("show-ref", "refs/heads/chore/sync-pymongo-pins", cwd=env["origin"])
    assert "sl-1" not in lst.busy


@pytest.mark.parametrize(
    "extra",
    [
        {"user": "U0SOMEONE"},
        {"channel": "C0OTHER"},
        {"bot_id": "B1", "user": USER},
        {"subtype": "message_changed"},
        {"subtype": "bot_message"},
    ],
    ids=["other-user", "other-channel", "bot", "edit", "bot-subtype"],
)
def test_filtered_messages_do_nothing(env, slack, extra):
    lst, api = slack
    lst.handle_event(*message("bmad: chore: x", "200.1", **extra))
    lst.drain()
    assert api.posts == [] and api.reactions == []
    assert not (env["state"] / "runs").exists() or list((env["state"] / "runs").iterdir()) == []


def test_redelivered_event_starts_one_ticket(env, slack):
    lst, api = slack
    event_id, event = message("bmad: chore: x", "300.1")
    lst.handle_event(event_id, event)
    lst.handle_event(event_id, event)
    lst.handle_event("E-other-envelope", event)  # same client_msg_id under a new envelope
    assert [p for p in api.posts if "alındı" in p[2]] == [(CHANNEL, "300.1", "`sl-1` alındı · dal `chore/x` · sıraya girdi")]


def test_dedupe_survives_restart(env, slack):
    lst, api = slack
    event = message("bmad: chore: x", "310.1")
    lst.handle_event(*event)
    again = listener_mod.Listener(lst.target, lst.cfg, api)
    again.handle_event(*event)
    assert len([p for p in api.posts if "alındı" in p[2]]) == 1


def test_bad_type_answers_in_thread_without_ticket(env, slack):
    lst, api = slack
    lst.handle_event(*message("bmad: perf: hızlandır", "400.1"))
    assert api.texts("400.1") == ["Koşu başlamadı: bilinmeyen tip `perf`; geçerli tipler: feat, fix, refactor, chore, docs, project"]
    assert not (env["state"] / "runs").exists()


def test_plain_message_is_ignored(env, slack):
    lst, api = slack
    lst.handle_event(*message("günaydın", "450.1"))
    assert api.posts == []


def test_questions_go_to_the_thread_and_the_reply_answers(env, slack, monkeypatch):
    lst, api = slack
    fake_results(env, {"plan": [QUESTION, {"verdict": "READY"}]})
    lst.handle_event(*message("bmad: chore: Sync pins", "500.1"))
    lst.drain()
    assert state_of(env, "sl-1")["status"] == "needs-user"
    last = api.texts("500.1")[-1]
    assert "Soruları var" in last and "1. Windows dosyası da mı?" in last and "hayır _(önerilen)_" in last
    assert gh_calls(env) == []

    lst.handle_event(*message("approve", "500.5", thread_ts="500.1"))
    assert api.texts("500.1")[-1] == "`sl-1` durumu *needs-user*; bu cevapla yapılacak bir şey yok."
    lst.handle_event(*message("Windows dosyasını sil.", "500.9", thread_ts="500.1"))
    assert api.texts("500.1")[-1] == "Tamam, sıraya girdi."
    lst.drain()
    assert api.texts("500.1")[-1].startswith("Draft PR: ")
    st = state_of(env, "sl-1")
    assert st["rounds"][-1]["via"] == "slack" and st["rounds"][-1]["by"] == USER
    assert "Windows dosyasını sil." in (env["state"] / "runs" / "sl-1" / "answers.md").read_text()


def test_plan_checkpoint_waits_for_approve(env, slack):
    lst, api = slack
    lst.handle_event(*message("bmad: !plan chore: Riskli iş", "600.1"))
    lst.drain()
    assert api.texts("600.1")[-1].startswith("Plan onayını bekliyor (plan sonrası")
    assert len(calls(env)) == 1

    lst.handle_event(*message("bir şey sor", "600.5", thread_ts="600.1"))
    assert api.texts("600.1")[-1] == "`sl-1` durumu *awaiting-approval*; bu cevapla yapılacak bir şey yok."
    lst.handle_event(*message("approve", "600.6", thread_ts="600.1"))
    lst.drain()
    assert state_of(env, "sl-1")["status"] == "done"
    assert api.texts("600.1")[-1].startswith("Draft PR: ")


def test_reply_while_busy_is_deferred(env, slack, monkeypatch):
    lst, api = slack
    lst.handle_event(*message("bmad: chore: x", "700.1"))
    lst.handle_event(*message("cevap", "700.2", thread_ts="700.1"))
    assert api.texts("700.1")[-1] == "`sl-1` şu an sırada ya da koşuyor; bitince tekrar yaz (yön vermek için `nudge: <mesaj>`)."


def test_a_nudge_reply_steers_even_while_busy(env, slack, monkeypatch):
    lst, api = slack
    lst.handle_event(*message("bmad: chore: x", "750.1"))  # queued: the listener counts it busy
    lst.handle_event(*message("nudge: no run yet", "750.2", thread_ts="750.1"))
    assert api.texts("750.1")[-1].startswith("Yönlendirilemedi: ")  # the engine refuses a ticket with no active run
    seen = []
    monkeypatch.setattr(listener_mod.pipeline, "nudge", lambda target, ticket_id, text, by=None, via="cli":
                        seen.append((ticket_id, text, by, via)) or {"message": "interrupting plan; it resumes with your message",
                                                                     "delivery": "interrupt", "stage": "plan"})
    lst.handle_event(*message("Nudge:  use the v2 API\nand keep the old one", "750.3", thread_ts="750.1"))
    assert seen == [("sl-1", "use the v2 API\nand keep the old one", USER, "slack")]
    assert api.texts("750.1")[-1] == "Tamam: plan durduruluyor; kendi oturumunda mesajınla devam edecek."
    assert lst.jobs.qsize() == 1  # only the run; a nudge never waits in the queue


def test_unknown_thread_reply_is_ignored(env, slack):
    lst, api = slack
    lst.handle_event(*message("approve", "800.2", thread_ts="800.1"))
    assert api.posts == []


def test_failure_is_reported_and_retry_runs_again(env, slack, monkeypatch):
    lst, api = slack
    fake_results(env, {"implement": [{"verdict": "FAILED", "summary": "cannot build"}, COMMIT]})
    lst.handle_event(*message("bmad: chore: x", "900.1"))
    lst.drain()
    assert api.texts("900.1")[-1].startswith("Koşu başarısız (implement")
    assert state_of(env, "sl-1")["status"] == "failed"

    lst.handle_event(*message("retry", "900.5", thread_ts="900.1"))
    lst.drain()
    assert api.texts("900.1")[-1].startswith("Draft PR: ")


def forwarding(env):
    from ziggy import attention, config
    api = FakeSlack()
    lst = listener_mod.Listener(load_target(), SlackConfig("true", "true", CHANNEL, USER, forward=True), api)
    return lst, api, attention, config


def test_forward_posts_other_tickets_questions_in_their_thread_and_replies_answer(env, monkeypatch):
    from conftest import cli
    script(env, {"plan": [QUESTION, {"verdict": "READY"}], "implement": [COMMIT]})
    cli("new", "chore: pin the windows file")
    assert cli("run", "local-1").returncode == 3  # waits for an answer, started from the CLI
    lst, api, attention, config = forwarding(env)
    attention.reconcile([config.load_target("demo")])
    assert lst.forward() == ["local-1"]
    [(channel, ts, text)] = api.posts
    assert channel == CHANNEL and text.startswith("`local-1` pin the windows file · *Ajanın soruları var*")
    assert "Windows dosyası da mı?" in text and "`approve`" in text
    assert lst.forward() == []  # once per inbox row
    lst.handle_event(*message("hayır, yalnız linux", "9100.000", thread_ts=ts))
    lst.drain()
    st = state_of(env, "local-1")
    assert st["status"] == "done" and st["rounds"][0]["answer"] == "hayır, yalnız linux" and st["rounds"][0]["via"] == "slack"
    assert any("Draft PR'ı panelden" in t for t in api.texts(ts))  # a panel ticket is not published from Slack
    assert not gh_calls(env)


def test_forward_is_off_by_default_and_skips_slack_tickets(env, slack):
    lst, api = slack
    assert lst.forward() == []
    lst.handle_event(*message("bmad: !plan chore: bump pins", "1700.000"))
    lst.drain()
    on, api2, attention, config = forwarding(env)
    attention.reconcile([config.load_target("demo")])
    assert on.forward() == [] and api2.posts == []  # its own thread already says it
