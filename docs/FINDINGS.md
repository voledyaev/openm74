# Findings

Research notes: which routes into this ECU exist, which are dead, what the flash map
actually looks like, and — the part that took longest to get right — how to prove a
flasher works without fooling yourself.

For the wire-level behaviour of the K-line itself see [QUIRKS.md](QUIRKS.md).

---

## 1. Routes in, and why only one of them is a route

### K-line, mask-ROM serial loader — the one that works

Accepts arbitrary code into RAM and jumps to it. That is the whole reason it wins:
arbitrary execution means it can be made to do anything, including stream flash out. It
lives in mask ROM, so it does not depend on flash contents, which is what makes erasing
recoverable.

### The resident CAN loader — abandoned, and not for the obvious reason

It exists, it is reachable, and it is proprietary. It also has a command table with
erase, program and a CRC check — **and no read opcode at all**.

That is decisive: even a complete win over the CAN path gives you a tool that can write
but cannot take a backup. For a device where the current contents may be the only copy in
existence, a write-only tool is worse than none. The effort went to K-line instead.

*(There is a way to read through it anyway — CRC a region against a known image, block by
block, and reconstruct by difference. Elegant, absurdly slow, and unnecessary once the
serial loader works. Recorded in case someone needs it on a unit without the K-line
option.)*

### The ROM's CAN loader — never answers, and the manual says why

**Measured:** it responds to nothing over CAN, under every condition tried. Frames sent
continuously, a scanner rebuilt to stop retrying when no ACK comes back, several different
reset timings, a full disassembly of the firmware to reach the loader's own call path — the
ECU acknowledges nothing at any point.

**The reason is that the CAN loader was never running.** The mask ROM does not offer all its
loaders at once: it runs exactly one, chosen by the level on **P10.[3:0] latched at
power-on** (XC2000 System Units UM V1.0, Table 10-1 and Table 10-10):

| Start-up mode | P10.[3:0] | Host talks on |
|---|---|---|
| Internal start from flash | `x x 1 1` | — |
| **Standard UART loader** | **`x 1 1 0`** | RxD = **P7.4**, TxD = **P7.3** |
| **CAN loader** | **`x 1 0 1`** | RxDC0 = **P2.6**, TxDC0 = **P2.5** |
| SSC loader | `1 0 0 1` | P2.3–P2.6 |

The K-line route in this project works because applying +12 V to X1:A4 and X1:B2 **before**
power puts that pattern at `x110` — the UART loader. It then listens on P7.4 and nowhere
else. Sending CAN frames to a part in that state cannot work, and no amount of persistence,
retry-suppression or reset timing changes it, because none of those touch Port 10. The
earlier conclusion from disassembly — that the BSL arms a wait on K-line — was right, and
this is the mechanism behind it.

**What the connector can and cannot do, from a published schematic.** An earlier version of
this section reasoned that two connector pins drive two of `P10.[3:0]`, and that the bit CAN
needs is simply not brought out. The premise was wrong: the diagnostic-cable schematics
published by the tool vendor show `Prg.En` as a **single net** forking to both `X1:A4` and
`X1:B2`. One externally controlled signal, not two. Nor are the resistors those cables call
for anything to do with boot mode — `1.2 k` is an adapter-side pull-up on `Prg.En`, and
`470 R` sits between `X1:K1` and the CAN-H net, biasing the bus.

The conclusion survives the correction and is now derivable rather than assumed. `P10.1` is
`1` in normal boot (`xx11`) and `1` in the state this ECU actually reaches (`x110`,
measured). CAN needs it `0`. **Nothing the connector controls moves that bit**, and there is
only one thing the connector controls.

A second-hand but consistent report from the same vendor's forum belongs beside it: even for
the M74 CAN, initialisation is said to be done over K-line, with CAN carrying only the bulk
transfer afterwards. If that is right, the commercial route depends on the same transceiver
this ECU does not have fitted from the factory — which would make the modification in
[HARDWARE.md](HARDWARE.md) not a way around missing knowledge but the same prerequisite
everybody else has.

**The pin route to the CAN loader does not work on a board like this, and Infineon says so.**
Errata sheet XC2700 V1.7, `BSL_CAN_X.001`: a CAN bootstrap loader entered from the pins
allows the external oscillator **0.5 ms** to settle, "for typical quartz crystal this
settling time is too short", and it goes into Startup Error State. The prescribed workaround
is to start from flash, switch the clock to the external source, and *then* trigger a
software reset with CAN bootstrap loader mode selected.

