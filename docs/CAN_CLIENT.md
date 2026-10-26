# Experimental CAN client contract

This is an offline design, not an implemented device transport. The executable model
is `tools/check_can_reply_matching.py`; its fixtures are synthetic vectors produced by
firmware p-code execution in [CAN_LOADER.md](CAN_LOADER.md). It never opens an adapter,
encodes erase/program commands or exports a firmware image.

## What the firmware evidence requires

Responses carry an 8-bit token but do not consistently identify the command or selected
address. The p-code test overwrites a small region of emulator memory with generated
bytes, then queries different ranges. Reassigning a token produces two responses with
identical service fields and different CRC payloads. A delayed earlier response would
pass the same header/token/length checks and reconstruct the wrong range.

The address-set response also retains the previous CRC marker and payload. Consequently,
matching only the CRC marker, or using independent token pools per command, is insufficient.
These are counterexamples on a peripheral model, not measurements of real bus reordering.
The vectors in [the execution report](data/can-loader-emulation.json) are generated test
data; they contain neither firmware contents nor a captured device conversation.

## Read-only state model

The model has one logical request outstanding. A retry keeps the same complete request
and token. A token is never assigned to another logical request during that lifetime.

| State | Accepted successful response | Next state | Injected timeout policy |
|---|---|---|---|
| new | No request yet | startup | Not applicable |
| startup | Matching token and startup signature | session | Stop; no automatic re-entry |
| session | Matching token and negotiation signature | address | Retry the identical request within budget |
| address | Matching token and zero status; trailing payload is unspecified | crc | Retry the identical assignment within budget |
| crc | Matching token, zero status and CRC marker | address or done | Retry the identical CRC request while the address is unchanged |
| done / failed | No request may advance | Terminal | No automatic restart |

Only standard response ID `0x101` with exactly 8 bytes is considered. Wrong ID, extended
frames, wrong length, header, token or response shape are counted and do not advance the
model. A nonzero device status fails the operation; a stale CRC payload accompanying an
error must never become recovered bytes. The extra zero response from the pending
startup frame is likewise counted and ignored.

No timeout duration is asserted. The retry budget is a model policy; it has not been
calibrated against an adapter or ECU. The session-negotiation retry rule applies only to
this read-only flow and must not be interpreted as clearing partial programming state.

## Finite token budget and unresolved long reads

The model consumes 2 tokens for startup/session and 2 per four-byte range for address/CRC.
With 256 token values, a lifetime is limited to 127 ranges, or 508 recovered bytes.
It stops instead of wrapping. Replaying an identical request may reuse its token; assigning
that token to another request is what is prohibited. Completing this model batch does not
establish device compatibility, a complete image or a verified recovery backup.

Creating another model instance does not clear a real adapter queue or pending ECU
transmission. Do not concatenate batches by assuming a new logical session creates a
fresh channel. Full-range reading still needs a backend freshness contract: ownership of
the channel, request/response ordering, outgoing and incoming queue semantics, and a way
to establish that older requests/replies cannot cross the chosen boundary. A FIFO/barrier
argument could be investigated under explicit backend assumptions; it is not established
by the current register model. Neither a guessed sleep nor a repeated handshake proves it.

## Conditional FIFO argument

`tools/check_can_fifo_barrier.py` explores an abstract channel with 3 epochs and 2 data
tokens. Request identities in its traces are oracle labels, invisible to the host; the
host compares only response kind and token. Each data request permits one retry. Events
include request execution, reply delivery, permanent request/reply loss and retries.
There is no clock, firmware execution, selected-address state or CAN register model.

Under strict FIFO request processing and FIFO reply delivery, the bounded exploration
finds no stale acceptance, including when data tokens cycle without a separate fence.
The reasoning is conditional: accepting a reply for the next request establishes that
all earlier requests have been processed or permanently discarded, and that replies
preceding that reply have been delivered or permanently discarded. Remaining replies
from retries of that request have its token. A different token on the following request
prevents those replies from advancing it. An old response may not later reappear from a
cache, replay queue, second consumer or delayed duplicate path outside this order.

The same exploration produces stale-acceptance counterexamples when either request
processing or reply delivery can reorder, even with an explicit fence. Assigning the
same token to adjacent requests also fails under FIFO. Adjacent copies of a fence reply
and fence retries remain safe in this bounded FIFO model; arbitrarily delayed replay is
outside that assumption. A fence alone does not repair an unordered channel.

This does not relax the finite model's token budget. A real backend must establish:

- Exclusive ownership, an initially clean channel and no unrelated response producer.
- Ordered execution at the ECU, including requests queued by host-side retries; a
  successful send call or transmit echo alone does not establish ECU processing order.
- Ordered reply visibility through ECU transmit objects, bus, adapter queues and the
  receiving API; a timeout must not hide a reply that can later reappear out of order.
- Explicit loss/overflow behavior and no replay or delayed duplication outside that order.
- Correct address-state behavior across queued retries, errors and cancellation; the
  abstract model checks reply identity only, not actual CRC execution or memory contents.

No currently implemented adapter contract establishes these conditions. A backend may
instead provide a separately justified freshness boundary. Until then, token cycling
remains disabled in the finite read model; no guessed delay, fence or reconnect enables it.
The exploration proves neither progress under losses nor compatibility with an ECU.

Run `python tools/check_can_fifo_barrier.py` to enumerate the bounded states and print
counterexample event sequences. This is a policy experiment, separate from the firmware
p-code evidence and the production K-line transport.

The firmware-object interleaving in [CAN_LOADER.md](CAN_LOADER.md) also produces a
successful address reply combining one request's token with another request's address.
The finite host model does not simulate this live-object race. Token uniqueness protects
against stale complete replies; it does not by itself validate atomic request consumption.
Queued retries must not overlap a different request without a justified drain/ordering
and receive-consistency contract. The abstract FIFO result assumes complete request
execution and does not discharge this separate obligation.

## Write ambiguity

The tested firmware appends duplicate fragments, preserves a partial page across session
end/re-negotiation, and keeps the saved active-page address despite a new address command.
The proposed client state after a missing/malformed write acknowledgement is therefore
uncertain. It must not automatically resend, advance to the next fragment or treat
reconnection as an empty buffer. An erase timeout likewise does not establish that no
sector changed or that the operation finished.

There is no write encoder in the executable read model. A future writer needs a separately
validated recovery procedure, actual flash completion/readback evidence and the existing
full-backup/identity requirements. The partial CRC window is not a substitute for that
backup. Nothing here authorizes a bench operation or changes the supported ECU list.

The ordered bench gates and required experiment records are defined in
[CAN_BENCH_PLAN.md](CAN_BENCH_PLAN.md). All gates remain unvalidated; the plan
does not enable a transport or authorize a hardware operation.

## Reproducible offline checks

```
python tools/check_can_reply_matching.py
```

The check validates the current p-code script fingerprint and replays the generated
vectors for I444GI04, I414DB02 and I465AI01. It injects lost/duplicate/delayed responses,
wrong IDs and frame shapes, device errors and retry exhaustion. It also demonstrates the
reused-token counterexample and exercises the entire finite budget with a separate
synthetic peer. That peer is a policy test, not another firmware-execution result.

For updated firmware-execution vectors, regenerate the report using
`tools/check_can_loader_emulation.py`. All inputs remain local; the JSON report includes
hashes and explicitly synthetic model vectors. Timing, physical CAN delivery and the
system clock source remain outside these checks.
