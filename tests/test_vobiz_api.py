from datetime import datetime, timedelta, timezone

import config
import vobiz_api

IST = timezone(timedelta(hours=5, minutes=30))


def load(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.Flags.load()


def test_call_payload_with_amd_async(monkeypatch):
    f = load(
        monkeypatch,
        AMD_MODE="async",
        AMD_TIME_MS="4000",
        VOICEMAIL_MESSAGE="x",
        CALLER_NAME="Vobiz Clinic",
        TIME_LIMIT_S="600",
        SEND_DIGITS="",
    )
    p = vobiz_api.call_payload(f, "+919111111111", "+919876543210", "https://b.test")
    assert p["answer_url"] == "https://b.test/answer" and p["hangup_url"] == "https://b.test/hangup"
    assert p["ring_url"] == "https://b.test/ring" and p["fallback_url"] == "https://b.test/fallback"
    assert p["machine_detection"] == "true" and p["machine_detection_url"] == "https://b.test/amd"
    assert p["machine_detection_time"] == 4000
    assert p["caller_name"] == "Vobiz Clinic" and p["time_limit"] == "600"


def test_call_payload_hangup_mode_and_no_amd(monkeypatch):
    f = load(monkeypatch, AMD_MODE="hangup", RING_CALLBACKS="false")
    p = vobiz_api.call_payload(f, "+919111111111", "+919876543210", "https://b.test")
    assert p["machine_detection"] == "hangup" and "machine_detection_url" not in p and "ring_url" not in p
    f = load(monkeypatch, AMD_MODE="off")
    assert "machine_detection" not in vobiz_api.call_payload(f, "+919111111111", "+919876543210", "https://b.test")


def test_compliance_window_and_caller_id(monkeypatch):
    f = load(monkeypatch, CAMPAIGN_TYPE="promotional")
    night = datetime(2026, 10, 24, 21, 30, tzinfo=IST)
    day = datetime(2026, 10, 24, 12, 0, tzinfo=IST)
    assert vobiz_api.compliance_check(f, "+919111111111", "+919876543210", now=night)
    assert not vobiz_api.compliance_check(f, "+919111111111", "+919876543210", now=day)
    assert vobiz_api.compliance_check(f, "+919111111111", "+12073687921", now=day)  # non-Indian caller ID
    f = load(monkeypatch, CAMPAIGN_TYPE="transactional", CONSENT_REQUIRED="true")
    assert vobiz_api.compliance_check(f, "+919111111111", "+919876543210", now=night)  # consent missing
    assert not vobiz_api.compliance_check(f, "+919111111111", "+919876543210", "CRM-1", now=night)


def test_signature_v3_and_v2():
    url = "https://b.test/hangup?x=1"
    headers = vobiz_api.sign_v3(url, "tok", "12345678901234567890")
    assert vobiz_api.verify_signature(url, "tok", headers)
    assert not vobiz_api.verify_signature(url, "wrong", headers)
    assert not vobiz_api.verify_signature(url, "tok", {})
    import base64
    import hashlib
    import hmac

    v2 = base64.b64encode(hmac.new(b"tok", b"https://b.test/hangup" + b"n2", hashlib.sha256).digest()).decode()
    assert vobiz_api.verify_signature(url, "tok", {"X-Vobiz-Signature-V2": v2, "X-Vobiz-Signature-V2-Nonce": "n2"})
