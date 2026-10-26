# Duplex models

The bridge works with any model that vLLM-Omni serves on the full-duplex
lane (`/v1/realtime?duplex=1`). It reads the `capabilities` object from
`session.created` and adapts: barge-in handling, playback acknowledgement and
tool calls are enabled only when advertised. These are the models served on
that lane today (vLLM-Omni `full_duplex_api.md`, October 2026).

## MiniCPM-o 4.5 (default)

```bash
vllm serve openbmb/MiniCPM-o-4_5 --omni --trust-remote-code --port 8091
```

Compose: `OMNI_MODEL=openbmb/MiniCPM-o-4_5`, no extra args. The default
deploy config is already `session_mode: duplex` with `max_sessions: 16`.

| Capability | Value | Effect on the bridge |
|---|---|---|
| `supports_barge_in` | yes | `speech_started` and `overlap.decision` cut replies |
| `supports_playback_ack` | yes | Vobiz `playedStream` becomes `playback.ack` |
| `supports_audio_truncate` | yes | truncated acks on barge-in adjust the stored reply |
| `chunk_period_ms` | 1000 | the model decides to listen or speak once a second |
| tool calls | no | `END_CALL_ON_GOODBYE` ends calls; no transfer by the model |

GPU: one 80 GB card for 16 sessions. Audio: 16 kHz in, 24 kHz out.

## Qwen3-Omni 30B-A3B

```bash
DEPLOY=$(python -c "import vllm_omni,os;print(os.path.dirname(vllm_omni.__file__))")/deploy/qwen3_omni_duplex.yaml
vllm serve Qwen/Qwen3-Omni-30B-A3B-Instruct --omni --deploy-config "$DEPLOY" --port 8091
```

Compose: `OMNI_MODEL=Qwen/Qwen3-Omni-30B-A3B-Instruct`,
`OMNI_EXTRA_ARGS=--deploy-config /usr/local/lib/python3.12/dist-packages/vllm_omni/deploy/qwen3_omni_duplex.yaml`
(check the path inside the image with
`docker compose run --rm vllm-omni python -c "import vllm_omni,os;print(os.path.dirname(vllm_omni.__file__))"`),
`OMNI_GPUS=2`.

Speech understanding in 19 languages (Urdu, not Hindi) and generation in 10.
Thinker, Talker and Code2Wav run as separate stages. GPU: 2 × 80 GB.

## Nemotron VoiceChat

```bash
DEPLOY=$(python -c "import vllm_omni,os;print(os.path.dirname(vllm_omni.__file__))")/deploy/nemotron_labs_voicechat_duplex.yaml
vllm serve <nemotron-voicechat-model> --omni --deploy-config "$DEPLOY" --port 8091
```

The only model on the lane with tool calls, so `end_call`,
`transfer_to_human` and `send_dtmf` work as functions. It advertises
`supports_barge_in: false`, so the bridge will not cut its replies on caller
speech; it expects 16 kHz float32 input and produces 22.05 kHz PCM16, which
means `OUTPUT_SAMPLE_RATE` cannot match it exactly. Treat Nemotron as
experimental with this bridge until a live run confirms the input format.

## PersonaPlex

Runs turn-based until vLLM-Omni ports it to the plugin contract (RFC
vllm-omni#7181). With `REQUIRE_DUPLEX=true` the bridge refuses a session to
it; set `REQUIRE_DUPLEX=false` to try the turn-based handler, which has no
barge-in and no playback acknowledgement.

## Checking what your server advertises

```bash
python - <<'EOF'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://localhost:8091/v1/realtime?duplex=1") as ws:
        print(json.dumps(json.loads(await ws.recv()).get("session", {}).get("capabilities"), indent=2))
asyncio.run(main())
EOF
```

The same object is logged by the bridge as `model:session.created` on every
call.
