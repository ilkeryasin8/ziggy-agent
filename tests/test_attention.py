"""attention: the needs-you item and its key per kind, the ledger's reconcile (baseline, new, resolved,
reappear-as-fresh, the engine's own lock), dismissal of the current item only, read state, the board."""
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ziggy import attention, config, state


def make(target, ticket_id, title=None, phases=(), **st):
    root = state.run_dir(target, ticket_id)
    root.mkdir(parents=True, exist_ok=True)
    state.write_json(root / "ticket.json", {"id": ticket_id, "title": title or f"feat: {ticket_id}", "branch": f"feat/{ticket_id}",
                                            "base_branch": "main", "type": "feat", "created_at": state.now()})
    for name in phases:
        (root / name).mkdir(exist_ok=True)
    st.setdefault("status", "queued")
    state.save_state(target, ticket_id, st)
    return root


def lock(target, ticket_id, pid):
    path = target.state_dir / "locks" / f"ticket-{ticket_id}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "owner").write_text(f"{pid} run {ticket_id} {state.now()}\n")
    return path


def job(target, ticket_id, name, pid, exit_code=None, command="run x"):
    root = state.run_dir(target, ticket_id) / "jobs" / name
    root.mkdir(parents=True)
    state.write_json(root / "job.json", {"command": command, "argv": [], "pid": pid, "started_at": state.now()})
    (root / "out.log").write_text("boom\n")
    if exit_code is not None:
        (root / "exit").write_text(f"{exit_code}\n")
    return root


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


QUESTION = {"rounds": [{"n": 1, "stage": "plan", "questions": [{"text": "a"}], "answer": "x"},
                       {"n": 2, "stage": "review", "questions": [{"text": "q"}, {"text": "r"}], "answer": None}]}


@pytest.fixture
def tk(env, monkeypatch):
    sent = []
    monkeypatch.setattr(attention.notify, "wants", lambda kind: True)
    monkeypatch.setattr(attention.notify, "send", lambda title, message, *, key, kind=None: sent.append((title, message, key)) or True)
    monkeypatch.setattr(attention.dispatch, "quiet_for", lambda stage_dir, threshold, pid=None: None)
    return config.load_target("demo"), sent


def ledger():
    return json.loads((config.state_home() / attention.LEDGER).read_text())


def rows(**match):
    return [r for r in ledger()["rows"] if all(r.get(k) == v for k, v in match.items())]


# --- keys --------------------------------------------------------------------------------------

def test_every_kind_has_a_key_naming_its_occurrence(tk, env, monkeypatch):
    target, _ = tk
    make(target, "dmo-1", status="needs-user", run={"n": 1}, **QUESTION)
    make(target, "dmo-2", status="awaiting-approval", run={"n": 3}, gate={"stage": "plan", "reason": "risky"})
    make(target, "dmo-3", status="done", run={"n": 4})
    make(target, "dmo-4", status="failed", run={"n": 2}, phases=("01-plan", "02-implement"))
    make(target, "dmo-5", status="running", run={"n": 1}, phases=("01-plan",))
    lock(target, "dmo-5", dead_pid())
    make(target, "dmo-6", status="queued")
    job(target, "dmo-6", "20260101-000000-000000-run", dead_pid(), exit_code=2)
    make(target, "dmo-7", status="done", pr="https://github.com/acme/demo/pull/7", pr_state="MERGED", cleanup_blocked=["a.py dirty"])
    make(target, "dmo-8", status="done", pr="https://github.com/acme/demo/pull/8", published_head="0" * 40, worktree=str(env["repo"]))
    make(target, "dmo-9", status="running", run={"n": 1}, phases=("01-implement",))
    lock(target, "dmo-9", os.getpid())
    seen_threshold = []
    monkeypatch.setattr(attention.dispatch, "quiet_for", lambda d, threshold, pid=None: seen_threshold.append(threshold) or 610.0)

    got = {tid: attention.needs(target, tid) for tid in (f"dmo-{n}" for n in range(1, 10))}
    assert {tid: (n["kind"], n["key"], n["dismissable"]) for tid, n in got.items()} == {
        "dmo-1": ("question", "question:2", False),
        "dmo-2": ("approval", "approval:3:plan", False),
        "dmo-3": ("publish", "publish:4", True),
        "dmo-4": ("failed", "failed:2:02-implement", True),
        "dmo-5": ("lost", "lost:01-plan", True),
        "dmo-6": ("job-error", "job-error:20260101-000000-000000-run", True),
        "dmo-7": ("merged-kept", "merged-kept:MERGED", True),
        "dmo-8": ("push", "push:" + "0" * 40, True),
        "dmo-9": ("quiet", "quiet:01-implement", False),
    }
    assert got["dmo-1"]["title"] == "review adımının 2 sorusu var" and got["dmo-1"]["round"]["n"] == 2
    assert got["dmo-9"]["minutes"] == 10 and seen_threshold == [target.quiet_after]
    assert all(n["unread"] and n["created_at"] is None for n in got.values())  # no ledger row yet


