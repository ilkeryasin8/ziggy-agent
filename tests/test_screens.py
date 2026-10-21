"""Screenshots after checks (screens.py): the app started and waited for in the checks' sandbox, each page at each size
taken as evidence for the review page; a page that cannot be taken goes back; no browser is an environment problem."""
import os
import socket
from pathlib import Path

import pytest
from conftest import ROOT, cli, script
from test_engine import _records, _review, _state

from ziggy import checks, config, screens, state

FAKE_BROWSER = ROOT / "tests" / "fake_browser.py"
needs_sandbox = pytest.mark.skipif(not checks.available(), reason="needs an OS sandbox for checks (sandbox-exec or bwrap)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def with_screens(env, pages, browser=None, sizes=None):
    port = free_port()
    extra = (f'[targets.demo.screens]\npages = {pages!r}\nurl = "http://127.0.0.1:{port}"\n'
             f'start = "python3 -m http.server {port} --bind 127.0.0.1"\nwait = "10s"\n'
             + (f'browser = "{browser}"\n' if browser else "") + (f"sizes = {sizes!r}\n" if sizes else ""))
    env["config"].write_text(env["config"].read_text() + extra.replace("'", '"'))
    os.chmod(FAKE_BROWSER, 0o755)
    return port


def test_names_and_the_script(env):
    assert screens.file_name("/", (1440, 900)) == "root-1440x900.png"
    assert screens.file_name("/cart/items?x=1", (390, 844)) == "cart-items-x-1-390x844.png"
    with_screens(env, ["/", "/cart"], browser=str(FAKE_BROWSER))
    target = config.load_target("demo")
    assert target.screens["sizes"] == [[1440, 900], [390, 844]] and screens.browser(target) == str(FAKE_BROWSER)
    text = screens.script(target, Path("/out"), "/bin/chrome", {})
    assert text.count("--screenshot=") == 4 and "--window-size=390,844" in text and "--no-sandbox" in text
    assert "python3 -m http.server" in text and "did not answer" in text


@needs_sandbox
def test_after_the_checks_each_page_is_taken_at_each_size_as_evidence(env):
    with_screens(env, ["/", "/README.md"], browser=str(FAKE_BROWSER))
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0 and _state(env)["status"] == "done"
    impl = next(r for r in _records(env) if r["stage"] == "implement")
    shot = next(c for c in impl["checks"] if c["name"] == "screens")
    assert shot["status"] == "pass" and "took /README.md at 390x844" in shot["detail"]
    paths = [a["path"] for a in impl["result"]["artifacts"]]
    assert paths == ["screens/root-1440x900.png", "screens/root-390x844.png", "screens/README-md-1440x900.png",
                     "screens/README-md-390x844.png"]
    out = state.inbox_dir(config.load_target("demo"), "local-1") / "artifacts" / "screens"
    assert (out / "README-md-390x844.png").read_bytes().startswith(b"\x89PNG") and b"target" in (out / "README-md-390x844.png").read_bytes()


@needs_sandbox
def test_a_page_that_cannot_be_taken_goes_back_like_a_check(env):
    with_screens(env, ["/broken"], browser=str(FAKE_BROWSER), sizes=[[800, 600]])
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 1  # every visit breaks it: the run stops at the cap
    impl = next(r for r in _records(env) if r["stage"] == "implement")
    shot = next(c for c in impl["checks"] if c["name"] == "screens")
    assert shot["status"] == "fail" and "could not take /broken at 800x600" in shot["detail"]
    assert impl["route"] == "checksFailed"


def test_no_browser_is_an_environment_problem(env, monkeypatch):
    with_screens(env, ["/"])
    monkeypatch.setattr(screens, "browser", lambda target: None)
    target = config.load_target("demo")
    outcome, evidence = screens.take(target, "local-1", env["repo"], env["repo"] / "artifacts", env["repo"] / "log", {}, {})
    assert outcome["status"] == "fail" and outcome["environment"]["why"].startswith("no browser for the screenshots")
    assert evidence == []


@pytest.mark.parametrize("table, error", [
    ('pages = []', "pages must be a list of paths starting with /"),
    ('pages = ["cart"]', "pages must be a list of paths starting with /"),
    ('pages = ["/"]\nsizes = [[10, 10]]', "sizes must be a list of"),
    ('pages = ["/"]\nzoom = 2', "unknown keys zoom"),
])
def test_screens_are_validated(env, table, error):
    env["config"].write_text(env["config"].read_text() + f"[targets.demo.screens]\n{table}\n")
    with pytest.raises(config.ConfigError, match=error):
        config.load_target("demo")


REAL = [p for p in (screens.browser(config.Target(name="x", path=Path("/"))),) if p and "headless" in p]


@needs_sandbox
@pytest.mark.skipif(not REAL, reason="needs Playwright's chrome-headless-shell (a real browser)")
def test_a_real_headless_shell_takes_a_page_inside_the_sandbox(env):
    with_screens(env, ["/README.md"], sizes=[[390, 844]])
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}], "review": [_review("converged", [], converged=True)]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    png = state.inbox_dir(config.load_target("demo"), "local-1") / "artifacts" / "screens" / "README-md-390x844.png"
    assert png.read_bytes().startswith(b"\x89PNG") and png.stat().st_size > 1000
