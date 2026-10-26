"""
realtime.py — one Vobiz stream, one vLLM-Omni full-duplex session
==================================================================

    REALTIME_URL=ws://<gpu-host>:8091/v1/realtime?duplex=1

Default formats line up so that no audio is converted in either direction:

    Vobiz -> app   L16 mono 16 kHz   == duplex input  (audio/pcm rate 16000)
    app -> Vobiz   L16 mono 24 kHz   == duplex output (audio/pcm rate 24000)

Barge-in: `input_audio_buffer.speech_started` or `overlap.decision` with
`action: barge_in` during playback becomes `clearAudio` plus a truncated
`playback.ack`. A carrier `playedStream` becomes a full `playback.ack`.

Models without tool calling (MiniCPM-o 4.5) cannot call `end_call`; with
END_CALL_ON_GOODBYE the bridge hangs up after the agent's transcript contains
one of GOODBYE_PHRASES.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid

import websockets

import tools as tools_mod
from config import Flags
from config import flags as get_flags
from vobiz_stream import VobizStream

logger = logging.getLogger("s2s")


def connect_url(f: Flags) -> str:
    return f.realtime_url


def said_goodbye(text: str, phrases: list[str]) -> bool:
    """True when the agent's final transcript contains a goodbye phrase as whole words."""
    lowered = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", lowered) for p in phrases if p)


def turn_detection(f: Flags) -> dict:
    td: dict = {"type": "server_vad"}
    if not f.interrupt_response:
        td["interrupt_response"] = False
    if f.vad_threshold:
        td["threshold"] = float(f.vad_threshold)
    if f.vad_silence_ms:
        td["silence_duration_ms"] = int(f.vad_silence_ms)
    if f.vad_prefix_padding_ms:
        td["prefix_padding_ms"] = int(f.vad_prefix_padding_ms)
    return td


def session_update(f: Flags) -> dict:
    """The one `session.update` that configures the whole call on the duplex lane."""
    td = turn_detection(f)
    session: dict = {
        "model": f.realtime_model,
        "modalities": ["audio", "text"],
        "instructions": f.system_prompt,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "sample_rate_hz": f.model_input_rate,
        "turn_detection": td,
        "overlap_policy": "barge_in_on_speech",
        "temperature": f.temperature,
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": f.model_input_rate}, "turn_detection": td},
            "output": {"format": {"type": "audio/pcm", "rate": f.model_output_rate}},
        },
    }
    if f.voice:
        session["audio"]["output"]["voice"] = f.voice
    if f.max_output_tokens:
        session["max_response_output_tokens"] = f.max_output_tokens
    if f.input_transcription:
        session["audio"]["input"]["transcription"] = {"enabled": True}
    defs = tools_mod.definitions(f)
    if defs:
        session["tools"] = defs
        session["tool_choice"] = "auto"
    if f.greeting and f.greeting_source == "model":
        session["extra_body"] = {"duplex_initial_user_text": f"Greet the caller with exactly: {f.greeting}"}
    return {"type": "session.update", "event_id": f"evt_{uuid.uuid4().hex[:8]}", "session": session}


