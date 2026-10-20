#!/usr/bin/env python3
"""Stand-in for `claude -p` in the tests.

Records its argv, cwd and selected environment to $FAKE_CLAUDE_LOG (one JSON line per call) and tries
a push, which the pre-push hook must reject.

Stage sessions (PIPELINE_STAGE set) write $PIPELINE_RESULT_FILE from FAKE_RESULTS, a path to a JSON
file {"<stage>": [result for the 1st visit, 2nd visit, …]} (the last entry repeats; default
{"verdict": "READY", "summary": "ok"}). Fake-only keys in an entry:
  _commit: "<path>"     write and commit that file in the worktree ({ticket} in it: the ticket id)
  _dirty: "<path>"      leave that file uncommitted
  _plan: "<markdown>"   write the plan (a default plan is written when none exists yet)
  _artifacts: {...}     write these files ({"<name>": "<text>"}) under $PIPELINE_ARTIFACTS_DIR
  _no_result: true      write no result file
  _verdict_line: "X"    write no result file; end the final message with `VERDICT: X`
  _sleep: <seconds>     pause after the init event, silent (watch, stop, nudge and quiet tests; not when resumed)
  _sleep_resumed: true  pause with _sleep in a resumed session too (retries that run out)
  _no_end: true         exit without a result event (a crash)
  _structured: true     write no result file; the answer is the final result event's structured_output
  _rate_limit: {...}    emit a rate_limit_event with this rate_limit_info after the init event
  _tools: [{...}]       (not when resumed) right after the init event, Bash tool calls, each {"command": "...",
                        "heartbeats": [elapsed seconds, …] (a tool_progress heartbeat each, `every` (0.5) seconds
                        apart), "hold": <s> (then wait), "returns": true (then its tool_result; false: left open)}
  _rejected_in: <s>     (not when resumed) hit the 5-hour limit: a `rejected` rate_limit_event resetting in that many
                        seconds, then the CLI's ending (a rate_limit turn error, an error result "You've hit your session
                        limit · …", exit 1); no result file
  _limit_text: "..."    (not when resumed) end on an error result with this text and nothing else (no rate-limit
                        event, no 429): the text fallback
SIGINT (a nudge, the timeout, a quiet stage) ends it at once without a result event, like claude ending its turn.
Without PIPELINE_STAGE and with --json-schema it is the router: FAKE_ROUTE (JSON) is its answer.
Output is stream-json, ending in two result events (cost is cumulative)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(0))
argv = sys.argv[1:]
if argv == ["--help"]:
    print("  --max-budget-usd <amount>  Maximum dollar amount to spend on API calls")
    print("  --json-schema <schema>     JSON Schema for structured output validation")
    print("  --effort <level>           Effort level for the session (low, medium, high, max)")
    sys.exit(0)
prompt = argv[argv.index("-p") + 1]
stage = os.environ.get("PIPELINE_STAGE")
log_path = os.environ["FAKE_CLAUDE_LOG"]
if not stage:  # the router: one structured answer, no tools
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": str(Path.cwd()), "router": True}) + "\n")
    answer = json.loads(os.environ.get("FAKE_ROUTE") or '{"type": "feat", "title": "Haftalık plan", "reason": "Yeni yetenek."}')
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": answer}))
    sys.exit(0)

resumed = "--resume" in argv
cwd = Path.cwd()
inbox = Path(os.environ["PIPELINE_INBOX"])
push = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/agent-push"], capture_output=True, text=True)
with open(log_path, "a", encoding="utf-8") as log:
    log.write(json.dumps({
        "argv": argv, "cwd": str(cwd), "stage": stage, "resumed": resumed, "prompt": prompt,
        "env": {k: os.environ.get(k) for k in ("ZIGGY_RUN", "TEST_DB_URI", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                                                "GIT_CONFIG_VALUE_0", "PIPELINE_RESULT_FILE", "PIPELINE_TICKET",
                                                "PIPELINE_BASE_COMMIT", "PIPELINE_PLAN", "PIPELINE_VISIT",
                                                "PIPELINE_LAST_VISIT", "PIPELINE_ARTIFACTS_DIR")},
        "push_exit": push.returncode, "push_stderr": push.stderr,
    }) + "\n")

# Which visit of this stage this is, over the ticket's sessions (a resumed session repeats its visit).
counter = inbox / ".fake-visits.json"
visits = json.loads(counter.read_text()) if counter.exists() else {}
if not resumed or stage not in visits:  # a session killed before it counted itself resumes as that visit
    visits[stage] = visits.get(stage, 0) + 1
counter.write_text(json.dumps(visits))
results_file = Path(os.environ.get("FAKE_RESULTS") or "/nonexistent")
script = json.loads(results_file.read_text()) if results_file.is_file() else {}
entries = script.get(stage) or [{"verdict": "READY", "summary": "ok"}]
entry = dict(entries[min(visits[stage], len(entries)) - 1])

session = argv[argv.index("--resume" if resumed else "--session-id") + 1]
print(json.dumps({"type": "system", "subtype": "init", "cwd": str(cwd), "model": "fake-model", "session_id": session}), flush=True)
for n, tool in enumerate(entry.get("_tools") or [] if not resumed else []):
    use_id = f"toolu_fake_{n}"
    print(json.dumps({"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": use_id, "name": "Bash", "input": {"command": tool["command"]}}]}}), flush=True)
    for elapsed in tool.get("heartbeats") or []:
        time.sleep(float(tool.get("every", 0.5)))
        print(json.dumps({"type": "tool_progress", "tool_use_id": f"{use_id}-heartbeat-0", "tool_name": "Bash",
                          "parent_tool_use_id": use_id, "elapsed_time_seconds": elapsed, "heartbeat": True,
                          "session_id": session}), flush=True)
    time.sleep(float(tool.get("hold", 0)))
    if tool.get("returns", True):
        print(json.dumps({"type": "user", "session_id": session, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use_id, "content": "done"}]}}), flush=True)
if entry.get("_sleep") and (not resumed or entry.get("_sleep_resumed")):
    if entry.get("_dirty"):
        (cwd / entry["_dirty"]).write_text("half done\n")
    time.sleep(float(entry["_sleep"]))
if entry.get("_rate_limit"):
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": entry["_rate_limit"], "session_id": session}), flush=True)
if not resumed and ("_rejected_in" in entry or "_limit_text" in entry):
    if "_rejected_in" in entry:
        resets = int(time.time() + float(entry["_rejected_in"]))
        info = {"status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour", "overageStatus": "rejected",
                "isUsingOverage": False, "unifiedWindows": {"five_hour": {"utilization": 1.03, "resetsAt": resets},
                                                            "seven_day": {"utilization": 0.37, "resetsAt": resets + 86400}}}
        text = "You've hit your session limit · resets 8pm (Europe/Istanbul)"
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": info, "session_id": session}), flush=True)
        print(json.dumps({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": text}]}}), flush=True)
        end = {"is_error": True, "result": text, "terminal_reason": "api_error", "api_error_status": 429}
    else:
        end = {"is_error": True, "result": entry["_limit_text"], "terminal_reason": "api_error"}
    print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.1, "duration_ms": 50, "num_turns": 2,
                      "session_id": session, "permission_denials": [], **end}), flush=True)
    sys.exit(1)

plan = inbox / "plan.md"
if "_plan" in entry:
    plan.write_text(entry["_plan"])
elif not plan.exists():
    plan.write_text("## Goal & acceptance\n\nDo the ticket.\n\n- AC-1: it works\n\nRisk: low · Size: small\n\n"
                    "## Plan\n\n### S1: change\n- [ ] T001 [S1] change it (README.md)\n\n"
                    "## Verification\n\n- [ ] V001 [AC-1] run the tests\n")
if entry.get("_commit"):
    rel = entry["_commit"].replace("{ticket}", os.environ["PIPELINE_TICKET"])
    path = cwd / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{stage} visit {visits[stage]}\n")
    subprocess.run(["git", "add", "--", rel], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat({os.environ['PIPELINE_TICKET']}): {stage} {visits[stage]}"],
                   check=True, capture_output=True)
    if plan.exists():
        plan.write_text(plan.read_text().replace("- [ ] T001", "- [x] T001"))
for name, text in (entry.get("_artifacts") or {}).items():
    Path(os.environ["PIPELINE_ARTIFACTS_DIR"], name).write_text(text)
if entry.get("_dirty") and not entry.get("_sleep"):
    (cwd / entry["_dirty"]).write_text("left over\n")
if entry.get("_no_end"):
    sys.exit(1)

final = f"{stage} finished."
result = {k: v for k, v in entry.items() if not k.startswith("_")}
if entry.get("_verdict_line"):
    final = f"Did the work.\nVERDICT: {entry['_verdict_line']}"
elif not entry.get("_no_result") and not entry.get("_structured"):
    Path(os.environ["PIPELINE_RESULT_FILE"]).write_text(json.dumps(result))

events = [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(cwd / "README.md")}}]}},
    {"type": "system", "subtype": "task_started", "description": "Investigate pins", "subagent_type": "general-purpose"},
    {"type": "assistant", "parent_tool_use_id": "toolu_x", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "grep -rn pymongo ."}}]}},
    {"type": "system", "subtype": "task_notification", "status": "completed", "summary": "Found three pins"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "1 failed"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}},
    {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.75, "duration_ms": 400, "num_turns": 3,
     "session_id": session, "terminal_reason": "completed", "permission_denials": [],
     "modelUsage": {"fake-model": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 300, "cacheCreationInputTokens": 40, "costUSD": 1.25}}, "result": "background agent running"},
    {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.25, "duration_ms": 600, "num_turns": 4,
     "session_id": session, "terminal_reason": "completed", "permission_denials": [],
     "modelUsage": {"fake-model": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 300, "cacheCreationInputTokens": 40, "costUSD": 1.25}},
     "result": final, **({"structured_output": result} if entry.get("_structured") else {})},
]
for event in events:
    print(json.dumps(event), flush=True)