That inverts the obvious framing. The pins are the unreliable route; the software one is the
remedy — and it is the one this project already has full access to.

**The software route, documented in Infineon AP16164 and in the manual**, with the trap the
manual does not spell out:

1. Settle the external oscillator first. That is the whole point of the workaround, and an
   Application Reset leaves the oscillators alone (UM Table 6-7).
2. **`RSTCON0.SW` has to be programmed before any of it works.** Its reset value is `00B`,
   "no reset is generated", so `SWRSTREQ` out of reset does nothing at all. Use `11B`
   (Application Reset only) or `10B`. **Never `01B`** — that is a System Reset, which
   Table 6-7 marks PSRAM "affected, un-reliable" for, and which resets the very oscillators
   the workaround exists to preserve.
3. `SWRSTCON.SWCFG` = the wanted mode, `SWBOOT` = 1, then `SWRSTREQ` = 1.

What survives that reset is what makes it usable: PSRAM, the oscillators, and `SWRSTCON`
itself, whose reset class is power-on. So a stage can leave data in PSRAM for whatever comes
next — and `SWBOOT` stays armed until power is actually removed, so every later application
reset re-enters the chosen mode.

The registers take the writes without ceremony: the register security mechanism engages only
after `EINIT`, and in a BSL-loaded stage `EINIT` has not run.

**A free experiment that separates two failure modes.** Before betting on an encoding, write
`SWCFG = 0x46` — the value this ECU *reports* for the mode it is already in — and reset. If
it comes back in the UART loader, `SWRSTCON` works on this silicon and the encoding is
simply "whatever `STSTAT.HWCFG` reads". If it does not, the mechanism is the problem and no
value would have helped. A power cycle clears it either way.

**A candidate for the undocumented bit 6** in the measured `HWCFG = 0x46`: later manuals in
this family list two UART loader variants, one on P7.3/P7.4 and one on P2.3/P2.4. This ECU
uses the P7 pins. It fits exactly, and it is not confirmed.

**Superseded, and worth keeping visible.** This section previously said the CAN loader was
"blocked in silicon" by an erratum: with an 8 MHz crystal the bit-timing measurement does not
finish inside the reset window. That erratum is real and would also produce silence, which is
exactly why it was believable. But it explains an observation that has a simpler cause — the
loader was not selected — and it was written here as settled fact on the strength of fitting
the symptom. Fitting the symptom is not the same as being the cause.

**None of which changes the decision**, because the route was abandoned for the reason above
it: the loader reachable from running firmware over CAN cannot read.

### Diagnostic services on the running application — absent

Probed on a live unit: the flash-download services return "not supported", memory read
gets no answer, and the write/routine services are locked. Not a permissions problem —
the handlers are not implemented. This ECU is not field-programmable through its
diagnostic interface.

---

## 2. The flash map holds a surprise

832 KB at `0xC00000`, 4 KB sectors, 128-byte pages.

**The hardware layout**, from the XC2000 System Units User's Manual V1.0 Table 3-1, which
is also where this tool's busy poll and flash command addresses come from:

| Array | Range | Size |
|---|---|---|
| Flash 0 | `0xC00000–0xC3FFFF` | 256 KB, of which **252 KB usable** — see below |
| Flash 1 | `0xC40000–0xC7FFFF` | 256 KB |
| Flash 2 | `0xC80000–0xCBFFFF` | 256 KB |
| a fourth array | `0xCC0000–0xCCFFFF` | 64 KB — beyond that manual, present on this part |

