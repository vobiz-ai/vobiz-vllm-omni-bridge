"""
audio.py — format conversion between the Vobiz stream and an OpenAI-style realtime API
=======================================================================================

Two independent directions, each with its own format:

    Vobiz  -> app     whatever `<Stream contentType>` asked for, echoed back
                      in the WebSocket `start.mediaFormat` event:
                      L16/8000, L16/16000 or mulaw/8000
    app    -> Vobiz   `playAudio.media`: L16 at 8/16/24 kHz, or mulaw/8000

The duplex side (a vLLM-Omni server) takes PCM16 at
the rate you declare in `session.update` and returns PCM16 at the output rate
you declare. So the default configuration — `audio/x-l16;rate=16000` in,
L16/24000 out — needs no conversion at all in either direction. Everything
below exists for the other combinations (mulaw legs, byte-order flips).

Pure Python on purpose: `audioop` was removed in Python 3.13.
"""

from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# G.711 mu-law
# ---------------------------------------------------------------------------

_BIAS = 0x84
_CLIP = 32635

_EXP_LUT = bytes([0, 0, 1, 1] + [2] * 4 + [3] * 8 + [4] * 16 + [5] * 32 + [6] * 64 + [7] * 128)


def _encode_mulaw_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = _EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_mulaw_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    t = ((byte & 0x0F) << 3) + _BIAS
    t <<= (byte & 0x70) >> 4
    return (_BIAS - t) if (byte & 0x80) else (t - _BIAS)


_ENCODE_TABLE = bytes(_encode_mulaw_sample(s) for s in range(-32768, 32768))
_DECODE_TABLE = [_decode_mulaw_sample(b) for b in range(256)]


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Signed 16-bit little-endian PCM -> G.711 mu-law."""
    samples = struct.unpack_from(f"<{len(pcm) // 2}h", pcm)
    return bytes(_ENCODE_TABLE[s + 32768] for s in samples)


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """G.711 mu-law -> signed 16-bit little-endian PCM."""
    return struct.pack(f"<{len(mulaw)}h", *(_DECODE_TABLE[b] for b in mulaw))


# ---------------------------------------------------------------------------
# Byte order and resampling
# ---------------------------------------------------------------------------


def byteswap16(pcm: bytes) -> bytes:
    """Flip the byte order of 16-bit samples (big <-> little endian)."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    out = bytearray(pcm)
    out[0::2], out[1::2] = pcm[1::2], pcm[0::2]
    return bytes(out)


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear-interpolation resample of mono 16-bit little-endian PCM."""
    if from_rate == to_rate or not pcm:
        return pcm
    count = len(pcm) // 2
    if count == 0:
        return b""
    samples = struct.unpack_from(f"<{count}h", pcm)
    out_count = max(1, int(count * to_rate / from_rate))
    step = (count - 1) / out_count if out_count > 1 and count > 1 else 0.0
    out = []
    for i in range(out_count):
        pos = i * step
        left = int(pos)
        right = min(left + 1, count - 1)
        frac = pos - left
        out.append(int(samples[left] + (samples[right] - samples[left]) * frac))
    return struct.pack(f"<{out_count}h", *out)


# ---------------------------------------------------------------------------
# Vobiz <-> realtime API
# ---------------------------------------------------------------------------


def parse_content_type(content_type: str) -> tuple[str, int]:
    """
    `audio/x-l16;rate=16000` -> ("l16", 16000). Accepts the `encoding` value
    from `start.mediaFormat` too, which carries no `;rate=` suffix.
    """
    head, _, tail = content_type.partition(";")
    encoding = "mulaw" if "mulaw" in head.lower() else "l16"
    rate = 8000
    for part in tail.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "rate" and value.strip().isdigit():
            rate = int(value.strip())
    return encoding, rate


def to_model(payload: bytes, encoding: str, sample_rate: int, model_rate: int, l16_big_endian: bool = False) -> bytes:
    """Caller audio in the stream's own format -> PCM16 LE mono at model_rate."""
    if encoding == "mulaw":
        pcm = mulaw_to_pcm16(payload)
    else:
        pcm = byteswap16(payload) if l16_big_endian else payload
    return resample_pcm16(pcm, sample_rate, model_rate)


def from_model(pcm: bytes, model_rate: int, encoding: str, sample_rate: int, l16_big_endian: bool = False) -> bytes:
    """Model PCM16 LE at model_rate -> the format declared in `playAudio.media`."""
    pcm = resample_pcm16(pcm, model_rate, sample_rate)
    if encoding == "mulaw":
        return pcm16_to_mulaw(pcm)
    return byteswap16(pcm) if l16_big_endian else pcm


def frame_bytes(encoding: str, sample_rate: int, ms: int) -> int:
    """Raw bytes in one `playAudio` frame of `ms` milliseconds."""
    width = 1 if encoding == "mulaw" else 2
    return int(sample_rate * width * ms / 1000)


def wav_header(pcm_len: int, sample_rate: int) -> bytes:
    """RIFF header for raw mono PCM16 — used by the test scripts."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + pcm_len)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", pcm_len)
    )