def test_a_running_ticket_needs_nothing_until_it_goes_quiet(tk):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1}, phases=("01-plan",))
    job(target, "dmo-1", "20260101-000000-000000-resume", os.getpid())  # a live job counts as running
    assert attention.needs(target, "dmo-1") is None


def test_unreadable_state_is_an_error_item(tk):
    target, sent = tk
    make(target, "dmo-1")
    (state.run_dir(target, "dmo-1") / "state.json").write_text("{")
    attention.reconcile([target])
    assert [(r["kind"], r["key"], r["severity"]) for r in rows(ticket="dmo-1")] == [("error", "error", "high")]


# --- reconcile ---------------------------------------------------------------------------------

def test_first_reconcile_records_without_a_burst_then_new_items_notify(tk):
    target, sent = tk
    make(target, "dmo-1", status="failed", run={"n": 1}, phases=("01-plan",))
    attention.reconcile([target])
    assert sent == []
    assert ledger()["baselined"] == ["demo"]
    assert [(r["key"], r["seen_at"], r["notified_at"]) for r in rows()] == [("failed:1:01-plan", None, None)]
    assert attention.unread([target]) == 1  # recorded quietly, still unread

    make(target, "dmo-2", title="feat: Pin it", status="needs-user", run={"n": 1}, **QUESTION)
    attention.reconcile([target])
    assert sent == [("dmo-2 · demo", "Ajanın soruları var: feat: Pin it", "demo/dmo-2/question:2")]
    assert rows(ticket="dmo-2")[0]["notified_at"]
    attention.reconcile([target])
    assert len(sent) == 1  # still the same occurrence


def test_reconcile_resolves_what_is_gone_and_a_returning_key_is_a_fresh_occurrence(tk):
    target, sent = tk
    make(target, "dmo-1", status="failed", run={"n": 1}, phases=("01-plan",))
    attention.reconcile([target])
    assert attention.seen(target, "dmo-1") == 1

    make(target, "dmo-1", status="done", run={"n": 1}, phases=("01-plan",))
    attention.reconcile([target])
    failed, = rows(key="failed:1:01-plan")
    assert failed["resolved_at"] and failed["seen_at"]
    assert [r["key"] for r in attention.items([target])] == ["publish:1"]
    assert {r["key"] for r in attention.items([target], all=True)} == {"publish:1", "failed:1:01-plan"}
    assert [s[2] for s in sent] == ["demo/dmo-1/publish:1"]

    make(target, "dmo-1", status="failed", run={"n": 1}, phases=("01-plan",))  # the same run fails again
    attention.reconcile([target])
    again, = rows(key="failed:1:01-plan")  # replaces the resolved row: one row per occurrence
    assert again["resolved_at"] is None and again["seen_at"] is None
    assert rows(key="publish:1")[0]["resolved_at"]
    assert [s[2] for s in sent] == ["demo/dmo-1/publish:1", "demo/dmo-1/failed:1:01-plan"]


