"""The lead: signals from each source (a roadmap.md in the memory folder, deferred findings, fake gh for CI runs and
issues), the session (fake claude as stage `lead`) and its proposals, no re-proposing, quota gating and the ajansız
fallback, the once-a-day schedule, personal vs controlled actions (a started proposal moves its roadmap item), expiry,
Bugün's 'dün biten' and the delivery metrics."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import calls, cli, script

from ziggy import attention, config, dashboard, lead, pipeline, quota, results, roadmap, state, ticket
from ziggy.config import ConfigError

DEFERRED = ("## local-1 round 2 (2026-10-10)\n\n"
            "- R2-1 [nit/low/bug] api/report.py:40 — Totals skip the last day of the month. Evidence: off by one. "
            "Why deferred: out of scope\n"
            "- R2-2 [nit/low/style] api/report.py:12 — Rename `tmp` to `rows`. Why deferred: cosmetic\n")
RUNS = [  # newest first, as the API lists them: CI red since 222, Docs green
    {"id": 3, "workflow_id": 11, "name": "CI", "head_branch": "main", "head_sha": "333" * 10, "status": "completed",
     "conclusion": "failure", "display_title": "Add report", "html_url": "https://github.example.test/acme/demo/actions/runs/3"},
    {"id": 2, "workflow_id": 11, "name": "CI", "head_branch": "main", "head_sha": "222" * 10, "status": "completed",
     "conclusion": "failure", "display_title": "Bump deps", "html_url": "https://github.example.test/acme/demo/actions/runs/2"},
    {"id": 1, "workflow_id": 11, "name": "CI", "head_branch": "main", "head_sha": "111" * 10, "status": "completed",
     "conclusion": "success"},
    {"id": 4, "workflow_id": 12, "name": "Docs", "head_branch": "main", "head_sha": "333" * 10, "status": "completed",
     "conclusion": "success"},
    {"id": 5, "workflow_id": 11, "name": "CI", "head_branch": "feat/x", "head_sha": "444" * 10, "status": "completed",
     "conclusion": "failure"},
]


def issue(number, title, assignee=None, labels=(), pr=False):
    return {"number": number, "title": title, "body": f"Body of {number}", "state": "open",
            "html_url": f"https://github.example.test/acme/demo/issues/{number}",
            "assignees": [{"login": assignee}] if assignee else [], "labels": [{"name": n} for n in labels],
            **({"pull_request": {"url": "x"}} if pr else {})}


ISSUES = [issue(7, "Login times out", "tester", ["bug"]), issue(8, "Someone else's", "other"),
          issue(9, "Dark mode", None, ["ziggy"]), issue(10, "A PR", "tester", pr=True)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A HOME of its own: quota labels read the login's .claude.json there, not the real one."""
    h = tmp_path / "home"
    h.mkdir()
    (h / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.test"}}))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


ROADMAP = """---
title: Ziggy hafızası · demo · Yol haritası
created: 2026-10-01
modified: 2026-10-01
type: memory
tags: [memory, ziggy, yol-haritası]
---
# Yol haritası

## P1 · Temel
- Durum: bitti
- [x] R-1 · Project skeleton

## P2 · Planning
- Durum: aktif
- [ ] R-3 · Weekly plan screen
  - Show the week's meals.
- [ ] R-4 · Shopping list
"""


@pytest.fixture
def road(env):
    """The target's roadmap.md: P2 is active, its next items are R-3 and R-4."""
    path = roadmap.memory_path(target())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ROADMAP, encoding="utf-8")
    return path


def item(item_id):
    return roadmap.find(roadmap.load(target()), item_id)[1]


def target(name="demo"):
    return config.load_target(name)


def set_mode(env, mode):
    text = env["config"].read_text()
    head, sep, rest = text.partition('gh_user = "tester"\n')
    env["config"].write_text(head + sep + f'mode = "{mode}"\n' + rest)


def add_to_target(env, line):
    text = env["config"].read_text()
    head, sep, rest = text.partition('gh_user = "tester"\n')
    env["config"].write_text(head + sep + line + "\n" + rest)


