## What this changes

<!-- One paragraph. Which flag, route, engine or doc, and why. -->

## How it was verified

- [ ] `python -m pytest -q` passes
- [ ] `python selftest.py --mock --mock-port 8791` completes with audio both ways
- [ ] `docker build .` succeeds
- [ ] Live call (optional): mode, persona and what the caller heard

## Checklist for new behaviour

- [ ] New flag has a default that keeps today's behaviour
- [ ] `.env.example` and the README flag table list it
- [ ] A test covers the XML or wire change
- [ ] No credentials, real phone numbers or call logs in the diff
