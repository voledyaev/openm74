# What the K-line actually does

None of this is in a datasheet. All of it was measured on hardware, and all of it had to
be handled before a write would complete reliably. If you are building something similar
on any K-line target, this is the part worth reading.

**Start with §1.** On this project, the single largest source of apparent link trouble was
the host's own serial driver, and every measurement taken before that was found is worth
nothing. Rule out your own software before you characterise a wire.

---

## 1. The host can be the fault, and the way it hides is specific

**Do not change a serial port's settings while a transfer is in flight.** In pyserial,
`timeout` is a property whose setter calls `_reconfigure_port()` on every assignment while
the port is open. On POSIX that is a `tcsetattr` with unchanged values and costs nothing.
On Windows `_reconfigure_port` builds a fresh DCB and calls **SetCommState** as well as
SetCommTimeouts — for a USB-serial adapter, control transfers that reprogram the device,
baud divider included.

Assign it per read, or between transmitting and reading the reply, and the port is being
reprogrammed at the exact moment bytes are on the wire. What that produces looks
convincingly like physics:

* the first byte after a direction turnaround arrives mangled, or is swallowed entirely;
* the failure rate depends on the *shape* of the data — payloads with identical one-bit
  counts but more transitions fail far more often, because a timing disturbance is
  invisible in constant data and fatal in alternating data;
* higher rates look unusable, and writing looks as though it has a lower ceiling than
  reading (a page payload is longer, so it spans more of these events than a command frame);
* a long idle period appears to cost the next transfer a byte, because "wait longer" and
  "reconfigure the port with a different value" are the same action;
* sector erase appears to take seconds, because every erase overruns the stall window and
  is driven through the recovery path, which is then what gets timed.

**How to catch it:** drive the identical bench from a different operating system. Physics
does not know which computer is at the far end of a cable; an IOCTL does. Here the same ECU,
cable, connector and pull-up needed hundreds of retries from Windows and produced **zero**
from macOS. That comparison is worth more than any amount of careful re-measurement on the
machine that has the bug.

**The fix** is `SteadyPort` in `klinebsl.py`: the port is configured once at open, and
`timeout` becomes a value the wrapper honours by polling, so no call site reaches the
driver. A test guards it — a regression would put the assignment count in the thousands.

With that in place, this bench runs clean at 115200 in both directions with zero retries
across a whole 832 KB image.

## 2. Recovery is worth more than avoidance

Nothing below is common on a healthy link, but a flasher runs on other people's benches and
has to survive all of it. The protocol therefore carries defences that cost nothing when
they are not needed: throwaway lead bytes before every reply, a marker run the monitor can
search for rather than count, 16-bit checksums, and a PROGRAM that asks before it sends.

Three failure shapes, and telling them apart is what makes recovery possible:

| What the host sees | What it means | What to do |
|---|---|---|
| No status at all | Either the monitor is counting payload bytes that never arrived, or it is idle and the host missed a reply | Drain, feed `0xA5` filler; if it answers, it was stuck — if it stays silent, it is idle, so resend the command |
| A status that is not `A5`-prefixed | The host is reading the middle of a stream rather than the front of one | Drain and ask again; the ECU is fine |
| Status `0x43` | The checksum failed, so **nothing was executed** | Resend; this is the checksum doing its job |

**Drain before interpreting anything.** A stalled READ leaves up to 4 KB of answer still
arriving, and a host looking for a status byte inside that finds payload for as long as it
cares to look — which turns a recoverable hiccup into "the link is gone".

Filler is safe from both ends: it completes whatever was being counted and breaks the
checksum, so a short payload can never reach flash, and if the monitor was not stuck then
`0xA5` is a frame marker it skips anyway.

**Never end a run for a reason the ECU has not given you.** A whole-image write that gives
up at sector 90 of 206 throws away everything done so far, and the ECU is usually still
sitting in the monitor answering perfectly.

## 3. Error rate depends on the data, through transitions

Measured with fixed patterns programmed into a scratch sector, 24 pages each:

| Payload | One-bits per byte | Rising edges per byte | Pages resent |
|---|---|---|---|
| `0x00` | 0 | 0 | 0 % |
| `0x0F` | 4 | 1 | 4 % |
| `0x55` | 4 | **4** | **25 %** |
| `0xFF` | **8** | 1 | 0 % |