def with_deferred(env):
    t = ticket.create(target(), "Monthly report", "feat", "Totals per month.", False, "low")
    inbox = state.inbox_dir(target(), t["id"])
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "deferred.md").write_text(DEFERRED)
    return t["id"]


def github(monkeypatch, runs=RUNS, issues=ISSUES, **extra):
    monkeypatch.setenv("FAKE_GH_LEAD", json.dumps({"runs": runs, "issues": issues, **extra}))


def lead_calls(env):
    return [c for c in calls(env) if c.get("stage") == "lead"]


def gh_calls(env):
    return [json.loads(line) for line in env["gh_log"].read_text().splitlines()] if env["gh_log"].exists() else []


def drafts(env, items, verdict="READY"):
    script(env, {"lead": [{"verdict": verdict, "summary": "today", "drafts": items}]})


def draft(title, sources, kind="feat", size="small"):
    return {"title": title, "type": kind, "body": f"Do {title}.", "why": f"{title} is next.", "sources": sources, "size": size}


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}:00").astimezone()


# --- collection ---------------------------------------------------------------------------------------------------

def test_signals_come_from_each_source_in_order(env, road, monkeypatch, home):
    add_to_target(env, 'lead_issue_label = "ziggy"')
    github(monkeypatch)
    source = with_deferred(env)
    mem = target().state_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "pitfalls.md").write_text("# Tuzaklar\n\n## M-1 · tuzak · Old pitfall\n- Ne oluyor: x\n- Ne yapılır: y\n"
                                     "- Kaynak: demo local-1 · 2020-01-01\n")  # faded: still context, never work
    signals, warnings = lead.collect(target())
    assert warnings == []
    assert [s["key"] for s in signals] == [
        "ci:11:" + "222" * 4, "roadmap:R-3", "roadmap:R-4", "issue:7", "issue:9",
        f"deferred:{source}:R2-1", f"deferred:{source}:R2-2"]
    ci, r3 = signals[0], signals[1]
    assert ci["kind"] == "ci" and ci["url"].endswith("/runs/3") and "son 2 koşusu başarısız" in ci["detail"] and ci["type"] == "fix"
    assert r3 == {"key": "roadmap:R-3", "kind": "roadmap", "title": "Weekly plan screen", "detail": "Show the week's meals.\nFaz: P2",
                  "url": None, "type": "feat", "item": "R-3", "phase": "P2", "name": "Weekly plan screen"}
    assert signals[3]["title"] == "#7 Login times out" and signals[3]["type"] == "fix" and signals[4]["type"] == "feat"
    assert signals[5]["type"] == "fix" and signals[5]["title"] == "Totals skip the last day of the month"
    assert not any(s["kind"] not in ("ci", "roadmap", "issue", "deferred") for s in signals)
    reads = [c["argv"] for c in gh_calls(env) if c["argv"][:1] == ["api"]]
    assert reads == [["api", "repos/acme/demo/actions/runs?branch=main&per_page=5"],
                     ["api", "repos/acme/demo/issues?state=open&per_page=20&assignee=tester"],
                     ["api", "repos/acme/demo/issues?state=open&per_page=20&labels=ziggy"]]
    assert all(c["gh_token"] == "fake-token-for-tester" for c in gh_calls(env) if c["argv"][:1] == ["api"])


def test_without_a_roadmap_or_github_there_are_fewer_signals(env, monkeypatch):
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"\n', ""))
    source = with_deferred(env)
    signals, warnings = lead.collect(target())
    assert [s["kind"] for s in signals] == ["deferred", "deferred"] and warnings == ["gh_user yok: CI ve issue'lar okunmadı"]
    assert not gh_calls(env)
    # A deferred entry already made into a ticket is not a signal.
    ticket.create(target(), "Fix month end", "fix", "x", False, "low", origin={"kind": "deferred", "ticket": source, "entry": "R2-1"})
    assert [s["key"] for s in lead.collect(target())[0]] == [f"deferred:{source}:R2-2"]


