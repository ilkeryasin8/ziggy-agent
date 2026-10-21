"""Triage: each changed file's risk on the review page (findings, CI/agent files, dependencies, size; tests and
generated files discounted) and the board putting the riskier of equally urgent tickets first."""
from test_attention import _need, _row
from test_changes import ID, commit, review_run, tk  # noqa: F401 - the tk fixture

from ziggy import attention, changes


def risk_of(s, path):
    return next((f["risk"], f["risk_why"]) for f in s["files"] if f["path"] == path)


def test_file_risk_weighs_findings_sensitive_files_dependencies_and_size():
    plain = {"path": "src/app.py", "additions": 10, "deletions": 0}
    assert changes.file_risk(plain, []) == (0, [])
    findings = [{"path": "src/app.py", "latest": True, "severity": "high", "disposition": "fix"},
                {"path": "src/app.py", "latest": True, "severity": "low", "disposition": "fix", "fixed": True},
                {"path": "src/app.py", "latest": False, "severity": "high", "disposition": "fix"},
                {"path": "src/app.py", "latest": True, "severity": "medium", "disposition": "dismiss"}]
    assert changes.file_risk(plain, findings) == (5, ["1 açık bulgu"])
    assert changes.file_risk({"path": ".github/workflows/ci.yml", "additions": 3}, []) == (6, ["CI ya da ajan dosyası"])
    assert changes.file_risk({"path": "web/package.json", "additions": 2}, []) == (4, ["bağımlılık"])
    assert changes.file_risk({"path": "src/big.py", "additions": 180, "deletions": 40}, []) == (4, ["220 satır"])
    test_file = {"path": "tests/test_app.py", "additions": 400}
    assert changes.file_risk(test_file, []) == (1, ["400 satır", "test"])
    lock = {"path": "uv.lock", "additions": 900, "generated": True}
    assert changes.file_risk(lock, []) == (2, ["bağımlılık", "900 satır", "üretilmiş"])


def test_the_review_page_carries_each_files_risk(tk):  # noqa: F811
    commit(tk["wt"], {"AGENTS.md": "rules\n"}, "docs: agent rules")
    review_run(tk, "03-review", tk["base"], [{"id": "R1-1", "severity": "critical", "disposition": "fix", "location": "a.py:2"}])
    s = changes.summary(tk["target"], ID)
    assert risk_of(s, "a.py") == (8, ["1 açık bulgu"])
    assert risk_of(s, "AGENTS.md") == (6, ["CI ya da ajan dosyası"])
    assert risk_of(s, "lib/b.py") == (0, [])


def test_the_board_puts_the_riskier_of_equally_urgent_tickets_first():
    rows = [_row("a-1", _need("question", "2026-01-01")),
            _row("a-2", _need("question", "2026-01-02"), risk="high"),
            _row("a-3", _need("question", "2026-01-03"), risk="medium", size="large"),
            _row("a-4", _need("failed", "2026-01-04")),
            _row("a-5", _need("publish", "2026-01-05")),
            _row("a-6", _need("publish", "2026-01-04"), risk="high")]
    board = attention.board_from(rows)
    # questions (high) by weight, then by age; the failure (medium) after them whatever its weight
    assert [e["id"] for e in board["waiting_on_you"]] == ["a-2", "a-3", "a-1", "a-4"]
    assert [e["id"] for e in board["ready_for_review"]] == ["a-6", "a-5"]
    assert all("_weight" not in e for g in board.values() for e in g)
    assert attention.triage_weight("high", "large") == 3 and attention.triage_weight(None, None) == 0
