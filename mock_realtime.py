"""
mock_realtime.py — a fake vLLM-Omni duplex server, so the bridge runs with no GPU
=======================================================================================

Speaks just enough of the protocol to exercise every path in realtime.py:

    session.created / session.update -> session.updated
    conversation.item.create + response.create -> a spoken reply (a tone)
    input_audio_buffer.append -> energy VAD -> speech_started / speech_stopped,
        a transcript, and a reply
    speech during a reply -> barge-in: response.done status=cancelled
    response.cancel, playback.ack -> acknowledged
    --end-call-after N  -> the Nth reply ends with an end_call tool call

    python mock_realtime.py --port 8765

Replies are 440 Hz tones so the mock Vobiz client can verify bytes, not words.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import struct
import uuid

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mock-rt] %(message)s")
log = logging.getLogger("mock-rt")


def tone(seconds: float, rate: int, hz: float = 440.0, amp: int = 8000) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack_from(f"<{n}h", pcm)
    return math.sqrt(sum(s * s for s in samples) / n)


class Session:
    def __init__(self, ws, args):
        self.ws = ws
        self.args = args
        self.id = f"mock-{uuid.uuid4().hex[:8]}"
        self.in_rate = 16000
        self.out_rate = 24000
        self.speaking = False
        self.silence_ms = 0.0
        self.speech_ms = 0.0
        self.response_task: asyncio.Task | None = None
        self.responses = 0
        self.tools: list = []

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def run(self):
        session: dict = {"id": self.id, "model": "mock", "object": "realtime.session"}
        if not getattr(self.args, "no_capabilities", False):
            session["capabilities"] = {
                "implementation_level": "mock",
                "supports_barge_in": True,
                "supports_playback_ack": True,
                "supports_audio_truncate": True,
                "chunk_period_ms": 20,
            }
        await self.send({"type": "session.created", "session": session})
        async for raw in self.ws:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self.on(ev)

    async def on(self, ev: dict):
        t = ev.get("type")
        if t == "session.update":
            s = ev.get("session") or {}
            audio_cfg = s.get("audio") or {}
            self.in_rate = int(((audio_cfg.get("input") or {}).get("format") or {}).get("rate") or s.get("sample_rate_hz") or 16000)
            self.out_rate = int(((audio_cfg.get("output") or {}).get("format") or {}).get("rate") or 24000)
            self.tools = s.get("tools") or []
            log.info(
                f"session.update in={self.in_rate} out={self.out_rate} tools={[x.get('name') for x in self.tools]} "
                f"instructions={str(s.get('instructions'))[:40]!r}"
            )
            await self.send({"type": "session.updated", "session": {"id": self.id, **s}})
        elif t == "conversation.item.create":
            item = ev.get("item") or {}
            if item.get("type") == "function_call_output":
                log.info(f"function_call_output call_id={item.get('call_id')} output={item.get('output')}")
            else:
                text = "".join(p.get("text", "") for p in item.get("content") or [])
                log.info(f"text item: {text[:60]!r}")
        elif t == "response.create":
            self.start_response("Hello from the mock realtime server.", seconds=1.2)
        elif t == "response.cancel":
            await self.cancel("client")
        elif t == "input_audio_buffer.append":
            await self.on_audio(base64.b64decode(ev.get("audio") or ""))
        elif t == "playback.ack":
            await self.send(
                {"type": "playback.acknowledged", "event": {"played_ms": ev.get("played_ms"), "truncate": ev.get("truncate", False)}}
            )
        else:
            log.info(f"ignored {t}")

    async def on_audio(self, pcm: bytes):
        ms = len(pcm) * 1000 / (self.in_rate * 2)
        loud = rms(pcm) > self.args.vad_rms
        if loud:
            self.silence_ms = 0.0
            self.speech_ms += ms
            if not self.speaking and self.speech_ms >= 60:
                self.speaking = True
                log.info("speech_started")
                await self.send(
                    {"type": "input_audio_buffer.speech_started", "audio_start_ms": 0, "item_id": f"item_{uuid.uuid4().hex[:6]}"}
                )
                if self.response_task and not self.response_task.done():
                    await self.cancel("barge_in")
        else:
            self.speech_ms = 0.0
            if self.speaking:
                self.silence_ms += ms
                if self.silence_ms >= self.args.silence_ms:
                    self.speaking = False
                    log.info("speech_stopped")
                    await self.send({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 0})
                    await self.send({"type": "input_audio_buffer.committed", "item_id": f"item_{uuid.uuid4().hex[:6]}"})
                    await self.send(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "item_x",
                            "transcript": "(mock heard the caller)",
                        }
                    )
                    self.start_response("Mock reply to what you said.", seconds=self.args.reply_seconds)

    async def cancel(self, reason: str):
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
            try:
                await self.response_task
            except (asyncio.CancelledError, Exception):
                pass
            log.info(f"response cancelled ({reason})")
            await self.send({"type": "response.done", "response": {"id": self.current, "status": "cancelled", "output": []}})

    def start_response(self, text: str, seconds: float):
        self.responses += 1
        self.current = f"resp_{self.responses:03d}"
        self.response_task = asyncio.create_task(self._respond(self.current, text, seconds))

    async def _respond(self, rid: str, text: str, seconds: float):
        await self.send({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        pcm = tone(seconds, self.out_rate)
        step = int(self.out_rate * 2 * 0.1)  # 100 ms deltas
        for i in range(0, len(pcm), step):
            await self.send(
                {
                    "type": "response.output_audio.delta",
                    "response_id": rid,
                    "delta": base64.b64encode(pcm[i : i + step]).decode(),
                    "sample_rate_hz": self.out_rate,
                }
            )
            await asyncio.sleep(0.1 * self.args.pace)
        await self.send({"type": "response.output_audio_transcript.delta", "response_id": rid, "delta": text})
        await self.send({"type": "response.output_audio.done", "response_id": rid})
        output = []
        if self.args.end_call_after and self.responses == self.args.end_call_after and any(t.get("name") == "end_call" for t in self.tools):
            call_id = f"call_{uuid.uuid4().hex[:6]}"
            await self.send(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": rid,
                    "call_id": call_id,
                    "name": "end_call",
                    "arguments": json.dumps({"reason": "mock"}),
                }
            )
        await self.send({"type": "response.done", "response": {"id": rid, "status": "completed", "output": output}})
        log.info(f"{rid} done ({seconds:.1f}s of audio at {self.out_rate} Hz)")


async def serve(args):
    async def handler(ws):
        path = getattr(ws, "request", None)
        log.info(f"client connected {getattr(path, 'path', '')}")
        try:
            await Session(ws, args).run()
        except websockets.ConnectionClosed:
            pass
        log.info("client gone")

    async with websockets.serve(handler, args.host, args.port, max_size=None):
        log.info(f"mock realtime on ws://{args.host}:{args.port}/v1/realtime")
        await asyncio.Future()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--vad-rms", type=float, default=300.0, help="RMS above which a frame counts as speech")
    p.add_argument("--silence-ms", type=float, default=500.0)
    p.add_argument("--reply-seconds", type=float, default=2.5)
    p.add_argument("--pace", type=float, default=1.0, help="1.0 = real time, 0 = as fast as possible")
    p.add_argument("--end-call-after", type=int, default=0)
    p.add_argument("--no-capabilities", action="store_true", help="pretend to be a turn-based server")
    asyncio.run(serve(p.parse_args()))


if __name__ == "__main__":
    main()