def test_github_trouble_is_a_warning_not_a_failure(env, monkeypatch):
    github(monkeypatch, error="HTTP 502")
    signals, warnings = lead.collect(target())
    assert signals == [] and len(warnings) == 2 and warnings[0].startswith("CI okunamadı") and "HTTP 502" in warnings[0]


# --- the session, dedupe, no re-proposing ----------------------------------------------------------------------------

def test_the_session_turns_signals_into_proposals_and_never_reproposes(env, road, monkeypatch, home):
    github(monkeypatch, issues=[ISSUES[0]])
    mem = target().state_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "rules.md").write_text("# Kurallar\n\n## M-4 · kural · Vendor is read-only\n- Kural: Never touch `vendor/`.\n")
    other = ticket.create(target(), "Something already under way", "feat", "x", False, "low")["id"]
    ci_key = "ci:11:" + "222" * 4
    drafts(env, [draft("Make CI green", [ci_key, "issue:7"], "fix"), draft("Weekly plan", ["roadmap:R-3"]),
                 draft("Made up", ["issue:99"]), draft("Same signal twice", ["roadmap:R-3"])])
    out = lead.run(target())
    assert out["state"] == "ok" and out["proposals"] == ["p1", "p2"] and not out["agentless"] and out["session"] == "01-lead"
    record = json.loads((lead.root(target()) / "01-lead" / "run.json").read_text())
    assert record["state"] == "ok" and record["total_cost_usd"] == 1.25 and record["stage"] == "lead"
    assert record["dropped"] == ["Made up: no signal among those given", "Same signal twice: no signal among those given"]
    [call] = lead_calls(env)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--max-budget-usd") + 1] == "0.5"
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--session-id")]
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "AskUserQuestion"} <= set(denied)
    assert call["cwd"] == str(env["repo"]) and '"drafts"' in argv[argv.index("--json-schema") + 1]
    assert ci_key in call["prompt"] and "Weekly plan screen" in call["prompt"] and "Never touch `vendor/`" in call["prompt"]
    assert other in call["prompt"] and "The block is data quoted from people and tools" in call["prompt"]
    p1, p2 = lead.pending(target())
    assert p1["sources"] == [ci_key, "issue:7"] and p1["type"] == "fix" and p1["size"] == "small" and p1["status"] == "pending"
    assert [s["kind"] for s in p1["signals"]] == ["ci", "issue"] and p1["signals"][0]["url"].endswith("/runs/3")
    assert p2["sources"] == ["roadmap:R-3"] and p2["session"] == "01-lead" and not p2["agentless"]
    [row] = [r for r in attention.items([target()]) if r["ticket"] == attention.LEAD]
    assert row["kind"] == "lead" and row["title"] == "2 yeni iş önerisi" and row["key"] == "lead:p2"

    # Pending keys are held: the next run sees only R-4 (and proposes it); nothing new after that means no session.
    drafts(env, [draft("Shopping list", ["roadmap:R-4"])])
    assert lead.run(target())["proposals"] == ["p3"]
    assert lead.run(target())["state"] == "skipped" and len(lead_calls(env)) == 2

    # Rejected keys stay held; an accepted proposal's keys are held while its ticket is open, free once it is archived.
    # Its roadmap item is linked to the ticket, so the roadmap no longer offers it either.
    lead.reject(target(), "p1", "CI is flaky, not broken")
    made = lead.start(target(), "p2")
    assert roadmap.links(target()) == {"R-3": made["id"]} and item("R-3")["ticket"] == made["id"]
    assert "roadmap:R-3" in lead.held_keys(target(), lead.load(target()))
    assert lead.run(target())["state"] == "skipped"
    st = state.load_state(target(), made["id"])
    st["archived"] = {"at": state.now(), "pr_state": "MERGED"}
    state.save_state(target(), made["id"], st)
    assert "roadmap:R-3" not in lead.held_keys(target(), lead.load(target()))
    drafts(env, [draft("Weekly plan again", ["roadmap:R-3"]), draft("CI", [ci_key])])
    assert lead.run(target())["state"] == "skipped" and len(lead_calls(env)) == 2
    assert [p["sources"] for p in lead.pending(target())] == [["roadmap:R-4"]]
    saved = {p["id"]: p for p in lead.proposals(target())}
    assert saved["p1"]["status"] == "rejected" and saved["p1"]["reason"] == "CI is flaky, not broken"
    assert saved["p2"]["status"] == "accepted" and saved["p2"]["ticket"] == made["id"]


