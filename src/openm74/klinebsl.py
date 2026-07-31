#!/usr/bin/env python3
"""K-line / ASC0 mask-ROM BSL host for Infineon XC2765X (VAZ Lada M74 CAN ECU).

Cross-platform: pure Python + pyserial (Windows / Linux / macOS), no vendor DLLs.
Talks to the MCU's Infineon BootROM over K-line (populated L9637D) through any
FTDI/serial K-line adapter (e.g. a K+DCAN cable).

Protocol (Infineon XC2200 UM ch.10, standard UART BSL -- same BootROM as XC2765X):
  1. host -> 0x00 ; BootROM autobauds, replies 0xD5           [CONFIRMED @9600]
  2. host -> EXACTLY 32 bytes ; loaded to PSRAM 0xE00000, then JMP 0xE00000
  BootROM leaves DPP1=0x81, so ASC0=USIC0 regs sit at 16-bit addresses 0x40xx:
     RBUF=0x405C  PSR=0x4044  PSCR=0x4048  TBUF00=0x4080  (TBUF00 = inferred)
  Flash is directly memory-mapped for READS at 0xC00000 (832 KB).  Stage in RAM
  only reads flash + writes ASC0 -> it CANNOT brick the ECU (no flash writes).

Usage (installed as `openm74`; `openm74 --help` is the authority, this is the tour):
  openm74 --port COM3                      # single handshake (0x00 -> 0xD5)
  openm74 --port COM3 --oneshot --out dump.bin      # full 832KB in ONE pass
  openm74 --port COM3 --flash image.bin --yes       # backup, erase, program, verify
  openm74 --port COM3 --linktest           # measure the bench, write nothing
  openm74 --verify FILE                    # offline: compare against --reference

Flash monitor (the write path -- stages/stage2mon.asm):
  openm74 --port COM3 --monitor                        # start it; READ-ONLY self-test
  openm74 --port COM3 --mon-read 0xCC0000 --len 4096   # read through it (read-only)
  openm74 --port COM3 --write-selftest 0xCCF000 --yes  # erase ONE page, restore it
  openm74 --port COM3 --write cal.bin --addr 0xC80000 --yes      # program a region
A read-only session compiles a segment mask of ZERO into the stage, so it cannot erase
even if the host is wrong; a write session enables only the segments --addr/--len cover.
Erased flash on this part reads back as 0x00, not 0xFF.
Power-cycle the ECU (A4/B2 asserted) before each run so the BSL is freshly armed.
"""
import sys, os, time, argparse, json

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("needs pyserial:  python -m pip install pyserial")

FTDI_VIDPID = (0x0403, 0x6001)
BSL_ACK = 0xD5

# ---------------------------------------------------------------------------
# BRING-UP DIAGNOSTICS -- five 32-byte stubs for a board that will not talk.
#
# Assembled from stages/diag-*.asm, and every one of them is checked back against these
# bytes by tools/build_stages.py.  That check is the point: these are uploaded into
# somebody's engine controller, so "here is a hex blob, and elsewhere is its alleged
# source" is not good enough -- the two have to be connectable by running one command.
# They were opaque literals with a prose description until publication review; four of
# them had no source in the repository at all.
#
# All five are READ-ONLY by construction: they touch the UART, and diag-flashprobe also
# reads flash.  None can erase or program anything, whatever the addressing turns out to
# be, which is why they are safe to run first on hardware nobody has talked to yet.
# docs/HARDWARE.md 6 says which to reach for when.

# --- stages/diag-txprobe.asm: does uploaded code run, and is TBUF00 where we think? ---
_TX_PROBE = bytes([0xE6, 0xF1, 0x80, 0x40,
                   0xE6, 0xF0, 0x55, 0x00,
                   0xB8, 0x01,
                   0x0D, 0xFE])
TX_PROBE = _TX_PROBE + b"\x00" * (32 - len(_TX_PROBE))   # BootROM wants exactly 32

# --- stages/diag-flashprobe.asm: is flash addressing right? ---
# Streams segment 0xC0 from 0xC00000 upward, one byte at a time, for ever.  Compare what
# arrives against a known image of the same ECU.
FLASH_PROBE = bytes([0xE6, 0xF1, 0x80, 0x40, 0xE0, 0x00, 0xE0, 0x05,
                     0xA7, 0x58, 0xA7, 0xA7, 0xD7, 0x00, 0xC0, 0x00,
                     0xA9, 0x05, 0xB8, 0x01, 0xE6, 0xF2, 0x00, 0x40,
                     0x28, 0x21, 0x3D, 0xFE, 0x08, 0x51, 0x0D, 0xF4])

FLASH_SIZE = 0xD0000            # 832 KB (segments 0xC0..0xCC)

# --- stages/diag-echo.asm: is the receive path alive? ---
# A byte sent to an ECU running this comes back TWICE: once from the half-duplex bus
# itself and once from the stub.  One echo proves nothing; two proves the receive path.
ECHO_SERVER = bytes([0xE6, 0xF1, 0x80, 0x40, 0xA7, 0x58, 0xA7, 0xA7, 0xF2, 0xF5, 0x58, 0x40,
                     0x66, 0xF5, 0x00, 0x60, 0x2D, 0xF9, 0xF2, 0xF5, 0x5C, 0x40, 0xB8, 0x51,
                     0x0D, 0xF5]) + b"\x00" * 6

# --- stages/diag-rxread.asm: does the buffer return the byte that arrived? ---
# Waits for ONE byte, then streams it for ever with no further receiving.  Send 0x5A: a
# clean unbroken run of 0x5A back means the read is right, and nothing else does.
RX_READ = bytes([0xE6, 0xF1, 0x80, 0x40, 0xA7, 0x58, 0xA7, 0xA7, 0xF2, 0xF5, 0x58, 0x40,
                 0x66, 0xF5, 0x00, 0x60, 0x2D, 0xF9, 0xF2, 0xF2, 0x5C, 0x40, 0xA7, 0x58,
                 0xA7, 0xA7, 0xB8, 0x21, 0x0D, 0xFC]) + b"\x00" * 2

# --- stages/diag-diswdt.asm: can the watchdog be switched off here? ---
# Disables it, then streams 0x55 for ever WITHOUT serving it.  A stream that outlives the
# watchdog period says DISWDT took -- which is what lets the receiver fit in 32 bytes.
DISWDT_TEST = bytes([0xA5, 0x5A, 0xA5, 0xA5, 0xE6, 0xF1, 0x80, 0x40, 0xE6, 0xF0, 0x55, 0x00,
                     0xB8, 0x01, 0x0D, 0xFE]) + b"\x00" * 16

SEG_LO, SEG_HI = 0xC0, 0xCC     # flash segments 0xC0..0xCC inclusive (13 x 64KB)

# ---------------------------------------------------------------------------
# ONE-SHOT 2-stage dump: 32-byte stage-1 receiver + 2KB stage-2 flash dumper.
# (stages/stage1recv.asm + stages/stage2dump.asm -- assemble with asw/p2bin.)
#
# The earlier receiver failed because it polled the wrong status word and never
# cleared the receive flags, so RBUF reads repeated and desynced ("RBUF returns
# garbage").  Four things fix it and make a correct receiver fit in 32 bytes:
#   1. DISWDT once -> watchdog off for the WHOLE session, stage-2 included.  That
#      is what allows a single uninterrupted 832KB pass instead of 13 power-cycles.
#   2. DPP0 = 0x380 maps data addresses 0x0000-0x3FFF onto PSRAM 0xE00000, so a
#      store is a 2-byte MOVB [Rn] instead of a 6-byte EXTS block.
#   3. The BootROM's own BSL receive loop leaves its USIC pointers in R0/R1/R2 and
#      they survive the jump into the stage:  R0->PSR 0x4044, R1->PSCR 0x4048,
#      R2->RBUF 0x405C.  Free pointers are what buys room for the flag-clear.
#   4. PSCR <- 0x6000 after every byte re-arms the receive flags (the missing piece),
#      and CMPI1 compares-and-increments in one instruction so the destination
#      pointer doubles as the loop counter.
# Stage-1 stores 2048 bytes to 0xE00020..0xE0081F, then FALLS THROUGH into stage-2.
STAGE1 = bytes([0xA5, 0x5A, 0xA5, 0xA5, 0xE6, 0x00, 0x80, 0x03, 0xE6, 0xF6, 0x20, 0x00,
                0xE6, 0xF3, 0x00, 0x60, 0xA8, 0x50, 0x9A, 0xF5, 0xFD, 0xE0, 0xB8, 0x31,
                0xC9, 0x62, 0x86, 0xF6, 0x1F, 0x08, 0x3D, 0xF8])
STAGE2_LEN = 2048               # stage-1 receives exactly this many bytes (zero-padded)

# Stage-2: streams flash 0xC00000..0xCCFFFF out of TBUF00, one unrolled loop per 64KB
# segment (immediate EXTS #seg,#1 -- the proven addressing form), then spins quietly.
# No SRVWDT: stage-1's DISWDT already killed the watchdog.  READ-ONLY -> cannot brick.
#
# It sends PREAMBLE first.  Measured on the bench: the very first byte stage-2
# transmits is corrupted (0xFA arrived as 0xFE, one bit) because the K-line
# transceiver is still turning around from receive to transmit; every byte after it
# was exact.  The preamble takes that hit so no flash byte ever does.
SYNC = 0xA5
PREAMBLE_LEN = 8
BASE_DELAY, BASE_BAUD = 0x4000, 9600    # the pacing proven on the bench: 610 B/s @9600


def pacing_delay(baud):
    """Inner-loop count that paces one transmitted byte, scaled from the proven point.

    0x4000 gave 1.64 ms/byte at 9600, where a byte occupies 1.04 ms -- a ~1.6x margin.
    Keeping the delay inversely proportional to the baud keeps exactly that margin, so
    a faster line actually goes faster instead of idling in a delay tuned for 9600."""
    return max(0x100, int(BASE_DELAY * BASE_BAUD / float(baud)))


def build_stage2(delay=BASE_DELAY):
    """Assemble stage-2 for a given pacing delay (see stages/stage2dump.asm)."""
    dlo, dhi = delay & 0xFF, (delay >> 8) & 0xFF
    code = ([0xE6, 0xF1, 0x80, 0x40,                    # MOV R1,#4080h   -> TBUF00
             0xE6, 0xF0, SYNC, 0x00,                    # MOV R0,#00A5h   sync byte
             0xE0, 0x84,                                # MOV R4,#8       preamble count
             0xB9, 0x01,                                # pL: MOVB [R1],RL0
             0xE6, 0xF2, dlo, dhi,                      # MOV R2,#delay   pacing
             0x28, 0x21,                                # pD: SUB R2,#1
             0x3D, 0xFE,                                # JMPR NZ,pD
             0x28, 0x41,                                # SUB R4,#1
             0x3D, 0xF9]                                # JMPR NZ,pL
            + [b for seg in range(SEG_LO, SEG_HI + 1)
               for b in (0xE0, 0x05,                    # MOV R5,#0     offset in segment
                         0xD7, 0x00, seg, 0x00,         # bL: EXTS #seg,#1
                         0xA9, 0x05,                    # MOVB RL0,[R5] read flash byte
                         0xB9, 0x01,                    # MOVB [R1],RL0 transmit it
                         0xE6, 0xF2, dlo, dhi,          # MOV R2,#delay pacing
                         0x28, 0x21, 0x3D, 0xFE,        # dL: SUB R2,#1 ; JMPR NZ,dL
                         0x08, 0x51,                    # ADD R5,#1  (Z on 0xFFFF->0 wrap)
                         0x3D, 0xF6)]                   # JMPR NZ,bL
            + [0x0D, 0xFF])                             # JMPR $        quiet spin
    body = bytes(code)
    # raise, not assert: under `python -O` this vanishes and the padding below becomes
    # b"\x00" * negative == b"", so an over-length blob is uploaded whole and the surplus
    # lands in the running stage.  Same reasoning as mon_erase/mon_program.
    if len(body) > STAGE2_LEN:
        raise ValueError("stage-2 is %d bytes; stage-1 receives exactly %d"
                         % (len(body), STAGE2_LEN))
    return body + b"\x00" * (STAGE2_LEN - len(body))


ONESHOT_ECHO = 32 + STAGE2_LEN  # K-line is half-duplex: both stages echo back first
ONESHOT_START = ONESHOT_ECHO + PREAMBLE_LEN         # where the flash payload begins

# An optional image to compare reads against -- your own earlier dump of this ECU, so a
# read can be checked for repeatability and a write self-test can report what changed.
# There is no default and none is shipped: a dump is one vehicle's data.  Set it with
# --reference, or the OPENM74_REFERENCE environment variable.  Everything that uses it
# degrades quietly when it is absent.
REF_BIN = os.environ.get("OPENM74_REFERENCE", "")

# ---------------------------------------------------------------------------
# FLASH MONITOR (stage-2 alternative): erase / program / read back in ONE session.
#
# Assembled from stages/stage2mon.asm -- 772 bytes into the same 2048-byte slot
# stage-1 already delivers.  Instead of blasting the whole flash out of TBUF00 like
# stage2dump, it sits in a command loop, so the host can erase a sector, program its
# pages and read them back WITHOUT a power cycle between the steps.
#
# The flash command cycles it issues are the documented Infineon FAPI ones, confirmed
# byte-for-byte against this ECU's own writer routines (see docs/PROTOCOL.md §6).  Erased flash
# reads back as all-ZERO on this part, not 0xFF -- blank-checks below expect 0x00.
#
# SAFETY, in layers:
#   * the stage carries a SEGMASK bitmap of the segments erase/program may touch, and
#     the host derives it from the --addr/--len you actually typed.  A monitor built
#     for 0xCC cannot reach program flash even if the host misbehaves.  ONE BIT PER 64 KB
#     SEGMENT, though: it cannot say "this segment, above this offset", so a whole-image
#     write -- which starts at 0xC02000, inside segment 0xC0 -- unlocks that whole segment
#     including the boot sector.  What keeps erases out of the boot sector there is the
#     host's addressing, asserted at mon_erase/mon_program rather than left to a loop bound.
#   * READ is ungated (reading damages nothing) -- which is what lets us prove the
#     addressing works before any erase ever runs.
#   * the host refuses the boot sector by default and requires --yes to write at all.
# Recovery: the BSL we enter through is MASK ROM, so even a fully erased flash still
# comes back through this same path.  A bad write is repairable; that is why erase and
# program are acceptable to run here at all.
MONITOR_HEX = (
    "e6f04440e6f14840e6f25c40e6f78040e6f30060e109e6fcffffe6fa8009b8ca"
    "e6fa8209b8cae088e7f8a500ca009e0228813dfce7f85500ca009e02ca009202"
    "47f8a500ea305c00ca00920247f8a500ea206800f064f0f4ca009202f05400f4"
    "ca009202f09400f45c897059ca009202f0b400f4ca009202f08400f4ca009202"
    "f09400f45c897089ca009202f0d4ca009202f0945c8970d940fdea30f4004861"
    "ea2000014862ea2024014863ea207c014864ea20bc014865ea2082014866ea20"
    "0c014867ea204001e7f84e00ca00ae02ea005c00e7f84300ca00ae02ea005c00"
    "e7f85500ca00ae02ea005c00e7f84b00ca00ae02e6fa1e40b88ae6faa202b85a"
    "ea005c00e7f84b00ca00ae02dc0ba985ca009e0208512881ea302c01ea005c00"
    "e7f84b00ca00ae02e00ce00ddc0ba98500c400dc08512881ea304c01f06cf18c"
    "ca009e02f18dca009e02f06df18cca009e02f18dca009e02ea005c00e03fea00"
    "8601e6ff3300ca00c80260ddea20e800ca000c03e6faaa00e6fc8000ca00c202"
    "e6fa5400e6fcaa00ca00c202f0a5f0cfca00c202ca00e402ea005802ca00c802"
    "60ddea20e800e7f84b00ca00ae02ca009202ca009202e6fa0009e6f98000e00f"
    "ca009202b98a00f408a12891ea30e001ca009202f0d4ca009202f0945c8970d9"
    "40fdea30f400ca000c03e6faaa00e6fc5000ca00c202f0a5e6fcaa00ca00c202"
    "ca00e402e6fd0009e6f9400098cde6faf200ca00c2022891ea302c02e6faaa00"
    "e6fca000ca00c202e6fa5a00e6fcaa00ca00c202ca00e402f06ce6fa083fd740"
    "ff03a8dae6fa8009b8dae6fa0a3fd740ff03a8dae6fa8209b8daca001803e7f8"
    "4b0048602d02e7f85400ca00ae02ea005c00a8e09afefde0b831a982cb00b987"
    "e6f9004028913dfeb831a892cb00f0d4e7f8a500ca009e02ca009e02f04dea00"
    "9e02dc0bb8cacb00f0cb26fcc000f0dc66fdf0ff3d05e01d4cdc66fd0010cb00"
    "e00dcb00e6f80002e6fa063fe6feffffd740ff03a8ca66fc0f002d0628e13df8"
    "28813df4e01ccb00e00ccb00e6faaa00e6fcf500ea00c202e6faaa00e6fcf000"
    "ea00c202")
MON_DELAY_OFF = 0x282           # the MOV R9,#DELAY immediate inside putb
MON_SEGMASK_OFF = 0x2BC         # the AND R13,#SEGMASK immediate inside gate
# The assembler's output, shipped as package data so the embedded bytes can be checked
# against it without installing a C166 assembler.
_HERE = os.path.dirname(os.path.abspath(__file__))
MON_ASM_BIN = os.path.join(_HERE, "stages", "stage2mon.bin")

MON_READY, MON_OK, MON_REFUSED, MON_TMO, MON_CKS = 0x55, 0x4B, 0x4E, 0x54, 0x43
# The complete set the monitor can send.  Anything else in a status position is noise, and
# must be retried rather than reported -- see mon_cmd.
MON_STATUS_BYTES = frozenset((MON_READY, MON_OK, MON_REFUSED, MON_TMO, MON_CKS))
# Every monitor reply arrives behind exactly this many 0xA5 lead bytes.  MEASURED: the
# first byte the ECU transmits after receiving is corrupted by the K-line turnaround
# (a PING answered 0xD5 for 0x55; a READ answered "e9 fa c0 a2 02" -- status mangled,
# all four flash bytes exact).  The lead takes that hit; lead[-1] must arrive as 0xA5,
# which doubles as proof the line is still in step.
MON_LEAD = 2
# How many 0xA5 markers precede a command frame.  Two: the glitch lands on the first, the
# monitor skips any further ones (a command byte is never 0xA5), and the frame survives.
#
# Do not raise this to defend against corruption.  On a healthy host the link needs no
# defending -- whole-image writes complete with zero retries -- and on a sick one the cause
# is not the number of markers (see SteadyPort).  The monitor skips however many arrive, so
# this stays a host-side number and costs two bytes per frame.
MON_MARKERS = 2
# Pause before WE transmit, once the ECU has just been transmitting (ISO 14230 calls
# this P2).  MEASURED, and it did NOT help: 6 ms gave the same ~10% of page transfers
# needing recovery, and one page then failed three times running.  So the corruption was
# not the line failing to settle.  Kept as a knob (--settle) because the experiment is
# cheap to repeat, but the default is deliberately small.
#
# ROOT CAUSE, since found: the host was reconfiguring its own serial port between
# transmitting and reading the reply (SteadyPort).  Everything that pointed away from the
# wire pointed at it and was read as a puzzle instead of a clue -- reads byte-perfect at
# 256/256 while only host->ECU corrupted, which no baud mismatch can produce because the
# ECU's USIC drives both directions from one generator; and repeated failures on the SAME
# page, which no random-noise model explains either.  With the port left alone the effect
# is gone: 0 recovery events across a whole 832 KB image at 115200.
SETTLE_S = 0.002

# How long to wait for a reply before assuming the line swallowed a byte and poking it.
# Getting this wrong costs both ways, and I got it wrong in both directions before
# measuring properly:
#   30 s  -- every genuine stall cost half a minute; a full image was heading for 4 hours
#   2.0 s -- looked right on paper and was WORSE on the bench: 15.7 recovery events per
#            sector against 2.3, and an ETA of 69 min against 28.
# CAVEAT ON THOSE COUNTS, added after they were understood: they were taken before erase
# got its own status wait (SLOW_LOOK), so roughly one "recovery" per sector was this code
# poking a flash chip that was simply still busy, not a line that had lost a byte.  The
# comparison between 0.8 s and 2.0 s stands -- both runs carried the same artefact -- but
# the absolute numbers were inflated.  With SLOW_LOOK in place a full 206-sector image
# wrote at 19200 with ZERO recovery events, measured.
# WHY the hasty value wins was explained here for a long time by an idle effect: silence
# of about a second was said to cost the next transfer a byte, so a patient timeout
# manufactured the very stalls it waited through.  That explanation is RETRACTED.  Measured
# afterwards under controlled conditions -- twenty frames per condition at 115200, back to
# back and after 1.5 s and 3.0 s of silence -- silence cost nothing at all, 0 of 20 failing
# in every case (QUIRKS section 5).  The original effect was almost certainly the host
# reconfiguring its own serial port, which is what SteadyPort exists to stop.
#
# The VALUE stays at 0.8, because the comparison that produced it was a direct measurement
# of this timeout against a slower one and is unaffected by why: a short timeout recovers a
# genuine stall in under a second instead of over two, and nothing observed since has argued
# for waiting longer.  Tuned empirically; do not "fix" it from first principles without
# measuring, and do not restore the idle story to justify it.
STATUS_LOOK = 0.8

# Two modes instead of asking the user to pick a baud rate -- picking a baud is an
# engineering decision, not a user preference.  The people who need this tool are
# flashing other people's ECUs and are far more afraid of bricking one than of waiting,
# so RELIABLE is the default and buys every safety measure available:
#   * the slower line rate (see the caveat above about what those recovery counts included)
#   * a full backup read BEFORE anything is erased -- and the write is refused if it
#     fails, so there is never a write without a recovery image of THAT ECU
#   * a blank-check read after every erase, so a bad erase is caught before programming
#   * a full image read-back at the end, on top of the per-sector verify
#   * generous retries
# FAST trades away the VERIFICATION DEPTH, and only that: the backup is taken in both, and
# was silently skipped in fast until a review caught the README promising otherwise.
# baud = where the handshake happens (9600 is where the BSL autobaud is most dependable;
# it misses 38400 two tries in three).  run = where the session actually runs, reached by
# setting the USIC divider once the monitor is up, which the autobaud ceiling cannot stop.
# MEASURED ceiling on this bench: 115200, streaming byte-perfect at 6973 B/s with nothing
# resent (the full ladder is tabulated above RUN_LADDER).  This comment used to record the
# ceiling as 57600, with a sustained read tearing at 76800 -- that was measured before
# SteadyPort, when the host was reprogramming its own port between transfers, and it is the
# single largest number this project has had to revise.
# The part that did NOT change: a PING answering proves nothing about a rate.  Short
# exchanges passed at rates that tore apart under a sustained read then, and the rate search
# still requires a full 16 KB at a sane fraction of the line rate for exactly that reason.
MODES = {
    # Both modes run at the same rate, decided by measurement, and differ in exactly one
    # thing: how hard a written sector is checked.  Do not make the safer mode slower on the
    # line as well -- that bundles "check it properly" with "and wait four times as long" for
    # no gain, since the rate a link sustains has nothing to do with how the result is
    # verified.
    "reliable": dict(baud=9600, run=115200, retries=12, blank_check=True, backup=True,
                     final_verify=True),
    # backup=True in BOTH, because that is what the README has always promised -- "a full
    # backup is read before a single byte is erased, and a write that cannot get that backup
    # does not start".  fast used to skip it, so the one documented, load-bearing safety
    # guarantee was false for exactly the mode people reach for when they are in a hurry.
    # What fast trades away is verification depth, not the recovery copy.  --no-backup is
    # still there for someone who means it, and now it is the ONLY way to lose the backup.
    "fast": dict(baud=9600, run=115200, retries=6, blank_check=False, backup=True,
                 final_verify=False),
}
CMD_PING, CMD_READ, CMD_ERASE_PAGE, CMD_PROG, CMD_ERASE_SECTOR = 1, 2, 3, 4, 5
CMD_BAUD, CMD_CKSUM = 6, 7
# baud x (PDIV+1) on this part.  Found by dumping the serial peripheral's registers at
# an autobauded 9600 and again at 19200: exactly one word moved, +0x1E, 130 -> 63.
# This is only a fallback -- learn_baud_const() reads the divider out of the ECU in front
# of it and recomputes this for that unit, because a different clock setup would put
# every rate change in the wrong place.
BRG_ADDR = 0x20401E     # serial peripheral, baud rate generator, high word (= PDIV)
BAUD_CONST = 1243200.0

