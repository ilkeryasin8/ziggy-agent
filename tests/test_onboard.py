"""Adding a target: what `detect` proposes for Python, Node and mixed repositories, how `add`
appends to the config (and restores it when the result does not load), the CLI and the panel."""
import json
import os
import tomllib

import pytest
from conftest import cli, git
from test_dashboard import panel, request  # noqa: F401 - the panel fixture

from ziggy import config, onboard, onboard_verify
from ziggy.config import ConfigError


def make_repo(root, files, remote="git@github.com:acme/shop.git"):
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(root), cwd=root.parent)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=root)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git("add", "-A", cwd=root)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    git("remote", "add", "origin", remote, cwd=root)
    return root


def checks(found):
    return {c["key"]: c["ok"] for c in found["checks"]}


def test_detect_python_and_node(env, tmp_path):
    repo = make_repo(tmp_path / "Shop", {
        "requirements.txt": "fastapi\n",
        "tests/test_app.py": "def test_x(): pass\n",
        "frontend/package-lock.json": "{}\n",
        "frontend/package.json": '{"scripts": {"test": "vitest run"}}\n',
        ".gitignore": ".venv/\n",
        "AGENTS.md": "rules\n",
        ".github/workflows/ci.yml": "jobs:\n  test:\n    env:\n      DB_URI: mongodb://127.0.0.1/test\n      TOKEN: ${{ secrets.X }}\n    steps:\n      - run: pytest\n",
        "node_modules/deep/package-lock.json": "{}\n",
    })
    found = onboard.detect(repo)
    spec = found["spec"]
    assert spec["name"] == "shop" and spec["github_repo"] == "acme/shop" and spec["base_branch"] == "main"
    assert any("-r requirements.txt" in c for c in spec["prepare"])
    assert spec["prepare"][-1] == "cd frontend && (npm ci --offline --no-audit --no-fund || npm ci --no-audit --no-fund)"
    assert len([c for c in spec["prepare"] if "npm ci" in c]) == 1  # node_modules is not searched
    assert spec["env"] == {"DB_URI": "mongodb://127.0.0.1/test"}
    c = checks(found)
    assert spec["checks"] == [{"name": "pytest", "run": ".venv/bin/python -m pytest -q"},
                              {"name": "frontend test", "run": "cd frontend && npm test"}]
    why = {c["name"]: c["why"] for c in found["proposal"]["checks"]}
    assert "tests/ folder" in why["pytest"] and "CI runs it too (.github/workflows/ci.yml:7)" in why["pytest"]
    assert c["project_doc"] is False and c["checks"] and c["rules"] and c["github"] and c["ignored:.venv"]
    assert c["ignored:frontend/node_modules"] is False  # prepare would dirty the tree
    assert found["can_add"]  # a missing project doc is reported, not blocking


def test_detect_gh_user_from_config_rules(env, tmp_path):
    work = make_repo(tmp_path / "Work" / "api", {"pyproject.toml": "[project]\nname='api'\n"})
    other = make_repo(tmp_path / "solo", {})
    env["config"].write_text(env["config"].read_text() + (
        '\n[onboard]\ndefault_gh_user = "me"\n'
        f'[onboard.gh_users]\n"{tmp_path / "work"}" = "me-at-work"\n'  # case-insensitive, like macOS
    ))
    assert onboard.detect(work)["spec"]["gh_user"] == "me-at-work"
    assert onboard.detect(work)["spec"]["prepare"][1].endswith("install --quiet -e . pytest")
    found = onboard.detect(other)
    assert found["spec"]["gh_user"] == "me" and found["spec"]["prepare"] == [] and checks(found)["prepare"] is False


def test_detect_flags_a_taken_name(env):
    found = onboard.detect(env["repo"])
    assert found["spec"]["name"] == "repo" and found["can_add"]
    env["config"].write_text(env["config"].read_text().replace("[targets.demo]", "[targets.repo]").replace("[targets.demo.env]", "[targets.repo.env]").replace('"demo"', '"repo"'))
    assert onboard.detect(env["repo"])["can_add"] is False