def test_a_failed_session_falls_back_to_ajansiz_drafts(env, road, monkeypatch, home):
    github(monkeypatch, issues=[])
    drafts(env, [], verdict="FAILED")
    out = lead.run(target())
    assert out["agentless"] and out["why"].startswith("oturum sonuç vermedi") and len(lead_calls(env)) == 1
    assert [p["title"] for p in lead.pending(target())] == ["CI iş akışını main dalında yeşile döndür", "Weekly plan screen", "Shopping list"]
    assert all(p["agentless"] for p in lead.pending(target()))


# --- quota gating ---------------------------------------------------------------------------------------------------

def test_without_quota_headroom_the_top_signals_become_drafts_without_a_session(env, road, monkeypatch, home):
    github(monkeypatch, issues=ISSUES)
    with_deferred(env)
    soon = int(datetime.now().timestamp()) + 3600
    quota.record(quota.DEFAULT, {"status": "allowed_warning", "rateLimitType": "five_hour", "resetsAt": soon,
                                 "unifiedWindows": {"five_hour": {"utilization": 0.83, "resetsAt": soon}}})
    assert lead.headroom(target()) == (False, "5 saatlik kota %83")
    out = lead.run(target())
    assert out["agentless"] and out["why"] == "5 saatlik kota %83" and out["session"] is None and lead_calls(env) == []
    p1, p2, p3 = lead.pending(target())
    assert len(lead.pending(target())) == lead.FALLBACK_DRAFTS
    assert (p1["type"], p1["sources"], p1["why"]) == ("fix", ["ci:11:" + "222" * 4], lead.WHY["ci"])
    assert "Kaynak: https://github.example.test/acme/demo/actions/runs/3" in p1["body"]
    assert (p2["title"], p2["why"]) == ("Weekly plan screen", "Yol haritasının sıradaki maddesi (P2).")
    body = lead.ticket_body(p1)
    assert "## Neden şimdi" in body and "CI: CI iş akışı main dalında kırmızı (https://" in body and "ajansız" in body

    # A blocked login is no headroom either; a window that has rolled over is room again.
    quota.block(quota.DEFAULT, soon, "five_hour", "rejected")
    assert lead.headroom(target()) == (False, "kota doldu")
    monkeypatch.setattr(quota, "now", lambda: soon + 10)
    assert lead.headroom(target()) == (True, None)


# --- the schedule ---------------------------------------------------------------------------------------------------

def test_the_tick_runs_the_lead_once_per_local_day_from_its_time(env, monkeypatch):
    monkeypatch.delenv("ZIGGY_LEAD")
    started = []
    monkeypatch.setattr(lead, "start_job", lambda t: started.append(t.name) or {"id": "job"})
    clock = {"now": at("2026-10-17", "08:00")}
    monkeypatch.setattr(lead, "now", lambda: clock["now"])
    targets = [target()]
    assert lead.tick(targets) == [] and started == []  # before 08:30
    clock["now"] = at("2026-10-17", "08:31")
    assert lead.tick(targets) == ["demo"] and lead.last_runs()["demo"]["day"] == "2026-10-17"
    clock["now"] = at("2026-10-17", "08:32")
    assert lead.tick(targets) == []  # once a day
    clock["now"] = at("2026-10-18", "07:59")
    assert lead.tick(targets) == []
    clock["now"] = at("2026-10-18", "14:12")  # the machine slept through 08:30: the first tick after the wake runs it
    assert lead.tick(targets) == ["demo"] and started == ["demo", "demo"]

    env["config"].write_text(env["config"].read_text() + '[lead]\nat = "15:00"\n')
    clock["now"] = at("2026-10-19", "14:59")
    assert lead.tick(targets) == []
    clock["now"] = at("2026-10-19", "15:00")
    assert lead.tick(targets) == ["demo"]
    env["config"].write_text(env["config"].read_text().replace('at = "15:00"', 'at = "15:00"\nenabled = false'))
    clock["now"] = at("2026-10-20", "16:00")
    assert lead.tick(targets) == []
    monkeypatch.setenv("ZIGGY_LEAD", "off")
    env["config"].write_text(env["config"].read_text().replace("enabled = false\n", ""))
    assert lead.tick(targets) == []
    assert started == ["demo", "demo", "demo"]


