"""Let Hermes reply in Slack threads it started itself, from any in-gateway agent, cron included.

Why: a cron agent has no ``messaging`` toolset, so it falls back to ``hermes send`` in a terminal.
That CLI process has no gateway runner, so Hermes's relay egress guard treats Slack as relay-routed
(Hermes Cloud's relay also fronts Slack) and refuses any thread it holds no session for:
"the parent chat is attested but this gateway has no record of the thread". A thread Hermes opened
with its own top-level post never gets a session, because the other agent's replies there don't
@mention Hermes. So Hermes could hand a task to another agent but never follow up in that thread.

This tool sends through the stock ``send_message`` path inside the gateway process, where the live
native Slack adapter makes the send native and the guard doesn't apply, the same as any gateway
turn's ``send_message``. Its own provenance rule is narrower than that: the parent message must be
a thread root posted by Hermes's own bot user, read back from Slack before every send.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

from ._bridge import blocking, call

LOG = logging.getLogger(__name__)

TOOL = "slack_reply_own_thread"
TOOLSET = "slack_own_threads"
MAX_PER_HOUR_ENV = "SLACK_OWN_THREAD_MAX_PER_HOUR"
CHANNELS_ENV = "SLACK_OWN_THREAD_CHANNELS"
DEFAULT_MAX_PER_HOUR = 4
MAX_TEXT = 3500

_LOCK = threading.Lock()
_SENT: dict[tuple[str, str], list[float]] = {}
_IDENTITY: dict[str, tuple[str, str]] = {}

SCHEMA = {
    "name": TOOL,
    "description": (
        "Reply in a Slack thread that Hermes itself started (its own bot posted the thread's top "
        "message), e.g. to follow up with another agent on a handoff you posted in #agents. Works "
        "from cron jobs, where send_message is unavailable and `hermes send` refuses threads with "
        "\"no record of the thread\". Include a real <@USER_ID> mention when the reply is meant to "
        "wake an agent. Refuses threads whose top message was posted by anyone else (including "
        "Composio posts in Leon's name) and caps replies per thread per hour."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Slack channel ID, e.g. C0THREADS01."},
            "thread_ts": {"type": "string",
                          "description": "ts of the thread's top message (Hermes's own post), e.g. 1794360293.700699."},
            "text": {"type": "string", "description": "Reply text (Slack mrkdwn). Keep it short."},
        },
        "required": ["channel", "thread_ts", "text"],
        "additionalProperties": False,
    },
}


def _runner():
    ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
    return ref() if callable(ref) else None


def _live_slack_adapter():
    runner = _runner()
    if runner is None:
        return None
    try:
        from gateway.config import Platform
        return (getattr(runner, "adapters", None) or {}).get(Platform.SLACK)
    except Exception:
        return None


def available() -> bool:
    """Only inside a gateway process with a live Slack adapter (gateway turns and cron jobs)."""
    return _live_slack_adapter() is not None


def _bot_token() -> str:
    from gateway.config import Platform, load_gateway_config
    pconfig = load_gateway_config().platforms.get(Platform.SLACK)
    token = str(getattr(pconfig, "token", "") or "").split(",")[0].strip()
    if not token.startswith("xoxb-"):
        raise RuntimeError("no Slack bot token in the gateway config")
    return token


def _slack(method: str, token: str, **params) -> dict:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown error')}")
    return data


def _identity(token: str) -> tuple[str, str]:
    """(user_id, bot_id) of the bot that owns ``token``."""
    cached = _IDENTITY.get(token)
    if cached:
        return cached
    auth = _slack("auth.test", token)
    identity = (str(auth.get("user_id") or ""), str(auth.get("bot_id") or ""))
    if not identity[0]:
        raise RuntimeError("Slack auth.test returned no bot user id")
    _IDENTITY[token] = identity
    return identity


def _thread_root(channel: str, thread_ts: str, token: str) -> dict:
    data = _slack("conversations.replies", token, channel=channel, ts=thread_ts, limit=1)
    messages = data.get("messages") or []
    return messages[0] if messages else {}


def _max_per_hour() -> int:
    try:
        value = int(os.environ.get(MAX_PER_HOUR_ENV, DEFAULT_MAX_PER_HOUR))
    except ValueError:
        return DEFAULT_MAX_PER_HOUR
    return value if value > 0 else DEFAULT_MAX_PER_HOUR


def _allowed_channels() -> set[str]:
    return {c.strip() for c in os.environ.get(CHANNELS_ENV, "").split(",") if c.strip()}


def _reserve(key: tuple[str, str], now: float) -> bool:
    """Count one send against the per-thread hourly budget; False when the budget is spent."""
    with _LOCK:
        result = call(__file__, "reserveReply", _SENT.get(key, []), now, _max_per_hour())
        _SENT[key] = result["recent"]
        return result["allowed"]


def _release(key: tuple[str, str], stamp: float) -> None:
    with _LOCK:
        recent = _SENT.get(key, [])
        if stamp in recent:
            recent.remove(stamp)


def _send_module():
    try:
        from gateway.outbound import send
    except ModuleNotFoundError as exc:
        if exc.name != "gateway.outbound":
            raise
        from tools import send_message_tool as send
    return send


def _read_thread(channel, thread_ts):
    try:
        token = _bot_token()
        user_id, bot_id = _identity(token)
        root = _thread_root(channel, thread_ts, token)
    except Exception as exc:
        return {"error": f"could not read the thread's top message: {exc}"}
    return {"userId": user_id, "botId": bot_id, "root": root}


def _send_thread(target, text):
    raw = _send_module().send_message_tool({"action": "send", "target": target, "message": text})
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"success": False, "error": str(raw)[:500]}


def handle(args: dict, **_kw) -> str:
    host = {
        "available": available,
        "readThread": blocking(_read_thread),
        "reserve": blocking(lambda channel, thread, stamp: _reserve((channel, thread), stamp)),
        "release": blocking(lambda channel, thread, stamp: _release((channel, thread), stamp)),
        "send": blocking(_send_thread),
        "logSend": lambda channel, thread: LOG.info(
            "%s: replied in Hermes's own thread %s:%s", TOOL, channel, thread),
        "now": lambda: time.time(),
    }
    request = {"channel": str(args.get("channel") or "").strip(),
               "thread": str(args.get("thread_ts") or "").strip(),
               "text": str(args.get("text") or "").strip()}
    return json.dumps(call(__file__, "reply",
        host, request, sorted(_allowed_channels()), CHANNELS_ENV, MAX_TEXT, _max_per_hour()))


def register(ctx):
    ctx.register_tool(name=TOOL, toolset=TOOLSET, schema=SCHEMA, handler=handle,
                      check_fn=available, emoji="🧵")
    LOG.info("slack-own-thread-reply: %s registered", TOOL)