`0x0F` and `0x55` carry the same number of one-bits and differ six-fold; `0xFF` carries the
most and costs nothing. So what matters is **how often the line has to change state**, not
how many ones the data holds. On a host with the §1 defect this is the signature to look
for; on a healthy one all four patterns measure zero.

**Consequence for calibration:** a probe must be **edge-rich** — alternating fields like
`0xA5`/`0x5A` — not merely "lots of ones", which `0xFF` shows is free. A probe of sparse or
constant fields measures the easiest traffic there is and flatters the link.

## 4. Speed is limited by the divider arithmetic, not the wire

Each rate exercised the way it is used — a sustained 32 KB read **and** real page
programming, not a PING:

| Rate | Sustained read | Page programming |
|---|---|---|
| 38400 | 2415 B/s, 0 bad | 0 resent |
| 57600 | 3604 B/s, 0 bad | 0 resent |
| 76800 | 4720 B/s, 0 bad | 0 resent |
| **115200** | **6973 B/s, 0 bad** | **0 resent** |
| 153600 | the ECU answers at no rate afterwards | — |

Reading and writing stop in the same place. There is no separate write ceiling.

**The rate you ask for is not the rate you get, and the error is not always small.** The
ECU's divider is an integer, so a requested 38400 lands on 38850. Leave the host at the
round number and it wears that 1.2 %; a UART resynchronises only on the start bit and then
counts blind, so the error accumulates across the byte.

**Worse, the arithmetic itself rests on one measurement.** The rate constant comes from the
divider the boot ROM chose while autobauding the host's 9600, and that divider is an
integer too — on the same ECU minutes apart it came back 130 and then 131, moving everything
derived from it by 0.8 %. At 115200 that is the difference between a 32 KB read arriving
byte-perfect and the stream tearing apart.

**So do not trust the estimate.** After setting the divider, sweep a few tenths of a percent
around the computed rate and keep the one that answers a PING — a small autobaud on the
host's side, which is the end that can afford to search.

## 5. There is no idle effect

Twenty frames per condition, at 115200:

| | |
|---|---|
| Back to back | 0 of 20 failed |
| After 1.5 s of silence | 0 of 20 failed |
| After 3.0 s of silence | 0 of 20 failed |

Silence costs nothing. If you see otherwise, suspect §1 before the wire — an experiment
whose independent variable also perturbs the apparatus cannot be rescued by repeating it
more carefully.

## 6. Write time is programming, not erase

Timed at 115200, per 4 KB sector:

| | |
|---|---|
| Sector erase | **0.02 s** |
| Programming its 32 pages | **1.44 s** |
| Verify by checksum | 0.02 s |
| Reading 4 KB back | 0.59 s |
| **End to end** | **1.48 s** fast, **2.64 s** reliable |

Reliable mode reads the sector back **twice** — once after the erase to prove it is blank,
once after programming to compare byte for byte — so it pays 0.59 s twice, not once. That is
easy to get wrong by adding up the rows: this table said 2.05 s until a 206-sector run
predicted 7:02 from that figure and took 9:12, its own ETA settling at 2.68 s per sector.

The erase is 20 ms and is a rounding error. Write time sits almost entirely in programming,
which **does** scale with the line rate, so raising the baud helps writing as much as
reading. A whole image runs about 8 minutes in fast mode and 13 in reliable, backup
included.

**If you measure a five-second erase, you are timing your own recovery path**, not the
flash.

## 7. Not every address in the window is flash

On this part, `0xC0F000` reads a constant pattern (`9b 1e` repeating), ignores erase
completely, and cannot be changed by any image.

**How to tell nothing is behind it:** addresses *past the end of the populated array*
(`0xCD0000`, `0xCF0000`) read the identical `9b 1e`, while a segment with no flash controller
at all (`0x300000`) reads `0x46` instead. So `9b 1e` is the flash controller's answer for
"my range, no memory here" — not a bus default, and not data.

**It is the part, not the unit.** Infineon reserves that sector on every device in the
family: XC2000 System Units UM V1.0, Table 3-1 note 3, *"The 4 KB sector from C0'F000H to
C0'FFFFH is not accessible to the software"*. It is physical sector 15 of flash module 0,
which is why the same table lists Flash 0 as 252 KB rather than 256. Do not go looking for a
fault here, and do not accept an image that claims to have data there — firmware files
holding 4096 zeros at that offset were written by a reader that zero-filled a sector it
could not read.

