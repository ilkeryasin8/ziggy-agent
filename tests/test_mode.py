"""A target's mode: "personal" (the default) runs as before; "controlled" (company repositories) defaults code
tickets to BMad when it is set up, stops every ticket at the plan gate (also one made before the switch), never starts
a dependant on its own (the inbox asks: kind `start`, and `run` starts it), and forces pr_replies off with a load
warning. Onboarding proposes controlled for a company repository and writes the mode into the table."""
import time

import pytest
from conftest import cli, script
from test_bmad import install
from test_onboard import make_repo
from test_stack import st, target, wait_until

from ziggy import attention, bmad, config, dashboard, onboard, stack, state, ticket
from ziggy.config import ConfigError

WARNING = '[targets.demo] pr_replies = true is ignored: mode = "controlled" never answers on GitHub'


@pytest.fixture(autouse=True)
def _fresh_bmad_cache():
    bmad._CACHE.clear()
    yield
    bmad._CACHE.clear()


def set_mode(env, mode, extra=""):
    text = env["config"].read_text()
    head, sep, rest = text.partition('gh_user = "tester"\n')
    env["config"].write_text(head + sep + f'mode = "{mode}"\n{extra}' + rest)


def settled(ticket_id, status):
    return lambda: st(ticket_id)["status"] == status and not state.running(target(), ticket_id) and not dashboard._busy(target(), ticket_id)


# --- config --------------------------------------------------------------------------------------------

def test_mode_defaults_to_personal_and_a_bad_value_is_refused(env):
    t = target()
    assert t.mode == "personal" and t.warnings == ()
    set_mode(env, "lead")
    with pytest.raises(ConfigError, match='mode must be "controlled" or "personal"'):
        target()


def test_controlled_forces_pr_replies_off_with_a_warning(env):
    set_mode(env, "personal", "pr_replies = true\n")
    assert target().pr_replies is True and target().warnings == ()
    env["config"].write_text(env["config"].read_text().replace('mode = "personal"', 'mode = "controlled"'))
    t = target()
    assert t.mode == "controlled" and t.pr_replies is False and t.warnings == (WARNING,)
    proc = cli("new", "chore: tidy")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count(f"uyarı: {WARNING}") == 1
    snap = dashboard.snapshot(time.time())
    entry = next(x for x in snap["targets"] if x["name"] == "demo")
    assert entry["mode"] == "controlled" and entry["warnings"] == [WARNING]
    assert next(r for r in snap["tickets"] if r["id"] == "local-1")["mode"] == "controlled"


# --- BMad by default -----------------------------------------------------------------------------------

def test_controlled_new_defaults_to_bmad_only_when_it_is_ready(env):
    set_mode(env, "controlled")
    assert cli("new", "fix: login loops").returncode == 0
    assert state.load_ticket(target(), "local-1")["pipeline"] == "fix"  # no BMad here: standard
    install(env)
    bmad._CACHE.clear()
    assert cli("new", "fix: login loops again").returncode == 0
    assert state.load_ticket(target(), "local-2")["pipeline"] == "bmad"
    assert cli("new", "fix: one more", "--pipeline", "standard").returncode == 0
    assert state.load_ticket(target(), "local-3")["pipeline"] == "fix"
    assert cli("new", "project: product doc").returncode == 0  # a project ticket keeps its own pipeline
    assert state.load_ticket(target(), "local-4")["pipeline"] == "project"
    entry = next(x for x in dashboard.snapshot(time.time())["targets"] if x["name"] == "demo")
    assert entry["default_pipeline"] == "bmad"
    (env["repo"] / "_bmad" / "bmm" / "config.yaml").unlink()  # a per-developer file missing: not ready
    assert ticket.default_choice(target()) == "standard"


def test_personal_never_defaults_to_bmad(env):
    install(env)
    assert ticket.default_choice(target()) == "standard"
    assert cli("new", "fix: login loops").returncode == 0
    assert state.load_ticket(target(), "local-1")["pipeline"] == "fix"


# --- the plan gate -------------------------------------------------------------------------------------

def test_controlled_tickets_always_stop_at_the_plan_gate(env):
    script(env, {"plan": [{"verdict": "READY", "risk": "low", "size": "small"}]})
    assert cli("new", "chore: before the switch").returncode == 0
    assert state.load_ticket(target(), "local-1")["plan_checkpoint"] is False
    set_mode(env, "controlled")
    made = ticket.create(target(), "from anywhere", "chore", "", False, "low")
    assert made["plan_checkpoint"] is True
    assert "- Plan checkpoint: yes" in (env["state"] / "runs" / made["id"] / "ticket.md").read_text()
    proc = cli("run", "local-1")  # made before the switch, without the flag: gated all the same
    assert proc.returncode == 3, proc.stderr
    s = st("local-1")
    assert s["status"] == "awaiting-approval" and s["gate"]["stage"] == "plan"
    assert s["gate"]["reason"] == "the ticket asks for a plan checkpoint"


