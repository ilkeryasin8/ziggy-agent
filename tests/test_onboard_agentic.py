"""Agentic onboarding: what detection proposes for a yarn v1 repository with git-ignored inputs (and that its registry
token never shows anywhere), check rewriting, the CI parse, env suggestions, {checkout}, verification in a throwaway
clone, the agent's answer and the repair round (fake claude), the AGENTS.md draft PR (fake gh) and the panel/CLI."""
import json
import os
import time
from pathlib import Path

import pytest
from conftest import cli, git
from test_dashboard import panel, request  # noqa: F401 - the panel fixture

from ziggy import checks, config, dashboard, onboard, onboard_agent, onboard_verify, results, worktree
from ziggy.config import Target

TOKEN = "ghp_FAKEtoken0123456789abcdefXYZ"
REAL_URL = "https://real-backend.internal.example"
HAS_SANDBOX = checks.available()
needs_sandbox = pytest.mark.skipif(not HAS_SANDBOX, reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")

PACKAGE = {
    "name": "field", "private": True,
    "scripts": {"dev": "next dev -H 0.0.0.0", "build": "next build", "start": "next start",
                "lint": 'eslint --fix "src/**/*.{js,jsx}"', "format": 'prettier --write "src/**/*.{js,jsx}"'},
    "dependencies": {"@example-org/ui": "0.2.3", "@acme/orbit": "file:./.vendor/orbit-1.0.8.tgz", "next": "15.2.3"},
    "devDependencies": {"eslint": "^8.36.0", "prettier": "^2.8.4"},
}


def make_repo(root: Path, files: dict, ignored: dict | None = None, origin: Path | None = None, force: tuple = ()) -> Path:
    """A checkout with `files` committed (`force`: committed although ignored) and `ignored` left on disk only."""
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(root), cwd=root.parent)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=root)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git("add", "-A", cwd=root)
    for rel in force:
        git("add", "-f", rel, cwd=root)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    for rel, text in (ignored or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if origin is not None:
        git("init", "-q", "--bare", "-b", "main", str(origin), cwd=root.parent)
        git("remote", "add", "origin", str(origin), cwd=root)
        git("push", "-q", "origin", "main", cwd=root)
    else:
        git("remote", "add", "origin", "git@github.com:acme/field.git", cwd=root)
    return root


def field_repo(tmp_path: Path, origin: bool = False) -> Path:
    """The shape of a yarn v1 Next.js app with a vendored tarball and a private registry, both git-ignored."""
    return make_repo(tmp_path / "field", {
        "package.json": json.dumps(PACKAGE, indent=2),
        "yarn.lock": "# yarn lockfile v1\n",
        ".gitignore": "/node_modules\n/.next/\n.env*\n.npmrc\n.vendor/\n",
        ".env.example": "NEXT_PUBLIC_API_BASE_URL=https://api.example.com\n",
        "src/api.js": "export const base = process.env.NEXT_PUBLIC_API_BASE_URL;\nif (process.env.NODE_ENV === 'test') {}\n",
    }, ignored={
        ".vendor/orbit-1.0.8.tgz": "tarball\n",
        ".npmrc": f"@example-org:registry=https://npm.pkg.github.com\n//npm.pkg.github.com/:_authToken={TOKEN}\n",
        ".env": f"NEXT_PUBLIC_API_BASE_URL={REAL_URL}\nONLY_IN_DOTENV=hush-hush-value\n",
    }, origin=tmp_path / "field.git" if origin else None, force=(".env.example",))


def everything_under(*roots: Path) -> str:
    text = []
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                text.append(path.read_text(errors="replace"))
    return "\n".join(text)


# --- detection ---------------------------------------------------------------------------------------------------

def test_yarn_v1_with_ignored_vendor_and_npmrc(env, tmp_path):
    repo = field_repo(tmp_path)
    found = onboard.detect(repo)
    p = found["proposal"]
    assert [x["run"] for x in p["prepare"]] == [
        'mkdir -p .vendor && cp -R "{checkout}/.vendor/." .vendor/',
        'cp "{checkout}/.npmrc" .npmrc && yarn install --frozen-lockfile --non-interactive; s=$?; rm -f .npmrc; exit $s',
    ]
    assert "@acme/orbit is file:./.vendor/orbit-1.0.8.tgz" in p["prepare"][0]["why"]
    assert "yarn v1" in p["prepare"][1]["why"] and "@example-org" in p["prepare"][1]["why"]
    assert {c["name"]: c["run"] for c in p["checks"]} == {"lint": 'node_modules/.bin/eslint "src/**/*.{js,jsx}"',
                                                           "build": "node_modules/.bin/next build"}
    assert "without --fix" in p["checks"][0]["why"]
    env_ = {e["key"]: e for e in p["env"]}
    assert env_["NEXT_TELEMETRY_DISABLED"]["value"] == "1" and not env_["NEXT_TELEMETRY_DISABLED"]["suggested"]
    api = env_["NEXT_PUBLIC_API_BASE_URL"]
    assert api["value"] == "http://127.0.0.1:9" and api["suggested"] and "src/api.js:1" in api["why"] and ".env.example" in api["why"]
    assert "NODE_ENV" not in env_ and "ONLY_IN_DOTENV" not in env_  # real .env files are never read
    assert found["spec"]["prepare"] == [x["run"] for x in p["prepare"]]
    out = json.dumps(found)
    assert TOKEN not in out and REAL_URL not in out and "hush-hush" not in out
    assert "{checkout}" in onboard.render(found["spec"])
    assert {c["key"]: c["ok"] for c in found["checks"]}["ignored:node_modules"]


def test_an_npmrc_no_dependency_needs_is_not_copied(env, tmp_path):
    repo = make_repo(tmp_path / "app", {"package.json": json.dumps({"dependencies": {"left-pad": "1.0.0"}}), "package-lock.json": "{}\n",
                                         ".gitignore": "node_modules/\n.npmrc\n"},
                     ignored={".npmrc": f"@other:registry=https://npm.pkg.github.com\n//x/:_authToken={TOKEN}\n"})
    p = onboard.detect(repo)["proposal"]
    assert p["prepare"][0]["run"] == "(npm ci --offline --no-audit --no-fund || npm ci --no-audit --no-fund)"
    assert any(".npmrc is git-ignored and not copied" in n for n in p["notes"])
    assert TOKEN not in json.dumps(p)


@pytest.mark.parametrize("lock,extra,command", [
    ("pnpm-lock.yaml", {}, "pnpm install --frozen-lockfile"),
    ("bun.lockb", {}, "bun install --frozen-lockfile"),
    ("yarn.lock", {".yarnrc.yml": "nodeLinker: node-modules\n"}, "yarn install --immutable"),
])
def test_package_managers_by_lockfile(env, tmp_path, lock, extra, command):
    repo = make_repo(tmp_path / "app", {"package.json": "{}", lock: "x\n", ".gitignore": "node_modules/\n", **extra})
    assert onboard.detect(repo)["proposal"]["prepare"][0]["run"] == command


def test_go_cargo_poetry_and_one_app_folder(env, tmp_path):
    go = make_repo(tmp_path / "svc", {"go.mod": "module svc\n"})
    assert onboard.detect(go)["spec"]["prepare"] == ["go mod download"]
    rust = make_repo(tmp_path / "crate", {"Cargo.toml": "[package]\n", "Cargo.lock": "x\n"})
    assert onboard.detect(rust)["spec"]["prepare"] == ["cargo fetch"]
    poetry = make_repo(tmp_path / "py", {"pyproject.toml": "[tool.poetry]\nname='a'\n[tool.pytest.ini_options]\n", "poetry.lock": "x\n"})
    spec = onboard.detect(poetry)["spec"]
    assert spec["prepare"] == [onboard.POETRY_PREPARE] and spec["checks"] == [{"name": "pytest", "run": ".venv/bin/python -m pytest -q"}]
    mono = make_repo(tmp_path / "mono", {"web/package.json": "{}", "web/yarn.lock": "x\n", "api/package.json": "{}", "api/package-lock.json": "{}"})
    found = onboard.detect(mono)["proposal"]
    assert found["prepare"] == [] and any("several folders (api, web)" in n for n in found["notes"])
    one = make_repo(tmp_path / "one", {"web/package.json": json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {"vitest": "1"}}),
                                        "web/pnpm-lock.yaml": "x\n"})
    found = onboard.detect(one)["proposal"]
    assert found["prepare"][0]["run"] == "cd web && pnpm install --frozen-lockfile"
    assert found["checks"] == [{"name": "web test", "run": "cd web && node_modules/.bin/vitest run", "source": "detect",
                                "why": "web/package.json script `test`: `vitest` (vitest run (no watch): a check must not change files or wait)"}]


