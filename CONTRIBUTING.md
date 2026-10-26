# Contributing

Thanks for looking at this. The bridge is small on purpose: one file per
concern, every behaviour behind a flag in `config.py`, and a test for each
flag that changes XML or wire behaviour.

## Before you open a pull request

```bash
python -m pytest -q                                   # unit tests, no network
python selftest.py --mock --mock-port 8791            # a mock call through the real app
docker build -t vobiz-vllm-omni-bridge .                   # the image still builds
```

CI runs the same three steps on Python 3.11 and 3.12.

## What a good change looks like

- A new Vobiz feature arrives as a flag with a default that keeps today's
  behaviour, a line in `.env.example`, a row in the README flag table, and a
  test in `tests/`.
- A new duplex model should need no code: the bridge branches on the
  `capabilities` object. If one does, add the branch in `realtime.py` keyed
  on a capability flag, never on the model name.
- A new persona is an entry in `personas.py`. Keep prompts short; they are
  read aloud by a model on a phone line.
- Wire-format changes (`vobiz_stream.py`, `audio.py`) need a `mock_vobiz.py`
  run that shows audio both ways.

## Reporting a live-call problem

Attach the per-call JSON from `data/call-<uuid>.json` and the matching lines
from `data/events.jsonl`, with your Vobiz auth token removed.
The `/calls` endpoint (with `ADMIN_TOKEN`) shows the same data for a running
bridge.

## Security issues

See `SECURITY.md`. Do not open a public issue for a credential leak.