def test_a_quiet_item_refreshes_its_title_in_place(tk, monkeypatch):
    target, sent = tk
    make(target, "dmo-1", status="running", run={"n": 1}, phases=("01-implement",))
    lock(target, "dmo-1", os.getpid())
    attention.reconcile([target])  # baseline: nothing yet
    monkeypatch.setattr(attention.dispatch, "quiet_for", lambda d, threshold, pid=None: 700.0)
    attention.reconcile([target])
    first, = rows(key="quiet:01-implement")
    monkeypatch.setattr(attention.dispatch, "quiet_for", lambda d, threshold, pid=None: 1300.0)
    attention.reconcile([target])
    row, = rows(key="quiet:01-implement")
    assert row["title"].startswith("21 ") and row["created_at"] == first["created_at"]
    assert len(sent) == 1


def test_deleted_tickets_resolve_and_old_resolved_rows_are_pruned(tk):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1})
    make(target, "dmo-2", status="failed", run={"n": 1})
    attention.reconcile([target])
    data = ledger()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    data["rows"] += [{**data["rows"][0], "key": "failed:0:-", "resolved_at": old},
                     {**data["rows"][0], "key": "publish:0", "resolved_at": recent}]
    state.write_json(config.state_home() / attention.LEDGER, data)
    import shutil
    shutil.rmtree(state.run_dir(target, "dmo-2"))
    attention.reconcile([target])
    assert {(r["ticket"], r["key"], r["resolved_at"] is None) for r in ledger()["rows"]} == {
        ("dmo-1", "failed:1:-", True), ("dmo-2", "failed:1:-", False), ("dmo-1", "publish:0", False)}


def test_raise_for_sees_through_the_engines_own_lock_and_job(tk):
    target, sent = tk
    make(target, "dmo-1", status="needs-user", run={"n": 1}, phases=("01-plan",), **QUESTION)
    held = lock(target, "dmo-1", os.getpid())  # this test process is the engine run
    running_job = job(target, "dmo-1", "20260101-000000-000000-run", os.getpid())
    attention.reconcile([target])  # the panel: a live run with a settled status is letting go; left alone
    assert rows() == [] and ledger()["baselined"] == ["demo"]
    assert attention.needs(target, "dmo-1") is None  # the panel shows it running

    attention.raise_for(target, "dmo-1")
    assert [r["key"] for r in rows(resolved_at=None)] == ["question:2"]
    assert [s[2] for s in sent] == ["demo/dmo-1/question:2"]
    attention.reconcile([target])  # still letting go: the row stays
    assert rows(key="question:2")[0]["resolved_at"] is None

    import shutil
    shutil.rmtree(held)
    (running_job / "exit").write_text("3\n")
    attention.reconcile([target])
    assert rows(key="question:2")[0]["resolved_at"] is None and len(sent) == 1


def test_raise_for_on_a_running_ticket_resolves_its_quiet_item(tk, monkeypatch):
    target, sent = tk
    make(target, "dmo-1", status="running", run={"n": 1}, phases=("01-implement",))
    lock(target, "dmo-1", os.getpid())
    monkeypatch.setattr(attention.dispatch, "quiet_for", lambda d, threshold, pid=None: 700.0)
    attention.reconcile([target])
    assert rows(key="quiet:01-implement")[0]["resolved_at"] is None
    attention.raise_for(target, "dmo-1")  # the engine retries the quiet stage
    assert rows(key="quiet:01-implement")[0]["resolved_at"]


def test_raise_for_on_a_new_target_records_the_rest_silently(tk):
    target, sent = tk
    make(target, "dmo-1", status="failed", run={"n": 1})
    make(target, "dmo-2", status="failed", run={"n": 1})
    attention.raise_for(target, "dmo-2")
    assert {r["ticket"] for r in rows()} == {"dmo-1", "dmo-2"}
    assert [s[2] for s in sent] == ["demo/dmo-2/failed:1:-"]
    assert ledger()["baselined"] == ["demo"]


def test_raise_for_never_raises(tk, monkeypatch, capsys):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1})

    def broken(*a, **kw):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(attention, "_commit", broken)
    attention.raise_for(target, "dmo-1")
    attention.reconcile([target])
    assert "ledger on fire" in capsys.readouterr().err


def test_a_failing_notification_leaves_the_row_unnotified(tk, monkeypatch):
    target, _ = tk
    attention.reconcile([target])
    monkeypatch.setattr(attention.notify, "send", lambda *a, **kw: False)
    make(target, "dmo-1", status="failed", run={"n": 1})
    attention.reconcile([target])
    assert rows(ticket="dmo-1")[0]["notified_at"] is None


