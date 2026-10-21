"""Packaging: what an installed Ziggy carries (pyproject package-data covers every file the package reads), its version,
and `ziggy doctor`."""
import tomllib

from conftest import ROOT, cli

from ziggy import __version__, config, dispatch, doctor

PACKAGE = ROOT / "ziggy"


def row(rows, name):
    return next(r for r in rows if r[1] == name)


def test_package_data_ships_every_non_python_file_the_package_holds():
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["ziggy"]
    shipped = {p for g in globs for p in PACKAGE.glob(g) if p.is_file()}
    held = {p for p in PACKAGE.rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
    assert held - shipped == set(), "add these to [tool.setuptools.package-data] in pyproject.toml"
    assert {p.relative_to(PACKAGE).as_posix() for p in shipped} >= {"claude-settings.json", "githooks/pre-push",
                                                                    "pipelines/feat.toml", "prompts/plan.md", "web/index.html"}


def test_the_package_finds_its_data_beside_itself_and_starts_itself_from_the_checkout():
    assert config.PACKAGE == PACKAGE and dispatch.SETTINGS == PACKAGE / "claude-settings.json"
    assert config.self_argv()[1:] == [str(ROOT / "bin" / "ziggy")]


def test_version_flag(env):
    out = cli("--version")
    assert out.returncode == 0 and out.stdout.strip() == f"ziggy {__version__}"


def test_doctor_passes_a_ready_machine_and_names_what_to_fix(env, tmp_path, monkeypatch):
    rows = doctor.checks()
    assert row(rows, "claude")[0] == "ok" and row(rows, "gh") == ("ok", "gh", "logged in")
    assert row(rows, "config")[0] == "ok" and row(rows, "target demo") == ("ok", "target demo", str(env["repo"]))
    assert row(rows, "pre-push")[0] == "ok" and row(rows, "settings")[0] == "ok" and row(rows, "state")[0] == "ok"

    old = tmp_path / "old-claude"
    old.write_text("#!/bin/sh\necho '2.1.40 (Claude Code)'\n")
    old.chmod(0o755)
    monkeypatch.setenv("ZIGGY_CLAUDE", str(old))
    monkeypatch.setenv("ZIGGY_CONFIG", str(tmp_path / "missing.toml"))
    rows = doctor.checks()
    assert row(rows, "claude")[0] == "fail" and "claude update" in row(rows, "claude")[2]
    assert row(rows, "config")[0] == "fail" and "does not exist" in row(rows, "config")[2]
    out = cli("doctor")
    assert out.returncode == 1 and "problem" in out.stdout and "fail  claude" in out.stdout