def test_check_scripts_are_rewritten_or_dropped():
    deps = {"eslint": "1", "prettier": "1", "typescript": "1", "vitest": "1", "next": "1"}
    assert onboard._rewrite('eslint --fix "src/**/*.js"', deps) == ('node_modules/.bin/eslint "src/**/*.js"', ["without --fix"], None)
    assert onboard._rewrite("prettier --write .", deps)[0] == "node_modules/.bin/prettier --check ."
    assert onboard._rewrite("prettier -w src", deps)[0] == "node_modules/.bin/prettier --check src"
    assert onboard._rewrite("tsc --noEmit -w", deps)[0] == "node_modules/.bin/tsc --noEmit"
    assert onboard._rewrite("vitest watch", deps)[0] == "node_modules/.bin/vitest run"
    assert onboard._rewrite("NODE_OPTIONS=--max-old-space-size=4096 next build", deps)[0] == \
        "NODE_OPTIONS=--max-old-space-size=4096 node_modules/.bin/next build"
    assert onboard._rewrite("next dev", deps)[2] == "starts a server or a watcher"
    assert onboard._rewrite("next start", deps)[2] == "starts a server or a watcher"
    assert "compound" in onboard._rewrite("eslint --fix . && prettier --write .", deps)[2]
    assert onboard._rewrite("node scripts/check.js && jest", deps) == ("node scripts/check.js && jest", [], None)
    assert onboard._rewrite("jest --ci", {}) == (None, [], None)  # not a dependency: the package manager runs it
    assert onboard._rewrite("ruff check --fix .", {}, script=False)[0] == "ruff check ."


