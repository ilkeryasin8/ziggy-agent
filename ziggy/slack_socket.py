"""Socket Mode transport for the listener: the only module that imports slack_sdk.

Socket Mode keeps an outbound WebSocket to Slack, so nothing listens on a port. Tokens come from
the config's *_token_cmd (e.g. `op read ...`) at start and are kept in memory only.
"""
import subprocess
import threading
import time

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient

from .config import ConfigError, SlackConfig, Target
from .listener import Listener


def _secret(command: str, what: str) -> str:
    proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        raise ConfigError(f"{what} command failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    return value


class WebAPI:
    def __init__(self, client: WebClient):
        self.client = client

    def post(self, channel: str, thread_ts: str, text: str) -> None:
        self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False)

    def react(self, channel: str, ts: str, name: str) -> None:
        self.client.reactions_add(channel=channel, timestamp=ts, name=name)

    def permalink(self, channel: str, ts: str) -> str | None:
        return self.client.chat_getPermalink(channel=channel, message_ts=ts).get("permalink")

    def post_top(self, channel: str, text: str) -> str | None:
        return self.client.chat_postMessage(channel=channel, text=text, unfurl_links=False).get("ts")


def _forward_loop(listener) -> None:
    """The other tickets' inbox items to the channel, every FORWARD_EVERY seconds (one failure never stops it)."""
    from .listener import FORWARD_EVERY
    while True:
        try:
            listener.forward()
        except Exception as exc:  # noqa: BLE001
            print(f"ziggy: forward failed: {exc}", flush=True)
        time.sleep(FORWARD_EVERY)


def listen(target: Target, cfg: SlackConfig) -> None:
    web = WebClient(token=_secret(cfg.bot_token_cmd, "bot token"))
    identity = web.auth_test()
    listener = Listener(target, cfg, WebAPI(web))
    client = SocketModeClient(app_token=_secret(cfg.app_token_cmd, "app token"), web_client=web)

    def on_request(sm: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge first: Slack redelivers envelopes not acknowledged within 3 seconds.
        sm.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "events_api":
            listener.handle_event(req.payload.get("event_id"), req.payload.get("event") or {})

    client.socket_mode_request_listeners.append(on_request)
    threading.Thread(target=listener.serve_forever, name="bmad-worker", daemon=True).start()
    if cfg.forward:
        threading.Thread(target=_forward_loop, args=(listener,), name="slack-forward", daemon=True).start()
    client.connect()
    print(f"ziggy: listening as {identity.get('user')} in {cfg.channel} for {target.name}", flush=True)
    threading.Event().wait()
