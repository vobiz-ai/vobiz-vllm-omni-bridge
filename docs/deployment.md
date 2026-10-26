# Deployment guide

Two processes: the vLLM-Omni model server on a GPU, and the bridge, which is
a stateless Python service Vobiz reaches over HTTPS and WSS. They can share a
host (the compose `gpu` profile) or not.

## 1. A GPU host

| Model | Minimum | Comfortable |
|---|---|---|
| MiniCPM-o 4.5 | 1 × 80 GB (A100 80 GB, H100) for the default 16 sessions | the same |
| Qwen3-Omni 30B-A3B | 2 × 80 GB | 2 × H100 |
| Nemotron VoiceChat | 1 × 80 GB | the same |

L4 (24 GB) cards do not fit a duplex model in one card; an 8 × L4 layout
exists for MiniCPM-o but is slow and expensive. Use an 80 GB card.

Google Cloud example, Mumbai region for Indian callers:

```bash
gcloud compute instances create omni-bridge \
  --zone asia-south1-b --machine-type a2-ultragpu-1g \
  --accelerator type=nvidia-a100-80gb,count=1 \
  --image-family common-cu124-ubuntu-2204 --image-project deeplearning-platform-release \
  --boot-disk-size 300GB --maintenance-policy TERMINATE
```

On the VM install Docker and the NVIDIA Container Toolkit, then:

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi   # must list the card
```

Open no inbound ports. Vobiz reaches the bridge through a tunnel or a
reverse proxy (section 3); the model server listens only on the Docker
network.

## 2. Run it

```bash
cp .env.example .env            # Vobiz credentials, numbers, HF_TOKEN
docker compose --profile gpu up -d --build
docker compose logs -f vllm-omni
```

The first start downloads the weights (about 20 GB for MiniCPM-o 4.5) and
captures CUDA graphs; allow 10 to 15 minutes. The compose health check gives
it 15 minutes before marking the service unhealthy. When
`curl localhost:8091/health` returns 200 the bridge's `/health` shows
`"omni": "ok"`.

Model cache: by default a named volume `hf-cache`. Set `HF_HOME=/data/hf` in
`.env` to keep weights on a host path that survives `docker compose down -v`.

Model choice and flags go through `.env`:

```
OMNI_MODEL=openbmb/MiniCPM-o-4_5
OMNI_GPUS=all
OMNI_EXTRA_ARGS=
```

See [models.md](models.md) for the Qwen3-Omni and Nemotron commands.

## 3. How Vobiz reaches the bridge

| Option | When | What to set |
|---|---|---|
| **Quick tunnel** (default) | Development, outbound calls | Leave `PUBLIC_URL` empty. The bridge container starts `cloudflared tunnel --url` and prints a `*.trycloudflare.com` hostname. New hostname on every restart. |
| **Named Cloudflare tunnel** | Production, no inbound ports | Create a tunnel in Cloudflare Zero Trust, route `bridge.example.com` to `http://bridge:8000`, set `PUBLIC_URL=https://bridge.example.com` and `TUNNEL_TOKEN`, run with `--profile tunnel`. |
| **Reverse proxy** | You already terminate TLS | Proxy `/` and `/ws` (WebSocket upgrade, long read timeout) to the bridge, set `PUBLIC_URL`. Build with `WITH_CLOUDFLARED=false`. |

The hostname must be stable for inbound calls because it is written into the
Vobiz Application. Outbound calls carry the URL per call.

nginx needs the upgrade headers on the stream path:

```
location / {
    proxy_pass http://bridge:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

## 4. Point Vobiz at it

**Inbound.** Voice Application with `answer_url = https://<host>/answer`
(POST) and `hangup_url = https://<host>/hangup`, number attached.

**Outbound.** `docker compose run --rm call --to +91XXXXXXXXXX` runs the
compliance check and places the call with AMD, ring, fallback and hangup
callbacks pointing at the bridge.

## 5. Production flags

```
VERIFY_SIGNATURES=true      # needs VOBIZ_AUTH_TOKEN; drops unsigned callbacks
WS_PATH_SECRET=<random>     # stream URL becomes wss://host/ws/<secret>
ADMIN_TOKEN=<random>        # /calls and /flags
MAX_CONCURRENT_CALLS=16     # match duplex_session.max_sessions on the server
MAX_CALL_SECONDS=900
CALLER_SILENCE_HANGUP_S=60  # no tool calling on MiniCPM-o: end abandoned calls
JSON_LOGS=true
RECORD_CALLS=true           # if you need recordings; the disclosure is spoken first
```

Signature verification only works when the callback URL configured in Vobiz is
exactly the one the bridge computes from `PUBLIC_URL`.

## 6. Capacity and scaling

- The model server caps sessions with `duplex_session.max_sessions` (16 in
  the MiniCPM-o deploy config). Beyond it the server closes new sessions with
  code 1013; keep the bridge's `MAX_CONCURRENT_CALLS` at or below it so the
  caller hears a busy message instead of silence.
- Bridge instances share nothing. Run several behind a load balancer with
  sticky WebSocket routing; all can point at one model server, or at a pool
  of servers behind their own balancer (the duplex docs describe replica
  groups and `max_sessions_per_replica`).
- CPU use on the bridge is small: resampling and base64, about 60 MB plus
  2 MB per call.
- Async AMD keeps state in memory; the `/amd` callback and the follow-up
  `/answer?amd=1` must land on the same instance.

## 7. Updating the model server

```bash
docker compose pull vllm-omni
docker compose --profile gpu up -d vllm-omni
```

Pin `OMNI_IMAGE_TAG` to a release (`v0.30.0`) or a commit-pinned nightly
(`nightly-<commit>`) for reproducible deployments. The bridge logs the
`capabilities` object from every `session.created`, so a model or image
change that alters barge-in or playback-ack support shows up in
`events.jsonl` on the first call.

## 8. Health and metrics

- `GET /health` on the bridge: `omni` is a cached probe of the model server's
  `/health`; `active_calls`; every non-secret flag.
- `GET /metrics`: Prometheus text.
- Model server: `GET /health` and `GET /v1/models` on port 8091 inside the
  Docker network.
- `data/events.jsonl` in the `bridge-data` volume for per-call timing
  (`model:first_audio` carries the time from end of caller speech to first
  reply audio).