def test_cli_lead_run_respects_the_day_and_now_runs_anyway(env, monkeypatch):
    lead.mark(target(), day=datetime.now().astimezone().date().isoformat(), started_at=state.now(), state="ok")
    out = cli("lead", "run")
    assert out.returncode == 0 and "demo: not now (already ran today" in out.stdout and lead_calls(env) == []
    out = cli("lead", "run", "--now", "--target", "demo")
    assert out.returncode == 0, out.stderr
    assert "demo: 0 signal(s), 0 new, 0 proposal(s); yeni sinyal yok" in out.stdout and "# demo (personal): 0 pending" in out.stdout
    assert lead.last_runs()["demo"]["state"] == "skipped" and lead.last_runs()["demo"]["by"] == "cli"
    assert cli("lead", "start").returncode == 2


def test_lead_config_keys_are_validated(env):
    assert config.load_lead() == config.LeadConfig(enabled=True, at="08:30") and target().lead_model == "sonnet"
    original = env["config"].read_text()
    env["config"].write_text(original + '[lead]\nat = "7:05"\nenabled = false\n')
    assert config.load_lead() == config.LeadConfig(enabled=False, at="7:05") and config.load_lead().clock == (7, 5)
    for bad in ('at = "8.30"', 'at = "24:00"', "enabled = 1", "when = 3"):
        env["config"].write_text(original + f"[lead]\n{bad}\n")
        with pytest.raises(ConfigError):
            config.load_lead()
    env["config"].write_text(original)
    add_to_target(env, 'lead_model = "haiku"\nlead_issue_label = " ziggy "')
    assert target().lead_model == "haiku" and target().lead_issue_label == "ziggy"
    env["config"].write_text(env["config"].read_text().replace('lead_issue_label = " ziggy "', "lead_issue_label = 3"))
    with pytest.raises(ConfigError, match="lead_issue_label"):
        target()


# --- actions: personal vs controlled ---------------------------------------------------------------------------------

def proposals_from_fallback(env, monkeypatch):
    github(monkeypatch, runs=[], issues=[])
    soon = int(datetime.now().timestamp()) + 3600
    quota.block(quota.DEFAULT, soon, "five_hour", "rejected")  # no session: ajansız drafts from R-3 and R-4
    lead.run(target())
    return [p["id"] for p in lead.pending(target())]


