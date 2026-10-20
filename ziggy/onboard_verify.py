"""Trying an onboarding proposal before it is saved: prepare and the checks in a throwaway clone, like a run would.

`verify` clones the main checkout (`git clone --local --no-hardlinks`, so nothing is shared with it) into the
request's state dir, runs the prepare lines there as the engine does (a shell in the clone, the proposal's env with
{checkout}/{state}/{worktree} expanded, network on, outside any sandbox) but with a timeout per line, then each check
exactly like the engine: `checks.run` under the sandbox-exec profile of the clone (plus the proposal's check_sandbox
presets, e.g. xcode), the session environment, the
`git status` guard (files a check left behind, tracked files it changed). Unlike the engine it runs every check, so
each line gets its own badge. The clone is always deleted, also after a failure or a timeout; the log stays next to
it (`verify-<stamp>/verify.log`).

Each line of the returned proposal gets `verify`: {status: pass|fail|untried, seconds, reason, tail, left_files}.
Output tails are redacted: token-shaped strings and any value of the checkout's real .env/.npmrc files (passed in as
`secrets`) become ***.
"""
import copy
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import checks as engine_checks
from . import config, dispatch, onboard, worktree
from .config import Target, expand

ROOT_NAME = "onboard"  # <state home>/onboard/<request id>/: a request's sessions, verifications and jobs
TICKET = "onboard"  # the run id {run_id} stands for during onboarding
PREPARE_TIMEOUT = 900  # seconds per prepare line
CLONE_TIMEOUT = 600
CHECK_TIMEOUT = 600  # a check without its own timeout, as in config.py
TAIL_MAX = 1500
_TOKEN = re.compile(r"(_authToken\s*=\s*|_password\s*=\s*|_auth\s*=\s*)\S+|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
                    r"|\bgithub_pat_[A-Za-z0-9_]{20,}|\bnpm_[A-Za-z0-9]{20,}|\bxox[abp]-[A-Za-z0-9-]{10,}")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def root() -> Path:
    return config.state_home() / ROOT_NAME


def work_target(repo: Path, request_id: str, env: dict[str, str], budget_usd: float | None = None,
                check_sandbox: tuple[str, ...] = ()) -> Target:
    """The stand-in target of one onboarding request: the checkout, the proposal's env and check_sandbox presets, and a
    state dir of its own (<state home>/onboard/<request id>), so dispatch, the prepare env and the check sandbox work as
    for a target."""
    return Target(name=f"{ROOT_NAME}/{request_id}", path=repo, env=dict(env), budget_usd=budget_usd,
                  check_sandbox=tuple(check_sandbox))


def redact(text: str, secrets: set[str] = frozenset()) -> str:
    text = _TOKEN.sub(lambda m: (m.group(1) or "") + "***", text)
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def _tail(log: Path, start: int, secrets: set[str]) -> str:
    with log.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(start, size - TAIL_MAX * 2))
        text = f.read().decode("utf-8", errors="replace")
    return redact(_ANSI.sub("", text).strip()[-TAIL_MAX:], secrets)


def _short(how: str, tail: str) -> str:
    """'exit 1: <the last line that says something>' for a badge."""
    lines = [line.strip() for line in tail.splitlines() if line.strip() and not line.startswith("===")]
    last = lines[-1][:140] if lines else ""
    return f"{how}: {last}" if last and last != how else how


def _run_line(command: str, cwd: Path, env: dict, log: Path, timeout: int, secrets: set[str]) -> dict:
    """One prepare line in its own process group, killed whole on timeout."""
    with log.open("a", encoding="utf-8") as out:
        out.write(f"\n$ {command}\n")
        out.flush()
        start = out.tell()
        started = time.monotonic()
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
            how = f"exit {code}"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            code, how = None, f"timed out after {engine_checks.short_duration(timeout)}"
        seconds = round(time.monotonic() - started, 1)
    tail = _tail(log, start, secrets)
    if code == 0:
        return {"status": "pass", "seconds": seconds, "reason": "", "tail": tail}
    return {"status": "fail", "seconds": seconds, "reason": _short(how, tail), "tail": tail}


def _untried(reason: str = "") -> dict:
    return {"status": "untried", "reason": reason}


