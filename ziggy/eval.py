"""`ziggy eval`: Ziggy against a fixed set of small tasks with hidden tests, so a change to the engine, a prompt or a
model can be measured instead of guessed.

A task (`ziggy/evals/tasks/<id>/`) is `task.toml` (title, type, body, check), `repo/` (the seed repository, visible
tests and AGENTS.md), `hidden/` (tests the agents never see) and `solution/` (a reference fix, only for `validate`).
`run` gives each task a throwaway repository with a local origin, its own config and state (nothing of yours is read
or written), runs the ticket through the real engine and Claude Code, answers questions with their recommendations and
passes the approval gate, then runs the hidden tests on the branch under the checks' sandbox. Results go to
`<state>/evals/<run>/results.json`; `compare` puts two runs side by side. `validate` checks the tasks themselves: the
hidden tests fail on the seed and pass with the solution."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import checks, config, dispatch
from .config import Target

TASKS = Path(__file__).resolve().parent / "evals" / "tasks"
VISIBLE_TESTS = "python3 -m unittest discover -s tests -q"
HIDDEN_DIR = "hidden_tests"
CHECK_TIMEOUT = 300
MAX_STEPS = 6  # run + answers + approvals before a task counts as stuck
DEFAULT_ANSWER = "Go with your recommendation for each question."


class EvalError(Exception):
    pass


def tasks(selected: list[str] | None = None) -> list[dict]:
    """The tasks ({id, dir, title, type, body, check}), optionally only the ids (or id prefixes) in `selected`."""
    out = []
    for d in sorted(p for p in TASKS.iterdir() if p.is_dir()) if TASKS.is_dir() else []:
        meta = tomllib.loads((d / "task.toml").read_text(encoding="utf-8"))
        out.append({"id": d.name, "dir": d, "title": meta["title"], "type": meta.get("type", "feat"),
                    "body": meta.get("body", ""), "check": meta.get("check") or f"python3 -m unittest discover -s {HIDDEN_DIR} -t . -q"})
    if selected:
        out = [t for t in out if any(t["id"] == s or t["id"].startswith(s) for s in selected)]
        if not out:
            raise EvalError(f"no task matches {', '.join(selected)}")
    return out


def evals_dir() -> Path:
    return config.state_home() / "evals"


def _run(argv: list[str], cwd: Path, env: dict | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _git(*args: str, cwd: Path) -> str:
    proc = _run(["git", *args], cwd)
    if proc.returncode != 0:
        raise EvalError(f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


# --- validate -------------------------------------------------------------------------------------------------

def _copy(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))


def validate(selected: list[str] | None = None) -> list[dict]:
    """Each task: the visible tests pass on the seed, the hidden ones fail there, and both pass with the solution."""
    out = []
    for t in tasks(selected):
        row = {"id": t["id"]}
        with tempfile.TemporaryDirectory() as tmp:
            seed, solved = Path(tmp) / "seed", Path(tmp) / "solved"
            for d in (seed, solved):
                _copy(t["dir"] / "repo", d)
                _copy(t["dir"] / "hidden", d / HIDDEN_DIR)
            _copy(t["dir"] / "solution", solved)
            row["seed_visible"] = _run(VISIBLE_TESTS.split(), seed, timeout=CHECK_TIMEOUT).returncode == 0
            row["seed_hidden_fails"] = _run(t["check"].split(), seed, timeout=CHECK_TIMEOUT).returncode != 0
            row["solution_passes"] = (_run(t["check"].split(), solved, timeout=CHECK_TIMEOUT).returncode == 0
                                      and _run(VISIBLE_TESTS.split(), solved, timeout=CHECK_TIMEOUT).returncode == 0)
        row["ok"] = row["seed_visible"] and row["seed_hidden_fails"] and row["solution_passes"]
        out.append(row)
    return out


# --- run ------------------------------------------------------------------------------------------------------

def _config_text(task_ids: list[str], root: Path, plan_model: str | None, build_model: str | None) -> str:
    lines = [f'default_target = "{task_ids[0]}"', ""]
    for tid in task_ids:
        lines += [f"[targets.{_name(tid)}]", f'path = "{root / tid / "repo"}"', "retro = false"]
        lines += [f'plan_model = "{plan_model}"'] if plan_model else []
        lines += [f'build_model = "{build_model}"'] if build_model else []
        lines += ["", f"[[targets.{_name(tid)}.checks]]", 'name = "tests"', f'run = "{VISIBLE_TESTS}"', ""]
    return "\n".join(lines)


def _name(task_id: str) -> str:
    return "eval-" + task_id.lower()


def _seed(t: dict, root: Path) -> Path:
    """The task's throwaway repository on main, pushed to a local bare origin."""
    repo, origin = root / t["id"] / "repo", root / t["id"] / "origin.git"
    _copy(t["dir"] / "repo", repo)
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=root)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    return repo


