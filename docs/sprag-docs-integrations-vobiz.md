<!-- Draft for sprag.ai/docs/integrations/vobiz — proposed source: content/docs/integrations/vobiz/index.md -->

# Vobiz

[Vobiz](https://www.vobiz.ai) puts Sprag models on a phone number. It is
India-first telephony infrastructure: numbers, SIP trunks and a bidirectional
media stream that carries a live call to your application as a WebSocket.
The Vobiz Direct Bridge connects that stream to Sprag's realtime WebSocket, so
a phone call becomes a Sprag session with nothing in between.

```text
phone <-- Vobiz --> Vobiz Direct Bridge <-- WebSocket --> api.sprag.ai
```

## Before you start

You need a Vobiz account with one number, your Vobiz Auth ID and Auth Token
from the Console, and a Sprag API key:

```bash
export SPRAG_API_KEY=<your_sprag_api_key>
export VOBIZ_AUTH_ID=<your_vobiz_auth_id>
export VOBIZ_AUTH_TOKEN=<your_vobiz_auth_token>
```

Generate a Sprag key from [API keys](/app/api-keys) in the Sprag app. See the
[quickstart](/docs/quickstart) if you do not have an account yet.

Inbound calls need a public HTTPS hostname. Vobiz posts to an answer URL on
your server and opens a secure WebSocket for media; a tunnel such as
`cloudflared` is enough while developing.

## Run the Vobiz Direct Bridge

```bash
git clone https://github.com/vobiz-ai/vobiz-vllm-omni-bridge.git
cd vobiz-vllm-omni-bridge
cp .env.example .env     # VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN, FROM_NUMBER, SPRAG_API_KEY
docker compose up -d --build
docker compose run --rm call --to +91XXXXXXXXXX
```

For inbound calls, create a Vobiz Voice Application whose `answer_url` is
`https://<your host>/answer` and `hangup_url` is `https://<your host>/hangup`,
and attach a number.

## Two engines

One setting chooses how the call reaches Sprag:

| `MODE` | Sprag sessions the bridge opens | Measured on a live call |
| --- | --- | --- |
| `s2s` | `wss://api.sprag.ai/v1/realtime?model=symphony` | first reply audio 197 to 286 ms after the caller stops |
| `pipeline` | Rhythm over `/v1/audio/transcriptions`, chat completions, Chorus over `/v1/audio/speech` | 1.5 to 1.8 s, with a caller transcript and the `priya` voice |

Use `s2s` for the lowest latency and natural turn-taking. Use `pipeline` when
you need a transcript, text-level guardrails, a different LLM, or an Indian
voice.

## What the Vobiz Direct Bridge handles

Every Vobiz call feature is a flag, read once at startup and shown on `/health`:

- Audio formats on both legs: L16 at 8, 16 or 24 kHz, mu-law at 8 kHz. The
  defaults (16 kHz in, 24 kHz out) need no conversion against Sprag.
- Barge-in: Sprag's `speech_started` becomes a Vobiz `clearAudio`; the next
  reply waits for the carrier's acknowledgement.
- Greeting from Vobiz text-to-speech before the stream opens, or from Chorus.
- Session recording with a spoken disclosure.
- Answering machine detection, with a voicemail message for outbound campaigns.
- Tools the agent can call: `end_call`, `transfer_to_human`, `send_dtmf`, and
  `record_outcome` for a structured result per call.
- India calling rules checked before an outbound dial: Indian caller ID,
  the 10:00 to 20:00 IST window for promotional traffic, consent reference.
- Signature verification of every Vobiz callback, admin token, concurrency
  cap, watchdogs, Prometheus metrics, per-call event logs.

## Audio formats

Vobiz sends L16 at 8 or 16 kHz or mu-law at 8 kHz and accepts L16 at 8, 16 or
24 kHz or mu-law at 8 kHz on the way back. Sprag realtime sessions take PCM16
and can emit 8, 16, 22.05, 24, 44.1 or 48 kHz. The defaults that need no
conversion are 16 kHz in and 24 kHz out; set `audio.output.format.rate` to
8000 for a mu-law leg.

## Languages for India

Rhythm transcribes Hindi and English. Chorus speaks English in Indian accents
through the `priya`, `arjuna` and `fatima` presets; Hindi speech output is not
yet available on Chorus or Symphony, so an Indian agent today understands
Hindi and replies in English.

If you are not using the bridge at all, the [realtime API](/docs/realtime)
is what it talks to.
