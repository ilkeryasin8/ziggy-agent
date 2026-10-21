"""Screenshots of the target's pages after an implement passes its checks (`[targets.<name>.screens]`): the engine
starts the app (`start`, in the background), waits for `url`, and takes each page at each size with a headless
Chromium, all inside the checks' sandbox (the code is the agent's). The images go to the ticket's evidence
(inbox/artifacts/screens/) for the review page; a page that cannot be taken fails like a check and goes back.

The browser is `browser` from the config, else $ZIGGY_BROWSER, else Playwright's chrome-headless-shell
(`npx playwright install chromium-headless-shell`), else a Chromium on the PATH. Google Chrome itself does not start
inside the macOS checks' sandbox (its profile lock needs a socket the sandbox refuses); the headless shell does."""
import glob
import os
import re
import shlex
import shutil
from pathlib import Path

from . import checks
from .config import Target, expand

NAME = "screens"
SIZES = ((1440, 900), (390, 844))
WAIT_S = 60
DIR = "screens"  # under the ticket's inbox/artifacts
HEADLESS_SHELL = ("~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
                  "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")
ON_PATH = ("chrome-headless-shell", "chromium", "chromium-browser")
FLAGS = ("--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--disable-crash-reporter", "--disable-breakpad", "--hide-scrollbars")


def browser(target: Target) -> str | None:
    if target.screens.get("browser"):
        return os.path.expanduser(target.screens["browser"])
    if os.environ.get("ZIGGY_BROWSER"):
        return os.environ["ZIGGY_BROWSER"]
    for pattern in HEADLESS_SHELL:
        found = sorted(glob.glob(os.path.expanduser(pattern)))
        if found:
            return found[-1]  # the newest build
    return next((p for p in map(shutil.which, ON_PATH) if p), None)


def file_name(page: str, size: tuple[int, int]) -> str:
    """`/cart/items?x=1` at 390x844 → `cart-items-x-1-390x844.png`; `/` → `root-…`."""
    stem = re.sub(r"[^A-Za-z0-9]+", "-", page).strip("-") or "root"
    return f"{stem[:60]}-{size[0]}x{size[1]}.png"


def script(target: Target, out_dir: Path, binary: str, values: dict) -> str:
    """The bash a sandboxed check runs: start the app, wait for it, take every page at every size, stop it."""
    cfg = target.screens
    base = expand(cfg["url"], values).rstrip("/")
    sizes = [tuple(s) for s in cfg.get("sizes") or SIZES]
    lines = ['export TMPDIR="$(cd "${TMPDIR:-/tmp}" && pwd -P)"', f"mkdir -p {shlex.quote(str(out_dir))}", "srv="]
    if cfg.get("start"):
        lines += [f"( {expand(cfg['start'], values)} ) > {shlex.quote(str(out_dir / 'server.log'))} 2>&1 &", "srv=$!",
                  # The app's children first, then it. `set -e` holds inside a trap too: keep the script's own exit code
                  # and let a kill that finds nothing to kill pass.
                  "trap 'rc=$?; set +e; pkill -P $srv 2>/dev/null; kill $srv 2>/dev/null; exit $rc' EXIT"]
    wait = int(cfg.get("wait") or WAIT_S)
    lines += [f"up=; for i in $(seq 1 {wait * 4}); do curl -fs -o /dev/null {shlex.quote(base + '/')} && up=1 && break; sleep 0.25; done",
              f'[ -n "$up" ] || {{ echo "the app did not answer at {base}/ within {wait}s"; '
              f"tail -20 {shlex.quote(str(out_dir / 'server.log'))} 2>/dev/null; exit 1; }}", "failed=0"]
    for page in cfg["pages"]:
        for w, h in sizes:
            shot = out_dir / file_name(page, (w, h))
            lines.append(f"{shlex.quote(binary)} {' '.join(FLAGS)} --user-data-dir=\"$(mktemp -d)\" --window-size={w},{h} "
                         # The check runs under `set -e`: a failing browser must not end the script before it says so.
                         f"--screenshot={shlex.quote(str(shot))} {shlex.quote(base + page)} "
                         f">>{shlex.quote(str(out_dir / 'browser.log'))} 2>&1 || true; "
                         f"[ -s {shlex.quote(str(shot))} ] && echo 'took {page} at {w}x{h}' || "
                         f"{{ echo 'could not take {page} at {w}x{h}'; failed=1; }}")
    lines.append('exit "$failed"')
    return "\n".join(lines)


def take(target: Target, ticket_id: str, wt: Path, artifacts_dir: Path, log: Path, env: dict, values: dict) -> tuple[dict, list[dict]]:
    """Run the screenshots as a check; (its outcome, the evidence entries of the images it took)."""
    binary = browser(target)
    if not binary:
        return ({"name": NAME, "status": "fail", "source": "engine", "exit_code": None, "duration_ms": 0,
                 "environment": {"line": "no headless browser", "why": "no browser for the screenshots: install one with "
                                 "`npx playwright install chromium-headless-shell`, or set [targets.<name>.screens] browser"},
                 "detail": "no headless browser found (chrome-headless-shell or chromium)"}, [])
    out_dir = artifacts_dir / DIR
    check = {"name": NAME, "run": script(target, out_dir, binary, values), "timeout_s": 600}
    outcome = checks.run(target, ticket_id, wt, [check], log, env)[0]
    evidence = []
    for page in target.screens["pages"]:
        for w, h in [tuple(s) for s in target.screens.get("sizes") or SIZES]:
            name = file_name(page, (w, h))
            if (out_dir / name).is_file():
                evidence.append({"path": f"{DIR}/{name}", "kind": "image", "caption": f"{page} at {w}×{h} (engine screenshot)"})
    return outcome, evidence
