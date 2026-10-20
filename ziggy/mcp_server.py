"""`ziggy mcp`: a Model Context Protocol server over stdio, so another Claude Code or Claude Desktop session can read and
drive Ziggy.

It is a client of the running panel (dashboard.py), not a second engine. The panel is the one place that knows how to
start a job, queue it for a run slot, refuse an action the ticket's state does not allow; going through its HTTP API
means a tool call does exactly what the button does, with the same refusals, and nothing here can drift from it. The
panel being down is the one failure of its own: the tools then say how to start it.

Transport: JSON-RPC 2.0, one JSON message per line on stdin and stdout; everything else (logs) goes to stderr, because
a stray byte on stdout breaks the client's parser. A tool failure is a result with `isError`, which the model can read
and act on; JSON-RPC errors are only for protocol faults (bad JSON, unknown method).

The write tools change real work (they start agents that spend quota, open PRs), so each needs `confirm: true`, which
the model is told to set only after the user agreed. `--read-only` removes them from `tools/list` and refuses them if
called anyway. Output is kept small: long fields are cut, since the result lands in another session's context.
"""
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TIMEOUT_S = 30
CUT = 600  # characters of one long field (a question, an error, an activity line)
LIST_MAX = 100
TYPES = ("feat", "fix", "refactor", "chore", "docs", "project")

INSTRUCTIONS = (
    "Ziggy turns a ticket (a title and a markdown body) into a pull request: it makes a git worktree, runs a pipeline of "
    "sandboxed Claude stages (plan, implement, review) and opens a draft PR. A ticket waits on the user for a question "
    "round, for approval of its plan, or after a failure. Use the read tools freely (ziggy_status first). Every tool "
    "that changes something requires confirm: true; set it only after the user has agreed to that specific action, "
    "because it starts agents that spend quota or opens a PR. Nothing here merges a PR.")

READ_ONLY_HINT = {"readOnlyHint": True}


class ToolError(Exception):
    """A tool failed in a way the model should read; becomes an isError result."""


# --- the panel -------------------------------------------------------------------------------

class Panel:
    """The panel's HTTP API as do_POST/do_GET want it: Host 127.0.0.1:<port>, JSON posts, no Sec-Fetch-Site."""

    def __init__(self, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Host": f"127.0.0.1:{self.port}", **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as exc:  # the panel's own refusal: {"error": "..."} with a 4xx/5xx
            raw = exc.read()
            try:
                message = json.loads(raw).get("error") or raw.decode(errors="replace")
            except (ValueError, AttributeError):
                message = raw.decode(errors="replace")
            raise ToolError(f"the panel refused: {message} (HTTP {exc.code})") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"cannot reach the Ziggy panel at {self.base} ({getattr(exc, 'reason', exc)}). Ask the user to "
                            "start it: `ziggy ui` in a terminal, or `launchctl kickstart gui/$(id -u)/local.ziggy.ui` "
                            "if it is installed in launchd (`ziggy ui --install`).") from None
        except ValueError:
            raise ToolError(f"{self.base} did not answer like the Ziggy panel (not JSON); is the port right?") from None


# --- shaping output --------------------------------------------------------------------------

def cut(value, limit: int = CUT):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"… [+{len(value) - limit} chars]"


def text_result(summary: str, data=None) -> dict:
    text = summary if data is None else f"{summary}\n\n{json.dumps(data, ensure_ascii=False, separators=(',', ':'))}"
    return {"content": [{"type": "text", "text": text}]}


def error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _ticket_line(t: dict) -> str:
    bits = [t["status"] + (" (running)" if t.get("running") else ""), f"stage {t['stage']}" if t.get("stage") else "",
            f"PR {t['pr']}" if t.get("pr") else ""]
    return f"{t['target']}/{t['id']}: {cut(t.get('title') or '', 90)} — " + ", ".join(b for b in bits if b)


