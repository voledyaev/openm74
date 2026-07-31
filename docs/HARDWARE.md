# Getting an M74 CAN to talk: the hardware side

Everything here was established on a bench, on a real unit. The dead ends are recorded too,
as advice about what not to try: they are weeks somebody else does not have to spend.

![The ECU's own label](img/ecu-label.jpg)

Unit this was done on: **Itelma M74 CAN, part 11183-1411020-62, board silkscreen
M74_v6.36**, MCU **Infineon SAK-XC2765X-104F80L** (C166SV2 core, 832 KB internal flash,
8 MHz crystal). There is no separate flash chip: program and calibration both live inside
the MCU, so the only way in is the MCU's own loader.

---

## 1. The short version

1. Populate **L9637D** (SO-8) in the empty footprint.
2. Populate the accompanying resistor footprint with **100 kΩ (marking `104`)** — *not*
   510 Ω. This one value is the difference between a working ECU and one that will not
   start. See §3.
3. K-line comes out on **X1:G3**. Wire it to pin 7 of the adapter's OBD connector.
4. Power the ECU, assert program-enable **before** power, run the tool.

That is the whole modification. Nothing else is added, nothing is cut.

---

## 2. Why a modification is needed at all — on this unit

**Check your own board before believing this section.** On the M74_v6.36 unit this project
was built against, the CAN transceiver is populated and the **L9637D footprint is empty**,
so out of the box it speaks CAN and nothing else. That is one board. There is second-hand
evidence — videos of M74 CAN units being read and written over K-line with no modification
at all — that other production runs leave the factory with it fitted, and the layout around
the footprint carries several other unpopulated pads, which is what a board shared across
variants looks like.

So the honest statement is: **this unit needed the modification, and yours may not.** Look
before you reach for an iron. If the part is there, everything below is already done for
you and the tool should simply work.

And if it is *not* there, K-line cannot work by any other means on this board: the connector
pin ends at an unpopulated pad, the microcontroller's receive pin ends at another, nothing
joins them, and the 12 V bus would need level conversion even if something did. There is no
setting, pull-up or command that substitutes for the part.

![Where the L9637D goes on an M74_v6.36 board](img/board-transceiver-location.jpg)

The footprint is the one boxed in red, just right of the microcontroller. The MCU marking is
readable in that photo — `Infineon SAK-XC2765X-104F80L` — which is worth knowing because the
832 KB size also happens to match an ST10F276, and that coincidence has sent people looking
at the wrong datasheet.

That matters because of what the two paths can actually do:

| Path | Verdict | Why |
|---|---|---|
| **ASC0 UART / K-line, mask-ROM loader** | works | The ROM loader accepts arbitrary code into RAM and jumps to it. Arbitrary code execution means it can do anything — including read flash out. |
| Resident CAN loader | abandoned | It exists and it is proprietary, but its command table has erase, program and CRC — **and no read opcode at all**. Winning that fight would still not let you take a backup. |
| ROM CAN loader | never selected | The mask ROM runs exactly **one** loader, chosen from the level on P10.[3:0] latched at power-on. Measured on this unit: `STSTAT.HWCFG = 0x46`, low bits `0110` — the UART loader. Nothing since then reads CAN, whatever the timing. (An erratum about CAN bit-timing was blamed here for a long time; it fits the symptom and is not the cause — see [FINDINGS.md](FINDINGS.md) § 2.) |
| Diagnostic services over the running application | absent | Probed on a live unit: the flash-download services simply are not implemented. Not a permission problem — the handlers do not exist. |

So the K-line path is not a preference. It is the only route that can both read and write.

**The consequence worth internalising:** the ROM loader is in mask ROM. It does not care
what is in flash, so an ECU whose flash is empty or half-written should still answer it, and
that is why this tool is willing to erase before it rewrites.

Stated as inference, because that is what it is: this bench has never deliberately erased an
ECU and then recovered it from nothing. The reasoning is sound and the loader has answered
every time it has been asked, but the experiment has not been run. And recovery is not just
the loader answering — it needs the transceiver fitted, program-enable held on `X1:A4/B2`,
and a power cycle you can actually perform. On a bench that is a minute; in a car it is not.

---

## 3. The resistor — the single most important number here

The L9637D footprint has a companion resistor pad. What goes there decides whether the
ECU works at all.

* **510 Ω → the ECU will not come up on the line.** Reported by others with this exact part
  number and board revision. **Not measured here** — this unit has never had a 510 Ω part
  fitted, so what we can say is that the value below works, not that this one fails.
* **100 kΩ (`104`) → works.** This is what the unit here runs, and what people flashing
  these ECUs use.

### Why it matters even when the transceiver is fitted correctly

With the transceiver populated and this pad empty, the ECU **would not start its
application at all**. Symptoms: dead on CAN, no broadcast, no response to anything — a
unit that looks bricked but is not.

The mechanism: the L9637D's receive output follows the K line. With nothing holding that
side, the line floats, the MCU's serial input sits in the wrong state, and startup is
disturbed. The resistor pulls the logic side to a defined level and the ECU boots
normally.

Note the two positions are electrically different things, and this is where reading
forum threads goes wrong: this pad is **not** the ISO bus pull-up to Vbatt (which the
transceiver datasheet would want at ≤5 kΩ). Fitting a bus-sized 510 Ω here is what breaks
it. Do not "correct" the 100 kΩ to an ISO-looking value.

### What that value costs you

Nothing measurable. With 100 kΩ fitted, this bench reads and writes a whole 832 KB image at
115200 with **zero** retries, driven from macOS and from Windows alike.

**If your link is noisy, suspect the host before the soldering iron.** The largest apparent
link problems on this project came from the host reprogramming its own serial port during
transfers, and the symptoms read convincingly as physical-layer trouble — including a
failure rate that tracks the *shape* of the data rather than its content. See
[QUIRKS.md](QUIRKS.md) §1 first. Changing this resistor to an ISO-looking value is a known
way to stop the ECU coming up at all, so it is an expensive place to experiment.

### L9637D pinout, for the soldering

![The L9637D pads, with every pin named](img/l9637d-pinout.jpg)

```
  1  RX   → MCU, receive          8  LI   (L-line input, unused)
  2  LO   (L-line output, unused) 7  VS   ← +12 V from the vehicle
  3  VCC  ← +5 V                  6  K    → the K line, to X1:G3
  4  TX   ← MCU, transmit         5  GND
```

SO-8 numbering: 1–4 down one side, 5–8 back up the other, which is what the photo shows.

**How each pin is established** — because "from a datasheet" and "measured on this board"
are different strengths of claim and the difference matters when someone is holding an iron:

| Pins | Established by |
|---|---|
| **6 K, 5 GND, 7 VS** | continuity-tested on this board |
| **3 VCC, 5 GND** | the part works at all — swap these and it never powers up |
| **1 RX, 4 TX** | two-way traffic works — swapped, there would be no link whatsoever |
| **2 LO, 8 LI** | the datasheet only. They are the unused L-line pins and play no part here |

So every pin that affects anything is confirmed by the modification *working*, not by a
drawing. Only the two L-line pins rest on the datasheet alone, and nothing in this project
touches them.

If the ECU still will not talk after fitting: check 1, 3, 4, 5, 6 and 7 for bridges and cold
joints, confirm pin 3 has 5 V and pin 7 has 12 V, and check the resistor described above.

> **If you are writing a document that tells someone where to put solder, something physical
> has to have checked it.** An earlier version of the table above was wrong on seven pins of
> eight and listed no supply pin at all. Three reviews read it; none could have caught it,
> because internally consistent prose reads as correct. A photograph and a continuity tester
> caught it in a minute.

---

## 4. Connectors and pinout

Two connectors, and **the pin coordinates repeat on both** — the single most common way
to wire this up wrong.

* **X1** — signal, the large one. 12 columns lettered **M L K J H G F E D C B A** left to
  right (no `I`), rows **4 3 2 1** top to bottom. 48 pins.
* **X2** — power, the small one. 8 columns lettered **A B C D E F G H** left to right,
  rows **1 2 3 4** top to bottom. 32 pins.

They are mirror images of each other. `G3` on X1 is not `G3` on X2.

| Circuit | Connector : pins | Notes |
|---|---|---|
| +12 permanent (K30) | **X2 : H1, H2** | main supply; removing and restoring this is the power-cycle the loader needs |
| +12 ignition (K15) | **X2 : F2** | without it the ECU sleeps |
| Ground | **X2 : G2, G3, G4** | **not G1**; take it straight from the supply, not through the adapter |
| Program enable | **X1 : A4 and B2** | **apply before main power** |
| K-line | **X1 : G3** | to pin 7 of the adapter's OBD connector |
| CAN-H / CAN-L | X1 : E3 / E2 | not used by this tool |

> **Correction worth recording:** several sources state that ignition must be applied to
> **both** X2:F2 *and* X1:J1, "both, not either". On the bench used here **X1:J1 is not
> connected at all** and everything works — reads, writes, full images with independent
> verification. The extra wire is not needed.

### Order of connection

1. Assemble everything with power off
2. Apply +12 V to **X1:A4 and X1:B2** (program enable)
3. *Now* apply main power to **X2:H1/H2**
4. Apply ignition to **X2:F2**
5. Start the tool

With program-enable asserted the ECU stays silent on CAN and does not run the engine
application. That is correct and expected: it is sitting in the loader waiting.

### Adapter side

| OBD pin | To |
|---|---|
| 7 | K-line → X1:G3 |
| 4, 5 | ground, common with the ECU |
| 16 | **+12 V** — without it the adapter's transmitter is mute and nothing happens |

Keep the K-line wire short and the ground close to the ECU. Supply the ECU directly
rather than through the adapter — a drop to 6–7 V through a thin harness is enough to
cause confusing failures.

### Automating the power-cycle

Every operation starts with a power-cycle because the ROM loader arms on nothing else — and
that is [measured, not assumed](PROTOCOL.md): a watchdog reset does not bring it back. If you
run this often enough to want the prompt gone, the supply has to be switched, and two facts
decide how:

* **The K+DCAN cable cannot do it.** Measured here: toggling its DTR and RTS moved no input
  line, so those pins are not brought out of the moulded connector. It is a UART plus a
  K-line transceiver. It also does not source the ECU's supply — it takes +12 V from OBD pin
  16 to power its own transmitter.
* **A logic pin cannot switch it either.** The ECU draws amps; DTR is milliamps at 3–5 V. A
  relay module or a high-side MOSFET switch in the +12 V feed is doing the actual work.

So the practical shape is: leave the flashing cable alone, add any cheap USB-serial dongle
purely as a commandable pin, wire its DTR to a relay in the ECU's supply line, and tell the
tool where it is:

```
openm74 --port /dev/ttyUSB0 --power-port /dev/ttyUSB1 --power-line dtr --flash image.bin --yes
```

`--power-invert` if your switch is wired the other way round. Note that opening a serial port
asserts its handshake lines on most platforms, so the relay clicks once when the dongle is
opened, before the tool drives it anywhere — wire and sequence with that in mind.

**Not exercised on hardware here**, because this bench has no such switch. The code path is
opt-in and small; what it cannot do is make a cable into a relay.

---

## 5. Things that cost us time, so they need not cost you any

**A bias resistor on CAN made things worse.** An earlier bench had 510 Ω added between a
+12 rail and CAN-H. It actively prevented communication; removing it made the ECU talk
immediately. Do not add bias to CAN — the ECU has its own termination, and the diagnostic
tool at the other end is a second active node that biases the bus anyway. Measured across
CAN-H/CAN-L with everything unpowered you should see ~120 Ω (we read 123 Ω), which
confirms the transceiver and terminator are intact.

**Continuity checks that tell you the unit is alive, before you suspect the ECU:**
with power off, ground pins read short to the case; H1/H2 to ground reads as a capacitor
charging (the supply input is intact); F2 to ground reads open (ignition is a
high-impedance input — that is normal, not a fault). If all that checks out, silence on
the bench is a wiring or sequencing problem, not a dead ECU.

**The USB adapter can hang solid.** Symptom: the tool stops responding with the CPU at
zero, the process cannot be killed, and its thread sits in an uninterruptible driver
wait. Nothing in software can clear it. Unplug and re-plug the adapter's USB cable. The
ECU is unaffected; whatever was already written stays verified.

**Two things changed at once is not a measurement.** At one point the link degraded over
a long session; reseating the connector *and* power-cycling the adapter fixed it, and we
could not say which. Later data showed the "degradation" was largely an artefact of what
data was being written at the time. If you chase link quality, change one thing and
measure with identical traffic — see [FINDINGS.md](FINDINGS.md) for how easy it is to
fool yourself here.

---

## 6. Bringing up a bench that does not work yet

Work down this list rather than guessing — each step isolates one thing.

Simplest first — each step needs less to be working than the one below it, so start at the
top and stop at the first thing that fails.

```
(no flags)                 just the handshake: 0x00 out, 0xD5 back.  Needs only the wiring
--tx-probe                 upload a 32-byte stub that streams 0x55: proves code runs and
                           that the transmit path works.  Nothing is written.
--echo-test                is the receive path working at all?
--rx-read                  does the receive buffer return the byte that was sent?
--diswdt                   can the watchdog be disabled in this state?
--flash-probe              a stub that reads flash and streams it: proves flash addressing
--linktest                 both directions measured separately, with a verdict.  Note this
                           one needs the whole 2 KB monitor delivered first, so it is a
                           poor FIRST test on a bench that does not talk yet -- it is the
                           one to run once something answers and you want numbers.
--scan-baud                which line speeds get an answer (see the caveat below)
```

**None of these writes flash**, but not all for the same reason, and the difference matters
to anyone adding a ninth entry to this list.

* `(no flags)` and `--scan-baud` upload nothing at all — they are the handshake and a sweep
  of it.
* `--tx-probe`, `--echo-test`, `--rx-read`, `--diswdt` and `--flash-probe` upload a fixed
  32-byte stub. There is **no segment gate in them** — 32 bytes has no room for one — and no
  read-back either. What makes them safe is that none of them contains a flash command
  cycle: the sources are in `stages/diag-*.asm` and `tools/build_stages.py` checks that what
  the tool uploads is byte-for-byte what those sources assemble to. Read-only by
  construction, verified by build, not by a runtime check.
* `--linktest` is the only one here that brings up the full monitor, so it is the only one
  that compiles a zero segment mask and has its delivered image read back out of PSRAM and
  compared. The same is true of the `tools/probe_*.py` scripts.

`--oneshot` also uploads a stage (the streaming reader) with no gate and no read-back; it is
read-only for the same reason the diagnostics are.

That is not the same as "cannot damage the ECU", and this page used to say the stronger
thing. Uploading *any* stage means running code on the part, and these stages are assembled
for one memory map — flash at `0xC00000`, the transmit buffer at `0x4080`, the flash
controller at `0xFFFF00`. On a different derivative those addresses mean something else, and
a stub that lands there executes with complete confidence against the wrong ones. The risk
is inherent in bootstrapping an unidentified part and cannot be designed away; see
[FINDINGS.md](FINDINGS.md) § *What could still be wrong*.

Note also that the identity check runs only on the write paths. `--monitor`, `--mon-read`,
`--linktest` and the `tools/probe_*.py` scripts will happily bring up a stage on an ECU they
have not identified — which is exactly what you want when bringing up an unknown bench, and
exactly what you should know you are doing.

The five stubs they upload are assembled from `stages/diag-*.asm`, and
`tools/build_stages.py` checks that what the tool uploads is exactly what that source
produces. If you are about to put someone else's code into your ECU, being able to read
what it does — and check that the bytes match — is not a luxury.

Interpreting `--linktest`:

| Result | Look at |
|---|---|
| host→ECU lossy, ECU→host clean | the ECU's receive side: pull-up value, K-line wire length, ground |
| ECU→host lossy only | the adapter's receiver, or its USB driver |
| both lossy | grounding and wiring first: common ground straight to the ECU, short runs, supply not routed through the adapter |
| nothing answers at all | not a link problem — see §4 sequencing, and check +12 V on OBD pin 16 |

> `--scan-baud` is of limited use: the ROM measures the line speed from the first byte
> after a reset, **once**. Only the first entry in the list is a real test; everything
> after it is talking to a loader that has already locked on. One power cycle per speed.

### Finding a safe place to experiment on your own unit

Read the whole image first, then look for 4 KB sectors that are entirely `0x00` — erased
flash reads as zero on this part, so those sectors hold nothing. They are where to try
erase and program before touching anything that matters. On the unit here there were a
handful, scattered across three of the four modules.

Then use `--write-selftest <address> --sector`: it reads the sector, erases it, writes a
distinctive pattern, verifies the pattern arrived, erases again and puts the original
back. Net change zero, every step observable.

## 7. A word on the flash map

Not every address in the 832 KB window is memory. **`0xC0F000` is not flash at all** — it
reads a constant pattern, ignores erase entirely, and no image can change it. This is not
a fault of any particular unit: Infineon reserves that sector for the device's own use on
every part in the family, and says so by address (XC2000 System Units UM V1.0, Table 3-1
note 3). Expect it, and treat a firmware file that claims to hold data there as a file
whose reader zero-filled a sector it could not read.

The tool detects it at runtime rather than assuming: it erases, checks whether anything
actually changed, asks the flash controller whether it objected, and if the region cannot
be written it says so and continues instead of dying halfway through an image. The known
address is only a label on that measurement — a flasher that skipped a sector because a
constant in its source said to would skip a real failure just as happily.

If your unit stops on a region like this, that is the tool telling you the truth, not
failing.