def test_ci_workflow_parse(env, tmp_path):
    workflow = """name: ci
on: push
defaults:
  run:
    working-directory: app
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - name: lint
        run: npm run lint
      - name: unit
        run: |
          npm run test:unit
          echo done
      - name: types
        working-directory: app
        run: "npx tsc --noEmit"
      - run: eslint . --fix
      - run: pytest -q ${{ matrix.flags }}
"""
    runs = onboard.ci_runs(make_repo(tmp_path / "parse", {".github/workflows/ci.yml": workflow}))
    assert [(r["run"], r["wd"], r["line"]) for r in runs] == [
        ("npm ci", "app", 11), ("npm run lint", "app", 13), ("npm run test:unit", "app", 16), ("echo done", "app", 17),
        ("npx tsc --noEmit", "app", 20), ("eslint . --fix", "app", 21), ("pytest -q ${{ matrix.flags }}", "app", 22)]

    pkg = {"scripts": {"lint": "eslint src", "test:unit": "jest --ci"}, "devDependencies": {"eslint": "8", "jest": "29"}}
    repo = make_repo(tmp_path / "ci", {".github/workflows/ci.yml": workflow, "app/package.json": json.dumps(pkg), "app/package-lock.json": "{}"})
    found = onboard.detect(repo)["proposal"]
    by_name = {c["name"]: c for c in found["checks"]}
    assert by_name["app lint"]["run"] == "cd app && node_modules/.bin/eslint src"
    assert "CI runs it too (.github/workflows/ci.yml:13)" in by_name["app lint"]["why"]
    assert by_name["ci test unit"]["run"] == "cd app && node_modules/.bin/jest --ci"
    assert by_name["ci npx tsc"]["run"] == "cd app && npx tsc --noEmit"
    assert by_name["ci eslint"]["run"] == "cd app && node_modules/.bin/eslint ." and "without --fix" in by_name["ci eslint"]["why"]
    assert not any("${{" in c["run"] for c in found["checks"])