def _brief(t: dict) -> dict:
    return {"target": t["target"], "id": t["id"], "title": cut(t.get("title"), 120), "status": t["status"], "stage": t.get("stage"),
            "running": bool(t.get("running")), "pr": t.get("pr"), "archived": bool(t.get("archived"))}


def _need(t: dict) -> dict:
    n = t.get("needs_you") or {}
    return {"target": t["target"], "id": t["id"], "title": cut(t.get("title"), 120), "kind": n.get("kind"),
            "severity": n.get("severity"), "what": cut(n.get("title"), 200)}


def _find(snapshot: dict, target: str, ticket_id: str) -> dict:
    for t in snapshot["tickets"]:
        if t["target"] == target and t["id"] == ticket_id:
            return t
    raise ToolError(f"no ticket {target}/{ticket_id}; ziggy_list_tickets shows what exists")


# --- read tools ------------------------------------------------------------------------------

def tool_status(panel: Panel, args: dict) -> dict:
    snap = panel.call("GET", "/api/status")
    by_id = {(t["target"], t["id"]): t for t in snap["tickets"]}
    board = snap.get("board") or {}
    waiting = [_need(by_id[(e["target"], e["id"])]) for e in board.get("waiting_on_you", []) if (e["target"], e["id"]) in by_id]
    targets = [{"name": t["name"], "mode": t.get("mode"), "slots": f"{len(t.get('active') or [])}/{t.get('max_active')}",
                **({"error": cut(t["error"])} if t.get("error") else {})} for t in snap["targets"]]
    running = [_brief(t) for t in snap["tickets"] if t.get("running")]
    counts = {group: len(entries) for group, entries in board.items()}
    out = {"targets": targets, "board": counts, "waiting_on_you": waiting, "running": running,
           "cost_today_usd": snap.get("cost_today_usd")}
    lines = [f"{len(targets)} target(s), {len(running)} running, {len(waiting)} waiting on you."]
    lines += [f"- {w['target']}/{w['id']} {w['kind']}: {w['title']}" for w in waiting]
    return text_result("\n".join(lines), out)


def tool_list_tickets(panel: Panel, args: dict) -> dict:
    snap = panel.call("GET", "/api/status")
    rows = snap["tickets"]
    if args.get("target"):
        rows = [t for t in rows if t["target"] == args["target"]]
    if args.get("status"):
        rows = [t for t in rows if t["status"] == args["status"]]
    if not args.get("include_archived"):
        rows = [t for t in rows if not t.get("archived")]
    total = len(rows)
    rows = rows[:LIST_MAX]  # the panel sorts newest first
    summary = f"{total} ticket(s)" + (f", showing the newest {LIST_MAX}" if total > LIST_MAX else "") + "."
    return text_result("\n".join([summary, *map(_ticket_line, rows)]), [_brief(t) for t in rows])


def tool_get_ticket(panel: Panel, args: dict) -> dict:
    target, ticket_id = _need_args(args, "target", "id")
    d = panel.call("GET", f"/api/ticket/{quote(target, safe='')}/{quote(ticket_id, safe='')}")
    rnd = d.get("round")
    question_round = None
    if rnd:
        question_round = {"n": rnd.get("n"), "questions": [
            {"id": q.get("id") or f"Q{i + 1}", "text": cut(q.get("text")), "options": [cut(o, 200) for o in (q.get("options") or [])[:8]],
             "recommendation": cut(q.get("recommendation"), 200)} for i, q in enumerate(rnd.get("questions") or [])]}
    gate = d.get("gate")
    out = {
        "target": d["target"], "id": d["id"], "title": d.get("title"), "type": d.get("type"), "status": d["status"],
        "stage": d.get("stage"), "running": bool(d.get("running")), "open_round": question_round,
        "approval_gate": {k: cut(v, 400) for k, v in gate.items()} if isinstance(gate, dict) else gate,
        "error": cut(d.get("error"), 800), "pr": d.get("pr"), "branch": d.get("branch"), "worktree": d.get("worktree"),
        "cost_usd": d.get("cost_usd"), "activity": cut(d.get("activity"), 300), "needs_you": _need(d)["what"] if d.get("needs_you") else None,
        "updated_at": d.get("updated_at"),
    }
    out = {k: v for k, v in out.items() if v not in (None, "", [])}
    lines = [_ticket_line(d)]
    if question_round:
        lines.append(f"Open question round {question_round['n']} with {len(question_round['questions'])} question(s): answer with ziggy_answer.")
    if gate:
        lines.append("Waiting at the approval gate: ziggy_approve continues, ziggy_answer with notes changes the plan.")
    if d.get("error"):
        lines.append("Last error: " + cut(d["error"], 300))
    return text_result("\n".join(lines), out)


