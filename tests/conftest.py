import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# .env may set demo values; tests pin the defaults explicitly (load_dotenv never overrides)
os.environ["GREETING_SOURCE"] = "speak"
os.environ["PERSONA"] = "generic"
os.environ["AMD_MODE"] = "off"
os.environ.setdefault("REALTIME_URL", "ws://127.0.0.1:1/v1/realtime?duplex=1")
os.environ.setdefault("OMNI_MODEL", "mock")
os.environ.setdefault("PUBLIC_URL", "https://bridge.example.test")
os.environ.setdefault("STREAM_CONTENT_TYPE", "audio/x-l16;rate=16000")
os.environ.setdefault("OUTPUT_CONTENT_TYPE", "audio/x-l16")
os.environ.setdefault("OUTPUT_SAMPLE_RATE", "24000")
os.environ.setdefault("FROM_NUMBER", "+919876543210")
os.environ.setdefault("VOBIZ_AUTH_ID", "MA_TEST")
os.environ.setdefault("VOBIZ_AUTH_TOKEN", "token-test")
