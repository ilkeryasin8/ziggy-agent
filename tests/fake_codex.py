#!/usr/bin/env python3
"""Stand-in for `codex review` in the review tests: records argv, cwd and the run-only variable to
$FAKE_CODEX_LOG and prints a review in Codex's format; FAKE_CODEX_FAIL=1 makes it fail."""
import json
import os
import sys

with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "run_env": os.environ.get("ZIGGY_RUN")}) + "\n")
if os.environ.get("FAKE_CODEX_FAIL"):
    print("codex: not logged in", file=sys.stderr)
    sys.exit(1)
print("""The change pins pymongo but leaves one caller on the old API.

Full review comments:

- [P1] Old cursor API still used — feature.txt:1
  The worker calls `find().count()`, removed in pymongo 4. Use `count_documents({})`.

- [P3] Missing test for the new pin — README.md:1
  Nothing checks the installed version. Add an import-time assertion.""")
