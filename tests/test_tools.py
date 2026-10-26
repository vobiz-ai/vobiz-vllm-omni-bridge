import json

import pytest

import config
import tools


@pytest.mark.asyncio
async def test_tool_runner_end_call_and_transfer(monkeypatch):
    monkeypatch.setenv("TOOL_TRANSFER", "true")
    monkeypatch.setenv("TRANSFER_NUMBER", "+919999999999")
    f = config.Flags.load()
    events = []
    r = tools.ToolRunner(f, "call-1", "https://b.test", on_event=lambda n, d: events.append((n, d)))
    out = await r.run("end_call", json.dumps({"reason": "done"}))
    assert out["result"] == "ok" and r.hangup_after_playback
    assert await r.after_playback() == "hangup"

    calls = {}

    async def fake_transfer(flags, call_uuid, aleg_url):
        calls["url"] = aleg_url
        return 202

    monkeypatch.setattr(tools.vobiz_api, "transfer", fake_transfer)
    r2 = tools.ToolRunner(f, "call-2", "https://b.test")
    await r2.run("transfer_to_human", "{}")
    assert await r2.after_playback() == "transfer" and calls["url"] == "https://b.test/transfer"
    assert (await r2.run("nope", "{}"))["error"].startswith("unknown")


def test_definitions_follow_flags(monkeypatch):
    monkeypatch.setenv("TOOL_TRANSFER", "true")
    monkeypatch.setenv("TRANSFER_NUMBER", "+919999999999")
    monkeypatch.setenv("TOOL_SEND_DTMF", "true")
    names = [t["name"] for t in tools.definitions(config.Flags.load())]
    assert names == ["end_call", "transfer_to_human", "send_dtmf"]


def test_barge_in_flags_in_config(monkeypatch):
    monkeypatch.setenv("BARGE_IN_PROTECT_MS", "900")
    monkeypatch.setenv("INTERRUPT_RESPONSE", "false")
    f = config.Flags.load()
    assert f.barge_in_protect_ms == 900 and f.interrupt_response is False
