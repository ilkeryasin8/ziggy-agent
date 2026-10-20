"""`ziggy mcp`: the stdio MCP server, run as a real subprocess against an in-process panel (the fake claude and gh stand
behind it), speaking JSON-RPC over its pipes."""
import json
import socket
import subprocess
import sys

import pytest
from conftest import BIN
from test_dashboard import panel, request  # noqa: F401 - the fixture

WRITE = {"ziggy_create_ticket", "ziggy_run", "ziggy_answer", "ziggy_approve", "ziggy_nudge", "ziggy_stop", "ziggy_publish"}
READ = {"ziggy_status", "ziggy_list_tickets", "ziggy_get_ticket"}


class Client:
    def __init__(self, port, *flags):
        self.proc = subprocess.Popen([sys.executable, str(BIN), "mcp", "--port", str(port), *flags], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.n = 0

    def raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def rpc(self, method, params=None):
        self.n += 1
        return self.raw(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})}))

    def tool(self, name, **arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()


@pytest.fixture
def mcp(panel):  # noqa: F811
    clients = []

    def start(*flags, port=None):
        c = Client(port or panel.server_address[1], *flags)
        clients.append(c)
        return c
    yield start
    for c in clients:
        c.close()


def text(result):
    return result["content"][0]["text"]


def test_initialize_and_tool_list(mcp):
    c = mcp()
    reply = c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert reply["result"]["serverInfo"]["name"] == "ziggy" and "confirm" in reply["result"]["instructions"]
    assert c.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    c.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")  # no reply
    assert c.rpc("ping")["result"] == {}  # the next line out is ping's, so the notification got none
    tools = c.rpc("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == READ | WRITE
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools)


def test_read_only_hides_and_refuses_writes(mcp):
    c = mcp("--read-only")
    assert {t["name"] for t in c.rpc("tools/list")["result"]["tools"]} == READ
    out = c.tool("ziggy_create_ticket", target="demo", title="x", confirm=True)
    assert out["isError"] and "read-only" in text(out)


def test_status_and_list_against_the_panel(mcp, panel):  # noqa: F811
    request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: Sync pins", "body": "b"})
    c = mcp()
    status = c.tool("ziggy_status")
    assert "isError" not in status and "demo" in text(status)
    assert "Sync pins" in text(c.tool("ziggy_list_tickets", target="demo"))
    assert text(c.tool("ziggy_list_tickets", status="done")).startswith("0 ticket")
    detail = c.tool("ziggy_get_ticket", target="demo", id="local-1")
    assert "isError" not in detail and "Sync pins" in text(detail)
    assert c.tool("ziggy_get_ticket", target="demo", id="local-99")["isError"]


def test_create_needs_confirm_then_works(mcp, panel):  # noqa: F811
    c = mcp()
    refused = c.tool("ziggy_create_ticket", target="demo", title="Add a thing", body="Do it.")
    assert refused["isError"] and "confirm" in text(refused)
    _, snap, _ = request(panel, "GET", "/api/status")
    assert snap["tickets"] == []
    made = c.tool("ziggy_create_ticket", target="demo", title="Add a thing", body="Do it.", type="docs", confirm=True)
    assert "isError" not in made, text(made)
    _, snap, _ = request(panel, "GET", "/api/status")
    assert [(t["title"], t["type"]) for t in snap["tickets"]] == [("Add a thing", "docs")]
    assert c.tool("ziggy_create_ticket", target="demo", title="x", type="nope", confirm=True)["isError"]


def test_action_routes_to_the_panel_and_its_refusal_is_a_tool_error(mcp, panel):  # noqa: F811
    request(panel, "POST", "/api/new", {"target": "demo", "title": "chore: Sync pins", "body": "b"})
    c = mcp()
    out = c.tool("ziggy_stop", target="demo", id="local-1", confirm=True)
    assert out["isError"] and "no running session" in text(out)
    out = c.tool("ziggy_approve", target="demo", id="local-1", confirm=True)
    assert out["isError"] and "approval" in text(out)
    out = c.tool("ziggy_answer", target="demo", id="local-1", confirm=True, answers={"Q1": "a"})
    assert out["isError"] and "answer applies" in text(out)
    assert c.tool("ziggy_answer", target="demo", id="local-1", confirm=True)["isError"]  # nothing to send


def test_answer_text_matches_the_panel_form():
    from ziggy.mcp_server import _answer_text
    assert _answer_text({"answers": {"Q1": "A", "Q2": " "}, "note": "fyi"}) == "Q1: A\n\nfyi"
    assert _answer_text({"answers": [{"id": "Q2", "answer": "B"}]}) == "Q2: B"


def test_unreachable_panel_is_a_tool_error(mcp):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    out = mcp(port=free).tool("ziggy_status")
    assert out["isError"] and "ziggy ui" in text(out)


def test_protocol_errors(mcp):
    c = mcp()
    parse = c.raw("{not json")
    assert parse["error"]["code"] == -32700 and parse["id"] is None
    unknown = c.rpc("resources/list")
    assert unknown["error"]["code"] == -32601 and unknown["id"] == c.n
    assert c.tool("ziggy_nope")["isError"]
