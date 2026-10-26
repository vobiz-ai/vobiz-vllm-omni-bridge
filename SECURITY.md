# Security

## Reporting

Email security@vobiz.ai with the details. Do not open a public issue for a
vulnerability or a leaked credential. You will get an acknowledgement within
two working days.

## What the bridge does to protect a deployment

- `VERIFY_SIGNATURES=true` checks the `X-Vobiz-Signature-V3` (or V2) HMAC on
  every Vobiz callback using your auth token. Turn it on in production.
- `WS_PATH_SECRET` moves the media WebSocket to `wss://host/ws/<secret>` so a
  guessed hostname cannot open a stream.
- `ADMIN_TOKEN` protects `/calls` and `/flags`. `/health` and `/metrics` are
  public by design and never show secrets.
- `MAX_CONCURRENT_CALLS` caps simultaneous sessions; `MAX_CALL_SECONDS`,
  `CALLER_SILENCE_HANGUP_S` and `TIME_LIMIT_S` bound call length and cost.
- `call.py` refuses promotional calls outside 10:00 to 20:00 IST and calls
  without a consent reference when `CONSENT_REQUIRED=true`.
- The container runs as an unprivileged user and holds no secrets in the
  image; everything comes from the environment at runtime.

## What you must do

- Keep `.env` out of version control (it is in `.gitignore`).
- Keep the model server off the public internet; it has no authentication of
  its own. The compose file exposes it only on the Docker network.
- Put the bridge behind TLS. Vobiz requires `https://` answer URLs and
  `wss://` stream URLs; a tunnel or reverse proxy provides them.
- Rotate the Vobiz auth token if a log or per-call JSON file
  leaves your control. Those files never contain the credentials, but they do
  contain caller numbers and transcripts.