class RealtimeSession:
    """Owns one realtime session for the lifetime of one Vobiz WebSocket."""

    def __init__(self, ws, flags: Flags | None = None, on_turn=None, on_event=None, tool_runner=None):
        self.f = flags or get_flags()
        self.vobiz = VobizStream(ws, self.f)
        self.on_turn = on_turn or (lambda role, text: None)
        self.on_event = on_event or (lambda name, data: None)
        self.tools = tool_runner

        self.model_ws = None
        self.capabilities: dict | None = None
        self.ready = asyncio.Event()
        self.runner: asyncio.Task | None = None
        self.pump: asyncio.Task | None = None
        self.closed = False
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self.said: list[str] = []
        self.frames_in = 0

        self.response_id: str | None = None
        self.response_bytes = 0
        self.response_started = 0.0
        self.pending_checkpoints: dict[str, str] = {}
        self.first_audio_at: float | None = None
        self.turn_ended_at: float | None = None
        self.last_caller_audio = time.monotonic()
        self.started = time.monotonic()
        self.watchdog: asyncio.Task | None = None

    # -- Vobiz -> here ------------------------------------------------------
    async def handle(self, message: str):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        event = data.get("event")
        if event == "start":
            stream_id, call_id = self.vobiz.read_start(data)
            logger.info(f"start — call {call_id} stream {stream_id} in {self.vobiz.in_encoding}/{self.vobiz.in_rate}")
            self.on_event("stream:start", data)
            await self.connect()
        elif event == "media":
            pcm = self.vobiz.decode_media(data)
            if pcm is None:
                return
            self.frames_in += 1
            try:
                self.inbound.put_nowait(pcm)
            except asyncio.QueueFull:
                try:
                    self.inbound.get_nowait()
                    self.inbound.put_nowait(pcm)
                except asyncio.QueueEmpty:
                    pass
        elif event == "dtmf":
            digit = (data.get("dtmf") or {}).get("digit", "")
            self.on_event("stream:dtmf", data)
            if digit and self.f.dtmf_to_model:
                await self.send_text_item(f"The caller pressed the keypad digit {digit}.")
        elif event == "playedStream":
            self.on_event("stream:playedStream", data)
            name = data.get("name", "")
            if self.vobiz.played(name):
                rid = self.pending_checkpoints.pop(name, "")
                await self.playback_ack(rid, truncate=False)
                await self._after_playback()
        elif event == "clearedAudio":
            self.vobiz.cleared()
            self.on_event("stream:clearedAudio", data)
        elif event == "stop":
            self.on_event("stream:stop", data)
            await self.cleanup()
        else:
            self.on_event(f"stream:{event or 'unknown'}", data)

    async def _after_playback(self):
        if not self.tools or self.vobiz.pending:
            return
        action = await self.tools.after_playback()
        if action == "hangup":
            logger.info("goodbye played — stopping the stream")
            await self.vobiz.stop()
            await self.vobiz.close()
        elif action == "transfer":
            logger.info("transfer announced — leg handed to /transfer XML")

    # -- model --------------------------------------------------------------
    async def connect(self):
        if self.runner:
            return
        self.runner = asyncio.create_task(self._run())
        self.pump = asyncio.create_task(self._pump_audio())
        self.watchdog = asyncio.create_task(self._watchdog())

    async def _send(self, payload: dict):
        if self.model_ws:
            await self.model_ws.send(json.dumps(payload))

    async def _run(self):
        url = connect_url(self.f)
        headers = {"Authorization": f"Bearer {self.f.api_key}"} if self.f.api_key else {}
        attempt = 0
        while not self.closed:
            try:
                async with websockets.connect(url, additional_headers=headers, max_size=None, ping_interval=20, ping_timeout=60) as ws:
                    self.model_ws = ws
                    self.on_event("model:connected", {"url": url, "attempt": attempt})
                    await self._handshake(ws)
                    self.ready.set()
                    await self._greet()
                    async for raw in ws:
                        try:
                            await self._on_event(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
                    return
            except asyncio.CancelledError:
                raise
            except websockets.InvalidStatus as exc:
                code = getattr(getattr(exc, "response", None), "status_code", "?")
                logger.error(f"realtime handshake rejected: HTTP {code}")
                self.on_event("model:error", {"error": f"HTTP {code}"})
                return  # auth/model errors do not heal by retrying
            except Exception as exc:
                logger.error(f"realtime session ended: {type(exc).__name__}: {exc}")
                self.on_event("model:error", {"error": f"{type(exc).__name__}: {exc}"})
                if attempt >= self.f.model_connect_retries or self.ready.is_set():
                    return
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
            finally:
                self.model_ws = None
        self.ready.set()

    async def _handshake(self, ws):
        created = await self._wait_for(ws, {"session.created"}, self.f.handshake_timeout_s)
        if created:
            await self._on_event(created)
            if self.f.require_duplex and self.capabilities is None:
                self.on_event("model:not_duplex", {})
                raise RuntimeError(
                    "server is not full duplex (no capabilities in session.created); set REQUIRE_DUPLEX=false to proceed anyway"
                )
        await ws.send(json.dumps(session_update(self.f)))
        updated = await self._wait_for(ws, {"session.updated", "error"}, self.f.handshake_timeout_s)
        if updated:
            await self._on_event(updated)
        if updated and updated.get("type") == "error":
            raise RuntimeError(f"session.update rejected: {updated.get('error')}")

    async def _wait_for(self, ws, types: set[str], timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(f"timed out waiting for {sorted(types)}")
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                return None
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") in types:
                return event
            await self._on_event(event)

    async def _greet(self):
        return  # the duplex greeting travels in session.update as duplex_initial_user_text

    async def send_text_item(self, text: str):
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
            }
        )

    async def _pump_audio(self):
        await self.ready.wait()
        while not self.closed:
            pcm = await self.inbound.get()
            if not self.model_ws:
                continue
            try:
                await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
            except Exception as exc:
                logger.error(f"append failed: {exc}")
                return

    async def _watchdog(self):
        """Max call length, caller-silence hangup, and checkpoint timeouts."""
        while not self.closed:
            await asyncio.sleep(1.0)
            for name in self.vobiz.expire_checkpoints():
                logger.warning(f"checkpoint {name} never acknowledged; assuming played")
                self.pending_checkpoints.pop(name, None)
                await self._after_playback()
            elapsed = time.monotonic() - self.started
            if elapsed > self.f.max_call_seconds:
                logger.warning("max call length reached — ending the call")
                self.on_event("watchdog:max_call", {"seconds": int(elapsed)})
                await self.vobiz.stop()
                await self.vobiz.close()
                return
            quiet = time.monotonic() - self.last_caller_audio
            if self.f.caller_silence_hangup_s and quiet > self.f.caller_silence_hangup_s and not self.vobiz.playing:
                logger.warning("caller silent — ending the call")
                self.on_event("watchdog:silence", {"seconds": int(quiet)})
                await self.vobiz.stop()
                await self.vobiz.close()
                return

    async def _on_event(self, event: dict):
        kind = event.get("type", "")
        if kind == "session.created":
            s = event.get("session") or {}
            self.capabilities = s.get("capabilities")
            self.on_event("model:session.created", {"id": s.get("id"), "model": s.get("model"), "capabilities": self.capabilities})
            if self.capabilities is None:
                logger.warning("session.created carries no capabilities: this server is not serving the full-duplex lane")
            elif not self.capabilities.get("supports_barge_in", True):
                logger.warning("model advertises supports_barge_in=false: caller speech will not cut replies")
        elif kind == "session.updated":
            self.on_event("model:session.updated", {})
        elif kind == "input_audio_buffer.speech_started":
            self.last_caller_audio = time.monotonic()
            young = self.first_audio_at and (time.monotonic() - self.first_audio_at) * 1000 < self.f.barge_in_protect_ms
            if self.vobiz.playing and young:
                # the server may still cancel its reply; we keep what Vobiz has queued
                self.on_event("model:barge_in_ignored", {"reason": "protect_window"})
            elif self.vobiz.playing:
                await self._barge_in("speech_started")
        elif kind == "input_audio_buffer.speech_stopped":
            self.turn_ended_at = time.monotonic()
            self.last_caller_audio = self.turn_ended_at
        elif kind == "overlap.decision":
            if event.get("action") == "barge_in":
                await self._barge_in(event.get("reason", "overlap"))
        elif kind == "response.created":
            resp = event.get("response") or {}
            self.response_id = resp.get("id") or event.get("response_id")
            self.response_bytes = 0
            self.response_started = time.monotonic()
            self.first_audio_at = None
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            delta = event.get("delta") or ""
            if not delta:
                return
            pcm = base64.b64decode(delta)
            rate = int(event.get("sample_rate_hz") or self.f.model_output_rate)
            if self.first_audio_at is None:
                self.first_audio_at = time.monotonic()
                ttfa = (self.first_audio_at - self.turn_ended_at) if self.turn_ended_at else None
                self.on_event("model:first_audio", {"after_turn_end_ms": int(ttfa * 1000) if ttfa else None})
                if ttfa is not None:
                    logger.info(f"first audio {int(ttfa * 1000)} ms after end of caller speech")
            self.response_bytes += await self.vobiz.play(pcm, rate)
        elif kind in (
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
            "transcription.delta",
            "response.output_text.delta",
        ):
            self.said.append(event.get("delta") or "")
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (event.get("transcript") or "").strip()
            if text:
                self.on_turn("caller", text)
        elif kind == "response.function_call_arguments.done":
            await self._on_tool_call(event.get("name"), event.get("call_id"), event.get("arguments"))
        elif kind == "response.done":
            resp = event.get("response") or {}
            status = resp.get("status", "completed")
            for item in resp.get("output") or []:
                if item.get("type") == "function_call":
                    await self._on_tool_call(item.get("name"), item.get("call_id"), item.get("arguments"))
            self._flush_said()
            if self.response_bytes:
                name = await self.vobiz.checkpoint(f"resp-{(self.response_id or 'x')[-8:]}")
                self.pending_checkpoints[name] = self.response_id or ""
            self.on_event("model:response.done", {"status": status, "audio_ms": self.vobiz.ms_of(self.response_bytes)})
        elif kind == "error":
            err = event.get("error") or event
            logger.error(f"realtime error: {json.dumps(err)[:300]}")
            self.on_event("model:error", {"error": err})
        elif kind in (
            "rate_limits.updated",
            "response.output_audio.done",
            "response.audio.done",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "conversation.item.created",
            "conversation.item.added",
            "conversation.item.done",
            "input_audio_buffer.committed",
            "input_audio_buffer.cleared",
            "playback.acknowledged",
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
            "transcription.done",
            "conversation.item.input_audio_transcription.delta",
        ):
            pass
        else:
            self.on_event(f"model:{kind or 'unknown'}", {k: v for k, v in event.items() if k not in ("delta", "audio")})

    async def _barge_in(self, reason: str):
        logger.info(f"barge-in ({reason}) — clearing queued playback")
        await self.vobiz.clear()
        self.on_event("model:barge_in", {"reason": reason})
        if self.response_id:
            await self.playback_ack(self.response_id, truncate=True)
        self.pending_checkpoints.clear()

    async def playback_ack(self, response_id: str, truncate: bool):
        generated_ms = self.vobiz.ms_of(self.response_bytes)
        played_ms = generated_ms if not truncate else max(0, min(generated_ms, int((time.monotonic() - self.response_started) * 1000)))
        ack = {"type": "playback.ack", "played_ms": played_ms, "committed_ms": played_ms}
        if response_id:
            ack["response_id"] = response_id
        if truncate:
            ack["truncate"] = True
        await self._send(ack)

    def _flush_said(self):
        if self.said:
            text = "".join(self.said).strip()
            self.said = []
            if text:
                self.on_turn("agent", text)
                if self.f.end_call_on_goodbye and self.tools is not None and said_goodbye(text, self.f.goodbye_list):
                    logger.info("agent said goodbye — hanging up after playback")
                    self.tools.hangup_after_playback = True
                    self.on_event("model:goodbye_detected", {"text": text[:120]})

    async def _on_tool_call(self, name, call_id, arguments):
        if not name:
            return
        result = await self.tools.run(name, arguments) if self.tools else {"result": "ok"}
        await self._send(
            {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}}
        )

    async def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self._flush_said()
        for task in (self.pump, self.runner, self.watchdog):
            if task:
                task.cancel()
        for task in (self.pump, self.runner, self.watchdog):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info(
            f"session closed — {self.frames_in} frames in, {self.vobiz.played_bytes} bytes played, {self.vobiz.checkpoints} checkpoints"
        )


def new_session(ws, flags: Flags | None = None, on_turn=None, on_event=None, tool_runner=None) -> RealtimeSession:
    return RealtimeSession(ws, flags=flags, on_turn=on_turn, on_event=on_event, tool_runner=tool_runner)