def test_env_suggestions_from_code_and_examples(env, tmp_path):
    repo = make_repo(tmp_path / "svc", {
        "requirements.txt": "fastapi\nonnxruntime==1.18\n",
        "app/settings.py": "from pydantic_settings import BaseSettings\nfrom pydantic import Field\n"
                           "class Settings(BaseSettings):\n    db: str = Field(validation_alias='MONGO_URI')\n",
        "app/main.py": "import os\nREDIS = os.environ['REDIS_URL']\nPORT = os.getenv('APP_PORT')\nX = os.environ.get('API_TOKEN')\n",
        "web/main.ts": "const u = import.meta.env.VITE_WS_URL; const m = import.meta.env.MODE;\n",
        ".env.sample": "export SENTRY_DSN=https://abc@sentry.example/1\n",
    }, ignored={".env": "API_TOKEN=real-secret-token-123\nSECRET_ONLY_HERE=x\n"})
    found = onboard.detect(repo)["proposal"]
    env_ = {e["key"]: e["value"] for e in found["env"]}
    assert env_ == {"ORT_DISABLE_TELEMETRY": "1", "API_TOKEN": "test", "APP_PORT": "9", "MONGO_URI": "mongodb://127.0.0.1:27017/test",
                    "REDIS_URL": "redis://127.0.0.1:6379/0", "SENTRY_DSN": "test", "VITE_WS_URL": "ws://127.0.0.1:9"}
    assert all(e["suggested"] for e in found["env"] if e["key"] != "ORT_DISABLE_TELEMETRY")
    assert "real-secret-token" not in json.dumps(found)


def test_checkout_placeholder_is_expanded(env, tmp_path):
    target = config.load_target("demo")
    target = Target(**{**target.__dict__, "env": {"FROM": "{checkout}/x"}, "prepare": ("echo {checkout} > from-checkout.txt",)})
    wt = tmp_path / "wt"
    wt.mkdir()
    environ, values = worktree.target_env(target, "local-1", wt)
    assert values["checkout"] == str(env["repo"]) and environ["FROM"] == f"{env['repo']}/x"
    worktree.run_prepare(target, "local-1", wt, tmp_path / "prepare.log")
    assert (wt / "from-checkout.txt").read_text().strip() == str(env["repo"])


def test_results_schema_is_not_a_stage_schema():
    assert "onboard" not in results.NAMES
    out, errors = results.normalize("onboard", {"verdict": "READY", "prepare": [{"run": "a\nb", "why": "x"}, {"run": "make deps", "why": "Makefile"}],
                                                "env": [{"key": "K", "value": "", "why": ""}], "checks": [{"name": "", "run": "x"}], "notes": ["n"]})
    assert not errors and out["prepare"] == [{"run": "make deps", "why": "Makefile"}] and out["env"] == [{"key": "K", "value": "", "why": ""}]
    assert out["checks"] == [] and out["agents_md"] == ""


# --- verification ------------------------------------------------------------------------------------------------

def proposal_with(prepare=(), checks_=(), env_=()):
    return {"prepare": [{"run": r, "why": "", "source": "detect"} for r in prepare],
            "checks": [{"name": n, "run": r, "why": "", "source": "detect", **({"timeout": t} if t else {})} for n, r, t in checks_],
            "env": [{"key": k, "value": v, "why": "", "suggested": False, "source": "detect"} for k, v in env_], "notes": [], "agents_md": ""}


@needs_sandbox
def test_verify_runs_prepare_and_checks_in_a_clone(env, tmp_path):
    repo = field_repo(tmp_path)
    proposal = proposal_with(
        prepare=['mkdir -p .vendor && cp -R "{checkout}/.vendor/." .vendor/',
                 'cp "{checkout}/.npmrc" .npmrc && cat .npmrc && test -f .vendor/orbit-1.0.8.tgz; s=$?; rm -f .npmrc; exit $s'],
        checks_=[("ok", 'test "$FLAG" = on && test -f .vendor/orbit-1.0.8.tgz', None), ("fails", "echo broken thing; exit 3", None),
                 ("stray", "touch stray.txt", None), ("tracked", "echo x >> package.json", None), ("slow", "sleep 5", "1s")],
        env_=[("FLAG", "on")])
    secrets = onboard_agent.secret_values(repo)
    out, summary = onboard_verify.verify(repo, proposal, "20261016-000000-aaaaaa", secrets)
    badges = {x.get("name") or x["run"][:5]: x["verify"] for x in out["prepare"] + out["checks"]}
    assert badges["mkdir"]["status"] == "pass" and badges["cp \"{"]["status"] == "pass"
    assert TOKEN not in badges["cp \"{"]["tail"] and "***" in badges["cp \"{"]["tail"]
    assert badges["ok"]["status"] == "pass"
    assert badges["fails"]["status"] == "fail" and badges["fails"]["reason"] == "exit 3: broken thing"
    assert badges["stray"]["status"] == "pass" and badges["stray"]["left_files"] == ["stray.txt"]
    assert badges["tracked"]["status"] == "fail" and "modified tracked files: package.json" in badges["tracked"]["reason"]
    assert badges["slow"]["status"] == "fail" and badges["slow"]["reason"].startswith("timed out after 1s")
    assert out["env"][0]["verify"]["status"] == "untried"  # not every check passed with it
    assert not summary["ok"] and [f["line"].split(":")[0] for f in summary["failures"]] == ["fails", "tracked", "slow"]
    assert onboard_verify.badge(badges["ok"]).startswith("denendi · ") and onboard_verify.badge(badges["fails"]) == "geçmedi: exit 3: broken thing"
    base = Path(summary["log"]).parent
    assert not (base / "clone").exists() and Path(summary["log"]).is_file()
    assert TOKEN not in Path(summary["log"]).read_text()
    assert git("status", "--porcelain", cwd=repo) == ""  # the checkout is untouched


