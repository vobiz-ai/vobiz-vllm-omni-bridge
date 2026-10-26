"""
vobiz_stream.py — the Vobiz half of a call: playAudio / checkpoint / clearAudio out, media in
=============================================================================================

Shared by both engines. Owns the playback bookkeeping that production needs:

- `play()` chunks model audio into 20 ms frames in the declared output format;
- `checkpoint()` marks the end of a response and arms a timeout, because the
  `playedStream` ack is conditional (never arrives after a clearAudio);
- `clear()` flushes the queue and, when gating is on, waits for `clearedAudio`
  before the next `play()` so a replacement reply cannot race the flush.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import audio
from config import Flags

logger = logging.getLogger("vobiz")


class VobizStream:
    def __init__(self, ws, flags: Flags):
        self.ws = ws
        self.flags = flags
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.in_encoding = "l16"
        self.in_rate = 16000
        self.out_encoding = flags.out_encoding
        self.out_rate = flags.model_output_rate
        self.chunk = audio.frame_bytes(self.out_encoding, self.out_rate, flags.play_chunk_ms)
        self.checkpoints = 0
        self.played_bytes = 0
        self.sent = 0
        self.playing = False
        self.pending: dict[str, float] = {}  # checkpoint name -> armed at
        self._cleared = asyncio.Event()
        self._cleared.set()
        self.closed = False

    # -- transport ----------------------------------------------------------
    async def send(self, payload: dict):
        if self.closed:
            return
        data = json.dumps(payload)
        self.sent += 1
        try:
            if hasattr(self.ws, "send_text"):
                await self.ws.send_text(data)
            else:
                await self.ws.send(data)
        except Exception as exc:  # socket gone: the call is over
            self.closed = True
            logger.info(f"vobiz socket closed while sending {payload.get('event')}: {exc}")

    def read_start(self, data: dict) -> tuple[str | None, str | None]:
        start = data.get("start", {}) or {}
        self.stream_id = data.get("streamId") or start.get("streamId")
        self.call_id = data.get("callId") or start.get("callId") or start.get("callUUID") or data.get("CallUUID")
        fmt = start.get("mediaFormat") or {}
        if fmt.get("encoding"):
            self.in_encoding, _ = audio.parse_content_type(fmt["encoding"])
        if fmt.get("sampleRate"):
            self.in_rate = int(fmt["sampleRate"])
        return self.stream_id, self.call_id

    # -- inbound ------------------------------------------------------------
    def decode_media(self, data: dict) -> bytes | None:
        payload = (data.get("media") or {}).get("payload")
        if not payload:
            return None
        return audio.to_model(
            base64.b64decode(payload), self.in_encoding, self.in_rate, self.flags.model_input_rate, self.flags.l16_big_endian
        )

    # -- outbound -----------------------------------------------------------
    async def play(self, pcm: bytes, model_rate: int) -> int:
        """Model PCM16 at model_rate -> playAudio frames. Returns bytes queued."""
        if not pcm or self.closed:
            return 0
        if self.flags.gate_on_cleared_audio and not self._cleared.is_set():
            try:
                await asyncio.wait_for(self._cleared.wait(), timeout=1.0)
            except TimeoutError:
                logger.warning("clearedAudio never arrived; playing anyway")
                self._cleared.set()
        data = audio.from_model(pcm, model_rate, self.out_encoding, self.out_rate, self.flags.l16_big_endian)
        self.played_bytes += len(data)
        self.playing = True
        for i in range(0, len(data), self.chunk):
            frame = {
                "event": "playAudio",
                "media": {
                    "contentType": self.flags.output_content_type,
                    "sampleRate": self.out_rate,
                    "payload": base64.b64encode(data[i : i + self.chunk]).decode(),
                },
            }
            if self.stream_id:
                frame["streamId"] = self.stream_id
            await self.send(frame)
        return len(data)

    def ms_of(self, nbytes: int) -> int:
        width = 1 if self.out_encoding == "mulaw" else 2
        return int(nbytes * 1000 / (self.out_rate * width))

    async def checkpoint(self, name: str = "") -> str:
        self.checkpoints += 1
        label = name or f"turn-{self.checkpoints}"
        self.pending[label] = time.monotonic()
        if self.stream_id:
            await self.send({"event": "checkpoint", "streamId": self.stream_id, "name": label})
        return label

    def played(self, name: str) -> bool:
        """Resolve a checkpoint ack. Returns True if it was one of ours."""
        armed = self.pending.pop(name, None)
        if not self.pending:
            self.playing = False
        return armed is not None

    def expire_checkpoints(self) -> list[str]:
        """Checkpoints older than the timeout: treat playback as finished."""
        now = time.monotonic()
        expired = [n for n, t in self.pending.items() if now - t > self.flags.checkpoint_timeout_s]
        for n in expired:
            self.pending.pop(n, None)
        if expired and not self.pending:
            self.playing = False
        return expired

    async def clear(self):
        """Barge-in: drop queued playback; gate the next play on clearedAudio."""
        if self.stream_id and not self.closed:
            self._cleared.clear()
            await self.send({"event": "clearAudio", "streamId": self.stream_id})
        self.playing = False
        self.pending.clear()

    def cleared(self):
        self._cleared.set()

    async def send_dtmf(self, digits: str):
        """Not a stream event: DTMF on a live call is a REST action (tools.py)."""
        raise NotImplementedError

    async def stop(self):
        """End the stream from our side; Vobiz runs the next XML element."""
        if self.stream_id and not self.closed:
            await self.send({"event": "stop", "streamId": self.stream_id})

    async def close(self):
        self.closed = True
        try:
            await self.ws.close()
        except Exception:
            pass
