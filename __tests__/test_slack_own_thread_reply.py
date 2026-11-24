"""Run against a Hermes checkout's environment (see README)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types

import pytest

import gateway.run
from gateway.config import Platform, PlatformConfig
from gateway.relay import egress

_spec = importlib.util.spec_from_file_location(
    "slack_own_thread_reply", pathlib.Path(__file__).resolve().parents[1] / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

CHANNEL = "C0THREADS01"
THREAD = "1794360293.700699"
HERMES = "U0HERMES001"
HERMES_BOT = "B0HERMES001"
LEON = "U0LEON00001"


class _Relay:
    """Hermes Cloud's relay connector, which also fronts Slack."""

    def fronts_platform(self, platform):
        return platform == Platform.SLACK


def _config():
    return types.SimpleNamespace(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="xoxb-test")},
        get_home_channel=lambda platform: None)


@pytest.fixture()
def relay_env(monkeypatch):
    """Slack fronted by the relay, #agents attested as a channel, no session in the handoff thread."""
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setattr(egress, "attested_relay_targets", lambda name: {CHANNEL})
    monkeypatch.setattr("gateway.config.load_gateway_config", _config)
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: None)


@pytest.fixture()
def live_gateway(relay_env, monkeypatch):
    runner = types.SimpleNamespace(adapters={Platform.SLACK: object(), Platform.RELAY: _Relay()})
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    return runner


@pytest.fixture()
def slack(monkeypatch):
    """Fake Slack Web API: the thread root, and the bot identity."""
    state = {"root": {"ts": THREAD, "user": HERMES, "bot_id": HERMES_BOT}, "calls": []}

    def fake(method, token, **params):
        state["calls"].append((method, params))
        if method == "auth.test":
            return {"ok": True, "user_id": HERMES, "bot_id": HERMES_BOT}
        if method == "conversations.replies":
            return {"ok": True, "messages": [state["root"]]}
        raise AssertionError(method)

    monkeypatch.setattr(plugin, "_slack", fake)
    plugin._IDENTITY.clear()
    plugin._SENT.clear()
    return state


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def fake_send(args, **_kw):
        calls.append(args)
        return json.dumps({"success": True, "message_id": "1794361999.000100"})

    monkeypatch.setattr(plugin._send_module(), "send_message_tool", fake_send)
    return calls


def _call(**overrides):
    args = {"channel": CHANNEL, "thread_ts": THREAD, "text": f"<@U0SILICON01> nudge"} | overrides
    return json.loads(plugin.handle(args))


def test_cli_guard_refuses_hermes_own_thread(relay_env):
    """The bug: `hermes send` from a cron terminal has no runner, so the thread is refused."""
    verdict = egress.authorize_relay_target("slack", CHANNEL, THREAD, native_token="xoxb-test")
    assert verdict and "no record of the thread" in verdict


def test_gateway_process_send_is_native_and_allowed(live_gateway):
    """The premise: inside the gateway the live Slack adapter makes the send native."""
    assert egress.authorize_relay_target("slack", CHANNEL, THREAD, native_token="xoxb-test") is None


def test_unavailable_outside_gateway(relay_env, slack, sent):
    assert plugin.available() is False
    result = _call()
    assert result["success"] is False and "inside the gateway" in result["error"]
    assert not sent


def test_replies_in_hermes_thread(live_gateway, slack, sent):
    result = _call()
    assert result["success"] is True
    assert sent == [{"action": "send", "target": f"slack:{CHANNEL}:{THREAD}", "message": "<@U0SILICON01> nudge"}]
    assert ("conversations.replies", {"channel": CHANNEL, "ts": THREAD, "limit": 1}) in slack["calls"]


def test_refuses_thread_started_by_someone_else(live_gateway, slack, sent):
    slack["root"] = {"ts": THREAD, "user": LEON, "bot_id": "B0WATCHBOT1"}  # a Composio post as Leon
    result = _call()
    assert result["success"] is False and "not posted by Hermes" in result["error"]
    assert not sent


def test_refuses_ts_of_a_reply(live_gateway, slack, sent):
    slack["root"] = {"ts": "1794360200.000001", "user": HERMES}
    result = _call()
    assert result["success"] is False and "not the top message" in result["error"]
    assert not sent


@pytest.mark.parametrize("bad", [
    {"channel": "slack:C0THREADS01"}, {"channel": "U0SILICON01"}, {"thread_ts": "p1794360293700699"},
    {"text": "  "}, {"text": "x" * 4000},
])
def test_rejects_bad_arguments(live_gateway, slack, sent, bad):
    assert _call(**bad)["success"] is False
    assert not sent


def test_channel_allowlist(live_gateway, slack, sent, monkeypatch):
    monkeypatch.setenv(plugin.CHANNELS_ENV, "C0TASKS0002")
    assert "not in" in _call()["error"]
    monkeypatch.setenv(plugin.CHANNELS_ENV, f"C0TASKS0002,{CHANNEL}")
    assert _call()["success"] is True


def test_hourly_cap_per_thread(live_gateway, slack, sent):
    for _ in range(plugin.DEFAULT_MAX_PER_HOUR):
        assert _call()["success"] is True
    result = _call()
    assert result["success"] is False and "wait for an answer" in result["error"]
    assert len(sent) == plugin.DEFAULT_MAX_PER_HOUR


def test_failed_send_does_not_spend_budget(live_gateway, slack, monkeypatch):
    monkeypatch.setattr(plugin._send_module(), "send_message_tool",
                        lambda args, **_kw: json.dumps({"error": "Slack down"}))
    for _ in range(plugin.DEFAULT_MAX_PER_HOUR + 1):
        assert _call()["error"] == "Slack down"
    assert plugin._SENT[(CHANNEL, THREAD)] == []


def test_full_send_path_in_gateway(live_gateway, slack, monkeypatch):
    """Through the real send_message tool: in the gateway the relay guard lets the thread through."""
    smt = plugin._send_module()

    delivered = []

    async def fake_platform_send(platform, pconfig, chat_id, message, thread_id=None, **_kw):
        delivered.append((platform, chat_id, thread_id, message))
        return {"success": True, "message_id": "1794361999.000100"}

    monkeypatch.setattr(smt, "_send_to_platform", fake_platform_send)
    monkeypatch.setattr(smt, "_mirror_sent_message", lambda *a, **k: False)
    result = _call()
    assert result["success"] is True, result
    assert delivered == [(Platform.SLACK, CHANNEL, THREAD, "<@U0SILICON01> nudge")]


def test_register_adds_tool_with_gateway_check():
    seen = {}
    ctx = types.SimpleNamespace(register_tool=lambda **kw: seen.update(kw))
    plugin.register(ctx)
    assert seen["name"] == "slack_reply_own_thread" and seen["toolset"] == "slack_own_threads"
    assert seen["check_fn"] is plugin.available and seen["schema"]["name"] == seen["name"]