@needs_sandbox
def test_verify_stops_at_a_failed_prepare_and_on_a_dirty_tree(env, tmp_path):
    repo = make_repo(tmp_path / "app", {"a.txt": "a\n"})
    out, summary = onboard_verify.verify(repo, proposal_with(prepare=["false", "true"], checks_=[("c", "true", None)]), "20261016-000000-bbbbbb")
    assert [x["verify"]["status"] for x in out["prepare"]] == ["fail", "untried"] and out["checks"][0]["verify"]["status"] == "untried"
    assert onboard_verify.badge(out["checks"][0]["verify"]) == "denenmedi"
    out, summary = onboard_verify.verify(repo, proposal_with(prepare=["mkdir -p build && touch build/x"], checks_=[("c", "true", None)]),
                                         "20261016-000000-cccccc")
    assert out["checks"][0]["verify"]["status"] == "fail" and "prepare left the tree dirty: build/x" in out["checks"][0]["verify"]["reason"]
    out, summary = onboard_verify.verify(repo, proposal_with(prepare=["sleep 5"]), "20261016-000000-dddddd", prepare_timeout=1)
    assert out["prepare"][0]["verify"]["reason"].startswith("timed out after 1s")
    assert not (Path(summary["log"]).parent / "clone").exists()


# --- the agent -----------------------------------------------------------------------------------------------------

def answer(**fields):
    return {"verdict": "READY", "summary": "ok", "_structured": True, "prepare": [], "env": [], "checks": [], "notes": [], "agents_md": "", **fields}


def onboard_calls(env):
    return [c for c in (json.loads(line) for line in env["log"].read_text().splitlines()) if c.get("stage", "").startswith("onboard")] \
        if env["log"].exists() else []


@needs_sandbox
def test_agent_fills_gaps_then_a_repair_round(env, tmp_path):
    repo = field_repo(tmp_path)
    vendor, install = ('mkdir -p .vendor && cp -R "{checkout}/.vendor/." .vendor/', 'cp "{checkout}/.npmrc" .npmrc && true; s=$?; rm -f .npmrc; exit $s')
    first = answer(prepare=[{"run": vendor, "why": "vendored tarball"}, {"run": install, "why": "install with the registry file"}],
                   env=[{"key": "NEXT_TELEMETRY_DISABLED", "value": "1", "why": "next"},
                        {"key": "NEXT_PUBLIC_API_BASE_URL", "value": REAL_URL, "why": "copied from .env (must be dropped)"}],
                   checks=[{"name": "lint", "run": "test -f .vendor/orbit-1.0.8.tgz", "why": "eslint config"},
                           {"name": "unit", "run": "exit 2", "why": "jest"}],
                   notes=["start the API by hand for e2e"], agents_md="# field\n\nNext.js app. Lint: `eslint src`.\n")
    repair = answer(prepare=first["prepare"], env=first["env"][:1],
                    checks=[first["checks"][0], {"name": "unit", "run": "true", "why": "jest, fixed"}], notes=["unit fixed"])
    env["results"].write_text(json.dumps({"onboard": [first], "onboard-repair": [repair]}))
    out = onboard_agent.suggest(repo, agent=True)
    p = out["proposal"]
    assert out["error"] is None and len(out["sessions"]) == 2 and out["sessions"][1]["repair"] and out["cost_usd"] == 2.5
    assert {c["name"]: (c["run"], c["source"], c["verify"]["status"]) for c in p["checks"]} == {
        "lint": ("test -f .vendor/orbit-1.0.8.tgz", "agent", "pass"), "unit": ("true", "agent", "pass")}
    assert [x["verify"]["status"] for x in p["prepare"]] == ["pass", "pass"] and p["prepare"][0]["source"] == "detect"
    assert [e["key"] for e in p["env"]] == ["NEXT_TELEMETRY_DISABLED"] and p["env"][0]["verify"]["status"] == "pass"
    assert any("value from a real .env/.npmrc file" in n for n in p["notes"]) and "unit fixed" in p["notes"]
    assert p["agents_md"].startswith("# field") and out["summary"]["ok"]

    calls = onboard_calls(env)
    assert [c["stage"] for c in calls] == ["onboard", "onboard-repair"]
    argv = calls[0]["argv"]
    assert calls[0]["cwd"] == str(repo.resolve()) and argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-budget-usd") + 1] == "1"
    denied = argv[argv.index("--disallowedTools") + 1: argv.index("--session-id")]
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "Read(**/.env)", "Read(**/.npmrc)"} <= set(denied)
    assert not {"Read", "Grep", "Glob"} & set(denied)
    assert "--json-schema" in argv and calls[0]["push_exit"] != 0  # the pre-push hook is in place for the session
    assert "exit 2" in calls[1]["prompt"] and "It was tried, and this failed" in calls[1]["prompt"]
    assert TOKEN not in calls[0]["prompt"] and REAL_URL not in calls[0]["prompt"]
    state_dir = onboard_verify.root() / out["request"]
    assert TOKEN not in everything_under(state_dir) and REAL_URL not in everything_under(state_dir)

    stats = dashboard.stats()
    onboard_stage = next(s for s in stats["by_stage"] if s["name"] == "onboard")
    assert (onboard_stage["cost"], onboard_stage["sessions"]) == (2.5, 2)
    assert any(t["name"] == "field" for t in stats["by_target"]) and stats["totals"]["tickets"] == 0


