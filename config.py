"""
config.py — every behaviour of the bridge is a feature flag read from the environment
======================================================================================

`Flags.load()` parses and validates once at startup; `flags.table()` renders the
active configuration for the banner and /health. Groups:

    model       the vLLM-Omni duplex server: URL, model, voice, VAD, greeting
    audio       Vobiz stream formats in both directions
    xml         what the answer XML contains: greeting, recording, stream attrs
    amd         answering-machine detection and what to do with a machine
    tools       which functions the agent may call, and the goodbye fallback
    call        outbound call parameters (ring url, time limit, caller name)
    compliance  India calling rules enforced before dialling
    security    callback signature verification and admin auth
    ops         concurrency, watchdogs, logging
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

from personas import persona

DEFAULT_REALTIME_URL = "ws://vllm-omni:8091/v1/realtime?duplex=1"
DEFAULT_MODEL = "openbmb/MiniCPM-o-4_5"
DEFAULT_GOODBYE_PHRASES = "goodbye,good bye,bye bye,have a nice day,have a good day,take care"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    if lo is not None and value < lo:
        raise ValueError(f"{name}={value} is below the minimum {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{name}={value} is above the maximum {hi}")
    return value


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _str(name, default).lower() or default
    if value not in allowed:
        raise ValueError(f"{name}={value!r} must be one of {allowed}")
    return value


def health_url_for(realtime_url: str) -> str:
    """ws://host:port/v1/realtime?duplex=1 -> http://host:port/health"""
    base = realtime_url.split("/v1/")[0].split("?")[0]
    base = base.replace("wss://", "https://").replace("ws://", "http://")
    return base.rstrip("/") + "/health"