@pytest.fixture
def panel(monkeypatch):
    started = []
    monkeypatch.setattr(dashboard, "start_job", lambda t, tid, *args: started.append((t.name, tid, *args)) or {"id": "job", "queued": False})
    server = dashboard.Panel(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, started
    server.shutdown()
    server.server_close()


def test_personal_start_creates_and_runs_the_ticket(env, road, monkeypatch, home, panel):
    from test_dashboard import request
    server, started = panel
    assert proposals_from_fallback(env, monkeypatch) == ["p1", "p2"]
    code, out, _ = request(server, "GET", "/api/lead")
    [view] = out["targets"]
    assert code == 200 and view["mode"] == "personal" and [p["id"] for p in view["pending"]] == ["p1", "p2"]
    assert "## Kaynaklar" in view["pending"][0]["ticket_body"] and out["at"] == "08:30" and out["enabled"]
    code, out, _ = request(server, "POST", "/api/lead/demo/proposals/p1", {"action": "start", "language": "tr"})
    assert code == 200, out
    t = out["ticket"]
    assert t["origin"] == {"kind": "lead", "proposal": "p1"} and t["title"] == "Weekly plan screen" and t["language"] == "tr"
    assert started == [("demo", t["id"], "run", t["id"])] and roadmap.links(target()) == {"R-3": t["id"]}
    md = (state.run_dir(target(), t["id"]) / "ticket.md").read_text()
    assert "- Source: Ziggy's lead proposal p1" in md and "Show the week's meals." in md
    assert request(server, "POST", "/api/lead/demo/proposals/p1", {"action": "start"})[0] == 409  # already accepted
    assert request(server, "POST", "/api/lead/demo/proposals/p9", {"action": "start"})[0] == 404
    assert request(server, "POST", "/api/lead/demo/proposals/p2", {"action": "nope"})[0] == 400
    # Reddet with a reason; the inbox row follows, and reading it marks it seen.
    [row] = [r for r in attention.items([target()]) if r["ticket"] == attention.LEAD]
    assert row["title"] == "1 yeni iş önerisi"
    assert request(server, "POST", "/api/attention/seen", {"target": "demo", "lead": True})[1]["changed"] == 1
    code, out, _ = request(server, "POST", "/api/lead/demo/proposals/p2", {"action": "reject", "reason": "not this month"})
    assert code == 200 and out["proposal"]["status"] == "rejected" and out["proposal"]["reason"] == "not this month"
    assert not [r for r in attention.items([target()]) if r["ticket"] == attention.LEAD]
    assert request(server, "POST", "/api/lead/demo/proposals/p2", {"action": "reject"})[0] == 409


def test_a_started_proposal_moves_its_roadmap_item(env, road, monkeypatch, home, panel):
    """roadmap item → lead proposal → Başlat (personal) → the item is 'ticket' in roadmap.md, then follows the run."""
    from test_dashboard import request
    server, started = panel
    assert item("R-3")["status"] == "todo" and [i["id"] for i in roadmap.next_items(target())] == ["R-3", "R-4"]
    assert proposals_from_fallback(env, monkeypatch) == ["p1", "p2"]
    assert lead.pending(target())[0]["sources"] == ["roadmap:R-3"]
    code, out, _ = request(server, "POST", "/api/lead/demo/proposals/p1", {"action": "start"})
    assert code == 200, out
    tid = out["ticket"]["id"]
    assert started == [("demo", tid, "run", tid)]
    r3 = item("R-3")
    assert (r3["status"], r3["ticket"]) == ("ticket", tid)
    assert f"  - Ticket: {tid} · Durum: ticket" in road.read_text(encoding="utf-8")
    assert roadmap.ticket_ref(target(), tid) == {"item": "R-3", "phase": "P2", "title": "Weekly plan screen"}
    assert [i["id"] for i in roadmap.next_items(target())] == ["R-4"]  # the roadmap offers the next one

    # The run the panel started (here: by hand) moves the item onward.
    quota.record(quota.DEFAULT, {"status": "allowed"}, since=quota.now() + 1)  # the login's window has reset
    script(env, {"plan": [{"verdict": "READY", "risk": "low", "size": "small"}], "verify": [{"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": "plan.txt"}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True, "findings": []}]})
    pipeline.run(target(), tid)
    assert state.load_state(target(), tid)["status"] == "done"
    assert item("R-3")["status"] == "review" and f"  - Ticket: {tid} · Durum: review" in road.read_text(encoding="utf-8")



