<!-- Draft for sprag.ai/docs/integrations/vobiz — proposed source: content/docs/integrations/vobiz/index.md -->

# Vobiz

[Vobiz](https://www.vobiz.ai) puts Sprag models on a phone number. It is
India-first telephony infrastructure: numbers, SIP trunks and a bidirectional
media stream that carries a live call to your application. A call reaches
Sprag one of three ways, and all three use the same Sprag API key and base
URL.

| Route | How the call reaches Sprag | Pick it for |
| --- | --- | --- |
| [Pipecat](#pipecat) | Vobiz media stream → `pipecat-vobiz` serializer → `pipecat-sprag` services | Swappable stages, Pipecat's own turn-taking and tools |
| [LiveKit](#livekit) | Vobiz SIP trunk → LiveKit room → agent on Sprag through the OpenAI plugin | WebRTC and phone callers in one room |
| [Direct bridge](#direct-bridge) | Vobiz media stream → `vobiz-omni-bridge` → Sprag realtime or STT, LLM and TTS sessions | Lowest latency, every Vobiz call feature as a flag |

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

## Pipecat

[Vobiz-X-Pipecat](https://github.com/vobiz-ai/Vobiz-X-Pipecat) is Vobiz's
Pipecat example. Its `VobizFrameSerializer` (from the `pipecat-vobiz`
package) turns the Vobiz media stream into Pipecat frames and back, and
handles barge-in and hangup. Put the three `pipecat-sprag` services in the
transcribe, generate and speak positions:

```bash
uv add pipecat-sprag pipecat-vobiz
```

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat_sprag.llm import SpragLLMService
from pipecat_sprag.stt import SpragSTTService
from pipecat_sprag.tts import SpragTTSService
from pipecat_vobiz import VobizFrameSerializer

transport = FastAPIWebsocketTransport(
    websocket=websocket,
    params=FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        add_wav_header=False,
        serializer=VobizFrameSerializer(stream_id=stream_id, call_id=call_id),
    ),
)

stt = SpragSTTService()
llm = SpragLLMService(
    settings=SpragLLMService.Settings(
        system_instruction="You are on a telephone call. Keep every reply to one or two short sentences.",
    ),
)
tts = SpragTTSService(settings=SpragTTSService.Settings(voice="priya"))

pipeline = Pipeline([
    transport.input(),
    stt,
    user_aggregator,
    llm,
    tts,
    transport.output(),
    assistant_aggregator,
])
```

To collapse the three stages into one Symphony session, use
`OpenAIRealtimeLLMService` with `base_url="wss://api.sprag.ai/v1/realtime"`
as described on the [Pipecat LLM page](/docs/integrations/pipecat/llm).

The serializer expects the answer document that Vobiz-X-Pipecat returns:

```xml
<Response>
    <Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000">wss://your-host/ws</Stream>
</Response>
```

## LiveKit

Vobiz delivers the call to LiveKit over SIP, and the agent uses Sprag through
LiveKit's OpenAI plugin as on the [LiveKit page](/docs/integrations/livekit).
Nothing Sprag-specific changes; only the trunk does.

1. In LiveKit Cloud, create an inbound trunk and a dispatch rule. Allow the
   [Vobiz SIP signalling addresses](https://www.vobiz.ai/docs/concepts/ip-whitelisting#sip-signaling).
2. In the Vobiz Console, create an inbound trunk whose primary URI is your
   LiveKit SIP host (`<id>.sip.livekit.cloud`, hostname only, no `sip:`), and
   link your number to it.
3. For outbound calls, create a Vobiz outbound trunk with a credential and use
   its `<id>.sip.vobiz.ai` domain, username and password on a LiveKit outbound
   trunk.

```python
from livekit.agents import AgentSession
from livekit.plugins import openai

session = AgentSession(
    llm=openai.realtime.RealtimeModel(
        base_url="wss://api.sprag.ai/v1/realtime",
        api_key=os.environ["SPRAG_API_KEY"],
        model="symphony",
    ),
)
```

The full trunk setup is on the
[Vobiz LiveKit integration page](https://www.vobiz.ai/docs/integrations/livekit).

## Direct bridge

[vobiz-omni-bridge](https://github.com/vobiz-ai/vobiz-omni-bridge) connects
the Vobiz media stream to Sprag with no framework in between. One setting
chooses the engine:

| `MODE` | Sprag sessions it opens | Measured on a live call |
| --- | --- | --- |
| `s2s` | `wss://api.sprag.ai/v1/realtime?model=symphony` | first reply audio 197 to 286 ms after the caller stops |
| `pipeline` | Rhythm over `/v1/audio/transcriptions`, chat completions, Chorus over `/v1/audio/speech` | 1.5 to 1.8 s, with a caller transcript and the `priya` voice |

```bash
git clone https://github.com/vobiz-ai/vobiz-omni-bridge.git
cd vobiz-omni-bridge
cp .env.example .env     # VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN, FROM_NUMBER, SPRAG_API_KEY
docker compose up -d --build
docker compose run --rm call --to +91XXXXXXXXXX
```

Every Vobiz call feature is a flag: audio formats (L16 at 8, 16 or 24 kHz,
mu-law at 8 kHz), answering machine detection, session recording, transfer to
a human, DTMF in both directions, HMAC verification of Vobiz callbacks, and
India calling-window and consent checks before an outbound dial.

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

## Choosing between them

Use [Pipecat](#pipecat) when you already build on Pipecat or want to swap a
stage. Use [LiveKit](#livekit) when browser and phone callers share a room.
Use the [direct bridge](#direct-bridge) when latency and Vobiz call control
matter most, or as the reference for the Vobiz stream protocol.

If you are not building on a framework at all, the [realtime API](/docs/realtime)
is what the direct bridge talks to.