**Why this matters for a flasher:** a whole-image write must not die on a region that is
physically unwritable. But "erase changed nothing" is *also* what a genuinely failing erase
looks like, and silently skipping that would leave stale data behind a green success line.
The deciding question is different: does what is already there happen to be what the image
wants? If yes, harmless, continue. If no, the image did not fully land and the tool must
say so.

**Ask the controller rather than inferring.** The stage latches `IMB_FSR_OP` and
`IMB_FSR_PROT` after every operation, so "the sector is write-protected", "the command
sequence was refused" and "there is nothing there" are three different sentences instead of
one shrug. `tools/probe_protection.py` asks read-only.

See [FINDINGS.md](FINDINGS.md) §2 for the full probe.

## 8. The USB adapter can hang unkillably

CPU at zero, process cannot be terminated, its thread parked in an uninterruptible driver
wait, `read()` never returning so no timeout ever fires. Only unplugging the adapter's USB
cable clears it.

Reprogramming a serial port thousands of times during active I/O is a plausible way to
wedge a driver, so §1 is the first thing to rule out. It has not been seen since that was
fixed — which is not proof, because it was never frequent. If you meet it, say so.

---

**A second shape of the same problem, measured 2026-07-31.** The adapter can also wedge
without hanging anything. The device node is present and freshly dated, the system still
lists the FT232R, `open()` succeeds — and then every attempt to configure the port returns
`EINVAL`. Not a wrong baud: writing back the *exact* attributes just read from the device
fails identically, and so does every rate.

What makes it worth documenting is where it surfaces. The failure happens inside pyserial's
own `open()`, so an unguarded tool reports a bare `termios.error (22, 'Invalid argument')`
with no port name in it — which reads as "your settings are wrong" and sends people to check
baud rates that were never the problem. The tool now names it, says what to do, and reports
it as a plain message with a non-zero status rather than a stack trace over the top of its
own diagnosis.

**Reading the port's settings still works, and that is a trap.** Measured 2026-07-31, the
second time this appeared: `stty -f /dev/cu.usbserial-XXXX` prints the current settings
quite happily and exits 0, `tcgetattr` succeeds — and then writing back *the very same
attributes* fails. `stty -f /dev/cu.usbserial-XXXX 9600` fails identically. So a quick
`stty` check reads as "the adapter is fine" when it is not; the diagnostic that
distinguishes the two is setting, not reading:

```python
fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
termios.tcsetattr(fd, termios.TCSANOW, termios.tcgetattr(fd))   # EINVAL => wedged
```

**What clears it: removing power from both sides.** Unplug the USB end *and* switch the
supply to the ECU off, then reconnect. USB alone is not enough, and the reason is structural
rather than particular to one adapter: on a K-line interface the transceiver is fed from the
vehicle's +12 V, so pulling the USB lead leaves the device part-powered with its state
intact. If your adapter has a power or ready indicator, watching it is quicker than any of
the checks above.

A different USB port, a reboot, and finally the same adapter on another machine are the
escalation if a full power-down does not clear it; the last of those separates a wedged
driver from a damaged adapter.

**Unplugging the USB end does not necessarily clear it, and this file said it would.** That
sentence was carried over from the hang above without being tested against this symptom.
Measured immediately afterwards: the adapter was unplugged and reconnected, the device node
was recreated with a fresh timestamp, no process held it, no third-party driver was
installed — and `EINVAL` came straight back. `stty` fails on it too, so this sits below any
application and below any language runtime.

What is worth trying, in order, and what each one tells you:

| | |
|---|---|
| A different USB port, not through a hub | a different controller path |
| Rebooting the host | clears the driver state completely; if this fixes it, the adapter is fine |
| **The same adapter on another computer** | **the discriminating test** — working there means the first host's driver was wedged; failing there too means the adapter itself is damaged |

Nothing on the vehicle side is involved either way: power-cycling the ECU and re-seating the
OBD end change nothing.

## The measurement lessons

They cost more than the findings, and they live one file over so there is one copy of them:
[FINDINGS.md](FINDINGS.md) §6. Four rules, each of which would have stopped a mistake this
project actually made — your own tooling is a suspect, a number is only comparable to one
taken the same way, measure in the mode you work in, and correlation finds passengers.
