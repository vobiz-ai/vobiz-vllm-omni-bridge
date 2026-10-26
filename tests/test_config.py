import pytest

import config


def load(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.Flags.load()


def test_defaults_and_persona(monkeypatch):
    f = load(monkeypatch, PERSONA="india-clinic")
    assert f.realtime_model == "mock"  # pinned by conftest
    assert "Namaste" in f.greeting and "Bengaluru" in f.system_prompt
    assert f.speak_language == "en-GB"
    assert f.stream_content_type == "audio/x-l16;rate=16000"
    assert f.model_output_rate == 24000
    assert f.goodbye_list[:2] == ["goodbye", "good bye"]


def test_health_url_derived_from_realtime_url(monkeypatch):
    f = load(monkeypatch, REALTIME_URL="ws://gpu-host:8091/v1/realtime?duplex=1")
    assert f.omni_health_url == "http://gpu-host:8091/health"
    f = load(monkeypatch, REALTIME_URL="wss://omni.example.com/v1/duplex", OMNI_HEALTH_URL="https://omni.example.com/healthz")
    assert f.omni_health_url == "https://omni.example.com/healthz"
    assert not f.warnings  # /v1/duplex is the duplex alias


def test_non_duplex_url_warns(monkeypatch):
    f = load(monkeypatch, REALTIME_URL="ws://gpu-host:8091/v1/realtime")
    assert any("duplex=1" in w for w in f.warnings)


def test_mulaw_output_forces_8k(monkeypatch):
    f = load(monkeypatch, OUTPUT_CONTENT_TYPE="audio/x-mulaw", OUTPUT_SAMPLE_RATE="24000")
    assert f.out_encoding == "mulaw" and f.model_output_rate == 8000


def test_invalid_inbound_rate_rejected(monkeypatch):
    with pytest.raises(ValueError):
        load(monkeypatch, STREAM_CONTENT_TYPE="audio/x-l16;rate=24000")


def test_amd_ranges_validated(monkeypatch):
    with pytest.raises(ValueError):
        load(monkeypatch, AMD_MODE="async", AMD_TIME_MS="1000")
    f = load(monkeypatch, AMD_MODE="async", AMD_TIME_MS="4000", VOICEMAIL_MESSAGE="Hi")
    assert f.amd_mode == "async" and f.amd_time_ms == 4000


def test_transfer_requires_number(monkeypatch):
    with pytest.raises(ValueError):
        load(monkeypatch, TOOL_TRANSFER="true", TRANSFER_NUMBER="")


def test_invalid_greeting_source_rejected(monkeypatch):
    with pytest.raises(ValueError):
        load(monkeypatch, GREETING_SOURCE="tts")


def test_warnings_not_fatal(monkeypatch):
    f = load(monkeypatch, RECORD_CALLS="true", RECORD_DISCLOSURE="", END_CALL_ON_GOODBYE="false")
    assert any("RECORD_DISCLOSURE" in w for w in f.warnings)
    assert any("END_CALL_ON_GOODBYE" in w for w in f.warnings)


def test_public_hides_secrets(monkeypatch):
    f = load(monkeypatch, REALTIME_API_KEY="secret", ADMIN_TOKEN="adm")
    pub = f.public()
    assert pub["api_key"] == "set"
    assert "admin_token" not in pub and "vobiz_auth_token" not in pub and "vobiz_auth_id" not in pub
