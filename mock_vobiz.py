"""
mock_vobiz.py — impersonate Vobiz and hold a whole conversation, no phone call
=============================================================================

Speaks the Vobiz stream protocol at the running bridge: opens the WebSocket,
sends `start`, streams a spoken prompt as `media` frames in real time, collects
the `playAudio` frames that come back, answers `checkpoint` with
`playedStream`, optionally talks over the reply to force a barge-in, then
sends `stop`.

    python mock_vobiz.py                      # media/prompt.wav, one turn
    python mock_vobiz.py --barge-in           # speak again while the reply plays
    python mock_vobiz.py --wav question.wav --seconds 25

The reply is written to media/reply.wav. Exits non-zero if no audio came back,
or if --barge-in produced no clearAudio.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import wave
from pathlib import Path

import websockets
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import audio  # noqa: E402

MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)
STREAM_CONTENT_TYPE = os.getenv("STREAM_CONTENT_TYPE", "audio/x-l16;rate=16000")


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            sys.exit(f"{path} must be mono 16-bit PCM WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def write_wav(path: Path, pcm: bytes, rate: int):
    path.write_bytes(audio.wav_header(len(pcm), rate) + pcm)


class MockVobiz:
    def __init__(self, ws, encoding: str, rate: int):
        self.ws = ws
        self.encoding = encoding
        self.rate = rate
        tag = f"{int(time.time()) % 100000:05d}"
        self.stream_id = f"mock-stream-{tag}"
        self.call_id = f"mock-call-{tag}"
        self.reply = bytearray()
        self.reply_rate = 0
        self.reply_content_type = ""
        self.frames = 0
        self.checkpoints: list[str] = []
        self.cleared = 0
        self.agent_hung_up = False
        self.first_audio: float | None = None
        self.last_audio: float | None = None
        self.started = time.monotonic()

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def start(self):
        await self.send(
            {
                "sequenceNumber": 0,
                "event": "start",
                "streamId": self.stream_id,
                "start": {
                    "callId": self.call_id,
                    "streamId": self.stream_id,
                    "accountId": "MOCK",
                    "tracks": ["inbound"],
                    "mediaFormat": {"encoding": "audio/x-mulaw" if self.encoding == "mulaw" else "audio/x-l16", "sampleRate": self.rate},
                },
            }
        )
        print(f"-> start  ({self.encoding}/{self.rate})")

    async def stream_audio(self, pcm: bytes, source_rate: int):
        data = audio.resample_pcm16(pcm, source_rate, self.rate)
        if self.encoding == "mulaw":
            data = audio.pcm16_to_mulaw(data)
        await self.stream_raw(data)

    async def silence(self, seconds: float):
        samples = int(self.rate * seconds)
        quiet = (bytes([0xFF]) * samples) if self.encoding == "mulaw" else (b"\x00" * samples * 2)
        await self.stream_raw(quiet)

    async def stream_raw(self, data: bytes):
        chunk = audio.frame_bytes(self.encoding, self.rate, 20)
        for i in range(0, len(data), chunk):
            await self.send(
                {
                    "event": "media",
                    "sequenceNumber": self.frames + 1,
                    "streamId": self.stream_id,
                    "media": {
                        "track": "inbound",
                        "chunk": self.frames + 1,
                        "timestamp": str(self.frames * 20),
                        "payload": base64.b64encode(data[i : i + chunk]).decode(),
                    },
                }
            )
            self.frames += 1
            await asyncio.sleep(0.02)

    async def reader(self):
        try:
            async for message in self.ws:
                data = json.loads(message)
                event = data.get("event")
                if event == "playAudio":
                    media = data.get("media", {})
                    payload = base64.b64decode(media.get("payload", ""))
                    now = time.monotonic()
                    if self.first_audio is None:
                        self.first_audio = now - self.started
                        print(
                            f"<- playAudio  first frame after {self.first_audio:.2f}s  "
                            f"({media.get('contentType')} @ {media.get('sampleRate')})"
                        )
                    self.last_audio = now
                    self.reply_rate = int(media.get("sampleRate") or 0)
                    self.reply_content_type = media.get("contentType") or ""
                    self.reply += payload
                elif event == "checkpoint":
                    name = data.get("name", "")
                    self.checkpoints.append(name)
                    print(f"<- checkpoint {name}  ({len(self.reply)} bytes queued)")
                    # Vobiz confirms once the queued audio has played; the mock
                    # approximates playback time with the audio's own duration.
                    width = 1 if "mulaw" in self.reply_content_type else 2
                    await asyncio.sleep(min(3.0, len(self.reply) / max(1, self.reply_rate * width) * 0.3))
                    await self.send({"event": "playedStream", "streamId": self.stream_id, "name": name})
                elif event == "clearAudio":
                    self.cleared += 1
                    print("<- clearAudio (barge-in)")
                    await self.send({"event": "clearedAudio", "streamId": self.stream_id})
                else:
                    print(f"<- {event}: {json.dumps(data)[:160]}")
        except websockets.ConnectionClosed:
            self.agent_hung_up = True

    async def stop(self):
        await self.send({"event": "stop", "streamId": self.stream_id, "stop": {"callId": self.call_id, "reason": "mock test finished"}})
        print("-> stop")


async def run(args) -> int:
    encoding, rate = audio.parse_content_type(STREAM_CONTENT_TYPE)
    pcm, source_rate = read_wav(Path(args.wav))

    print(f"Connecting to {args.ws}")
    async with websockets.connect(args.ws, max_size=None) as ws:
        mock = MockVobiz(ws, encoding, rate)
        reader = asyncio.create_task(mock.reader())
        try:
            await mock.start()
            await mock.silence(args.greeting_wait)
            print(f"-> media  {len(pcm)} bytes of speech @ {source_rate} Hz")
            await mock.stream_audio(pcm, source_rate)

            if args.barge_in:
                # Wait for the reply to start, then talk over it.
                for _ in range(300):
                    if mock.first_audio is not None and (time.monotonic() - mock.started) > mock.first_audio + 0.4:
                        break
                    await mock.silence(0.02)
                print("-> media  (barge-in: speaking over the reply)")
                await mock.stream_audio(pcm, source_rate)

            remaining = max(0.0, args.seconds - (time.monotonic() - mock.started))
            await mock.silence(remaining)
            await mock.stop()
        except websockets.ConnectionClosed:
            mock.agent_hung_up = True
            print("<- stream closed by the agent (end_call)")
        await asyncio.sleep(0.5)
        reader.cancel()

    rate_out = mock.reply_rate or 24000
    reply = bytes(mock.reply)
    reply_encoding, _ = audio.parse_content_type(mock.reply_content_type)
    if reply_encoding == "mulaw":
        reply = audio.mulaw_to_pcm16(reply)
    seconds = len(reply) / (rate_out * 2)
    print()
    print(f"  media frames sent      {mock.frames}")
    print(f"  reply bytes received   {len(mock.reply)} of {reply_encoding}/{rate_out}  ({seconds:.1f}s)")
    print(f"  checkpoints            {len(mock.checkpoints)} {mock.checkpoints}")
    print(f"  barge-ins (clearAudio) {mock.cleared}")
    print(f"  agent hung up          {'yes (end_call)' if mock.agent_hung_up else 'no'}")
    if mock.first_audio is not None:
        print(f"  first audio            {mock.first_audio:.2f}s after start")

    ok = bool(reply)
    if args.barge_in and mock.cleared == 0:
        print("\nFAIL — --barge-in requested but no clearAudio arrived.")
        ok = False
    if reply:
        out = MEDIA_DIR / "reply.wav"
        write_wav(out, reply, rate_out)
        print(f"  reply audio            {out}")
    if ok:
        print("\nPASS — the bridge carried audio in both directions.")
        return 0
    if not reply:
        print("\nFAIL — no playAudio came back. Check the server log.")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", default=f"ws://127.0.0.1:{os.getenv('HTTP_PORT', '8000')}/ws")
    parser.add_argument("--wav", default=str(MEDIA_DIR / "prompt.wav"))
    parser.add_argument("--seconds", type=float, default=16.0)
    parser.add_argument("--greeting-wait", type=float, default=3.0)
    parser.add_argument("--barge-in", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
