"""Spending caps (budget.py): a ticket over ticket_budget_usd stops between stages and `run --now` goes on; a target
over daily_budget_usd parks its tickets (status budget) until the panel's tick starts them the next day."""
from datetime import timedelta

import pytest
from conftest import cli, script
from test_engine import _review, _stages, _state

from ziggy import budget, config, dashboard

SESSION = 1.25  # what each fake session reports


def caps(env, **values):
    extra = "".join(f"{k} = {v}\n" for k, v in values.items())
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]\n", f"[targets.demo]\n{extra}"))


def three_stages(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})


def test_a_ticket_over_its_budget_stops_before_the_next_stage_and_now_goes_on(env):
    caps(env, ticket_budget_usd=2.0)
    three_stages(env)
    cli("new", "chore: tidy")
    out = cli("run", "local-1")
    assert out.returncode == 1 and _stages(env) == ["plan", "implement"]  # 2.50 spent: review does not start
    st = _state(env)
    assert st["status"] == "failed" and "spent $2.50 of its $2.00 budget (ticket_budget_usd)" in st["error"]
    assert "ziggy run local-1 --now" in st["error"]
    out = cli("run", "local-1", "--now")
    assert out.returncode == 0 and "going on (--now)" in out.stderr and _state(env)["status"] == "done"
    assert budget.ticket_spent(config.load_target("demo"), "local-1") == 3 * SESSION


def test_a_target_over_its_daily_budget_parks_tickets_until_tomorrow(env, monkeypatch):
    caps(env, daily_budget_usd=1.0)
    three_stages(env)
    cli("new", "chore: tidy")
    out = cli("run", "local-1")
    assert out.returncode == 0 and _stages(env) == ["plan"] and "starts tomorrow" in out.stderr
    st = _state(env)
    t = config.load_target("demo")
    assert st["status"] == budget.STATUS and st["next"]["stage"] == "implement"
    assert st["budget"]["kind"] == "day" and st["budget"]["spent"] == SESSION and st["budget"]["cap"] == 1.0
    assert not budget.due(st) and budget.wake(t) == []
    row = dashboard.ticket_row(t, "local-1")
    assert row["budget"]["cap"] == 1.0 and row["status"] == "budget"
    tomorrow = budget.now() + timedelta(days=1)
    monkeypatch.setattr(budget, "now", lambda: tomorrow.replace(hour=8))
    assert budget.due(_state(env)) and budget.wake(t) == ["local-1"]
    [job] = dashboard.jobs(t, "local-1")
    assert job["command"] == "run local-1"


def test_no_caps_no_holds(env):
    three_stages(env)
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0 and _state(env)["status"] == "done"
    assert budget.over(config.load_target("demo"), "local-1") is None


@pytest.mark.parametrize("value", ["0", "-3", '"ten"', "true"])
def test_caps_must_be_positive_dollars(env, value):
    caps(env, daily_budget_usd=value)
    with pytest.raises(config.ConfigError, match="daily_budget_usd must be a positive number of dollars"):
        config.load_target("demo")
