"""
app.py — Vobiz XML streaming server with every feature behind a flag
=====================================================================

    GET/POST /answer            the XML Vobiz runs when the call is answered
    POST     /answer-after-amd  second fetch after an async AMD hold (<Redirect>)
    POST     /amd               machine_detection_url callback
    POST     /voicemail         XML that waits for the beep and leaves VOICEMAIL_MESSAGE
    POST     /transfer          XML that <Dial>s TRANSFER_NUMBER
    POST     /dial-status       <Dial action> result
    WS       /ws[/<secret>]     the bidirectional media stream
    POST     /stream-status     <Stream statusCallbackUrl>
    POST     /record-action     <Record action>   (must return an empty Response)
    POST     /record-status     <Record callbackUrl> RecordStop
    POST     /ring /fallback /hangup
    GET      /health /flags /metrics /calls

Run it, then place a call with `python call.py`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import config  # noqa: E402
import realtime  # noqa: E402
import vobiz_api  # noqa: E402
from tools import ToolRunner  # noqa: E402

F = config.flags()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
        )


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
if F.json_logs:
    for h in logging.getLogger().handlers:
        h.setFormatter(JsonFormatter())
logger = logging.getLogger("app")
for w in F.warnings:
    logger.warning(f"config: {w}")

BASE_URL = F.public_url
CALLS: list[dict] = []
MAX_CALLS = 100
AMD_RESULTS: dict[str, dict] = {}
ACTIVE: set[str] = set()
METRICS = {
    "calls_answered": 0,
    "calls_rejected_busy": 0,
    "streams": 0,
    "barge_ins": 0,
    "tool_calls": 0,
    "model_errors": 0,
    "machines_detected": 0,
    "transfers": 0,
    "hangups": 0,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def xe(value) -> str:
    return escape(str(value), quote=True)


def url(path: str) -> str:
    return f"{BASE_URL}{path}"


def ws_url() -> str:
    raw = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    if F.ws_path_secret:
        raw += f"/{F.ws_path_secret}"
    return xe(raw)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


async def call_params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        body = (await request.body()).decode(errors="replace")
        if "json" in request.headers.get("content-type", ""):
            try:
                params.update(json.loads(body or "{}"))
            except json.JSONDecodeError:
                pass
        else:
            params.update(dict(parse_qsl(body)))
    return params


def record(call_uuid: str, kind: str, payload: dict) -> dict:
    call_uuid = call_uuid or "unknown"
    for entry in CALLS:
        if entry["call_uuid"] == call_uuid:
            break
    else:
        entry = {"call_uuid": call_uuid, "started": now(), "model": F.realtime_model, "events": [], "turns": []}
        CALLS.insert(0, entry)
        del CALLS[MAX_CALLS:]
    item = {"at": now(), "kind": kind, **payload}
    entry["turns" if kind == "transcript" else "events"].append(item)
    if F.events_jsonl:
        with open(DATA_DIR / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"call_uuid": call_uuid, **item}, default=str) + "\n")
    return entry


def persist(entry: dict) -> Path:
    safe = "".join(ch for ch in entry["call_uuid"] if ch.isalnum() or ch in "-_")[:48] or "unknown"
    path = DATA_DIR / f"call-{safe}.json"
    path.write_text(json.dumps(entry, indent=2, default=str))
    return path


def verify(request: Request):
    """Reject callbacks that are not signed by Vobiz when VERIFY_SIGNATURES is on."""
    if not F.verify_signatures:
        return
    full = url(request.url.path)
    if not vobiz_api.verify_signature(full, F.vobiz_auth_token, dict(request.headers)):
        logger.warning(f"unsigned or badly signed callback on {request.url.path}")
        raise HTTPException(status_code=403, detail="bad signature")


def admin(request: Request):
    if F.admin_token and request.headers.get("x-admin-token") != F.admin_token:
        raise HTTPException(status_code=401, detail="admin token required")


def xml(body: str) -> Response:
    document = '<?xml version="1.0" encoding="UTF-8"?>\n' + body.strip()
    logger.info(f"XML:\n{document}")
    return Response(content=document, media_type="text/xml")


# ---------------------------------------------------------------------------
# XML builders
# ---------------------------------------------------------------------------
def speak(text: str) -> str:
    return f'    <Speak voice="{xe(F.speak_voice)}" language="{xe(F.speak_language)}">{xe(text)}</Speak>\n' if text else ""


def agent_xml() -> str:
    parts = []
    if F.record_calls:
        attrs = (
            f'recordSession="true" redirect="false" playBeep="false" fileFormat="{F.record_format}" '
            f'maxLength="{F.record_max_length}" action="{xe(url("/record-action"))}" method="POST" '
            f'callbackUrl="{xe(url("/record-status"))}" callbackMethod="POST"'
        )
        if F.record_transcribe:
            attrs += f' transcriptionType="auto" transcriptionUrl="{xe(url("/record-transcription"))}" transcriptionMethod="POST"'
        parts.append(f"    <Record {attrs}/>\n")
        parts.append(speak(F.record_disclosure))
    if F.greeting_source == "speak":
        parts.append(speak(F.greeting))
    stream_attrs = (
        f'bidirectional="true" audioTrack="inbound" keepCallAlive="true" '
        f'contentType="{xe(F.stream_content_type)}" maxRetries="{F.stream_max_retries}" '
        f'streamTimeout="{F.stream_timeout_s}"'
    )
    if F.stream_status_callbacks:
        stream_attrs += f' statusCallbackUrl="{xe(url("/stream-status"))}" statusCallbackMethod="POST"'
    if F.stream_extra_headers:
        stream_attrs += f' extraHeaders="{xe(F.stream_extra_headers)}"'
    parts.append(f"    <Stream {stream_attrs}>\n        {ws_url()}\n    </Stream>\n")
    parts.append("    <Hangup/>\n")
    return "<Response>\n" + "".join(parts) + "</Response>"


def voicemail_xml() -> str:
    if not F.voicemail_message:
        return "<Response>\n    <Hangup/>\n</Response>"
    return (
        "<Response>\n"
        f'    <Wait length="{F.voicemail_wait_s}" silence="true" minSilence="2000"/>\n'
        f"{speak(F.voicemail_message)}"
        "    <Hangup/>\n</Response>"
    )


def busy_xml() -> str:
    return "<Response>\n" + speak("Sorry, all our lines are busy right now. Please call again shortly.") + "    <Hangup/>\n</Response>"


def transfer_xml() -> str:
    return (
        "<Response>\n"
        f"{speak(F.transfer_announce)}"
        f'    <Dial callerId="{xe(F.from_number)}" timeout="{F.transfer_timeout_s}" action="{xe(url("/dial-status"))}" method="POST">\n'
        f"        <Number>{xe(F.transfer_number)}</Number>\n"
        "    </Dial>\n"
        f"{speak('Sorry, nobody is available right now. We will call you back.')}"
        "    <Hangup/>\n</Response>"
    )


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------
app = FastAPI(title="Vobiz × vLLM-Omni bridge")


@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    verify(request)
    params = await call_params(request)
    call_uuid = params.get("CallUUID", "")
    record(call_uuid, "answer", {"params": params})
    logger.info(f"answer — call {call_uuid or '(none)'} from {params.get('From')} machine={params.get('Machine')}")

    if len(ACTIVE) >= F.max_concurrent_calls:
        METRICS["calls_rejected_busy"] += 1
        return xml(busy_xml())
    METRICS["calls_answered"] += 1

    # Synchronous AMD: Vobiz decided before fetching the XML.
    if F.amd_mode in ("sync", "hangup") and str(params.get("Machine", "")).lower() == "true":
        METRICS["machines_detected"] += 1
        record(call_uuid, "amd", {"machine": True, "mode": F.amd_mode})
        return xml(voicemail_xml() if F.amd_on_machine == "voicemail" else "<Response>\n    <Hangup/>\n</Response>")

    # Asynchronous AMD with a hold: keep quiet until the callback, then refetch.
    if F.amd_mode == "async" and F.amd_hold_before_agent and params.get("Direction") == "outbound" and "amd" not in request.query_params:
        hold = max(1, round(F.amd_time_ms / 1000) + 1)
        return xml(
            f'<Response>\n    <Wait length="{hold}"/>\n    <Redirect method="POST">{xe(url("/answer?amd=1"))}</Redirect>\n</Response>'
        )
    if "amd" in request.query_params:
        result = AMD_RESULTS.get(call_uuid)
        if result and result.get("machine"):
            if F.amd_on_machine == "voicemail":
                return xml(voicemail_xml())
            if F.amd_on_machine == "hangup":
                return xml("<Response>\n    <Hangup/>\n</Response>")
    return xml(agent_xml())


@app.api_route("/amd", methods=["GET", "POST"])
async def amd(request: Request):
    verify(request)
    params = await call_params(request)
    call_uuid = params.get("CallUUID", "")
    machine = str(params.get("Machine", "")).lower() == "true"
    AMD_RESULTS[call_uuid] = {"machine": machine, "at": now()}
    record(call_uuid, "amd", {"machine": machine, "if_machine": params.get("IfMachine")})
    logger.info(f"amd — call {call_uuid[:8]} machine={machine}")
    if machine:
        METRICS["machines_detected"] += 1
        # The agent is already live (no hold): move the leg to voicemail XML or end it.
        if not F.amd_hold_before_agent and call_uuid in ACTIVE:
            if F.amd_on_machine == "voicemail" and F.voicemail_message:
                await vobiz_api.transfer(F, call_uuid, url("/voicemail"))
            elif F.amd_on_machine == "hangup":
                await vobiz_api.hangup(F, call_uuid)
    return JSONResponse({"ok": True})


@app.api_route("/voicemail", methods=["GET", "POST"])
async def voicemail(request: Request):
    verify(request)
    params = await call_params(request)
    record(params.get("CallUUID", ""), "voicemail", {})
    return xml(voicemail_xml())


@app.api_route("/transfer", methods=["GET", "POST"])
async def transfer(request: Request):
    verify(request)
    params = await call_params(request)
    METRICS["transfers"] += 1
    record(params.get("CallUUID", ""), "transfer", {"to": F.transfer_number})
    return xml(transfer_xml())


@app.api_route("/dial-status", methods=["GET", "POST"])
async def dial_status(request: Request):
    verify(request)
    params = await call_params(request)
    record(params.get("CallUUID", ""), "dial-status", {k: params.get(k) for k in ("DialStatus", "DialHangupCause", "DialBLegUUID")})
    return xml("<Response>\n    <Hangup/>\n</Response>")


@app.websocket("/ws")
@app.websocket("/ws/{secret}")
async def stream_ws(websocket: WebSocket, secret: str = ""):
    if F.ws_path_secret and secret != F.ws_path_secret:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    METRICS["streams"] += 1
    state = {"call_uuid": ""}

    def on_event(name: str, data: dict):
        call_uuid = data.get("callId") or (data.get("start") or {}).get("callId") or state["call_uuid"]
        state["call_uuid"] = call_uuid or state["call_uuid"]
        if name == "model:barge_in":
            METRICS["barge_ins"] += 1
        elif name == "tool":
            METRICS["tool_calls"] += 1
        elif name.endswith(":error"):
            METRICS["model_errors"] += 1
        record(state["call_uuid"], name, {k: v for k, v in data.items() if k != "media"})

    def on_turn(role: str, text: str):
        if text:
            logger.info(f"[{role}] {text}")
            record(state["call_uuid"], "transcript", {"role": role, "text": text})

    # The tool runner needs the call UUID, which arrives in the start frame; it
    # is created lazily on the first event that carries it.
    runner_holder: dict = {}

    class LazyRunner:
        def __getattr__(self, item):
            if "runner" not in runner_holder:
                runner_holder["runner"] = ToolRunner(F, state["call_uuid"], BASE_URL, on_event=on_event)
            return getattr(runner_holder["runner"], item)

    engine = realtime
    session = engine.new_session(websocket, flags=F, on_turn=on_turn, on_event=on_event, tool_runner=LazyRunner())
    try:
        while True:
            msg = await websocket.receive_text()
            await session.handle(msg)
            if state["call_uuid"]:
                ACTIVE.add(state["call_uuid"])
    except WebSocketDisconnect:
        logger.info("WebSocket closed by Vobiz")
    except RuntimeError as exc:
        logger.info(f"WebSocket already closed: {exc}")
    except Exception as exc:
        logger.error(f"WebSocket error: {type(exc).__name__}: {exc}")
    finally:
        await session.cleanup()
        ACTIVE.discard(state["call_uuid"])
        for entry in CALLS:
            if entry["call_uuid"] == state["call_uuid"]:
                logger.info(f"call saved to {persist(entry)}")
                break


@app.api_route("/stream-status", methods=["GET", "POST"])
async def stream_status(request: Request):
    verify(request)
    params = await call_params(request)
    record(params.get("CallUUID", ""), "stream-status", {"params": params})
    return JSONResponse({"ok": True})


@app.api_route("/record-action", methods=["GET", "POST"])
async def record_action(request: Request):
    verify(request)
    params = await call_params(request)
    record(params.get("CallUUID", ""), "record-start", {"recording_id": params.get("RecordingID")})
    return xml("<Response></Response>")


@app.api_route("/record-status", methods=["GET", "POST"])
async def record_status(request: Request):
    verify(request)
    params = await call_params(request)
    record(
        params.get("CallUUID", ""),
        "record-stop",
        {k: params.get(k) for k in ("RecordingID", "RecordUrl", "RecordFile", "RecordingDuration", "RecordingEndReason")},
    )
    return JSONResponse({"ok": True})


@app.api_route("/record-transcription", methods=["GET", "POST"])
async def record_transcription(request: Request):
    verify(request)
    params = await call_params(request)
    record(
        params.get("CallUUID", "") or params.get("call_uuid", ""), "record-transcription", {"transcription": params.get("transcription")}
    )
    return JSONResponse({"ok": True})


@app.api_route("/ring", methods=["GET", "POST"])
async def ring(request: Request):
    verify(request)
    params = await call_params(request)
    record(params.get("CallUUID", ""), "ring", {"to": params.get("To")})
    return JSONResponse({"ok": True})


@app.api_route("/fallback", methods=["GET", "POST"])
async def fallback(request: Request):
    params = await call_params(request)
    record(params.get("CallUUID", ""), "fallback", {})
    return xml("<Response>\n" + speak("Sorry, we are having trouble right now. Please call again later.") + "    <Hangup/>\n</Response>")


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup(request: Request):
    verify(request)
    params = await call_params(request)
    METRICS["hangups"] += 1
    logger.info(f"hangup — call {params.get('CallUUID')} duration {params.get('Duration')}s cause {params.get('HangupCauseName')}")
    entry = record(params.get("CallUUID", ""), "hangup", {"params": params})
    ACTIVE.discard(params.get("CallUUID", ""))
    AMD_RESULTS.pop(params.get("CallUUID", ""), None)
    persist(entry)
    return JSONResponse({"ok": True})


_OMNI_PROBE: dict = {"at": 0.0, "status": "unknown"}


async def omni_health() -> str:
    """Probe the model server's /health, cached for five seconds."""
    if time.monotonic() - _OMNI_PROBE["at"] < 5.0:
        return _OMNI_PROBE["status"]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(F.omni_health_url)
        status = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as exc:
        status = f"unreachable: {type(exc).__name__}"
    _OMNI_PROBE.update(at=time.monotonic(), status=status)
    return status


