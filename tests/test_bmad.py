"""BMad as a per-ticket choice: detection on the remote base, the refusal without BMad or its per-developer
files, the six files copied into a bmad ticket's worktree, and `commit_paths` committing BMad's records."""
import json

import pytest
from conftest import calls, cli, git, script

from ziggy import bmad, config, pipelines, ticket, worktree

IGNORED = ("_bmad/config.user.toml", "_bmad/core/config.yaml", "_bmad/bmm/config.yaml", "_bmad/_config/manifest.yaml",
           "_bmad/_config/files-manifest.csv")


@pytest.fixture(autouse=True)
def _fresh_cache():
    bmad._CACHE.clear()
    yield
    bmad._CACHE.clear()


def install(env, local=True, ignored=IGNORED):
    """BMad as a repository has it: the skill, _bmad/ and _bmad-output/ tracked and pushed to the base; the
    per-developer files gitignored (the custom one by _bmad/custom/.gitignore) and, with `local`, present."""
    r = env["repo"]
    (r / ".gitignore").write_text(".venv\n" + "".join(f"{p}\n" for p in ignored))
    (r / ".claude" / "skills" / "bmad-build-auto").mkdir(parents=True)
    (r / ".claude" / "skills" / "bmad-build-auto" / "SKILL.md").write_text("---\nname: bmad-build-auto\n---\n")
    (r / "_bmad" / "custom").mkdir(parents=True)
    (r / "_bmad" / "config.toml").write_text("[core]\n")
    (r / "_bmad" / "custom" / ".gitignore").write_text("*.user.toml\n")
    (r / "_bmad-output").mkdir()
    (r / "_bmad-output" / "README.md").write_text("BMad records\n")
    git("add", "-A", cwd=r)
    git("commit", "-q", "-m", "install bmad", cwd=r)
    git("push", "-q", "origin", "main", cwd=r)
    if local:
        install_local(env)


def test_detection_reads_the_remote_base_and_the_main_checkout(env):
    target = config.load_target("demo")
    found = bmad.status(target)
    assert found["installed"] is False and "origin/main has no" in found["reason"]
    install(env)
    assert bmad.status(target)["installed"] is False  # cached for a minute
    found = bmad.status(target, fresh=True)
    assert found == {"installed": True, "version": "6.12.0", "reason": found["reason"]}  # manifest is gitignored: main checkout
    assert bmad.local_ready(target) == []
    (env["repo"] / "_bmad" / "bmm" / "config.yaml").unlink()
    assert bmad.local_ready(target) == ["_bmad/bmm/config.yaml"]


def test_detection_ignores_bmad_only_in_the_working_tree(env):
    r = env["repo"]
    (r / ".claude" / "skills" / "bmad-build-auto").mkdir(parents=True)
    (r / ".claude" / "skills" / "bmad-build-auto" / "SKILL.md").write_text("x\n")
    (r / "_bmad").mkdir()
    (r / "_bmad" / "config.toml").write_text("x\n")
    assert bmad.status(config.load_target("demo"), fresh=True)["installed"] is False


def test_bmad_tickets_are_refused_without_bmad_or_its_local_files(env):
    proc = cli("new", "fix: login loops", "--pipeline", "bmad")
    assert proc.returncode != 0 and "BMad is not installed" in proc.stderr
    install(env, local=False)
    proc = cli("new", "fix: login loops", "--pipeline", "bmad")
    assert proc.returncode != 0 and "per-developer files are missing" in proc.stderr and "_bmad/core/config.yaml" in proc.stderr
    install_local(env)
    target = config.load_target("demo")
    with pytest.raises(ValueError, match="project ticket"):
        ticket.create(target, "Product doc", "project", "", False, "low", pipeline="bmad")
    with pytest.raises(ValueError, match="pipeline must be one of"):
        ticket.create(target, "x", "feat", "", False, "low", pipeline="other")
    assert cli("new", "fix: login loops", "--pipeline", "bmad").returncode == 0
    assert cli("new", "chore: tidy", "--pipeline", "standard").returncode == 0
    st = json.loads((env["state"] / "runs" / "local-1" / "state.json").read_text())
    assert st["pipeline"] == "bmad"
    assert "- Pipeline: bmad" in (env["state"] / "runs" / "local-1" / "ticket.md").read_text()
    assert json.loads((env["state"] / "runs" / "local-2" / "state.json").read_text())["pipeline"] == "chore"
    listing = cli("status").stdout
    assert listing.startswith("# demo: BMad 6.12.0 installed")


def install_local(env):
    for rel in bmad.LOCAL_FILES:
        path = env["repo"] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("installation:\n  version: 6.12.0\n" if rel == bmad.MANIFEST else f"# local {rel}\n")


def _records(env, ticket_id="local-1"):
    return [json.loads((d / "run.json").read_text()) for d in sorted((env["state"] / "runs" / ticket_id).glob("[0-9][0-9]-*"))]