def test_agent_model_login_and_a_failed_session(env, tmp_path):
    repo = make_repo(tmp_path / "work" / "api", {"AGENTS.md": "rules\n"})
    login = tmp_path / ".claude-work"
    env["config"].write_text(env["config"].read_text() + f'\n[onboard]\nonboard_model = "haiku"\n[onboard.claude_config_dirs]\n"{tmp_path / "work"}" = "{login}"\n')
    env["results"].write_text(json.dumps({"onboard": [{"_no_result": True}]}))
    out = onboard_agent.suggest(repo, agent=True)
    assert out["sessions"][0]["state"] == "failed" and any("ended without an answer" in n for n in out["proposal"]["notes"])
    assert len(out["sessions"]) == 1  # no repair round without an answer
    call = onboard_calls(env)[0]
    assert call["argv"][call["argv"].index("--model") + 1] == "haiku"
    record = json.loads((Path(out["sessions"][0]["dir"]) / "run.json").read_text())
    assert record["stage"] == "onboard" and record["target"] == "api"
    assert onboard.company(repo) and not onboard.company(env["repo"])


def test_merge_keeps_sources_and_never_takes_an_existing_rules_draft():
    current = proposal_with(prepare=["npm ci"], checks_=[("lint", "eslint .", None)], env_=[("A", "1")])
    out = onboard_agent.merge(current, {"prepare": [{"run": "npm ci", "why": "same"}, {"run": "make gen", "why": "codegen"}], "env": [],
                                        "checks": [{"name": "lint", "run": "eslint src", "why": "src only"}], "notes": ["x"],
                                        "agents_md": "# draft"}, has_rules=True)
    assert [(p["run"], p["source"]) for p in out["prepare"]] == [("npm ci", "detect"), ("make gen", "agent")]
    assert out["env"] == current["env"] and out["checks"][0]["source"] == "agent" and out["agents_md"] == "" and out["notes"] == ["x"]


# --- AGENTS.md as a draft PR ---------------------------------------------------------------------------------------

