# CAN loader: bench validation plan

Status: planned, not performed. No bench is assembled, no adapter has been opened,
and no CAN transport is enabled in the application. This document defines evidence
needed to test the hypotheses in [CAN_LOADER.md](CAN_LOADER.md), with the client
constraints in [CAN_CLIENT.md](CAN_CLIENT.md). It is not an executable flashing recipe.

## Evidence and scope

Keep a separate result for each board, silicon marking, firmware hash and adapter
configuration. A shared boot fingerprint does not validate another application's reset
path or another board's wiring. The stock I444GI04 used in the offline checks is not
the historical bench reference. Record the actual unit before selecting a hypothesis.

Each experiment record needs:

- Board and MCU identity, firmware identity and how that identity was established;
  a file label alone is insufficient. Mark unknown fields explicitly.
- Wiring and power/reset setup, CAN adapter hardware/firmware, host driver/API versions,
  bitrate, filters, receive mode and termination as actually configured or measured.
- Exact request bytes and standard/extended ID, direction and timestamps with their
  source/resolution; distinguish host enqueue, transmit echo, bus observation and reply.
- Queue ownership, retry/cancellation settings, overflow/error counters, power/reset
  events, expected observation, actual result and a pass/fail/inconclusive verdict.
- References to private raw captures and hashes of artifacts. Keep full images,
  identifiers and unredacted captures outside the public repository. Publish only
  reviewed metadata and redacted evidence with a description of what was removed.

A trace gap or a timeout is an observation, not proof that the ECU ignored a request.
Reset trials need a separate capture per initial state; do not combine several changed
variables into a claimed explanation. Preserve failures as evidence too.

## Ordered experiments

| Gate | Experiment | Required evidence before progressing |
|---|---|---|
| Identity and recovery | Inventory the exact unit and establish the existing recovery route | Verified complete backup and donor identity before destructive work; do not substitute the partial CAN window |
| Physical routing | Check connector/transceiver/MCU connectivity without assuming the firmware pin map describes the board | Board evidence for CAN routing; distinguish measured continuity from inferred routing |
| Passive startup | Observe traffic and reset/power events with no injected protocol requests | Capture configuration and actual bus activity, or an explicitly inconclusive silent trace |
| Resident-loader entry | Test the bounded startup hypothesis under a controlled, documented reset condition | Reproducible loader-shaped reply on the expected channel, separated from application traffic |
| Bounded read | Validate selected-address and short-CRC behavior against independently known bytes | Exact recovered bytes agree; raw request/reply evidence and negative controls |
| Backend freshness | Characterize queues, loss, cancellation and delayed replies | A justified ownership/ordering/freshness contract, including request-state consistency |
| Write preparation | Review backup, recovery, page state, excluded areas and verification method | A concrete experiment for one sacrificial unit, with explicit authorization for erase/program |
| Write and recovery | Test completion and interruption behavior in separate authorized experiments | Independent content verification and demonstrated recovery for each claimed failure mode |

Failure or missing evidence at a gate leaves later claims unvalidated. Passive observation
can precede a complete backup; destructive work cannot. Every hardware operation requires
the user to be present and authorize it under the repository's hardware rules.

## Entry: what to distinguish

The examined loader uses standard request/response IDs `0x100`/`0x101` and a bounded
startup exchange. The application software route uses receive IDs `0x7DF`/`0x7E0`;
the modeled diagnostic TX buffer route uses `0x7E8`, while full admission conditions
and wire delivery are unresolved. A reply or bus ACK on
an application ID does not demonstrate resident-loader entry.

The MCU routing hypothesis is RX P0.4, pin 63, and TX P2.2 ALT1, pin 41. It is not
connector pinout evidence. The conditional clock result gives 500 kbit/s for an assumed
8 MHz oscillator and the required system-clock selection; neither assumption is a
measured startup state here. Establish clock/bit timing with evidence rather than
interpreting silence at that bitrate as absence of the loader.

Record power-on, external reset and application reset as distinct starting conditions.
Do not equate a reset pulse with a power cycle or assume which reset status it produces.
The static application-reset path normally returns to the application; the argument
`0x90` has unresolved preparation effects and possible serial-memory transactions.
It is not a routine entry probe. PORST, ROM BSL selection and the resident flash loader
are separate hypotheses; a prior unsuccessful attempt does not settle their causes.

