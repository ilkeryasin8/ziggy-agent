"""A ticket's deferred review findings (inbox/deferred.md) as entries that can become tickets.

The reviewer appends one `## <ticket> round <n> (<date>)` heading per round and one bullet per finding
(`- R2-1 [nit/low/bug] path:line — text. Evidence: … Why deferred: …`). Each bullet is an entry keyed by its
finding id (R2-1), or by round + a hash of its text when the reviewer wrote none. A ticket made from an entry
records `origin = {"kind": "deferred", "ticket": <id>, "entry": <key>}` in its ticket.json; that link is how
the entry shows as converted and why it cannot be converted twice.
"""

from __future__ import annotations

import hashlib
import re

from . import state
from .config import Target

KIND = "deferred"
TITLE_MAX = 100

_ROUND = re.compile(r"^#{2,6}\s+.*?\bround\s+(\d+)", re.I)
_HEADING = re.compile(r"^#{1,6}\s")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_FINDING = re.compile(r"^(R\d+-\d+)\s+(?:\[([^\]]*)\]\s*)?(.*)$", re.S)
_CUT = re.compile(r"\s+(?:Evidence|Why deferred|Öneri)\s*:", re.I)


def _title(rest: str) -> str:
    """The finding's claim: the text after `path — `, up to its Evidence/Why deferred part, one sentence."""
    head, sep, tail = rest.partition(" — ")
    claim = _CUT.split(tail if sep else head, maxsplit=1)[0].strip()
    claim = re.split(r"(?<=[.;])\s", claim, maxsplit=1)[0].rstrip(".;").strip() or rest.strip()
    claim = " ".join(claim.split())
    return claim if len(claim) <= TITLE_MAX else claim[: TITLE_MAX - 1].rstrip() + "…"


def parse(text: str) -> list[dict]:
    """deferred.md → [{key, round, finding, class, text, title, type}] in file order. A bullet's wrapped
    continuation lines belong to it; lines outside bullets are left out."""
    out: list[dict] = []
    round_no = None
    current: list[str] | None = None

    def close():
        if current is None:
            return
        body = "\n".join(current).strip()
        m = _FINDING.match(body)
        finding, cls, rest = (m[1], m[2] or "", m[3]) if m else (None, "", body)
        digest = hashlib.sha1(body.encode()).hexdigest()[:8]
        out.append({
            "key": finding or f"round{round_no or 0}-{digest}",
            "round": round_no,
            "finding": finding,
            "class": cls,
            "text": body,
            "title": _title(rest),
            # A bug the review let go is still a bug: fix. Risks, nits, style and the rest: chore.
            "type": "fix" if "bug" in cls.lower().split("/") else "chore",
        })

    for line in text.splitlines():
        bullet = _BULLET.match(line)
        if _HEADING.match(line):
            close()
            current = None
            m = _ROUND.match(line)
            round_no = int(m[1]) if m else None
        elif bullet:
            close()
            current = [bullet[1]]
        elif current is not None and line.strip():
            current.append(line.strip())
        elif current is not None:
            close()
            current = None
    close()
    seen: set[str] = set()
    for e in out:  # a finding id written twice keeps the first; later copies get a round-qualified key
        if e["key"] in seen:
            e["key"] = f"{e['key']}@round{e['round'] or 0}-{hashlib.sha1(e['text'].encode()).hexdigest()[:8]}"
        seen.add(e["key"])
    return out


def converted(target: Target, ticket_id: str) -> dict[str, str]:
    """{entry key: ticket id} for the target's tickets made from `ticket_id`'s deferred entries."""
    out: dict[str, str] = {}
    for other in state.ticket_ids(target):
        try:
            origin = state.load_ticket(target, other).get("origin") or {}
        except (OSError, ValueError):
            continue
        if origin.get("kind") == KIND and origin.get("ticket") == ticket_id and origin.get("entry"):
            out.setdefault(str(origin["entry"]), other)
    return out


def entries(target: Target, ticket_id: str) -> list[dict]:
    """The ticket's deferred entries, each with `ticket` (the ticket made from it, or None) and the
    composer draft (`body`: the entry plus where it came from)."""
    path = state.inbox_dir(target, ticket_id) / "deferred.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    links = converted(target, ticket_id)
    return [{**e, "ticket": links.get(e["key"]),
             "body": f"{e['text']}\n\nKaynak: {ticket_id} deferred (round {e['round'] if e['round'] is not None else '?'})"}
            for e in parse(text)]


def origin(target: Target, body) -> dict | None:
    """A new ticket's `origin` from the composer: None, or a checked deferred link. Refuses an entry that
    is not in that ticket's deferred.md (same target) or was already converted."""
    if not body:
        return None
    if not isinstance(body, dict) or body.get("kind") != KIND:
        raise ValueError("origin must be {kind: deferred, ticket, entry}")
    ticket_id, key = str(body.get("ticket") or ""), str(body.get("entry") or "")
    if ticket_id not in state.ticket_ids(target):
        raise LookupError(f"no ticket {ticket_id} in {target.name}")
    found = next((e for e in entries(target, ticket_id) if e["key"] == key), None)
    if found is None:
        raise LookupError(f"{ticket_id} has no deferred entry {key}")
    if found["ticket"]:
        raise ValueError(f"{ticket_id} deferred {key} is already ticket {found['ticket']}")
    return {"kind": KIND, "ticket": ticket_id, "entry": key}
