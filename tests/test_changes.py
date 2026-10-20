"""changes.py: the review page backend (ranges, drafts, viewed marks, findings, request-changes section)."""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from ziggy import changes, state
from ziggy.config import Target

ID = "feat-1"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(wt: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        path = wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", message, cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


@pytest.fixture
def tk(tmp_path, monkeypatch):
    """A target whose ticket feat-1 has a worktree on feat/x with two commits over origin/main."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    for key, value in (("user.name", "Test"), ("user.email", "t@example.test"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=repo)
    base = commit(repo, {"a.py": "one\ntwo\nthree\n", "src/b.py": "x = 1\n"}, "init")
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("fetch", "-q", "origin", cwd=repo)
    target = Target(name="t", path=repo)
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", "--no-track", "-b", "feat/x", str(wt), "origin/main", cwd=repo)
    c1 = commit(wt, {"a.py": "one\nTWO\nthree\n"}, "feat: change two")
    c2 = commit(wt, {"src/b.py": "x = 1\ny = 2\n", "lib/b.py": "z = 3\n"}, "feat: add y")
    run = state.run_dir(target, ID)
    run.mkdir(parents=True)
    state.write_json(run / "ticket.json", {"id": ID, "title": "Add y", "type": "feat", "branch": "feat/x", "base_branch": "main"})
    state.write_json(run / "state.json", {"status": "done", "worktree": str(wt), "stage": "review", "run": {"n": 1, "base_commit": base},
                                          "user_reviews": []})
    return {"target": target, "wt": wt, "base": base, "c1": c1, "c2": c2, "run": run}


def set_state(tk, **values):
    st = state.load_state(tk["target"], ID)
    st.update(values)
    state.write_json(tk["run"] / "state.json", st)


def review_run(tk, name, head_before, findings, fixed=(), run_state="ok", **result):
    d = tk["run"] / name
    d.mkdir()
    state.write_json(d / "run.json", {"stage": "review", "state": run_state, "head_before": head_before, "started_at": state.now(),
                                      "result": {"verdict": "READY", "findings": findings, "fixedIds": list(fixed), **result}})


def stage_run(tk, name, result=None, run_state="ok", **record):
    """A stage run's run.json (`<nn>-<stage>`); `result` None leaves it without one (killed, lost)."""
    d = tk["run"] / name
    d.mkdir()
    state.write_json(d / "run.json", {"stage": name.split("-", 1)[1], "state": run_state, "started_at": state.now(), **record,
                                      **({"result": result} if result is not None else {})})


def post(tk, **payload):
    return changes.review_post(tk["target"], ID, payload)


# --- pure parsers --------------------------------------------------------------------------------

def test_parse_location_formats():
    p = changes.parse_location
    assert p("a/b.ts:3") == {"path": "a/b.ts", "line": 3, "end_line": None}
    assert p("`./a/b.ts:3:14`") == {"path": "a/b.ts", "line": 3, "end_line": None}
    assert p("a.py:12-18") == {"path": "a.py", "line": 12, "end_line": 18}
    assert p("a.py:18-12")["end_line"] is None
    assert p("src/x.kt#L5-L9") == {"path": "src/x.kt", "line": 5, "end_line": 9}
    assert p("x.py (lines 4–6)") == {"path": "x.py", "line": 4, "end_line": 6}
    assert p("docs/README.md, the intro") == {"path": "docs/README.md", "line": None, "end_line": None}
    assert p("/wt/src/a.py:2", "/wt") == {"path": "src/a.py", "line": 2, "end_line": None}
    assert p("/etc/passwd:1", "/wt") is None
    assert p("a.py:0")["line"] is None
    assert p("") is None and p(None) is None


def test_match_changed_path():
    files = ["src/b.py", "lib/b.py", "app/Foo.kt"]
    assert changes.match_changed_path("src/b.py", files) == "src/b.py"
    assert changes.match_changed_path("Foo.kt", files) == "app/Foo.kt"
    assert changes.match_changed_path("b.py", files) is None  # ambiguous
    assert changes.match_changed_path("c.py", files) is None


def test_parse_raw_numstat_status():
    z = "0" * 40
    raw = (f":100644 100644 {'a' * 40} {'b' * 40} M\0m.py\0"
           f":000000 100644 {z} {'c' * 40} A\0new.png\0"
           f":100644 100644 {'d' * 40} {'d' * 40} R100\0old.py\0moved.py\0"
           f":100644 000000 {'e' * 40} {z} D\0gone.py\0")
    entries = changes.parse_raw(raw)
    assert [(e["status"], e["path"], e["old_path"]) for e in entries] == [
        ("modified", "m.py", None), ("added", "new.png", None), ("renamed", "moved.py", "old.py"), ("deleted", "gone.py", None)]
    assert entries[1]["old_blob"] is None and entries[3]["new_blob"] is None
    stats = changes.parse_numstat("3\t1\tm.py\0-\t-\tnew.png\0" + "0\t0\t\0old.py\0moved.py\0")
    assert stats == {"m.py": (3, 1), "new.png": (None, None), "moved.py": (0, 0)}
    files = changes.change_files(entries, stats, {"m.py": "b" * 40, "gone.py": "deleted"})
    assert files[1]["binary"] and files[1]["image"]
    assert [f["viewed"] for f in files] == ["viewed", "no", "no", "viewed"]
    assert changes.parse_status(" M a.py\0?? new.txt\0R  b.py\0a2.py\0") == [
        {"path": "a.py", "status": "M"}, {"path": "new.txt", "status": "untracked"}, {"path": "b.py", "status": "R"}]
    assert changes.is_generated("web/package-lock.json") and changes.is_generated("dist/app.js")


def test_parse_unified_numbering_and_cap():
    text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@ def f\n one\n-two\n+TWO\n three\n\\ No newline at end of file\n"
    d = changes.parse_unified(text)
    assert d["hunks"][0]["section"] == "def f"
    assert [(l["t"], l["o"], l["n"], l["s"]) for l in d["hunks"][0]["lines"]] == [
        ("ctx", 1, 1, "one"), ("del", 2, None, "two"), ("add", None, 2, "TWO"), ("ctx", 3, 3, "three"),
        ("note", None, None, "No newline at end of file")]
    assert not d["truncated"]
    assert changes.parse_unified(text, max_lines=2)["truncated"]
    assert changes.parse_unified("Binary files a/x and b/x differ\n")["binary"]


# --- summary and ranges ----------------------------------------------------------------------------

def test_summary_all_and_commit(tk):
    s = changes.summary(tk["target"], ID)
    assert s["error"] is None
    assert s["base_commit"] == tk["base"] and s["head"]["hash"] == tk["c2"]
    assert s["range"] == {"kind": "all", "spec": "all", "from": tk["base"], "to": tk["c2"], "label": "Tüm değişiklikler"}
    assert sorted(f["path"] for f in s["files"]) == ["a.py", "lib/b.py", "src/b.py"]
    assert s["totals"] == {"files": 3, "additions": 3, "deletions": 1}
    assert [c["hash"] for c in s["commits"]] == [tk["c2"], tk["c1"]]
    assert s["uncommitted"] == [] and s["drafts"] == 0 and s["approve"] is True

    one = changes.summary(tk["target"], ID, f"commit:{tk['c1'][:10]}", to=tk["c2"])
    assert one["range"]["kind"] == "commit" and one["range"]["spec"] == f"commit:{tk['c1']}"
    assert (one["range"]["from"], one["range"]["to"]) == (tk["base"], tk["c1"])
    assert [f["path"] for f in one["files"]] == ["a.py"] and one["new_commits"] == 0


def test_since_ai_and_user_ranges(tk):
    review_run(tk, "03-review", tk["c1"], [])
    set_state(tk, user_reviews=[{"round": 1, "head": tk["c1"], "base_commit": tk["base"], "comments": 1, "at": state.now(), "source": "user"}])
    s = changes.summary(tk["target"], ID, "since:ai:1")
    assert s["range"]["kind"] == "since" and s["range"]["spec"] == "since:ai:1"
    assert (s["range"]["from"], s["range"]["to"]) == (tk["c1"], tk["c2"])
    assert sorted(f["path"] for f in s["files"]) == ["lib/b.py", "src/b.py"]
    assert {p["spec"] for p in s["points"]} == {"since:ai:1", "since:user:1"}
    u = changes.summary(tk["target"], ID, "since:user:1")
    assert u["range"]["from"] == tk["c1"]
    bad = changes.summary(tk["target"], ID, "since:ai:9")
    assert "unknown review point" in bad["error"] and bad["files"] == []


def test_pin_reports_new_commits_and_uncommitted(tk):
    s = changes.summary(tk["target"], ID, "all", to=tk["c1"])
    assert s["range"]["to"] == tk["c1"] and s["new_commits"] == 1
    assert [f["path"] for f in s["files"]] == ["a.py"]
    (tk["wt"] / "a.py").write_text("dirty\n")
    (tk["wt"] / "new.txt").write_text("n\n")
    s = changes.summary(tk["target"], ID)
    assert s["uncommitted"] == [{"path": "a.py", "status": "M"}, {"path": "new.txt", "status": "untracked"}]


def test_no_worktree(tk):
    set_state(tk, worktree=None)
    s = changes.summary(tk["target"], ID)
    assert not s["worktree"] and s["error"] == "this ticket has no worktree yet"
    with pytest.raises(LookupError):
        changes.summary(tk["target"], "feat-99")


def test_file_diff_and_blob(tk):
    d = changes.file_diff(tk["target"], ID, "a.py", tk["base"], tk["c2"])
    assert [(l["t"], l["s"]) for l in d["hunks"][0]["lines"] if l["t"] != "ctx"] == [("del", "two"), ("add", "TWO")]
    assert changes.file_diff(tk["target"], ID, "a.py", tk["base"], tk["c2"]) is d  # cached
    full = changes.file_diff(tk["target"], ID, "a.py", tk["base"], tk["c2"], full=True)
    assert full["hunks"][0]["new_lines"] == 3
    with pytest.raises(changes.ChangesError):
        changes.file_diff(tk["target"], ID, "a.py", "HEAD~1", tk["c2"])
    with pytest.raises(changes.ChangesError):
        changes.file_diff(tk["target"], ID, "", tk["base"], tk["c2"])
    (tk["wt"] / "a.py").write_text("one\nTWO\nthree\nfour\n")
    live = changes.file_diff(tk["target"], ID, "a.py", tk["c2"], "worktree")
    assert [l["s"] for l in live["hunks"][0]["lines"] if l["t"] == "add"] == ["four"]

    png = b"\x89PNG\r\n\x1a\nfake"
    c3 = commit(tk["wt"], {"img/logo.png": png}, "logo")
    assert changes.blob(tk["target"], ID, c3, "img/logo.png") == (png, "image/png")
    for rev, path in ((c3, "a.py"), ("HEAD", "img/logo.png"), (tk["base"], "img/logo.png")):
        with pytest.raises(LookupError):
            changes.blob(tk["target"], ID, rev, path)


# --- drafts and viewed marks -----------------------------------------------------------------------

def test_drafts_lifecycle(tk):
    c = post(tk, op="comment.add", path="a.py", line=2, side="new", line_text="TWO", commit_sha=tk["c2"], body="  why caps?  ")["comment"]
    assert c["round"] is None and c["body"] == "why caps?" and c["line"] == 2 and c["commit_sha"] == tk["c2"]
    edited = post(tk, op="comment.edit", id=c["id"], body="why upper case?")["comment"]
    assert edited["body"] == "why upper case?"
    with pytest.raises(changes.ChangesError, match="empty comment"):
        post(tk, op="comment.add", path="a.py", body=" ")
    with pytest.raises(changes.ChangesError, match="needs a file or a finding"):
        post(tk, op="comment.add", body="floating")
    reply = post(tk, op="comment.add", finding_id="R1-1", finding_stage="03-review", finding_action="dismiss", body="")["comment"]
    assert reply["finding_action"] == "dismiss" and reply["path"] is None and reply["line"] is None
    gone = post(tk, op="comment.add", path="a.py", body="drop me")["comment"]
    assert post(tk, op="comment.delete", id=gone["id"])["ok"]
    assert changes.draft_count(tk["target"], ID) == 2
    assert changes.summary(tk["target"], ID)["drafts"] == 2
    with pytest.raises(changes.ChangesError, match="unknown op"):
        post(tk, op="nope")

    changes.submit(tk["target"], ID, by="dev")
    with pytest.raises(changes.ChangesError, match="sent with round 1"):
        post(tk, op="comment.edit", id=c["id"], body="later")
    with pytest.raises(changes.ChangesError, match="sent with round 1"):
        post(tk, op="comment.delete", id=c["id"])
    assert changes.draft_count(tk["target"], ID) == 0


def test_viewed_resets_when_blob_changes(tk):
    files = {f["path"]: f for f in changes.summary(tk["target"], ID)["files"]}
    assert files["src/b.py"]["viewed"] == "no"
    post(tk, op="viewed.set", path="src/b.py", blob=files["src/b.py"]["new_blob"])
    assert {f["path"]: f["viewed"] for f in changes.summary(tk["target"], ID)["files"]}["src/b.py"] == "viewed"
    commit(tk["wt"], {"src/b.py": "x = 1\ny = 3\n"}, "tweak")
    s = changes.summary(tk["target"], ID)
    assert {f["path"]: f["viewed"] for f in s["files"]}["src/b.py"] == "changed"
    assert s["viewed"]["src/b.py"] == files["src/b.py"]["new_blob"]
    post(tk, op="viewed.set", path="src/b.py", blob=None)
    assert {f["path"]: f["viewed"] for f in changes.summary(tk["target"], ID)["files"]}["src/b.py"] == "no"
    with pytest.raises(changes.ChangesError, match="bad blob id"):
        post(tk, op="viewed.set", path="src/b.py", blob="HEAD")


# --- findings ----------------------------------------------------------------------------------------

def test_findings_placement_and_flags(tk):
    review_run(tk, "03-review", tk["base"], [
        {"id": "R1-1", "severity": "high", "kind": "bug", "summary": "caps", "location": "a.py:2", "disposition": "fix"},
        {"severity": "low", "summary": "which b?", "location": "b.py:1"},
        {"id": "R1-3", "summary": "abs", "location": f"{tk['wt']}/src/b.py#L1-L2"},
        {"id": "R1-4", "summary": "off diff", "location": "docs/none.md:4"},
    ])
    review_run(tk, "05-review-2", tk["c2"], [{"id": "R2-1", "summary": "again", "location": "`lib/b.py (line 1)`"}], fixed=["R1-1"])
    review_run(tk, "07-review", tk["c2"], [{"id": "X"}], run_state="failed")
    s = changes.summary(tk["target"], ID)
    by = {f["id"]: f for f in s["findings"]}
    assert set(by) == {"R1-1", "R1-2", "R1-3", "R1-4", "R2-1"}
    assert s["findings"][0]["id"] == "R2-1"  # newest round first
    assert (by["R1-1"]["path"], by["R1-1"]["line"], by["R1-1"]["fixed"], by["R1-1"]["latest"]) == ("a.py", 2, True, False)
    assert by["R1-1"]["key"] == "03-review#R1-1" and by["R1-1"]["round"] == 1
    assert (by["R1-2"]["path"], by["R1-2"]["line"], by["R1-2"]["severity"], by["R1-2"]["disposition"]) == (None, None, "low", "fix")
    assert (by["R1-3"]["path"], by["R1-3"]["line"], by["R1-3"]["end_line"]) == ("src/b.py", 1, 2)
    assert by["R1-4"]["path"] is None
    assert (by["R2-1"]["path"], by["R2-1"]["line"], by["R2-1"]["latest"], by["R2-1"]["round"]) == ("lib/b.py", 1, True, 2)
    assert [p["spec"] for p in s["points"] if p["kind"] == "ai"] == ["since:ai:1", "since:ai:2"]


# --- submit ------------------------------------------------------------------------------------------

def test_submit_builds_section_and_continues_task_numbers(tk):
    review_run(tk, "03-review", tk["base"], [
        {"id": "R1-1", "severity": "high", "kind": "bug", "disposition": "defer", "summary": "caps  are\nloud", "location": "a.py:2"},
        {"id": "R1-2", "severity": "medium", "kind": "risk", "disposition": "fix", "summary": "y unused", "location": "src/b.py:2"},
    ])
    plan = state.plan_path(tk["target"], ID)
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n\n- [x] T014 do it\n- [ ] V020 verify\n")
    post(tk, op="comment.add", finding_id="R1-2", finding_stage="03-review", finding_action="dismiss", body="")
    post(tk, op="comment.add", finding_id="R1-1", finding_stage="03-review", finding_action="fix", body="please do")
    post(tk, op="comment.add", path="src/b.py", line=2, side="new", line_text="y = `2`", body="rename y\n\nto ys")
    post(tk, op="comment.add", path="a.py", line=2, side="old", line_text="two", body="keep lower")

    out = changes.submit(tk["target"], ID, by="dev")
    today = datetime.now().astimezone().date().isoformat()
    expected = f"""## Review (user, round 1)

Requested by dev on {today} · diff `{tk['base'][:10]}..{tk['c2'][:10]}` · 4 comments. The comments are the user's own words
(verbatim) and outrank the plan where they disagree. Fix tasks are at the end; their ids continue the plan's.

### U1-1 · `a.py:2`
Line (old side, removed): `two`

> keep lower

### U1-2 · `src/b.py:2`
Line (new side): `` y = `2` ``

> rename y
>
> to ys

### U1-3 · reviewer finding R1-2 · `src/b.py:2`
Finding (medium risk, fix): y unused
The user: **dismiss it**

### U1-4 · reviewer finding R1-1 · `a.py:2`
Finding (high bug, defer): caps are loud
The user: **fix it**

> please do

### Fix tasks
- [ ] T015 [U1-1] Address U1-1 at `a.py:2`
- [ ] T016 [U1-2] Address U1-2 at `src/b.py:2`
- [ ] T017 [U1-4] Fix reviewer finding R1-1 as the user asks (`a.py:2`)
Not a task: U1-3, the user dismisses reviewer finding R1-2; leave that code as it is.
"""
    assert out == {"round": 1, "heading": "Review (user, round 1)", "comments": 4, "section_md": expected, "findings": ["R1-1"]}
    review = changes.load_review(tk["target"], ID)
    assert {c["round"] for c in review["comments"]} == {1}
    assert review["rounds"][0] == {"round": 1, "head": tk["c2"], "base_commit": tk["base"], "comments": 4, "by": "dev",
                                   "at": review["rounds"][0]["at"]}
    assert (changes.review_dir(tk["target"], ID) / "round-1.md").read_text() == expected
    assert plan.read_text() == "# Plan\n\n- [x] T014 do it\n- [ ] V020 verify\n"  # pipeline.request_changes appends it

    post(tk, op="comment.add", path="a.py", body="file note")
    second = changes.submit(tk["target"], ID, by="dev")
    assert second["round"] == 2 and second["heading"] == "Review (user, round 2)"
    assert "### U2-1 · `a.py`\n\n> file note" in second["section_md"]
    assert "- [ ] T015 [U2-1] Address U2-1 at `a.py`" in second["section_md"]  # the plan was not appended here


def test_submit_guards(tk):
    with pytest.raises(changes.ChangesError, match="no draft comments"):
        changes.submit(tk["target"], ID, by="me")
    post(tk, op="comment.add", path="a.py", body="x")
    lock = tk["target"].state_dir / "locks" / f"ticket-{ID}"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{os.getpid()} run {ID}\n")
    with pytest.raises(changes.ChangesError, match="active run"):
        changes.submit(tk["target"], ID, by="me")
    assert changes.summary(tk["target"], ID)["submit_blocked"].endswith("wait for it to finish")
    (lock / "owner").unlink()
    lock.rmdir()
    set_state(tk, status="needs-user")
    with pytest.raises(changes.ChangesError, match="needs-user"):
        changes.submit(tk["target"], ID, by="me")
    set_state(tk, status="queued")
    with pytest.raises(changes.ChangesError, match="run slot"):
        changes.submit(tk["target"], ID, by="me")
    set_state(tk, status="failed", worktree=str(tk["wt"]) + "-gone")
    with pytest.raises(changes.ChangesError, match="no worktree"):
        changes.submit(tk["target"], ID, by="me")
    assert changes.draft_count(tk["target"], ID) == 1  # nothing was marked sent
    set_state(tk, worktree=str(tk["wt"]))
    assert changes.submit(tk["target"], ID, by="me")["comments"] == 1
    assert json.loads((changes.review_dir(tk["target"], ID) / "review.json").read_text())["rounds"][0]["by"] == "me"


# --- findings detail, guide, commits, evidence, fix selected, PR draft ------------------------------

def test_findings_carry_class_confidence_verification_and_outcome(tk):
    stage_run(tk, "02-implement", {"verdict": "READY", "outcomes": [{"finding_id": "R1-1", "outcome": "fixed"}]})  # before it
    review_run(tk, "03-review", tk["base"], [
        {"id": "R1-1", "class": "Pre_Existing", "confidence": "85%", "verified": True, "location": "a.py:2", "summary": "a"},
        {"id": "R1-2", "severity": "low", "confidence": 0.4, "verified": "yes", "summary": "b"},
        {"id": "R1-3", "disposition": "defer", "confidence": "high", "summary": "c"},
        {"id": "R1-4", "severity": "high", "confidence": 140, "summary": "d"},
        {"id": "R1-5", "severity": "high", "disposition": "decision", "summary": "e"},
        {"id": "R1-6", "severity": "high", "disposition": "dismiss", "summary": "f"},
    ], unverified=[{"location": "x.py:1", "summary": "maybe", "reason": "could not reproduce"}, "bare", 3])
    stage_run(tk, "04-implement", {"verdict": "READY", "outcomes": [
        {"finding_id": "R1-4", "outcome": "SKIPPED", "note": "by design"}, {"id": "R1-2", "outcome": "weird"}]})
    stage_run(tk, "05-implement", {"verdict": "READY", "outcomes": [{"finding_id": "R1-4", "outcome": "fixed", "note": " "}]})
    stage_run(tk, "06-implement", {"verdict": "FAILED", "outcomes": [{"finding_id": "R1-4", "outcome": "no_change"}]}, run_state="failed")
    s = changes.summary(tk["target"], ID)
    by = {f["id"]: f for f in s["findings"]}
    assert (by["R1-1"]["class"], by["R1-1"]["confidence"], by["R1-1"]["verified"]) == ("pre-existing", 0.85, True)
    assert (by["R1-2"]["class"], by["R1-2"]["confidence"], by["R1-2"]["verified"]) == ("nit", 0.4, None)
    assert (by["R1-3"]["class"], by["R1-3"]["confidence"]) == ("pre-existing", "high")
    assert (by["R1-4"]["class"], by["R1-4"]["confidence"]) == ("important", None)
    assert by["R1-4"]["outcome"] == {"outcome": "fixed", "note": None, "stage_dir": "05-implement"}  # newest ok report wins
    assert by["R1-2"]["outcome"] == {"outcome": "no_change", "note": None, "stage_dir": "04-implement"}
    assert by["R1-1"]["outcome"] is None  # a report from before the review is about something else
    assert [i for i, f in by.items() if f["open_important"]] == ["R1-4"]
    assert by["R1-5"]["selectable"] and not by["R1-5"]["open_important"] and not by["R1-5"]["dismissed"]
    assert by["R1-6"]["dismissed"] and by["R1-6"]["selectable"] and not by["R1-6"]["open_important"]  # the user may still fix it
    assert s["unverified"] == [
        {"stage_dir": "03-review", "location": "x.py:1", "summary": "maybe", "reason": "could not reproduce"},
        {"stage_dir": "03-review", "location": None, "summary": "bare", "reason": None}]
    post(tk, op="comment.add", finding_id="R1-4", finding_stage="03-review", body="why?")
    assert not {f["id"]: f for f in changes.summary(tk["target"], ID)["findings"]}["R1-4"]["open_important"]


def test_dismissed_findings_stay_until_a_later_review_lists_them(tk):
    review_run(tk, "03-review", tk["base"], [
        {"id": "R1-1", "disposition": "dismiss", "summary": "a", "reason": "unreachable", "location": "a.py:1"},
        {"id": "R1-2", "disposition": "dismiss", "summary": "b"},
        {"id": "R1-3", "disposition": "dismiss", "summary": "c"}])
    review_run(tk, "05-review", tk["base"], [{"id": "R1-2", "disposition": "fix", "summary": "b again"},
                                             {"id": "R2-1", "disposition": "dismiss", "summary": "d"}], fixed=["R1-3"])
    reviews = changes._review_runs(tk["target"], ID)
    assert changes.dismissed_findings(reviews, changes._records(tk["target"], ID), []) == [
        {"key": "03-review#R1-1", "id": "R1-1", "stage_dir": "03-review", "summary": "a", "reason": "unreachable", "location": "a.py:1"},
        {"key": "05-review#R2-1", "id": "R2-1", "stage_dir": "05-review", "summary": "d", "reason": None, "location": None}]
    assert changes.dismissed_count(tk["target"], ID) == 2
    by = {f["key"]: f for f in changes.summary(tk["target"], ID)["findings"]}
    assert by["03-review#R1-1"]["selectable"] and not by["03-review#R1-1"]["latest"]  # an older round's dismissal can be sent
    assert not by["03-review#R1-2"]["dismissed"] and not by["03-review#R1-2"]["selectable"]


def test_a_dismissal_ends_when_the_user_sends_it_to_fix_or_the_implementer_fixed_it(tk):
    review_run(tk, "03-review", tk["base"], [{"id": f"R1-{n}", "disposition": "dismiss", "summary": f"s{n}"} for n in (1, 2, 3, 4)])
    stage_run(tk, "04-implement", {"verdict": "READY", "outcomes": [{"finding_id": "R1-2", "outcome": "fixed"},
                                                                    {"finding_id": "R1-4", "outcome": "skipped"}]})
    reply = {"round": 1, "path": None, "line": None, "side": "new", "body": "", "finding_action": "fix"}
    changes._save_review(tk["target"], ID, {"rounds": [], "viewed": {}, "comments": [
        {**reply, "id": "a", "finding_id": "R1-1", "finding_stage": "03-review"},  # sent: the user wants it fixed
        {**reply, "id": "b", "round": None, "finding_id": "R1-3", "finding_stage": "03-review"}]})  # still a draft
    assert changes.dismissed_count(tk["target"], ID) == 2
    by = {f["id"]: f for f in changes.summary(tk["target"], ID)["findings"]}
    assert [i for i, f in sorted(by.items()) if f["dismissed"]] == ["R1-3", "R1-4"]
    body = changes.pr_draft(tk["target"], ID)["body"]
    assert "bulgular (2)" in body and "`R1-3` s3" in body and "`R1-4` s4" in body and "`R1-1`" not in body and "`R1-2`" not in body


def test_guide_reads_the_newest_review_in_order(tk):
    review_run(tk, "03-review", tk["base"], [], guide=[{"file": "old.py", "summary": "old"}])
    review_run(tk, "05-review", tk["c2"], [], summary="2 important", guide=[
        {"file": "b.py", "summary": "adds  y\n", "howToVerify": "run b", "order": "2"},
        {"file": "a.py:2", "summary": "caps", "order": 1},
        {"file": "docs/x.md", "howToVerify": "  "},
        {"summary": "no file"},
    ])
    review_run(tk, "07-review", tk["c2"], [], guide=[])
    g = changes.summary(tk["target"], ID)["guide"]
    assert (g["stage_dir"], g["summary"]) == ("05-review", "2 important")
    assert [(e["file"], e["path"], e["line"], e["summary"], e["how_to_verify"]) for e in g["entries"]] == [
        ("a.py:2", "a.py", 2, "caps", None), ("b.py", None, None, "adds y", "run b"), ("docs/x.md", None, None, "", None)]
    set_state(tk, worktree=None)
    assert [e["file"] for e in changes.summary(tk["target"], ID)["guide"]["entries"]] == ["a.py:2", "b.py", "docs/x.md"]


def test_commits_name_the_stage_run_that_made_them(tk):
    c3 = commit(tk["wt"], {"hand.txt": "x\n"}, "chore: by hand")
    stage_run(tk, "02-implement", {"verdict": "READY"}, head_before=tk["base"], head_after=tk["c1"], ticket_visit=1)
    stage_run(tk, "03-review", {"verdict": "READY", "findings": []}, head_before=tk["c1"], head_after=tk["c1"])
    stage_run(tk, "04-implement", None, run_state="killed", head_before=tk["c1"], head_after=tk["c2"], ticket_visit=2)
    stage_run(tk, "05-implement", {"verdict": "READY"}, head_before=tk["base"], head_after=tk["c2"], ticket_visit=3)
    s = changes.summary(tk["target"], ID)
    assert {c["hash"]: (c["stage_dir"], c["stage"], c["visit"]) for c in s["commits"]} == {
        c3: (None, None, None), tk["c2"]: ("04-implement", "implement", 2), tk["c1"]: ("02-implement", "implement", 1)}


def test_attribute_commits_needs_both_heads_in_the_branch():
    commits = [{"hash": "c"}, {"hash": "b"}, {"hash": "a"}]
    records = [("01-implement", {"stage": "implement", "head_before": "rewritten", "head_after": "b"}),
               ("02-implement", {"stage": "implement", "head_before": "c", "head_after": "b"}),  # went backwards
               ("03-implement", {"stage": "implement", "head_before": "b", "head_after": "c", "visit": 2}),
               ("04-implement", {"stage": "implement", "head_before": "base", "head_after": "a"})]
    assert changes.attribute_commits(commits, records, "base") == [
        {"stage_dir": "03-implement", "stage": "implement", "visit": 2}, None,
        {"stage_dir": "04-implement", "stage": "implement", "visit": None}]


def test_evidence_groups_keep_the_first_listing(tk):
    root = changes.artifacts_dir(tk["target"], ID)
    root.mkdir(parents=True)
    (root / "shot.png").write_bytes(b"12345")
    stage_run(tk, "02-implement", {"verdict": "READY", "artifacts": [
        {"path": "shot.png", "caption": "The app"}, "gone.png", "../escape.png", {"path": str(root / "notes.log")}]},
        ticket_visit=1, finished_at="2026-10-16T10:00:00+00:00")
    stage_run(tk, "03-review", {"verdict": "READY", "findings": [], "artifacts": [{"path": "shot.png", "caption": "Re-checked"}]})
    stage_run(tk, "04-implement", {"verdict": "READY", "artifacts": ["late.mp4"]}, run_state="failed")
    stage_run(tk, "05-implement", {"verdict": "READY", "artifacts": ["new.mp4"]})
    ev = changes.summary(tk["target"], ID)["evidence"]
    assert [g["stage_dir"] for g in ev] == ["05-implement", "02-implement"]  # newest first, empty groups dropped
    first = ev[1]
    assert (first["stage"], first["visit"], first["at"]) == ("implement", 1, "2026-10-16T10:00:00+00:00")
    assert [(a["path"], a["kind"], a["caption"], a["exists"], a["size"]) for a in first["artifacts"]] == [
        ("shot.png", "image", "The app", True, 5), ("gone.png", "image", None, False, None), ("notes.log", "log", None, False, None)]
    assert first["artifacts"][0]["url"] == "/api/files/t/feat-1/shot.png"
    assert changes.file_url("t", "feat-1", "a b/c#.png") == "/api/files/t/feat-1/a%20b/c%23.png"


def test_artifact_serves_only_regular_files_inside_the_artifacts_dir(tk, monkeypatch):
    root = changes.artifacts_dir(tk["target"], ID)
    (root / "shots").mkdir(parents=True)
    (root / "shots" / "home.png").write_bytes(b"png")
    (root / "notes.log").write_text("log")
    (root / "run.sh").write_text("x")
    (root / ".hidden.png").write_bytes(b"x")
    (root / "dir.png").mkdir()
    secret = tk["run"] / "secret.png"  # runs/<id>/, next to inbox/
    secret.write_bytes(b"s")
    (root / "link.png").symlink_to(secret)
    (root / "inner.png").symlink_to(root / "shots" / "home.png")
    (root / "script.png").symlink_to(root / "run.sh")
    t = tk["target"]
    assert changes.artifact(t, ID, "shots/home.png") == ((root / "shots" / "home.png").resolve(), "image/png")
    assert changes.artifact(t, ID, "notes.log")[1] == "text/plain; charset=utf-8"
    assert changes.artifact(t, ID, "inner.png")[0] == (root / "shots" / "home.png").resolve()
    for rel in ("../../secret.png", "shots/../../../secret.png", "../secret.png", str(secret), str(root / "shots" / "home.png"),
                "link.png", "script.png", "run.sh", ".hidden.png", "missing.png", "dir.png", "shots", "", "shots//home.png",
                "shots/./home.png", "shots\\home.png", "a\0.png", None):
        with pytest.raises(LookupError):
            changes.artifact(t, ID, rel)
    monkeypatch.setitem(changes.ARTIFACT_LIMITS, ".png", 2)
    with pytest.raises(changes.ArtifactTooLarge):
        changes.artifact(t, ID, "shots/home.png")
    with pytest.raises(LookupError):
        changes.artifact(t, "feat-99", "notes.log")


def test_findings_fix_drafts_each_reply_once(tk):
    review_run(tk, "03-review", tk["base"], [{"id": "R1-1", "summary": "a"}, {"id": "R1-2", "summary": "b"}])
    review_run(tk, "05-review", tk["c2"], [{"id": "R1-1", "summary": "a again"}, {"id": "R2-2", "summary": "c"}, {"id": "R2-3"}])
    post(tk, op="comment.add", finding_id="R2-3", finding_stage="05-review", finding_action="dismiss", body="")
    out = post(tk, op="findings.fix", ids=["05-review#R2-2", "R1-1", "03-review#R1-2", "05-review#R2-3", "05-review#nope", 7,
                                            "05-review#R2-2"])
    assert out == {"ok": True, "added": 3, "message": '3 "fix it" replies drafted'}
    comments = changes.load_review(tk["target"], ID)["comments"]
    fixes = [c for c in comments if c["finding_action"] == "fix"]
    assert [(c["finding_id"], c["finding_stage"]) for c in fixes] == [("R2-2", "05-review"), ("R1-1", "05-review"), ("R1-2", "03-review")]
    assert all(c["round"] is None and c["body"] == "" and c["path"] is None and c["line"] is None for c in fixes)
    assert post(tk, op="findings.fix", ids=["05-review#R2-2", "R1-1"]) == {
        "ok": True, "added": 0, "message": "those findings already have replies"}
    assert changes.draft_count(tk["target"], ID) == 4
    assert changes.submit(tk["target"], ID, by="me")["findings"] == ["R2-2", "R1-1", "R1-2"]
    assert post(tk, op="findings.fix", ids=["05-review#R2-2"])["added"] == 0  # a sent fix reply still counts
    assert post(tk, op="findings.fix", ids=["05-review#R2-3"])["added"] == 1  # a sent dismissal does not
    with pytest.raises(changes.ChangesError, match="no findings selected"):
        post(tk, op="findings.fix", ids=[])
    with pytest.raises(changes.ChangesError, match="none of the selected"):
        post(tk, op="findings.fix", ids=["05-review#zzz"])
    with pytest.raises(changes.ChangesError, match="must be a list"):
        post(tk, op="findings.fix", ids="R1-1")


def test_pr_draft_composes_summary_guide_acceptance_and_evidence(tk):
    plan = state.plan_path(tk["target"], ID)
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n\n## Goal & acceptance\n\nAdd y.\n\n- AC-1 y is 2\n- [x] AC-2 x stays 1\n- AC-1 → T001\n\n"
                    "## Plan\n\n- [ ] T001 add y\n")
    root = changes.artifacts_dir(tk["target"], ID)
    root.mkdir(parents=True)
    (root / "y.png").write_bytes(b"p")
    (root / "run.log").write_text("ok")
    stage_run(tk, "02-implement", {"verdict": "READY", "summary": "old"})
    stage_run(tk, "04-implement", {"verdict": "READY", "summary": "  Added y to b.  ", "artifacts": [
        {"path": "y.png", "caption": "AC-1: y\nshows"}, "run.log", "gone.png"]})
    review_run(tk, "05-review", tk["c2"], [], guide=[
        {"file": "src/b.py", "summary": "adds y", "howToVerify": "python -c 'import b'"}, {"file": "a.py", "summary": "caps"}])
    stage_run(tk, "06-implement", {"verdict": "READY", "summary": " "})
    d = changes.pr_draft(tk["target"], ID)
    assert d["title"] == "feat: Add y"
    assert d["body"] == (
        "## Summary\n\nAdded y to b.\n\n"
        "## Changes\n\n- `src/b.py` — adds y\n- `a.py` — caps\n\n"
        "## How to verify\n\n- `src/b.py`: python -c 'import b'\n\n"
        "## Acceptance criteria\n\n- AC-1 y is 2\n- AC-2 x stays 1\n\n"
        "## Review\n\n**READY** · no findings\n\n"
        "## Evidence\n\n- AC-1: y shows (`y.png`)\n")  # no footer: publish appends it; logs and missing files left out
    plan.write_text("# Plan\n\n## Goal & acceptance\n\nAdd y; AC are in prose here.\n")
    assert "## Goal & acceptance\n\nAdd y; AC are in prose here." in changes.pr_draft(tk["target"], ID)["body"]