def test_personal_runs_through_without_a_gate_and_wakes_dependants(env):
    script(env, {"plan": [{"verdict": "READY", "risk": "low", "size": "small"}],
                 "implement": [{"verdict": "READY", "_commit": "{ticket}.txt"}]})
    cli("new", "chore: first")
    cli("new", "chore: second", "--after", "local-1")
    assert state.load_ticket(target(), "local-2")["plan_checkpoint"] is False
    assert cli("run", "local-2").returncode == 0 and st("local-2")["status"] == "waiting"
    s = st("local-1")
    s["status"] = "done"
    state.save_state(target(), "local-1", s)
    assert attention.needs(target(), "local-2") is None
    assert stack.wake(target()) == ["local-2"]
    wait_until(settled("local-2", "done"), what="local-2 to finish on its own")


# --- dependants ask --------------------------------------------------------------------------------------

def test_controlled_dependant_asks_before_it_starts_and_run_starts_it(env):
    set_mode(env, "controlled")
    script(env, {"plan": [{"verdict": "READY", "risk": "low", "size": "small"}]})
    cli("new", "chore: first")
    proc = cli("new", "chore: second", "--after", "local-1", "--run")
    assert proc.returncode == 0, proc.stderr
    assert "the inbox asks to start it then" in proc.stderr
    assert attention.needs(target(), "local-2") is None  # its dependency is not done yet
    assert "ziggy run local-2 starts it" in cli("status", "local-2").stdout

    s = st("local-1")
    s["status"] = "done"
    state.save_state(target(), "local-1", s)
    assert stack.wake(target()) == []
    assert st("local-2")["status"] == "waiting" and not (env["state"] / "runs" / "local-2" / "jobs").exists()
    item = attention.needs(target(), "local-2")
    assert item["kind"] == "start" and item["title"] == "local-1 bitti; local-2 başlasın mı?"
    assert item["key"] == "start:local-1" and item["severity"] == "medium" and item["dismissable"] is False
    assert attention._group({"needs_you": item, "status": "waiting"}) == "waiting_on_you"
    rows = [r for r in attention.items([target()]) if r["ticket"] == "local-2"]
    assert [(r["kind"], r["title"]) for r in rows] == [("start", "local-1 bitti; local-2 başlasın mı?")]  # wake raised it
    assert stack.wake(target()) == []  # asked once: the ledger row stays as it is
    assert [r["key"] for r in attention.items([target()]) if r["ticket"] == "local-2"] == ["start:local-1"]
    assert "ziggy run local-2" in cli("status", "local-2").stdout

    out = dashboard.do_action(target(), "local-2", "run")  # the board card's Başlat
    assert out["job"]
    wait_until(settled("local-2", "awaiting-approval"), what="local-2 to plan and stop at the gate")
    assert attention.needs(target(), "local-2")["kind"] == "approval"


def test_the_start_title_names_every_finished_dependency():
    t = {"id": "local-4", "after": ["local-3", "local-5"]}
    assert attention.start_title(t) == "local-3, local-5 bitti; local-4 başlasın mı?"
    assert attention.start_key(t) == "start:local-3,local-5"
    assert attention.start_title({"id": "local-4", "after": []}) == "local-4 başlasın mı?"
    assert "start" in config.NOTIFY_EVENTS and attention.LABEL["start"] == "Başlatılmayı bekliyor"


# --- onboarding ----------------------------------------------------------------------------------------

def test_onboarding_proposes_and_writes_the_mode(env, tmp_path):
    work = make_repo(tmp_path / "Work" / "api", {}, remote="git@github.com:acme/api.git")
    solo = make_repo(tmp_path / "solo", {}, remote="git@github.com:me/solo.git")
    env["config"].write_text(env["config"].read_text() + f'\n[onboard.gh_users]\n"{tmp_path / "Work"}" = "me-at-work"\n')
    found = onboard.detect(work)
    assert found["company"] and found["spec"]["mode"] == "controlled"
    assert onboard.detect(solo)["spec"]["mode"] == "personal"

    onboard.add(found["spec"])
    assert 'mode = "controlled"' in env["config"].read_text()
    assert config.load_target("api").mode == "controlled"
    with pytest.raises(ConfigError, match='mode must be "controlled" or "personal"'):
        onboard.add({**onboard.detect(solo)["spec"], "mode": "lead"})
    assert "solo" not in config.target_names()

    proc = cli("target", "add", str(solo), "--yes")
    assert proc.returncode == 0, proc.stderr
    assert 'mode = "personal"' in proc.stdout and config.load_target("solo").mode == "personal"


def test_target_add_mode_overrides_the_proposal(env, tmp_path):
    work = make_repo(tmp_path / "Work" / "web", {}, remote="git@github.com:acme/web.git")
    env["config"].write_text(env["config"].read_text() + f'\n[onboard.gh_users]\n"{tmp_path / "Work"}" = "me-at-work"\n')
    proc = cli("target", "add", str(work), "--yes", "--mode", "personal")
    assert proc.returncode == 0, proc.stderr
    assert config.load_target("web").mode == "personal"