def test_controlled_only_proposes_and_the_composer_accepts(env, road, monkeypatch, home, panel):
    from test_dashboard import request
    server, started = panel
    set_mode(env, "controlled")
    proposals_from_fallback(env, monkeypatch)
    code, out, _ = request(server, "POST", "/api/lead/demo/proposals/p1", {"action": "start"})
    assert code == 409 and "kontrollü" in out["error"] and started == [] and state.ticket_ids(target()) == []
    with pytest.raises(lead.LeadRefused):
        lead.start(target(), "p1")
    # "Ticket olarak aç": the composer creates it (the user), with the proposal as origin.
    code, out, _ = request(server, "POST", "/api/new", {"target": "demo", "title": "Haftalık plan ekranı", "type": "feat",
                                                         "body": "edited", "origin": {"kind": "lead", "proposal": "p1"}})
    assert code == 200, out
    t = out["ticket"]
    assert t["origin"] == {"kind": "lead", "proposal": "p1"} and t["plan_checkpoint"] is True and started == []
    saved = {p["id"]: p for p in lead.proposals(target())}
    assert saved["p1"]["status"] == "accepted" and saved["p1"]["ticket"] == t["id"]
    assert roadmap.links(target()) == {"R-3": t["id"]} and item("R-3")["status"] == "ticket"
    again = request(server, "POST", "/api/new", {"target": "demo", "title": "x", "origin": {"kind": "lead", "proposal": "p1"}})
    assert again[0] == 409 and "already accepted" in again[1]["error"]
    assert request(server, "POST", "/api/new", {"target": "demo", "title": "x", "origin": {"kind": "lead", "proposal": "p7"}})[0] == 404
    out = cli("lead", "start", "p2")
    assert out.returncode == 2 and "kontrollü" in out.stderr
    out = cli("lead", "start", "p2", "--no-run")  # the user opens it; it runs only when they start it
    assert out.returncode == 0, out.stderr
    made = out.stdout.split("\t")[0]
    assert state.load_state(target(), made)["status"] == "new" and {p["id"]: p for p in lead.proposals(target())}["p2"]["ticket"] == made


def test_refresh_starts_a_job_once(env, monkeypatch, panel):
    from test_dashboard import request
    server, _ = panel
    jobs = []
    monkeypatch.setattr(lead, "start_job", lambda t: jobs.append(t.name) or {"id": "j1"})
    assert request(server, "POST", "/api/lead/demo/run", {})[1] == {"job": {"id": "j1"}}
    monkeypatch.setattr(lead, "job_running", lambda t: True)
    code, out, _ = request(server, "POST", "/api/lead/demo/run", {})
    assert code == 409 and "zaten çalışıyor" in out["error"] and jobs == ["demo"]


# --- expiry ---------------------------------------------------------------------------------------------------------

def test_pending_proposals_expire_after_a_week_and_their_keys_come_back(env, road, monkeypatch, home):
    proposals_from_fallback(env, monkeypatch)
    later = datetime.now().astimezone() + timedelta(days=lead.EXPIRE_DAYS, minutes=1)
    monkeypatch.setattr(lead, "now", lambda: later)
    assert lead.pending(target()) == [] and {p["status"] for p in lead.proposals(target())} == {"expired"}
    assert not [r for r in (lead.load(target())["proposals"]) if r["status"] == "pending"]  # saved
    lead.run(target())
    assert [p["sources"] for p in lead.pending(target())] == [["roadmap:R-3"], ["roadmap:R-4"]]


# --- Bugün ----------------------------------------------------------------------------------------------------------

def test_bugun_lists_what_finished_or_merged_yesterday(env, panel):
    from test_dashboard import request
    server, _ = panel
    now = datetime.now().astimezone()

    def stamp(days_ago, hour=10):
        return (now - timedelta(days=days_ago)).replace(hour=hour, minute=0).astimezone(timezone.utc).isoformat(timespec="seconds")

    def made(title, **st_fields):
        t = ticket.create(target(), title, "feat", "x", False, "low")
        st = state.load_state(target(), t["id"])
        st.update(st_fields)
        state.save_state(target(), t["id"], st)
        return t["id"]

    done = made("Done yesterday", status="done", run={"n": 1, "finished_at": stamp(1), "outcome": "done"})
    merged = made("Merged yesterday", status="done", run={"n": 1, "finished_at": stamp(3), "outcome": "done"},
                  pr="https://github.example.test/acme/demo/pull/4", pr_state="MERGED",
                  archived={"at": stamp(1, 18), "pr_state": "MERGED"})
    made("Done today", status="done", run={"n": 1, "finished_at": stamp(0, 0), "outcome": "done"})
    made("Done before", status="done", run={"n": 1, "finished_at": stamp(2), "outcome": "done"})
    made("Failed yesterday", status="failed", run={"n": 1, "finished_at": stamp(1), "outcome": "failed"})
    _, out, _ = request(server, "GET", "/api/lead")
    assert [(r["id"], r["how"]) for r in out["yesterday"]] == [(done, "done"), (merged, "merged")]
    assert out["yesterday"][1]["pr"].endswith("/pull/4") and out["yesterday"][1]["pr_state"] == "MERGED"


