"""
vobiz_api.py — the Vobiz REST calls the bridge makes, and callback verification
================================================================================

    make_call(...)        POST /Account/{id}/Call/            (with AMD, ring, limits)
    transfer(...)         POST /Account/{id}/Call/{uuid}/     (aleg to new XML)
    hangup(...)           DELETE /Account/{id}/Call/{uuid}
    send_dtmf(...)        POST /Account/{id}/Call/{uuid}/DTMF/
    verify_signature(...) X-Vobiz-Signature-V2 / V3 (HMAC-SHA256 over URL + nonce)

Path casing is part of the contract: `/Account/` and `/Call/` uppercase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import Flags

logger = logging.getLogger("vobiz-api")
API_BASE = "https://api.vobiz.ai/api/v1"
IST = timezone(timedelta(hours=5, minutes=30))


def _headers(flags: Flags) -> dict:
    return {"Content-Type": "application/json", "X-Auth-ID": flags.vobiz_auth_id, "X-Auth-Token": flags.vobiz_auth_token}


def call_payload(flags: Flags, to: str, from_: str, base_url: str) -> dict:
    """Everything the make-call request carries, driven by the flags."""
    p = {
        "from": from_,
        "to": to,
        "answer_url": f"{base_url}/answer",
        "answer_method": "POST",
        "hangup_url": f"{base_url}/hangup",
        "hangup_method": "POST",
        "fallback_url": f"{base_url}/fallback",
        "fallback_method": "POST",
        "time_limit": str(flags.time_limit_s),
    }
    if flags.ring_callbacks:
        p.update({"ring_url": f"{base_url}/ring", "ring_method": "POST"})
    if flags.caller_name:
        p["caller_name"] = flags.caller_name
    if flags.send_digits:
        p["send_digits"] = flags.send_digits
    if flags.amd_mode != "off":
        p["machine_detection"] = "hangup" if flags.amd_mode == "hangup" else "true"
        p["machine_detection_time"] = flags.amd_time_ms
        p["machine_detection_initial_silence"] = flags.amd_initial_silence_ms
        p["machine_detection_initial_greeting"] = flags.amd_initial_greeting_ms
        p["machine_detection_maximum_words"] = flags.amd_maximum_words
        p["machine_detection_maximum_speech_length"] = flags.amd_maximum_speech_length_ms
        if flags.amd_mode == "async":
            p["machine_detection_url"] = f"{base_url}/amd"
            p["machine_detection_method"] = "POST"
    return p


def compliance_check(flags: Flags, to: str, from_: str, consent_ref: str = "", now: datetime | None = None) -> list[str]:
    """India calling rules enforced before a promotional or domestic dial. Returns blockers."""
    problems = []
    now = now or datetime.now(IST)
    domestic = to.startswith("+91")
    if flags.region == "IN" and domestic and flags.require_india_caller_id and not from_.startswith("+91"):
        problems.append("domestic India call needs an Indian Vobiz caller ID (media anchoring, cause 2070 otherwise)")
    if flags.campaign_type == "promotional" and flags.enforce_calling_window:
        hour = now.astimezone(IST).hour
        if not (10 <= hour < 20):
            problems.append(f"promotional calls are allowed 10:00-20:00 IST only; it is {now.astimezone(IST):%H:%M} IST")
    if flags.consent_required and not consent_ref:
        problems.append("CONSENT_REQUIRED=true: pass a consent reference (NDNC/DND consent must be on file, under 6 months old)")
    return problems


async def make_call(flags: Flags, to: str, from_: str, base_url: str) -> dict:
    payload = call_payload(flags, to, from_, base_url)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{API_BASE}/Account/{flags.vobiz_auth_id}/Call/", json=payload, headers=_headers(flags))
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text[:300]}
    body["_status"] = r.status_code
    return body


async def transfer(flags: Flags, call_uuid: str, aleg_url: str) -> int:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{API_BASE}/Account/{flags.vobiz_auth_id}/Call/{call_uuid}/",
            json={"legs": "aleg", "aleg_url": aleg_url, "aleg_method": "POST"},
            headers=_headers(flags),
        )
    logger.info(f"transfer {call_uuid[:8]} -> {aleg_url}: HTTP {r.status_code}")
    return r.status_code


async def hangup(flags: Flags, call_uuid: str) -> int:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{API_BASE}/Account/{flags.vobiz_auth_id}/Call/{call_uuid}", headers=_headers(flags))
    logger.info(f"hangup {call_uuid[:8]}: HTTP {r.status_code}")
    return r.status_code


async def send_dtmf(flags: Flags, call_uuid: str, digits: str, leg: str = "bleg") -> int:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{API_BASE}/Account/{flags.vobiz_auth_id}/Call/{call_uuid}/DTMF/", json={"digits": digits, "leg": leg}, headers=_headers(flags)
        )
    return r.status_code


def verify_signature(callback_url: str, auth_token: str, headers: dict) -> bool:
    """V3 preferred, V2 accepted. Signature covers the URL and a nonce, never the body."""
    h = {k.lower(): v for k, v in headers.items()}
    base_url = callback_url.split("?", 1)[0]
    key = auth_token.encode()
    v3, n3 = h.get("x-vobiz-signature-v3", ""), h.get("x-vobiz-signature-v3-nonce", "")
    if v3 and n3:
        expected = base64.b64encode(hmac.new(key, (base_url + "." + n3).encode(), hashlib.sha256).digest()).decode()
        return hmac.compare_digest(v3, expected)
    v2, n2 = h.get("x-vobiz-signature-v2", ""), h.get("x-vobiz-signature-v2-nonce", "")
    if v2 and n2:
        expected = base64.b64encode(hmac.new(key, (base_url + n2).encode(), hashlib.sha256).digest()).decode()
        return hmac.compare_digest(v2, expected)
    return False


def sign_v3(callback_url: str, auth_token: str, nonce: str) -> dict:
    """Produce the headers Vobiz would send (used by the tests)."""
    base_url = callback_url.split("?", 1)[0]
    sig = base64.b64encode(hmac.new(auth_token.encode(), (base_url + "." + nonce).encode(), hashlib.sha256).digest()).decode()
    return {"X-Vobiz-Signature-V3": sig, "X-Vobiz-Signature-V3-Nonce": nonce}
