# lablab submission copy

Do not paste this until the BAND room export is in the repository. See README.

## Project title

Pocketful

## Short description

A clean-room wallet: pay, request, split, hold, and correct money without creating or spending it twice.

## Long description

Pocketful is a clean-room wallet for the pocketful track of the WeAreDevelopers Dark Factory hackathon. People keep one currency in exact minor units, and money moves only between wallets that already exist. They pay by handle, request a payment, split a bill, and place a hold that can be captured in part or voided. Later stages keep history honest: a statement can be read as of an instant, a correction changes the balance without rewriting the original receipt, a receiver can refund from available funds, and an operator can correct a whole settlement in one atomic batch.

Each stage is its own container. The service is Python's standard library, with scrypt password hashes and a small browser UI. The container needs no outbound network after it starts. A three-seat factory — coordinator, implementer, and reviewer — is described here. The BAND Desktop room that has to generate the submitted run is not in this tree yet.

Word count: 156.

## Technology and category tags

- Python 3.12 (stdlib HTTP server, no runtime packages)
- scrypt password hashes
- HTML, CSS, and vanilla JavaScript
- Docker (python:3.12-alpine)
- BAND Desktop (three seats: coordinator, implementer, reviewer) — not yet run for this tree
- Track: pocketful

## What the form still needs from Joshua

- Public GitHub URL after this repository is pushed
- Cover image
- Slide deck
- Video that shows the BAND Desktop room (the seats handing work to each other) and a walkthrough of the service. A video without that room recording is a disqualifier.
- Login on lablab as the enrolled solo team. Do not submit until `room.json` is the full-session download.