def test_agents_md_draft_pr(env, tmp_path, monkeypatch, panel):  # noqa: F811
    repo = field_repo(tmp_path, origin=True)
    monkeypatch.setenv("FAKE_GH_ORIGIN", str(tmp_path / "field.git"))
    head = git("rev-parse", "HEAD", cwd=repo)
    body = {"path": str(repo), "text": "# field\n\nRun `yarn lint`.", "gh_user": "tester", "github_repo": "acme/field", "base_branch": "main"}
    monkeypatch.setenv("ZIGGY_GH", "/usr/bin/false")  # gh fails after the push: the branch goes again
    code, out, _ = request(panel, "POST", "/api/onboard/agents-md", body)
    assert code == 400 and "no gh token" in out["error"]
    assert git("--git-dir", str(tmp_path / "field.git"), "branch", "--list", "docs/agents-md", cwd=tmp_path) == ""
    monkeypatch.setenv("ZIGGY_GH", str(Path(__file__).parent / "fake_gh.py"))
    code, out, _ = request(panel, "POST", "/api/onboard/agents-md", body)
    assert code == 200 and out["url"].endswith("/pull/1"), out
    shown = git("--git-dir", str(tmp_path / "field.git"), "show", f"{onboard_agent.AGENTS_BRANCH}:AGENTS.md", cwd=tmp_path)
    assert shown == "# field\n\nRun `yarn lint`.\n"
    create = next(json.loads(line)["argv"] for line in env["gh_log"].read_text().splitlines() if '"create"' in line)
    assert create[:3] == ["pr", "create", "--draft"] and create[create.index("--head") + 1] == "docs/agents-md"
    assert git("rev-parse", "HEAD", cwd=repo) == head and git("branch", "--show-current", cwd=repo).strip() == "main"
    assert git("status", "--porcelain", cwd=repo) == "" and len(git("worktree", "list", cwd=repo).splitlines()) == 1
    assert "docs/agents-md" not in git("branch", "--list", cwd=repo)
    code, out, _ = request(panel, "POST", "/api/onboard/agents-md", body)
    assert code == 400 and "already has a docs/agents-md branch" in out["error"]

    env["config"].write_text(env["config"].read_text() + f'\n[onboard.gh_users]\n"{tmp_path}" = "work-me"\n')
    code, out, _ = request(panel, "POST", "/api/onboard/agents-md", body)
    assert code == 409 and "company repository" in out["error"]
    with pytest.raises(onboard_agent.OnboardError, match="explicit confirmation"):
        onboard_agent.agents_md_pr(repo, "x", gh_user="tester", github_repo="acme/field", base_branch="main")
    found = onboard.detect(repo)
    assert found["company"] and not found["has_rules"]


# --- panel and CLI -------------------------------------------------------------------------------------------------

@needs_sandbox
def test_panel_verify_job(panel, env, tmp_path):  # noqa: F811
    repo = make_repo(tmp_path / "app", {"a.txt": "a\n"})
    proposal = proposal_with(prepare=["touch .ok-ignored"], checks_=[("a", "test -f a.txt", None), ("b", "exit 1", None)])
    proposal["prepare"][0]["verify"] = {"status": "pass"}  # what the form sends back is cleaned
    code, out, _ = request(panel, "POST", "/api/onboard", {"path": str(repo), "proposal": proposal, "agent": False, "name": "app"})
    assert code == 200 and onboard_agent.REQUEST_ID.match(out["request"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        _, view, _ = request(panel, "GET", f"/api/onboard/{out['request']}")
        if view.get("phase") == "done":
            break
        time.sleep(0.3)
    assert view["phase"] == "done", view
    # .ok-ignored is not ignored: the tree is dirty after prepare, as the engine would find it
    assert [c["verify"]["status"] for c in view["proposal"]["checks"]] == ["fail", "fail"]
    assert "prepare left the tree dirty: .ok-ignored" in view["proposal"]["checks"][0]["verify"]["reason"]
    assert request(panel, "GET", "/api/onboard/not-a-request")[0] == 404
    assert request(panel, "POST", "/api/onboard", {"path": str(repo), "proposal": {"prepare": [{"run": "a\nb"}]}})[0] == 400


@needs_sandbox
def test_cli_target_add_verify(env, tmp_path):
    repo = make_repo(tmp_path / "shop", {"package.json": json.dumps({"scripts": {"test": "node -e 0"}}), "package-lock.json": "{}",
                                         ".gitignore": "node_modules/\n"})
    proc = cli("target", "add", str(repo), "--verify", "--yes")
    assert proc.returncode == 0, proc.stderr
    assert "prepare:\n  [geçmedi: " in proc.stdout or "prepare:\n  [denendi · " in proc.stdout
    assert "why: package-lock.json: npm ci" in proc.stdout and "checks:\n  [" in proc.stdout
    assert "[targets.shop]" in proc.stdout and config.load_target("shop").checks
