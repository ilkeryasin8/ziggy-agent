"""The weekly report: what it counts in its window, the Markdown, Monday's tick (once per week, with a "report"
notification), `ziggy report` and the panel's /api/report."""
from datetime import datetime, timedelta

from conftest import cli, script
from test_dashboard import panel, request  # noqa: F401 - the panel fixture

from ziggy import config, weekly


def done_ticket(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True}]})
    cli("new", "chore: tidy the cart")
    assert cli("run", "local-1").returncode == 0


def targets():
    return [config.load_target("demo")]


def test_the_report_counts_the_days_before_today(env):
    done_ticket(env)
    today = datetime.now().astimezone().date()
    r = weekly.report(targets(), today=today + timedelta(days=1))
    assert [t["id"] for t in r["done"]] == ["local-1"] and r["merged"] == [] and r["failed"] == []
    assert r["to"] == today.isoformat() and r["from"] == (today - timedelta(days=6)).isoformat()
    assert r["sessions"] >= 3 and r["cost_usd"] > 0 and r["ready_for_review"] == 1
    md = weekly.markdown(r)
    assert md.startswith(f"# Haftalık rapor · {weekly.week_label(today)}")
    assert "- Biten: **1** · birleşen PR: **0** · başarısız: **0**" in md and "- `local-1` tidy the cart (demo)" in md
    assert weekly.report(targets(), today=today)["done"] == []  # today is not in the window yet


def test_monday_morning_writes_last_weeks_report_once_and_notifies(env, monkeypatch):
    sent = []
    monkeypatch.setattr(weekly.notify, "wants", lambda kind: kind == "report")
    monkeypatch.setattr(weekly.notify, "send", lambda title, message, *, key, kind=None: sent.append((title, message, kind)) or True)
    monday = datetime(2026, 10, 19, 8, 30).astimezone()
    assert weekly.tick(targets(), at=monday) is None  # before 09:00
    path = weekly.tick(targets(), at=monday.replace(hour=9, minute=5))
    assert path.name == "2026-W42.md" and path.read_text().startswith("# Haftalık rapor · 2026-W42\n\n2026-10-12 – 2026-10-18")
    assert sent == [("Haftalık rapor", "2026-W42: 0 PR birleşti, 0 iş bitti, $0.00", "report")]
    assert weekly.tick(targets(), at=monday.replace(hour=15)) is None  # once per week
    assert weekly.tick(targets(), at=monday + timedelta(days=2)) is None
    assert weekly.latest()["week"] == "2026-W42" and len(sent) == 1


def test_cli_and_panel_show_the_report(env, panel):  # noqa: F811
    done_ticket(env)
    out = cli("report", "--save")
    assert out.returncode == 0 and out.stdout.startswith("# Haftalık rapor · ") and "saved:" in out.stderr
    code, body, _ = request(panel, "GET", "/api/report")
    assert code == 200 and body["markdown"].startswith("# Haftalık rapor") and body["week"]
    assert (config.state_home() / "reports" / f"{body['week']}.md").exists()


def test_the_panel_shows_this_week_so_far_before_any_report_is_saved(env, panel):  # noqa: F811
    code, body, _ = request(panel, "GET", "/api/report")
    assert code == 200 and body["week"] is None and "Haftalık rapor" in body["markdown"]
    assert not (config.state_home() / "reports").exists()


def test_the_report_renders_in_english_from_its_saved_data(env):
    done_ticket(env)
    r = weekly.report(targets(), today=datetime.now().astimezone().date() + timedelta(days=1))
    md = weekly.markdown(r, "en")
    assert md.startswith("# Weekly report · ") and "- Done: **1** · merged PRs: **0** · failed: **0**" in md
    assert "- Delivery: no PR merged this week" in md and "## Merged\n- none" in md
    path = weekly.save(r)  # the notifications' language: Turkish here (LANG)
    assert path.read_text().startswith("# Haftalık rapor") and path.with_suffix(".json").is_file()
    assert weekly.latest("en")["markdown"].startswith("# Weekly report") and weekly.latest()["markdown"].startswith("# Haftalık")
