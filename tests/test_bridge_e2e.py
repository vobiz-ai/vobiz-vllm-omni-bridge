"""
End-to-end through the bridge with the mock realtime server, in one process.

A fake Vobiz socket records the frames the bridge sends; the mock realtime
server runs on a free port; we drive RealtimeSession.handle() with the same
JSON Vobiz would send and assert on playAudio, checkpoint and clearAudio.
"""

import asyncio
import base64
import json
import math
import socket
import struct
import types

import pytest
import websockets

import config
import mock_realtime
import realtime
import tools


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeVobizSocket:
    def __init__(self):
        self.frames: list[dict] = []
        self.closed = False

    async def send_text(self, data: str):
        self.frames.append(json.loads(data))

    async def close(self):
        self.closed = True

    def count(self, event: str) -> int:
        return sum(1 for f in self.frames if f.get("event") == event)


def speech(ms: int, rate: int = 16000) -> bytes:
    n = int(rate * ms / 1000)
    return struct.pack(f"<{n}h", *(int(6000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(n)))


def media_frame(payload: bytes, seq: int) -> str:
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": seq,
            "streamId": "s1",
            "media": {"track": "inbound", "chunk": seq, "timestamp": str(seq * 20), "payload": base64.b64encode(payload).decode()},
        }
    )


