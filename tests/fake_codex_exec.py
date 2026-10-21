#!/usr/bin/env python3
"""Stand-in for `codex exec` running a stage (dispatch.run_codex; tests/test_codex_stage.py).

Appends {argv, cwd, stage, prompt} to $FAKE_CODEX_EXEC_LOG, then plays the next entry for $PIPELINE_STAGE from the
JSON file $FAKE_RESULTS (the format tests/fake_claude.py reads; the last entry repeats): writes the entry (minus
underscore keys) to $PIPELINE_RESULT_FILE, `_commit` a file (written and committed), `_touch` a file (written, not
committed). Emits codex's --json events and writes the last message to --output-last-message."""
import json
import os
import subprocess
import sys
from pathlib import Path

argv = sys.argv[1:]
prompt = sys.stdin.read()
stage = os.environ.get("PIPELINE_STAGE", "")
log = Path(os.environ["FAKE_CODEX_EXEC_LOG"])
calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
visit = sum(1 for c in calls if c["stage"] == stage)
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stage": stage, "prompt": prompt}) + "\n")
script = json.loads(Path(os.environ["FAKE_RESULTS"]).read_text()) if os.environ.get("FAKE_RESULTS") else {}
entries = script.get(stage) or [{"verdict": "READY", "summary": f"{stage} by codex"}]
entry = entries[min(visit, len(entries) - 1)]
if entry.get("_commit"):
    Path(entry["_commit"]).write_text(f"{stage} by codex\n")
    subprocess.run(["git", "add", "--", entry["_commit"]], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"{stage} by codex"], check=True, capture_output=True)
if entry.get("_touch"):
    Path(entry["_touch"]).write_text("changed by codex\n")
result = {k: v for k, v in entry.items() if not k.startswith("_")}
Path(os.environ["PIPELINE_RESULT_FILE"]).write_text(json.dumps(result))
message = f"VERDICT: {result.get('verdict', 'READY')}"
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread-1"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": message}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}), flush=True)
Path(argv[argv.index("--output-last-message") + 1]).write_text(message)