# --- write tools -----------------------------------------------------------------------------

def _need_args(args: dict, *names: str) -> list[str]:
    missing = [n for n in names if not isinstance(args.get(n), str) or not args[n].strip()]
    if missing:
        raise ToolError(f"missing or empty argument: {', '.join(missing)}")
    return [args[n].strip() for n in names]


def _action(panel: Panel, args: dict, action: str, text: str = "", **extra) -> dict:
    target, ticket_id = _need_args(args, "target", "id")
    return panel.call("POST", "/api/action", {"target": target, "id": ticket_id, "action": action, "text": text, **extra})


def _job_note(out: dict) -> str:
    job = out.get("job") or {}
    return " It is queued: every run slot of the project is busy." if job.get("queued") else ""


def tool_create_ticket(panel: Panel, args: dict) -> dict:
    target, title = _need_args(args, "target", "title")
    kind = args.get("type") or "feat"
    if kind not in TYPES:
        raise ToolError(f"type must be one of {', '.join(TYPES)}")
    out = panel.call("POST", "/api/new", {"target": target, "title": title, "type": kind, "body": str(args.get("body") or ""),
                                          "plan_checkpoint": bool(args.get("plan_checkpoint")), "run": bool(args.get("start"))})
    t = out["ticket"]
    started = " Started." + _job_note(out) if out.get("job") else " Not started: ziggy_run starts it."
    return text_result(f"Created {target}/{t['id']}: {cut(t.get('title'), 120)}.{started}", {"id": t["id"], "branch": t.get("branch")})


def tool_run(panel: Panel, args: dict) -> dict:
    out = _action(panel, args, "run")
    return text_result(f"Run started for {args['target']}/{args['id']}.{_job_note(out)} Check progress with ziggy_get_ticket.")


def _answer_text(args: dict) -> str:
    """The text the panel's answer form sends: `Q1: …` blocks, blank-line separated, then the free note."""
    answers, note = args.get("answers"), str(args.get("note") or "").strip()
    parts = []
    if isinstance(answers, dict):
        parts = [f"{qid}: {str(a).strip()}" for qid, a in answers.items() if str(a).strip()]
    elif isinstance(answers, list):
        for item in answers:
            if not isinstance(item, dict) or not str(item.get("id", "")).strip() or not str(item.get("answer", "")).strip():
                raise ToolError("each entry of answers needs an id and an answer")
            parts.append(f"{item['id']}: {str(item['answer']).strip()}")
    elif answers is not None:
        raise ToolError("answers must be an object {\"Q1\": \"…\"} or a list of {id, answer}")
    if note:
        parts.append(note)
    if not parts:
        raise ToolError("nothing to send: give answers (per question id) and/or a note")
    return "\n\n".join(parts)


def tool_answer(panel: Panel, args: dict) -> dict:
    out = _action(panel, args, "answer", _answer_text(args))
    return text_result(f"Answer recorded for {args['target']}/{args['id']}; the run continues.{_job_note(out)}")


def tool_approve(panel: Panel, args: dict) -> dict:
    out = _action(panel, args, "approve")
    return text_result(f"Plan approved for {args['target']}/{args['id']}; the run continues.{_job_note(out)}")