def test_add_appends_and_keeps_existing_lines(env, tmp_path):
    repo = make_repo(tmp_path / "shop", {"requirements.txt": "x\n"})
    before = env["config"].read_text()
    spec = onboard.detect(repo)["spec"] | {"gh_user": "me", "env": {"DB": 'say "hi"'}}
    backup = onboard.add(spec)
    after = env["config"].read_text()
    assert after.startswith(before) and backup.read_text() == before
    target = config.load_target("shop")
    assert target.github_repo == "acme/shop" and target.gh_user == "me" and target.env == {"DB": 'say "hi"'}
    assert target.prepare == tuple(spec["prepare"])
    assert config.target_names() == ["demo", "shop"]


def test_add_refuses_duplicates_and_restores_a_broken_result(env, tmp_path):
    before = env["config"].read_text()
    with pytest.raises(ConfigError, match="already exists"):
        onboard.add({"name": "demo", "path": str(env["repo"])})
    with pytest.raises(ConfigError, match="not a git checkout"):
        onboard.add({"name": "ghost", "path": str(tmp_path / "not-a-repo")})
    with pytest.raises(ConfigError, match="config restored"):  # writes, then fails to load
        onboard.add({"name": "ghost", "path": str(env["repo"]), "prepare": [1]})
    with pytest.raises(ConfigError, match="lowercase"):
        onboard.add({"name": "Bad Name", "path": str(env["repo"])})
    with pytest.raises(ConfigError, match="unknown target keys"):
        onboard.add({"name": "x", "path": str(env["repo"]), "token": "secret"})
    assert env["config"].read_text() == before


def test_cli_target_add(env, tmp_path):
    repo = make_repo(tmp_path / "shop", {"package-lock.json": "{}\n", ".gitignore": "node_modules/\n"})
    proc = cli("target", "add", str(repo), "--yes")
    assert proc.returncode == 0, proc.stderr
    assert "[targets.shop]" in proc.stdout and "-- " in proc.stdout and "project doc: docs/PROJECT.md (none)" in proc.stdout
    assert config.load_target("shop").prepare == ("(npm ci --offline --no-audit --no-fund || npm ci --no-audit --no-fund)",)
    proc = cli("target", "add", str(repo), "--yes")
    assert proc.returncode == 1 and "blocking" in proc.stderr
    proc = cli("target", "add", str(repo), stdin="n\n")
    assert proc.returncode == 1
    assert cli("target", "add", str(repo), "--name", "shop-2", stdin="y\n").returncode == 0
    assert "shop-2" in config.target_names()


def test_panel_detects_and_adds(panel, env, tmp_path):  # noqa: F811
    repo = make_repo(tmp_path / "shop", {})
    code, found, _ = request(panel, "POST", "/api/targets/detect", {"path": str(repo)})
    assert code == 200 and found["spec"]["name"] == "shop"
    assert request(panel, "POST", "/api/targets/detect", {"path": str(tmp_path)})[0] == 400
    code, out, _ = request(panel, "POST", "/api/targets", {"spec": found["spec"]})
    assert code == 200 and out["added"] == "shop"
    _, snap, _ = request(panel, "GET", "/api/status")
    assert [t["name"] for t in snap["targets"]] == ["demo", "shop"]
    assert request(panel, "POST", "/api/targets", {"spec": found["spec"]})[0] == 400


def _two_targets(env, tmp_path):
    """demo plus a second target `shop` with sub-tables, between comments and an unrelated table."""
    repo = tmp_path / "shop" if (tmp_path / "shop").exists() else make_repo(tmp_path / "shop", {})  # removing keeps the repository
    text = env["config"].read_text() + (
        "\n# shop: the store front\n"
        "[targets.shop]\n"
        f'path = "{repo}"\n'
        "\n[[targets.shop.checks]]\nname = \"unit\"\nrun = \"make test\"\n"
        "\n[targets.shop.env]\nK = \"v\"\n"
        "\n# memory settings stay\n"
        "[memory]\nretro = true\n")
    env["config"].write_text(text)
    return text


def test_remove_takes_out_only_the_targets_tables(env, tmp_path):
    original = env["config"].read_text()
    env["config"].write_text(original.replace("[targets.demo]", "\n\n[targets.demo]"))  # blank lines elsewhere stay
    before = _two_targets(env, tmp_path)
    backup = onboard.remove("shop")
    after = env["config"].read_text()
    assert backup.read_text() == before
    assert config.target_names() == ["demo"]
    assert after == original.replace("[targets.demo]", "\n\n[targets.demo]") + "\n# memory settings stay\n[memory]\nretro = true\n"
    # Last in the file: the table before it ends the file again.
    env["config"].write_text(before.split("\n# memory settings stay")[0] + "\n")
    onboard.remove("shop")
    assert env["config"].read_text() == original.replace("[targets.demo]", "\n\n[targets.demo]")


