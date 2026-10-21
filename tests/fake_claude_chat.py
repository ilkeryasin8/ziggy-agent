#!/usr/bin/env python3
"""Stand-in for a chat turn's `claude -p` (tests/test_chat.py).

Every call appends {argv, cwd, env subset} to $FAKE_CHAT_LOG and plays the next entry of the JSON list
in $FAKE_CHAT_SCRIPT (the last entry repeats). Entry keys (all optional):
  text        the answer (default "answer N")
  cost        the session's running total reported in the result
  sleep       seconds to wait after the init event (Stop and timeout tests)
  hold        seconds to wait after the text deltas, before the full message (draft tests)
  error       the result reports is_error with this text
  no_result   exit 1 without a result event, writing "boom" to stderr
  write       a file name: the turn writes it in its working directory (what the sandbox still allows)
  commit      a file name: the turn writes and commits it
With --include-partial-messages the answer streams as text deltas first (plus one subagent delta that
must not reach the draft), like claude 2.x."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
if argv == ["--help"]:
    print("  --include-partial-messages  Include partial message chunks as they arrive")
    sys.exit(0)
if argv == ["--version"]:  # the engine's version gate asks first; not a turn
    print("2.1.305 (Claude Code)")
    sys.exit(0)

log = Path(os.environ["FAKE_CHAT_LOG"])
calls = log.read_text().count("\n") if log.exists() else 0
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(),
                        "env": {k: os.environ.get(k) for k in ("PIPELINE_STAGE", "PIPELINE_TICKET", "ZIGGY_RUN",
                                                               "GIT_OPTIONAL_LOCKS", "GIT_CONFIG_KEY_0")}}) + "\n")
script = json.loads(Path(os.environ["FAKE_CHAT_SCRIPT"]).read_text()) if os.environ.get("FAKE_CHAT_SCRIPT") else [{}]
entry = script[min(calls, len(script) - 1)]
session = argv[argv.index("--resume" if "--resume" in argv else "--session-id") + 1]


def emit(event):
    print(json.dumps({**event, "session_id": session}), flush=True)


emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "model": "fake-model",
      # FAKE_CHAT_MCP: the MCP server names the login has (a JSON list), as a real init event lists them
      **({"mcp_servers": [{"name": n, "status": "connected"} for n in json.loads(os.environ["FAKE_CHAT_MCP"])]}
         if os.environ.get("FAKE_CHAT_MCP") else {})})
if entry.get("sleep"):
    time.sleep(float(entry["sleep"]))
if entry.get("write"):
    Path(entry["write"]).write_text("chat was here\n")
if entry.get("commit"):
    Path(entry["commit"]).write_text("committed by chat\n")
    subprocess.run(["git", "add", entry["commit"]], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "chat commit"], check=True, capture_output=True)
if entry.get("no_result"):
    print("boom", file=sys.stderr)
    sys.exit(1)

emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "git log --oneline"}}]}})
emit({"type": "user", "parent_tool_use_id": None, "message": {"content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "fatal"}]}})
text = entry.get("text", f"answer {calls + 1}")
if "--include-partial-messages" in argv:
    emit({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start", "message": {}}})
    emit({"type": "stream_event", "parent_tool_use_id": "toolu_9",
          "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "SUBAGENT"}}})
    for word in text.split(" "):
        emit({"type": "stream_event", "parent_tool_use_id": None,
              "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": word + " "}}})
    emit({"type": "stream_event", "parent_tool_use_id": None,
          "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{"}}})
    if entry.get("hold"):
        time.sleep(float(entry["hold"]))
emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": text}]}})
if "--include-partial-messages" in argv:
    emit({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_stop"}})
emit({"type": "result", "subtype": "error_during_execution" if entry.get("error") else "success",
      "is_error": bool(entry.get("error")), "result": entry.get("error") or text,
      "total_cost_usd": entry.get("cost"), "num_turns": 2, "duration_ms": 1200})