def _env(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ZIGGY_RUN", "GIT_CONFIG_COUNT")}
    env.update(ZIGGY_CONFIG=str(root / "config.toml"), XDG_STATE_HOME=str(root / "state"), ZIGGY_RETRO="off",
               ZIGGY_LEAD="off", ZIGGY_NOTIFY="off")
    return env


def _state_of(root: Path, task_id: str, ticket_id: str) -> tuple[dict, Path]:
    run_dir = root / "state" / "ziggy" / _name(task_id) / "runs" / ticket_id
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8")), run_dir


def _sessions(run_dir: Path) -> list[dict]:
    out = []
    for d in sorted(run_dir.glob("[0-9][0-9]-*")):
        try:
            out.append(json.loads((d / "run.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _answer_text(st: dict) -> str:
    rounds = [r for r in st.get("rounds") or [] if r.get("answer") is None]
    questions = rounds[-1].get("questions") if rounds else None
    if questions and all(q.get("recommendation") for q in questions):
        return "\n\n".join(f"{q.get('id') or f'Q{i + 1}'}: {q['recommendation']}" for i, q in enumerate(questions))
    return DEFAULT_ANSWER


def _hidden(t: dict, repo: Path, branch: str, root: Path) -> dict:
    """The hidden tests on the ticket's branch, in a fresh clone, under the checks' sandbox (the code is the agent's)."""
    check_dir = root / t["id"] / "check"
    _git("clone", "-q", "--branch", branch, str(repo), str(check_dir), cwd=root)
    _copy(t["dir"] / "hidden", check_dir / HIDDEN_DIR)
    target = Target(name=_name(t["id"]), path=repo)
    env = dispatch.scrub(dict(os.environ), keep=set())
    outcome = checks.run(target, "eval", check_dir, [{"name": "hidden", "run": t["check"], "timeout_s": CHECK_TIMEOUT}],
                         root / t["id"] / "hidden.log", env)[0]
    return {"passed": outcome["status"] == "pass", "detail": (outcome.get("detail") or "")[-600:]}


def run_task(t: dict, root: Path, env: dict) -> dict:
    started = time.monotonic()
    repo = _seed(t, root)
    ziggy = [*config.self_argv(), "--target", _name(t["id"])]
    new = subprocess.run([*ziggy, "new", f"{t['type']}: {t['title']}", "--body-file", "-"], cwd=repo, env=env,
                         input=t["body"], capture_output=True, text=True)
    if new.returncode != 0:
        raise EvalError(f"{t['id']}: ziggy new failed: {new.stderr.strip()[-300:]}")
    ticket_id = new.stdout.split("\t")[0].strip()
    # --now: an eval is a run you asked for, like `ziggy run --now`; the machine-memory and usage-limit holds do not
    # park it (a hold after an answer or an approval is resumed the same way).
    steps, interventions = [["run", ticket_id, "--now"]], 0
    st, run_dir = {}, None
    for _ in range(MAX_STEPS):
        if not steps:
            break
        _run([*ziggy, *steps.pop()], repo, env)
        st, run_dir = _state_of(root, t["id"], ticket_id)
        if st.get("status") == "needs-user":
            steps.append(["answer", ticket_id, "--text", _answer_text(st), "--by", "eval"])
            interventions += 1
        elif st.get("status") == "awaiting-approval":
            steps.append(["approve", ticket_id])
            interventions += 1
        elif st.get("status") in ("host", "quota"):
            steps.append(["run", ticket_id, "--now"])
    sessions = _sessions(run_dir) if run_dir else []
    row = {"id": t["id"], "type": t["type"], "ticket": ticket_id, "status": st.get("status"), "error": st.get("error"),
           "cost_usd": round(sum(s.get("total_cost_usd") or 0 for s in sessions), 4), "sessions": len(sessions),
           "implement_visits": sum(1 for s in sessions if s.get("stage") == "implement"),
           "review_rounds": sum(1 for s in sessions if s.get("stage") == "review"), "interventions": interventions,
           "risk": st.get("risk"), "size": st.get("size")}
    ticket = json.loads((run_dir / "ticket.json").read_text(encoding="utf-8")) if run_dir else {}
    hidden = _hidden(t, repo, ticket["branch"], root) if st.get("status") == "done" and ticket.get("branch") else \
        {"passed": False, "detail": f"not done: {st.get('status')}"}
    return {**row, "passed": hidden["passed"], "hidden_detail": hidden["detail"], "seconds": round(time.monotonic() - started)}


def run(selected: list[str] | None = None, label: str | None = None, plan_model: str | None = None,
        build_model: str | None = None, echo=print) -> Path:
    """Every selected task through the engine, one after the other; returns the run's directory."""
    chosen = tasks(selected)
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
    root = evals_dir() / name
    root.mkdir(parents=True)
    (root / "config.toml").write_text(_config_text([t["id"] for t in chosen], root, plan_model, build_model), encoding="utf-8")
    env = _env(root)
    meta = {"name": name, "label": label, "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "plan_model": plan_model, "build_model": build_model, "claude": ".".join(map(str, dispatch.claude_version() or ())),
            "results": []}
    for t in chosen:
        try:
            result = run_task(t, root, env)
        except (EvalError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            result = {"id": t["id"], "type": t["type"], "status": "error", "error": str(exc)[:500], "passed": False,
                      "cost_usd": 0.0, "sessions": 0, "seconds": 0}
        meta["results"].append(result)
        (root / "results.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        echo(f"{t['id']}: {'pass' if result['passed'] else 'FAIL'} · {result['status']} · ${result.get('cost_usd', 0):.2f} · "
             f"{result.get('seconds', 0)}s")
    return root


# --- results --------------------------------------------------------------------------------------------------

def load(ref: str) -> dict:
    """A run's results by directory, run name or a unique name prefix (`latest` for the newest)."""
    path = Path(ref).expanduser()
    if not path.is_dir():
        runs = sorted(p for p in evals_dir().iterdir() if (p / "results.json").is_file()) if evals_dir().is_dir() else []
        found = runs[-1:] if ref == "latest" else [p for p in runs if p.name == ref] or [p for p in runs if p.name.startswith(ref)]
        if len(found) != 1:
            raise EvalError(f"no single eval run matches {ref!r} under {evals_dir()}")
        path = found[0]
    return json.loads((path / "results.json").read_text(encoding="utf-8"))


def totals(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    return {"tasks": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else None,
            "cost_usd": round(sum(r.get("cost_usd") or 0 for r in results), 2),
            "seconds": sum(r.get("seconds") or 0 for r in results),
            "implement_visits": sum(r.get("implement_visits") or 0 for r in results),
            "interventions": sum(r.get("interventions") or 0 for r in results)}


def summary(meta: dict) -> str:
    t = totals(meta["results"])
    lines = [f"{meta['name']}: {t['passed']}/{t['tasks']} passed · ${t['cost_usd']:.2f} · {t['seconds'] // 60}m"]
    for r in meta["results"]:
        lines.append(f"  {r['id']:<28} {'pass' if r.get('passed') else 'FAIL':<5} {str(r.get('status')):<18} "
                     f"${r.get('cost_usd') or 0:>6.2f} {r.get('seconds') or 0:>5}s  impl {r.get('implement_visits') or 0}"
                     f"  review {r.get('review_rounds') or 0}")
    return "\n".join(lines)


def compare(a: dict, b: dict) -> str:
    """Two runs task by task (pass, cost, time) and in total; a task only one ran is listed as missing in the other."""
    ra, rb = {r["id"]: r for r in a["results"]}, {r["id"]: r for r in b["results"]}
    ta, tb = totals(a["results"]), totals(b["results"])
    mark = lambda r: "—" if r is None else "pass" if r.get("passed") else "FAIL"  # noqa: E731
    lines = [f"A {a['name']}  vs  B {b['name']}",
             f"passed  {ta['passed']}/{ta['tasks']}  →  {tb['passed']}/{tb['tasks']}",
             f"cost    ${ta['cost_usd']:.2f}  →  ${tb['cost_usd']:.2f}",
             f"time    {ta['seconds'] // 60}m  →  {tb['seconds'] // 60}m", ""]
    for tid in sorted(set(ra) | set(rb)):
        x, y = ra.get(tid), rb.get(tid)
        changed = " *" if (x or {}).get("passed") != (y or {}).get("passed") else ""
        lines.append(f"  {tid:<28} {mark(x):<5} → {mark(y):<5} ${(x or {}).get('cost_usd') or 0:>6.2f} → "
                     f"${(y or {}).get('cost_usd') or 0:>6.2f}{changed}")
    return "\n".join(lines)
