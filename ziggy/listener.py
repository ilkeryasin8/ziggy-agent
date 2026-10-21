"""Slack intake: turns messages in one private channel into tickets, runs them one at a time, and
reports each step in the message's thread.

Transport-free: `Listener.handle_event` takes the Events API `event` of a Socket Mode envelope and
`api` is anything with post/react/permalink, so tests drive it with a fake. Only the configured
member's own messages in the configured channel act; bot messages, edits and deletions never do.

    bmad: [!plan] [<type>:] <title>     top-level message; following lines are the ticket body
    approve                              thread reply: continue a ticket waiting at the approval gate
    retry                                thread reply: run a failed ticket again (retries the stage it failed in)
    nudge: <message>                     thread reply: steer the running stage (it resumes its session with the message)
    <anything else>                      thread reply to a ticket with open questions: the answer

With `[slack] forward`, the target's other tickets (panel, CLI, issues) are followed too: their questions, approvals
and failures are posted to the channel, one thread per ticket (`slack-threads.json`), and the same replies work there.
A ticket from the panel is not published from Slack when it is done: its draft PR stays your click, as on the panel.
"""
import html
import json
import queue
import re
import threading
from typing import Protocol

from . import attention, pipeline, publish, state, ticket
from .config import SlackConfig, Target

COMMAND = re.compile(r"^\s*bmad:\s*(?P<rest>.*)$", re.I | re.S)
NUDGE = re.compile(r"^\s*nudge:\s*(?P<text>.*)$", re.I | re.S)
SEEN_LIMIT = 1000
FORWARD = ("question", "approval", "failed", "lost", "protected")  # inbox kinds `forward` posts
FORWARD_EVERY = 30  # seconds between looks at the inbox


class SlackAPI(Protocol):
    def post(self, channel: str, thread_ts: str, text: str) -> None: ...
    def react(self, channel: str, ts: str, name: str) -> None: ...
    def permalink(self, channel: str, ts: str) -> str | None: ...
    def post_top(self, channel: str, text: str) -> str | None: ...  # a new message; its ts (the thread to reply in)


class CommandError(ValueError):
    pass


def parse_command(text: str) -> dict | None:
    """None when the message is not a pipeline command; CommandError when it is one but malformed."""
    match = COMMAND.match(html.unescape(text or ""))
    if not match:
        return None
    first, _, body = match["rest"].partition("\n")
    first = first.strip()
    plan_checkpoint = False
    if first.lower().startswith("!plan"):
        plan_checkpoint, first = True, first[5:].strip()
    try:
        ticket_type, title = ticket.split_type(first)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not title:
        raise CommandError("başlık boş; örnek: `bmad: fix: login timeout'u 30 sn yap`")
    return {"type": ticket_type, "title": title, "body": body.strip(), "plan_checkpoint": plan_checkpoint}


def _minutes(record: dict) -> str:
    ms = record.get("duration_ms")
    return f"{ms / 60000:.1f} dk" if ms else "?"


def _summary(record: dict) -> str:
    verdict = record.get("verdict") or (record.get("result") or {}).get("verdict") or record.get("state") or "failed"
    cost = record.get("total_cost_usd")
    cost_text = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?"
    return (f"{record.get('stage')}: *{verdict}* · {record.get('model')} · {cost_text} · "
            f"{record.get('num_turns') or '?'} tur · {_minutes(record)}")


def questions_text(round_: dict) -> str:
    """An open question round as a Slack message body."""
    lines = [round_["context"]] if round_.get("context") else []
    for i, q in enumerate(round_.get("questions") or [], 1):
        lines.append(f"{i}. {q.get('text')}")
        for option in q.get("options") or []:
            lines.append(f"   • {option}" + (" _(önerilen)_" if option == q.get("recommendation") else ""))
        if q.get("recommendation") and q["recommendation"] not in (q.get("options") or []):
            lines.append(f"   Önerilen: {q['recommendation']}")
    return "\n".join(lines)