def test_a_bmad_run_gets_the_local_files_and_commits_only_the_records(env):
    install(env)
    script(env, {
        "bmad-plan": [{"verdict": "READY", "planPath": "_bmad-output/spec.md", "risk": "low", "size": "small",
                       "_dirty": "_bmad-output/spec.md"}],
        "bmad-build": [{"verdict": "READY", "_commit": "feature.txt", "_dirty": "_bmad-output/build-log.md"}],
        "review": [{"verdict": "READY", "route": "converged", "converged": True}],
    })
    assert cli("new", "feat: weekly plan", "--pipeline", "bmad").returncode == 0
    proc = cli("run", "local-1")
    assert proc.returncode == 0, proc.stderr
    assert [c["stage"] for c in calls(env) if c.get("stage")] == ["bmad-plan", "bmad-build", "review"]
    plan_prompt, build_prompt = (c["prompt"] for c in calls(env) if c.get("stage") in ("bmad-plan", "bmad-build"))
    assert plan_prompt.splitlines()[:2] == [f"/bmad-build-auto {env['state'] / 'runs' / 'local-1' / 'inbox' / 'ticket.md'}",
                                            "Halt after planning."]
    assert build_prompt.splitlines()[0] == "/bmad-build-auto _bmad-output/spec.md"
    wt = env["state"] / "worktrees" / "local-1"
    for rel in bmad.LOCAL_FILES:
        assert (wt / rel).read_text() == (env["repo"] / rel).read_text()
    assert worktree.dirty_paths(wt) == []
    plan, build, review = _records(env)
    assert plan["state"] == "ok" and plan["records_commit"] and build["records_commit"] and "records_commit" not in review
    assert git("show", "--name-only", "--format=%s", plan["records_commit"], cwd=wt).split() == \
        ["docs(local-1):", "BMad", "bmad-plan", "records", "_bmad-output/spec.md"]
    assert git("show", "--name-only", "--format=", build["records_commit"], cwd=wt).split() == ["_bmad-output/build-log.md"]
    st = json.loads((env["state"] / "runs" / "local-1" / "state.json").read_text())
    assert st["status"] == "done" and [c["name"] for c in st["checks"]] == []


def test_the_plan_stage_still_may_not_touch_anything_else(env):
    install(env)
    script(env, {"bmad-plan": [{"verdict": "READY", "planPath": "_bmad-output/spec.md", "_dirty": "notes.txt"}]})
    cli("new", "feat: weekly plan", "--pipeline", "bmad")
    assert cli("run", "local-1").returncode == 1
    st = json.loads((env["state"] / "runs" / "local-1" / "state.json").read_text())
    assert st["status"] == "failed" and "must not change the repository" in st["error"] and "notes.txt" in st["error"]


def test_a_local_file_that_is_not_gitignored_is_never_copied_and_fails_the_run(env):
    install(env, ignored=IGNORED[1:])  # _bmad/config.user.toml is not ignored in this checkout
    cli("new", "feat: weekly plan", "--pipeline", "bmad")
    proc = cli("run", "local-1")
    assert proc.returncode != 0
    st = json.loads((env["state"] / "runs" / "local-1" / "state.json").read_text())
    assert st["status"] == "failed" and "_bmad/config.user.toml" in st["error"] and "per-developer files" in st["error"]
    assert not calls(env)
    wt = env["state"] / "worktrees" / "local-1"
    assert not (wt / "_bmad" / "config.user.toml").exists() and (wt / "_bmad" / "core" / "config.yaml").is_file()


def test_copies_never_overwrite_what_the_worktree_has(env):
    install(env)
    target = config.load_target("demo")
    t = ticket.create(target, "weekly plan", "feat", "", False, "low", pipeline="bmad")
    wt = env["state"] / "worktrees" / t["id"]
    worktree.create(target, t, wt)
    (wt / "_bmad" / "core").mkdir(parents=True)
    (wt / "_bmad" / "core" / "config.yaml").write_text("mine\n")
    copied = worktree.preserve(target, wt, bmad.LOCAL_FILES)
    assert "_bmad/core/config.yaml" not in copied and len(copied) == 5
    assert (wt / "_bmad" / "core" / "config.yaml").read_text() == "mine\n"


def test_commit_paths_are_validated():
    base = {"id": "s", "prompt": "implement.md", "result": "implement"}
    for bad in ("_bmad-output", ["../x"], ["/abs"], [""], [".git/hooks"]):
        with pytest.raises(pipelines.PipelineConfigError, match="commit_paths"):
            pipelines.parse("t", {"stages": [{**base, "commit_paths": bad}]})
    with pytest.raises(pipelines.PipelineConfigError, match="commit_message"):
        pipelines.parse("t", {"stages": [{**base, "commit_paths": ["out"], "commit_message": ""}]})
    p = pipelines.load("bmad")
    assert [s.id for s in p.stages] == ["bmad-plan", "bmad-build", "review"] and not p.warnings
    assert p.stage("bmad-plan").commit_paths == ("_bmad-output",) and p.ask_user_then == "bmad-plan"
    assert p.user_review_then == "bmad-build" and p.stage("review").routes == {"fix": "bmad-build", "converged": "done"}