def test_unwanted_kinds_are_recorded_but_not_notified(tk, monkeypatch):
    target, sent = tk
    attention.reconcile([target])
    monkeypatch.setattr(attention.notify, "wants", lambda kind: kind == "question")
    make(target, "dmo-1", status="failed", run={"n": 1})
    attention.reconcile([target])
    assert sent == [] and len(rows()) == 1


def test_concurrent_writers_lose_no_rows(tk):
    target, sent = tk
    attention.reconcile([target])
    ids = [f"dmo-{n}" for n in range(1, 9)]
    for tid in ids:
        make(target, tid, status="failed", run={"n": 1})
    threads = [threading.Thread(target=attention.raise_for, args=(target, tid)) for tid in ids]
    threads += [threading.Thread(target=attention.seen, args=(target,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["ticket"] for r in rows(resolved_at=None)) == sorted(ids)
    assert len(sent) == 8 and all(r["notified_at"] for r in rows())


# --- dismissal ---------------------------------------------------------------------------------

def test_dismiss_closes_only_the_current_item_and_what_shows_next_is_not_notified(tk):
    target, sent = tk
    make(target, "dmo-1", status="failed", run={"n": 1}, phases=("01-plan",))
    lock(target, "dmo-1", dead_pid())
    attention.reconcile([target])
    assert attention.needs(target, "dmo-1")["key"] == "lost:01-plan"
    with pytest.raises(attention.Refused, match="değişti"):
        attention.dismiss(target, "dmo-1", "failed:1:01-plan")  # not the one shown now
    out = attention.dismiss(target, "dmo-1", "lost:01-plan")
    assert out["dismissed"] == "lost:01-plan" and out["needs_you"]["key"] == "failed:1:01-plan"
    assert json.loads((state.run_dir(target, "dmo-1") / attention.DISMISSED).read_text()) == ["lost:01-plan"]
    lost, = rows(key="lost:01-plan")
    assert lost["dismissed_at"] and lost["seen_at"] and lost["resolved_at"]
    assert [r["key"] for r in attention.items([target])] == ["failed:1:01-plan"]
    assert sent == []
    attention.reconcile([target])
    assert attention.needs(target, "dmo-1")["key"] == "failed:1:01-plan"  # closed for good
    attention.dismiss(target, "dmo-1", "failed:1:01-plan")
    assert attention.needs(target, "dmo-1") is None and attention.unread([target]) == 0


def test_questions_and_approvals_need_an_action_not_a_dismissal(tk):
    target, _ = tk
    make(target, "dmo-1", status="needs-user", run={"n": 1}, **QUESTION)
    make(target, "dmo-2", status="queued")
    with pytest.raises(attention.Refused, match="kapatılamaz"):
        attention.dismiss(target, "dmo-1", "question:2")
    with pytest.raises(attention.Refused, match="kapatılacak bir şey yok"):
        attention.dismiss(target, "dmo-2", "publish:1")
    assert not (state.run_dir(target, "dmo-1") / attention.DISMISSED).exists()


# --- read state --------------------------------------------------------------------------------

def test_unread_counts_and_seen_on_open(tk):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1})
    make(target, "dmo-2", status="needs-user", run={"n": 1}, **QUESTION)
    make(target, "dmo-3", status="done", run={"n": 1})
    assert attention.unread([target]) == 0  # nothing reconciled yet
    attention.reconcile([target])
    assert attention.unread([target]) == 3 and attention.unread([]) == 0
    first = attention.needs(target, "dmo-1")
    assert first["unread"] and first["created_at"] and first["seen_at"] is None

    assert attention.seen(target, "dmo-1") == 1  # opening the ticket
    assert attention.seen(target, "dmo-1") == 0
    assert attention.unread([target]) == 2
    opened = attention.needs(target, "dmo-1")
    assert not opened["unread"] and opened["seen_at"]
    assert attention.seen(target, "dmo-2", keys=["question:1"]) == 0
    assert attention.seen(target, "dmo-2", keys=["question:2"]) == 1
    with pytest.raises(ValueError):
        attention.seen(target, keys=["publish:1"])
    assert attention.seen(target) == 1  # mark all read
    assert attention.unread([target]) == 0