# ---------------------------------------------------------------------------
# WHAT THE FLASH CONTROLLER ITSELF SAYS
#
# The monitor decides an operation is finished from the module's busy flag, and that flag
# says only "no longer working" -- it cannot tell an operation that was refused from one
# that ran and achieved nothing.  The reason lives in two status registers, which the stage
# copies into PSRAM at FSTAT before issuing "Reset to Read" (documented to clear exactly
# those bits).  So the host fetches them with an ordinary READ; there is no extra command
# and no reply anywhere changed shape.
#
# Register map: XC2000 System Units User's Manual V1.0 (2007-06) Table 3-7, pp. 3-52..3-62.
# The same table is where waitbusy's FF'FF06 comes from, so the block is not guesswork:
# the busy poll built on it has driven every verified write this tool has done.
MON_BASE = 0xE00020             # stage-1 stores the monitor here; ORG 0020h in the .asm
MON_FSTAT = 0xE00980            # IMB_FSR_OP then IMB_FSR_PROT, latched by the stage
FSTAT_NONE = 0xFFFF             # what the stage writes there before anything has run
IMB_BASE = 0xFFFF00             # the IMB register block, readable with a plain READ
IMB_FSR_OP, IMB_FSR_PROT = IMB_BASE + 0x08, IMB_BASE + 0x0A
IMB_PROCON0 = IMB_BASE + 0x10   # one per flash module, +2 each; bit s = logical sector s
# Only the bits that mean something WENT WRONG are given sentences, and that is the whole
# point: a completed operation with none of them set is the signature of an address with
# nothing behind it, and it has to be reportable as such rather than drowned in narration.
# The other bits are state, not faults, and are printed raw or by report_protection():
#   IMB_FSR_OP   0x01 PROG / 0x02 ERASE -- which kind of operation ran
#                0x04 POWER / 0x08 MAR  -- module startup and read-margin changes
#   IMB_FSR_PROT 0x01 PROIN             -- protection is installed
#                0x04 RPRODIS / 0x08 WPRODIS -- protection temporarily disabled by password
#                0x100 ISBER / 0x400 DSBER   -- single-bit ECC errors, which were CORRECTED
FSR_OP_FAULTS = ((0x10, "SEQUENCE ERROR -- the command sequence was not accepted"),
                 (0x20, "OPERATION ERROR -- an earlier erase/program was cut short by a "
                        "reset"))
FSR_PROT_FAULTS = ((0x02, "PROTECTION INSTALL ERROR -- the security pages did not read "
                          "back consistently"),
                   (0x10, "PROTECTION ERROR -- the operation hit installed write "
                          "protection"),
                   (0x200, "an UNCORRECTABLE ECC error was seen on an instruction fetch"),
                   (0x800, "an UNCORRECTABLE ECC error was seen on a data read"))

# The one address on this family that is documented not to be there.  Physical sector 15 of
# flash module 0 is reserved for the device's own use: XC2000 System Units UM V1.0, Table 3-1
# note 3 -- "The 4 KB sector from C0'F000H to C0'FFFFH is not accessible to the software" --
# and 3.9.2, "In the Flash0 one sector is reserved for device internal purposes".  The same
# manual lists Flash 0 as 252 KB rather than 256 for exactly this reason.
#
# It is named here so the tool can say WHICH region it met instead of only "no memory here",
# but nothing downstream is allowed to trust the name: the run still proves the region is
# unwritable by measurement, because a tool that skips a sector on the strength of a
# hard-coded address is one datasheet erratum away from skipping a real failure.
RESERVED = {0xC0F000: "reserved by the device (XC2000 flash module 0, physical sector 15)"}

# ---------------------------------------------------------------------------
# WHAT PART IS THIS, ASKED OF THE PART
#
# The stages are assembled for ONE memory map.  Flash at 0xC00000, the transmit buffer at
# 0x4080, the status word at 0x4044, the flash controller at 0xFFFF00.  On a different
# derivative those addresses mean different things, and a stage that lands there executes
# with complete confidence against the wrong ones.
#
# The handshake proves nothing about this.  0x00 -> 0xD5 is a property of the whole C166
# mask-ROM family, so an ECU this tool has never seen can answer it perfectly and then
# accept code that ruins it.  What DOES prove it is asking the silicon: these registers are
# the device reporting its own identity, not us inferring it from behaviour, so a check
# built on them cannot mistake a part it has actually been tested against for a stranger.
#
# Measured on the reference unit, an Itelma M74 CAN marked SAK-XC2765X-104F80L:
#     IDMANUF 0x1820   JEDEC 0xC1 (Infineon), section 0x00 (standard microcontroller)
#     IDCHIP  0x3801   CHIPID 0x38, revision step 0x01
#     IDMEM   0x30D0   type 3 (flash), 208 blocks x 4 KB = 832 KB
IDMANUF_ADDR, IDCHIP_ADDR, IDMEM_ADDR = 0x00F07E, 0x00F07C, 0x00F07A
STSTAT_ADDR = 0x00F1E0          # HWCFG in the low byte: which loader the ROM started

KNOWN_CHIPID = 0x38             # the device, from IDCHIP[15:8]
KNOWN_MANUF = 0xC1              # Infineon, from IDMANUF[15:5]
# The REVISION is deliberately not checked.  A different step of the same device has the
# same memory map, and refusing one would turn a silicon respin into a support ticket.


def read_identity(k):
    """IDMANUF, IDCHIP and IDMEM, decoded.  None if they cannot be read.

    Read-only, three 2-byte reads, and they work through the ordinary READ command because
    the ESFR area is memory-mapped at 0x00F000-0x00F1FF (UM Table 3-1)."""
    try:
        vals = {}
        for name, addr in (("manuf", IDMANUF_ADDR), ("chip", IDCHIP_ADDR),
                           ("mem", IDMEM_ADDR)):
            d = k.mon_read(addr, 2)
            vals[name] = d[0] | (d[1] << 8)
    except IOError:
        return None
    return {"manuf": vals["manuf"] >> 5, "chipid": vals["chip"] >> 8,
            "revision": vals["chip"] & 0xFF, "mem_type": vals["mem"] >> 12,
            "flash_bytes": (vals["mem"] & 0xFFF) * 4096, "raw": vals}


# The four module bases this part must have memory at, and the one it must NOT.  Read
# before the first erase, because IDMEM cannot be trusted to answer this on its own.
#
# CHIPID 0x38 is a DIE, not a part number: XC2765X, XC2785X, XC228xM, XC236xA, XE164xM and
# XE167xM all report it and all share this map.  That is fine.  What is not fine is that
# other members of the same family answer the same handshake with a DIFFERENT map, and one
# of them -- XC2768X / XC228xI -- exists in an 832 KB configuration whose fourth module sits
# at 0xD00000 with 0xCC0000 empty.  Same size, same 0xD5, wrong addresses.
#
# IDMEM will not catch that on its own.  The field is documented `rw` -- trimmed -- and the
# XE164xM and XE167xM datasheets print IDMEM = 0x30D0 (832 KB) for parts whose largest
# derivative is 576 KB.  So it may report what the die could hold rather than what this one
# does.  Four read-only frames settle it against the silicon instead of against a claim, and
# they also catch the 576 KB and 448 KB variants of our own die, which have holes where this
# tool assumes flash.
MODULE_BASES = (0xC00000, 0xC40000, 0xC80000, 0xCC0000)

# DO NOT probe 0xD00000 to detect the foreign layout.  It was tried, it refused every write
# on a known-good unit, and the reason was already written down in QUIRKS section 7 before
# the check was built: `9b 1e` is the FLASH CONTROLLER's answer for "my range, no memory
# here", and a segment with no flash controller at all (measured at 0x300000) answers 0x46
# instead.  0xD00000 is outside this controller's decode, so it can never match the pattern
# looks_unmapped() compares against, so the probe reports "there is memory here" on every
# genuine M74 CAN.  Measured on the bench 2026-07-31: a correct ECU -- Infineon, CHIPID 0x38,
# 832 KB, all four modules answering -- was refused with exactly that diagnosis.
#
# The discriminator that DOES work is already here and needs no new address: on the foreign
# layout 0xCC0000 is EMPTY, and 0xCC0000 is inside the controller's range, so it answers the
# controller's own nothing-pattern and the missing-module check below catches it. Ask about
# the memory the map says should be there, not about memory it says should not.
#
# And that replacement is REASONING, not a measurement: it holds if the foreign part answers
# the same no-memory word at 0xCC0000 as at 0xCD0000, which is where `nothing` is learned
# from.  Both sit in the same hole on the layout we have and the argument survives whichever
# word it is, as long as the two agree -- but nobody here owns an XC2768X, so this has not
# been tried on one, and the simulated ECU satisfies the assumption rather than testing it.


def modules_present(k, nothing=None):
    """Which of the four module bases hold memory, asked of the ECU.  List, or None.

    `nothing` is this unit's own answer for an address with no memory behind it; it is read
    here if not supplied, because the whole point is to compare against the part rather than
    against a constant.  Every address probed is inside the flash controller's own range,
    which is what makes that comparison meaningful -- see the note above."""
    try:
        if nothing is None:
            nothing = bytes(k.mon_read(FLASH_BASE + FLASH_SIZE, 8))
        # looks_unmapped() is the predicate this project already tests: one 16-bit word
        # repeated, and that word is what this ECU answers for nothing.  Rolling a fresh
        # one here was a mistake -- "more than one distinct byte" is true of every
        # two-byte repeating pattern, so the first version called every address populated.
        return [b for b in MODULE_BASES
                if not looks_unmapped(bytes(k.mon_read(b, 16)), nothing)]
    except IOError:
        return None


