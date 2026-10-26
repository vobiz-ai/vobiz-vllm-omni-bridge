import config
import realtime


def load(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.Flags.load()


def test_session_update_carries_the_duplex_keys(monkeypatch):
    f = load(
        monkeypatch,
        OMNI_MODEL="openbmb/MiniCPM-o-4_5",
        REALTIME_URL="ws://gpu:8091/v1/realtime?duplex=1",
        AGENT_GREETING="Hello there",
        GREETING_SOURCE="model",
        INPUT_TRANSCRIPTION="true",
    )
    ev = realtime.session_update(f)
    s = ev["session"]
    assert ev["type"] == "session.update"
    assert s["model"] == "openbmb/MiniCPM-o-4_5"
    assert s["modalities"] == ["audio", "text"]
    assert s["input_audio_format"] == "pcm16" and s["output_audio_format"] == "pcm16"
    assert s["sample_rate_hz"] == 16000
    assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["turn_detection"]["type"] == "server_vad"
    assert s["overlap_policy"] == "barge_in_on_speech"
    assert s["extra_body"]["duplex_initial_user_text"].endswith("Hello there")
    assert [t["name"] for t in s["tools"]] == ["end_call"]
    assert realtime.connect_url(f) == "ws://gpu:8091/v1/realtime?duplex=1"


def test_speak_greeting_does_not_also_ask_the_model_to_greet(monkeypatch):
    f = load(monkeypatch, GREETING_SOURCE="speak", AGENT_GREETING="Hello there")
    assert "extra_body" not in realtime.session_update(f)["session"]
    f = load(monkeypatch, GREETING_SOURCE="none", AGENT_GREETING="Hello there")
    assert "extra_body" not in realtime.session_update(f)["session"]


def test_voice_and_limits_are_optional(monkeypatch):
    s = realtime.session_update(load(monkeypatch))["session"]
    assert "voice" not in s["audio"]["output"] and "max_response_output_tokens" not in s
    s = realtime.session_update(load(monkeypatch, VOICE="alloy", MAX_OUTPUT_TOKENS="300"))["session"]
    assert s["audio"]["output"]["voice"] == "alloy" and s["max_response_output_tokens"] == 300


def test_tools_follow_flags(monkeypatch):
    f = load(monkeypatch, TOOL_TRANSFER="true", TRANSFER_NUMBER="+919999999999", TOOL_SEND_DTMF="true")
    names = [t["name"] for t in realtime.session_update(f)["session"]["tools"]]
    assert names == ["end_call", "transfer_to_human", "send_dtmf"]
    f = load(monkeypatch, TOOL_END_CALL="false", TOOL_TRANSFER="false", TOOL_SEND_DTMF="false")
    assert "tools" not in realtime.session_update(f)["session"]


def test_vad_env_passthrough(monkeypatch):
    f = load(monkeypatch, VAD_THRESHOLD="0.8", VAD_SILENCE_MS="700", VAD_PREFIX_PADDING_MS="200")
    assert realtime.turn_detection(f) == {"type": "server_vad", "threshold": 0.8, "silence_duration_ms": 700, "prefix_padding_ms": 200}
    f = load(monkeypatch, INTERRUPT_RESPONSE="false")
    assert realtime.turn_detection(f)["interrupt_response"] is False


def test_goodbye_detection():
    assert realtime.said_goodbye("Thank you, goodbye!", ["goodbye", "take care"])
    assert realtime.said_goodbye("Alright, take care now.", ["goodbye", "take care"])
    assert not realtime.said_goodbye("Good, the bye-laws allow parking.", ["goodbye"])
    assert not realtime.said_goodbye("Goodbye is a word I will not say until the end.", [])
