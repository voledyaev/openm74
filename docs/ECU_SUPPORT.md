# ECU support: evidence and research coverage

The implemented transport remains K-line ROM BSL. The hardware reference is one
M74 CAN unit; its identity, board and measured behavior are described in
[HARDWARE.md](HARDWARE.md). No CAN reader or writer is enabled by the research tools.

A shared CPU family, ROM handshake, file size or boot-region hash does not establish
ECU compatibility. Keep separate the board wiring, silicon identity and memory map,
firmware's resident protocol, image layout and a measured read/write result.

## Available evidence

| ECU group | Images inspected in this audit | What is established here |
|---|---|---|
| M74 / M74 CAN | 120 | Hash/size inventory verified; 3 boot-region groups; detailed disassembly of representatives. This does not classify each physical board as CAN or non-CAN |
| Mikas 12 / 12.3 / 12.48 | 0 | Firmware and exact hardware variants were not inspected; no transfer of M74 protocol findings |
| Marelli IAW 7GV | 0 | Firmware and board not inspected; candidate only |
| Marelli IAW MIU4 | 0 | Firmware and board not inspected; candidate only |
| M74K | 2 | Control samples from the separate ST10-family inventory; no reviewed M74 boot-region match; outside support |
| M73 | 45 | Control samples from the separate ST10-family inventory; no reviewed M74 boot-region match; outside support |
| M74M, M74.8 and later, Yanvar | 0 | No firmware inspection in this audit; no change to the existing support boundary |

These are files, not independent physical devices. Some files share code or duplicate
an earlier application's region. Do not turn the image count into a claim of hardware
coverage. Images described as factory in an external inventory remain samples rather
than proof of manufacturer provenance; the local identity check establishes their bytes.
Public candidate-family names do not establish that every variant uses the stated CPU.
The Mikas and Marelli rows in the README must not be read as supported read/write targets.

The local corpus did not contain those candidate families. Public-source discovery did
not yield a usable anonymous firmware download during this audit; no commercial file
was purchased and no account access was attempted. Their sample coverage remains empty.

For reproducible hash-only evidence, see [CAN_LOADER.md](CAN_LOADER.md) and its linked
CSV reports. The survey deliberately reports unknown images as unknown, including all
control samples. It is not a CPU detector or a compatibility gate for writing.

## What the offline CAN result means

Byte comparison and disassembly support a resident CAN loader in the examined M74
images. The command paths admit a bounded range; they do not imply unrestricted
full-flash programming. The short-CRC reconstruction hypothesis passes local numerical
checks and bounded firmware p-code execution with modeled peripheral registers.
It has not been exercised against an ECU or a complete peripheral emulator.

This is a possible additional transport, with distinct requirements. The existing K-line
writer reads and verifies a full backup before erase. A resident CAN service with partial
coverage cannot silently substitute for that read. Before implementation, define image
coverage, recovery from a lost session, confirmation of flash completion, preservation of
excluded areas and a source of a verified recovery image. Do not weaken the existing
backup/identity gates to make an experimental transport fit.

## Work that can continue without a bench

- Obtain an individually identified full image for each candidate hardware variant,
  record length and SHA256, and establish its address mapping before disassembly.
- Map its reset path and actual peripheral setup; do not copy M74 addresses or pin maps.
- Trace every dispatch branch, range check, state transition and authorization condition.
  Respect access checks; their presence changes the host protocol requirements.
- Specify positive and negative frame cases and model them offline. A byte/CRC model
  validates only that model; it does not establish peripheral timing or device behavior.
- Keep MCU pin selection separate from a board schematic or continuity measurement.
  A firmware image alone cannot prove which connector reaches a transceiver.

A future bench session must confirm timing, actual traffic, power/reset behavior and
recovery on the exact board before a support claim changes. There is no bench assembled
for this investigation, so the current work remains static and offline.