def verify(repo: Path, proposal: dict, request_id: str, secrets: set[str] = frozenset(), progress=None,
           prepare_timeout: int = PREPARE_TIMEOUT) -> tuple[dict, dict]:
    """(the proposal with a `verify` badge on every line, summary {ok, failures, seconds, log, error})."""
    progress = progress or (lambda message: None)
    proposal = copy.deepcopy(proposal)
    for kind in ("prepare", "checks", "env"):
        for item in proposal.get(kind) or []:
            item["verify"] = _untried()
    spec = onboard.spec_from(proposal)
    target = work_target(repo, request_id, spec["env"], check_sandbox=tuple(spec["check_sandbox"]))
    base = target.state_dir / f"verify-{datetime.now():%Y%m%d-%H%M%S-%f}"
    clone, log = base / "clone", base / "verify.log"
    started = time.monotonic()
    summary = {"ok": False, "failures": [], "seconds": 0.0, "log": str(log), "error": None}
    try:
        base.mkdir(parents=True)
        log.write_text(f"verification of {repo}\n", encoding="utf-8")
        progress("klonlanıyor")
        try:
            cloned = subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", str(repo), str(clone)],
                                    capture_output=True, text=True, timeout=CLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            cloned = None
        if cloned is None or cloned.returncode != 0:
            summary["error"] = "clone failed: " + (cloned.stderr.strip() if cloned else "timed out")
            return proposal, summary
        env, values = worktree.target_env(target, TICKET, clone)
        lines = [p for p in proposal.get("prepare") or [] if str(p.get("run") or "").strip()]
        prepared = True
        for n, item in enumerate(lines, 1):
            if not prepared:
                item["verify"] = _untried("an earlier prepare line failed")
                continue
            progress(f"prepare {n}/{len(lines)}")
            item["verify"] = _run_line(expand(item["run"].strip(), values), clone, env, log, prepare_timeout, secrets)
            prepared = item["verify"]["status"] == "pass"
            if not prepared:
                summary["failures"].append({"kind": "prepare", "line": item["run"], **_failure(item["verify"])})
        items = [c for c in proposal.get("checks") or [] if str(c.get("name") or "").strip() and str(c.get("run") or "").strip()]
        dirty = worktree.dirty_paths(clone) if prepared else []
        for n, item in enumerate(items, 1):
            if not prepared:
                item["verify"] = _untried("prepare failed")
                continue
            if dirty:  # the engine fails a run's checks on a dirty tree before it runs any
                shown = ", ".join(dirty[:10]) + (f" (+{len(dirty) - 10})" if len(dirty) > 10 else "")
                item["verify"] = {"status": "fail", "seconds": 0, "reason": f"prepare left the tree dirty: {shown}",
                                  "tail": "add these to .gitignore (or have prepare remove them)"}
                summary["failures"].append({"kind": "check", "line": item["name"], **_failure(item["verify"])})
                continue
            progress(f"kontrol {n}/{len(items)}: {item['name']}")
            item["verify"] = _check(target, clone, item, log, secrets)
            if item["verify"]["status"] != "pass":
                summary["failures"].append({"kind": "check", "line": f"{item['name']}: {item['run']}", **_failure(item["verify"])})
        all_passed = prepared and bool(items) and not summary["failures"]
        for item in proposal.get("env") or []:
            item["verify"] = {"status": "pass", "reason": "the checks passed with it"} if all_passed else _untried()
        summary["ok"] = prepared and not summary["failures"]
        return proposal, summary
    except OSError as exc:
        summary["error"] = f"verification failed: {exc}"
        return proposal, summary
    finally:
        shutil.rmtree(clone, ignore_errors=True)
        try:  # what the commands printed stays, without credentials
            log.write_text(redact(log.read_text(encoding="utf-8", errors="replace"), secrets), encoding="utf-8")
        except OSError:
            pass
        summary["seconds"] = round(time.monotonic() - started, 1)


def _failure(badge: dict) -> dict:
    return {"reason": badge.get("reason", ""), "tail": badge.get("tail", "")}


def _check(target: Target, clone: Path, item: dict, log: Path, secrets: set[str]) -> dict:
    """One check as the engine runs it (sandbox-exec, the session env, the git status guard)."""
    try:
        timeout_s = config.parse_duration(item.get("timeout") or CHECK_TIMEOUT)
    except ValueError:
        timeout_s = CHECK_TIMEOUT
    env = dispatch.environment(target, TICKET, clone, {"PIPELINE_TICKET": TICKET, "PIPELINE_WORKTREE": str(clone),
                                                       "PIPELINE_VISIT": "1", "PIPELINE_LAST_VISIT": "1"})
    check = {"name": str(item["name"]).strip(), "run": str(item["run"]).strip(), "timeout_s": timeout_s}
    outcome = engine_checks.run(target, TICKET, clone, [check], log, env)[0]
    detail = redact(outcome.get("detail") or "", secrets)
    result = {"status": outcome["status"], "seconds": round((outcome.get("duration_ms") or 0) / 1000, 1),
              "reason": "" if outcome["status"] == "pass" else _short(detail.split("\n", 1)[0], detail), "tail": detail}
    if outcome.get("left_files"):
        result["left_files"] = outcome["left_files"]
    return result


def badge(result: dict | None) -> str:
    """A line's badge as the form shows it: 'denendi · 31 sn', 'geçmedi: <short reason>', 'denenmedi'."""
    status = (result or {}).get("status")
    if status == "pass":
        return f"denendi · {int(round(result['seconds']))} sn" if result.get("seconds") is not None else "denendi"
    if status == "fail":
        return f"geçmedi: {result.get('reason') or 'hata'}"
    return "denenmedi"