def tool_nudge(panel: Panel, args: dict) -> dict:
    (text,) = _need_args(args, "text")
    out = _action(panel, args, "nudge", text)
    nudged = out.get("nudged") or {}
    return text_result(f"Nudge sent to {args['target']}/{args['id']} ({nudged.get('delivery', 'sent')}).", {"nudged": nudged})


def tool_stop(panel: Panel, args: dict) -> dict:
    out = _action(panel, args, "stop")
    return text_result(f"Stop requested for {args['target']}/{args['id']} ({out.get('stopped')}).")


def tool_publish(panel: Panel, args: dict) -> dict:
    out = _action(panel, args, "publish", "foreign-ok" if args.get("foreign_ok") else "")
    return text_result(f"Publishing {args['target']}/{args['id']}: pushing the branch and opening a draft PR.{_job_note(out)} "
                       "ziggy_get_ticket shows the PR link once it exists.")


# --- the tool table --------------------------------------------------------------------------

def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TARGET = {"type": "string", "description": "Project name as ziggy_status lists it."}
TICKET = {"type": "string", "description": "Ticket id such as local-12."}
CONFIRM = {"type": "boolean", "description": "Must be true; set only after the user agreed to this action."}
REF = {"target": TARGET, "id": TICKET, "confirm": CONFIRM}

TOOLS = [
    {"name": "ziggy_status", "write": False, "handler": tool_status,
     "description": "Start here. Overview of Ziggy: projects (mode, run slots in use), board counts, the tickets waiting on the "
                    "user (open questions, plans to approve, failures) with what each needs, and the running tickets.",
     "inputSchema": _schema({}, [])},
    {"name": "ziggy_list_tickets", "write": False, "handler": tool_list_tickets,
     "description": "List tickets, newest first: id, project, title, status, stage, whether it runs, PR link. Use to find a "
                    "ticket id or to see what exists; filter by project and status.",
     "inputSchema": _schema({"target": TARGET, "status": {"type": "string", "description": "Only this status, e.g. needs-user, "
                            "awaiting-approval, running, done, failed."},
                            "include_archived": {"type": "boolean", "description": "Include archived tickets (default false)."}}, [])},
    {"name": "ziggy_get_ticket", "write": False, "handler": tool_get_ticket,
     "description": "One ticket in detail: status, stage, the open question round (question texts and options, to answer with "
                    "ziggy_answer), the approval gate, last error, PR, branch, worktree, cost and the latest activity line.",
     "inputSchema": _schema({"target": TARGET, "id": TICKET}, ["target", "id"])},
    {"name": "ziggy_create_ticket", "write": True, "handler": tool_create_ticket,
     "description": "Create a ticket for a project. Write the body as markdown: what to build, constraints, acceptance criteria. "
                    "With start true the pipeline begins at once (spends quota); otherwise start it later with ziggy_run.",
     "inputSchema": _schema({"target": TARGET, "title": {"type": "string", "description": "One line, imperative."},
                             "body": {"type": "string", "description": "Markdown description."},
                             "type": {"type": "string", "enum": list(TYPES), "description": "Default feat."},
                             "start": {"type": "boolean", "description": "Start the run now (default false)."},
                             "plan_checkpoint": {"type": "boolean", "description": "Stop for the user's approval of the plan before coding."},
                             "confirm": CONFIRM}, ["target", "title", "confirm"])},
    {"name": "ziggy_run", "write": True, "handler": tool_run,
     "description": "Start a ticket, or retry a failed or interrupted one. Refused if it is done, waiting for an answer or "
                    "approval, or already running.",
     "inputSchema": _schema(REF, ["target", "id", "confirm"])},
    {"name": "ziggy_answer", "write": True, "handler": tool_answer,
     "description": "Answer the open question round of a ticket in needs-user (read the questions with ziggy_get_ticket first), "
                    "or send change notes for a plan at the approval gate, or guidance to a failed ticket. The run continues.",
     "inputSchema": _schema({**REF, "answers": {"type": "object", "additionalProperties": {"type": "string"},
                             "description": "Answers keyed by question id, e.g. {\"Q1\": \"use option A\"}. Unanswered questions "
                             "are decided by the agent."},
                             "note": {"type": "string", "description": "Free-form note not tied to a question."}},
                            ["target", "id", "confirm"])},
    {"name": "ziggy_approve", "write": True, "handler": tool_approve,
     "description": "Approve the plan of a ticket in awaiting-approval so the run continues to implementation.",
     "inputSchema": _schema(REF, ["target", "id", "confirm"])},
    {"name": "ziggy_nudge", "write": True, "handler": tool_nudge,
     "description": "Steer a running ticket: interrupt its current stage and resume it with this message (a stage not started yet "
                    "gets it in its prompt).",
     "inputSchema": _schema({**REF, "text": {"type": "string", "description": "What the agent should do differently."}},
                            ["target", "id", "text", "confirm"])},
    {"name": "ziggy_stop", "write": True, "handler": tool_stop,
     "description": "Stop the ticket's running session (or take it out of the queue). The ticket can be resumed or run again.",
     "inputSchema": _schema(REF, ["target", "id", "confirm"])},
    {"name": "ziggy_publish", "write": True, "handler": tool_publish,
     "description": "Push a done ticket's branch and open a draft PR. Never merges. A PR someone else opened needs foreign_ok.",
     "inputSchema": _schema({**REF, "foreign_ok": {"type": "boolean", "description": "Push to a PR another author opened."}},
                            ["target", "id", "confirm"])},
]
for _t in TOOLS:
    _t["annotations"] = READ_ONLY_HINT if not _t["write"] else {"readOnlyHint": False, "destructiveHint": False}


