"""
call.py — place an outbound Vobiz call into the agent, with every flag applied
==============================================================================

    python call.py                       # dial TO_NUMBER from .env
    python call.py --to +9198XXXXXXXX --consent-ref CRM-12345
    python call.py --force               # skip the India calling-window check

AMD, ring callbacks, time limit and caller name come from the flags. The
compliance check runs first and refuses a promotional call outside 10:00-20:00
IST, a domestic call without an Indian caller ID, or a call without consent
when CONSENT_REQUIRED is on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import config  # noqa: E402
import vobiz_api  # noqa: E402

F = config.flags()


def server_base() -> str:
    try:
        internal = os.environ.get("BRIDGE_INTERNAL_URL", f"http://127.0.0.1:{F.http_port}").rstrip("/")
        public = requests.get(f"{internal}/health", timeout=3).json().get("public_url", "")
        if public:
            return public.rstrip("/")
    except Exception:
        pass
    runtime = BASE_DIR / "data" / "runtime.json"
    if runtime.exists():
        try:
            return json.loads(runtime.read_text()).get("public_url", "").rstrip("/")
        except json.JSONDecodeError:
            pass
    if F.public_url:
        return F.public_url
    sys.exit("Could not find the server. Start `python app.py`, or pass --url.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--to", default=F.to_number)
    p.add_argument("--from", dest="from_", default=F.from_number)
    p.add_argument("--url", default="")
    p.add_argument("--consent-ref", default="")
    p.add_argument("--force", action="store_true", help="ignore compliance blockers")
    args = p.parse_args()

    if not F.vobiz_auth_id or not F.vobiz_auth_token:
        sys.exit("Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN in .env")
    if not args.to:
        sys.exit("Set TO_NUMBER in .env or pass --to")

    problems = vobiz_api.compliance_check(F, args.to, args.from_, args.consent_ref)
    for pr in problems:
        print(f"compliance: {pr}")
    if problems and not args.force:
        sys.exit("refusing to dial; pass --force to override")

    base = (args.url or server_base()).rstrip("/")
    payload = vobiz_api.call_payload(F, args.to, args.from_, base)
    print(json.dumps(payload, indent=2))
    result = asyncio.run(vobiz_api.make_call(F, args.to, args.from_, base))
    print(f"\nHTTP {result.pop('_status')}")
    print(json.dumps(result, indent=2))
    if "request_uuid" not in result:
        sys.exit(1)
    print(f"\nAnswer the phone and talk. Events: curl -s localhost:{F.http_port}/calls | python -m json.tool")


if __name__ == "__main__":
    main()
