import importlib
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

import config


def make_app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.reload()
    import app as bridge_app

    importlib.reload(bridge_app)
    bridge_app.BASE_URL = "https://bridge.example.test"
    return bridge_app, TestClient(bridge_app.app)


def test_answer_xml_default(monkeypatch):
    _, c = make_app(monkeypatch, PERSONA="india-clinic")
    resp = c.post("/answer", data={"CallUUID": "abc", "From": "+911234567890", "Direction": "outbound"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/xml")
    root = ET.fromstring(resp.text)
    speak = root.find("Speak")
    stream = root.find("Stream")
    assert speak is not None and "Namaste" in speak.text and speak.attrib["language"] == "en-GB"
    assert stream.attrib["bidirectional"] == "true" and stream.attrib["keepCallAlive"] == "true"
    assert stream.attrib["contentType"] == "audio/x-l16;rate=16000"
    assert stream.attrib["maxRetries"] == "2" and stream.attrib["streamTimeout"] == "3600"
    assert stream.text.strip() == "wss://bridge.example.test/ws"
    assert root.find("Hangup") is not None and root.find("Record") is None


def test_answer_xml_with_recording_and_ws_secret(monkeypatch):
    _, c = make_app(monkeypatch, RECORD_CALLS="true", RECORD_FORMAT="wav", WS_PATH_SECRET="s3cr3t", GREETING_SOURCE="none")
    root = ET.fromstring(c.post("/answer", data={"CallUUID": "abc"}).text)
    rec = root.find("Record")
    assert rec.attrib["recordSession"] == "true" and rec.attrib["redirect"] == "false" and rec.attrib["fileFormat"] == "wav"
    assert rec.attrib["callbackUrl"] == "https://bridge.example.test/record-status"
    assert list(root)[0].tag == "Record"  # recording starts first
    assert root.find("Stream").text.strip() == "wss://bridge.example.test/ws/s3cr3t"
    # record-action must answer with an empty Response
    assert c.post("/record-action", data={"CallUUID": "abc", "RecordingID": "r1"}).text.strip().endswith("<Response></Response>")


def test_amd_sync_machine_goes_to_voicemail(monkeypatch):
    _, c = make_app(monkeypatch, AMD_MODE="sync", VOICEMAIL_MESSAGE="Please call us back.")
    root = ET.fromstring(c.post("/answer", data={"CallUUID": "m1", "Machine": "true", "Direction": "outbound"}).text)
    assert root.find("Stream") is None
    assert root.find("Wait").attrib["silence"] == "true"
    assert "call us back" in root.find("Speak").text
    human = ET.fromstring(c.post("/answer", data={"CallUUID": "h1", "Machine": "false", "Direction": "outbound"}).text)
    assert human.find("Stream") is not None


def test_amd_async_hold_then_branch(monkeypatch):
    app_mod, c = make_app(monkeypatch, AMD_MODE="async", AMD_TIME_MS="4000", VOICEMAIL_MESSAGE="Bye", AMD_HOLD_BEFORE_AGENT="true")
    first = ET.fromstring(c.post("/answer", data={"CallUUID": "a1", "Direction": "outbound"}).text)
    assert first.find("Wait").attrib["length"] == "5"
    assert first.find("Redirect").text == "https://bridge.example.test/answer?amd=1"
    c.post("/amd", data={"CallUUID": "a1", "Machine": "true"})
    second = ET.fromstring(c.post("/answer?amd=1", data={"CallUUID": "a1", "Direction": "outbound"}).text)
    assert second.find("Stream") is None and second.find("Speak") is not None
    c.post("/amd", data={"CallUUID": "a2", "Machine": "false"})
    third = ET.fromstring(c.post("/answer?amd=1", data={"CallUUID": "a2", "Direction": "outbound"}).text)
    assert third.find("Stream") is not None


def test_busy_rejection(monkeypatch):
    app_mod, c = make_app(monkeypatch, MAX_CONCURRENT_CALLS="1")
    app_mod.ACTIVE.add("busy-call")
    root = ET.fromstring(c.post("/answer", data={"CallUUID": "x"}).text)
    assert root.find("Stream") is None and "busy" in root.find("Speak").text
    assert c.get("/metrics").text.count("vobiz_bridge_calls_rejected_busy 1") == 1


def test_transfer_xml(monkeypatch):
    _, c = make_app(monkeypatch, TOOL_TRANSFER="true", TRANSFER_NUMBER="+919999999999", FROM_NUMBER="+919876543210")
    root = ET.fromstring(c.post("/transfer", data={"CallUUID": "t1"}).text)
    dial = root.find("Dial")
    assert dial.attrib["callerId"] == "+919876543210" and dial.find("Number").text == "+919999999999"
    assert dial.attrib["action"] == "https://bridge.example.test/dial-status"


def test_signature_enforced(monkeypatch):
    import vobiz_api

    _, c = make_app(monkeypatch, VERIFY_SIGNATURES="true", VOBIZ_AUTH_TOKEN="tok")
    assert c.post("/hangup", data={"CallUUID": "s1"}).status_code == 403
    headers = vobiz_api.sign_v3("https://bridge.example.test/hangup", "tok", "11111111111111111111")
    assert c.post("/hangup", data={"CallUUID": "s1"}, headers=headers).status_code == 200


def test_admin_token_and_health(monkeypatch):
    _, c = make_app(monkeypatch, ADMIN_TOKEN="adm")
    assert c.get("/calls").status_code == 401
    assert c.get("/calls", headers={"x-admin-token": "adm"}).status_code == 200
    h = c.get("/health").json()
    assert h["stream_ws"] == "wss://bridge.example.test/ws" and h["model"] == "mock" and "omni" in h