@dataclass
class Flags:
    # ---- model (vLLM-Omni duplex server) ------------------------------------
    realtime_url: str = DEFAULT_REALTIME_URL
    omni_health_url: str = ""  # derived from REALTIME_URL unless set
    realtime_model: str = DEFAULT_MODEL
    api_key: str = ""  # only when the server sits behind an authenticating proxy
    voice: str = ""  # passed as audio.output.voice when the model supports named voices
    temperature: float = 0.6
    max_output_tokens: int = 0  # 0 = server default
    persona: str = "generic"
    greeting: str = ""
    system_prompt: str = ""
    input_transcription: bool = True  # ask for caller transcripts when the model emits them
    vad_threshold: str = ""
    vad_silence_ms: str = ""
    vad_prefix_padding_ms: str = ""
    handshake_timeout_s: float = 10.0
    model_connect_retries: int = 2
    require_duplex: bool = True  # refuse a session whose session.created carries no capabilities

    # ---- audio ------------------------------------------------------------
    stream_content_type: str = "audio/x-l16;rate=16000"  # Vobiz -> bridge
    output_content_type: str = "audio/x-l16"  # bridge -> Vobiz
    output_sample_rate: int = 24000
    model_input_rate: int = 16000
    play_chunk_ms: int = 20
    l16_big_endian: bool = False
    gate_on_cleared_audio: bool = True
    barge_in_protect_ms: int = 700  # ignore speech_started this soon after a reply starts
    interrupt_response: bool = True  # let the server cancel a reply on caller speech

    # ---- xml --------------------------------------------------------------
    greeting_source: str = "speak"  # speak (Vobiz TTS before the stream) | model (duplex_initial_user_text) | none
    speak_voice: str = "WOMAN"
    speak_language: str = "en-US"
    record_calls: bool = False
    record_format: str = "mp3"
    record_max_length: int = 3600
    record_transcribe: bool = False
    record_disclosure: str = ""
    stream_max_retries: int = 2
    stream_timeout_s: int = 3600
    stream_status_callbacks: bool = True
    stream_extra_headers: str = ""

    # ---- amd --------------------------------------------------------------
    amd_mode: str = "off"  # off | sync | hangup | async
    amd_on_machine: str = "voicemail"  # voicemail | hangup | continue
    amd_hold_before_agent: bool = True  # async: hold the leg until the callback
    amd_time_ms: int = 5000
    amd_initial_silence_ms: int = 4500
    amd_initial_greeting_ms: int = 1500
    amd_maximum_words: int = 3
    amd_maximum_speech_length_ms: int = 5000
    voicemail_message: str = ""
    voicemail_wait_s: int = 15

    # ---- tools ------------------------------------------------------------
    tool_end_call: bool = True
    tool_transfer: bool = False
    transfer_number: str = ""
    transfer_timeout_s: int = 30
    transfer_announce: str = "Please hold while I connect you to a colleague."
    tool_send_dtmf: bool = False
    dtmf_to_model: bool = True
    end_call_on_goodbye: bool = True  # models without tool calling: hang up after the agent says goodbye
    goodbye_phrases: str = DEFAULT_GOODBYE_PHRASES

    # ---- call -------------------------------------------------------------
    from_number: str = ""
    to_number: str = ""
    caller_name: str = ""
    time_limit_s: int = 1800
    ring_callbacks: bool = True
    send_digits: str = ""

    # ---- compliance (India) ----------------------------------------------
    region: str = "IN"
    campaign_type: str = "transactional"  # transactional | promotional
    enforce_calling_window: bool = True  # promotional: 10:00-20:00 IST
    consent_required: bool = False
    require_india_caller_id: bool = True

    # ---- security ---------------------------------------------------------
    verify_signatures: bool = False
    vobiz_auth_token: str = ""
    vobiz_auth_id: str = ""
    admin_token: str = ""
    ws_path_secret: str = ""

    # ---- ops --------------------------------------------------------------
    max_concurrent_calls: int = 8
    max_call_seconds: int = 900
    caller_silence_hangup_s: int = 0  # 0 disables
    checkpoint_timeout_s: int = 15
    json_logs: bool = False
    events_jsonl: bool = True
    http_port: int = 8000
    public_url: str = ""

    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> Flags:
        f = cls()
        p = persona(_str("PERSONA", "generic"))
        f.persona = _str("PERSONA", "generic")

        f.realtime_url = _str("REALTIME_URL", f.realtime_url)
        f.omni_health_url = _str("OMNI_HEALTH_URL") or health_url_for(f.realtime_url)
        f.realtime_model = _str("OMNI_MODEL") or _str("REALTIME_MODEL", f.realtime_model)
        f.api_key = _str("REALTIME_API_KEY")
        f.voice = _str("VOICE", p["voice"])
        f.temperature = float(_str("TEMPERATURE", str(f.temperature)))
        f.max_output_tokens = _int("MAX_OUTPUT_TOKENS", 0, 0, 4096)
        f.greeting = _str("AGENT_GREETING", p["greeting"])
        f.system_prompt = _str("AGENT_SYSTEM_PROMPT", p["system_prompt"])
        f.input_transcription = _bool("INPUT_TRANSCRIPTION", True)
        f.vad_threshold = _str("VAD_THRESHOLD")
        f.vad_silence_ms = _str("VAD_SILENCE_MS")
        f.vad_prefix_padding_ms = _str("VAD_PREFIX_PADDING_MS")
        f.handshake_timeout_s = float(_str("HANDSHAKE_TIMEOUT_S", "10"))
        f.model_connect_retries = _int("MODEL_CONNECT_RETRIES", 2, 0, 5)
        f.require_duplex = _bool("REQUIRE_DUPLEX", True)

        f.stream_content_type = _str("STREAM_CONTENT_TYPE", f.stream_content_type)
        if f.stream_content_type not in ("audio/x-l16;rate=8000", "audio/x-l16;rate=16000", "audio/x-mulaw;rate=8000"):
            raise ValueError(
                "STREAM_CONTENT_TYPE must be audio/x-l16;rate=8000, audio/x-l16;rate=16000 or audio/x-mulaw;rate=8000 (24 kHz inbound never delivers media)"
            )
        f.output_content_type = _choice("OUTPUT_CONTENT_TYPE", "audio/x-l16", ("audio/x-l16", "audio/x-mulaw"))
        f.output_sample_rate = _int("OUTPUT_SAMPLE_RATE", 24000)
        if f.output_content_type == "audio/x-mulaw":
            f.output_sample_rate = 8000
        elif f.output_sample_rate not in (8000, 16000, 24000):
            raise ValueError("OUTPUT_SAMPLE_RATE must be 8000, 16000 or 24000 for L16")
        f.model_input_rate = _int("MODEL_INPUT_RATE", 16000, 8000, 48000)
        f.play_chunk_ms = _int("PLAY_CHUNK_MS", 20, 10, 100)
        f.l16_big_endian = _str("VOBIZ_L16_ENDIAN", "le").lower() == "be"
        f.gate_on_cleared_audio = _bool("GATE_ON_CLEARED_AUDIO", True)
        f.barge_in_protect_ms = _int("BARGE_IN_PROTECT_MS", 700, 0, 5000)
        f.interrupt_response = _bool("INTERRUPT_RESPONSE", True)

        f.greeting_source = _choice("GREETING_SOURCE", "speak", ("speak", "model", "none"))
        f.speak_voice = _choice("SPEAK_VOICE", "WOMAN", ("WOMAN", "MAN")).upper() if _str("SPEAK_VOICE") else "WOMAN"
        f.speak_language = _str("SPEAK_LANGUAGE", p["speak_language"])
        f.record_calls = _bool("RECORD_CALLS", False)
        f.record_format = _choice("RECORD_FORMAT", "mp3", ("mp3", "wav"))
        f.record_max_length = _int("RECORD_MAX_LENGTH", 3600, 1)
        f.record_transcribe = _bool("RECORD_TRANSCRIBE", False)
        f.record_disclosure = _str("RECORD_DISCLOSURE", "This call may be recorded for quality purposes." if f.record_calls else "")
        f.stream_max_retries = _int("STREAM_MAX_RETRIES", 2, 0, 10)
        f.stream_timeout_s = _int("STREAM_TIMEOUT_S", 3600, 1)
        f.stream_status_callbacks = _bool("STREAM_STATUS_CALLBACKS", True)
        f.stream_extra_headers = _str("STREAM_EXTRA_HEADERS")

        f.amd_mode = _choice("AMD_MODE", "off", ("off", "sync", "hangup", "async"))
        f.amd_on_machine = _choice("AMD_ON_MACHINE", "voicemail", ("voicemail", "hangup", "continue"))
        f.amd_hold_before_agent = _bool("AMD_HOLD_BEFORE_AGENT", True)
        f.amd_time_ms = _int("AMD_TIME_MS", 5000, 2000, 10000)
        f.amd_initial_silence_ms = _int("AMD_INITIAL_SILENCE_MS", 4500, 2000, 10000)
        f.amd_initial_greeting_ms = _int("AMD_INITIAL_GREETING_MS", 1500, 1000, 5000)
        f.amd_maximum_words = _int("AMD_MAXIMUM_WORDS", 3, 2, 10)
        f.amd_maximum_speech_length_ms = _int("AMD_MAXIMUM_SPEECH_LENGTH_MS", 5000, 1000, 6000)
        f.voicemail_message = _str("VOICEMAIL_MESSAGE")
        f.voicemail_wait_s = _int("VOICEMAIL_WAIT_S", 15, 1, 60)

        f.tool_end_call = _bool("TOOL_END_CALL", True)
        f.tool_transfer = _bool("TOOL_TRANSFER", False)
        f.transfer_number = _str("TRANSFER_NUMBER")
        f.transfer_timeout_s = _int("TRANSFER_TIMEOUT_S", 30, 5, 120)
        f.transfer_announce = _str("TRANSFER_ANNOUNCE", f.transfer_announce)
        f.tool_send_dtmf = _bool("TOOL_SEND_DTMF", False)
        f.dtmf_to_model = _bool("DTMF_TO_MODEL", True)
        f.end_call_on_goodbye = _bool("END_CALL_ON_GOODBYE", True)
        f.goodbye_phrases = _str("GOODBYE_PHRASES", DEFAULT_GOODBYE_PHRASES)

        f.from_number = _str("FROM_NUMBER")
        f.to_number = _str("TO_NUMBER")
        f.caller_name = _str("CALLER_NAME")[:50]
        f.time_limit_s = _int("TIME_LIMIT_S", 1800, 30, 86400)
        f.ring_callbacks = _bool("RING_CALLBACKS", True)
        f.send_digits = _str("SEND_DIGITS")

        f.region = _str("REGION", "IN").upper()
        f.campaign_type = _choice("CAMPAIGN_TYPE", "transactional", ("transactional", "promotional"))
        f.enforce_calling_window = _bool("ENFORCE_CALLING_WINDOW", True)
        f.consent_required = _bool("CONSENT_REQUIRED", False)
        f.require_india_caller_id = _bool("REQUIRE_INDIA_CALLER_ID", True)

        f.verify_signatures = _bool("VERIFY_SIGNATURES", False)
        f.vobiz_auth_token = _str("VOBIZ_AUTH_TOKEN")
        f.vobiz_auth_id = _str("VOBIZ_AUTH_ID")
        f.admin_token = _str("ADMIN_TOKEN")
        f.ws_path_secret = _str("WS_PATH_SECRET")

        f.max_concurrent_calls = _int("MAX_CONCURRENT_CALLS", 8, 1, 500)
        f.max_call_seconds = _int("MAX_CALL_SECONDS", 900, 30, 86400)
        f.caller_silence_hangup_s = _int("CALLER_SILENCE_HANGUP_S", 0, 0, 3600)
        f.checkpoint_timeout_s = _int("CHECKPOINT_TIMEOUT_S", 15, 1, 120)
        f.json_logs = _bool("JSON_LOGS", False)
        f.events_jsonl = _bool("EVENTS_JSONL", True)
        f.http_port = _int("HTTP_PORT", 8000, 1, 65535)
        f.public_url = _str("PUBLIC_URL").rstrip("/")

        f.validate()
        return f

    def validate(self):
        w = self.warnings
        if "duplex=" not in self.realtime_url and not self.realtime_url.rstrip("/").endswith("/v1/duplex"):
            w.append("REALTIME_URL has no duplex=1 query: a duplex-capable server will fall back to the turn-based handler")
        if self.tool_transfer and not self.transfer_number:
            raise ValueError("TOOL_TRANSFER=true needs TRANSFER_NUMBER")
        if self.amd_mode != "off" and self.amd_on_machine == "voicemail" and not self.voicemail_message:
            w.append("AMD_ON_MACHINE=voicemail without VOICEMAIL_MESSAGE: machines will be hung up instead")
        if self.verify_signatures and not self.vobiz_auth_token:
            raise ValueError("VERIFY_SIGNATURES=true needs VOBIZ_AUTH_TOKEN (the signing key)")
        if self.record_calls and not self.record_disclosure:
            w.append("RECORD_CALLS=true with no RECORD_DISCLOSURE")
        if self.require_india_caller_id and self.from_number and not self.from_number.startswith("+91"):
            w.append("FROM_NUMBER is not an Indian (+91) Vobiz number; domestic routes require one (media anchoring)")
        if self.tool_end_call and not self.end_call_on_goodbye:
            w.append("TOOL_END_CALL without END_CALL_ON_GOODBYE: MiniCPM-o has no tool calling, so only the watchdogs end a call")

    # ------------------------------------------------------------------
    def public(self) -> dict:
        """Everything except secrets, for /health and /flags."""
        out = {}
        for fld in fields(self):
            if fld.name in ("api_key", "vobiz_auth_id", "vobiz_auth_token", "admin_token", "ws_path_secret", "warnings"):
                continue
            out[fld.name] = getattr(self, fld.name)
        out["api_key"] = "set" if self.api_key else "none"
        out["warnings"] = list(self.warnings)
        return out

    def table(self) -> str:
        rows = self.public()
        width = max(len(k) for k in rows)
        return "\n".join(f"  {k:<{width}}  {v}" for k, v in rows.items() if k != "warnings")

    @property
    def out_encoding(self) -> str:
        return "mulaw" if self.output_content_type == "audio/x-mulaw" else "l16"

    @property
    def model_output_rate(self) -> int:
        return self.output_sample_rate if self.out_encoding == "l16" else 8000

    @property
    def goodbye_list(self) -> list[str]:
        return [p.strip().lower() for p in self.goodbye_phrases.split(",") if p.strip()]


_FLAGS: Flags | None = None


def flags() -> Flags:
    global _FLAGS
    if _FLAGS is None:
        _FLAGS = Flags.load()
    return _FLAGS


def reload() -> Flags:
    global _FLAGS
    _FLAGS = Flags.load()
    return _FLAGS