def identity_ok(ident):
    """Is this the part the stages were built for?  (verdict, reasons that failed)"""
    if ident is None:
        return False, ["the identification registers could not be read"]
    bad = []
    if ident["manuf"] != KNOWN_MANUF:
        bad.append("manufacturer 0x%02X, expected 0x%02X" % (ident["manuf"], KNOWN_MANUF))
    if ident["chipid"] != KNOWN_CHIPID:
        bad.append("CHIPID 0x%02X, expected 0x%02X" % (ident["chipid"], KNOWN_CHIPID))
    if ident["flash_bytes"] != FLASH_SIZE:
        bad.append("flash %d KB, expected %d KB"
                   % (ident["flash_bytes"] // 1024, FLASH_SIZE // 1024))
    return not bad, bad


def check_identity(k, force=False):
    """Gate a WRITE on the part being the one the stages were assembled for.

    Reading is never gated: it cannot damage anything, and a read from an unrecognised ECU
    is exactly the report that would let it be supported one day.  Writing is gated,
    because the failure it prevents is not a bad write but a stage running against the
    wrong addresses on hardware nobody here has ever seen.

    `force` proceeds anyway, loudly.  It exists because a refusal that cannot be overridden
    turns a tool into a wall for the one person who genuinely knows more than it does --
    but it prints what it is overriding, so the decision is made with the numbers in view."""
    ident = read_identity(k)
    if ident is not None:
        print("[kline] this part reports: Infineon-style manufacturer 0x%02X, CHIPID 0x%02X "
              "rev 0x%02X, %d KB of %s"
              % (ident["manuf"], ident["chipid"], ident["revision"],
                 ident["flash_bytes"] // 1024,
                 "flash" if ident["mem_type"] == 3 else "memory type %d" % ident["mem_type"]))
    ok, bad = identity_ok(ident)
    # Where the memory actually is, which is a different question from what the part says
    # about itself, and the one that separates this die's layout from XC2768X's.
    mods = modules_present(k)
    if mods is None:
        # The probe could not be taken.  That must NOT read as a pass: the ID registers on
        # their own do not separate this die from XC2768X -- they are identical there, and
        # the layout is the only thing that tells them apart.  Skipping it silently left the
        # earlier `ok` standing and let the write go ahead on the weaker evidence, which is
        # the opposite of what the check is for.  A read that fails moments before a write
        # is also its own bad sign.
        ok = False
        bad.append("the flash layout could not be read, so the one check that separates "
                   "this part from the same-ID 832 KB variant did not run")
    else:
        present = mods
        print("[kline] flash modules answering: %s"
              % (", ".join("0x%06X" % b for b in present) if present else "none"))
        missing = [b for b in MODULE_BASES if b not in present]
        if missing:
            ok = False
            bad.append("no memory at %s, which this tool assumes is flash"
                       % ", ".join("0x%06X" % b for b in missing))
            if 0xCC0000 in missing:
                bad.append("0xCC0000 in particular is empty on the XC2768X/XC228xI map, "
                           "whose fourth module sits at 0xD00000 instead -- same 832 KB, "
                           "same handshake, different addresses")
    event("stage", stage="identity", ok=ok,
          chipid=ident["chipid"] if ident else None,
          flash_bytes=ident["flash_bytes"] if ident else None,
          forced=bool(force and not ok))
    if ok:
        return True
    print("[kline] THIS IS NOT THE ECU THIS TOOL WAS BUILT FOR:")
    for b in bad:
        print("[kline]   %s" % b)
    if not force:
        print("[kline] refusing to write.  The stages uploaded into an ECU are assembled for")
        print("[kline] one memory map; on another part those addresses mean something else,")
        print("[kline] and the handshake this ECU just answered is common to the whole family")
        print("[kline] -- it proves nothing about the map.  Reading is NOT blocked, and a read")
        print("[kline] from an unrecognised unit is exactly what would let it be supported.")
        print("[kline] If you know this part is compatible, pass --force-unknown-ecu.")
        return False
    print("[kline] --force-unknown-ecu given: writing anyway, against the tool's own advice.")
    return True


# ---------------------------------------------------------------------------
# MACHINE-READABLE PROGRESS
#
# A front end must never have to read this tool's prose.  The GUI used to recover the
# progress bar with `re.compile(r"\[(\d+)/(\d+),")` and `"/ 832 KB" in line`, which means
# rewording a print statement silently broke the progress bar -- and, worse, the same habit
# decided SUCCESS by searching the log for "WRITE COMPLETE AND VERIFIED", so a success
# printed by an EARLIER operation could be reported for a later one that failed.  A flasher
# that can say "done, verified" about a write that did not finish has no business shipping.
#
# So with --progress json there are two streams and one rule each:
#     stdout  exactly one JSON object per line, nothing else, ever
#     stderr  the human log, unchanged and still worth reading
#
# The vocabulary is deliberately tiny, because a contract is only worth having if it stays
# still:
#   stage     a coarse milestone: handshake, handoff, calibrated, backup, erasing...
#   progress  op, done, total, unit, and whatever of rate/eta/retries is known
#   problem   something survivable worth surfacing: a retry, an unwritable region
#   result    the outcome of a phase that can pass or fail: read, write, verify.
#             THE VERDICT RULE: an operation succeeded iff at least one result arrived and
#             none of them said ok=false.  Absence of results is not success.
_EVENT_OUT = None       # the real stdout, kept aside once the human log moves to stderr


def event(kind, /, **fields):
    """Emit one event, if a machine is listening.  Cheap and silent when it is not.

    The kind is positional-ONLY, and that is not stylistic: `event("problem",
    kind="unwritable", ...)` is the natural way to write a problem event, and with an
    ordinary parameter that call collides with this function's own signature -- which it
    did, on the one code path that only runs when a region turns out not to be flash."""
    if _EVENT_OUT is None:
        return
    fields["event"] = kind
    try:
        _EVENT_OUT.write(json.dumps(fields, sort_keys=True) + "\n")
        _EVENT_OUT.flush()
    except Exception:
        pass            # a front end that closed its pipe must not take the flash job down


def sums16(data):
    """The two running 16-bit sums the monitor's CHECKSUM command builds."""
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) & 0xFFFF
        s2 = (s2 + s1) & 0xFFFF
    return s1, s2


def cksum16(data):
    """16-bit sum, little-endian.  An 8-bit one let a corrupted page through 1 try in
    256, and a real write retries corrupted payloads hundreds of times."""
    s = sum(data) & 0xFFFF
    return bytes([s & 0xFF, (s >> 8) & 0xFF])


def pdiv_for(baud):
    """Divider for a target baud, and the baud it will really produce."""
    p = max(1, round(BAUD_CONST / baud) - 1)
    return p, BAUD_CONST / (p + 1)
PAGE, SECTOR = 128, 4096        # program granularity / erase-sector granularity
BOOT_END = 0xC02000             # boot sector: refused unless --allow-boot
MON_STATUS = {MON_OK: "ok", MON_REFUSED: "REFUSED by the segment gate",
              MON_TMO: "flash TIMED OUT (never went idle)", MON_READY: "ready",
              MON_CKS: "checksum mismatch (nothing was executed)"}


def build_monitor(delay=BASE_DELAY, segmask=1 << (0xCC - 0xC0)):
    """Assemble the flash monitor for a pacing delay and a writable-segment bitmap.

    segmask bit n enables erase/program on flash segment 0xC0+n; the default enables
    only 0xCC, the EEPROM-emulation area the ECU rewrites by itself anyway."""
    code = bytearray(bytes.fromhex(MONITOR_HEX))
    code[MON_DELAY_OFF] = delay & 0xFF
    code[MON_DELAY_OFF + 1] = (delay >> 8) & 0xFF
    code[MON_SEGMASK_OFF] = segmask & 0xFF
    code[MON_SEGMASK_OFF + 1] = (segmask >> 8) & 0xFF
    if len(code) > STAGE2_LEN:
        raise ValueError("the monitor is %d bytes; stage-1 receives exactly %d"
                         % (len(code), STAGE2_LEN))
    return bytes(code) + b"\x00" * (STAGE2_LEN - len(code))


def check_monitor_blob():
    """Cross-check the embedded bytes against what the assembler produced.

    The .bin is a build artifact and need not be present in a deployed tool; when it
    is, a mismatch means the .asm moved and the blob was not refreshed."""
    try:
        ref = open(MON_ASM_BIN, "rb").read()
    except Exception:
        return None
    # Byte-for-byte, including the two immediates the host patches at run time: those hold
    # the .asm's own defaults here, so a difference means the source moved and the embedded
    # copy did not follow.  Three states, not two -- None above says the .bin is absent,
    # which is not a mismatch and must not be reported as one.
    return bytes.fromhex(MONITOR_HEX) == ref


def segmask_for(addr, length, allow_boot=False):
    """Bitmap of the flash segments a write touches -- the stage's whole permission set.

    Anything outside 0xC0..0xCC is not flash, and the boot sector holds the resident
    CAN bootloader, so it stays out unless explicitly unlocked."""
    if length <= 0:
        sys.exit("[kline] nothing to write (length 0)")
    if addr < BOOT_END and not allow_boot:
        sys.exit("[kline] refusing to touch the boot sector 0x%06X-0x%06X "
                 "(pass --allow-boot only if you mean it)" % (0xC00000, BOOT_END))
    lo, hi = addr >> 16, (addr + length - 1) >> 16
    mask = 0
    for seg in range(lo, hi + 1):
        if not (SEG_LO <= seg <= SEG_HI):
            sys.exit("[kline] 0x%06X..0x%06X leaves flash (segments 0x%02X..0x%02X)"
                     % (addr, addr + length - 1, SEG_LO, SEG_HI))
        mask |= 1 << (seg - 0xC0)
    return mask


def mask_str(mask):
    return ", ".join("0x%02X" % (0xC0 + i) for i in range(16) if mask & (1 << i)) or "(none)"


def find_port(explicit=None):
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    for p in ports:
        if (p.vid, p.pid) == FTDI_VIDPID:
            return p.device
    if len(ports) == 1:
        return ports[0].device
    names = ", ".join(p.device for p in ports) or "none found"
    sys.exit("[kline] pass --port <dev> ; serial ports: " + names)


class SteadyPort(object):
    """A serial port whose hardware settings are written ONCE, with a virtual timeout.

    Assigning pyserial's `timeout` looks free.  It is not, and where it is not is
    host-specific -- which is the shape of the bug this exists to remove.

    pyserial's `timeout` setter calls `_reconfigure_port()` on every assignment while the
    port is open (serialutil.py).  On POSIX that is a `tcsetattr` with unchanged values.  On
    Windows `_reconfigure_port` builds a fresh DCB and calls **SetCommState** as well as
    SetCommTimeouts (serialwin32.py) -- for a USB-serial adapter that means control
    transfers that reprogram the device, baud divider included.

    **Never assign `timeout` per read, per retry, or between transmitting and reading a
    reply.**  That last one is the direction turnaround, so the port gets reprogrammed at the
    precise moment bytes are on the wire, and what comes out looks exactly like physical-layer
    trouble: a mangled or swallowed first byte after every turnaround, a failure rate that
    tracks the SHAPE of the data, rates that appear unusable, an erase that appears to take
    seconds.  All of it host-side, none of it visible on POSIX.

    So the port is given one small poll interval at open and never touched again.  `timeout`
    becomes a value this wrapper honours by looping, so every call site keeps working
    unchanged and none of them reaches the driver.  A test guards the invariant: a regression
    would put the driver-side assignment count in the thousands.
    """
    POLL = 0.03                 # long enough not to spin, short enough to notice an answer

    def __init__(self, ser):
        self.__dict__["_ser"] = ser
        self.__dict__["_timeout"] = ser.timeout
        ser.timeout = self.POLL            # the last time this is ever assigned

    def __getattr__(self, name):           # only reached for names we do not hold
        if name == "timeout":
            return self.__dict__["_timeout"]
        return getattr(self.__dict__["_ser"], name)

    def __setattr__(self, name, value):
        if name == "timeout":
            self.__dict__["_timeout"] = value
        else:
            setattr(self.__dict__["_ser"], name, value)

    def read(self, n=1):
        """Read up to n bytes, honouring the virtual timeout by polling."""
        ser = self.__dict__["_ser"]
        tmo = self.__dict__["_timeout"]
        got = ser.read(n)
        if len(got) >= n or not tmo:
            return got
        end = time.time() + tmo
        while len(got) < n and time.time() < end:
            more = ser.read(n - len(got))
            if more:
                got += more
        return got


class KlineBSL:
    def __init__(self, port, baud=9600, verbose=True):
        self.port, self.baud, self.v = port, baud, verbose
        self.ser = None
        self.retries_used = 0        # how often the link needed a retry, per session
        self.allow_boot = False      # set from --allow-boot; guards erase at the last step
        self.monitor_up = False      # a monitor is listening; the interrupt path checks it

    def open(self):
        try:
            self.ser = SteadyPort(serial.Serial(self.port, self.baud, timeout=0.3,
                                                bytesize=8, parity="N", stopbits=1))
        except Exception as e:
            # A wedged FTDI does not refuse to open -- it opens, and then rejects every
            # attempt to configure it.  MEASURED on this bench: the device node was fresh,
            # the adapter was enumerated and visible to the system, and writing back the
            # EXACT termios attributes just read from it still returned EINVAL.  Nothing in
            # software clears that -- and, measured straight afterwards, neither does
            # replugging: the node came back with a fresh timestamp and EINVAL came back with
            # it.  (This comment used to say unplugging was the cure, copied from the OTHER
            # way this adapter wedges -- the uninterruptible hang -- where it is true.  The
            # error message below has been right about this for longer than the comment was.)
            #
            # Worth catching by hand because of where it surfaces: this happens inside
            # pyserial's open(), so what reaches the user is a bare `termios.error (22,
            # 'Invalid argument')` with no port name and nothing to act on -- and the
            # obvious reading of it, that the baud or the settings are wrong, is wrong.
            # Every rate fails identically.
            if "Invalid argument" in str(e) or getattr(e, "errno", None) == 22:
                raise IOError(
                    "%s opened but refuses every setting (%s).  The adapter is wedged: it "
                    "is still enumerated and its device node is there, but its driver will "
                    "not accept a configuration, at any baud.  Reading the settings still "
                    "works -- `stty -f %s` prints them quite happily -- so do not take that "
                    "as the adapter being healthy; it is SETTING them that fails, and "
                    "`stty -f %s 9600` fails the same way.\n"
                    "[kline] WHAT CLEARS IT: remove power from the adapter COMPLETELY -- "
                    "unplug the USB end AND switch the bench supply off.  Unplugging USB "
                    "alone does not, and that is not a quirk: on a K-line adapter the "
                    "transceiver side is fed from the vehicle +12 V, so pulling the USB "
                    "leaves the device part-powered with its state intact.  Do both, then "
                    "reconnect; an adapter with a power indicator will show it is back.\n"
                    "[kline] If that does not do it, try another USB port, then rebooting, "
                    "then the same adapter on another computer -- the last one separates a "
                    "wedged driver from a damaged adapter.  Nothing on the ECU side is at "
                    "fault here.  See docs/QUIRKS.md."
                    % (self.port, e, self.port, self.port)) from e
            raise
        self.ser.reset_input_buffer(); self.ser.reset_output_buffer()
        if self.v:
            print("[kline] open %s @%d" % (self.port, self.baud))

    def close(self):
        """Let the line go quiet before dropping the port, rather than yanking it.

        A bare `ser.close()` is what this used to be, and it closes the descriptor while the
        ECU may still be mid-answer: a READ hands over up to 4 KB, and an interrupted write
        leaves the monitor COUNTING payload bytes that will never arrive, so the ECU sits
        there driving nothing and the host's driver keeps a part-read transfer.  On this
        bench the adapter has repeatedly ended a day refusing every `tcsetattr` afterwards.
        That has not been traced to this -- it is the user's observation that the adapter's
        ready LED is dark while the port looks free, and no process holds the node -- but
        closing a USB serial device in the middle of a transfer is a bad idea on its own
        terms, and it costs a fraction of a second to stop doing it.

        Bounded on purpose.  Draining is a courtesy, not a duty: if the ECU is silent this
        returns almost at once, and if it is chattering this gives up rather than hanging a
        program the user has already asked to end."""
        if not self.ser:
            return
        try:
            self._drain(quiet=0.05, cap=1.0)     # let any answer in flight finish arriving
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except BaseException:
            # BaseException, not Exception.  KeyboardInterrupt is not an Exception, so a
            # second Ctrl-C landing inside the drain used to skip the close() below and
            # leave the descriptor open -- the un-drained yank this method exists to stop.
            pass
        try:
            self.ser.close()
        finally:
            self.ser = None

    def _tx(self, data):
        self.ser.write(data); self.ser.flush()

    def _rx(self, n=64, tmo=0.25):
        old = self.ser.timeout
        self.ser.timeout = tmo
        d = self.ser.read(n)
        self.ser.timeout = old
        return d

    def power_cycle(self, line, off_ms, settle_ms, invert=False, port=None):
        """Switch the ECU's supply off and on again through the adapter's DTR or RTS.

        MEASURED (--rearm-test, on the bench): there is no software route to a fresh BSL.
        The stub streamed for 0.21 s and went quiet -- the watchdog reset demonstrably
        happened -- and twelve handshakes over the next 3.6 s got back nothing but our own
        echo.  After a watchdog reset control goes to the resident CAN loader, which does
        not speak K-line.  The mask-ROM loader arms on a true power-on reset and on nothing
        else, exactly as the manual says.

        So the only way to stop asking a human to reach for the supply is to put the supply
        under the tool's control.  DTR and RTS are ordinary output pins on a USB serial
        adapter and pyserial drives them directly, so a relay or a high-side switch on the
        +12 V feed is all the hardware this needs.

        `port` is why this takes a port at all.  MEASURED on a K+DCAN cable here: toggling
        its DTR and RTS moved no input line, i.e. those pins are not brought out of the
        moulded connector -- which is normal, since such a cable is a UART plus a K-line
        transceiver and nothing else.  So do NOT expect the flashing cable to double as the
        switch.  Point this at a separate cheap USB-serial dongle whose DTR drives the relay;
        it never carries a byte, it is only a pin you can command.  A logic pin cannot
        switch an ECU's supply current directly in any case -- the relay is not optional.

        UNTESTED HERE: this bench has no such switch, so this path has never driven real
        hardware.  It is opt-in for that reason, and the polarity is yours to declare:
        asserted means POWERED unless --power-invert says otherwise.  Get that backwards and
        the tool cuts power when it means to apply it -- a confusing session, not a damaged
        ECU, since an unpowered ECU simply does not answer.  Note also that opening a serial
        port asserts its handshake lines on most platforms, so the relay will see one brief
        pulse when the dongle is opened, before this code drives it anywhere.
        """
        if line not in ("dtr", "rts"):
            return False
        on, off = (not invert), bool(invert)
        own = port and port != self.port
        dev = serial.Serial(port, 9600, timeout=0.2) if own else self.ser

        def drive(state):
            if line == "dtr":
                dev.dtr = state
            else:
                dev.rts = state

        try:
            if self.v:
                print("[kline] power-cycling the ECU through %s on %s "
                      "(%d ms off, %d ms to settle)"
                      % (line.upper(), port if own else self.port, off_ms, settle_ms))
            drive(off)
            time.sleep(off_ms / 1000.0)
            drive(on)
            time.sleep(settle_ms / 1000.0)
        finally:
            if own:
                dev.close()
        # Whatever the ECU said while it was booting is not ours to parse, and the autobaud
        # must measure OUR first byte rather than something left in the driver's buffer.
        self.ser.reset_input_buffer()
        return True

    def handshake(self):
        """0x00 -> autobaud -> 0xD5 (K-line half-duplex echoes our 0x00: '00 d5').

        A near miss counts, but only by ONE bit.  0xD5 is the very first byte the ECU ever
        transmits in a session, which makes it precisely the byte the K-line turnaround is
        most likely to mangle -- it arrived as 0xB5 once here, and the run was thrown away
        over a single bit even though the BSL was armed and waiting.  Autobaud fires once
        per reset, so that costs a power cycle for nothing.

        TWO bits, because that is the measured distance: 0xD5 ^ 0xB5 = 0x60, which is two.
        Tightening this to one bit -- on the strength of the phrase "a single bit" in this
        very docstring -- rejected the only case the tolerance exists for, while STILL
        accepting 0x55, which is one bit away and is the monitor's own READY byte.  So the
        threshold now matches the measurement, and READY is excluded by name rather than by
        a distance that never excluded it: a session that left the ECU in the monitor would
        otherwise answer READY here, be read as a live BSL, and get a 32-byte stage pushed
        into a command parser.  Nothing can be damaged -- no valid frame forms out of it --
        but a clear "power-cycle me" turns into a baffling failure.

        The tolerance is safe at all because the ACK is only a hint: what actually proves the
        stage got in is the sync run and READY byte at handoff, and that check is exact."""
        self.ser.reset_input_buffer()
        self._tx(b"\x00")
        r = self._rx(8, 0.2)
        ok = BSL_ACK in r
        near = None
        if not ok:
            for b in r:
                if b and b != MON_READY and bin(b ^ BSL_ACK).count("1") <= 2:
                    ok, near = True, b
                    break
        if self.v:
            print("[kline] handshake: TX 00 -> RX %s%s"
                  % (r.hex(" ") or "(none)",
                     "  (0x%02X is a turnaround-mangled 0xD5 -- accepting)" % near
                     if near is not None else ""))
        return ok

    def upload32(self, stub):
        """Send exactly 32 bytes -> BootROM copies to 0xE00000 and jumps there."""
        # raise, not assert: the BootROM takes a fixed 32 bytes and jumps.  Send fewer and it
        # jumps into whatever was already in PSRAM; send more and the surplus is executed as
        # the start of the next thing.  Neither is something to leave to an optimisation flag.
        if len(stub) != 32:
            raise ValueError("the BootROM expects EXACTLY 32 bytes, got %d" % len(stub))
        self._tx(stub)

    def capture(self, stub, secs=3.0, chunk=1024):
        """Upload a 32-byte stub (BootROM loads to 0xE00000 and jumps), then capture
        the line for `secs`. Returns raw bytes: first ~32 are our echoed stub, then
        whatever the running stage transmits."""
        self.ser.reset_input_buffer()
        self.upload32(stub)
        buf = b""
        deadline = time.time() + secs
        while time.time() < deadline:
            buf += self.ser.read(chunk)
        return buf

    def oneshot(self, want, stage2=None, progress_every=32768):
        """Deliver STAGE1 + STAGE2 and stream `want` bytes of flash in ONE pass.

        Returns the raw capture (echo included); call split_oneshot() to locate the
        flash payload.  Nothing here writes flash -- both stages only read it."""
        self.ser.reset_input_buffer()
        self.upload32(STAGE1)
        time.sleep(0.25)                       # let stage-1 reach its PSR poll loop
        self._tx(stage2 or build_stage2())      # 2048 B; stage-1 falls through into it
        need = ONESHOT_START + want
        buf = bytearray()
        self.ser.timeout = 2.0
        silence, nextmark, announced = 0, progress_every, False
        t0 = time.time()
        while len(buf) < need:
            chunk = self.ser.read(min(16384, need - len(buf)))
            if chunk:
                buf += chunk; silence = 0
            else:
                silence += 1
                if silence >= 8:               # ~16 s dead line -> the stage stopped
                    break
            got = max(0, len(buf) - ONESHOT_START)
            if not announced and len(buf) >= ONESHOT_START:
                announced = True                   # report the handoff now, not in 23 min
                _, note = split_oneshot(bytes(buf))
                print("[kline]   handoff: %s" % (note or "PREAMBLE NOT FOUND -- aborting"))
                event("stage", stage="handoff", ok=note is not None,
                      detail=note or "preamble not found")
                if note is None:
                    break
            if got >= nextmark:
                el = time.time() - t0
                rate = got / el if el else 0
                eta = (want - got) / rate if rate else 0
                print("[kline]   %6d / %d KB  (%.0f%%, %.0f B/s, ETA %d:%02d)"
                      % (got // 1024, want // 1024, 100.0 * got / want, rate,
                         int(eta) // 60, int(eta) % 60))
                event("progress", op="read", done=got, total=want, unit="bytes",
                      rate=round(rate, 1), eta=int(eta))
                nextmark += progress_every
        return bytes(buf)

    # --- flash monitor session ---------------------------------------------
    def start_monitor(self, segmask, delay):
        """Deliver stage-1 + the monitor and wait for its sync run and READY byte.

        Same handoff as oneshot(): both stages echo back first (2080 bytes), then the
        monitor announces itself.  Its first transmitted byte takes the K-line
        turnaround glitch, which is what the sync run is for."""
        self.ser.reset_input_buffer()
        self.upload32(STAGE1)
        time.sleep(0.25)                       # let stage-1 reach its PSR poll loop
        img = build_monitor(delay, segmask)
        self._tx(img)
        run = bytes([SYNC]) * (PREAMBLE_LEN - 1)
        buf = bytearray()
        self.ser.timeout = 1.0
        deadline = time.time() + 25.0
        while time.time() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                buf += chunk
            i = buf.find(run, max(0, ONESHOT_ECHO - 16))
            if i >= 0:
                j = i
                while j < len(buf) and buf[j] == SYNC:
                    j += 1
                if j < len(buf):
                    if buf[j] == MON_READY:
                        note = "monitor up (sync at +%d, nominal +%d)" % (i, ONESHOT_ECHO)
                        ok, why = self.verify_monitor_image(img)
                        if not ok:
                            return False, note + " -- but " + why
                        # Recorded so the interrupt path knows whether anything is listening
                        # for a resync; see the KeyboardInterrupt handler in _main().
                        self.monitor_up = True
                        return True, note + ", image verified"
                    return False, "sync found but got 0x%02X where READY should be" % buf[j]
        return False, "no sync run -- stage-1 did not hand off (%d bytes seen)" % len(buf)

    def verify_monitor_image(self, img):
        """Read the delivered stage back out of PSRAM and compare it byte for byte.

        The monitor is uploaded by the mask-ROM loader with no checksum and no retry: the
        BootROM takes 32 bytes, stage-1 takes 2048 more, and neither checks anything.  Up
        to here the only evidence that the right program landed is that it greets us -- and
        a greeting is a weak test.  The two bytes that matter most are an immediate buried
        in the middle of the blob:

            AND R13,#SEGMASK        opcode 66 FD, immediate at image offset 0x2BC

        That immediate IS the write gate.  Every claim this tool makes about what a session
        can physically reach -- a read-only probe that "cannot erase", a write confined to
        the segments being written -- rests on those two bytes having arrived intact.  One
        flipped bit there yields a monitor that starts normally, answers normally, and
        allows erases the host would never ask for.  Nothing downstream would notice,
        because everything downstream trusts the gate.

        So read it back.  Reads are ungated and non-destructive, the stage's own scratch
        (BUF 0x900, FSTAT 0x980) lives past the end of its image so the code region never
        changes under us, and at the rate the handoff runs at the whole 2 KB costs about
        3.4 s -- 2048 bytes at the ~610 B/s the stage's own transmit pacing gives at 9600
        -- against a write measured in minutes.

        One limit, because the difference belongs in the sentence: the read-back is
        performed BY the program under suspicion, through its own read loop.  Any
        corruption touching that loop breaks the read rather than passing it, so this
        catches the overwhelming majority of cases -- but it is a self-report, not an
        independent measurement, and it cannot be made into one without a second channel
        this protocol does not have."""
        try:
            back = self.mon_read(MON_BASE, len(img))
        except IOError as e:
            return False, "the stage could not be read back to verify it (%s)" % e
        if len(back) != len(img):
            return False, ("only %d of %d bytes of the stage read back -- cannot confirm "
                           "the write gate is what we compiled" % (len(back), len(img)))
        if back == img:
            return True, "stage verified byte for byte"
        bad = [i for i in range(len(img)) if img[i] != back[i]]
        where = ("including the write-gate immediate at 0x%X"
                 % MON_SEGMASK_OFF if MON_SEGMASK_OFF in bad
                 or MON_SEGMASK_OFF + 1 in bad else "first at 0x%X" % bad[0])
        return False, ("the stage in the ECU is NOT what was sent: %d of %d bytes differ, %s. "
                       "Refusing to use it -- the write gate cannot be trusted."
                       % (len(bad), len(img), where))

    def _frame(self, cmd, addr, count):
        seg, off = (addr >> 16) & 0xFF, addr & 0xFFFF
        return bytes([cmd, off & 0xFF, (off >> 8) & 0xFF, seg,
                      count & 0xFF, (count >> 8) & 0xFF])

    def _settle(self):
        """Let K climb back to recessive before we drive it (see SETTLE_S)."""
        time.sleep(SETTLE_S)

    def _swallow(self, n, tmo):
        """K-line is half-duplex: everything WE transmit comes straight back at us."""
        got = b""
        deadline = time.time() + tmo
        while len(got) < n and time.time() < deadline:
            self.ser.timeout = 0.5
            got += self.ser.read(n - len(got))
        return len(got) == n

    def wire_frame(self, cmd, addr, count):
        """Markers, 6-byte body, 16-bit checksum -- always 10 bytes, whatever the command says.

        The two leading markers do for the host->ECU direction what the reply lead does
        for the other one: the turnaround mangles at most the first byte, and the monitor
        discards anything that is not 0xA5 while it waits for a frame.  The FIXED length
        is what stops a mangled command byte from stranding a payload on the wire."""
        body = self._frame(cmd, addr, count)
        return bytes([SYNC]) * MON_MARKERS + body + cksum16(body)

    def _read_status(self, tmo, what, required=True):
        """Read the two lead bytes and the status byte behind them.

        Returns None instead of raising when nothing arrives and `required` is False,
        so the caller can try to unstick the monitor first."""
        self.ser.timeout = tmo
        lead = b""
        deadline = time.time() + tmo
        while len(lead) < MON_LEAD + 1 and time.time() < deadline:
            lead += self.ser.read(MON_LEAD + 1 - len(lead))
        if len(lead) < MON_LEAD + 1:
            if not required:
                return None
            raise IOError("no status byte (%s, got %s)" % (what, lead.hex(" ") or "nothing"))
        if lead[MON_LEAD - 1] != SYNC:
            # Bytes arrived, but not the ones a reply starts with, so the host is reading
            # the middle of something rather than the front of it.  That is true about the
            # BYTES and says nothing about the SESSION: the monitor is almost certainly fine
            # and still answering.  Do not end a run here -- a whole-image write that gives
            # up at sector 90 of 206 discards everything it has done.
            #
            # Same treatment as a status that never came (mon_cmd): report nothing read and
            # let the caller drain and ask again.  `required` separates the two cases; a
            # caller that cannot recover still gets the exception.
            if not required:
                return None
            raise IOError("reply lead-in %s is not %02X (%s) -- the host lost its place in "
                          "the stream" % (lead[:MON_LEAD].hex(" "), SYNC, what))
        return lead[MON_LEAD]

    def _count_retry(self):
        """One retry, and only when it says something about the LINK.

        Everything that reports link quality reads this counter, and everything that
        decides to slow a write down does too, so what goes in has to be traffic the tool
        expected to succeed.  While the rate search is running it is deliberately probing
        rates that may be wrong -- those failures are the search working, not the line
        failing, and counting them prints "1 retry" on a bench that measured zero errors in
        both directions on the very next line.  The same reasoning excludes anything else
        the tool does knowing it may fail."""
        if getattr(self, "_sweeping", False):
            return
        self.retries_used += 1

    def _drain(self, quiet=0.08, cap=4.0):
        """Read until the line has been silent for `quiet` seconds, or `cap` runs out.

        A stalled exchange is not an empty line: the ECU may still be part-way through
        handing over a 4 KB READ answer.  Anything that tries to interpret the next few
        bytes before that backlog is gone is reading payload as protocol."""
        seen = 0
        end = time.time() + cap
        self.ser.timeout = quiet
        while time.time() < end:
            got = self.ser.read(4096)
            if not got:
                break
            seen += len(got)
        return seen

    def _resync(self, what, limit=60):
        """Unstick a monitor that is waiting for bytes that never arrived.

        The failure is real and was seen live: a page payload is framed by COUNT, not by a
        marker, so one byte short leaves the monitor counting forever and the line silent.
        Feeding it 0xA5 finishes whatever it was counting; the checksum then fails, which is
        exactly what we want -- a short payload never reaches flash.  0xA5 is also the frame
        marker, so surplus ones are skipped if the monitor was not stuck at all.  Confirmed
        live: one filler byte -> 0x43, then a normal PING answered READY.

        What this docstring USED TO SAY, and no longer does, is that the turnaround itself
        swallows bytes.  That was the reading at the time; the cause was later traced to the
        host reconfiguring the serial port between transmitting and reading the reply (see
        SteadyPort), which drops bytes on Windows and looks precisely like a line defect.
        Since that was fixed the effect has not been reproduced on this bench.

        The recovery stays, and so does its test.  A payload framed by count is stuck by ANY
        lost byte, whatever loses it, and a tool that can only recover from causes it has
        already identified is not much of a recovery.  But it is a safety net now, not
        evidence about K-line physics, and the distinction is the whole point of QUIRKS."""
        # Drain first, and this is not tidiness.  A READ whose status was mangled leaves the
        # ECU streaming the REST of its answer -- up to 4096 bytes -- so a loop that feeds
        # one marker and reads four bytes finds payload where a status should be, for as many
        # attempts as it cares to make.  Interpreting that as "the monitor is gone" is how a
        # recoverable hiccup becomes a lost session.
        self._drain()
        for i in range(limit):
            self._settle()
            self._tx(bytes([SYNC]))
            got = b""
            deadline = time.time() + 0.6
            self.ser.timeout = 0.3
            while len(got) < 1 + MON_LEAD + 1 and time.time() < deadline:
                got += self.ser.read(1 + MON_LEAD + 1 - len(got))
            if len(got) >= 1 + MON_LEAD + 1 and got[MON_LEAD] == SYNC:
                self._count_retry()
                if self.v:
                    print("[kline]   %s: line had swallowed %d byte(s); resynced (status "
                          "0x%02X)" % (what, i + 1, got[MON_LEAD + 1]))
                return got[MON_LEAD + 1]
        return None

    # How long to wait for the status of an operation that may be slow BY PHYSICS rather
    # than slow because the line stalled.  A cap, not a delay: when things work it is never
    # reached.
    #
    # It was introduced because every erase was timing out on the stall window and being
    # driven through the resync path -- which worked, but logged "the line swallowed N
    # byte(s)" on a clean bench and counted a fault per sector.  The reasoning at the time
    # was that a sector erase takes about 5 s.  MEASURED once the host defect was fixed: a
    # sector erase takes **20 ms**.  The 5 s was the resync path's own cost being mistaken
    # for flash physics -- the measurement measured the workaround.
    #
    # Kept anyway, generously, because it costs nothing when nothing is wrong and it is the
    # difference between tolerating a slow operation and mangling it: if a reply lands
    # between two filler bytes the resync reads it as a lead byte, discards it, and the
    # now-idle monitor eats the rest of the filler as markers -- reported as a lost link on
    # an erase that in fact succeeded.
    SLOW_LOOK = 8.0

    def mon_cmd(self, cmd, addr=0, count=0, tmo=20.0, retries=3, slow=False):
        """Send one command, swallow its echo, return the monitor's status byte.

        A frame the monitor could not checksum is answered ST_CKS and NOT executed, so
        resending it is safe -- that is exactly what the checksum buys.  Every other
        status is returned as-is; deciding what a REFUSED means is the caller's job."""
        frame = self.wire_frame(cmd, addr, count)
        for _ in range(retries):
            self.ser.reset_input_buffer()
            self._settle()
            self._tx(frame)
            # the monitor paces every byte it echoes, so a 137-byte frame takes a while
            if not self._swallow(len(frame), tmo=2.0 + len(frame) * 12.0 / self.baud):
                raise IOError("command frame was not echoed -- the monitor is not listening")
            what = "cmd %d @0x%06X" % (cmd, addr)
            look = min(tmo, self.SLOW_LOOK if slow else STATUS_LOOK)
            st = self._read_status(look, what, required=False)
            if st is None:                          # a frame byte was swallowed
                st = self._resync(what)
            if st is None:
                # Silence after a drain and a filler run means the monitor is IDLE, not
                # stuck: a stuck one is counting payload bytes and answers filler with a
                # checksum error, while an idle one waits for a whole frame and ignores
                # markers by design.  So this is the easy case -- the host missed a status,
                # the ECU is fine, resend the command.  Raising here instead costs a
                # pre-flight backup and the write that depends on it.
                self._count_retry()
                if self.v:
                    print("[kline]   %s: no status; line drained, resending the command"
                          % what)
                continue
            if st not in MON_STATUS_BYTES:
                # A byte arrived where a status belongs, and it is not one of the five the
                # monitor can send.  It is noise, not an answer -- treat it exactly like a
                # checksum failure and ask again.  Returning it instead hands the caller a
                # verdict the ECU never gave, and callers act on that: a READ turns it into
                # "the ECU refused", ends the run and, mid-backup, refuses the write behind
                # it.  Never let an unrecognised byte out of this function.
                self._count_retry()
                if self.v:
                    print("[kline]   reply 0x%02X is not a status byte, resending (%s)"
                          % (st, what))
                self._drain()
                continue
            if st != MON_CKS:
                return st
            self._count_retry()
            if self.v:
                print("[kline]   frame corrupted in flight, resending (cmd %d @0x%06X)"
                      % (cmd, addr))
        # Out of attempts at this rate.  Before giving up on the session, give up on the
        # SPEED: a rung down costs minutes, and the alternative costs the whole run.
        lower = [] if getattr(self, "_sweeping", False) else \
            [b for b in getattr(self, "ladder", ()) if b < self.baud]
        if lower and self.mon_set_baud(lower[0]):
            if self.v:
                print("[kline]   cmd %d @0x%06X failed %d times at the old rate -- stepping "
                      "down to %d and trying again" % (cmd, addr, retries, self.baud))
            return self.mon_cmd(cmd, addr, count, tmo=tmo, retries=retries, slow=slow)
        raise IOError("cmd %d @0x%06X did not get through %d times running, and there is no "
                      "slower rate left to try" % (cmd, addr, retries))

    def mon_ping(self):
        return self.mon_cmd(CMD_PING, tmo=5.0) == MON_READY

    def mon_read(self, addr, n, progress=False, op="read"):
        """Read flash through the monitor.  Ungated and non-destructive.

        `op` labels the reported progress, because the same read serves three very different
        jobs -- the pre-flight backup, a final verify, and a plain dump -- and a progress bar
        that cannot tell them apart tells the user the wrong thing three times."""
        out = bytearray()
        t0 = time.time()
        while n > 0:
            rate = 610.0 * self.baud / BASE_BAUD     # inside the loop: the baud can change
            room = 0x10000 - (addr & 0xFFFF)        # the stage's offset is 16-bit
            chunk = min(n, 0x1000, room)
            st = self.mon_cmd(CMD_READ, addr, chunk, tmo=10.0)
            if st != MON_OK:
                raise IOError("READ @0x%06X: %s" % (addr, MON_STATUS.get(st, "0x%02X" % st)))
            got = b""
            deadline = time.time() + 10.0 + chunk / rate * 2
            while len(got) < chunk and time.time() < deadline:
                self.ser.timeout = 2.0
                got += self.ser.read(chunk - len(got))
            if len(got) != chunk:
                # Drain, drop a rung, ask for the same chunk again.  Do not end the session:
                # a short answer says the rate is wrong, not that the ECU is gone.  Reads are
                # not immune to a bad rate just because a mangled READ command gets resent --
                # a mangled READ also leaves a part-delivered ANSWER behind, and enough of
                # those in a row lose the stream.
                self._drain()
                lower = [b for b in getattr(self, "ladder", ()) if b < self.baud]
                if lower and self.mon_set_baud(lower[0]):
                    if self.v:
                        print("[kline]   READ @0x%06X came back short (%d of %d) -- stepping "
                              "down to %d and retrying" % (addr, len(got), chunk, self.baud))
                    continue
                raise IOError("READ @0x%06X: got %d of %d bytes" % (addr, len(got), chunk))
            out += got
            addr += chunk
            n -= chunk
            if progress and (len(out) % 0x10000 == 0 or n == 0):
                el = time.time() - t0
                eta = n / (len(out) / el) if el and len(out) else 0
                print("[kline]     %6d / %d KB  (%.0f%%, ETA %d:%02d)"
                      % (len(out) // 1024, (len(out) + n) // 1024,
                         100.0 * len(out) / (len(out) + n),
                         int(eta) // 60, int(eta) % 60))
                event("progress", op=op, done=len(out), total=len(out) + n,
                      unit="bytes", eta=int(eta))
        return bytes(out)

    def mon_erase(self, addr, sector=False):
        gran = SECTOR if sector else PAGE
        # raise, not assert.  `python -O` strips assertions, and this one is the last thing
        # between a mis-computed address and an erase of the wrong 4 KB of somebody's ECU.
        # A safety invariant that a command-line flag can remove is not a safety invariant.
        if addr % gran:
            raise ValueError("erase address 0x%06X is not %d-byte aligned" % (addr, gran))
        # Boot sector, host side, at the last gate before the wire.  The compiled-in stage
        # mask is 64 KB-granular, so any write touching segment 0xC0 unlocks 0xC00000 too --
        # the mask cannot express "this segment, above this offset".  What actually keeps
        # erases out of the boot sector is therefore the host's addressing, and an invariant
        # that important should be asserted rather than left to emerge from a loop bound.
        if addr < BOOT_END and not self.allow_boot:
            raise IOError("refusing to erase 0x%06X: inside the boot sector" % addr)
        return self.mon_cmd(CMD_ERASE_SECTOR if sector else CMD_ERASE_PAGE,
                            addr, tmo=45.0, slow=True)   # erase is slow by physics

    def flash_status(self):
        """What the flash controller said about the LAST erase or program it ran.

        Returns (IMB_FSR_OP, IMB_FSR_PROT), or None if nothing has run yet or the stage is
        an older build that does not latch them.  Diagnostic only: every caller treats a
        None as "no extra information" and carries on with what it measured, because this
        is evidence ABOUT a failure, never the thing that decides there was one."""
        try:
            d = self.mon_read(MON_FSTAT, 4)
        except IOError:
            return None
        op, prot = d[0] | (d[1] << 8), d[2] | (d[3] << 8)
        if op == FSTAT_NONE or prot == FSTAT_NONE:
            return None
        return op, prot

    def protection_state(self):
        """Read the protection configuration out of the ECU, before writing anything.

        PROIN says whether any protection is installed at all; the PROCON registers say
        which logical sectors would be locked if it were.  Both are read-only and cost
        three 2-byte reads, which buys the difference between a write that fails at some
        sector with no explanation and a run that says up front what it is up against.
        Returns (prot, [procon0, procon1, procon2]) or None if the reads fail."""
        try:
            d = self.mon_read(IMB_FSR_PROT, 2)
            prot = d[0] | (d[1] << 8)
            procon = []
            for i in range(3):
                p = self.mon_read(IMB_PROCON0 + 2 * i, 2)
                procon.append(p[0] | (p[1] << 8))
        except IOError:
            return None
        return prot, procon

    def learn_baud_const(self):
        """Derive the rate constant from THIS ECU instead of trusting a measured one.

        The divider register holds whatever the ROM worked out for the speed we are
        already talking at, so `baud x (PDIV+1)` can simply be read back -- and it is
        this unit's own clock, not the one this constant happened to be measured on.
        Another board revision or a different PLL setting would otherwise send every
        subsequent rate change to the wrong place.  Falls back quietly if the read fails;
        the shipped default is right for the unit this was developed on."""
        try:
            d = self.mon_read(BRG_ADDR, 2)
        except IOError:
            return BAUD_CONST
        pdiv = d[0] | (d[1] << 8)
        if not 1 <= pdiv <= 0x7FF:              # not a plausible divider -- keep the default
            return BAUD_CONST
        const = float(self.baud) * (pdiv + 1)
        # The divider is an INTEGER, so this constant is quantised by however far the ROM's
        # measurement of our 9600 had to be rounded: one count here is 1/(pdiv+1), about
        # 0.8 % on this part, and the same ECU has returned both 130 and 131 minutes apart.
        # Everything derived from it inherits that, which is fine at 19200 and decides
        # whether 115200 works at all.  Keep the neighbours so the host can try them.
        self.const_candidates = [float(self.baud) * (pdiv + 1 + d) for d in (0, -1, +1)]
        if self.v:
            print("[kline] rate constant from this ECU: %d x %d = %.0f (default %.0f)"
                  % (self.baud, pdiv + 1, const, BAUD_CONST))
        globals()["BAUD_CONST"] = const
        return const

    def mon_set_baud(self, baud):
        """Retune both ends mid-session, past what the BSL autobaud can reach.

        The monitor acknowledges at the old rate and only then moves.  The ack is a hint,
        not proof: it travels over the very link we are unhappy enough with to be changing
        rates, and MEASURED, a switch issued over an already-lossy 57600 lost its ack --
        at which point treating that as failure killed a session where the ECU had in fact
        switched perfectly well.  So afterwards we simply ASK, at both rates, and believe
        whichever one answers."""
        pdiv, real = pdiv_for(baud)
        old = self.baud
        if self.v:
            print("[kline] switching to %d baud (PDIV %d -> really %.0f, %+.1f%%)"
                  % (baud, pdiv, real, 100.0 * (real - baud) / baud))
        # offset field = the stage's new TX pacing, count field = the divider.
        # retries=1 on purpose: a resend would leave at the OLD rate while the ECU has
        # already moved to the new one, turning a lost ack into pure noise.
        try:
            self.mon_cmd(CMD_BAUD, pacing_delay(baud), pdiv, tmo=3.0, retries=1)
        except IOError:
            pass                    # says nothing about where the ECU ended up
        for candidate, moved in ((baud, True), (old, False)):
            self.baud = candidate       # stays NOMINAL: the ladder compares against it
            hit = self._find_host_rate(self._host_candidates(pdiv_for(candidate)[0]),
                                       allow_unproven=False)
            if hit:
                if self.v:
                    print("[kline]   %s %d baud (port at %d)"
                          % (u"confirmed at" if moved else u"did not take, still at",
                             candidate, hit))
                return moved
        # Neither rate could be PROVEN.  That is not the same as the change having failed,
        # and it must not end the session: on a line dirty enough that a 16 KB probe cannot
        # complete, the probe fails at whatever rate the ECU is really at, so refusing to
        # continue would abandon a write that the per-frame retries and the per-sector
        # verify would have carried through anyway.  The ECU acknowledged the rate change,
        # so the new rate is the better guess -- take it, and say plainly that it is a guess
        # rather than printing the word "confirmed" over nothing.
        self.baud = baud
        self._find_host_rate(self._host_candidates(pdiv), allow_unproven=True)
        if self.v:
            print("[kline]   could NOT confirm the rate change at either %d or %d baud; "
                  "assuming it took" % (baud, old))
            print("[kline]   (the ECU acked the command; every frame is retried and every "
                  "sector verified, so an error here shows up as a retry, not as bad data)")
        # And say it in the stream, not only in the prose.  "No silent caps, no silent
        # skips" applies to a rate the code itself calls a guess: a front end that shows a
        # green line over a session running at an unconfirmed rate is doing exactly what
        # this project exists to replace.  Guarded by nothing -- self.v controls the human
        # log, and a machine consumer is not reading that.
        event("problem", kind="rate_unconfirmed", baud=baud, previous=old,
              detail="the rate change could not be confirmed at either rate; proceeding "
                     "on the arithmetic")
        return True

    def _host_candidates(self, pdiv):
        """The rates the ECU could actually be running at, given one integer of doubt.

        Not a blind sweep.  The ECU's rate is CONST/(pdiv+1) where CONST came from the
        ROM's own divider, and the only real uncertainty is that that divider is an integer
        -- so enumerate it: the neighbours are exactly one count either way, about 0.8 %
        apart, and one of the three is right.  A percentage sweep would have to guess both
        the spacing and the width; this knows both.

        The small offsets after them cover the adapter's own divider quantisation, which is
        a different and much smaller error."""
        rates = []
        for const in getattr(self, "const_candidates", None) or [BAUD_CONST]:
            rates.append(round(const / (pdiv + 1)))
        for r in list(rates):
            for parts in (-3, +3):
                rates.append(round(r * (1.0 + parts / 1000.0)))
        seen, out = set(), []
        for r in rates:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def _find_host_rate(self, want, allow_unproven=True):
        """Set the port to whatever rate the ECU is really answering at, and prove it.

        `allow_unproven` chooses what "no candidate answered" means, and the two callers
        need opposite answers.  Opening a session, a rate that could not be proven still
        beats no session at all, so the arithmetic guess is taken and labelled.  Asking
        whether a rate CHANGE took effect, the same fallback would be a lie: it would report
        the new rate as confirmed without a single byte having come back at it, leave the
        host at a rate the ECU may not have moved to, and make the caller's fallback path
        unreachable.  That caller passes False and gets 0, then decides for itself.

        Two things make the arithmetic alone insufficient, and both are measured:

        The nominal rate is not the rate.  The ECU's divider is an integer, so a requested
        38400 lands on 38850.  Leaving the port at the round number wears that 1.2 %, and a
        UART resynchronises only on the start bit and then counts blind, so the error
        accumulates across the byte -- constant data survives any drift, alternating data
        does not.

        And the arithmetic itself is only as good as one measurement made by the ROM.  The
        rate constant comes from the divider the boot loader chose while autobauding our
        9600, and that divider is an integer too: MEASURED on the same ECU minutes apart, it
        came back 130 and then 131, which moves everything derived from it by 0.8 %.  At
        115200 that was the difference between a 32 KB read arriving byte-perfect and the
        stream tearing apart -- same hardware, same code, different session.

        So do not trust the estimate: sweep a few tenths of a percent around it and keep the
        first rate that answers a PING.  A tiny autobaud on the host's side, which is the one
        end that can afford to search.
        """
        self._sweeping = True               # keep mon_cmd from changing rates under us
        best, best_score = 0, None
        try:
            for rate in want:
                self.ser.baudrate = rate
                self.ser.reset_input_buffer()
                time.sleep(0.05)
                score = self._rate_score()
                if score is None:
                    continue
                if score == 0:              # clean: no reason to keep looking
                    return rate
                # Passable but not clean.  Keep it and carry on -- taking the FIRST rate that
                # merely works picks one sitting on the edge of the tolerance, which survives
                # a probe and then fails somewhere in the middle of an 832 KB transfer.
                if best_score is None or score < best_score:
                    best, best_score = rate, score
        finally:
            self._sweeping = False
        if not best and want and allow_unproven:
            # Nothing scored.  On a genuinely dirty line every candidate fails the probe,
            # and refusing to pick one ends the session -- which is worse than running at a
            # rate that could not be proven, because the transport retries and the per-sector
            # verify still has to pass.  Take the arithmetic answer and say it is unproven.
            best = want[0]
            if self.v:
                print("[kline]   could not verify any rate; using %d, the computed one"
                      % best)
            event("problem", kind="rate_unverified", baud=best,
                  detail="no candidate rate could be proven; using the computed one")
        elif best and self.v and best_score:
            print("[kline]   no clean rate found; using %d, the least lossy of %d tried"
                  % (best, len(want)))
        if best:
            self.ser.baudrate = best
            self.ser.reset_input_buffer()
        return best

    def _rate_score(self, probe=0x4000, frames=48):
        """How well does this rate carry real traffic?  0 is perfect, None is unusable.

        Two things are proven here, and neither can be skipped.

        **A stream, not a frame.**  A PING is ten bytes with a one-byte answer, and a rate a
        fraction of a percent off passes it happily and then tears apart on the first bulk
        read.  512 bytes is not enough either.  So ask for one full READ chunk -- the size
        the session actually uses -- and require every byte of it back.

        **And the SPEED it arrives at.**  This is the discriminator that matters and the one
        that took longest to find.  A rate half a percent off still delivers the chunk --
        every byte, no error reported -- because each individual failure is retried
        underneath.  What it cannot hide is the cost: measured here, a matched rate moved
        6973 B/s and a mismatched one 1400 B/s, while both "passed".  Frame error counts
        separate them by a few percent; throughput separates them fivefold.  So require the
        chunk to arrive at a sane fraction of the line rate, and treat anything slower as a
        rate that does not fit however clean it looks.
        """
        bad = 0
        t0 = time.time()
        got_total = 0
        # Several chunks, not one.  A single 4 KB chunk goes through cleanly often enough on
        # a rate that does not fit -- measured, a whole backup at such a rate still delivers
        # every byte, just five times slower -- so one sample decides nothing.
        # Read from ABOVE the boot sector, never from 0xC00000.  Reading damages nothing,
        # but the frame that carries the request does not: cksum16 is a plain 16-bit sum
        # over six bytes, so two errors that cancel pass it, and calibrate_link already
        # names that threat -- "one such pair could turn a PING into an ERASE the gate would
        # allow, before the backup has even been taken" -- and answers it by aiming PING at
        # a segment the mask does not cover.  The rate search runs BEFORE preflight_backup,
        # so the same reasoning applies here.  It cannot be answered the same way (a rate
        # test has to move real quantities of data, and the flash is what there is 16 KB
        # of), but it can be kept off the boot sector, which is the one region with no copy
        # anywhere: --flash never rewrites it, so a backup taken afterwards would faithfully
        # record it as erased.
        #
        # It does NOT make everything above BOOT_END recoverable, and saying so would be
        # too broad.  For a whole-image --flash it does: the probe window is inside the
        # payload about to be written.  For a narrow --write, or --write-selftest, the mask
        # still covers segment 0xC0 while the probe window sits outside both the payload
        # and (for the self-test) any backup at all.  The residual is small -- it needs two
        # cancelling errors in one six-byte frame -- but it is not zero, and the honest
        # bound is "off the one region with no copy", not "harmless".
        for off in range(0, probe, 0x1000):
            want = min(0x1000, probe - off)
            try:
                if self.mon_cmd(CMD_READ, BOOT_END + off, want,
                                tmo=5.0, retries=1) != MON_OK:
                    return None
            except IOError:
                return None
            got = b""
            deadline = time.time() + 5.0
            while len(got) < want and time.time() < deadline:
                chunk = self.ser.read(want - len(got))
                if not chunk:
                    break
                got += chunk
            if len(got) != want:
                self._drain()
                return None
            got_total += len(got)
        elapsed = time.time() - t0
        self._drain()
        if got_total != probe:
            return None
        # 10 bits per byte on the wire; anything under half of that is not this rate working
        # badly, it is a rate that does not fit and is being carried by retries.
        if elapsed <= 0 or probe / elapsed < (self.ser.baudrate / 10.0) * 0.5:
            return None
        for _ in range(frames):
            try:
                if self.mon_cmd(CMD_PING, 0xA5A5, 0x5A5A, tmo=2.0, retries=1) != MON_READY:
                    bad += 1
            except IOError:
                bad += 1
                self._drain()
        return bad

    def mon_checksum(self, addr, n):
        """Ask the ECU to add a region up and send four bytes instead of all of it.

        A byte-exact read-back of every sector is a quarter of a whole-image write spent
        re-fetching data we already have.  Two accumulators rather than one: the second
        depends on each byte's position, so shifts and swaps are caught as well as value
        changes.  The failures actually seen on this bench -- a sector that did not erase,
        a page that landed corrupt -- move both sums by a mile."""
        st = self.mon_cmd(CMD_CKSUM, addr, n, tmo=20.0)
        if st != MON_OK:
            raise IOError("CHECKSUM @0x%06X: %s"
                          % (addr, MON_STATUS.get(st, "0x%02X" % st)))
        got = b""
        deadline = time.time() + 15.0
        while len(got) < 4 and time.time() < deadline:
            self.ser.timeout = 2.0
            got += self.ser.read(4 - len(got))
        if len(got) != 4:
            raise IOError("CHECKSUM @0x%06X: got %d of 4 bytes" % (addr, len(got)))
        return (got[0] | (got[1] << 8), got[2] | (got[3] << 8))

    def mon_program(self, addr, page, retries=8):
        """Program one page, in two steps: ask, then send the bytes it asked for.

        The monitor answers ST_OK only once it has passed the gate and is waiting for
        exactly 132 bytes (two lead bytes, the 128-byte page, a 16-bit checksum), so a
        refused or corrupted command never leaves a page's worth of loose data on the wire
        -- loose data that could otherwise be re-read as a command frame and executed."""
        # raise, not assert -- see mon_erase.  A short page would leave the monitor counting
        # and an unaligned one would program across a page boundary.
        if len(page) != PAGE:
            raise ValueError("program takes exactly one %d-byte page, got %d"
                             % (PAGE, len(page)))
        if addr % PAGE:
            raise ValueError("program address 0x%06X is not page-aligned" % addr)
        if addr < BOOT_END and not self.allow_boot:
            raise IOError("refusing to program 0x%06X: inside the boot sector" % addr)
        what = "page @0x%06X" % addr
        for _ in range(retries):
            st = self.mon_cmd(CMD_PROG, addr, tmo=20.0)
            if st != MON_OK:
                return st                      # refused: the page was never transmitted
            # Two lead bytes, the page, a 16-bit checksum.  The lead is COUNTED here, not
            # searched for like a command frame's marker run: page data may legitimately be
            # 0xA5, so a search has nothing to anchor on.  That is safe only because the
            # monitor has already answered ST_OK and is therefore owed exactly these 132
            # bytes and nothing else -- which is the reason PROGRAM asks first and sends
            # second.  If a lead byte is swallowed the count comes up short, the monitor
            # goes quiet, and _resync() below feeds it filler until the checksum fails.
            body = bytes([SYNC]) * MON_LEAD + bytes(page) + cksum16(page)
            self._settle()
            self._tx(body)
            if not self._swallow(len(body), tmo=2.0 + len(body) * 12.0 / self.baud):
                raise IOError("page payload was not echoed (%s)" % what)
            # SLOW_LOOK, for the same reason erase uses it: this reply arrives only after
            # the flash has finished programming the page, which is milliseconds of physics
            # rather than a line that stalled.  Treating it as a stall manufactures phantom
            # "the line swallowed a byte" reports on a perfectly clean bench.
            st = self._read_status(self.SLOW_LOOK, what, required=False)
            if st is None:                          # a payload byte was swallowed
                st = self._resync(what)
                if st is None:
                    # Deliberately no longer says "the monitor is gone".  Measured: it
                    # usually is not -- the stage answered a PING at the same baud straight
                    # after a session had been declared lost.  Claiming the ECU died sends
                    # people to the power switch and, mid-write, discards the work so far.
                    raise IOError("%s: no status even after draining the line and resyncing "
                                  "-- the host has lost its place in the stream.  The stage "
                                  "is probably still running; retry, and power-cycle only if "
                                  "that does not help" % what)
            if st != MON_CKS:
                return st
            self._count_retry()
            if self.v:
                print("[kline]   page corrupted in flight, resending (%s)" % what)
            # Let anything still in flight drain before trying again: a retry that
            # starts on top of a half-finished exchange just fails the same way, which
            # is what a page failing three times running looked like.
            self.ser.timeout = 0.2
            while self.ser.read(256):
                pass
        raise IOError("%s was corrupted %d times running -- the link is too dirty to "
                      "write over" % (what, retries))


def split_oneshot(buf):
    """Strip the echo of both stages plus the sync preamble; return (flash, note).

    The echo length is exact -- the bench run confirmed all 2080 echoed bytes come
    back byte-for-byte -- so the payload offset is deterministic.  We still verify
    the preamble, tolerating a glitch in its first byte (that is what it is for),
    and fall back to scanning for the sync run if the fixed offset does not line up."""
    # The byte immediately BEFORE the preamble must be the stage's own zero padding: both
    # stage-2 flavours are padded to STAGE2_LEN with 0x00, so the last byte echoed back is
    # always 0x00.  Checking it is what makes the fixed offset trustworthy.
    #
    # Without it, a single byte swallowed anywhere in the 2080-byte echo shifts everything
    # left by one, and if the first flash byte then happens to be 0xA5 the preamble test
    # STILL passes -- the window it looks at is seven real preamble bytes plus that flash
    # byte.  The result is a dump missing its first byte, every later byte shifted, and the
    # word "clean" printed over it.  Two coincidences, and a silently wrong image is exactly
    # the outcome this tool exists to not produce.
    aligned = ONESHOT_ECHO == 0 or buf[ONESHOT_ECHO - 1:ONESHOT_ECHO] == b"\x00"
    pre = buf[ONESHOT_ECHO:ONESHOT_START]
    if aligned and len(pre) == PREAMBLE_LEN and all(b == SYNC for b in pre[1:]):
        return buf[ONESHOT_START:], ("clean" if pre[0] == SYNC
                                     else "turnaround glitch absorbed (0x%02X)" % pre[0])
    run = bytes([SYNC]) * (PREAMBLE_LEN - 1)        # scan: first byte may be mangled
    idx = buf.find(run, max(0, ONESHOT_ECHO - 512))
    if idx < 0:
        return buf[ONESHOT_START:], None

    # Measure from where the run BEGINS, not where it ends.  Skipping trailing 0xA5 looks
    # obvious and is wrong: flash may legitimately start with 0xA5, and those bytes are
    # indistinguishable from preamble, so the "end of the run" is not a boundary at all --
    # it silently ate the first byte of every image that began that way.  The START is
    # unambiguous, and the preamble has a fixed length, so the payload follows by counting.
    rs = idx
    while rs > 0 and buf[rs - 1] == SYNC:
        rs -= 1
    # What sits in front of the run says whether the preamble's first byte survived: the
    # stage's own zero padding means it did, anything else is the mangled first byte itself.
    glitched = rs > 0 and buf[rs - 1] != 0x00
    start = rs + PREAMBLE_LEN - (1 if glitched else 0)
    return buf[start:], "resynced at +%d (nominal +%d)" % (start, ONESHOT_START)


def load_reference():
    """The reference dump, or None -- and it SAYS which, instead of skipping in silence.

    Three call sites used to do this inline with `except Exception: pass`, so a --reference
    with a typo in it produced no comparison and no complaint: the run looked exactly like a
    run with no reference set, which is the default.  "No silent skips" applies to the
    diagnostics as much as to the flash.  No reference configured is not an error and says
    nothing; a reference configured and unreadable is worth one line."""
    if not REF_BIN:
        return None
    try:
        return open(REF_BIN, "rb").read()
    except Exception as e:
        print("[kline]   (reference dump set but unreadable, so no comparison: %s)" % e)
        return None


def compare_report(data, label="dump"):
    """Print how `data` lines up with the reference dump of this ECU, and say whether it did.

    Returns True when there is nothing to disagree with -- no reference set, or every
    compared byte matched -- and False when a reference was read and bytes differed.  The
    return value is what --verify hands the shell, so a script can use it as a gate."""
    import hashlib
    print("[kline] %s: %d bytes  sha256=%s" % (label, len(data),
          hashlib.sha256(data).hexdigest().upper()))
    agreed = True
    for name, path in (("reference dump", REF_BIN),):
        if not path:
            # Not an error, and it must not look like one: no reference is the DEFAULT.
            # Printing an errno for an empty filename reads as a malfunction and invites
            # people to go hunting for a file the project deliberately does not ship.
            print("[kline]   vs %-14s no reference set (--reference or OPENM74_REFERENCE)"
                  % name)
            continue
        try:
            ref = open(path, "rb").read()
        except Exception as e:
            print("[kline]   vs %-14s skipped (%s)" % (name, e)); continue
        n = min(len(data), len(ref))
        # The length check comes FIRST.  `if not n: continue` used to run ahead of it, so a
        # zero-byte file -- a full disk, a cancelled copy, a stray `> backup.bin` -- printed
        # no comparison at all and exited 0, while a merely short one exited 1.  The empty
        # case is the one where `openm74 --verify backup.bin && openm74 --flash new.bin
        # --yes` is most dangerous, because there is no backup behind it whatsoever.
        if len(data) != len(ref):
            # A LENGTH mismatch is a mismatch.  Comparing min(len) and reporting
            # "1024/1024 bytes match <-- BYTE-IDENTICAL" over a file that is 830 KB short
            # of the reference is the single most misleading thing this function could
            # say, and it said it -- measured, on a truncated dump -- while exiting 0
            # under a docstring offering the result as a gate for somebody's script.
            agreed = False
            print("[kline]   vs %-14s LENGTH MISMATCH: %d bytes against the reference's %d"
                  % (name, len(data), len(ref)))
            if not n:
                print("[kline]      the file is EMPTY -- there is nothing to compare and "
                      "nothing to fall back on")
                continue
            print("[kline]      comparing the %d bytes they have in common; a short file "
                  "is not a copy of this ECU" % n)
        diff = [i for i in range(n) if data[i] != ref[i]]
        print("[kline]   vs %-14s %d/%d bytes match (%.4f%%)%s"
              % (name, n - len(diff), n, 100.0 * (n - len(diff)) / n,
                 "" if diff or len(data) != len(ref) else "   <-- BYTE-IDENTICAL"))
        if diff:
            agreed = False
            print("[kline]      first differing offsets: %s"
                  % ", ".join("0x%06X" % (0xC00000 + o) for o in diff[:6]))
    return agreed


FLASH_BASE = 0xC00000


def monitor_selftest(k):
    """Read-only proof that the monitor works before anything can be erased.

    It exercises exactly the machinery a write depends on -- the command frame, the
    status byte, and the register-form EXTS that puts a flash command cycle into the
    target's own segment -- but only through READ, so a mistake here costs nothing."""
    if not k.mon_ping():
        print("[kline] PING did not answer -- monitor not responding"); return False
    print("[kline] PING ok")
    ok = True
    for addr, n in ((FLASH_BASE, 256), (0xC80000, 256), (0xCC0000, 256)):
        data = k.mon_read(addr, n)
        note = ""
        for name, ref in (("dump", load_reference()),):
            if ref is None:
                continue
            off = addr - FLASH_BASE
            if off + n <= len(ref):
                m = sum(1 for i in range(n) if data[i] == ref[off + i])
                note += "  vs %s %d/%d" % (name, m, n)
        print("[kline] READ 0x%06X: %s%s" % (addr, data[:16].hex(" "), note))
    # No reference is the normal case, not an error: a dump is one vehicle's data and none
    # is shipped.  Saying so plainly beats reporting an errno for a filename nobody set.
    if not REF_BIN:
        print("[kline] reset vector %s (no --reference set, so nothing to compare it with)"
              % k.mon_read(FLASH_BASE, 4).hex(" "))
    else:
        try:                                    # the boot vector is a known constant
            ref = open(REF_BIN, "rb").read(4)
            head = k.mon_read(FLASH_BASE, 4)
            if head == ref:
                print("[kline] *** MONITOR READ CONFIRMED (reset vector %s matches the "
                      "dump) ***" % head.hex(" "))
            else:
                print("[kline] reset vector %s != %s -- addressing is off, do NOT write"
                      % (head.hex(" "), ref.hex(" ")))
                ok = False
        except Exception as e:
            print("[kline] reference compare skipped:", e)
    # Read-only, and the natural place for it: this session is the one someone runs before
    # deciding to write.  An ECU with protection installed is worth knowing about then.
    report_protection(k)
    return ok


def blank_check(data):
    """Erased flash on this part reads back all-ZERO, not 0xFF (XE166N UM, both erases)."""
    bad = [i for i, b in enumerate(data) if b != 0x00]
    return (not bad), bad


def test_pattern(n):
    """Varied bits in every byte, so a half-written page, a shifted read or a stuck bit
    shows up as an obvious mismatch rather than a plausible-looking result."""
    return bytes((i ^ 0xA5) & 0xFF for i in range(n))


def write_selftest(k, addr, sector=False, outdir="."):
    """Prove erase AND program on one page or one sector, then put it back as found.

    Writing the ORIGINAL bytes back would prove nothing where the original is already
    blank -- programming zeros into erased flash is a no-op -- so the test writes a
    distinctive pattern, verifies it landed, erases again and restores the original.
    Net change: zero.

    With sector=True this exercises the 4 KB sector erase and the 32-page programming
    loop, which is exactly what --write is built on."""
    gran = SECTOR if sector else PAGE
    what = "sector" if sector else "page"
    if addr % gran:
        sys.exit("[kline] --write-selftest needs a %d-byte-aligned address for a %s"
                 % (gran, what))
    print("[kline] write self-test on the %s at 0x%06X (%d bytes)" % (what, addr, gran))
    print("[kline]   erase -> blank-check -> program a pattern -> verify -> restore")
    before = k.mon_read(addr, gran)
    print("[kline]   as found: %s ..." % before[:16].hex(" "))
    # Check that read before erasing on the strength of it.  This is the only copy of the
    # bytes about to be destroyed, and step [4/4] compares flash against THIS buffer -- so a
    # single flipped byte here is restored into flash and then confirmed by a check that
    # agrees with itself, under a line reading "restored to exactly what it was".
    # write_region already refuses to trust a read it is going to write back; this path had
    # the same shape with the check missing.
    if verify_against_ecu(k, addr, before):
        print("[kline]   the read of the original does not match what the ECU computes "
              "over it -- refusing to erase, because that read is the only copy")
        return False
    # And put it on disk before erasing.  This path takes no pre-flight backup -- it rewrites
    # one page or one sector, so reading all 832 KB for it would be absurd -- but "the only
    # copy is a variable in a running process" is not what the README means when it says a
    # write is backed up first.  The default target (0xCCF000) is live adaptation data.  If
    # this process dies between the erase and the restore, this file is what is left.
    keep = os.path.join(outdir or ".",
                        "m74_selftest_%s_0x%06X_%s.bin"
                        % (what, addr, time.strftime("%Y-%m-%d_%H%M%S")))
    try:
        with open(keep, "wb") as f:
            f.write(before)
            f.flush()
            os.fsync(f.fileno())
        print("[kline]   the original %s saved to %s before anything is erased" % (what, keep))
    except OSError as e:
        print("[kline]   could NOT save the original to disk (%s) -- refusing to erase" % e)
        return False
    ref = load_reference()
    if ref is not None:
        off = addr - FLASH_BASE
        print("[kline]   vs the reference dump: %s"
              % ("identical" if before == ref[off:off + gran]
                 else "DIFFERENT (the ECU has rewritten this area since the dump)"))

    def erase_and_check(tag):
        st = k.mon_erase(addr, sector=sector)
        print("[kline]   %s ERASE %s: %s" % (tag, what, MON_STATUS.get(st, "0x%02X" % st)))
        # What the controller made of it, not just whether it went idle.  Printed on the
        # way past rather than only on failure: this self-test is where someone checks that
        # the machinery works, and a diagnostic nobody ever sees working is one nobody
        # trusts when it finally says something.
        note = flash_status_note(k.flash_status())
        if note:
            print("[kline]   %s %s" % (tag, note))
        if st != MON_OK:
            return False
        blank, bad = blank_check(k.mon_read(addr, gran))
        print("[kline]   %s blank-check (expecting all 0x00): %s"
              % (tag, "clean" if blank else "%d bytes still set, first at +%d"
                 % (len(bad), bad[0])))
        return blank

    def program(data, tag):
        for p in range(0, gran, PAGE):
            st = k.mon_program(addr + p, data[p:p + PAGE])
            if st != MON_OK:
                print("[kline]   %s PROGRAM page 0x%06X: %s"
                      % (tag, addr + p, MON_STATUS.get(st, "0x%02X" % st)))
                return False
        return True

    if not erase_and_check("[1/4]"):
        return False

    pattern = test_pattern(gran)
    if not program(pattern, "[2/4]"):
        return False
    print("[kline]   [2/4] PROGRAM pattern: ok (%d page(s))" % (gran // PAGE))
    back = k.mon_read(addr, gran)
    if back != pattern:
        bad = [i for i in range(gran) if back[i] != pattern[i]]
        print("[kline]   [2/4] VERIFY FAILED: %d/%d bytes differ, first at +%d (%02X != %02X)"
              % (len(bad), gran, bad[0], back[bad[0]], pattern[bad[0]]))
        print("[kline]   read back: %s" % back[:16].hex(" "))
        print("[kline]   the %s still holds the test pattern -- restore it with --write "
              "from the reference dump" % what)
        return False
    print("[kline]   [3/4] pattern verified byte-for-byte")

    if not erase_and_check("[3/4]"):
        return False
    if not program(before, "[4/4]"):
        return False
    print("[kline]   [4/4] PROGRAM the original back: ok")
    back = k.mon_read(addr, gran)
    if back == before:
        print("[kline] *** WRITE PATH PROVEN: %s erase + program verified, restored "
              "to exactly what it was ***" % what)
        return True
    bad = [i for i in range(gran) if back[i] != before[i]]
    print("[kline] restore MISMATCH: %d/%d bytes differ, first at +%d (%02X != %02X)"
          % (len(bad), gran, bad[0], back[bad[0]], before[bad[0]]))
    return False


def calibrate_link(k, rounds=16, quiet=False):
    """Measure this bench and tune ourselves to it, automatically, every session.

    Nobody is going to run a separate diagnostic before flashing -- the tool has to work
    like the thing it replaces: plug the cable in, press the button.  So the measurement
    that used to need --linktest happens here, costs a fraction of a second, and feeds
    two numbers that were previously guesses:

      * how long a healthy reply actually takes -> the stall timeout.  If replies land
        in 20 ms then waiting 80 ms is both safer (no false stalls) and far quicker to
        recover than any fixed guess.  This docstring used to add that an over-long
        timeout "manufactures idle line time, and after ~1 s idle this hardware drops a
        byte from the next transfer".  That is RETRACTED -- measured twenty frames per
        condition, silence cost nothing (QUIRKS section 5, and the STATUS_LOOK comment at
        the top of this file).  A short timeout is still right; the reason is simply that
        it recovers a genuine stall sooner.
      * how lossy the link is -> how many retries to allow.

    Returns a dict; also prints one human line, because a number nobody sees is useless."""
    # EDGE-RICH fields on purpose: what a marginal link charges for is how often the line has
    # to change state, not how many one-bits the data holds.  Measured with fixed patterns,
    # 0x0F and 0x55 carry the same four ones per byte and cost 4% and 25% of pages, while
    # 0xFF -- every bit a one -- costs nothing.  So do not "improve" this to sparse or
    # constant fields: a probe of 0xFF would measure the easiest traffic there is and flatter
    # the link.  PING ignores the fields; only the checksum sees them.
    DENSE = (0xA5A5, 0x5A5A, 0xF0F0, 0x0FF0, 0xFF00, 0x33CC)
    lat, bad = [], 0
    for i in range(rounds):
        # Segment 0x00, not 0xCC.  These frames are deliberately bit-dense to measure the
        # link honestly, and the frame checksum is a plain sum -- so two errors that cancel
        # pass it.  Pointed at real flash, one such pair could turn a PING into an ERASE the
        # gate would allow, before the backup has even been taken.  Naming a segment that is
        # not flash costs nothing (PING ignores the field, the checksum still covers it) and
        # makes any mangled command refusable by the gate.  mon_set_baud already does this.
        frame = k.wire_frame(CMD_PING, DENSE[i % len(DENSE)],
                             DENSE[(i + 3) % len(DENSE)])
        # DO NOT pad this frame with extra 0xA5 markers to make a page-length probe.  It
        # looks free -- the monitor skips a marker run by design, so it is the same PING --
        # but corrupt one byte INSIDE a long marker run and the monitor takes it for the
        # command byte, reads the rest of the padding as a body, answers, then finds the next
        # 0xA5 and does it again.  One probe, several replies, and the host loses step.  A
        # long marker run is a stack of loose frames waiting for one bad byte.
        k.ser.reset_input_buffer()
        k._settle()
        k._tx(frame)
        if not k._swallow(len(frame), tmo=2.0 + len(frame) * 12.0 / k.baud):
            bad += 1
            continue
        t0 = time.time()
        st = k._read_status(2.0, "calibrate", required=False)
        if st is None:
            bad += 1
            k._resync("calibrate")
        elif st == MON_CKS:
            bad += 1
        else:
            lat.append(time.time() - t0)

    worst = max(lat) if lat else 0.2
    look = min(1.0, max(0.08, worst * 4.0))
    frame_err = float(bad) / rounds
    # Per-byte rate implied by the frame length, kept for the log and the event stream so a
    # bad link can be described in a comparable unit.  Nothing decides anything from it any
    # more -- the model it belongs to (loss spread evenly over the bytes) was measured wrong,
    # and what replaced it is slow_down_if_needed(), which watches real pages instead of
    # extrapolating from short frames.  The length is taken from the frame actually sent
    # rather than hand-counted, which was itself once off by one.
    nbytes = len(k.wire_frame(CMD_PING, 0, 0))
    per_byte = 1.0 - (1.0 - frame_err) ** (1.0 / nbytes) if frame_err < 1 else 1.0
    retries = 6 if frame_err < 0.02 else (12 if frame_err < 0.15 else 20)
    globals()["STATUS_LOOK"] = look
    event("stage", stage="calibrated", baud=k.baud, frame_err=round(frame_err, 4),
          per_byte=per_byte, retries=retries, look_ms=int(look * 1000))
    if not quiet:
        # k of N, not just a percentage.  "0.0%" here is 0 out of 48, and that is a very
        # different statement: against a quarter of page payloads failing, a uniform
        # per-byte model still gives a clean sweep of 48 short frames about a one-in-three
        # chance.  Printed as a bare percentage it was once read as proof that failures
        # depended on transfer length, and sent an investigation somewhere there was
        # nothing.  The sample size is what stops a reader over-reading the number.
        print("[kline] link: %d of %d %d-byte probe frames needed a retry (%.1f%%, "
              "%.0e per byte), replies within %d ms"
              % (bad, rounds, nbytes, 100.0 * frame_err, per_byte, int(worst * 1000)))
        print("[kline] tuned to this bench: stall timeout %d ms, %d retries per page"
              % (int(look * 1000), retries))
        if frame_err > 0.25:
            print("[kline] this link is poor -- writes will still complete, but check the")
            print("[kline] ground to the ECU, keep the K wire short, and make sure the")
            print("[kline] adapter has +12 on OBD pin 16.  --linktest gives the details.")
    return {"frame_err": frame_err, "per_byte": per_byte, "look": look,
            "retries": retries, "nbytes": nbytes}


# How many pages may be expected to exhaust their retries across a WHOLE image before a
# rate is called unusable.  0.05 means: at the rate we pick, the odds of the write dying on
# a page are about one run in twenty -- and the per-sector verify is still behind that.
PAGE_FAIL_ALLOW = 0.05


def page_budget(retries, pages=FLASH_SIZE // PAGE, allow=PAGE_FAIL_ALLOW):
    """The worst per-page failure rate that still finishes an image.

    Derived rather than picked: the host gives up on a page after `retries` attempts, so a
    page is lost with probability q**retries, and over `pages` of them the expected number
    of losses is pages x q**retries.  Hold that at `allow` and solve for q.  A hand-chosen
    constant would go stale the moment the retry budget or the image size moved -- and both
    are already computed per session."""
    return (float(allow) / max(1, pages)) ** (1.0 / max(1, retries))


def slow_down_if_needed(k, resends, attempts, retries, quiet=False):
    """Step the line down when the sectors just written say the rate is too fast.

    This is where a write's speed is decided, and it is decided by WATCHING rather than
    predicting.  **Do not judge a write by a probe of command frames.**  A probe frame is ten
    bytes and a page payload is a hundred and thirty-two; on a marginal link the two behave
    differently enough that a rate extrapolated from the short one can be wrong in either
    direction, and being wrong costs the whole run's duration.  Every sector is 32 real pages
    and every resend is already counted, so after two sectors the write has measured itself
    on the exact traffic at the exact moment.

    Two independent reasons to drop a rung, and both are needed:

      * COMPLETION -- losses past what the retry budget absorbs put the run at risk of dying
        on a page, and no amount of speed is worth that.
      * THROUGHPUT -- bytes delivered scale with baud x (1 - q), and the rung below cannot do
        better than its own baud with no losses at all.  Step down only when even that
        ceiling beats what this rung is measurably achieving, so the move cannot be wrong in
        expectation.  Without this test a run can sit at a rate that is technically safe and
        five times slower than the one below it.

    **Only downwards.**  Climbing back would let a lucky quiet stretch raise the rate again
    and turn the run into an oscillator.

    **Feed this LINE errors only.**  `retries_used` is incremented in exactly four places -- a
    successful resync, a status that never arrived, and the two checksum failures -- while a
    flash timeout ('T') and a gate refusal ('N') come back as statuses and are handled by the
    caller.  Keep it that way: a slower baud does nothing for a flash that is busy or a
    segment that is locked, so counting those here steps the rate down in response to
    something the rate cannot fix."""
    if attempts < 64 or not getattr(k, "ladder", None):
        return False                      # fewer than two sectors: too noisy to act on
    lower = [b for b in k.ladder if b < k.baud]
    if not lower:
        return False
    q = float(resends) / max(1, attempts + resends)

    # Two independent reasons to drop a rung, and the second one was missing until the bench
    # showed what its absence looks like.
    #
    # 1. COMPLETION.  Beyond what the retry budget can absorb, the run is at risk of dying on
    #    a page, and no amount of speed is worth that.
    risky = q > page_budget(retries or 8)
    #
    # 2. THROUGHPUT, which a completion-only rule ignores entirely -- and ignoring it is not
    #    academic: MEASURED on hardware, 57600 lost 38% of page attempts with 20 retries
    #    allowed, so the odds of losing a page were 4e-9 and the completion test was happy to
    #    sit there.  ETA: FIVE HOURS, against about 25 minutes at 19200.  Correct by the
    #    stated rule, useless to the person waiting.
    #
    #    The test is deliberately one-sided so it cannot be argued with: bytes actually
    #    delivered scale with baud x (1 - q), and the rung below cannot do better than its own
    #    baud with no losses at all.  Step down only when even that CEILING beats what this
    #    rung is measurably achieving -- then the move cannot be a mistake in expectation.
    #    If the lower rung turns out just as lossy, little was lost, and since the rate never
    #    climbs back there is nothing here to oscillate.
    slow = lower[0] > k.baud * (1.0 - q)

    if not (risky or slow):
        return False
    if not quiet:
        if risky:
            print("[kline]   %d baud is losing %.0f%% of page attempts, past the %.0f%% the "
                  "retry budget absorbs" % (k.baud, 100.0 * q, 100.0 * page_budget(retries or 8)))
        else:
            print("[kline]   %d baud is losing %.0f%% of page attempts, so it is delivering "
                  "about %d baud's worth" % (k.baud, 100.0 * q, int(k.baud * (1.0 - q))))
        print("[kline]   -- stepping down to %d and carrying on" % lower[0])
    return k.mon_set_baud(lower[0])


# Fastest first.  MEASURED with each rate exercised the way it is used -- a sustained 32 KB
# read AND real page programming, never a PING:
#
#     38400   2415 B/s   0 bad, 0 resent        115200   6973 B/s   0 bad, 0 resent
#     57600   3604 B/s   0 bad, 0 resent        153600   the ECU answers at no rate at all
#     76800   4720 B/s   0 bad, 0 resent
#
# Reading and writing stop in the same place; there is no separate write ceiling.  The wall
# is the divider arithmetic rather than the wire.
RUN_LADDER = (115200, 76800, 57600, 38400, 19200, 9600)

# What a sector costs, timed at 115200:
#
#     sector erase          0.02 s
#     32 pages programmed   1.44 s
#     verify by checksum    0.02 s   (fast mode)
#     verify by read-back   0.59 s   (reliable mode)
#
# The erase is a rounding error; a write costs its programming, which DOES scale with the
# line rate.  If you ever measure a multi-second erase, you are timing a recovery path rather
# than the flash.
ERASE_S = 0.02
SECTOR_PROGRAM_S = 1.44          # at 115200; scales roughly with the rate
SECTOR_VERIFY_S = 0.59           # read-back; the checksum path is 0.02


def tune_speed(k, ladder, writing, quiet=False):
    """Pick the fastest rate this bench actually sustains FOR WHAT WE ARE ABOUT TO DO.

    Speed and safety are not one dial: the same link that streams reads perfectly at
    57600 cannot push a 132-byte page across at all.  So measure at each step and stop
    at the first rate good enough for the job -- fast for a read, quiet enough for a
    write -- instead of trusting a number measured in the other direction."""
    # A write decision needs a finer measurement than a read one.  With 16 probe frames a
    # single failure reads as 6.2%, which straddles the threshold -- the same 38400 link
    # measured 6.2% on one run and 0.0% on the next, so the ladder picked differently each
    # time.  The probe costs a fraction of a second; the decision costs half an hour.
    rounds = 48 if writing else 16

    def good_enough(c):
        """A read needs any working link.  A write needs one that is not hopeless.

        **Do not convert this frame rate into a per-byte figure and judge a page by it.**
        That assumes loss is spread evenly over the bytes; on a marginal link it is not, and
        a ten-byte frame then misrepresents a hundred-and-thirty-two-byte payload badly
        enough to pick the wrong rate for an entire run.

        What a frame probe CAN say honestly is a lower bound: a page attempt costs one
        command frame plus a payload, so it cannot fail less often than the frame alone.  If
        even that lower bound exceeds what the retry budget absorbs, the rate is hopeless and
        there is no point starting.  Everything below that is left to the write itself to
        measure, in slow_down_if_needed()."""
        if not writing:
            return True, 0.0, 0.0
        limit = page_budget(c["retries"])
        return c["frame_err"] <= limit, c["frame_err"], limit

    cal = calibrate_link(k, rounds=rounds, quiet=quiet)
    for want in ladder:
        if want == k.baud:
            pass
        elif not k.mon_set_baud(want):
            continue
        else:
            cal = calibrate_link(k, rounds=rounds, quiet=quiet)
        ok, got, limit = good_enough(cal)
        if ok:
            return cal
        lower = [b for b in ladder if b < k.baud]
        if lower:
            print("[kline]   %d baud loses %.0f%% of command frames alone (%.0f%% is all the "
                  "retry budget absorbs) -- stepping down to %d"
                  % (k.baud, 100.0 * got, 100.0 * limit, lower[0]))
    # Off the bottom of the ladder: the slowest rate we have is still worse than the write
    # wants, and the run is about to go ahead at it anyway.  Say so.  Announcing a step down
    # and then not stepping (there is nothing below) reads as a decision that was made,
    # while the fact worth knowing -- that this bench is below spec for writing today -- goes
    # unsaid.  It is also the single most useful line in the log if the write later fails.
    ok, got, limit = good_enough(cal)
    if writing and not ok:
        print("[kline] no rate is clean enough for %d-byte pages today: staying at %d baud,"
              % (PAGE, k.baud))
        print("[kline]   the slowest there is.  %.0f%% of command frames fail against the "
              "%.0f%% the retry budget absorbs," % (100.0 * got, 100.0 * limit))
        print("[kline]   so expect many resends -- each one is caught and retried, and the")
        print("[kline]   per-sector verify still has to pass before the next sector starts.")
        # The tool has bounded what it can do and is proceeding anyway.  That is the
        # project's own definition of a silent cap if it reaches only the prose log.
        event("problem", kind="link_below_target", baud=k.baud,
              frame_err=round(got, 4), budget=round(limit, 4),
              detail="no rate is clean enough for page programming; proceeding at the "
                     "slowest rate with resends expected")
        print("[kline]   If this write does fail, the link is why: check ground, the +12 on")
        print("[kline]   OBD pin 16, and the K-line run length before blaming the image.")
    return cal


def link_test(k, rounds=150):
    """Measure each direction of the K line separately, without writing anything.

    Anyone bringing up their own bench needs a number, not a feeling.  The two
    directions are measured independently because on THIS bench they differ by orders
    of magnitude, and which one is bad points at a different culprit:

      host->ECU bad, ECU->host clean   the ECU's receiver is missing our edges.  The
                                       L9637D's threshold is referenced to Vbatt, so a
                                       weak bus pull-up (slow rise) hurts it long before
                                       it bothers the adapter's receiver.  Check the K
                                       pull-up and that the adapter has +12 on OBD 16.
      both directions bad              grounding or wiring: no common ground with the
                                       ECU, long unshielded runs, power through the
                                       adapter instead of straight from the supply.
      ECU->host bad only               the adapter's receiver or its USB driver.

    host->ECU is measured with PING frames whose address/count fields vary: PING ignores
    them but the checksum covers them, so a mangled byte is reported as 0x43 and nothing
    is executed.  ECU->host is measured by reading known flash and comparing."""
    print("[kline] link test: %d frames each way, nothing is written" % rounds)
    tx_bad = tx_stall = 0
    for i in range(rounds):
        frame = k.wire_frame(CMD_PING, (i * 0x9E37) & 0xFFFF,     # segment 0x00: see above
                             (i * 0x5A5B) & 0xFFFF)
        k.ser.reset_input_buffer()
        k._settle()
        k._tx(frame)
        if not k._swallow(len(frame), tmo=2.0 + len(frame) * 12.0 / k.baud):
            raise IOError("link test: the frame was not even echoed -- check the adapter")
        # A generous window ON PURPOSE, not the operational STATUS_LOOK: this must measure
        # the LINE, not how quickly the host gives up.  Learned the hard way -- shortening
        # STATUS_LOOK for speed moved this same bench from "12% of frames" to "47%" with
        # no change to the hardware or the stage, which made two runs look like a
        # regression when nothing had regressed.
        st = k._read_status(2.0, "linktest", required=False)
        if st is None:
            tx_stall += 1
            st = k._resync("linktest")
            if st is None:
                raise IOError("link test: monitor stopped answering")
        elif st == MON_CKS:
            tx_bad += 1
        if (i + 1) % 50 == 0:
            print("[kline]   %d/%d frames" % (i + 1, rounds))
    tx_events = tx_bad + tx_stall
    tx_bytes = rounds * len(k.wire_frame(CMD_PING, 0, 0))    # measured, not hand-counted

    n = 8192
    ref = None
    if not REF_BIN:
        # Not an error and it must not read as one: no reference is the DEFAULT, and
        # printing an errno for an empty filename sends people hunting for a file this
        # project deliberately does not ship.
        print("[kline]   (no reference dump set, so the ECU->host direction cannot be")
        print("[kline]    measured -- pass --reference or set OPENM74_REFERENCE)")
    else:
        try:
            ref = open(REF_BIN, "rb").read()[:n]
        except Exception as e:
            print("[kline]   (the reference dump could not be read, so the ECU->host "
                  "direction is not measured: %s)" % e)
    rx_bad = 0
    if ref:
        got = k.mon_read(FLASH_BASE, n)
        rx_bad = sum(1 for i in range(n) if got[i] != ref[i])

    print("[kline] ---- link quality ----")
    print("[kline]   host -> ECU : %d corrupted + %d swallowed of %d frames "
          "(%.2f%% of frames, %.1e per byte)"
          % (tx_bad, tx_stall, rounds, 100.0 * tx_events / rounds,
             float(tx_events) / tx_bytes))
    if ref:
        print("[kline]   ECU -> host : %d bad bytes of %d (%.1e per byte)"
              % (rx_bad, n, float(rx_bad) / n))
    print("[kline] ---- verdict ----")
    # `rx_bad == 0` means "no bad bytes" only when the direction was actually measured.
    # Without a reference it is zero because nothing counted it, and the verdict used to
    # say "clean BOTH ways" on the strength of one measured direction and one unmeasured
    # one -- caught on the bench, where the default is to have no reference.
    if tx_events == 0 and ref is None:
        print("[kline]   host->ECU is clean; the other direction was NOT measured, so this")
        print("[kline]   is half a verdict.  Give it a reference dump for the other half.")
    elif tx_events == 0 and rx_bad == 0:
        print("[kline]   clean both ways -- this bench needs no retries at all")
    elif tx_events and not rx_bad:
        # Deliberately does NOT tell anyone to change a resistor.  The earlier wording here
        # said the bus wants ~510 ohm and called the fitted 100k part slow -- true of the
        # ISO pull-up to Vbatt, and ruinous advice about the pad next to the transceiver,
        # which is a different position doing a different job.  People with this exact board
        # have fitted 510 ohm there and the ECU then does not come up on the line at all.
        # An asymmetric link is the NORMAL state of this bench (see docs/FINDINGS.md §4),
        # so a verdict that sends someone soldering is wrong before it is even read.
        print("[kline]   only host->ECU is lossy, which is this link's normal asymmetry:")
        print("[kline]   the ECU pulls the line down hard and lets a pull-up bring it back")
        print("[kline]   up, so the host's direction is the fragile one.  Every bad frame")
        print("[kline]   is caught and resent, so writes still complete -- just slower.")
        print("[kline]   Worth checking: +12 on OBD pin 16, ground straight to the ECU, a")
        print("[kline]   short K-line run.  Do NOT change the pull-up by the transceiver")
        print("[kline]   on this advice -- docs/HARDWARE.md section 3 says why.")
    elif rx_bad and not tx_events:
        print("[kline]   only ECU->host is lossy: suspect the adapter's receiver/driver")
    else:
        print("[kline]   both directions lossy: check grounding and wiring first --")
        print("[kline]   common ground straight to the ECU, short runs, supply not")
        print("[kline]   routed through the adapter")
    return tx_events, rx_bad


def unmapped_pattern(k):
    """What THIS ECU answers for an address its flash controller has no memory behind.

    Measured, and asked of the unit in front of us rather than baked in: reading just past
    the end of the populated array gives the controller's "nothing here" answer, whatever it
    happens to be on this part.  On the bench unit it is 0x9B1E repeating -- and a segment
    with no controller at all answers 0x46 instead, so the value belongs to the silicon and
    hard-coding it would be exactly the wrong move.

    This is what lets a region that HOLDS NO MEMORY be told apart from a region that failed
    to erase.  Both leave the contents unchanged, which is why "erase changed nothing" could
    never separate them on its own."""
    try:
        return bytes(k.mon_read(FLASH_BASE + FLASH_SIZE, PAGE))
    except IOError:
        return None                 # cannot probe -> the caller stays conservative


def describe_flash_status(op, prot):
    """Turn the two status words into sentences.  Empty means the controller flagged
    nothing -- which is itself an answer, and the one that matters here."""
    out = [text for bit, text in FSR_OP_FAULTS if op & bit]
    out += [text for bit, text in FSR_PROT_FAULTS if prot & bit]
    return out


def flash_status_note(st):
    """One line about the last flash operation, for printing beside a surprising result.

    Takes what k.flash_status() returned rather than fetching it, so a caller that also
    has to reason about the bits reads them once.

    The distinction this exists for: an erase that changes nothing looks identical from
    outside whether the sector is write-protected, whether the command was malformed, or
    whether there is simply no memory at that address.  The controller knows which, and
    until the stage latched these registers we had no way to ask -- the tool could only
    report the symptom.  A quiet controller after a completed erase is the signature of an
    address with nothing behind it: the command was accepted, and it had nothing to do."""
    if st is None:
        return None
    op, prot = st
    why = describe_flash_status(op, prot)
    head = "flash controller: FSR_OP=0x%04X FSR_PROT=0x%04X" % (op, prot)
    if not why:
        return head + " -- no error reported; it accepted the command"
    return head + " -- " + "; ".join(why)


def report_protection(k):
    """Say up front whether this ECU has flash protection installed.

    Read-only, three registers, once per write.  If protection IS installed, a locked
    sector will refuse to erase later on and the run would otherwise have to guess why;
    saying it here turns that into a diagnosis instead of a mystery.  Returns True when
    nothing is protected, None when the registers could not be read."""
    st = k.protection_state()
    if st is None:
        return None
    prot, procon = st
    if not prot & 0x01:                 # PROIN clear: nothing is installed to violate
        print("[kline] flash protection: none installed (PROIN clear) -- no sector is locked")
        event("stage", stage="protection", installed=False, fsr_prot=prot, procon=procon)
        return True
    # Installed.  A PROCON bit CLEAR means that logical sector is locked, so report the
    # zeros, not the ones -- getting that backwards would print reassurance about a
    # locked part.
    locked = [(m, s) for m in range(3) for s in range(10) if not procon[m] & (1 << s)]
    print("[kline] flash protection IS INSTALLED on this ECU (FSR_PROT=0x%04X)" % prot)
    print("[kline]   locked logical sectors: %s"
          % (", ".join("module %d sector %d" % ms for ms in locked) or "none"))
    print("[kline]   a locked sector will refuse to erase; that is the ECU's own setting,")
    print("[kline]   not something this tool can talk it out of")
    event("problem", kind="protection", installed=True, fsr_prot=prot, procon=procon,
          locked=len(locked), detail="flash write protection is installed on this ECU")
    return False


def looks_unmapped(sector, pattern):
    """Three signals, all required, before believing a region holds no memory.

    A false positive here means skipping a region a write really did fail on, so the bar is
    high: a sector of real flash would have to fail to erase AND consist of a single repeated
    word AND that word would have to be this controller's own no-memory answer."""
    if not pattern or len(sector) < 2:
        return False
    if pattern[0:2] == b"\x00\x00":
        # Erased flash on this part reads all zeros, so if a unit ever answered 00 00 for
        # an address with no memory the two would be indistinguishable -- and this
        # predicate would classify an erased-but-unprogrammed sector as a hole, letting
        # final_verify print PASSED over a sector that never got its data.  The measured
        # answer here is 9b 1e; refuse the ambiguous one rather than guess.
        return False
    word = sector[0:2]
    if any(sector[i:i + 2] != word for i in range(0, len(sector) - 1, 2)):
        return False                # not one word repeated
    return pattern[0:2] == word     # and it is what this ECU says for nothing


def verify_against_ecu(k, addr, data, chunk=0x8000):
    """Ask the ECU to add up what it holds and compare with what we think we read.

    Chunked below a segment boundary on purpose: the stage's offset register wraps inside
    its own 64 KB segment and the count field is 16-bit, so a request that crosses a
    boundary would silently sum the wrong bytes.  Returns the addresses that disagree."""
    bad = []
    off = 0
    while off < len(data):
        room = 0x10000 - ((addr + off) & 0xFFFF)
        n = min(chunk, room, len(data) - off)
        if k.mon_checksum(addr + off, n) != sums16(data[off:off + n]):
            bad.append(addr + off)
        off += n
    return bad


def preflight_backup(k, outdir="."):
    """Read the WHOLE flash and save it before a single byte is erased.

    The point is not convenience, it is that a write must never be the only copy of what
    was there.  Reading it in the same session as the write also means the backup is
    provably of the ECU actually on the bench, not of whatever file happened to be named
    similarly.  A write that cannot get this is refused."""
    import hashlib
    print("[kline] pre-flight backup: reading all %d bytes BEFORE touching anything"
          % FLASH_SIZE)
    event("stage", stage="backup", detail="reading the whole flash before any erase")
    data = k.mon_read(FLASH_BASE, FLASH_SIZE, progress=True, op="backup")
    # VERIFY IT.  The read path carries no per-chunk checksum -- a short read raises, but a
    # read of the right length with a flipped byte is silent -- and this is the one copy the
    # whole write is predicated on: the next thing that happens is erasing the original.  The
    # ECU can add a region up itself and send four bytes, so asking it to agree with what we
    # think we received costs seconds and turns "probably intact" into "checked".  The
    # measured ECU->host direction is clean, which is exactly why this is cheap.
    print("[kline] verifying the backup against the ECU's own checksum")
    bad = verify_against_ecu(k, FLASH_BASE, data)
    if bad:
        event("result", what="backup", ok=False, regions=len(bad),
              message="the backup does not read back consistently")
        raise IOError("the backup does not match what the ECU computes over %d region(s) "
                      "(first at 0x%06X) -- it is not a copy worth writing over"
                      % (len(bad), bad[0]))
    print("[kline] backup verified: the ECU agrees with every byte we recorded")

    name = os.path.join(outdir, time.strftime("m74_backup_%Y-%m-%d_%H%M%S.bin"))
    # Flushed and fsynced, not just written.  Everywhere else in this tool a file left to the
    # interpreter's refcount is fine; here it is not.  This is the copy the whole write is
    # predicated on -- the run refuses to start without it -- and the very next thing that
    # happens is erasing the original.  The failure this guards against is not theoretical on
    # this hardware either: docs/QUIRKS.md §8 records the USB stack hanging so hard the
    # process cannot be killed, and a machine that dies there would take an unflushed 832 KB
    # with it.  Two syscalls against losing the only copy is not a trade worth thinking about.
    with open(name, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    digest = hashlib.sha256(data).hexdigest().upper()
    print("[kline] backup saved: %s  (%d bytes, sha256 %s...)"
          % (name, len(data), digest[:16]))
    event("stage", stage="backup-saved", path=name, bytes=len(data), sha256=digest)
    return name, data


def final_verify(k, addr, data):
    """Read the written range back in full and compare -- on top of the per-sector check.

    The per-sector verify already read each sector once, so this catches only the things
    that could change afterwards; it is cheap insurance on a job where being wrong means
    a dead ECU on someone's bench.

    It also has to know about regions with no memory behind them, and it works that out
    ITSELF rather than trusting what the write phase concluded.  Two reasons.  The cheap one:
    the write said "complete and verified" while this said "FAILED, 4096 bytes differ" about
    the very same sector, on the bench, and a run whose two halves contradict each other is
    useless whichever half is right.  The real one: an independent check that believes the
    thing it is checking is not independent.  So it re-establishes the ECU's own answer for
    an unmapped address and applies the same three-signal test."""
    print("[kline] final verify: reading 0x%06X..0x%06X back" % (addr, addr + len(data) - 1))
    event("stage", stage="verifying", addr=addr, bytes=len(data))
    back = k.mon_read(addr, len(data), progress=True, op="verify")
    if back == data:
        print("[kline] *** FINAL VERIFY PASSED: %d bytes match the image exactly ***"
              % len(data))
        event("result", what="verify", ok=True, bytes=len(data),
              message="every byte of the written range matches the image")
        return True

    bad = [i for i in range(min(len(back), len(data))) if back[i] != data[i]]
    nothing = unmapped_pattern(k)
    # Group the mismatches by sector, and ask of each whether that sector holds memory.
    unmapped_secs, real = [], []
    for sec in sorted({((addr + i) & ~(SECTOR - 1)) for i in bad}):
        # Clip the sector to what was actually read, in ADDRESS space.  `sec` is rounded
        # down to a sector boundary, so with an unaligned --addr the first sector starts
        # BEFORE the range: the old `max(0, sec - addr)` collapsed to 0 and then took a
        # full SECTOR from there, which is a window starting at `addr` and running into the
        # next sector -- the wrong bytes, judged for the wrong sector.
        lo = max(sec, addr)
        hi = min(sec + SECTOR, addr + len(back))
        chunk = back[lo - addr:hi - addr]
        (unmapped_secs if looks_unmapped(chunk, nothing) else real).append(sec)

    if real:
        first = next(i for i in bad if (addr + i) & ~(SECTOR - 1) in real)
        print("[kline] FINAL VERIFY FAILED: %d byte(s) differ in memory that exists, "
              "first at 0x%06X" % (sum(1 for i in bad
                                       if (addr + i) & ~(SECTOR - 1) in real), addr + first))
        event("result", what="verify", ok=False, differing=len(bad), first=addr + first,
              message="the written range does not match the image")
        return False

    n = len(bad)
    print("[kline] *** FINAL VERIFY PASSED: every byte of memory that exists matches the "
          "image ***")
    print("[kline] %d byte(s) in %d region(s) with no memory behind them do not match, and "
          "cannot: %s"
          % (n, len(unmapped_secs),
             ", ".join("0x%06X%s" % (x, " (%s)" % RESERVED[x] if x in RESERVED else "")
                       for x in unmapped_secs)))
    event("result", what="verify", ok=True, bytes=len(data),
          unmapped=len(unmapped_secs), unmapped_image_bytes=n,
          message="every byte of memory that exists matches the image")
    return True


# ---------------------------------------------------------------------------
# COOPERATIVE STOP
#
# A write used to be stoppable only by killing the process, which is what closing the GUI
# window did -- mid-erase if that is where it happened to be, with the port dropped by the
# operating system rather than by this code.  A flag checked BETWEEN SECTORS is the safe
# place to stop instead: the sector just finished has been erased, programmed and verified,
# and the next one has not been touched, so the image is part-written in a state --resume
# understands rather than in an arbitrary one.
#
# Deliberately not a way to stop mid-sector.  There is no safe point inside one: an erase
# that has begun must be allowed to finish and the pages after it must be programmed, or
# the sector is left blank with the old contents gone.
_STOP = None


def sector_seconds(baud, reliable=True, resume=False):
    """How long one sector takes at this rate -- the same arithmetic write_region uses.

    Public because a front end needs it to decide how long to WAIT for a cooperative stop.
    Hard-coding a timeout there is how the GUI ended up allowing eight seconds, which is
    fine at 115200 and expires mid-sector at 19200 and 9600 -- the two rates the ladder
    steps down to on exactly the sort of link where someone reaches for the close button."""
    scale = 115200.0 / max(1, baud)
    rate = 610.0 * baud / BASE_BAUD
    return (ERASE_S + SECTOR_PROGRAM_S * scale
            + (2 * SECTOR_VERIFY_S * scale if reliable else 0.02)
            + (SECTOR / rate if resume else 0.0))


def stop_on_sigint(writing):
    """Make Ctrl-C ASK a write to stop, instead of tearing it open where it stands.

    Without this, `KeyboardInterrupt` is raised wherever the interpreter happens to be --
    which during a write is most often between `mon_erase(sec)` and the thirty-two
    `mon_program()` calls that refill that sector.  The result is 4 KB erased with part of
    it written, in the middle of somebody's application, which is precisely the state the
    cooperative stop exists to avoid.  The READMEs promised this behaviour before the
    handler existed; this is the code that makes the promise true.

    First press: set the flag, say so, and let the sector finish.  Second press: the
    previous handler is already back in place, so it does the ordinary thing and raises --
    someone pressing twice has decided they want out now, and that is their call to make.

    Only for writes.  A read has no sector boundary to stop on and nothing to leave
    half-done, so swallowing the first Ctrl-C there would just make the tool feel stuck.
    Returns whatever handler was in place, or None if there was nothing to do -- including
    when this is not the main thread, which is how the GUI runs it."""
    if not writing:
        return None
    import signal
    try:
        prev = signal.getsignal(signal.SIGINT)

        def ask_to_stop(signum, frame):
            signal.signal(signal.SIGINT, prev)      # a second press is not caught
            request_stop()
            print("\n[kline] Ctrl-C: finishing this sector, then stopping.  Press again to "
                  "stop immediately -- which can leave a sector erased and only part "
                  "written.")

        signal.signal(signal.SIGINT, ask_to_stop)
        return prev
    except (ValueError, OSError):
        # Not the main thread (the GUI runs main() on a worker), or a platform that will
        # not take the handler.  The GUI has its own stop button in the close handler.
        return None


def request_stop():
    """Ask a running write to stop at the next sector boundary."""
    global _STOP
    _STOP = True


def clear_stop():
    global _STOP
    _STOP = None


def stop_requested():
    return bool(_STOP)


def write_region(k, addr, data, resume=False, blank_check_each=False, retries=8):
    """Write `data` at `addr`, one 4 KB sector at a time, verifying every sector.

    Erase granularity is a sector, so a sector only PARTLY covered by the range has to
    be read first and merged, or the bytes outside the range would be lost.  A sector
    covered in full needs no such read -- which matters, because reads run at the
    stage's paced rate and otherwise dominate the wall clock: three 4 KB reads per 4 KB
    written turns a full image into hours.  The blank-check read is gone for the same
    reason and loses nothing: a failed erase shows up in the verify read either way.

    resume=True reads each sector first and skips the ones that already hold the right
    bytes, so a write interrupted by a lost link (or a hung USB port) can be re-run
    instead of restarted.  That is the failure WinFlashECU is notorious for."""
    end = addr + len(data)
    first = addr & ~(SECTOR - 1)
    total = (((end + SECTOR - 1) & ~(SECTOR - 1)) - first) // SECTOR
    rate = 610.0 * k.baud / BASE_BAUD
    # Built from the parts, each timed at 115200 and scaled by the rate in use: programming
    # the 32 pages, the verify, and the extra read a resume needs to decide whether to skip.
    # The erase is 20 ms and does not matter here.
    scale = 115200.0 / max(1, k.baud)
    # TWO full-sector reads in reliable mode, not one: the blank check after the erase and
    # the byte-exact verify after programming.  Counting only the verify made this estimate
    # 23% low -- MEASURED on a 206-sector run that predicted 7:02 and cost 9:12, and the
    # run's own ETA converged on 2.68 s/sector against the 2.05 predicted here.
    per_sector = (ERASE_S + SECTOR_PROGRAM_S * scale
                  + (2 * SECTOR_VERIFY_S * scale if blank_check_each else 0.02)
                  + (SECTOR / rate if resume else 0.0))
    print("[kline] writing 0x%06X..0x%06X (%d bytes) across %d sector(s)%s"
          % (addr, end - 1, len(data), total, ", resume mode" if resume else ""))
    print("[kline] rough estimate %d:%02d at %d baud"
          % (int(total * per_sector) // 60, int(total * per_sector) % 60, k.baud))
    # Announced before the first sector so a front end can size its progress bar from the
    # real total instead of assuming a whole image
    event("stage", stage="writing", addr=addr, bytes=len(data), sectors=total,
          resume=bool(resume), baud=k.baud, estimate=int(total * per_sector))
    done = skipped = 0
    unmapped = []                  # (address, how many bytes the image wanted there)
    # Asked once, before anything is erased: the answer cannot change during the run, and
    # having it up front means the first surprising sector is judged against evidence.
    nothing = unmapped_pattern(k)
    if nothing:
        print("[kline] this ECU answers %s for an address with no memory behind it"
              % nothing[:6].hex(" "))
    # Read-only, once, before the first erase: whether anything on this unit is locked.
    # Cheaper to ask than to deduce from a sector that will not erase 40 minutes in.
    report_protection(k)
    t0 = time.time()

    def progress(i, sec, skipped=False):
        """Report position, on EVERY outcome -- written, skipped or unwritable.

        Emitting only on the written branch is why a resume run looked frozen: resume exists
        precisely to skip work, so the run it is built for is the run that reports almost
        nothing.  Position in the job is what a progress bar shows; whether this particular
        sector needed writing is a detail that belongs in the event, not in whether the event
        happens at all."""
        el = time.time() - t0
        eta = (total - i - 1) * el / (i + 1) if i + 1 else 0
        event("progress", op="write", done=i + 1, total=total, unit="sectors",
              addr=sec, retries=k.retries_used, eta=int(eta), skipped=bool(skipped))
        return eta
    # What the write has observed about its own line since the last speed decision.
    watched = {"pages": 0, "resends": 0, "mark": k.retries_used}
    for i, sec in enumerate(range(first, first + total * SECTOR, SECTOR)):
        # Between sectors, which is the only safe place: what is behind us is verified and
        # what is ahead is untouched.  Stopping inside a sector would leave it erased with
        # the original gone.
        if stop_requested():
            touched = bool(done)
            print("[kline] STOPPED at the operator's request after %d of %d sector(s)."
                  % (i, total))
            if touched:
                # Only when something really was written.  Stopping at i == 0 -- which the
                # GUI reaches by closing during the pre-flight backup -- used to announce a
                # part-written image over an ECU nothing had touched, and steer the user
                # into an unnecessary restore.
                print("[kline] The image is PART-WRITTEN and every sector written was "
                      "verified.")
                print("[kline] Power-cycle and re-run the same command with --resume to "
                      "finish, or write the pre-flight backup to go back.")
            else:
                print("[kline] Nothing was erased or programmed: the stop landed before the "
                      "first sector.")
            event("result", what="write", ok=False, written=done, skipped=skipped,
                  addr=sec, interrupted=True, wrote_nothing=not touched,
                  message=("stopped by the operator between sectors; the image is "
                           "part-written" if touched else
                           "stopped by the operator before any sector was touched"))
            return False
        lo, hi = max(addr, sec), min(end, sec + SECTOR)
        partial = (lo != sec or hi != sec + SECTOR)
        cur = None
        if partial or resume:
            cur = k.mon_read(sec, SECTOR)
            # This is the ONLY read whose contents get written back into flash: a sector the
            # range covers partly is read, spliced, and programmed whole.  A read of the
            # right length with one flipped byte is silent, and the per-sector verify cannot
            # catch it -- it compares flash against this very buffer, so it agrees with
            # itself.  The result would be a byte OUTSIDE the range the user asked to change,
            # quietly altered, under a line reading WRITE COMPLETE AND VERIFIED.  The ECU can
            # add the sector up itself; ask it before trusting the bytes.  (Short-circuit:
            # a whole-sector write never asks, because nothing of the old sector survives.)
            if partial and verify_against_ecu(k, sec, cur):
                print("[kline]   sector 0x%06X: the read used for the merge does not "
                      "match what the ECU computes -- stopping rather than writing "
                      "bytes nobody asked for" % sec)
                event("result", what="write", ok=False, written=done, addr=sec,
                      message="the merge read at 0x%06X did not verify" % sec)
                return False
        if partial:
            merged = bytearray(cur)
            merged[lo - sec:hi - sec] = data[lo - addr:hi - addr]
            new = bytes(merged)
        else:
            new = data[lo - addr:hi - addr]
        if cur is not None and new == cur:
            skipped += 1
            print("[kline]   sector 0x%06X: already correct, skipped   [%d/%d]"
                  % (sec, i + 1, total))
            progress(i, sec, skipped=True)
            continue

        # cheap snapshot so a failed erase can be told apart from an unwritable region
        was = k.mon_read(sec, PAGE)
        st = k.mon_erase(sec, sector=True)
        if st != MON_OK:
            print("[kline]   sector 0x%06X: ERASE %s -- stopping"
                  % (sec, MON_STATUS.get(st, "0x%02X" % st)))
            note = flash_status_note(k.flash_status())
            if note:
                print("[kline]   %s" % note)
            event("result", what="write", ok=False, written=done, addr=sec,
                  message="erase refused or timed out at 0x%06X" % sec)
            return False
        # ALWAYS check the erase took, even in fast mode -- just cheaply.  An erase that
        # silently does nothing is invisible until the sector verify, and only then if
        # the new data happens to differ from the old: MEASURED, a run died at 0xC0F000
        # where the image was all zeros, so programming wrote nothing and the read-back
        # was simply the stale sector.  Sectors before it looked fine purely because the
        # image and the ECU already agreed there.  128 bytes costs nothing and catches it
        # at the sector that failed rather than somewhere downstream.
        probe = SECTOR if blank_check_each else PAGE
        after = k.mon_read(sec, probe)
        blank, bad = blank_check(after)
        if not blank:
            if after[:PAGE] == was:
                # Erase ran, reported success, changed nothing.  True of BOTH a region with
                # no memory behind it and a genuine erase failure on real flash -- so ask
                # which: is this the controller's own "nothing here" answer?  Anything else
                # is a failure and must stop the run rather than be waved through.
                current = k.mon_read(sec, SECTOR)
                # Ask the controller what it made of that erase.  It is read AFTER the
                # sector, not before, because the read itself is harmless to these bits
                # (only Clear Status and Reset to Read touch them) and because the order
                # keeps the measurement first and the explanation second.
                st_fl = k.flash_status()
                note = flash_status_note(st_fl)
                # PROER means the operation was actively refused by installed protection.
                # That is NOT an absent region, whatever the contents look like, so it is
                # checked first -- a locked sector full of the no-memory pattern would
                # otherwise be waved through as "nothing here".
                if st_fl and st_fl[1] & 0x10:
                    print("[kline]   sector 0x%06X: LOCKED -- the flash controller refused "
                          "the erase (protection error)" % sec)
                    print("[kline]   %s" % note)
                    event("result", what="write", ok=False, written=done, addr=sec,
                          fsr_op=st_fl[0], fsr_prot=st_fl[1],
                          message="the flash controller refused the erase at 0x%06X: "
                                  "write protection" % sec)
                    return False
                if looks_unmapped(current, nothing):
                    wanted = sum(1 for j in range(SECTOR) if current[j] != new[j])
                    unmapped.append((sec, wanted))
                    print("[kline]   sector 0x%06X: no memory behind this address (reads "
                          "%s..., same as an address past the end of flash)"
                          % (sec, current[:6].hex(" ")))
                    if sec in RESERVED:
                        print("[kline]     this one is documented: %s" % RESERVED[sec])
                    if note:
                        print("[kline]     %s" % note)
                    if wanted:
                        print("[kline]     the image has %d byte(s) here; nothing can store "
                              "them on this ECU" % wanted)
                    event("problem", kind="unmapped", addr=sec, image_bytes=wanted,
                          known=RESERVED.get(sec, ""),
                          fsr_op=st_fl[0] if st_fl else None,
                          fsr_prot=st_fl[1] if st_fl else None,
                          detail="no memory behind this address")
                    progress(i, sec)
                    continue
                print("[kline]   sector 0x%06X: ERASE REPORTED OK AND CHANGED NOTHING, on "
                      "memory that exists (%s...) -- stopping" % (sec, current[:8].hex(" ")))
                print("[kline]   that is a failing erase, not an unwritable region")
                if note:
                    print("[kline]   %s" % note)
                event("result", what="write", ok=False, written=done, addr=sec,
                      fsr_op=st_fl[0] if st_fl else None,
                      fsr_prot=st_fl[1] if st_fl else None,
                      message="erase at 0x%06X reported success and changed nothing on "
                              "memory that exists" % sec)
                return False
            print("[kline]   sector 0x%06X: erase did not take (%d of the first %d bytes "
                  "still set) -- stopping" % (sec, len(bad), probe))
            event("result", what="write", ok=False, written=done, addr=sec,
                  message="erase did not take at 0x%06X" % sec)
            return False
        for p in range(0, SECTOR, PAGE):
            st = k.mon_program(sec + p, new[p:p + PAGE], retries=retries)
            if st != MON_OK:
                print("[kline]   page 0x%06X: PROGRAM %s -- stopping"
                      % (sec + p, MON_STATUS.get(st, "0x%02X" % st)))
                event("result", what="write", ok=False, written=done, addr=sec + p,
                      message="programming refused at 0x%06X" % (sec + p))
                return False
        # blank_check_each doubles as "verify byte-exact": reliable mode reads it all
        # back, fast mode has the ECU add it up and send four bytes instead.  Either way
        # a mismatch falls through to the byte-exact read, so the report is always exact.
        mismatch = False
        if blank_check_each:
            back = k.mon_read(sec, SECTOR)
            mismatch = back != new
        else:
            mismatch = k.mon_checksum(sec, SECTOR) != sums16(new)
        if mismatch:
            back = k.mon_read(sec, SECTOR)
            bad = [j for j in range(SECTOR) if back[j] != new[j]]
            print("[kline]   sector 0x%06X: VERIFY FAILED, %d bytes differ (first +0x%X)"
                  % (sec, len(bad), bad[0] if bad else 0))
            print("[kline]   re-run the same command with --resume to continue from here")
            event("result", what="write", ok=False, written=done, addr=sec,
                  differing=len(bad), resumable=True,
                  message="sector 0x%06X did not verify; re-run with resume" % sec)
            return False
        done += 1
        eta = progress(i, sec)
        print("[kline]   sector 0x%06X: erased, programmed, verified   [%d/%d, "
              "%d retries, ETA %d:%02d]"
              % (sec, i + 1, total, k.retries_used, int(eta) // 60, int(eta) % 60))
        # The write measures its own line as it goes: this sector was 32 real pages, and the
        # resends are already counted.  If the rate is not carrying them, step down here --
        # a probe beforehand cannot tell (wrong traffic shape) and cannot stay right anyway
        # (this link drifts several-fold within minutes, measured).
        watched["pages"] += SECTOR // PAGE
        watched["resends"] += k.retries_used - watched["mark"]
        watched["mark"] = k.retries_used
        if slow_down_if_needed(k, watched["resends"], watched["pages"], retries):
            watched.update(pages=0, resends=0, mark=k.retries_used)
    # A region with no memory behind it is not a failed write.  Nothing can be stored there
    # by this tool or any other, on this ECU or with this image, so failing every run over it
    # would make the failure signal meaningless -- and a signal nobody believes is worse than
    # none.  What must NOT happen is silence: it is named, counted, and the bytes the image
    # wanted there are stated, so the decision about whether that matters stays with the
    # person, who is the only one who can know what those bytes were for.
    #
    # The case that DOES fail is handled where it is found, not here: an erase that reports
    # success and changes nothing on memory that exists stops the run at that sector.
    wanted_total = sum(w for _, w in unmapped)
    print("[kline] *** WRITE COMPLETE AND VERIFIED: %d sector(s) written, %d already "
          "correct ***" % (done, skipped))
    if unmapped:
        print("[kline] %d region(s) have no memory behind them and hold nothing: %s"
              % (len(unmapped), ", ".join("0x%06X" % a for a, _ in unmapped)))
        if wanted_total:
            print("[kline] the image has %d byte(s) in those regions.  They are not on the "
                  "ECU and cannot be put there -- established against this unit's own answer "
                  "for an unmapped address, not assumed." % wanted_total)
    event("result", what="write", ok=True, written=done, skipped=skipped,
          unmapped=len(unmapped), unmapped_image_bytes=wanted_total,
          message="every sector written was verified")
    return True


def report_boot_difference(args, whole):
    """Say whether the image's boot sector differs from the one staying in the ECU.

    `--flash` deliberately writes from BOOT_END up, so the boot sector -- with the resident
    CAN loader in it -- is never erased.  That is the right default: it is the one region
    with no copy anywhere, and rewriting it buys nothing for a calibration change.

    But it means a full image is only PARTLY applied, and nothing said so.  Flash an image
    from a different firmware version and the ECU ends up running that version's application
    against its own older loader.  Usually harmless, occasionally not, and never something
    to find out later.  This costs nothing: the pre-flight backup already holds the ECU's
    boot sector, so the answer is a slice comparison against a file already in memory."""
    if not args.flash or len(whole) < BOOT_END - FLASH_BASE:
        return
    try:
        img = open(args.flash, "rb").read()
    except OSError:
        return
    n = BOOT_END - FLASH_BASE
    if len(img) < n:
        return
    if img[:n] == whole[:n]:
        print("[kline] the image's boot sector matches the one already in the ECU "
              "(not written either way)")
        return
    diff = sum(1 for i in range(n) if img[i] != whole[i])
    print("[kline] NOTE: the image's boot sector differs from this ECU's in %d of %d bytes,"
          % (diff, n))
    print("[kline] and it is NOT being written -- 0x%06X..0x%06X keeps what the ECU has now."
          % (FLASH_BASE, BOOT_END - 1))
    print("[kline] The result is this image's application running under the ECU's existing")
    print("[kline] boot loader.  If that is not what you want, --addr with --allow-boot is")
    print("[kline] the deliberate way to write it, and the backup just taken is your way back.")
    # `detail`, not `message`: the other two problem kinds use detail, and DEVELOPING.md
    # calls this vocabulary deliberately tiny.  A third spelling for the same idea is how
    # a contract a front end depends on stops being one.
    event("problem", kind="boot_sector_differs", addr=FLASH_BASE, differing=diff, bytes=n,
          detail="the image's boot sector differs and is not being written")


def run_monitor(k, args, payload, default_out):   # -> True if the job it did succeeded
    """Drive one monitor session.  The stage is already up and already knows, from its
    compiled-in segment mask, what this session is permitted to erase."""
    writing = bool(args.write or args.flash or args.write_selftest is not None)
    if args.no_calibrate:
        cal = {"retries": None}
        if args.fast_baud:
            k.mon_set_baud(args.fast_baud)
    else:
        # an explicit --fast-baud is an instruction, not a suggestion: honour it alone.
        # otherwise start from the mode's target and climb down until the link is good
        # enough for the job -- reads tolerate far more loss than writes do.
        ladder = ((args.fast_baud,) if args.fast_baud
                  else tuple(b for b in RUN_LADDER if b <= MODES[args.mode]["run"]))
        k.learn_baud_const()        # calibrate the rate maths to this unit before using it
        # Kept for BOTH loops -- the write re-decides the rate from what it observes, and a
        # read that comes back short steps down instead of ending the session.  Neither can
        # be decided up front: measured, this link drifts several-fold within minutes.
        k.ladder = ladder
        cal = tune_speed(k, ladder, writing)
    if args.write or args.write_selftest is not None:
        # Ask the silicon what it is before erasing any of it.  Ordered before the read
        # self-test on purpose: the self-test proves the addressing works on THIS part, and
        # that is a different question from whether this part is the one the stages were
        # built for.  A stranger can pass the self-test and still be ruined by the write.
        if not check_identity(k, force=args.force_unknown_ecu):
            event("result", what="write", ok=False, wrote_nothing=True,
                  message="the ECU is not the part this tool was built for; nothing was "
                          "written and nothing erased")
            return False
        # Never erase on the strength of an unverified link: the read self-test proves
        # the command protocol and the flash addressing first, and costs nothing.
        if not monitor_selftest(k):
            print("[kline] read self-test failed -> refusing to write")
            # A RESULT, not a silent return.  This path has just decided that writing would
            # be unsafe -- the addressing does not line up -- and a front end that sees no
            # result at all reads it as "the run died somewhere" and offers to try again.
            event("result", what="write", ok=False, wrote_nothing=True,
                  message="the read self-test failed; nothing was written and nothing erased")
            return False
    if args.write:
        m = MODES[args.mode]
        if (m["backup"] or args.backup) and not args.no_backup:
            if args.resume:
                # On a resume the ECU is PART-WRITTEN, so this backup is a snapshot of a
                # half-finished job, not of the ECU as it was.  The file names are
                # timestamped so the good copy from the first attempt survives -- but it is
                # the OLDER one, and the trouble-shooting text tells people to flash "the
                # backup taken before the write".  Say which this is.
                print("[kline] NOTE: --resume, so this backup is of a PART-WRITTEN ECU.")
                print("[kline] The copy of the original is the one from the first attempt "
                      "-- an earlier timestamp, not the newest file.")
            try:
                _, whole = preflight_backup(k, args.backup_dir)
                # --flash starts at BOOT_END and never touches the boot sector, so if the
                # image carries a DIFFERENT one the result is a hybrid: this image's
                # application under the ECU's existing loader.  That may be exactly right --
                # it is what makes the boot sector safe to leave alone -- but it is not
                # something to discover afterwards.  The backup just read is the ECU's own
                # boot sector, so the comparison is free and needs no extra traffic.
                report_boot_difference(args, whole)
            except IOError as e:
                event("result", what="write", ok=False, wrote_nothing=True,
                      message="the pre-flight backup failed: %s" % e)
                sys.exit("[kline] pre-flight backup FAILED (%s) -- refusing to write. "
                         "A write with no recovery image is how ECUs die." % e)
        # the mode sets a floor; a link measured as lossy gets more retries than that
        retries = max(m["retries"], cal["retries"] or 0)
        ok = write_region(k, args.addr, payload, resume=args.resume,
                          blank_check_each=m["blank_check"], retries=retries)
        if ok and m["final_verify"]:
            ok = final_verify(k, args.addr, payload) and ok
        return ok
    elif args.write_selftest is not None:
        ok = write_selftest(k, args.write_selftest, sector=args.sector,
                            outdir=args.backup_dir)
        # This command erases and programs a live ECU, and emitted no result at all -- so
        # by the project's own contract ("absence of results is not success") a front end
        # could not tell a proven write path from a failed one except by the exit code.
        event("result", what="write-selftest", ok=bool(ok), addr=args.write_selftest,
              bytes=SECTOR if args.sector else PAGE,
              message=("erase and program proven, and the original restored" if ok else
                       "the write self-test did not complete"))
        return ok
    elif args.linktest:
        # link_test PRINTS a verdict and RETURNS (tx_events, rx_bad).  Returning True
        # regardless threw that away at the one place a script could read it.  Only the
        # both-directions case fails the status: an asymmetric link is this bench's normal
        # state and writes still complete over it, so failing on that would make the
        # signal useless -- but a link losing bytes BOTH ways is the one verdict that says
        # fix the bench before writing anything.
        tx_events, rx_bad = link_test(k, rounds=args.linktest)
        lossy_both = bool(tx_events) and bool(rx_bad)
        if lossy_both:
            event("result", what="linktest", ok=False, tx_events=tx_events, rx_bad=rx_bad,
                  message="both directions are losing bytes")
        else:
            event("result", what="linktest", ok=True, tx_events=tx_events, rx_bad=rx_bad,
                  message="the link was measured")
        return not lossy_both
    elif args.mon_read is not None:
        n = args.length or 256
        data = k.mon_read(args.mon_read, n, progress=(n > 4096))
        outfile = args.out if args.out != default_out else None
        if outfile:
            # Flushed and fsynced like every other file this tool produces that someone
            # might rely on: --mon-read is how a region gets saved before it is touched.
            with open(outfile, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            print("[kline] read %d bytes from 0x%06X -> %s"
                  % (len(data), args.mon_read, outfile))
        else:
            print("[kline] 0x%06X: %s%s" % (args.mon_read, data[:64].hex(" "),
                                            " ..." if len(data) > 64 else ""))
        ref = load_reference()
        if ref is not None:
            off = args.mon_read - FLASH_BASE
            seg = ref[off:off + len(data)]
            if len(seg) == len(data):
                m = sum(1 for i in range(len(data)) if data[i] == seg[i])
                print("[kline] vs the reference dump: %d/%d match%s"
                      % (m, len(data), "  <-- IDENTICAL" if m == len(data) else ""))
            else:
                print("[kline] the reference dump does not cover 0x%06X..0x%06X, so there "
                      "is nothing to compare against there"
                      % (args.mon_read, args.mon_read + len(data) - 1))
        blank, _ = blank_check(data)
        if blank:
            print("[kline] the whole range reads as 0x00 = erased "
                  "(a safe place to prove the write path)")
        # NOT `len(data) == n`: mon_read either delivers every byte or raises, so that
        # comparison is always true and the "check" it looked like was decoration.  The
        # read got here, so it succeeded; say so plainly rather than dressing it up.
        event("result", what="read", ok=True, addr=args.mon_read, bytes=len(data),
              path=outfile, message="the requested range was read")
        return True
    else:
        # --monitor IS the self-test.  Dropping its verdict meant "addressing is off, do
        # NOT write" exited 0, which is the one message that most needs a non-zero status.
        ok = monitor_selftest(k)
        event("result", what="selftest", ok=bool(ok), wrote_nothing=True,
              message=("the monitor answered and its addressing checks out" if ok else
                       "the monitor's addressing did not check out -- do NOT write"))
        return ok


def exit_status(rv):
    """Turn whatever a branch of _main() returned into a shell exit status.

    This exists because `sys.exit(True)` exits **1**.  bool is a subclass of int, sys.exit
    passes an int straight through as the status, and so the natural-looking `return ok`
    reports every success as a failure and every failure as a success.  _main() has around
    a dozen exit points written over time in three different conventions -- `return 1` for
    failure, a bare `return` for success, and `return <predicate>` -- and the inversion is
    invisible at each individual site because each one reads correctly on its own.

    So normalise here, once, rather than at a dozen call sites where the next branch added
    will get it wrong again:

        None / True  -> 0    the job succeeded (or had no verdict to give)
        False        -> 1    the job ran and did not succeed
        int          -> as-is

    The mapping is pinned by a test.  The premise underneath it -- that `sys.exit(True)`
    exits 1 -- is a documented property of sys.exit and bool, not something re-measured
    against a shell here."""
    if rv is None or rv is True:
        return 0
    if rv is False:
        return 1
    return rv


def main():
    """Entry point.  Exists to guarantee the --progress stream swap is always undone.

    _main() moves the human log onto stderr so stdout can carry nothing but events.  That
    is a mutation of module-global state, and main() gets called more than once per process
    (the GUI runs it per button press, the tests run it per case).  Left unrestored, the
    SECOND call would read an already-swapped sys.stdout and quietly point the event stream
    at the log -- every event would vanish into the text nobody is parsing, which is the
    exact failure this contract exists to prevent."""
    # The return value IS the process exit status -- the console entry point is
    # `sys.exit(main())`.  It used to be 0 for every failure, so `openm74 --flash img --yes
    # && echo ok` printed ok over an ECU that had just been left unwritten.
    # A stop asked for during the LAST run must not kill this one.  The GUI calls main()
    # once per button press in the same process, so this flag has to start clean.
    clear_stop()
    saved = (sys.stdout, STATUS_LOOK, SETTLE_S, BAUD_CONST, REF_BIN)
    # Line-buffer the log, always.  Python switches stdout to BLOCK buffering the moment it
    # is not a terminal, so `openm74 --flash img.bin --yes | tee run.log` -- or any front end
    # reading the pipe -- shows nothing at all for as long as the job takes, and shows
    # nothing whatsoever if the run is then interrupted.  Found the hard way: a bench write
    # was started with its output redirected, produced an EMPTY file, and killing it lost the
    # lot.  A progress log that only arrives after the thing it reported on is not one.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass                        # a stream that cannot be reconfigured still works
    try:
        return exit_status(_main())
    finally:
        # ALL of the mutated globals, not just the stream.  BAUD_CONST is the dangerous one:
        # learn_baud_const() rewrites it from the ECU in front of us, and a second ECU whose
        # own read fails would otherwise inherit the first one's clock -- sending every
        # subsequent rate change to the wrong divider.  The GUI runs main() per button press.
        (sys.stdout, globals()["STATUS_LOOK"], globals()["SETTLE_S"],
         globals()["BAUD_CONST"], globals()["REF_BIN"]) = saved
        globals()["_EVENT_OUT"] = None


def _main():
    ap = argparse.ArgumentParser(description="XC2765X K-line BSL host (cross-platform)")
    ap.add_argument("--port", default=None, help="serial device (auto-detect FTDI if omitted)")
    ap.add_argument("--mode", choices=("reliable", "fast"), default="reliable",
                    help="reliable (default): pre-flight backup, blank-check "
                         "after every erase, full read-back at the end. fast: about a "
                         "third quicker (measured: ~8 min against ~13 for a whole image), "
                         "per-sector verify only -- the backup is taken in both")
    ap.add_argument("--flash", metavar="FILE",
                    help="ONE BUTTON: write a full 832KB image (backup, erase, program, "
                         "verify; boot sector untouched). Needs --yes")
    ap.add_argument("--fast-baud", type=int, default=0, metavar="BAUD",
                    help="once the monitor is up, retune both ends to BAUD by setting the "
                         "USIC divider -- goes past what the BSL autobaud can reach")
    ap.add_argument("--reference", metavar="FILE",
                    help="your own earlier dump of THIS ECU, to compare reads against "
                         "(also settable as OPENM74_REFERENCE). None is shipped: a dump "
                         "is one vehicle's data")
    # Automating the power-cycle needs hardware, and that is a measured conclusion rather
    # than a design preference -- see power_cycle() and docs/PROTOCOL.md §1.
    ap.add_argument("--power-line", choices=("none", "dtr", "rts"), default="none",
                    help="switch the ECU's +12 V through the adapter's DTR or RTS line "
                         "before each run, instead of asking a human to do it.  Needs a "
                         "relay or high-side switch wired to that pin; asserted = powered")
    ap.add_argument("--power-port", default=None, metavar="DEV",
                    help="serial device whose handshake pin drives the relay (default: the "
                         "K-line adapter itself -- but MEASURED, a K+DCAN does not bring "
                         "those pins out, so this usually wants a separate cheap dongle)")
    ap.add_argument("--power-off-ms", type=int, default=400, metavar="MS",
                    help="how long the supply stays off during an automatic power-cycle")
    ap.add_argument("--power-settle-ms", type=int, default=300, metavar="MS",
                    help="pause after restoring power before the first byte goes out")
    ap.add_argument("--power-invert", action="store_true",
                    help="the switch is wired the other way round: asserted = OFF")
    ap.add_argument("--progress", choices=("human", "json"), default="human",
                    help="json: one machine-readable event per line on stdout, human log to "
                         "stderr -- so a front end never has to parse prose to know how far "
                         "along it is or whether it worked")
    ap.add_argument("--force-unknown-ecu", action="store_true",
                    help="write even though the ECU does not identify as the part this "
                         "tool was built for.  Reading never needs this.")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="skip the per-session link measurement and use fixed timeouts")
    ap.add_argument("--linktest", type=int, nargs="?", const=150, default=0, metavar="N",
                    help="measure K-line quality in BOTH directions and say what to fix; "
                         "writes nothing. Run this first when bringing up a new bench")
    ap.add_argument("--backup", action="store_true",
                    help="force the pre-flight backup even in fast mode -- the single "
                         "most valuable safety step, and cheap next to a write")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the pre-flight backup in reliable mode (not advised)")
    # The backup must land somewhere writable, and the current directory is not always
    # that: a GUI bundle launched from a desktop starts with its working directory at the
    # filesystem root, where the write fails and the whole run is refused.  A terminal
    # never shows this, which is why it needs to be sayable rather than assumed.
    ap.add_argument("--backup-dir", default=".", metavar="DIR",
                    help="where the pre-flight backup is written (default: alongside you, "
                         "in the current directory)")
    ap.add_argument("--baud", type=int, default=None,
                    help="override the baud the mode would pick")
    ap.add_argument("--scan-baud", action="store_true")
    ap.add_argument("--tx-probe", action="store_true", help="confirm TBUF00 by streaming 0x55")
    ap.add_argument("--flash-probe", action="store_true",
                    help="read flash 0xC00000+ and compare to the reference dump")
    ap.add_argument("--out", default="m74_flash_dump.bin", help="output file for --dump")
    ap.add_argument("--stitch", action="store_true",
                    help="combine seg_C0..CC.bin (next to --out) into the full 832KB --out")
    ap.add_argument("--oneshot", action="store_true",
                    help="2-stage ONE-SHOT dump: whole 832KB in a single pass, no power-cycling")
    ap.add_argument("--limit", type=lambda s: int(s, 0), default=0,
                    help="with --oneshot: stop after N bytes "
                         "(e.g. 8192 for a quick check; 0 = full)")
    ap.add_argument("--verify", metavar="FILE",
                    help="offline: compare FILE against the reference dump")
    # --- flash monitor (stage2mon): the write path -------------------------
    ap.add_argument("--monitor", action="store_true",
                    help="start the flash monitor and run its READ-ONLY self-test")
    ap.add_argument("--mon-read", type=lambda s: int(s, 0), default=None, metavar="ADDR",
                    help="read --len bytes from ADDR through the monitor (read-only)")
    ap.add_argument("--write-selftest", type=lambda s: int(s, 0), default=None, metavar="ADDR",
                    help="prove erase+program on ONE page with a test pattern, then "
                         "restore it (net zero change)")
    ap.add_argument("--write", metavar="FILE",
                    help="program FILE at --addr (sector read-merge-erase-program-verify)")
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=None,
                    help="flash address for --write / --mon-read")
    ap.add_argument("--len", dest="length", type=lambda s: int(s, 0), default=0,
                    help="byte count for --mon-read, or a shorter slice of --write FILE")
    ap.add_argument("--resume", action="store_true",
                    help="with --write: read each sector first and skip the ones already "
                         "correct, so an interrupted write continues instead of restarting")
    ap.add_argument("--settle", type=float, default=None, metavar="MS",
                    help="pause before each host transmission, in ms (ISO 14230 P2); "
                         "6 ms measured no better than 2 -- knob kept for experiments")
    ap.add_argument("--sector", action="store_true",
                    help="with --write-selftest: exercise the 4KB SECTOR erase and the "
                         "32-page programming loop that --write is built on")
    ap.add_argument("--image", action="store_true",
                    help="--write FILE is a full 832KB image: take the bytes at --addr's offset")
    ap.add_argument("--yes", action="store_true",
                    help="required to actually erase/program -- without it writes are refused")
    ap.add_argument("--allow-boot", action="store_true",
                    help="permit writes below 0x%06X (the resident CAN bootloader)" % BOOT_END)
    ap.add_argument("--echo-test", action="store_true",
                    help="diagnostic: is the U0C0 RX path working?")
    ap.add_argument("--rearm-test", action="store_true",
                    help="diagnostic: does a watchdog reset re-arm the BSL?")
    ap.add_argument("--rx-read", action="store_true",
                    help="diagnostic: does the RBUF read return the correct byte?")
    ap.add_argument("--diswdt", action="store_true",
                    help="diagnostic: can DISWDT disable the watchdog?")
    args = ap.parse_args()

    # Done before anything prints.  One swap moves every existing print() onto the log
    # stream and keeps the real stdout for events, so no call site has to know about this.
    #
    # Assigned unconditionally, including the None: main() can be called more than once in
    # one process (the GUI does exactly that, and so do the tests), and a stale _EVENT_OUT
    # from a previous run would keep writing events into a stream nobody is reading.
    globals()["_EVENT_OUT"] = sys.stdout if args.progress == "json" else None
    if args.progress == "json":
        sys.stdout = sys.stderr

    if args.reference:
        globals()["REF_BIN"] = args.reference

    if args.baud is None:                  # the mode picks the line rate, not the user
        args.baud = MODES[args.mode]["baud"]

    if args.settle is not None:
        globals()["SETTLE_S"] = args.settle / 1000.0

    if args.verify:                                    # no serial port needed
        # The exit status matters most on THIS path: --verify exists to be a gate in
        # somebody's script, and a gate that exits 0 whatever it finds is not a gate.
        return compare_report(open(args.verify, "rb").read(),
                              os.path.basename(args.verify))

    if args.stitch:                                    # no serial port needed
        d = os.path.dirname(os.path.abspath(args.out))
        full = bytearray(); ok = True
        for seg in range(SEG_LO, SEG_HI + 1):
            fn = os.path.join(d, "seg_%02X.bin" % seg)
            try:
                data = open(fn, "rb").read()
            except FileNotFoundError:
                print("[kline] MISSING %s -> dump it first" % fn); ok = False; data = b""
            if len(data) != 65536:
                print("[kline] seg %02X: %d bytes (expected 65536)" % (seg, len(data))); ok = False
            full += data
        ok = ok and len(full) == FLASH_SIZE
        if not ok:
            # Do NOT write.  A missing segment contributes b"", so every later segment
            # shifts down by 64 KB and the result is a plausible-looking file that is
            # wrong everywhere after the gap.  Writing it destroyed whatever was at
            # --out -- and the most likely thing there is the user's previous COMPLETE
            # dump, which is exactly the file they would reach for next.  --oneshot was
            # hardened against this with a .part file and a rename; this was not.
            print("[kline] stitched image is INCOMPLETE (%d of %d bytes) -- NOT writing "
                  "%s" % (len(full), FLASH_SIZE, args.out))
            print("[kline] a gap shifts every later segment down, so the file would be "
                  "wrong from the gap onwards; dump the missing segments and re-run.")
            event("result", what="stitch", ok=False, bytes=len(full), path=args.out,
                  message="segments are missing; no file was written")
            return False
        tmp = args.out + ".part"
        with open(tmp, "wb") as f:
            f.write(bytes(full))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, args.out)
        print("[kline] stitched %d bytes -> %s  [COMPLETE]" % (len(full), args.out))
        event("result", what="stitch", ok=True, bytes=len(full), path=args.out,
              message="every segment was present and the image is full length")
        return True

    port = find_port(args.port)

    if args.scan_baud:
        for b in (9600, 19200, 38400, 10400, 4800, 57600, 115200):
            k = KlineBSL(port, b, verbose=False); k.open()
            ok = k.handshake(); k.close()
            print("[kline] baud=%6d  %s" % (b, "0xD5 ALIVE" if ok else "-"))
        return

    k = KlineBSL(port, args.baud)
    k.allow_boot = bool(args.allow_boot)
    # Ctrl-C should ask a write to stop at a sector boundary, not raise between an erase
    # and the programming that refills it.  Installed before the port opens so there is no
    # window where the old behaviour applies.
    prev_sigint = stop_on_sigint(bool(args.write or args.flash
                                      or args.write_selftest is not None))
    try:
        k.open()
    except IOError as e:
        # open() raises with a diagnosis that took a bench session to write.  Letting it
        # escape printed a stack trace ABOVE that diagnosis, which buries the one part the
        # user can act on under fifteen lines they cannot.  Nothing has been sent to the
        # ECU at this point -- the port never opened -- so there is nothing to report but
        # the failure itself.
        print("[kline] %s" % e)
        # One sentence in the event, the three paragraphs of advice in the log.  str(e) here
        # is a multi-line remedy complete with "[kline]" prefixes, and pouring that into a
        # JSON `message` gives a front end something it can only print raw -- the opposite
        # of why the event stream exists.
        event("result", what="open", ok=False, port=port,
              reason="port_unconfigurable",
              message="the serial port opened but refuses every configuration")
        return 1
    try:
        # Before the handshake, because the point of it is to give the ROM loader the
        # power-on reset it insists on -- and the autobaud then measures our first byte.
        if args.power_line != "none":
            k.power_cycle(args.power_line, args.power_off_ms, args.power_settle_ms,
                          args.power_invert, args.power_port)
            event("stage", stage="power-cycled", line=args.power_line,
                  port=args.power_port or port)
        if not k.handshake():
            print("[kline] no 0xD5 -> power-cycle the ECU (A4/B2 asserted) and retry")
            event("result", what="handshake", ok=False,
                  message="the ECU did not answer; power-cycle it with A4/B2 asserted")
            return 1
        print("[kline] BSL alive (0xD5).")
        event("stage", stage="handshake", ok=True, baud=args.baud)
        if args.tx_probe:
            print("[kline] uploading 32B TX-probe (streams 0x55; RAM-only, harmless)...")
            buf = k.capture(TX_PROBE, 2.0)
            body = buf[32:] if len(buf) > 32 else buf     # drop the echoed stub
            print("[kline] captured %d bytes; 0x55 after echo = %d; sample %s"
                  % (len(buf), body.count(0x55), buf[:48].hex(" ")))
            print("[kline] %s" % ("TBUF00=0x4080 CONFIRMED, TX works"
                  if body.count(0x55) >= 4 else "no 0x55 stream -> iterate"))
            streams = body.count(0x55) >= 4
            event("result", what="tx-probe", ok=streams, wrote_nothing=True,
                  message="the transmit path %s" % ("streams" if streams else "does not"))
            return streams
        elif args.flash_probe:
            print("[kline] uploading 32B FLASH-probe (reads 0xC00000+, SRVWDT; RAM-only)...")
            buf = k.capture(FLASH_PROBE, 3.0)
            body = buf[32:] if len(buf) > 32 else buf     # drop the echoed stub
            cap = os.path.join(args.backup_dir or ".", "flashprobe_capture.bin")
            with open(cap, "wb") as f:
                f.write(buf)
                f.flush()
                os.fsync(f.fileno())
            print("[kline] captured %d bytes (after echo %d) -> %s"
                  % (len(buf), len(body), cap))
            print("[kline] flash bytes: %s" % body[:64].hex(" "))
            ref = load_reference()
            works = None
            if ref is not None:
                ref = ref[:max(1, len(body))]
                n = min(len(body), len(ref))
                m = sum(1 for i in range(n) if body[i] == ref[i])
                print("[kline] reference : %s" % ref[:64].hex(" "))
                print("[kline] match vs reference first %d bytes: %d/%d (%.0f%%)"
                      % (n, m, n, (100.0 * m / n) if n else 0))
                works = bool(n) and m >= n * 0.9
                if works:
                    print("[kline] *** FLASH READ WORKS (matches the reference dump) "
                          "-> build full dumper ***")
                elif len(set(body[:64])) <= 2:
                    print("[kline] flash reads all-same (0xFF/0x00?) -> addressing off; "
                          "iterate")
                else:
                    print("[kline] differs from the reference -> read-addr off, or a "
                          "modified region")
            else:
                print("[kline] with no reference there is nothing to compare against, so "
                      "this probe can only show you the bytes.")
            event("result", what="flash-probe", ok=bool(works), wrote_nothing=True,
                  path=cap, message=("flash reads correctly" if works else
                                     "flash did not read as the reference expects"
                                     if ref is not None else
                                     "no reference set, so nothing was verified"))
            return bool(works)
        elif args.oneshot:
            want = args.limit or FLASH_SIZE
            # never clobber the reference dump by accident: own default filename
            outfile = args.out if args.out != ap.get_default("out") else "m74_oneshot.bin"
            delay = pacing_delay(args.baud)
            rate = 610.0 * args.baud / BASE_BAUD    # measured 610 B/s @9600, scales with baud
            print("[kline] ONE-SHOT dump: stage-1 (32B receiver) + stage-2 (2KB, pacing 0x%04X)"
                  % delay)
            print("[kline] reading %d bytes in a single pass (~%.0f min @%d baud); READ-ONLY"
                  % (want, want / rate / 60.0, args.baud))
            buf = k.oneshot(want, stage2=build_stage2(delay))
            body, note = split_oneshot(buf)
            print("[kline] captured %d bytes; sync: %s; payload %d bytes"
                  % (len(buf), note or "PREAMBLE NOT FOUND", len(body)))
            if note is None:
                print("[kline] no 0xA5 sync run -> stage-1 did not hand off to stage-2;")
                print("[kline] bytes at the nominal offset: %s"
                      % buf[ONESHOT_ECHO:ONESHOT_ECHO + 32].hex(" "))
                raw = os.path.join(args.backup_dir or ".", "oneshot_raw.bin")
                open(raw, "wb").write(buf)
                print("[kline] raw capture saved to %s for analysis" % raw)
                print("[kline] NO DUMP WAS PRODUCED. Power-cycle and run the same command "
                      "again.")
                # A result, and a non-zero status.  This branch is the TOTAL failure -- not
                # one byte of flash came back and no output file exists -- and it used to
                # fall off the end of _main() and exit 0, while the lesser failure below
                # (a short dump, which at least leaves a file) exited 1.  So the shell
                # chain the comment below warns about proceeded on the WORSE of the two.
                event("result", what="read", ok=False, bytes=0, path=raw,
                      message="stage-1 never handed off; no dump was produced")
                return False
            else:
                body = body[:want]
                # Written under a temporary name and moved into place only once the length
                # is right.  Writing straight to the chosen file destroyed whatever was
                # there the moment a stream stalled -- and the name the GUI offers by
                # default is the same every time, so the file most likely to be sitting
                # there is the user's previous, complete dump.
                tmp = outfile + ".part"
                with open(tmp, "wb") as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                if len(body) == want:
                    # os.replace, not remove-then-rename.  Between those two calls the
                    # user's previous complete dump does not exist at all, and if the
                    # rename then fails they have neither file.  replace() is atomic on
                    # POSIX and on Windows and needs no unlink first.
                    os.replace(tmp, outfile)
                    print("[kline] wrote %d bytes -> %s" % (len(body), outfile))
                else:
                    outfile = tmp
                    print("[kline] short read kept as %s; %s was left untouched"
                          % (tmp, args.out))
                compare_report(body, outfile)
                import hashlib
                digest = hashlib.sha256(body).hexdigest().upper()
                if len(body) == FLASH_SIZE:
                    print("[kline] *** ONE-SHOT 832KB DUMP COMPLETE -- no power-cycling needed ***")
                    event("result", what="read", ok=True, bytes=len(body), path=outfile,
                          sha256=digest, message="the whole image was read in one pass")
                elif len(body) < want:
                    print("[kline] SHORT by %d bytes -> the stream stalled; retry"
                          % (want - len(body)))
                    event("result", what="read", ok=False, bytes=len(body), path=outfile,
                          sha256=digest, short_by=want - len(body),
                          message="the stream stalled and the dump is incomplete")
                else:
                    event("result", what="read", ok=True, bytes=len(body), path=outfile,
                          sha256=digest, message="requested range read")
                # The shell gets the same verdict the event stream does.  Every branch that
                # can print a failure used to fall off the end of _main() and exit 0, so
                # `openm74 --oneshot --out backup.bin && openm74 --flash new.bin --yes`
                # would write over an ECU whose only backup was a truncated file.
                return len(body) >= want
        elif args.monitor or args.mon_read is not None or args.write_selftest is not None \
                or args.write or args.linktest or args.flash:
            delay = pacing_delay(args.baud)
            default_out = ap.get_default("out")
            payload = b""
            if check_monitor_blob() is False:
                print("[kline] WARNING: embedded monitor != the shipped stage2mon.bin "
                      "(re-assemble or refresh MONITOR_HEX)")

            # --flash is the one-button form: a whole image, at the only address it can
            # go, with the boot sector left alone.  Everything else is the toolkit.
            if args.flash:
                img = open(args.flash, "rb").read()
                if len(img) != FLASH_SIZE:
                    sys.exit("[kline] --flash wants a full %d-byte image; %s is %d bytes"
                             % (FLASH_SIZE, args.flash, len(img)))
                # --flash is the WHOLE image at the ONE address it can go.  Silently
                # overriding a typed --addr/--len/--image would erase 206 sectors for
                # someone who asked for one -- the same silent cap that --write was just
                # taught to refuse, left open on the button people reach for first.
                ignored = [n for n, v in (("--addr", args.addr), ("--len", args.length),
                                          ("--image", args.image)) if v]
                if ignored:
                    sys.exit("[kline] --flash writes the whole image to 0x%06X and takes no "
                             "%s.  Use --write for a partial write; --flash would have "
                             "erased everything above the boot sector."
                             % (BOOT_END, " or ".join(ignored)))
                args.write, args.addr = args.flash, BOOT_END
                payload = img[BOOT_END - FLASH_BASE:]
                segmask = segmask_for(args.addr, len(payload), args.allow_boot)
                print("[kline] flashing %s: 0x%06X..0x%06X (%d bytes, boot sector left "
                      "untouched)" % (os.path.basename(args.flash), args.addr,
                                      args.addr + len(payload) - 1, len(payload)))
            # Work out what this session is allowed to write BEFORE starting it: the
            # permission is compiled into the stage, so a read-only session literally
            # cannot erase, and a write session reaches only the range you asked for.
            elif args.write:
                if args.addr is None:
                    sys.exit("[kline] --write needs --addr")
                blobf = open(args.write, "rb").read()
                if args.image:
                    off = args.addr - FLASH_BASE
                    if off < 0 or off >= len(blobf):
                        sys.exit("[kline] --addr 0x%06X is outside the %d-byte image"
                                 % (args.addr, len(blobf)))
                    payload = blobf[off:off + (args.length or (len(blobf) - off))]
                else:
                    payload = blobf[:args.length] if args.length else blobf
                # --len larger than the file used to write whatever there was and report
                # success, which is a silent cap on the one number the user typed.  Refuse
                # instead: asking to write N bytes and being given fewer is a mistake worth
                # stopping for, not rounding down.
                if args.length and len(payload) < args.length:
                    sys.exit("[kline] --len asks for %d bytes but %s only supplies %d from "
                             "0x%06X.  Refusing rather than writing less than you asked "
                             "for." % (args.length, os.path.basename(args.write),
                                       len(payload), args.addr))
                segmask = segmask_for(args.addr, len(payload), args.allow_boot)
            elif args.write_selftest is not None:
                segmask = segmask_for(args.write_selftest,
                                     SECTOR if args.sector else PAGE, args.allow_boot)
            else:
                segmask = 0            # read-only session: erase/program refused in-stage

            print("[kline] monitor: pacing 0x%04X, writable segments: %s"
                  % (delay, mask_str(segmask)))
            if segmask and not args.yes:
                sys.exit("[kline] this would ERASE flash -- re-run with --yes once you "
                         "have a backup you trust")
            ok, note = k.start_monitor(segmask, delay)
            print("[kline] handoff: %s" % note)
            # The oneshot path announced its handoff and this one did not, so a front end
            # saw nothing for the seconds it takes to deliver 2 KB and read it back --
            # which is exactly when it should be saying what it is doing.
            event("stage", stage="handoff", ok=bool(ok), detail=note,
                  segments=mask_str(segmask))
            if not ok:
                # Two ways to get here, and `note` says which.  The stage goes across with no
                # checksum and no retry -- stage-1 is 32 bytes and has no room for either --
                # so a corrupted byte usually means the monitor simply never greets us.  The
                # other way is the greeting arriving from a stage that is NOT what was sent,
                # caught by reading it back; that one would previously have gone unnoticed.
                # Neither damages anything: no erase has been armed and none can be, because
                # the only thing that could arm one is the stage we have just refused.
                print("[kline] the 2KB stage did not arrive intact.")
                print("[kline] Nothing was written and nothing is damaged. Power-cycle "
                      "and run the same command again.")
                print("[kline] If this keeps happening the link has degraded: check the "
                      "connector seating, the ground lead and the K-line wire.")
                event("result", what="handoff", ok=False, wrote_nothing=True,
                      message="the 2 KB stage did not arrive; nothing was written or erased")
                return 1
            try:
                ok = run_monitor(k, args, payload, default_out)
                print("[kline] link quality: %d retry/resync event(s) this session "
                      "(settle %d ms)" % (k.retries_used, int(SETTLE_S * 1000)))
                if ok is False:
                    return 1
            except IOError as e:
                print("[kline] link quality: %d retry/resync event(s) before the failure"
                      % k.retries_used)
                print("[kline] LINK LOST: %s" % e)
                # A result, not just a problem: this ends the operation, and a front end
                # that saw no result must not conclude anything went well.
                event("result", what="link", ok=False, retries=k.retries_used,
                      resumable=True, message="the link was lost: %s" % e)
                print("[kline] the ECU is still sitting in the monitor; power-cycle it "
                      "(A4/B2 asserted) and re-read the affected range before writing again")
                return 1
        elif args.echo_test:
            print("[kline] echo-server: sending bytes; each should return TWICE (hw + sw echo)...")
            k.ser.reset_input_buffer()
            k.upload32(ECHO_SERVER)
            time.sleep(0.2)
            k.ser.timeout = 0.3
            k.ser.read(64)                       # drain the 32B upload echo
            tests = [0x42, 0xA5, 0x3C, 0x81, 0x18, 0xE7]
            ok = 0
            for tb in tests:
                k.ser.reset_input_buffer()
                k.ser.write(bytes([tb])); k.ser.flush()
                time.sleep(0.08)
                r = k.ser.read(8)
                cnt = r.count(tb)
                good = cnt >= 2
                ok += 1 if good else 0
                print("[kline]   sent %02X -> got %s  (%s)" % (
                    tb, r.hex(" ") or "(none)",
                    "RX+TX OK" if good
                    else ("hw-echo only -> RX NOT working" if cnt == 1 else "no echo")))
            works = ok >= len(tests) - 1
            print("[kline] %s" % (
                "*** RECEIVE PATH WORKS -> the handoff / stage-2 placement is the bug ***"
                if works else
                "*** RECEIVE PATH BROKEN -> RBUFSR poll is wrong ***"))
            # A bring-up diagnostic's verdict IS its result, so the shell gets it too:
            # these all printed a failure and exited 0, which makes them useless in a script.
            event("result", what="echo-test", ok=works, wrote_nothing=True,
                  message="the receive path %s" % ("works" if works else "is broken"))
            return works
        elif args.rearm_test:
            # This settles whether the manual power-cycle in front of every operation is
            # actually required, so it has to be able to answer NO for the right reason.
            # It used to make ONE handshake attempt 1.4 s after the upload, while --dump,
            # which depends on the same behaviour, polls eight times -- an impatient probe
            # reporting failure would have killed the idea on a false negative.  So: watch
            # for the 0x55 stream to STOP (that is the reset actually happening, not an
            # assumption about when), then poll as patiently as the dependent code does.
            print("[kline] watchdog auto-rearm test: upload a stub that stops feeding the")
            print("[kline] watchdog, watch for the reset, then look for a re-armed BSL.")
            print("[kline] READ-ONLY: the stub only writes the serial transmit buffer.")
            k.ser.reset_input_buffer()
            k.upload32(TX_PROBE)               # streams 0x55, no SRVWDT -> watchdog reset
            k.ser.timeout = 0.2
            t0 = time.time()
            total, last_rx, quiet_since = 0, None, None
            while time.time() - t0 < 6.0:
                chunk = k.ser.read(4096)
                now = time.time()
                if chunk:
                    total += len(chunk); last_rx = now; quiet_since = None
                elif last_rx is not None:
                    if quiet_since is None:
                        quiet_since = now
                    elif now - quiet_since > 0.6:
                        break                  # the stream has genuinely stopped
            stopped = (last_rx - t0) if last_rx else None
            print("[kline] stub streamed %d bytes, went quiet after %s"
                  % (total, "%.2f s" % stopped if stopped else "never started"))
            if not total:
                print("[kline] the stub never ran -- this says nothing about re-arming; "
                      "check the handshake first")
            ok, when = False, 0.0
            for attempt in range(12):
                time.sleep(0.3)
                if k.handshake():
                    ok, when = True, 0.3 * (attempt + 1)
                    break
            print("[kline] %s" % (
                "*** WATCHDOG AUTO-REARM WORKS: the BSL answered %.1f s after the stream "
                "stopped -- the ECU can be reset in software ***" % when if ok
                else "auto-rearm FAILED: no 0xD5 in %.1f s of polling -> a physical "
                     "power-cycle is required" % 3.6))
            event("result", what="rearm-test", ok=bool(ok), wrote_nothing=True,
                  message="the loader %s after a watchdog reset"
                          % ("came back" if ok else "did not come back"))
            return bool(ok)
        elif args.diswdt:
            print("[kline] DISWDT test: streaming 0x55 with the watchdog DISABLED (no SRVWDT)...")
            buf = k.capture(DISWDT_TEST, 2.0)
            body = buf[32:] if len(buf) > 32 else buf
            n55 = body.count(0x55)
            print("[kline] captured %d bytes; 0x55 after echo = %d; sample %s"
                  % (len(buf), n55, buf[:48].hex(" ")))
            took = n55 >= 50
            if took:
                print("[kline] *** DISWDT WORKS -> watchdog off; drop SRVWDT everywhere "
                      "(receiver fits) ***")
            else:
                print("[kline] DISWDT did NOT take (%d x 0x55, reset) -> watchdog still "
                      "on, must feed SRVWDT" % n55)
            event("result", what="diswdt", ok=took, wrote_nothing=True,
                  message="the watchdog %s be disabled" % ("can" if took else "cannot"))
            return took
        elif args.rx_read:
            print("[kline] RX-read probe: upload, send 0x5A, expect a clean 0x5A stream back...")
            k.ser.reset_input_buffer()
            k.upload32(RX_READ)
            time.sleep(0.15)
            k._tx(bytes([0x5A]))
            buf = b""; deadline = time.time() + 2.0
            while time.time() < deadline:
                buf += k.ser.read(512)
            body = buf[32:] if len(buf) > 32 else b""     # drop the 32B stage echo
            n5a = body.count(0x5A)
            ratio = (n5a / len(body)) if body else 0.0
            print("[kline] captured %d bytes; stream sample: %s" % (len(buf), body[:32].hex(" ")))
            print("[kline] 0x5A in stream: %d/%d (%.0f%%)" % (n5a, len(body), 100 * ratio))
            correct = ratio > 0.7
            if correct:
                print("[kline] *** RBUF READ CORRECT -> RX read works; the 2-stage bug "
                      "was timing/count, not the read ***")
            elif body:
                mc = max(set(body), key=body.count)
                print("[kline] stream is NOT 0x5A (most common 0x%02X) -> RBUF read/poll "
                      "wrong; dig deeper" % mc)
            else:
                print("[kline] no stream -> stage didn't run / RX poll stuck")
            event("result", what="rx-read", ok=correct, wrote_nothing=True,
                  message="the receive buffer %s the byte that was sent"
                          % ("returns" if correct else "does not return"))
            return correct
    except KeyboardInterrupt:
        # Ctrl-C during a write is a real thing people do, and it used to drop the port
        # mid-transfer with nothing said.  Two things matter here.  What the ECU is: still
        # in the monitor, possibly counting payload bytes that will now never arrive -- so
        # feed it filler and let it fail that page's checksum, which is how it stops
        # counting without anything reaching flash.  And what the user has: a partly
        # written image and a backup, and the right next command is --resume, which reads
        # every sector and rewrites only the wrong ones.
        print()
        print("[kline] INTERRUPTED.  Letting the ECU finish what it was doing and closing "
              "the port cleanly -- do not unplug anything yet.")
        if getattr(k, "monitor_up", False):
            # ONLY with a monitor listening.  _resync feeds 0xA5 filler to unstick a monitor
            # counting payload bytes -- but a Ctrl-C during the handshake leaves the mask-ROM
            # loader counting its 32-byte stage instead, and filling that with 0xA5 hands the
            # ROM 32 bytes of nonsense to jump into.  Nothing there can form a flash command,
            # so it is a trap or a reset rather than damage; it is still the opposite of
            # "letting the ECU finish what it was doing".
            try:
                k._resync("interrupt")
            except BaseException:
                pass            # including a second Ctrl-C; close() drains and resets anyway
        print("[kline] If a write was under way the image is PART-WRITTEN.  Power-cycle "
              "and re-run the same command with --resume: it reads every sector and")
        print("[kline] rewrites only the ones that are wrong.  The pre-flight backup is "
              "the file to go back to if you would rather start over.")
        # `what` follows the operation, not the handler's location.  This clause covers
        # every branch of _main(), including read-only ones whose stage carries a segment
        # mask of zero and physically cannot erase -- telling a front end that THOSE may
        # have part-written an ECU is a false alarm about damage.
        was_writing = bool(args.write or args.flash or args.write_selftest is not None)
        event("result", what="write" if was_writing else "read", ok=False, interrupted=True,
              wrote_nothing=not was_writing,
              message=("interrupted by the operator; the image may be part-written"
                       if was_writing else
                       "interrupted by the operator; this operation writes nothing"))
        return 1
    finally:
        k.close()
        if prev_sigint is not None:
            import signal
            try:
                signal.signal(signal.SIGINT, prev_sigint)
            except (ValueError, OSError):
                pass        # same reasons stop_on_sigint may decline to install one


if __name__ == "__main__":
    # sys.exit, not a bare call: main() RETURNS the status, and the installed console script
    # is generated as sys.exit(main()) -- so running the module directly was the one entry
    # point that still threw the exit code away.  Caught on the bench, where a self-test that
    # correctly reported failure exited 0.
    sys.exit(main())
