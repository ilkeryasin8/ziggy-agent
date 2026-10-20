"""Protected files (AGENTS.md, .claude/settings.json): the patch a stage leaves for them, its validation, the run that
goes on, the inbox card, and Uygula / Reddet (panel and CLI)."""
import json
import subprocess

import pytest
from conftest import calls, cli, git, script
from test_dashboard import act, panel, request  # noqa: F401 - the panel fixture

from ziggy import attention, config, protected, results, state

PATCH = """diff --git a/AGENTS.md b/AGENTS.md
new file mode 100644
--- /dev/null
+++ b/AGENTS.md
@@ -0,0 +1,2 @@
+# Agents
+Run the tests with pytest.
"""
README_PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-target
+changed
"""
LISTED = [{"path": "artifacts/agents.patch", "files": ["AGENTS.md"], "summary": "document the tests"}]
DONE_REVIEW = {"review": [{"verdict": "READY", "route": "converged", "converged": True}]}


def _state(env, ticket_id="local-1"):
    return json.loads((env["state"] / "runs" / ticket_id / "state.json").read_text())


def _wt(env, ticket_id="local-1"):
    return env["state"] / "worktrees" / ticket_id


def _run_with_patch(env, implement: dict) -> subprocess.CompletedProcess:
    script(env, {"implement": [{"_commit": "feature.txt", "_artifacts": {"agents.patch": PATCH}, **implement}], **DONE_REVIEW})
    assert cli("new", "feat: document the tests").returncode == 0
    return cli("run", "local-1")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=repo)
    (repo / "README.md").write_text("target\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    art = tmp_path / "artifacts"
    art.mkdir()
    return repo, art


# --- the protected list and validation ----------------------------------------------------------

def test_the_protected_files_are_the_settings_edit_denies():
    settings = json.loads(protected.SETTINGS.read_text())
    denied = {r[len("Edit(./"):-1] for r in settings["permissions"]["deny"] if r.startswith("Edit(./")}
    assert set(protected.FILES) == denied and "AGENTS.md" in protected.FILES and ".claude/settings.json" in protected.FILES
    assert protected.patch_files(PATCH) == ["AGENTS.md"]
    assert protected.patch_files(README_PATCH + PATCH) == ["AGENTS.md", "README.md"]
    assert protected.patch_files("just text\n") == []


def test_settle_keeps_only_existing_protected_patches_that_apply(tmp_path):
    repo, art = _repo(tmp_path)
    (art / "agents.patch").write_text(PATCH)
    (art / "readme.patch").write_text(README_PATCH)
    (art / "both.patch").write_text(PATCH + README_PATCH)
    (art / "stale.patch").write_text(PATCH.replace("new file mode 100644\n--- /dev/null", "--- a/AGENTS.md")
                                     .replace("@@ -0,0 +1,2 @@", "@@ -1 +1,2 @@\n-old line"))
    result = {"verdict": "READY", "protectedPatches": [
        *LISTED, {"path": "artifacts/missing.patch", "files": ["AGENTS.md"]}, {"path": "readme.patch", "files": ["README.md"]},
        {"path": "both.patch"}, {"path": "stale.patch"}, {"path": "../escape.patch"}]}
    warnings = protected.settle("implement", result, str(art), repo)
    assert result["protectedPatches"] == [{"artifact": "agents.patch", "files": ["AGENTS.md"], "summary": "document the tests"}]
    joined = "\n".join(warnings)
    assert "missing.patch: not found" in joined and "touches files that are not protected: README.md" in joined
    assert "stale.patch: does not apply on HEAD" in joined and "escape.patch" in joined and len(warnings) == 5
    # Other schemas never carry it; the schema offers it to implement and generic stages only.
    review = {"verdict": "READY", "protectedPatches": LISTED}
    assert protected.settle("review", review, str(art), repo) == [] and "protectedPatches" not in review
    assert "protectedPatches" in results.json_schema("implement")["properties"]
    assert "protectedPatches" in results.json_schema("generic")["properties"]
    assert "protectedPatches" not in results.json_schema("review")["properties"]


def test_settle_reads_the_old_failed_shape_as_ready(tmp_path):
    repo, art = _repo(tmp_path)
    (art / "s7-agents-md.patch").write_text(PATCH)
    (art / "test.log").write_text("ok\n")
    failed = {"verdict": "FAILED", "summary": "T033: AGENTS.md'ye yazmak yasak; yama artifacts/s7-agents-md.patch'te",
              "openItems": ["T033: AGENTS.md bu oturumda yazmaya kapalı"],
              "artifacts": [{"path": "s7-agents-md.patch", "kind": "text", "caption": "AGENTS.md Stack/Tests"},
                            {"path": "test.log", "kind": "log", "caption": None}]}
    warnings = protected.settle("implement", failed, str(art), repo)
    assert failed["verdict"] == "READY"
    assert failed["protectedPatches"] == [{"artifact": "s7-agents-md.patch", "files": ["AGENTS.md"], "summary": "AGENTS.md Stack/Tests"}]
    assert warnings and "FAILED read as READY" in warnings[-1]
    # A failure about something else stays a failure, patch or not; so does one whose patch touches other files.
    other = {"verdict": "FAILED", "summary": "the build broke", "artifacts": failed["artifacts"]}
    assert protected.settle("implement", other, str(art), repo) == [] and other["verdict"] == "FAILED"
    (art / "s7-agents-md.patch").write_text(PATCH + README_PATCH)
    again = {"verdict": "FAILED", "summary": "AGENTS.md is denied", "artifacts": failed["artifacts"]}
    protected.settle("implement", again, str(art), repo)
    assert again["verdict"] == "FAILED"


# --- the engine ---------------------------------------------------------------------------------

def test_ready_with_a_patch_continues_and_waits_on_the_inbox(env):
    proc = _run_with_patch(env, {"verdict": "READY", "protectedPatches": LISTED})
    assert proc.returncode == 0, proc.stderr
    st = _state(env)
    assert st["status"] == "done"
    [entry] = st["protected_patches"]
    assert entry["status"] == "pending" and entry["files"] == ["AGENTS.md"] and entry["stage"] == "implement"
    assert entry["created_at"] and entry["summary"] == "document the tests"
    stored = env["state"] / "runs" / "local-1" / "protected" / "1.patch"
    assert stored.read_text() == PATCH
    assert not (_wt(env) / "AGENTS.md").exists()  # nothing lands on the branch before you say so
    review_prompt = [c["prompt"] for c in calls(env) if c.get("stage") == "review"][0]
    assert "## Protected file changes waiting for the user" in review_prompt and "#1 AGENTS.md: document the tests" in review_prompt
    plan_prompt = [c["prompt"] for c in calls(env) if c.get("stage") == "plan"][0]
    assert "`AGENTS.md`" in plan_prompt and "prepare a patch for <file>" in plan_prompt
    target = config.load_target("demo")
    need = attention.needs(target, "local-1")
    assert need["kind"] == "protected" and need["title"] == "Korumalı dosya değişikliği: AGENTS.md"
    assert need["key"] == "protected:1" and need["patch"]["diff"] == PATCH and need["severity"] == "medium"
    assert not need["dismissable"]
    board = attention.board([target])
    assert [e["kind"] for e in board["waiting_on_you"]] == ["protected"]
    # The same patch reported again by a later visit is not a second item.
    st_before = state.load_state(target, "local-1")
    assert protected.record(target, "local-1", st_before, [{"artifact": "agents.patch", "files": ["AGENTS.md"], "summary": ""}],
                            str(env["state"] / "runs" / "local-1" / "inbox" / "artifacts"), "implement", "04-implement",
                            base_head="HEAD") == []
    assert st["protected_patches"][0]["base_head"] == git("rev-parse", "HEAD", cwd=_wt(env)).strip()


def test_the_old_failed_shape_continues_with_a_warning(env):
    proc = _run_with_patch(env, {"verdict": "FAILED", "summary": "AGENTS.md is denied; the patch is in artifacts/agents.patch",
                                 "artifacts": [{"path": "agents.patch", "kind": "text", "caption": "AGENTS.md tests section"}]})
    assert proc.returncode == 0, proc.stderr
    st = _state(env)
    assert st["status"] == "done" and st["protected_patches"][0]["status"] == "pending"
    run = json.loads(next((env["state"] / "runs" / "local-1").glob("*-implement")).joinpath("run.json").read_text())
    assert run["verdict"] == "READY" and any("FAILED read as READY" in w for w in run["result_warnings"])
    assert run["protected_patches"] == [1]


def test_apply_commits_exactly_the_patch(env):
    _run_with_patch(env, {"verdict": "READY", "protectedPatches": LISTED})
    wt = _wt(env)
    before = git("rev-parse", "HEAD", cwd=wt).strip()
    proc = cli("protected", "local-1")
    assert proc.returncode == 0 and "#1\tpending\tAGENTS.md" in proc.stdout and "  | +# Agents" in proc.stdout
    proc = cli("protected", "local-1", "apply", "1")
    assert proc.returncode == 0, proc.stderr
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    assert git("rev-parse", "HEAD~1", cwd=wt).strip() == before
    assert git("log", "-1", "--format=%s", cwd=wt).strip() == "docs(local-1): apply approved change to AGENTS.md"
    assert git("diff", "--name-only", f"{before}..{head}", cwd=wt).split() == ["AGENTS.md"]
    assert (wt / "AGENTS.md").read_text() == "# Agents\nRun the tests with pytest.\n"
    assert git("status", "--porcelain", cwd=wt) == ""
    entry = _state(env)["protected_patches"][0]
    assert entry["status"] == "applied" and entry["sha"] == head and entry["applied_at"]
    assert head[:10] in proc.stdout
    assert attention.needs(config.load_target("demo"), "local-1")["kind"] == "publish"  # the card is gone
    assert cli("protected", "local-1", "apply", "1").returncode == 2  # applied already


def test_apply_refuses_while_running_and_on_a_stale_patch(env):
    _run_with_patch(env, {"verdict": "READY", "protectedPatches": LISTED})
    target = config.load_target("demo")
    wt = _wt(env)
    before = git("rev-parse", "HEAD", cwd=wt).strip()
    with state.lock(target, "ticket-local-1", "ticket local-1 run"):
        with pytest.raises(state.LockError):
            protected.apply(target, "local-1", 1)
        with pytest.raises(state.LockError):
            protected.reject(target, "local-1", 1)
    assert git("rev-parse", "HEAD", cwd=wt).strip() == before
    # The branch moved on and the patch no longer applies: nothing changes, it stays pending.
    (wt / "AGENTS.md").write_text("# Someone else's rules\n")
    git("add", "AGENTS.md", cwd=wt)
    git("commit", "-q", "-m", "docs: rules", cwd=wt)
    moved = git("rev-parse", "HEAD", cwd=wt).strip()
    proc = cli("protected", "local-1", "apply", "1")
    assert proc.returncode == 2 and "AGENTS.md bu yama hazırlandıktan sonra başka bir değişiklikle değişti" in proc.stderr
    assert git("rev-parse", "HEAD", cwd=wt).strip() == moved and git("status", "--porcelain", cwd=wt) == ""
    assert _state(env)["protected_patches"][0]["status"] == "pending"


def test_reject_with_a_reason(env):
    _run_with_patch(env, {"verdict": "READY", "protectedPatches": LISTED})
    assert cli("protected", "local-1", "apply").returncode == 2  # needs the number
    proc = cli("protected", "local-1", "reject", "1", "--reason", "I will write it myself")
    assert proc.returncode == 0 and "#1 rejected" in proc.stdout
    entry = _state(env)["protected_patches"][0]
    assert entry["status"] == "rejected" and entry["reason"] == "I will write it myself"
    assert not (_wt(env) / "AGENTS.md").exists()
    assert "rejected: I will write it myself" in cli("protected", "local-1").stdout
    assert attention.needs(config.load_target("demo"), "local-1")["kind"] == "publish"


def test_panel_apply_reject_and_the_pr_form_warning(panel, env):  # noqa: F811
    _run_with_patch(env, {"verdict": "READY", "protectedPatches": LISTED})
    code, rows, _ = request(panel, "GET", "/api/status")
    row = next(t for t in rows["tickets"] if t["id"] == "local-1")
    assert row["needs_you"]["kind"] == "protected" and row["protected_pending"] == [["AGENTS.md"]]
    code, draft, _ = request(panel, "GET", "/api/changes/demo/local-1/pr-draft")
    assert code == 200 and draft["protected_pending"] == ["AGENTS.md"]
    assert act(panel, "local-1", "protected_apply")[0] == 400  # no patch number
    assert act(panel, "local-1", "protected_apply", n=7)[0] == 404
    code, out, _ = act(panel, "local-1", "protected_apply", n=1)
    assert code == 200 and out["protected"]["status"] == "applied"
    assert act(panel, "local-1", "protected_reject", n=1)[0] == 409  # decided already
    code, draft, _ = request(panel, "GET", "/api/changes/demo/local-1/pr-draft")
    assert draft["protected_pending"] == []


# --- supersede: a newer patch of the same file, and one prepared without an applied earlier one ---------------

ORIGINAL = "# Agents\nRun the tests.\n"
V1 = "# Agents\nRun the tests with pytest.\nStack: Python.\n"
V2 = "# Agents\nRun the tests with pytest -q.\nStack: Python 3.12, stdlib only.\n"


def _ticket_with_agents_md(env, tmp_path):
    """A finished ticket whose branch has AGENTS.md (ORIGINAL), and patches to V1 and V2 both made from ORIGINAL (the
    local-11 shape: the second implement round revised the section without the first patch)."""
    script(env, {"implement": [{"_commit": "feature.txt", "verdict": "READY"}], **DONE_REVIEW})
    assert cli("new", "feat: document the tests").returncode == 0
    assert cli("run", "local-1").returncode == 0
    wt = _wt(env)
    (wt / "AGENTS.md").write_text(ORIGINAL)
    git("add", "AGENTS.md", cwd=wt)
    git("commit", "-q", "-m", "docs: agents", cwd=wt)
    art = tmp_path / "art"
    art.mkdir()
    for name, text in (("v1.patch", V1), ("v2.patch", V2)):
        (wt / "AGENTS.md").write_text(text)
        (art / name).write_text(git("diff", cwd=wt))
    git("checkout", "--", "AGENTS.md", cwd=wt)
    return config.load_target("demo"), wt, art, git("rev-parse", "HEAD", cwd=wt).strip()


def _record(target, art, name, base, phase_dir="04-implement"):
    st = state.load_state(target, "local-1")
    added = protected.record(target, "local-1", st, [{"artifact": name, "files": ["AGENTS.md"], "summary": name}], str(art),
                             "implement", phase_dir, base_head=base)
    state.save_state(target, "local-1", st)
    return added


def test_a_newer_patch_supersedes_the_pending_one(env, tmp_path):
    target, wt, art, base = _ticket_with_agents_md(env, tmp_path)
    _record(target, art, "v1.patch", base)
    [entry] = _record(target, art, "v2.patch", base, "06-implement")
    first, second = _state(env)["protected_patches"]
    assert first["status"] == "superseded" and first["superseded_by"] == 2 and second["status"] == "pending"
    assert entry["base_head"] == base
    assert [p["n"] for p in protected.pending(_state(env))] == [2]
    need = attention.needs(target, "local-1")
    assert need["key"] == "protected:2" and need["patch"]["replaces"] == []
    listed = cli("protected", "local-1").stdout
    assert "#1\tsuperseded (superseded by #2)\tAGENTS.md" in listed and "#2\tpending\tAGENTS.md" in listed
    proc = cli("protected", "local-1", "apply", "1")
    assert proc.returncode == 2 and "superseded by #2" in proc.stderr
    assert cli("protected", "local-1", "apply", "2").returncode == 0
    assert (wt / "AGENTS.md").read_text() == V2


def test_a_patch_prepared_without_an_applied_one_replaces_it(env, tmp_path):
    target, wt, art, base = _ticket_with_agents_md(env, tmp_path)
    _record(target, art, "v1.patch", base)
    assert cli("protected", "local-1", "apply", "1").returncode == 0
    first_sha = git("rev-parse", "HEAD", cwd=wt).strip()
    _record(target, art, "v2.patch", base, "06-implement")  # made from ORIGINAL, while #1 was not on the branch
    need = attention.needs(target, "local-1")
    assert need["key"] == "protected:2" and need["patch"]["replaces"] == [1]
    assert "#2\tpending (replaces applied #1)" in cli("protected", "local-1").stdout
    proc = cli("protected", "local-1", "apply", "2")
    assert proc.returncode == 0, proc.stderr
    assert (wt / "AGENTS.md").read_text() == V2  # ORIGINAL + patch 2, not V1 + anything
    assert git("log", "-1", "--format=%s", cwd=wt).strip() == "docs(local-1): apply approved change to AGENTS.md (supersedes #1)"
    assert git("rev-parse", "HEAD~1", cwd=wt).strip() == first_sha and git("status", "--porcelain", cwd=wt) == ""
    assert git("diff", "--name-only", "HEAD~1", "HEAD", cwd=wt).split() == ["AGENTS.md"]
    first, second = _state(env)["protected_patches"]
    assert first["status"] == "applied" and first["superseded_by"] == 2 and first["sha"] == first_sha
    assert second["status"] == "applied" and second["sha"] == git("rev-parse", "HEAD", cwd=wt).strip()
    assert "#1\tapplied " in (listed := cli("protected", "local-1").stdout) and "(superseded by #2)" in listed


def test_a_change_by_someone_else_since_refuses_the_replacement(env, tmp_path):
    target, wt, art, base = _ticket_with_agents_md(env, tmp_path)
    _record(target, art, "v1.patch", base)
    assert cli("protected", "local-1", "apply", "1").returncode == 0
    _record(target, art, "v2.patch", base, "06-implement")
    (wt / "AGENTS.md").write_text(V1 + "Mine.\n")
    git("commit", "-q", "-am", "docs: my line", cwd=wt)
    moved = git("rev-parse", "HEAD", cwd=wt).strip()
    proc = cli("protected", "local-1", "apply", "2")
    assert proc.returncode == 2
    assert ("AGENTS.md bu yama hazırlandıktan sonra başka bir değişiklikle değişti; yama uygulanamıyor. Reddet ve ajanın "
            "yeni bir yama hazırlamasını iste.") in proc.stderr
    assert git("rev-parse", "HEAD", cwd=wt).strip() == moved and git("status", "--porcelain", cwd=wt) == ""
    assert (wt / "AGENTS.md").read_text() == V1 + "Mine.\n"
    first, second = _state(env)["protected_patches"]
    assert second["status"] == "pending" and "superseded_by" not in first


@pytest.mark.parametrize("fallback", ["run.json", "applied parent"])
def test_a_patch_recorded_before_base_head_uses_the_fallback(env, tmp_path, fallback):
    target, wt, art, base = _ticket_with_agents_md(env, tmp_path)
    _record(target, art, "v1.patch", base)
    assert cli("protected", "local-1", "apply", "1").returncode == 0
    _record(target, art, "v2.patch", base, "99-implement")
    st = state.load_state(target, "local-1")
    del st["protected_patches"][1]["base_head"]  # recorded before base_head existed
    state.save_state(target, "local-1", st)
    if fallback == "run.json":  # the stage's HEAD at its end
        stage_dir = env["state"] / "runs" / "local-1" / "99-implement"
        stage_dir.mkdir()
        (stage_dir / "run.json").write_text(json.dumps({"head_after": base}))
    assert protected.base_head(target, "local-1", st, st["protected_patches"][1]) == (
        base if fallback == "run.json" else f"{st['protected_patches'][0]['sha']}^")
    proc = cli("protected", "local-1", "apply", "2")
    assert proc.returncode == 0, proc.stderr
    assert (wt / "AGENTS.md").read_text() == V2 and "(supersedes #1)" in git("log", "-1", "--format=%s", cwd=wt)