async def feed(session, pcm: bytes, rate: int = 16000, frame_ms: int = 20, pace: float = 0.0):
    step = int(rate * 2 * frame_ms / 1000)
    for i in range(0, len(pcm), step):
        await session.handle(media_frame(pcm[i : i + step], i // step))
        if pace:
            await asyncio.sleep(pace)


@pytest.fixture
async def mock_server():
    port = free_port()
    args = types.SimpleNamespace(vad_rms=300.0, silence_ms=300.0, reply_seconds=1.5, pace=0.05, end_call_after=0)

    async def handler(ws):
        try:
            await mock_realtime.Session(ws, args).run()
        except websockets.ConnectionClosed:
            pass

    async with websockets.serve(handler, "127.0.0.1", port, max_size=None):
        yield port


async def wait_until(pred, timeout=8.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def start_session(port, monkeypatch, **env):
    monkeypatch.setenv("REALTIME_URL", f"ws://127.0.0.1:{port}/v1/realtime?duplex=1")
    monkeypatch.setenv("OMNI_MODEL", "mock")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rt = realtime
    f = config.Flags.load()
    ws = FakeVobizSocket()
    events: list[tuple[str, dict]] = []
    turns: list[tuple[str, str]] = []
    runner = tools.ToolRunner(f, "c1", "https://b.test", on_event=lambda n, d: events.append((n, d)))
    session = rt.new_session(
        ws, flags=f, on_turn=lambda r, t: turns.append((r, t)), on_event=lambda n, d: events.append((n, d)), tool_runner=runner
    )
    await session.handle(
        json.dumps(
            {
                "event": "start",
                "streamId": "s1",
                "start": {"callId": "c1", "streamId": "s1", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 16000}},
            }
        )
    )
    assert await wait_until(lambda: any(n == "model:session.updated" for n, _ in events), 5)
    return rt, session, ws, events, turns


@pytest.mark.asyncio
async def test_one_turn_produces_playaudio_and_checkpoint(mock_server, monkeypatch):
    rt, session, ws, events, turns = await start_session(mock_server, monkeypatch)
    assert any(n == "model:session.created" and (d.get("capabilities") or {}).get("supports_barge_in") for n, d in events)
    await feed(session, speech(600), pace=0.005)
    await feed(session, b"\x00" * (16000 * 2), pace=0.005)  # 1 s of silence closes the turn
    assert await wait_until(lambda: ws.count("checkpoint") >= 1)
    assert ws.count("playAudio") > 0
    first = next(f for f in ws.frames if f["event"] == "playAudio")
    assert first["media"]["contentType"] == "audio/x-l16"
    assert first["media"]["sampleRate"] == 24000
    assert len(base64.b64decode(first["media"]["payload"])) == 960  # 20 ms at 24 kHz
    assert any(r == "agent" for r, _ in turns)
    assert any(n == "model:first_audio" for n, _ in events)
    # playedStream resolves the checkpoint and clears the playing flag
    name = next(f["name"] for f in ws.frames if f["event"] == "checkpoint")
    await session.handle(json.dumps({"event": "playedStream", "streamId": "s1", "name": name}))
    assert session.vobiz.playing is False
    await session.handle(json.dumps({"event": "stop", "streamId": "s1"}))
    assert session.closed


@pytest.mark.asyncio
async def test_barge_in_sends_clearaudio(mock_server, monkeypatch):
    rt, session, ws, events, turns = await start_session(mock_server, monkeypatch, BARGE_IN_PROTECT_MS="0")
    await feed(session, speech(600), pace=0.005)
    await feed(session, b"\x00" * (16000 * 2 // 2), pace=0.005)  # 500 ms silence
    assert await wait_until(lambda: ws.count("playAudio") > 0)
    # talk over the reply while it is still streaming
    await feed(session, speech(600), pace=0.005)
    assert await wait_until(lambda: ws.count("clearAudio") >= 1)
    assert any(n == "model:barge_in" for n, _ in events)
    await session.handle(json.dumps({"event": "stop", "streamId": "s1"}))


@pytest.mark.asyncio
async def test_mulaw_leg_is_converted_both_ways(mock_server, monkeypatch):
    rt, session, ws, events, turns = await start_session(
        mock_server, monkeypatch, OUTPUT_CONTENT_TYPE="audio/x-mulaw", OUTPUT_SAMPLE_RATE="8000"
    )
    # inbound mulaw 8 kHz
    session.vobiz.in_encoding, session.vobiz.in_rate = "mulaw", 8000
    import audio

    pcm8 = audio.resample_pcm16(speech(600), 16000, 8000)
    await feed(session, audio.pcm16_to_mulaw(pcm8), rate=4000, pace=0.005)  # 160 bytes per 20 ms frame
    await feed(session, bytes([0xFF]) * 8000, rate=4000, pace=0.005)
    assert await wait_until(lambda: ws.count("playAudio") > 0)
    first = next(f for f in ws.frames if f["event"] == "playAudio")
    assert first["media"]["contentType"] == "audio/x-mulaw"
    assert first["media"]["sampleRate"] == 8000
    assert len(base64.b64decode(first["media"]["payload"])) == 160
    await session.handle(json.dumps({"event": "stop", "streamId": "s1"}))


@pytest.mark.asyncio
async def test_unreachable_model_server_still_closes_cleanly(monkeypatch):
    monkeypatch.setenv("REALTIME_URL", "ws://127.0.0.1:1/v1/realtime?duplex=1")
    monkeypatch.setenv("MODEL_CONNECT_RETRIES", "0")
    f = config.Flags.load()
    ws = FakeVobizSocket()
    events: list[tuple[str, dict]] = []
    session = realtime.new_session(ws, flags=f, on_event=lambda n, d: events.append((n, d)))
    await session.handle(json.dumps({"event": "start", "streamId": "s1", "start": {"callId": "c1"}}))
    assert await wait_until(lambda: any(n == "model:error" for n, _ in events), 5)
    await session.handle(json.dumps({"event": "stop", "streamId": "s1"}))
    assert session.closed


@pytest.mark.asyncio
async def test_goodbye_in_transcript_hangs_up_after_playback(mock_server, monkeypatch):
    rt, session, ws, events, turns = await start_session(mock_server, monkeypatch, GOODBYE_PHRASES="what you said")
    await feed(session, speech(600), pace=0.005)
    await feed(session, b"\x00" * (16000 * 2), pace=0.005)
    assert await wait_until(lambda: ws.count("checkpoint") >= 1)
    assert any(n == "model:goodbye_detected" for n, _ in events)
    name = next(f["name"] for f in ws.frames if f["event"] == "checkpoint")
    await session.handle(json.dumps({"event": "playedStream", "streamId": "s1", "name": name}))
    assert await wait_until(lambda: ws.count("stop") >= 1, 3)


@pytest.mark.asyncio
async def test_barge_in_ignored_inside_protect_window(mock_server, monkeypatch):
    rt, session, ws, events, turns = await start_session(mock_server, monkeypatch, BARGE_IN_PROTECT_MS="5000")
    await feed(session, speech(600), pace=0.005)
    await feed(session, b"\x00" * (16000 * 2 // 2), pace=0.005)
    assert await wait_until(lambda: ws.count("playAudio") > 0)
    await feed(session, speech(600), pace=0.005)
    assert await wait_until(lambda: any(n == "model:barge_in_ignored" for n, _ in events))
    assert ws.count("clearAudio") == 0
    await session.handle(json.dumps({"event": "stop", "streamId": "s1"}))
