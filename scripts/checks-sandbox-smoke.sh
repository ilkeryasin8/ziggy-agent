#!/bin/bash
# Proves on this Mac that the sandbox-exec profile engine-run checks use (ziggy/checks.py) confines them:
# no internet, no writes outside the allowed roots, writes in the worktree work, a localhost listener is reachable.
# Usage: bash scripts/checks-sandbox-smoke.sh <worktree>
#   <worktree>: any directory standing in for a ticket worktree; one marker file is written and removed there.

set -u

PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -eq 1 ] || { echo "usage: bash scripts/checks-sandbox-smoke.sh <worktree>" >&2; exit 2; }
WT="$(cd "$1" 2>/dev/null && pwd)" || { echo "checks-sandbox-smoke: $1 is not a directory" >&2; exit 2; }
for tool in python3.12 curl /usr/bin/sandbox-exec; do
    command -v "$tool" >/dev/null || { echo "checks-sandbox-smoke: $tool not found" >&2; exit 2; }
done

STATE_DIR="$(mktemp -d /tmp/checks-sandbox-smoke.XXXXXX)" || exit 2
OUTSIDE="$HOME/.checks-sandbox-smoke-$$"
MARKER="$WT/.checks-sandbox-smoke-$$"
SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && { kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; }
    rm -rf "$STATE_DIR" "$OUTSIDE" "$MARKER"
}
trap cleanup EXIT

PROFILE="$(cd "$PIPELINE_ROOT" && python3.12 -c '
import sys
from pathlib import Path
from ziggy import checks
print(checks.profile(Path(sys.argv[1]), Path(sys.argv[2])))' "$WT" "$STATE_DIR")" || exit 2

PORT="$(python3.12 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
python3.12 -m http.server "$PORT" --bind 127.0.0.1 --directory "$STATE_DIR" >/dev/null 2>&1 &
SERVER_PID=$!
for _ in $(seq 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done

sandboxed() { (cd "$WT" && /usr/bin/sandbox-exec -p "$PROFILE" bash -c "set -eo pipefail
$1") >/dev/null 2>&1; }

FAILED=0
expect() {  # expect <pass|fail> <label> <command>
    if sandboxed "$3"; then got=pass; else got=fail; fi
    if [ "$got" = "$1" ]; then echo "ok    $2 ($got)"; else echo "FAIL  $2 (expected $1, got $got)"; FAILED=1; fi
}

expect fail "curl https://github.com is blocked" "curl -sS -m 10 -o /dev/null https://github.com"
expect fail "writing \$HOME/x is blocked" "touch '$OUTSIDE'"
[ -e "$OUTSIDE" ] && { echo "FAIL  $OUTSIDE exists after the sandboxed write"; FAILED=1; }
expect pass "writing in the worktree works" "touch '$MARKER'"
expect pass "localhost listener is reachable" "curl -sS -m 10 -o /dev/null http://localhost:$PORT/"

[ "$FAILED" = 0 ] && echo "checks sandbox: all good" || echo "checks sandbox: FAILED"
exit "$FAILED"
