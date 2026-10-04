# Factory

Track: pocketful. Team: Joshua Jubelo, solo. This file is the standing design. The judged run has not happened. See the warning in `README.md`.

## Seats

Three seats, configured by the human in BAND Desktop before the room starts. Names below are the display names the mandate files must match after punctuation and case are ignored.

| Seat | Mandate | Owns | Does not own |
|---|---|---|---|
| coordinator | `mandates/coordinator.md` | Splits a stage into one handoff, adds the other two seats to the room, accepts or returns work | Writing the service, running the checks that decide acceptance |
| implementer | `mandates/implementer.md` | The stage folder: copy the previous folder forward, extend it, commit, report the revision | Declaring the work done |
| reviewer | `mandates/reviewer.md` | An independent check of the reported revision, including the harness suites up to that stage | Editing the code |

Fill `Harness:` and `Model:` with the runtime and model id BAND Desktop shows for that seat. The lines in the files today say the seats are not configured. Replace them. Do not leave a model name you did not actually attach.

Any runtime BAND Desktop can host is allowed, including one seat on the BAND SDK on your own machine. All three may share one harness and one model. Do not put endpoint paths, field names, error codes, or test ids in a mandate. Those belong in the task pasted into the room.

## How a stage runs

One room, one result repository, stages in order.

1. The human dispatches the stage once. The message contains the whole spec for that stage, the absolute path of this repository, and the instruction to copy `stage-(N-1)/` to `stage-N/` and extend it. For stage 1 there is no previous folder.
2. The coordinator pastes that full text to the implementer. A pointer to an earlier message is not a handoff.
3. The implementer commits and sends the revision, the commands, and the spec text to the reviewer and the coordinator.
4. The reviewer runs the checks. A failure goes back to the implementer with the log. The reviewer does not patch.
5. The coordinator reports the revision the reviewer accepted, or the blocker and the evidence, and stops. No "looks good, continue" from the human between stages if you want the run judged as autonomous. Dispatch the next stage as its own task, or dispatch all four at the start and then send nothing.

The human does not debug, approve, or rerun a stage in the submitted room. Development of the factory can happen in other rooms. Those rooms are not the submission.

## Design choices

- One service process per stage folder, standard-library HTTP, one lock around the ledger. That is the choice for a 2 vCPU, 2 GiB, 50-in-flight budget with a 5 second request timeout. A database would also fit the spec; it was not used, so the image has nothing to install and no network at run time.
- Stage behavior is a constant in that folder's ledger, not an environment switch. A later folder is the earlier one plus the new rules. Serving the next stage's routes from an earlier folder would make that folder claim nothing.
- Idempotency keys are stored with the request fingerprint. A replay of the same body returns the first response, including after a 4xx, only when the spec says the key was claimed. A different body with the same key conflicts.
- The reviewer is a different seat from the implementer so a green check is not the author marking their own homework.

## What this costs

No judged room was run from the machine that prepared this draft, so there is no measured seat time and no measured model spend to report. Do not invent a token count. After the real run, write three numbers in this section: wall-clock time per stage, the model id each seat used, and the provider usage the seats actually recorded. If a seat fails or retries inside the band, count that in the same note. Human steering is not a cost line; it is a failed autonomy run.

The workspace that drafted the service did not call a paid model API and did not buy credits.

## Catching bad work

The reviewer rejects a stage when any of these is true:

- The folder does not build from its `RUN.md` with no manual setup.
- A shipped check for this stage, or for an earlier stage, fails.
- The folder also passes every shipped check of the next stage. That means the requirements were not added in order.
- The reported revision is not the tree that was checked, or the history was amended after the handoff.

The implementer gets the command output, not a hint about which branch to take. If the same failure returns unchanged, the coordinator records it as the stage outcome instead of asking the human.

A bad result worth showing in the video is one the reviewer actually sent back and that changed a later commit. Do not stage a fake argument.

## What was tried and is not the submission

Building the service by hand, which is what `stage-1/` through `stage-4/` currently are. That was done so the HTTP contract could be exercised before a room existed. It does not satisfy the hand-built rule. The submitted history has to be the seats' commits, not a squash of this draft on top of them.
