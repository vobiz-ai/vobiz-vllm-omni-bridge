"""
selftest.py — start the bridge, hold a mock call through it, stop it
=====================================================================

    python selftest.py --mock              # local mock realtime server, no key
    python selftest.py                     # the vLLM-Omni server in .env
    python selftest.py --barge-in          # talk over the reply, expect clearAudio

Exit code is the mock caller's: 0 when audio came back both ways.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent


def wait_http(url: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="run against mock_realtime.py")
    p.add_argument("--port", type=int, default=8010)
    p.add_argument("--mock-port", type=int, default=8765)
    p.add_argument("--barge-in", action="store_true")
    p.add_argument("--seconds", type=float, default=16.0)
    p.add_argument("--end-call-after", type=int, default=0)
    p.add_argument("--persona", default="", help="generic | india-clinic (overrides PERSONA)")
    args = p.parse_args()

    env = dict(os.environ)
    env.update({"HTTP_PORT": str(args.port), "PUBLIC_URL": f"http://127.0.0.1:{args.port}"})
    if args.persona:
        env["PERSONA"] = args.persona
    procs: list[subprocess.Popen] = []
    logs = BASE_DIR / "data"
    logs.mkdir(exist_ok=True)

    try:
        if args.mock:
            env.update({"REALTIME_URL": f"ws://127.0.0.1:{args.mock_port}/v1/realtime?duplex=1", "OMNI_MODEL": "mock"})
            mock_cmd = [sys.executable, "mock_realtime.py", "--port", str(args.mock_port)]
            if args.end_call_after:
                mock_cmd += ["--end-call-after", str(args.end_call_after)]
            procs.append(
                subprocess.Popen(
                    mock_cmd, cwd=BASE_DIR, env=env, stdout=open(logs / f"mock_realtime-{args.port}.log", "w"), stderr=subprocess.STDOUT
                )
            )

        procs.append(
            subprocess.Popen(
                [sys.executable, "app.py"], cwd=BASE_DIR, env=env, stdout=open(logs / f"app-{args.port}.log", "w"), stderr=subprocess.STDOUT
            )
        )
        if not wait_http(f"http://127.0.0.1:{args.port}/health", 20):
            print("bridge did not come up; see data/app.log")
            return 2
        print(requests.get(f"http://127.0.0.1:{args.port}/health").json())

        cmd = [sys.executable, "mock_vobiz.py", "--ws", f"ws://127.0.0.1:{args.port}/ws", "--seconds", str(args.seconds)]
        if args.barge_in:
            cmd.append("--barge-in")
        result = subprocess.run(cmd, cwd=BASE_DIR, env=env)
        time.sleep(1.0)
        return result.returncode
    finally:
        for proc in procs:
            proc.send_signal(signal.SIGINT)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