@app.get("/health")
async def health():
    return {
        "ok": True,
        "public_url": BASE_URL,
        "answer_url": url("/answer"),
        "stream_ws": BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws",
        "model": F.realtime_model,
        "omni_url": F.realtime_url,
        "omni": await omni_health(),
        "persona": F.persona,
        "active_calls": len(ACTIVE),
        "flags": F.public(),
        "calls": len(CALLS),
    }


@app.get("/flags")
async def flags_endpoint(request: Request):
    admin(request)
    return F.public()


@app.get("/metrics")
async def metrics():
    lines = [f"vobiz_bridge_{k} {v}" for k, v in METRICS.items()]
    lines.append(f"vobiz_bridge_active_calls {len(ACTIVE)}")
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/calls")
async def calls(request: Request, limit: int = 10):
    admin(request)
    return CALLS[:limit]


# ---------------------------------------------------------------------------
# tunnel + startup
# ---------------------------------------------------------------------------
def _ngrok_url() -> str | None:
    try:
        for t in requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2).json().get("tunnels", []):
            if t.get("public_url", "").startswith("https://") and t.get("config", {}).get("addr", "").endswith(f":{F.http_port}"):
                return t["public_url"].rstrip("/")
    except Exception:
        return None
    return None


def start_tunnel(port: int) -> str | None:
    """PUBLIC_URL wins. Else reuse an ngrok tunnel that targets OUR port. Else start cloudflared."""
    existing = _ngrok_url()
    if existing:
        return existing
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
            stdout=open(DATA_DIR / "cloudflared.log", "w"),
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        logger.error("cloudflared not installed and no ngrok tunnel for this port — set PUBLIC_URL")
        return None
    import re

    for _ in range(40):
        time.sleep(1)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", (DATA_DIR / "cloudflared.log").read_text())
        if m:
            return m.group(0)
    proc.kill()
    return None


def banner():
    print("\n" + "=" * 78)
    print(f"  Vobiz × vLLM-Omni bridge   model={F.realtime_model}  persona={F.persona}")
    print(f"  omni url     {F.realtime_url}")
    print("=" * 78)
    print(f"  public url   {BASE_URL}\n  answer url   {url('/answer')}\n  stream ws    {ws_url()}")
    print(f"  Vobiz -> app {F.stream_content_type}    app -> Vobiz {F.output_content_type}/{F.model_output_rate}")
    print("-" * 78)
    print(F.table())
    print("=" * 78, flush=True)


def main():
    global BASE_URL
    if not BASE_URL:
        BASE_URL = start_tunnel(F.http_port) or ""
    if not BASE_URL:
        raise SystemExit("No public URL. Set PUBLIC_URL in .env, run ngrok for this port, or install cloudflared.")
    (DATA_DIR / "runtime.json").write_text(json.dumps({"public_url": BASE_URL, "port": F.http_port}, indent=2))
    banner()
    uvicorn.run(app, host="0.0.0.0", port=F.http_port, log_level="warning")


if __name__ == "__main__":
    main()
