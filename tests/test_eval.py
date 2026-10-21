"""`ziggy eval`: tasks with hidden tests run through the real engine (fake claude here), questions answered with their
recommendations, the hidden check under the checks' sandbox, validation of the tasks, results and compare."""
import json
import os

import pytest
from conftest import script

from ziggy import checks
from ziggy import eval as evaluation

needs_sandbox = pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")


def task(root, tid, check, *, solution=None, hidden_test=None):
    d = root / tid
    (d / "repo" / "tests").mkdir(parents=True)
    (d / "repo" / "tests" / "__init__.py").write_text("")
    (d / "repo" / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    (d / "repo" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (d / "repo" / "AGENTS.md").write_text("Python; tests: python3 -m unittest discover -s tests -q\n")
    (d / "hidden").mkdir()
    (d / "hidden" / "__init__.py").write_text("")
    if hidden_test:
        (d / "hidden" / "test_add.py").write_text(hidden_test)
    if solution:
        (d / "solution").mkdir()
        (d / "solution" / "calc.py").write_text(solution)
    (d / "task.toml").write_text(f'title = "Make {tid} work"\ntype = "fix"\nbody = """It is broken."""\ncheck = "{check}"\n')


@pytest.fixture
def tasks_dir(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    root.mkdir()
    monkeypatch.setattr(evaluation, "TASKS", root)
    for key, value in (("GIT_AUTHOR_NAME", "Eval"), ("GIT_AUTHOR_EMAIL", "eval@example.test"),
                       ("GIT_COMMITTER_NAME", "Eval"), ("GIT_COMMITTER_EMAIL", "eval@example.test")):
        monkeypatch.setenv(key, value)  # the throwaway seed repositories of a CI machine with no git identity
    return root


@needs_sandbox
def test_a_run_drives_each_task_to_done_and_judges_it_by_its_hidden_check(env, tasks_dir):
    task(tasks_dir, "01-good", "test -f fixed.txt")
    task(tasks_dir, "02-bad", "test -f other.txt")
    script(env, {"reproduce": [{"verdict": "NEEDS_USER", "questions": [{"id": "Q1", "text": "Round it?", "options": ["yes", "no"],
                                                                         "recommendation": "yes"}]}, {"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": ["fixed.txt", "proof.txt"],  # the fix, and its regression test
                                "regressionTest": {"path": "proof.txt", "run": "test -f fixed.txt && test -f proof.txt"}}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True}]})
    lines = []
    root = evaluation.run(label="smoke", echo=lines.append)
    meta = evaluation.load(str(root))
    good, bad = meta["results"]
    assert good["id"] == "01-good" and good["status"] == "done" and good["passed"] is True, (good.get("error"), good.get("hidden_detail"))
    assert good["interventions"] >= 1 and good["implement_visits"] >= 1 and good["sessions"] >= 3
    assert bad["status"] == "done" and bad["passed"] is False
    assert lines[0].startswith("01-good: pass · done") and lines[1].startswith("02-bad: FAIL · done")
    assert meta["label"] == "smoke" and root.name.endswith("-smoke") and evaluation.load("latest")["name"] == root.name
    rounds = json.loads((root / "state" / "ziggy" / "eval-01-good" / "runs" / good["ticket"] / "state.json").read_text())["rounds"]
    assert rounds[0]["answer"] == "Q1: yes" and rounds[0]["by"] == "eval"
    summary = evaluation.summary(meta)
    assert summary.startswith(f"{root.name}: 1/2 passed") and "02-bad" in summary


def test_validate_names_a_task_whose_hidden_tests_do_not_fail_on_the_seed(tasks_dir):
    hidden = "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    task(tasks_dir, "01-ok", "python3 -m unittest discover -s hidden_tests -t . -q", hidden_test=hidden,
         solution="def add(a, b):\n    return a + b\n")
    task(tasks_dir, "02-broken", "python3 -m unittest discover -s hidden_tests -t . -q",
         hidden_test="import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n", solution="x = 1\n")
    rows = {r["id"]: r for r in evaluation.validate()}
    assert rows["01-ok"]["ok"] is True
    assert rows["02-broken"]["ok"] is False and rows["02-broken"]["seed_hidden_fails"] is False
    assert [t["id"] for t in evaluation.tasks(["02"])] == ["02-broken"]
    with pytest.raises(evaluation.EvalError, match="no task matches"):
        evaluation.tasks(["99"])


def test_compare_puts_two_runs_side_by_side():
    a = {"name": "a", "results": [{"id": "01", "passed": True, "cost_usd": 1.0, "seconds": 60},
                                  {"id": "02", "passed": False, "cost_usd": 2.0, "seconds": 120}]}
    b = {"name": "b", "results": [{"id": "01", "passed": True, "cost_usd": 0.5, "seconds": 60},
                                  {"id": "02", "passed": True, "cost_usd": 1.5, "seconds": 60}]}
    out = evaluation.compare(a, b)
    assert "passed  1/2  →  2/2" in out and "cost    $3.00  →  $2.00" in out
    assert [line for line in out.splitlines() if line.strip().startswith("02")][0].endswith("*")
    assert evaluation.totals(b["results"])["pass_rate"] == 1.0


def test_the_shipped_tasks_are_valid():
    shipped = evaluation.tasks()
    assert len(shipped) >= 15
    broken = [r for r in evaluation.validate() if not r["ok"]]
    assert broken == []