def test_remove_refuses_what_would_leave_the_config_pointing_at_it(env, tmp_path):
    text = _two_targets(env, tmp_path)
    with pytest.raises(ConfigError, match="no target named"):
        onboard.remove("ghost")
    with pytest.raises(ConfigError, match="default_target"):
        onboard.remove("demo")
    with pytest.raises(ConfigError, match=r"under way \(local-1\)"):
        onboard.remove("shop", busy=lambda target: ["local-1"])
    assert env["config"].read_text() == text
    env["config"].write_text(text + '\n[sources.youtrack.projects]\nSHOP = "shop"\n')
    with pytest.raises(ConfigError, match="maps SHOP to shop"):
        onboard.remove("shop")


def test_cli_and_panel_remove(panel, env, tmp_path):  # noqa: F811
    text = _two_targets(env, tmp_path)
    assert cli("target", "remove", "shop", stdin="n\n").returncode == 1
    assert config.target_names() == ["demo", "shop"]
    proc = cli("target", "remove", "shop", "--yes")
    assert proc.returncode == 0 and "removed shop" in proc.stdout, proc.stderr
    assert config.target_names() == ["demo"]
    env["config"].write_text(text)
    code, out, _ = request(panel, "POST", "/api/targets/shop/remove", {})
    assert code == 200 and out["removed"] == "shop"
    _, snap, _ = request(panel, "GET", "/api/status")
    assert [t["name"] for t in snap["targets"]] == ["demo"]
    code, out, _ = request(panel, "POST", "/api/targets/demo/remove", {})
    assert code == 400 and "default_target" in out["error"]


def test_leftovers_of_a_removed_target_and_purging_its_worktrees(env, tmp_path, panel):  # noqa: F811
    _two_targets(env, tmp_path)
    shop = config.load_target("shop")
    clean, dirty = shop.state_dir / "worktrees" / "local-1", shop.state_dir / "worktrees" / "local-2"
    for wt, branch in ((clean, "feat/one"), (dirty, "feat/two")):
        git("worktree", "add", "-q", "-b", branch, str(wt), cwd=tmp_path / "shop")
    (dirty / "scratch.txt").write_text("unsaved\n")
    (shop.state_dir / "runs").mkdir()
    assert onboard.leftovers() == []  # still configured: not a leftover
    onboard.remove("shop")

    [found] = onboard.leftovers()
    assert found["name"] == "shop" and [(t["name"], t["dirty"]) for t in found["worktrees"]] == [("local-1", False), ("local-2", True)]
    with pytest.raises(ConfigError, match="local-2 still hold uncommitted changes"):
        onboard.purge("shop")
    code, out, _ = request(panel, "POST", "/api/leftovers/shop/purge", {})
    assert code == 409 and "uncommitted" in out["error"]
    code, out, _ = request(panel, "POST", "/api/leftovers/shop/purge", {"force": True})
    assert code == 200 and out["removed"] == ["local-1", "local-2"] and not clean.exists() and not dirty.exists()
    assert (shop.state_dir / "runs").is_dir()  # tickets stay
    assert "feat/one" in git("branch", "--list", "feat/one", cwd=tmp_path / "shop")  # branches stay
    assert git("worktree", "list", cwd=tmp_path / "shop").count("\n") == 1  # pruned
    assert request(panel, "GET", "/api/leftovers")[1]["leftovers"][0]["worktrees"] == []
    with pytest.raises(ConfigError, match="no leftovers"):
        onboard.purge("demo")


def test_render_is_valid_toml_for_odd_values():
    text = onboard.render({"name": "x", "path": "/a b/c", "prepare": ['echo "q" \\ done'], "env": {"K": "ç\nline"}})
    data = tomllib.loads(text)["targets"]["x"]
    assert data["prepare"] == ['echo "q" \\ done'] and data["env"]["K"] == "ç\nline"


