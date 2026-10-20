"""One headless `claude -p` session of a pipeline stage, recorded in <stage dir>/run.json.

While the session runs, `run` looks at it about once a second (there is no daemon: this loop is the
watchdog). It interrupts the agent with SIGINT (claude ends its turn and exits; the session can be
resumed) for a nudge (`runs/<id>/nudge.json`, written by `pipeline.nudge`), past the stage's timeout,
and, when the stage retries quiet sessions, after `quiet_after` without output while no tool it started
is running. An agent still alive a minute after the interrupt is killed."""
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from . import quota, state, stalls
from .config import PIPELINE_ROOT, Target
from .worktree import target_env

SETTINGS = PIPELINE_ROOT / "claude-settings.json"
GITHOOKS = PIPELINE_ROOT / "githooks"
RESULT_FIELDS = ("total_cost_usd", "session_id", "is_error", "terminal_reason", "api_error_status", "modelUsage")
OUTPUT = "claude.jsonl"  # the session's event stream, one JSON object per line
PID_FILE = "pid"  # present while the stage's process (claude, then its checks) is alive
STOP_FILE = "stop-requested"
NUDGE_FILE = "nudge.json"  # runs/<id>/nudge.json: a pending nudge; <stage dir>/nudge.json: the one that interrupted it
POLL_S = 1.0  # how often a running session is looked at
INTERRUPT_GRACE_S = 60  # an interrupted agent still alive after this is killed
QUIET_CHECK_S = 15  # between process-table looks while a stage is quiet but a tool keeps it busy
TOOL_GRACE_S = 30  # processes started this soon after the agent are its own (MCP servers), not tool calls
MAX_NUDGE = 4000
# Nobody watches a headless session, so it may not ask questions. Nor may it park itself on a timer: a pending
# ScheduleWakeup or session cron (CronCreate) keeps `claude -p` alive and idle until it fires (a review that
# backgrounded its lens subagents and scheduled a 20-minute wakeup sat idle that long after its work).
ALWAYS_DENIED = ("AskUserQuestion", "ScheduleWakeup", "CronCreate")


def claude_bin() -> str:
    return os.environ.get("ZIGGY_CLAUDE", "claude")


RESUME_MARK = "The stage's instruction was:\n"


def resume_prompt(original: str, nudge: dict | None = None) -> str:
    return ("Your previous process for this step stopped before it wrote its result file (it was killed, crashed "
            "or timed out). Continue the same task from where you left off: check what is already done in the "
            "working tree and the plan, finish the rest, then write the result file.\n\n"
            + (nudge_section(nudge) + "\n" if nudge else "") + RESUME_MARK + original)


def nudge_resume_prompt(nudge: dict, original: str) -> str:
    """The prompt that resumes an interrupted session: why it was stopped (a person's message verbatim, or
    the watchdog's reason), then the stage's instruction again."""
    kind, text = nudge.get("kind"), nudge.get("text") or ""
    if kind == "user":
        head = (f"{nudge.get('by') or 'The user'} interrupted you to steer this step. Their message, verbatim:\n\n"
                f"{text}\n\nFollow it where it changes your approach. ")
    elif kind == "quota":
        head = (f"Ziggy paused you because {text}; the limit has reset now. ")
    elif kind == "quiet":
        head = (f"Ziggy stopped you because {text}: if a command hangs (waiting for input, a server that never "
                "exits, a watcher), run it differently or in the background with a timeout. ")
    else:
        head = f"Ziggy stopped you because {text}: finish the essential part first and leave the rest as open items. "
    return (head + "Your last turn was interrupted, so a tool call may not have finished: check what is already done, "
            "then continue the same task and write the result file as before.\n\n" + RESUME_MARK + original)


def nudge_section(nudge: dict) -> str:
    """A nudge added to a fresh prompt (a stage that had not started when it came, or a fresh retry)."""
    if nudge.get("kind") == "user":
        return (f"## A message from {nudge.get('by') or 'the user'}\n\nThey want this step steered as follows (verbatim):"
                f"\n\n{nudge.get('text') or ''}\n")
    return ("## Note from Ziggy\n\nThe previous attempt of this step was stopped: "
            f"{nudge.get('text') or ''}. Check what is already done before you continue.\n")