# --- protocol --------------------------------------------------------------------------------

class Server:
    def __init__(self, port: int, read_only: bool):
        self.panel = Panel(port)
        self.read_only = read_only

    def visible(self) -> list[dict]:
        return [t for t in TOOLS if not (t["write"] and self.read_only)]

    def tools_list(self) -> dict:
        return {"tools": [{k: t[k] for k in ("name", "description", "inputSchema", "annotations")} for t in self.visible()]}

    def tools_call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return error_result(f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return error_result("arguments must be an object")
        if tool["write"]:
            if self.read_only:
                return error_result(f"{name} is disabled: ziggy mcp runs with --read-only.")
            if args.get("confirm") is not True:
                return error_result(f"{name} changes things. Ask the user to confirm this exact action first, then call again with confirm: true.")
        try:
            return tool["handler"](self.panel, args)
        except ToolError as exc:
            return error_result(str(exc))
        except (KeyError, TypeError) as exc:  # a panel answer of a shape this version does not know
            return error_result(f"unexpected answer from the panel ({type(exc).__name__}: {exc}); is it the same Ziggy version?")

    def handle(self, message) -> dict | None:
        """One decoded message → the reply, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _fault(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
        method, params, mid = message["method"], message.get("params") or {}, message.get("id")
        if "id" not in message:
            return None  # notifications (notifications/initialized, cancelled, …) never get a reply
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return _ok(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {}},
                             "serverInfo": {"name": "ziggy", "version": VERSION}, "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, self.tools_list())
        if method == "tools/call":
            if not isinstance(params, dict):
                return _fault(mid, -32602, "params must be an object")
            return _ok(mid, self.tools_call(params))
        return _fault(mid, -32601, f"method not found: {method}")


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _fault(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(port: int, read_only: bool, stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(port, read_only)
    print(f"ziggy mcp: panel http://127.0.0.1:{port}" + (", read-only" if read_only else ""), file=sys.stderr, flush=True)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _fault(None, -32700, "parse error")
        else:
            try:
                reply = server.handle(message)
            except Exception as exc:  # noqa: BLE001 - a bug in one call must not end the session
                print(f"ziggy mcp: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                reply = _fault(message.get("id") if isinstance(message, dict) else None, -32603, f"internal error: {type(exc).__name__}")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0