def test_proposed_checks_are_rendered_and_loaded(env, tmp_path):
    repo = make_repo(tmp_path / "svc", {"go.mod": "module svc\n", "docs/PROJECT.md": "# svc\n"})
    found = onboard.detect(repo)
    assert found["spec"]["checks"] == [{"name": "go test", "run": "go test ./..."}]
    assert {c["key"]: c["ok"] for c in found["checks"]}["project_doc"]
    text = onboard.render({**found["spec"], "checks": [*found["spec"]["checks"], {"name": "lint", "run": "make lint", "timeout": "5m"}]})
    assert tomllib.loads(text)["targets"]["svc"]["checks"][1] == {"name": "lint", "run": "make lint", "timeout": "5m"}
    onboard.add({**found["spec"], "gh_user": "me"})
    assert [c["run"] for c in config.load_target("svc").checks] == ["go test ./..."]


def test_detect_proposes_the_claude_login_by_path(env, tmp_path, monkeypatch):
    work = make_repo(tmp_path / "work" / "api", {})
    solo = make_repo(tmp_path / "solo", {})
    login = tmp_path / ".claude-work"
    env["config"].write_text(env["config"].read_text() + f'\n[onboard.claude_config_dirs]\n"{tmp_path / "Work"}" = "{login}"\n')
    fake = tmp_path / "fake-auth"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$CLAUDE_CONFIG_DIR" = "' + str(login) + '" ]; then\n'
        '  echo \'{"loggedIn": true, "email": "me@work.example", "orgName": "Work", "subscriptionType": "team"}\'\n'
        "else\n"
        '  echo \'{"loggedIn": true, "email": "me@home.example", "subscriptionType": "max"}\'\n'
        "fi\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("ZIGGY_CLAUDE", str(fake))

    found = onboard.detect(work)
    assert found["spec"]["env"]["CLAUDE_CONFIG_DIR"] == str(login)
    claude = next(c for c in found["checks"] if c["key"] == "claude")
    assert claude["ok"] and "me@work.example (Work, team)" in claude["message"]

    found = onboard.detect(solo)
    assert "CLAUDE_CONFIG_DIR" not in found["spec"]["env"]
    assert "me@home.example (max)" in next(c for c in found["checks"] if c["key"] == "claude")["message"]

    monkeypatch.setenv("ZIGGY_CLAUDE", str(tmp_path / "missing"))
    claude = next(c for c in onboard.detect(work)["checks"] if c["key"] == "claude")
    assert not claude["ok"] and not claude["blocking"] and f"CLAUDE_CONFIG_DIR={login} claude /login" in claude["fix"]


def test_pasted_paths_with_blanks_and_quotes_are_accepted(env, tmp_path):
    repo = make_repo(tmp_path / "shop", {})
    for pasted in (f"  {repo}  ", f"'{repo}'\n", f'"{repo}"'):
        assert onboard.detect(pasted)["spec"]["path"] == str(repo)
    with pytest.raises(ConfigError, match="path is required"):
        onboard.detect("   ")


def test_a_repository_without_origin_cannot_be_added(env, tmp_path):
    repo = make_repo(tmp_path / "local-only", {})
    git("remote", "remove", "origin", cwd=repo)
    found = onboard.detect(repo)
    remote = next(c for c in found["checks"] if c["key"] == "remote")
    assert remote["blocking"] and "gh repo create" in remote["fix"] and found["can_add"] is False
    before = env["config"].read_text()
    with pytest.raises(ConfigError, match="no origin remote"):
        onboard.add(found["spec"])
    assert env["config"].read_text() == before


def test_candidates_lists_checkouts_and_marks_targets(env, tmp_path, panel):  # noqa: F811
    root = tmp_path / "dev"
    make_repo(root / "alpha", {"sub/inner/.keep": ""})
    make_repo(root / "work" / "beta", {})
    make_repo(root / "_archive" / "old", {})
    (root / "work" / "beta" / "nested").mkdir()
    git("init", "-q", str(root / "work" / "beta" / "nested"), cwd=root)  # inside a checkout: skipped
    found = onboard.candidates(root)
    assert [c["name"] for c in found] == ["alpha", "work/beta"]
    assert all(c["remote"] and not c["project_doc"] and c["target"] is None for c in found)
    env["config"].write_text(env["config"].read_text().replace(str(env["repo"]), str(root / "alpha")))
    assert onboard.candidates(root)[0]["target"] == "demo"
    code, out, _ = request(panel, "GET", "/api/targets/candidates")
    assert code == 200 and isinstance(out["candidates"], list)