For a startup trial, use a separately reviewed request schedule derived from the
examined startup exchange. Record every attempted request and the reset reference.
Do not select a fixed startup deadline from emulator instruction counts. A successful
trial needs a complete response matching the signature and token, then reproducibility
under the same condition; CAN transmit acknowledgement alone is insufficient. A failed
trial must leave bitrate, routing, entry state and timing as separate possible causes.

## Bounded read: first useful functional proof

Use independently known contents from the exact unit, established through a validated
route. A downloaded stock image is not ground truth for a different donor. First query
a small unchanged range, then a distinct range with a different expected CRC. Check
address selection as well as CRC mathematics; repeat at the original address to expose
stale address state. Avoid deriving the expected bytes through the same CAN oracle.

Keep the finite client model's one pending logical request and lifetime budget:
2 startup/session tokens, 2 tokens per four-byte range, at most 127 ranges or 508 bytes.
Do not reset the budget by constructing another client instance, reconnecting or sleeping.
The admitted interval is `[0xC02000, 0xC90000)`; that is 581632 bytes and excludes
parts of the full image. This first test cannot establish a full recovery backup.

Exercise lost/late/duplicate replies and cancellation in a synthetic adapter test before
injecting such faults on a real read-only flow. A queued identical retry must not overlap
a different request without evidence that the ECU consumed a complete frame consistently.
The modeled mixed-token/address race is a separate concern from stale complete replies.
If an answer could belong to another address, record it as ambiguous and stop the read.

## Backend freshness: what a capture can and cannot prove

Document send completion semantics, transmit and receive queue boundaries, flush/drain
behavior, bus-off and overflow handling, and whether cancellation leaves queued work.
Exclusive host ownership alone does not establish empty ECU transmit state. Measure
or otherwise establish the backend contract at each boundary; a small set of clean
traces cannot prove that reordering is impossible.

The abstract FIFO experiment assumes ordered request execution and ordered reply
visibility, with no delayed replay outside that order. Validate selected-address state
and firmware receive-object consistency too. Until these obligations are met, retain
the finite budget and make no full-range reading claim. A fence reply or reconnect alone
does not establish freshness.

## Write completion and recovery: separate destructive phase

Before scheduling this phase, identify a sacrificial unit, a verified complete recovery
image, a demonstrated independent recovery route, a permitted test region and a way to
verify contents independently of the write acknowledgement. Preserve excluded boot/tail
regions and donor data. If any prerequisite is missing, stop at read-only validation.

Prepare separate experiments for normal completion, missing acknowledgement, device
error and interrupted operation. Define expected observations and the recovery action
before each trial. Do not begin with a deliberate interruption. A successful normal write
does not establish recovery from a damaged application or interrupted erase.

The offline model shows duplicate fragments append, session restart preserves a partial
page, and address reassignment does not replace the active-page snapshot. After uncertain
completion, do not resend, advance or assume an empty buffer. The real flash helpers
can return after a transient error without repeating the operation; persistent error
or busy state has no helper-local bounded exit. Host timeout/cancellation does not cancel
ECU flash activity. Verify contents, not merely a cleared status or ordinary reply.

Recovery claims must name the failure condition actually tested and show restoration
of the intended contents and subsequent normal startup. Do not infer recovery from
an intact boot region, availability of K-line equipment or an emulator return alone.

## Current outcome

All bench gates remain unvalidated. Current evidence supports a resident-loader
hypothesis and offline protocol constraints, not a supported CAN reader/writer.
[ECU_SUPPORT.md](ECU_SUPPORT.md) remains the coverage record. A successful result
should update that record and the current findings with its exact board/image scope;
an inconclusive experiment should narrow the next question without changing support.

Application storage mode 2 is not a read-only guarantee: the offline
[redundant-descriptor check](CAN_LOADER.md) executes automatic serial write calls
from `0xC1055E(2,0x55D4)` when only one copy is valid. Do not use that helper as a
non-writing entry probe; physical repair behavior is unverified.