class Listener:
    def __init__(self, target: Target, cfg: SlackConfig, api: SlackAPI):
        self.target = target
        self.cfg = cfg
        self.api = api
        self.jobs: queue.Queue = queue.Queue()
        self.busy: set[str] = set()
        self._lock = threading.Lock()
        self._seen_path = target.state_dir / "slack-seen.json"
        try:
            self._seen = list(json.loads(self._seen_path.read_text()))
        except (OSError, ValueError):
            self._seen = []
        self._threads_path = target.state_dir / "slack-threads.json"  # forwarded tickets: id -> their thread's ts
        self._forwarded_path = target.state_dir / "slack-forwarded.json"  # inbox rows already posted

    # -- intake ------------------------------------------------------------------------------

    def _first_time(self, keys: list[str]) -> bool:
        """Slack redelivers events it thinks were not acknowledged; act on each message once."""
        with self._lock:
            if any(k in self._seen for k in keys):
                return False
            self._seen = (self._seen + keys)[-SEEN_LIMIT:]
            self._seen_path.parent.mkdir(parents=True, exist_ok=True)
            state.write_json(self._seen_path, self._seen)
            return True

    def handle_event(self, event_id: str | None, event: dict) -> None:
        if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
            return
        if event.get("channel") != self.cfg.channel or event.get("user") != self.cfg.user:
            return
        keys = [k for k in (event_id and f"event:{event_id}", event.get("client_msg_id") and f"msg:{event['client_msg_id']}") if k]
        if not keys or not self._first_time(keys):
            return
        thread_ts = event.get("thread_ts")
        if thread_ts and thread_ts != event.get("ts"):
            self._reply(thread_ts, event.get("text") or "")
        else:
            self._command(event["ts"], event.get("text") or "")

    def _command(self, ts: str, text: str) -> None:
        try:
            parsed = parse_command(text)
        except CommandError as exc:
            self.api.post(self.cfg.channel, ts, f"Koşu başlamadı: {exc}")
            return
        if parsed is None:
            return
        try:
            t = ticket.create(
                self.target, parsed["title"], parsed["type"], parsed["body"], parsed["plan_checkpoint"], "low",
                slack={"channel": self.cfg.channel, "thread_ts": ts},
                source_ref=self.api.permalink(self.cfg.channel, ts),
                requester=self.cfg.user,
            )
        except (ValueError, OSError, state.LockError) as exc:
            self.api.post(self.cfg.channel, ts, f"Ticket açılamadı: {exc}")
            return
        self.api.react(self.cfg.channel, ts, "eyes")
        checkpoint = " · plan onayında duracak" if t["plan_checkpoint"] else ""
        self.api.post(self.cfg.channel, ts, f"`{t['id']}` alındı · dal `{t['branch']}`{checkpoint} · sıraya girdi")
        self._enqueue("run", t["id"])

    def _ticket_for_thread(self, thread_ts: str) -> str | None:
        for ticket_id in state.ticket_ids(self.target):
            slack = state.load_ticket(self.target, ticket_id).get("slack") or {}
            if slack.get("channel") == self.cfg.channel and slack.get("thread_ts") == thread_ts:
                return ticket_id
        return next((tid for tid, ts in self._threads().items() if ts == thread_ts), None)

    def _threads(self) -> dict:
        try:
            data = json.loads(self._threads_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _thread_of(self, ticket_id: str) -> str | None:
        slack = state.load_ticket(self.target, ticket_id).get("slack") or {}
        if slack.get("channel") == self.cfg.channel and slack.get("thread_ts"):
            return slack["thread_ts"]
        return self._threads().get(ticket_id)

    # -- forward (other tickets' inbox items) -----------------------------------------------

    def forward(self) -> list[str]:
        """Post the target's new questions, approvals and failures of tickets not started from Slack, each in its
        ticket's thread (opened on the first one); returns the ticket ids posted for. Off unless `forward`."""
        if not self.cfg.forward:
            return []
        try:
            done = set(json.loads(self._forwarded_path.read_text()))
        except (OSError, ValueError):
            done = set()
        posted = []
        for row in reversed(attention.items([self.target])):  # oldest first
            key = f"{row['ticket']}/{row['key']}/{row['created_at']}"
            if row["kind"] not in FORWARD or key in done:
                continue
            ticket_id = row["ticket"]
            try:
                t = state.load_ticket(self.target, ticket_id)
            except (OSError, ValueError, KeyError):
                continue
            if (t.get("slack") or {}).get("thread_ts"):
                done.add(key)  # a Slack ticket: its own thread already says it
                continue
            text = self._forward_text(t, row)
            thread = self._threads().get(ticket_id)
            if thread:
                self.api.post(self.cfg.channel, thread, text)
            else:
                ts = self.api.post_top(self.cfg.channel, text)
                if ts:
                    with self._lock:
                        threads = self._threads()
                        threads[ticket_id] = ts
                        state.write_json(self._threads_path, threads)
            done.add(key)
            posted.append(ticket_id)
        self._forwarded_path.parent.mkdir(parents=True, exist_ok=True)
        state.write_json(self._forwarded_path, sorted(done)[-SEEN_LIMIT:])
        return posted

    def _forward_text(self, t: dict, row: dict) -> str:
        st = state.load_state(self.target, t["id"])
        head = f"`{t['id']}` {t.get('title') or ''} · *{attention.LABEL.get(row['kind'], row.get('title') or row['kind'])}*"
        body = ""
        if row["kind"] == "question":
            round_ = pipeline.open_round(st)
            body = questions_text(round_) if round_ else ""
        elif row["kind"] == "approval":
            gate = st.get("gate") or {}
            body = f"{gate.get('stage') or '?'} sonrası: {gate.get('reason') or '-'}"
        elif st.get("error"):
            body = st["error"].split("\n")[0][:500]
        hint = ("Bu thread'e yaz: cevabın, `approve`, `retry` ya da `nudge: <mesaj>`. Ayrıntı panelde.")
        return "\n".join(x for x in (head, body, hint) if x)

    def _reply(self, thread_ts: str, text: str) -> None:
        ticket_id = self._ticket_for_thread(thread_ts)
        if ticket_id is None:
            return
        text = html.unescape(text).strip()
        nudge = NUDGE.match(text)
        if nudge:
            # Steering a running stage is the point, so this skips the busy check; it never waits in the queue.
            try:
                out = pipeline.nudge(self.target, ticket_id, nudge["text"], by=self.cfg.user, via="slack")
            except pipeline.PipelineError as exc:
                self.api.post(self.cfg.channel, thread_ts, f"Yönlendirilemedi: {exc}")
                return
            said = {"interrupt": f"{out.get('stage') or 'aşama'} durduruluyor; kendi oturumunda mesajınla devam edecek.",
                    "queued": f"mesajın kaydedildi; {out.get('stage') or 'sıradaki aşama'} başlayınca önce onu okuyacak."}
            self.api.post(self.cfg.channel, thread_ts, f"Tamam: {said.get(out.get('delivery'), out.get('message'))}")
            return
        if ticket_id in self.busy:
            self.api.post(self.cfg.channel, thread_ts, f"`{ticket_id}` şu an sırada ya da koşuyor; bitince tekrar yaz "
                                                       "(yön vermek için `nudge: <mesaj>`).")
            return
        status = state.load_state(self.target, ticket_id)["status"]
        word = text.lower()
        if word == "approve" and status == "awaiting-approval":
            self._enqueue("approve", ticket_id)
        elif word == "retry" and status == "failed":
            self._enqueue("run", ticket_id)
        elif status == "needs-user" and word not in ("approve", "retry") and text:
            self._enqueue("answer", ticket_id, text)
        else:
            self.api.post(self.cfg.channel, thread_ts, f"`{ticket_id}` durumu *{status}*; bu cevapla yapılacak bir şey yok.")
            return
        self.api.post(self.cfg.channel, thread_ts, "Tamam, sıraya girdi.")

    def _enqueue(self, kind: str, ticket_id: str, text: str = "") -> None:
        with self._lock:
            self.busy.add(ticket_id)
        self.jobs.put((kind, ticket_id, text))

    # -- worker ------------------------------------------------------------------------------

    def drain(self) -> None:
        """Process queued jobs until the queue is empty (the worker thread calls run_job forever)."""
        while not self.jobs.empty():
            self.run_job(*self.jobs.get())

    def serve_forever(self) -> None:
        while True:
            self.run_job(*self.jobs.get())

    def run_job(self, kind: str, ticket_id: str, text: str) -> None:
        thread = self._thread_of(ticket_id)
        say = lambda message: self.api.post(self.cfg.channel, thread, message)  # noqa: E731
        try:
            if kind == "answer":
                records = pipeline.answer(self.target, ticket_id, text, by=self.cfg.user, via="slack")
            elif kind == "approve":
                records = pipeline.approve(self.target, ticket_id)
            else:
                records = pipeline.run(self.target, ticket_id)
            for record in records if isinstance(records, list) else []:
                say(_summary(record))
            self._report(ticket_id, say)
        except Exception as exc:  # the listener must survive any single ticket's failure
            say(f"Hata: {exc}")
        finally:
            with self._lock:
                self.busy.discard(ticket_id)

    def _report(self, ticket_id: str, say) -> None:
        st = state.load_state(self.target, ticket_id)
        status = st["status"]
        if status == "done" and not (state.load_ticket(self.target, ticket_id).get("slack") or {}).get("thread_ts"):
            say(f"Bitti. Draft PR'ı panelden ya da `ziggy publish {ticket_id}` ile aç.")
        elif status == "done":
            try:
                url = publish.publish(self.target, ticket_id)
            except publish.PublishError as exc:
                say(f"Koşu bitti ama PR açılamadı: {exc}")
                return
            say(f"Draft PR: {url}\nReview edip ready'e alman ve merge etmen sende.")
        elif status == "awaiting-approval":
            gate = st.get("gate") or {}
            say(f"Plan onayını bekliyor ({gate.get('stage') or '?'} sonrası: {gate.get('reason') or '-'}).\n"
                "Devam etmesi için bu thread'e `approve` yaz; yön değiştirmek için panelden ya da "
                f"`ziggy answer {ticket_id} --text ...` ile cevap ver.")
        elif status == "needs-user":
            round_ = pipeline.open_round(st)
            body = questions_text(round_) if round_ else ""
            say("Soruları var; cevabını bu thread'e yaz." + (f"\n{body}" if body else ""))
        elif status == "failed":
            error = f" ({st['error']})" if st.get("error") else ""
            say(f"Koşu başarısız{error}. Ayrıntı: `ziggy log {ticket_id} --message`. "
                "Kaldığı aşamayı tekrar denemek için `retry` yaz.")