def test_base_branch_comes_from_the_remote_and_must_exist_there(env, tmp_path):
    bare = tmp_path / "shop.git"
    git("init", "-q", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    repo = make_repo(tmp_path / "shop", {"a.txt": "a\n"}, remote=str(bare))
    git("push", "-q", "origin", "main:refs/heads/trunk", cwd=repo)  # remote default: trunk, no main
    found = onboard.detect(repo)
    assert found["spec"]["base_branch"] == "trunk"
    assert next(c for c in found["checks"] if c["key"] == "base")["ok"]

    work = make_repo(tmp_path / "wardrobe", {"a.txt": "a\n"}, remote=str(tmp_path / "wardrobe.git"))
    git("init", "-q", "--bare", "-b", "feat/p0", str(tmp_path / "wardrobe.git"), cwd=tmp_path)
    git("push", "-q", "origin", "main:refs/heads/feat/p0", cwd=work)
    found = onboard.detect(work)
    base = next(c for c in found["checks"] if c["key"] == "base")
    assert found["spec"]["base_branch"] == "feat/p0" and not base["ok"] and not base["blocking"] and "work branch" in base["fix"]

    git("branch", "-m", "main", "other", cwd=work)  # no local main/master to guess from either
    assert onboard.detect(work)["spec"]["base_branch"] == "feat/p0"

    gone = make_repo(tmp_path / "gone", {})  # remote URL that cannot be reached: not checked, not blocking
    base = next(c for c in onboard.detect(gone)["checks"] if c["key"] == "base")
    assert not base["ok"] and not base["blocking"] and "not reachable" in base["message"]


def test_uv_projects_and_the_python_a_project_asks_for(env, tmp_path):
    uv = make_repo(tmp_path / "uvapp", {"pyproject.toml": "[project]\nname='a'\nrequires-python='>=3.13'\n", "uv.lock": "x\n", ".gitignore": ".venv/\n"})
    spec = onboard.detect(uv)["spec"]
    assert spec["prepare"] == [onboard.UV_PREPARE]
    assert spec["env"] == {"UV_OFFLINE": "1", "UV_NO_SYNC": "1", "UV_CACHE_DIR": "{worktree}/.venv/.uv-cache"}

    pinned = make_repo(tmp_path / "pinned", {"pyproject.toml": "[project]\nname='b'\nrequires-python='>=3.11'\n", ".python-version": "3.13.2\n"})
    assert onboard.python_version(pinned) == "3.13"
    assert "python3.13 -m venv" in onboard.detect(pinned)["spec"]["prepare"][0]
    ranged = make_repo(tmp_path / "ranged", {"pyproject.toml": "[project]\nname='c'\nrequires-python='>=3.11,<4'\n"})
    assert onboard.python_version(ranged) == "3.11"
    plain = make_repo(tmp_path / "plain", {"requirements.txt": "x\n"})
    assert onboard.python_version(plain) == "3.12"
    assert onboard.render({"name": "u", "path": "/p", "env": spec["env"]}).count("UV_") == 3


SIMCTL = {"devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-18-5": [{"name": "iPhone 16", "isAvailable": True}],
    "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
        {"name": "iPhone 17 Pro Max", "isAvailable": True}, {"name": "iPhone 17", "isAvailable": True},
        {"name": "iPhone 16e", "isAvailable": True}, {"name": "iPad Pro 13-inch (M5)", "isAvailable": True}],
    "com.apple.CoreSimulator.SimRuntime.watchOS-26-0": [{"name": "Apple Watch Series 11", "isAvailable": True}]}}