def test_seen_rows_stay_seen_while_the_item_lasts(tk):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1})
    attention.reconcile([target])
    attention.seen(target, "dmo-1")
    make(target, "dmo-1", status="failed", run={"n": 1}, error="still")
    attention.reconcile([target])
    assert attention.unread([target]) == 0


# --- board -------------------------------------------------------------------------------------

def _row(tid, need=None, **extra):
    return {"target": "demo", "id": tid, "running": False, "run_started_at": None, "pr": None, "pr_state": None,
            "archived": None, "updated_at": extra.pop("updated_at", "2026-01-01T00:00:00+00:00"), "needs_you": need, **extra}


def _need(kind, created, seen=None):
    return {**attention.need(kind, kind, key=f"{kind}:1"), "created_at": created, "seen_at": seen}


def test_board_from_groups_and_sorts_tickets():
    rows_ = [
        _row("a-1", _need("failed", "2026-01-01")),
        _row("a-2", _need("question", "2026-01-02")),
        _row("a-3", _need("approval", "2026-01-01", seen="2026-01-01")),
        _row("a-4", running=True, run_started_at="2026-01-03"),
        _row("a-5", _need("quiet", "2026-01-04"), running=True, run_started_at="2026-01-01"),
        _row("a-6", _need("publish", "2026-01-05")),
        _row("a-7", _need("publish", "2026-01-06")),
        _row("a-8", pr="u", pr_state="OPEN", updated_at="2026-01-01"),
        _row("a-9", _need("push", "2026-01-01"), pr="u", pr_state="OPEN", updated_at="2026-01-09"),
        _row("a-10", pr="u", pr_state="MERGED"),
        _row("a-11", pr="u", archived=True),
        _row("a-12"),
        _row("a-13", _need("merged-kept", "2026-01-02"), pr="u", pr_state="MERGED", updated_at="2026-01-02"),
        _row("a-14", _need("lost", "2026-01-03")),
        _row("a-15", status="waiting", updated_at="2026-01-02"),
        _row("a-16", status="split", split_open=True, updated_at="2026-01-03"),
        _row("a-17", status="split", split_open=False),  # every slice merged and archived: off the board
        _row("a-18", status="split", split_landing=True, updated_at="2026-01-04"),  # slices done, PRs open
        _row("a-18", _need("blocked", "2026-01-04"), status="waiting"),
        _row("a-19", _need("restack", "2026-01-05"), status="done", pr="u", pr_state="OPEN"),
    ]
    board = attention.board_from(rows_)
    assert {g: [e["id"] for e in entries] for g, entries in board.items()} == {
        "waiting_on_you": ["a-3", "a-2", "a-14", "a-1", "a-18", "a-19"],  # high first (oldest first), then medium
        "working": ["a-5", "a-4"],
        "ready_for_review": ["a-7", "a-6"],
        "waiting_on_others": ["a-16", "a-15"],  # waiting for other tickets; a split ticket with open slices
        "landing": ["a-9", "a-18", "a-13", "a-8"],  # newest update first
    }
    first = board["waiting_on_you"][0]
    assert first == {"target": "demo", "id": "a-3", "kind": "approval", "severity": "high", "key": "approval:1",
                     "unread": False, "since": "2026-01-01"}
    assert board["waiting_on_you"][1]["unread"] and not board["working"][1]["unread"]


def test_board_reads_the_targets(tk):
    target, _ = tk
    make(target, "dmo-1", status="failed", run={"n": 1})
    make(target, "dmo-2", status="done", run={"n": 1}, pr="https://github.com/acme/demo/pull/2", pr_state="OPEN")
    make(target, "dmo-3", status="done", run={"n": 1}, pr="https://github.com/acme/demo/pull/3", pr_state="MERGED", archived=True)
    attention.reconcile([target])
    board = attention.board([target])
    assert [e["id"] for e in board["waiting_on_you"]] == ["dmo-1"] and board["waiting_on_you"][0]["since"]
    assert [e["id"] for e in board["landing"]] == ["dmo-2"]
    assert board["working"] == [] and board["ready_for_review"] == []
