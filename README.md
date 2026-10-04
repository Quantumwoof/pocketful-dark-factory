# Pocketful

Solo entry for Joshua Jubelo (GitHub [Quantumwoof](https://github.com/Quantumwoof)) on the **pocketful** track of the WeAreDevelopers x BAND Dark Factory hackathon.

Pocketful is a wallet. People hold one currency in minor units, pay each other by handle, request money, split a bill, and (from stage 2) place holds. Stage 3 adds statements and corrections that do not rewrite receipts. Stage 4 adds refunds and operator batch corrections. Deposits and withdrawals are out of scope: money only moves between wallets that already exist, and balances sum to the last reset.

## Read this before submitting

The code under `stage-1/` through `stage-4/` was written in a workspace on 4 October 2026 because BAND Desktop was not available there. The event rule is that hand-built code does not count. This tree is a working service and a factory description. It is **not** yet a ranked entry.

Still required, and not invented here:

1. Configure three distinct seats in BAND Desktop whose display names match `mandates/coordinator.md`, `mandates/implementer.md`, and `mandates/reviewer.md`. Put the real harness name and model id on the first two lines of each mandate.
2. Run one fresh room. The only human input per stage is the task you dispatch. The seats copy each stage folder forward and extend it. Leave their commit history intact.
3. Download the **full session** and save it, unchanged, as `room.json` at this repository root.
4. Record a video of that room plus a walkthrough. Upload the video, slides, and a cover image on lablab. Do not submit a video that does not show the room.

`room.json` is absent on purpose. A fabricated room log would fail the entry.

## Layout

```text
README.md
FACTORY.md
SUBMISSION.md
mandates/          coordinator, implementer, reviewer
stage-1/           API only
stage-2/           browser UI, holds, captures, voids
stage-3/           statements, as-of reads, corrections
stage-4/           refunds and correction batches
```

Each stage folder has its own `Dockerfile` and `RUN.md`. Stage N serves stage N and not the next stage.

## Run locally

Python 3.12 or newer. No pip install. From `stage-4/` (the UI is in stage 2 and later):

```sh
cd stage-4
PORT=8080 python3 app.py
```

Open http://127.0.0.1:8080/login after the demo seed below. Stage 1 has no pages; use curl against `stage-1/`.

### Docker

Docker was not available in the workspace that prepared this tree, so the image was not built here. A judge runs:

```sh
cd stage-1
docker build -t pocketful-stage-1 .
docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage-1
```

Same command in `stage-2`, `stage-3`, or `stage-4`. The image is `python:3.12-alpine` with the sources copied in. It does not install packages and does not need a network at run time.

## Demo script

In another terminal, with stage 4 listening on port 8080:

```sh
curl -s http://127.0.0.1:8080/health

curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8080/_test/reset \
  -H 'Content-Type: application/json' \
  -d '{"currency":"EUR","minor_units":2,"users":[
        {"id":"u_ada","email":"ada@example.com","password":"correct horse","display_name":"Ada","handle":"ada","balance":10000},
        {"id":"u_bob","email":"bob@example.com","password":"correct horse","display_name":"Bob","handle":"bob","balance":2500}
      ],"payments":[],"requests":[]}'

TOKEN=$(curl -s -X POST http://127.0.0.1:8080/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

curl -s http://127.0.0.1:8080/me -H "Authorization: Bearer $TOKEN"

curl -s -X POST http://127.0.0.1:8080/payments \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-pay-1' \
  -d '{"to_handle":"bob","amount":1500,"note":"lunch","visibility":"public"}'

curl -s http://127.0.0.1:8080/me -H "Authorization: Bearer $TOKEN"
```

Ada starts at 100.00 EUR and should show 85.00 EUR after the payment. Repeating the payment with the same idempotency key returns the original receipt and does not move the money again. The browser at http://127.0.0.1:8080/login accepts the same email and password.

The reset endpoint is part of the published harness contract. It is not a hidden back door and it is not authenticated, matching the spec.

## Checks run here

Official shipped samples only, on the host, not in Docker. These samples are not the full graded suite.

| Folder | Suites run | Result |
|---|---|---|
| stage-1 | stage 1 (147) | pass. A stage 2 hold route returns 404 |
| stage-2 | stage 1 (147), stage 2 (35) | pass. `GET /statement` returns 404 |
| stage-3 | stage 1 (147), stage 2 (35), stage 3 (6) | pass. A stage 4 refund route returns 404 |
| stage-4 | stage 1 (147), stage 2 (35), stage 3 (6), stage 4 (5) | pass |

`python -m harness check --track pocketful` reports one problem: `room.json` is missing. Mandates do not trip the track-vocabulary scan. Isolated `harness run` was not executed because Docker is not installed on this machine.