def _xcode_tools(tmp_path, monkeypatch, schemes=("PantryCore", "Pantry")):
    """Fake xcrun and xcodebuild first on PATH: their -list / simctl answers, and a log of their argv."""
    bin_ = tmp_path / "fakebin"
    bin_.mkdir()
    log = tmp_path / "xcode-tools.log"
    answers = {"xcrun": json.dumps(SIMCTL), "xcodebuild": json.dumps({"project": {"name": "Pantry", "schemes": list(schemes)}})}
    for tool, answer in answers.items():
        path = bin_ / tool
        path.write_text(f"#!/bin/sh\necho \"{tool} $*\" >> '{log}'\ncat <<'JSON'\n{answer}\nJSON\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return log


def test_an_xcode_project_proposes_the_preset_and_its_checks(env, tmp_path, monkeypatch):
    log = _xcode_tools(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "Pantry", {
        "Pantry.xcodeproj/project.pbxproj": "// !$*UTF8*$!\n",
        "PantryCore/Package.swift": "// swift-tools-version: 6.0\n",
        "PantryCore/Sources/Domain/Meal.swift": "struct Meal {}\n",
        "PantryCore/Tests/DomainTests/MealTests.swift": "import Testing\n",
        "Tools/Package.swift": "// a package without tests\n",
        "AGENTS.md": "rules\n",
    })
    found = onboard.detect(repo)
    proposal, spec = found["proposal"], found["spec"]
    assert proposal["check_sandbox"] == ["xcode"] and spec["check_sandbox"] == ["xcode"]
    assert spec["checks"] == [
        {"name": "swift test PantryCore",
         "run": "swift test --disable-sandbox --package-path PantryCore --scratch-path .build/PantryCore"},
        {"name": "xcodebuild test", "timeout": "30m",
         "run": "xcodebuild test -project Pantry.xcodeproj -scheme Pantry -destination 'platform=iOS Simulator,name=iPhone 17' "
                "-derivedDataPath .build/DerivedData -IDEPackageSupportDisableManifestSandbox=YES "
                "-IDEPackageSupportDisablePluginExecutionSandbox=YES 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' -quiet"}]
    assert "xcodebuild -list -json -project" in log.read_text() and "xcrun simctl list devices available -j" in log.read_text()
    c = checks(found)
    assert c["ignored:.build"] is False and "tool:xcodebuild" in c  # .build/ must be ignored, or checks rebuild each time
    assert any('check_sandbox = ["xcode"]' in n for n in proposal["notes"])
    # The table carries the preset and the timeout, and loads.
    text = onboard.render({**spec, "gh_user": "me"})
    table = tomllib.loads(text)["targets"]["pantry"]
    assert table["check_sandbox"] == ["xcode"] and table["checks"][1]["timeout"] == "30m"
    onboard.add({**spec, "gh_user": "me"})
    loaded = config.load_target("pantry")
    assert loaded.check_sandbox == ("xcode",) and loaded.checks[1]["timeout_s"] == 1800
    # The panel's form sends the lines back: the preset and the timeout survive, an unknown preset does not pass.
    cleaned = onboard.clean_proposal(json.loads(json.dumps(proposal)))
    assert cleaned["check_sandbox"] == ["xcode"] and cleaned["checks"][1]["timeout"] == "30m"
    with pytest.raises(ValueError):
        onboard.clean_proposal({**proposal, "check_sandbox": ["docker"]})


def test_verification_runs_the_checks_with_the_preset(env, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(onboard_verify.engine_checks, "run",
                        lambda target, *a, **k: seen.append(target.check_sandbox) or [{"status": "pass", "duration_ms": 5}])
    repo = make_repo(tmp_path / "App", {"README.md": "x\n"})
    proposal = {"prepare": [], "env": [], "notes": [], "agents_md": "", "check_sandbox": ["xcode"],
                "checks": [{"name": "swift test", "run": "true", "timeout": "30m"}]}
    out, summary = onboard_verify.verify(repo, proposal, "r-1")
    assert summary["ok"] and seen == [("xcode",)]


def test_newest_iphone_and_no_simulator(env, tmp_path, monkeypatch):
    assert onboard.newest_iphone(SIMCTL) == "iPhone 17"
    assert onboard.newest_iphone({"devices": {}}) is None
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no xcrun or xcodebuild of ours (and none on Linux)
    monkeypatch.setattr(onboard.shutil, "which", lambda name: None)
    repo = make_repo(tmp_path / "Solo", {"Solo.xcodeproj/project.pbxproj": "x\n", "Package.swift": "// root\n",
                                         "Tests/SoloTests/A.swift": "x\n"})
    found = onboard.detect(repo)
    assert [c["run"] for c in found["spec"]["checks"]] == [
        "swift test --disable-sandbox --package-path . --scratch-path .build/Solo"]
    assert any("no available iPhone simulator" in n for n in found["proposal"]["notes"])
    assert found["spec"]["check_sandbox"] == ["xcode"]
    # A repository without Xcode or Swift gets no preset.
    plain = onboard.detect(make_repo(tmp_path / "plain", {"go.mod": "module p\n"}))
    assert plain["spec"]["check_sandbox"] == [] and "check_sandbox" not in onboard.render(plain["spec"])