# --- metrics --------------------------------------------------------------------------------------------------------

def test_delivery_metrics_over_the_last_30_days(env, home):
    now = datetime.now().astimezone()

    def ago(days):
        return (now - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")

    def merged_ticket(title, days_ago, cost, reviews=(), opened=None, pr_state="MERGED"):
        t = ticket.create(target(), title, "feat", "x", False, "low")
        st = state.load_state(target(), t["id"])
        st.update(status="done", pr=f"https://github.example.test/acme/demo/pull/{t['id'][-1]}", pr_state=pr_state,
                  user_reviews=list(reviews), **({"pr_opened_at": opened} if opened else {}),
                  **({"archived": {"at": ago(days_ago), "pr_state": pr_state}} if pr_state != "OPEN" else {}))
        state.save_state(target(), t["id"], st)
        d = state.run_dir(target(), t["id"]) / "01-plan"
        d.mkdir()
        state.write_json(d / "run.json", {"stage": "plan", "started_at": ago(days_ago + 1), "total_cost_usd": cost})
        return t["id"]

    merged_ticket("Clean", 2, 1.0)
    merged_ticket("A GitHub round", 5, 3.0, reviews=[{"round": 1, "source": "github", "at": ago(6)}], opened=ago(8))
    merged_ticket("Review before the PR", 10, 2.0, reviews=[{"round": 1, "source": "user", "at": ago(13)}], opened=ago(12))
    merged_ticket("Two rounds after", 1, 6.0, reviews=[{"round": 1, "source": "user", "at": ago(3)},
                                                       {"round": 2, "source": "github", "at": ago(2)}], opened=ago(4))
    merged_ticket("Too old", 40, 9.0)
    merged_ticket("Still open", 1, 9.0, pr_state="OPEN")
    data = dashboard.delivery()
    [row] = data["by_target"]
    assert row == {"name": "demo", "unit": "usd", "merged": 4, "clean": 2, "clean_share": 0.5, "rounds_per_pr": 0.75, "cost_per_pr": 3.0}
    assert data["overall"]["merged"] == 4 and data["overall"]["unit"] == "usd"
    # A login that reports rate limits is a subscription: its cost is quota, not dollars.
    quota.record(quota.DEFAULT, {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}})
    assert dashboard.delivery()["by_target"][0]["unit"] == "kota" and dashboard.stats()["delivery"]["overall"]["unit"] == "kota"
    out = cli("stats")
    assert out.returncode == 0, out.stderr
    assert ("demo\t4 merged · 50% without change requests · 0.75 change rounds per PR · $3.00 of quota "
            "(subscription, API-priced) per PR") in out.stdout


def test_lead_sessions_count_in_the_stats(env, road, monkeypatch, home):
    github(monkeypatch, runs=[], issues=[])
    drafts(env, [draft("Weekly plan", ["roadmap:R-3"])])
    lead.run(target())
    assert {"name": "lead", "cost": 1.25, "sessions": 1, "duration_ms": 1000, "turns": 7} in dashboard.stats()["by_stage"]


def test_results_schema_for_lead_is_engine_only():
    assert "lead" not in results.NAMES
    out, errors = results.normalize("lead", {"verdict": "READY", "drafts": [
        {"title": "  Fix\n login ", "type": " FIX ", "body": " text ", "why": "now", "sources": ["issue:7", 3, "issue:7"], "size": "huge"},
        {"title": "project ticket", "type": "project", "sources": []}, {"type": "feat"},
        *[{"title": f"d{i}", "type": "chore", "sources": ["x"]} for i in range(6)]]})
    assert not errors and len(out["drafts"]) == 5
    assert out["drafts"][0] == {"title": "Fix login", "type": "fix", "body": "text", "why": "now", "sources": ["issue:7"], "size": None}
    assert "memoryUsed" not in results.json_schema("lead")["properties"] and json.dumps(results.json_schema("lead"))
