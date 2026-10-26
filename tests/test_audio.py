import struct

import audio


def test_mulaw_roundtrip_is_close():
    pcm = struct.pack("<8h", 0, 1000, -1000, 8000, -8000, 30000, -30000, 123)
    back = audio.mulaw_to_pcm16(audio.pcm16_to_mulaw(pcm))
    for a, b in zip(struct.unpack("<8h", pcm), struct.unpack("<8h", back), strict=False):
        assert abs(a - b) <= max(64, abs(a) // 8)


def test_byteswap_is_involution():
    pcm = struct.pack("<4h", 1, -2, 300, -4000)
    assert audio.byteswap16(audio.byteswap16(pcm)) == pcm
    assert audio.byteswap16(b"\x01\x02") == b"\x02\x01"


def test_resample_lengths():
    pcm = b"\x00\x01" * 1600  # 100 ms at 16 kHz
    assert len(audio.resample_pcm16(pcm, 16000, 24000)) == 2400 * 2
    assert len(audio.resample_pcm16(pcm, 16000, 8000)) == 800 * 2
    assert audio.resample_pcm16(pcm, 16000, 16000) is pcm


def test_parse_content_type():
    assert audio.parse_content_type("audio/x-l16;rate=16000") == ("l16", 16000)
    assert audio.parse_content_type("audio/x-mulaw;rate=8000") == ("mulaw", 8000)
    assert audio.parse_content_type("audio/x-mulaw") == ("mulaw", 8000)


def test_to_model_from_mulaw_leg_resamples_to_model_rate():
    mulaw = bytes([0xFF]) * 160  # 20 ms of mulaw silence at 8 kHz
    pcm = audio.to_model(mulaw, "mulaw", 8000, 16000)
    assert len(pcm) == 320 * 2  # 20 ms at 16 kHz


def test_from_model_to_mulaw_leg():
    pcm24 = b"\x00\x00" * 480  # 20 ms at 24 kHz
    out = audio.from_model(pcm24, 24000, "mulaw", 8000)
    assert len(out) == 160  # 20 ms of mulaw at 8 kHz


def test_frame_bytes():
    assert audio.frame_bytes("mulaw", 8000, 20) == 160
    assert audio.frame_bytes("l16", 8000, 20) == 320
    assert audio.frame_bytes("l16", 16000, 20) == 640
    assert audio.frame_bytes("l16", 24000, 20) == 960