def fmt_duration(seconds: float) -> str:
    """Whole minutes: "2d 3h", "1h 05m", "12m", "0m"."""
    m = int(seconds // 60)
    d, h, m = m // 1440, (m % 1440) // 60, m % 60
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


_HELP: dict[str, str] = {}


def supports(flag: str) -> bool:
    """Whether this claude knows a flag (asked once per process), so an older CLI keeps working."""
    if "text" not in _HELP:
        try:
            _HELP["text"] = subprocess.run([claude_bin(), "--help"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            _HELP["text"] = ""
    return flag in _HELP["text"]


def command(model: str, session_id: str, prompt: str, inbox: Path, resume: bool = False, budget_usd: float | None = None,
            tools_deny: tuple[str, ...] = (), json_schema: dict | None = None, effort: str | None = None,
            mcp: bool = False) -> list[str]:
    # --setting-sources project: the target's own settings apply, the user's global ones
    # (CLAUDE.md, skills, hooks) do not. --strict-mcp-config without --mcp-config: no MCP, unless `mcp`
    # (Sohbet, talk.py): then the login's claude.ai connectors and the repository's MCP servers load.
    # --add-dir inbox: the agent reads the ticket and writes the plan and its result there; the
    # sandbox lets Bash write there too, so it holds nothing the wrapper trusts without rewriting.
    deny = list(dict.fromkeys([*tools_deny, *ALWAYS_DENIED]))
    return [
        claude_bin(), "-p", prompt,
        "--model", model,
        "--settings", str(SETTINGS),
        "--setting-sources", "project",
        *([] if mcp else ["--strict-mcp-config"]),
        "--permission-mode", "auto",
        "--add-dir", str(inbox),
        "--disallowedTools", *deny,
        # A fresh session gets the id the wrapper chose; a resumed one continues that id.
        *(["--resume", session_id] if resume else ["--session-id", session_id]),
        # stream-json writes each event as it happens, so a run can be watched while it works.
        "--output-format", "stream-json", "--verbose",
        *(["--json-schema", json.dumps(json_schema)] if json_schema and supports("--json-schema") else []),
        *(["--max-budget-usd", f"{budget_usd:g}"] if budget_usd and supports("--max-budget-usd") else []),
        *(["--effort", effort] if effort and supports("--effort") else []),
    ]


def environment(target: Target, ticket_id: str, wt: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env, _ = target_env(target, ticket_id, wt)
    env.update(extra or {})
    env["ZIGGY_RUN"] = "1"
    # The pre-push hook reaches git through the environment, so the target's .git/config is
    # never edited; the wrapper's own git calls do not carry it.
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    env[f"GIT_CONFIG_KEY_{n}"] = "core.hooksPath"
    env[f"GIT_CONFIG_VALUE_{n}"] = str(GITHOOKS)
    return env


def read_events(text: str) -> list[dict]:
    """Events from stream-json (one object per line) or from json (one list or object)."""
    try:
        data = json.loads(text)
        return [e for e in (data if isinstance(data, list) else [data]) if isinstance(e, dict)]
    except json.JSONDecodeError:
        pass
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def parse_output(text: str) -> dict:
    """A session can emit several result events (one per segment, e.g. after a subagent
    notification): cost is cumulative, so it comes from the last one; turns and time add up."""
    events = read_events(text)
    # A session that ended without a result event (stopped, crashed, timed out) has no outcome.
    if not events:
        return {"parse_error": "claude output has no JSON events"}
    results = [e for e in events if e.get("type") == "result"]
    if not results:
        return {"parse_error": "no result event in claude output"}
    final = results[-1]
    parsed = {k: final.get(k) for k in RESULT_FIELDS}
    parsed["num_turns"] = sum(r.get("num_turns") or 0 for r in results)
    parsed["duration_ms"] = sum(r.get("duration_ms") or 0 for r in results)
    parsed["permission_denials"] = final.get("permission_denials") or []
    parsed["final_message"] = (final.get("result") or "")[-4000:]
    structured = final.get("structured_output")
    if structured is not None:
        parsed["structured_output"] = structured
    return parsed


def run(target: Target, ticket_id: str, wt: Path, inbox: Path, stage_dir: Path, model: str, prompt: str, *,
        record: dict, resume_session: str | None = None, timeout: int | None = None, tools_deny: tuple[str, ...] = (),
        json_schema: dict | None = None, extra_env: dict[str, str] | None = None, effort: str | None = None,
        quiet_after: int | None = None, interrupt_on_quiet: bool = False, stall_after: int | None = None) -> dict:
    """Run one session in `stage_dir` (already created); `record` holds the caller's fields for run.json.
    An interrupt is recorded as `interrupt` ("nudge:user", "nudge:quiet", "timeout") and `interrupted_at`;
    the message that caused a nudge interrupt is `stage_dir/nudge.json`. `stall_after`: a tool call running longer is
    the record's `stall` while it runs (stalls.Watch, which also adds `host` samples and `tool_calls`)."""
    session_id = resume_session or str(uuid.uuid4())
    argv = command(model, session_id, prompt, inbox, resume=bool(resume_session), budget_usd=target.budget_usd,
                   tools_deny=tools_deny, json_schema=json_schema, effort=effort)
    account = quota.account_key(target)
    record.update({
        "model": model,
        "effort": effort if effort and supports("--effort") else None,
        "session_id": session_id,
        **({"resumed": True} if resume_session else {}),
        "prompt": prompt,
        "worktree": str(wt),
        "account": account,
        "started_at": state.now(),
        "dir": str(stage_dir),
    })
    watch = stalls.Watch(stage_dir, record, stall_after)  # its first `host` sample goes in with the record
    # The session id is saved before the spawn, so a run that dies mid-spawn can still be resumed.
    state.write_json(stage_dir / "run.json", record)
    out_path, err_path = stage_dir / OUTPUT, stage_dir / "claude.err"
    pid_path = stage_dir / PID_FILE
    feed = quota.Feed(account, out_path)  # the account's limits, as the stream reports them
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        # Own process group, so `stop`, a nudge and the timeout reach claude together with its children.
        proc = subprocess.Popen(
            argv, cwd=wt, env=environment(target, ticket_id, wt, extra_env),
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
        )
        pid_path.write_text(f"{proc.pid}\n")
        try:
            _watch(proc, stage_dir, record, timeout or target.timeout, quiet_after if interrupt_on_quiet else None, feed,
                   watch)
        finally:
            pid_path.unlink(missing_ok=True)
    feed.poll()
    watch.finish()
    if feed.last:
        record["rate_limit"] = feed.last
    if feed.rate_limited:
        record["rate_limited"] = True
    if (stage_dir / STOP_FILE).exists():
        record["stopped"] = True
    record["finished_at"] = state.now()
    record.update(parse_output(out_path.read_text(encoding="utf-8")))
    state.write_json(stage_dir / "run.json", record)
    return record


def _watch(proc: subprocess.Popen, stage_dir: Path, record: dict, limit: int, quiet_after: int | None,
           feed: quota.Feed | None = None, watch: stalls.Watch | None = None) -> None:
    """Wait for the session, interrupting it for a nudge, its timeout or (with `quiet_after`) silence; `feed`
    passes the stream's rate-limit events to the quota ledger as they come, `watch` follows its tool calls."""
    started = time.monotonic()
    interrupted = None  # monotonic time of the interrupt
    next_quiet_look = 0.0
    while True:
        try:
            record["exit_code"] = proc.wait(timeout=POLL_S)
            return
        except subprocess.TimeoutExpired:
            pass
        if feed:
            feed.poll()
        if watch:
            watch.poll()
        now = time.monotonic()
        if interrupted is not None:
            if now - interrupted >= INTERRUPT_GRACE_S:
                _end_group(proc)
                record.update(exit_code=None, killed_after_interrupt=True)
                return
            continue
        if (stage_dir / STOP_FILE).exists():  # stop() already sent SIGTERM
            if now - started >= limit:
                _end_group(proc)
                record["exit_code"] = None
                return
            continue
        if _take_nudge(stage_dir):
            interrupted = _interrupt(proc, stage_dir, record, "nudge:user")
        elif now - started >= limit:
            record["timed_out"] = True
            interrupted = _interrupt(proc, stage_dir, record, "timeout")
        elif quiet_after and now >= next_quiet_look:
            quiet = quiet_for(stage_dir, quiet_after, pid=proc.pid)
            if quiet is None:
                # Not quiet yet (stat only), or a tool keeps it busy (ps): look at the process table less often.
                last = _last_output(stage_dir)
                next_quiet_look = now + (QUIET_CHECK_S if last and time.time() - last > quiet_after else 0)
            else:
                state.write_json(stage_dir / NUDGE_FILE, {"text": f"it produced no output for {fmt_duration(quiet)}",
                                                          "by": "watchdog", "kind": "quiet", "at": state.now()})
                interrupted = _interrupt(proc, stage_dir, record, "nudge:quiet")


def _take_nudge(stage_dir: Path) -> bool:
    """Claim the ticket's pending nudge for this session (whoever renames it first owns it)."""
    try:
        os.replace(stage_dir.parent / NUDGE_FILE, stage_dir / NUDGE_FILE)
    except FileNotFoundError:
        return False
    return True


def _interrupt(proc: subprocess.Popen, stage_dir: Path, record: dict, reason: str) -> float:
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    record.update(interrupt=reason, interrupted_at=state.now())
    state.write_json(stage_dir / "run.json", record)  # the panel sees it at once
    return time.monotonic()


def _last_output(stage_dir: Path) -> float | None:
    times = []
    for name in (OUTPUT, PID_FILE):
        try:
            times.append((stage_dir / name).stat().st_mtime)
        except OSError:
            pass
    return max(times, default=None)


_PS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d\d:\d\d:\d\d \d{4})\s+(.*)$")


def parse_ps(text: str) -> list[dict]:
    """Rows of `ps -axww -o pid=,ppid=,pgid=,lstart=,command=` (lstart in local time, whole seconds)."""
    rows = []
    for line in text.splitlines():
        m = _PS_ROW.match(line)
        if not m:
            continue
        try:
            started = time.mktime(time.strptime(" ".join(m.group(4).split()), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "pgid": int(m.group(3)), "started": started,
                     "command": m.group(5)})
    return rows


def _ps() -> list[dict]:
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,lstart=,command="], capture_output=True, text=True,
                             timeout=30, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ps(out)


def tool_running(rows: list[dict], leader: dict, last_output: float) -> bool:
    """A process of the agent's group (not the agent) started after its last output: a tool call (a build,
    a test run) is working. Processes started within TOOL_GRACE_S of the agent are its own (MCP servers)."""
    return any(r["pgid"] == leader["pgid"] and r["pid"] != leader["pid"]
               and r["started"] > leader["started"] + TOOL_GRACE_S and r["started"] >= last_output - 5
               for r in rows)


def quiet_for(stage_dir: Path, threshold: int, pid: int | None = None) -> float | None:
    """Seconds since the session in `stage_dir` last wrote output, when that is past `threshold` and no
    tool call it started is still running; else None (also when no session is live there)."""
    if pid is None:
        try:
            pid = int((stage_dir / PID_FILE).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None
    last = _last_output(stage_dir)
    if last is None:
        return None
    quiet = time.time() - last
    if quiet <= threshold:
        return None
    rows = _ps()
    leader = next((r for r in rows if r["pid"] == pid), None)
    if leader is None:
        return None if rows else quiet  # gone: not ours to judge; no ps at all: trust the clock
    return None if tool_running(rows, leader, last) else quiet


def _end_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def stop(target: Target, ticket_id: str) -> str:
    """End the ticket's running session (or its checks); the engine then records the stage as killed."""
    live = [p / PID_FILE for p in state.phase_dirs(target, ticket_id) if (p / PID_FILE).exists()]
    st = state.load_state(target, ticket_id) if not live else {}
    if st.get("waiting_since") or st.get("retry_at"):
        # Waiting in line for a run slot, or before an automatic retry: no session now; the waiting process
        # ends the wait (and the run).
        owner = state.holder(target, f"ticket-{ticket_id}")
        if not owner:
            raise LookupError(f"{ticket_id}: the waiting process is already gone")
        os.kill(int(owner.split()[0]), signal.SIGTERM)
        return "queue" if st.get("waiting_since") else "retry-wait"
    if not live:
        raise LookupError(f"{ticket_id} has no running session")
    pid_path = live[-1]
    pid = int(pid_path.read_text().split()[0])
    (pid_path.parent / STOP_FILE).write_text(state.now() + "\n")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise LookupError(f"{ticket_id}: session process {pid} is already gone") from None
    return pid_path.parent.name