An earlier version of this file said "four independent 256 KB modules", which was a guess
dressed as a fact: 4 × 256 is 1 MB and this part has 832 KB. The fourth array does not
appear in the base XC2000 manual at all (it lists `0xCC0000` upward as "reserved for
flash"), which fits the incidental finding further down — `0xCE0000` reads back as a
mirror of `0xCC0000`, exactly what a 64 KB array whose upper address bits are not fully
decoded would do. Module boundaries are not academic: they are what the `XX` in every
flash command sequence selects, and a write self-test succeeded in segment `0xC7`, whose
module base is `0xC4`.

**How the firmware uses that space** — a different question with a different answer, and
one measured here rather than read from a manual: blank-sector density across a dump.
Note that these boundaries are *software* ones and do not line up with the arrays above:

| Range | Size | Blank sectors | What lives there |
|---|---|---|---|
| `0xC00000–0xC7FFFF` | 512 KB | 16 % | code |
| `0xC80000–0xCBFFFF` | 256 KB | 81 % | calibrations |
| `0xCC0000–0xCCFFFF` | 64 KB | 12 % | the area the ECU rewrites itself (EEPROM emulation) |

The densities are the evidence: code is packed, a calibration area is mostly empty, and the
self-written area is full. Every difference we have found between images falls on those
lines — a VIN at `0xC70000` in the code region, calibration bytes in the second, adaptation
data in the third.

Note the sizes also decompose as 512+256+64, which is the bank layout of an **ST10F276** —
a different part, and a coincidence that has already sent one analysis to the wrong
datasheet. The die reads `Infineon SAK-XC2765X-104F80L` (photographed in
[HARDWARE.md](HARDWARE.md)), the register map and flash command set are Infineon's, and
the real array boundaries are the ones in the table above, which the 512+256+64 reading
does not have.

**One sector is not backed by memory.** On the unit here, `0xC0F000` reads `9b 1e`
repeating, ignores erase, and no image changes it. Its neighbours erase and program
normally, checked in the same session. Erase reports success — the module's busy flag clears
normally — and the contents do not move.

**How to tell what it is, without guessing.** A read-only probe over one monitor session
settles it, and the comparison rows are the point — a single address tells you nothing:

| address | reads |
|---|---|
| `0xC0F000` | `9b 1e` repeating |
| `0xCD0000`, `0xCF0000` — past the end of the array | **`9b 1e` repeating** |
| `0x300000` — a segment with no flash controller at all | `46 46` repeating |
| `0xCE0000` | a byte-for-byte **mirror of `0xCC0000`** |

So `9b 1e` is what the **flash controller** answers for an address inside its own range with
nothing behind it — demonstrated by reading past the end of the populated array, which gives
exactly the same thing. It is *not* a generic bus default: a truly unmapped segment answers
`0x46`. `0xC0F000` therefore behaves precisely like unpopulated flash.

**And it is documented — it is a property of the part, not of this unit.** XC2000 System
Units User's Manual V1.0 (2007-06), **Table 3-1, note 3**, quotes the address exactly:

> The 4 KB sector from C0'F000H to C0'FFFFH is not accessible to the software.

And §3.9.2, on the array structure: *"Each array has in the XC2000 64 sectors — in the
Flash0 one sector is reserved for device internal purposes. It is not accessible by
software."* Figure 3-6 places it: **physical sector 15 of flash module 0**, which is
`0xC00000 + 15 × 4096`. Two independent confirmations inside the same manual: Table 3-1
lists Flash 0 as **252 KB** rather than 256, and Figure 3-6 annotates logical sector 6 as
"12 KB/16 KB" — a sector short in Flash 0 and only there.

So it is neither unpopulated silicon nor a locked sector: it is the sector the device keeps
for itself, on **every** part in this family. Firmware images from elsewhere that hold 4096
zeros there are consistent with this — their reader zero-filled a sector it could not read,
which is what a reader that does not know about this would do.

**Do not diagnose an ECU for something that is in its manual.** "Is this unit damaged?" is a
question the unit cannot answer, and it is expensive to ask. The answer here sat in the
memory map table of the same document this project already cites for its busy poll — one
section away from the one that was being read.

**The tool can now ask, too.** The monitor used to decide an operation had finished from the
module's busy flag alone, which says "no longer working" and nothing about why. The stage
now copies `IMB_FSR_OP` and `IMB_FSR_PROT` into PSRAM at the end of every erase and program,
*before* it issues Reset to Read — which is documented to clear exactly the bits worth
having — and the host reads them back with an ordinary READ. That turns three
indistinguishable outcomes into three different sentences:

| what the controller says | what it means |
|---|---|
| PROER set | the operation hit **installed write protection**: the sector is locked |
| SQER set | the command sequence was **not accepted** — a protocol fault, not a flash fault |
| nothing set, operation completed | it was **accepted and had nothing to do**: no memory there |

A write also reads `PROIN` and the `PROCON` registers once, before the first erase, so an
ECU with protection installed is announced up front instead of being discovered forty
minutes in. `tools/probe_protection.py` asks the same questions read-only.

**What this unit reports**, read over two bench sessions:

| register | value | meaning |
|---|---|---|
| `IMB_FSR_PROT` | `0x0000` | **PROIN clear — no protection installed at all.** Nothing on this ECU is locked, and nothing *can* be refused for that reason |
| `IMB_PROCON0/1/2` | `0x0000` | their reset value. Only loaded from the security pages when protection is installed, so while PROIN is clear these bits mean nothing |
| `IMB_FSR_BUSY` | `0x0000` | idle |
| `IMB_MAR` | `0x0000` | normal read margin |
| `IMB_FSR_OP` | `0x0004` before any operation, `0x0002` after an erase | see below |

That last row is the evidence that these are the real registers and not a coincidence of
zeros. `0x0004` is POWER, which the manual says is set when a flash module goes through its
startup phase and is cleared **only by a Clear Status command** — exactly what you would
expect after a reset that nothing has cleared. After an erase it reads `0x0002`: the ERASE
bit set, and POWER *gone*, because the monitor issues Clear Status before every flash
operation. The value changed in precisely the documented way.

**Do not use `IMB_IMBCTRL` as that anchor**, which an early version of the probe told the
reader to do. Its documented reset value is `0x558C`; this unit reads `0xA54C`. It is the
one register in the block that software configures — wait states and prefetch — so it sits
at whatever the ROM left it, and checking it would make a correct reading look like a
broken one.

**Incidental finding:** `0xCE0000` mirrors `0xCC0000`, so the fourth module does not decode
its upper address bits fully. Harmless — nothing addresses above `0xCCFFFF` — but worth
knowing before trusting a read outside the array.

**Design consequence, unchanged by any of it.** A flasher meeting a region like this must not
die halfway through an image — but it also must not skip it silently, because a genuinely
failing erase looks identical from the outside. The rule that separates them: *does what is
already there happen to be what the image wants?* If yes, harmless. If no, the image did not
fully land and the tool says so instead of printing success.

The known address is a **label on that measurement, never a replacement for it**. The tool
names `0xC0F000` when it meets it, and still proves the region is unwritable exactly as it
would for any other address. A flasher that skips a sector because a constant in its source
says to is one datasheet erratum — or one wrong part number — away from skipping a real
failure on somebody's ECU.

**The boot sector** (`0xC00000–0xC02000`) is left alone by default, because it holds the
resident loader. That protection is **software, in this tool** — the host refuses the range
unless `--allow-boot` and asserts it again immediately before every erase. No hardware
write-protect on this region has been demonstrated, and the compiled-in segment gate cannot
help: it is 64 KB-granular, so a whole-image write unlocks segment `0xC0` entire (see
[PROTOCOL.md](PROTOCOL.md) §4). An earlier version of this file said the sector was
"protected against erase anyway", which would be the sentence someone leaned on when
deciding `--allow-boot` was safe.

---

## 2b. Other ECUs, and what "M74" turns out to mean

Researched from public sources, not tested — nothing below has been near this bench, and
none of it is a promise. It is here because the question "could this tool work on my ECU"
deserves a better answer than a shrug, and because the answer decides how the tool is built.

**"M74" is not one hardware design.** The badge covers at least six different
microcontrollers, and a loader branching on the name rather than on the silicon would be
wrong most of the time:

| ECU | Microcontroller | Same stages could apply? |
|---|---|---|
| M74 (front-drive, K-line) | **SAK-XC2765X** | **the same part as this project's** |
| M74 CAN | **SAK-XC2765X** | this is the reference unit |
| M74K (Classic) | ST10F273 | no — different core, different map |
| M74M | XC2361A, or SAK-XC2060M on 2022 builds | no |
| M74.8 and later | SPC58 (Power Architecture) | no |
| M74.9 / .91 | Artery AT32F435 (ARM) | no |

The evidence is mixed in strength — firmware packs and tool compatibility lists for most of
it, one actual board photograph for M74M. Treat the table as a map of where to look, not as
a datasheet.

**The same part appears outside the family.** `SAK-XC2765X` is documented for **Mikas 12 /
12.3 / 12.48** (GAZelle, Sobol, UMZ), and the wider XC27x5 series for **Marelli IAW 7GV**
(XC2785X, also 832 KB) and **Marelli IAW MIU4** on Vespa, Moto Guzzi and Aprilia. So the
silicon this project targets is not rare, and neither is the bootloader in it.

**Yanvar is not a candidate**, which is worth stating because it is the obvious guess:
Yanvar 5.1 and 7.2 are built on the 8-bit `SAF-C509`, and Yanvar 7.2+ and M73 on `ST10F273`.
Nothing there resembles this memory map.

Two more things worth knowing before anyone tries: `XC2755X`, which appears in several
online references, **does not exist** — the series is XC2765X and XC2785X, and the other is
a typo that has propagated. And an M74 carries a second Infineon part, an `XC866L` 8-bit
safety coprocessor, alongside the main MCU.

**What this project does about it.** It asks the silicon (§2, "What the part says it is")
and refuses to *write* to anything whose manufacturer, device ID and flash size do not
match, `--force-unknown-ecu` notwithstanding. It never restricts *reading*, and a read from
an unrecognised unit is the one thing that could turn any row of that table from a guess
into something supportable. If you have one of these and a K-line transceiver on it, a dump
and the identity registers would be genuinely useful — `tools/probe_identity.py` prints the
latter and cannot write anything.

## 3. How the tool was proven — and the trap in the middle of it

### Reading

Two independent mechanisms had to agree:

* a streaming dumper reading all 832 KB in one pass, and
* the command monitor reading the same flash a completely different way.

Both produced the same image. The one-pass read was also repeated at two different line
speeds in different sessions and gave an **identical SHA256**, so the read is
deterministic, not merely plausible.

### Writing — and the test that looks like proof and is not

**Do not prove a writer by writing the ECU's own image back.** It passes, and it proves
almost nothing. Flash only turns bits on, so when old and new content are identical:

* a working erase-and-program produces the right bytes, and
* an erase that silently did nothing *also* produces the right bytes, because programming
  the same data over itself changes nothing.

The verify cannot tell those apart, and a run that "verified 100 %" on that basis is far
weaker evidence than it looks.

**What proves it:** write a *different* image. Doing that here failed on the 14th sector —
an erase reporting success while changing nothing — which same-image runs could never have
surfaced. The failure was real, reproducible, and turned out to be the memory hole above.

The same logic applies at the smallest scale: a write self-test on a sector that is
already blank proves nothing either, because a no-op erase is indistinguishable from a
real one there. Self-tests write a distinctive pattern, verify it landed, erase again and
restore the original — net change zero, but every step observable.

### End to end

A whole different image written, verified sector by sector, then the original written back
and the ECU read out in full — matching the original dump's SHA256 byte for byte, all 851968
of them.

**Reliable mode at 19200, the first end-to-end run** — kept because it is the run the
independent re-read below was taken against:

| | |
|---|---|
| Sectors erased, programmed and verified byte-for-byte | 205 of 206 |
| Recovery events across the whole run | **0** |
| Probe frames needing a retry, at 19200 | **0.0 %**, replies within 13–15 ms |
| Pre-flight backup | taken, and checked against the ECU's own checksum |
| Independent read afterwards | streaming reader, matched the file everywhere memory exists |
| Bytes differing from the image | 4096, all in `0xC0F000`; **zero** anywhere else |
| Bytes changed in the ECU | 23648 across 25 sectors, including the 17-character VIN |

The independent read is the part that matters. The streaming reader shares no code with the
monitor that did the writing, so it is not confirming its own work — the same reason two
mechanisms were used to prove reading. And the image written was a *different* one: writing
the same image back would have proven almost nothing, for the reason above.

Three separate reads of this ECU on two days produced an identical SHA256, one of them the
backup the tool took by itself.

**Reliable mode, start to finish** — pre-flight backup, every sector read back and compared
byte for byte, then the whole image read again at the end:

| | |
|---|---|
| Sectors erased, programmed and verified byte-for-byte | 205 of 206 |
| Line rate | 115200 throughout |
| Recovery events across the whole run | **0** |
| Final read-back | 843776 bytes match the image exactly |
| Whole run, backup and final verify included | ≈ 13 minutes |
| Region with no memory | `0xC0F000`, named and skipped |

**Fast mode, start to finish** — the mode that verifies each sector by asking the ECU to
add it up rather than reading it back:

| | |
|---|---|
| Sectors erased, programmed and verified | 205 of 206 |
| Line rate | 115200 throughout |
| Recovery events across the run | **0** |
| Whole run, backup included | ≈ 8 minutes |
| Region with no memory | `0xC0F000`, named and skipped, as designed |

**Checked by something that shares no code with it.** A later session's pre-flight backup —
a full 851968-byte read through the monitor, taken before anything was erased — matched the
reference image **851968/851968**, and the two SHA-256 digests agreed. (The digest itself is
not printed here: it fingerprints one specific vehicle's firmware, which is no use to anyone
who does not already have the file and is not this project's to publish.) A mode whose
verification is a checksum computed *by the ECU* was therefore confirmed byte-exact by a
separate read on a separate day.

That check is worth insisting on: a checksum the writer asks the writee to compute is
exactly the kind of verification that can agree with itself.

---

## 4. Error model of the link

**On a healthy host, this link has no measurable error rate.** A whole 832 KB image reads
and writes at 115200 with **zero** retries or resync events, in both modes, and a 32 KB
sustained read compares byte-perfect against a reference.

That is the finding. Everything this project once believed about per-byte error
probabilities, direction asymmetry and data dependence was measuring the host defect in
[QUIRKS.md](QUIRKS.md) §1 — a serial port being reprogrammed in the middle of transfers.
The numbers were reproducible and had coherent explanations, which is exactly why they
survived so long.

**What the tool still does about errors, and why it is not wasted.** A flasher runs on
benches it has never seen, and the cost of a lost link is measured in bricked ECUs rather
than in retries. So the transport keeps every defence it had:

* checksums on every frame and every payload, 16-bit, so a corrupted command is refused
  rather than executed;
* recovery paths that drain, resync and resend instead of ending a run ([QUIRKS.md](QUIRKS.md) §2);
* a speed ladder that measures rather than assumes, and steps **down** — never up — when
  the write itself reports that the rate is not carrying it.

On a bench in good order none of that fires. That is the design: insurance, not a workaround
for something known to be broken.

**If you are characterising your own bench**, the numbers worth taking are in
[QUIRKS.md](QUIRKS.md) §3 and §4 — pattern sensitivity and per-rate throughput — and the
tool will take them for you with `--linktest`, which measures both directions separately
and says which one is bad.

### What the part says it is

Read off the reference unit through the running loader, from registers the device maintains
about itself rather than anything inferred from behaviour:

| Register | Value | Meaning |
|---|---|---|
| `IDMANUF` `0x00F07E` | `0x1820` | JEDEC manufacturer `0xC1` — Infineon; section `0x00`, standard microcontroller |
| `IDCHIP` `0x00F07C` | `0x3801` | CHIPID `0x38`, revision step `0x01` |
| `IDMEM` `0x00F07A` | `0x30D0` | type 3 = flash; 208 blocks × 4 KB = **832 KB**, exactly this tool's `FLASH_SIZE` |
| `STSTAT` `0x00F1E0` | `0x8046` | `HWCFG` `0x46`, low four bits `0110` — **the UART bootloader** |

The ESFR area is memory-mapped at `0x00F000–0x00F1FF`, so the monitor's ordinary READ
reaches all of it; `tools/probe_identity.py` prints the lot.

Every one of those values is printed verbatim in the XC2765X data sheet V2.12, Table 7, so
this is not a fingerprint we invented — it is the part matching its own documentation, step
`01` included. The same data sheet independently confirms two things this project learned the
hard way: Table 4 gives the four flash modules as 256/256/256/**64 KB**, and Table 3 note 1
says *"The uppermost 4-Kbyte sector of the first Flash segment is reserved for internal use
(C0'F000H to C0'FFFFH)"* — the sector §2 above spent three bench sessions on.

**`0xD5` is not an identity check, and Infineon says so.** Both user's manuals print the same
note: `D5H` means *"all devices equipped with identification registers"*, and *"does not
directly identify a specific derivative"*. `55H`, `A5H`, `B5H` and `C5H` are the older
generations. **ST10 parts answer `D5H` too** — and take 32 bytes, and jump — but to
`00'FA40` in IRAM, not `E0'0000` in PSRAM. That matters here rather than academically:
Yanvar 7.2+, M73, M74K, Mikas 10.3 and Mikas 11 are ST10-based, which is to say the ECUs
most likely to be on the same bench as this one are exactly the ones a successful handshake
would mislead you about.

**CHIPID identifies a die, not a part.** `0x38` covers XC2765X, XC2785X, XC228xM, XC236xA,
XE164xM and XE167xM — all register- and map-identical, so the stages are portable across all
of them by construction. What it does *not* cover is the rest of the family, and one member
is genuinely dangerous: **XC2768X / XC228xI exists in an 832 KB configuration whose fourth
module sits at `0xD00000`, with `0xCC0000` empty.** Same total size, same handshake, wrong
addresses.

**So the gate asks two different questions.** What the part says about itself — manufacturer,
CHIPID and flash size, all of which must match — and, separately, **where memory actually
answers from**: one read at each of the four module bases, compared against this unit's own
answer for an address with nothing behind it. An *empty* `0xCC0000` is what gives the foreign
layout away, because that is precisely the module it does not have.

**This replacement is reasoning, not a demonstration.** It holds if the foreign part
answers the same no-memory word at `0xCC0000` as at `0xCD0000`, which is where this tool
learns that word from. Both addresses sit in the same hole on the layout we have, and the
argument survives whichever word that is *as long as the two agree* — but nobody here owns
an XC2768X, so that has not been measured, and the simulated ECU satisfies the assumption by
construction rather than testing it. If you have one of those parts, a single read at each of
`0xCC0000` and `0xCD0000` would settle it.

**Ask about memory the map says should be there, not about memory it says should not.** The
first version of this check also read `0xD00000` and treated content there as proof of the
foreign layout. That is unsound, and § 7 of [QUIRKS.md](QUIRKS.md) already said why before it
was written: `9b 1e` is the *flash controller's* answer for "my range, no memory here", and a
segment no flash controller decodes answers `0x46` instead — measured at `0x300000`.
`0xD00000` is outside this controller's range, so it can never return the pattern the test
compares against, so the test fires on every genuine unit. Measured on the bench 2026-07-31:
a correct ECU — Infineon, CHIPID `0x38`, 832 KB, all four modules answering — had its write
refused with a confident and completely wrong diagnosis. The offline suite had not caught it
because the simulated ECU returned `9b 1e` for every address outside its image, modelling the
controller as if it decoded the whole address space; it now models the boundary.

That second question is not redundant. `IDMEM` is a documented **`rw`** field — trimmed — and
the XE164xM and XE167xM data sheets print `0x30D0`, meaning 832 KB, for parts whose largest
derivative is 576 KB. So it may report what the die could hold rather than what this one
does. Four read-only frames settle against the silicon what a register cannot be trusted to
say. They also catch the 576 KB and 448 KB variants of *our own* die, which have a hole at
`0xC80000` where this tool assumes flash.

The revision step is deliberately not checked — a respin of the same device has the same map,
and refusing one would turn silicon housekeeping into a support ticket.
`--force-unknown-ecu` overrides the refusal, loudly.

**Reading is not gated**, and the reason is worth stating precisely rather than as a slogan.
A read cannot erase anything: the stage carries a segment mask of zero and the ECU itself
refuses. But uploading *any* stage is running code on the part, and on a part this was not
built for those 32 bytes are a different program. That risk is inherent to every bootloader
tool and cannot be designed away — what can be done is to say so, and to check identity as
early as the protocol allows, which is the first thing after the monitor answers.

## 5. Notes for anyone doing their own RE on this core

The flash command sequences above were cross-checked against the ECU's own firmware,
which means disassembling C166 code. Two things that cost time there:

* **Ghidra can do C166** with a community processor module, but at least one release of
  it references a register in its compiler spec that the language definition does not
  define, and the language then refuses to load at all. Deleting the offending line is
  enough. Worth knowing before concluding that C166 support is broken.
* **The decompiler drops flash command writes.** The first write of the program sequence
  (`0x50`, entering page mode) does not appear in decompiled output at all — it is only
  visible in the raw instruction bytes. If you are reconstructing a flash algorithm from
  decompiled C, you will get a sequence that looks complete and is not.

A raw-binary import needs its base address set to the flash address and needs the reset
vectors seeded by hand, since there is no entry point to discover automatically.

**The one document worth having open** is the *XC2000 Derivatives System Units User's
Manual* V1.0 (2007-06), volume 1 of 2 — chapter 3, "Memory Organization". Everything this
project needed about the flash is in it: the command sequences (§3.9.4), the array and
sector structure (§3.9.2), the protection model (§3.9.5.4 and §3.9.6), and the register
block at `0xFFFF00` (Table 3-7) that gives both the busy poll and the status registers.
It covers the base family rather than this exact derivative — the 64 KB array at
`0xCC0000` is not in it — but the flash controller is the same one.

**Assembling the stages** needs a C166 assembler. ASL — Alfred Arnold's macro cross
assembler, GPL, `asl` on Unix and `asw` on Windows — handles this core with `CPU 80C167`
and builds from source on all three platforms. `tools/build_stages.py` runs it and checks
the result against the committed blobs; a build of ASL 1.42 Bld 311 on macOS/arm64
reproduced all three byte for byte, including blobs originally produced on Windows.

## 6. How to avoid the errors this project made

These got furthest here, and each has a rule that would have stopped it.  This is the
single home for them; [QUIRKS.md](QUIRKS.md) points here rather than keeping a second copy.

**Suspect your own tooling before you characterise the hardware.** The largest apparent
physical-layer effects on this link — a corrupted byte at every turnaround, data-dependent
failure rates, a write ceiling below the read ceiling, a five-second erase — were the host
reprogramming its serial port mid-transfer. They were reproducible and had coherent
bit-level explanations. Drive the same bench from a second operating system before you
believe any of it.

**Prose review cannot catch a wrong fact.** The L9637D pinout in HARDWARE.md was wrong on
seven pins of eight and listed no supply pin at all for a part that needs 5 V. Three reviews
read that file; none could have caught it, because the table was internally consistent. A
photograph and a continuity tester caught it in a minute. Check hardware claims against
hardware.

**Read the page next to the one you needed.** `0xC0F000` cost several bench sessions of
"is this unit damaged?" — a question the unit cannot answer. The answer was note 3 under the
memory map table of the manual this project already quotes for its busy poll. Measurement
beats a plausible story; it does not beat not having read the datasheet.

**A percentage without its sample size is not a measurement.** Before a write
`calibrate_link` sends **48** probe frames (16 before a read), so "0.0 % of probe frames
needed a retry" is *0 out of 48* — and against a quarter of page payloads failing, a uniform
per-byte model puts the frame failure rate at 2.2 % and gives that clean sweep about a
one-in-three chance. The arithmetic: a 132-byte payload failing a quarter of the time means
a per-byte failure of *q* = 1 − 0.75^(1/132) = 0.00218, so a 10-byte frame fails
1 − (1−*q*)^10 = **2.2 %**, and P(0 of 48) = 0.978^48 = **0.35**. It was read instead as proof that something
depended on transfer length, which sent one investigation down a corridor that had nothing
in it. Nobody had written down N, because the log did not print it. It does now.

**A verdict is only as good as the comparison behind it.** The probe that settled
`0xC0F000` originally printed a conclusion drawn from two of its four readings, and that
conclusion was wrong. It ships without the verdict, printing the readings instead.

**A number is only comparable to a number taken the same way.** Failure counts depend on the
host's own stall window: the same bench read as 12 % of frames with one setting and 47 % with
another. Numbers taken under different settings are not evidence about hardware.

**Measure in the mode you work in.** A rate that streams reads perfectly says nothing about
whether it carries a write. Probe frames are ten bytes and page payloads a hundred and
thirty-two; test with the traffic the job actually uses.

**Correlation finds passengers.** Error rate correlated +0.70 with the fraction of one-bits
in the data, and bit density is not the mechanism — transitions are. The correlation was
real; the conclusion drawn from it was not.

Where this document states something, it should also say how it was established. Where it
cannot, it should say so plainly.

## 7. Validation you can run without an ECU

`tests/test_monitor_fakeecu.py` simulates the ECU at the wire level: half-duplex echo,
the two-stage handoff, flash that erases to `0x00` and programs bits one way only, the
segment gate read out of the uploaded stage image, the stage read back out of PSRAM, and
— importantly — the ability to dirty the line, both corrupting and dropping bytes.

One caveat on that last part, because it is the kind of thing this document exists to keep
honest: the dropped-byte scenario was written when the loss was thought to be a K-line
turnaround effect. It was not — it was the host reconfiguring its serial port mid-transfer
(§ *The host was the fault*), and it has not reproduced since. The scenario is kept as a
regression guard for the recovery path, not as a claim about the wire.

It has caught defects that hardware runs did not, including a payload framing flaw where
loose bytes on the wire could be re-read as a valid command. That one changed the
protocol.

`tests/test_monitor_offline.py` checks the embedded stage bytes against the assembler's
output, the patch offsets, the segment gate arithmetic and the frame construction.

Neither needs an ECU, a dump, or a serial port.

`tools/build_stages.py` goes one step further back, and needs a C166 assembler: it
re-assembles all three `.asm` files and compares the result with the `.bin` files committed
here. The blobs are committed so that flashing an ECU needs no toolchain; this is how you
check that those bytes are the ones the source in front of you produces, rather than taking
it on trust — for code that gets uploaded into an engine controller.
