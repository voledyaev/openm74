#!/usr/bin/env python3
"""Bench probe: ask the MCU what it is, and which bootloader it actually started.

READ-ONLY BY CONSTRUCTION.  The monitor is started with a segment mask of ZERO, so the
uploaded stage physically cannot erase or program anything.  Reads damage nothing.

    python tools/probe_identity.py /dev/cu.usbserial-XXXX

Two questions, both answered by registers the part maintains about itself.

**What is this chip.**  `IDCHIP`, `IDMANUF`, `IDMEM` and `IDPROG` are identification
registers -- the device reporting its own identity rather than us inferring it from
behaviour.  That matters for supporting other ECUs: this tool's stages are compiled for one
memory map, and uploading them into a different part would run code at addresses that mean
something else there.  A check built on these registers cannot mistake a real M74 CAN for
something else, because it is not guessing.

**Which bootloader the ROM chose.**  The mask ROM runs exactly ONE loader, selected by the
level on P10.[3:0] latched at power-on (XC2000 System Units UM V1.0, Table 10-1).  If the
pattern this ECU presents selects the UART loader, then the part is listening on the K-line
pin and on nothing else -- and no amount of CAN traffic can reach it, whatever the timing.
`STSTAT.HWCFG` says which one was chosen, so this stops being an argument and becomes a
reading.

`SWRSTCON` is read too, for completeness: it is the documented way for running code to boot
the part into a DIFFERENT mode (write the configuration into SWCFG, set SWBOOT, request a
software reset).  Nothing here writes it.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))
from openm74 import klinebsl as K

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

# ESFR area, memory-mapped at 00'F000H-00'F1FFH (UM Table 3-1), so an ordinary data read
# reaches these; no special addressing is needed and the monitor's READ works unchanged.
REGS = [
    ("IDMANUF   manufacturer",        0x00F07E),
    ("IDCHIP    chip identification", 0x00F07C),
    ("IDMEM     program memory",      0x00F07A),
    ("IDPROG    programming voltage", 0x00F078),
    ("STSTAT    start-up status",     0x00F1E0),
    ("SWRSTCON  software reset ctl",  0x00F0AE),
]

# Table 10-1 gives the start-up mode as the pattern on P10.[3:0]; STSTAT.HWCFG reports it
# back.  Keyed on the low four bits ONLY: this ECU answers 0x46, whose documented part is
# 0110 -- the UART loader -- with bit 6 also set, and Table 10-1 does not cover that bit.
# Matching the whole byte would have called a perfectly clear answer "unknown", which is
# exactly what the first version of this file did.
MODES = {
    0x3: ("internal start from flash", "the application is running; no loader"),
    0x6: ("STANDARD UART BOOTLOADER", "listening on P7.4 (RxD) -- the K-line pin"),
    0x5: ("CAN BOOTLOADER", "listening on P2.6 (RxDC0)"),
    0x9: ("SSC bootloader", "listening on P2.4 (MRST)"),
    0x0: ("external start", "executing from off-chip memory"),
}


def decode(seen):
    """Turn the raw registers into what they say, per UM section 6 and Table 10-1."""
    out = []
    v = seen.get(0x00F07E)
    if v is not None:
        out.append("manufacturer : JEDEC code 0x%02X%s, section 0x%02X"
                   % (v >> 5, " (Infineon)" if (v >> 5) == 0xC1 else "", v & 0x1F))
    v = seen.get(0x00F07C)
    if v is not None:
        out.append("chip         : CHIPID 0x%02X, revision step 0x%02X" % (v >> 8, v & 0xFF))
    v = seen.get(0x00F07A)
    if v is not None:
        kind = {0x3: "flash"}.get(v >> 12, "type 0x%X" % (v >> 12))
        blocks = v & 0xFFF
        out.append("program mem  : %s, %d blocks x 4 KB = %d KB (0x%X bytes)%s"
                   % (kind, blocks, blocks * 4, blocks * 4096,
                      "  <- matches this tool's FLASH_SIZE"
                      if blocks * 4096 == K.FLASH_SIZE else
                      "  <- DOES NOT match this tool's FLASH_SIZE of 0x%X" % K.FLASH_SIZE))
    return out


def main():
    k = K.KlineBSL(PORT, 9600)
    k.open()
    try:
        if not k.handshake():
            sys.exit("no 0xD5 -- power-cycle the ECU (A4/B2 asserted) and run again")
        print("[probe] BSL alive")

        ok, note = k.start_monitor(0, K.pacing_delay(9600))   # segmask 0: read-only stage
        print("[probe] handoff: %s" % note)
        if not ok:
            sys.exit("the stage did not land; power-cycle and try again")

        print("\n%-30s %-10s %-8s %s" % ("register", "address", "value", "raw"))
        print("-" * 70)
        seen = {}
        for name, addr in REGS:
            try:
                d = k.mon_read(addr, 2)
                v = d[0] | (d[1] << 8)
                seen[addr] = v
                print("%-30s 0x%06X   0x%04X   %s" % (name, addr, v, d.hex(" ")))
            except Exception as e:
                print("%-30s 0x%06X   read failed: %s" % (name, addr, e))
    finally:
        # ALWAYS, not just on the happy path.  Every sys.exit above used to leave the
        # port open mid-transfer -- the ECU may still be handing over a 4 KB answer,
        # and dropping the descriptor there is how a USB serial adapter ends up in a
        # state the next run cannot configure.
        k.close()

    print("\n---- what this part says it is ----")
    for line in decode(seen):
        print(line)

    print("\n---- which loader is running ----")
    st = seen.get(0x00F1E0)
    if st is None:
        print("STSTAT did not read back; nothing can be concluded.")
        return
    hwcfg = st & 0xFF
    mode, where = MODES.get(hwcfg & 0xF, ("UNKNOWN", "not a documented start-up mode"))
    print("STSTAT = 0x%04X, so HWCFG = 0x%02X, and its low four bits are %s -> %s"
          % (st, hwcfg, format(hwcfg & 0xF, "04b"), mode))
    print("   %s" % where)
    if hwcfg & 0xF0:
        print("   (bits 0x%02X above the documented field are also set; Table 10-1 does not"
              % (hwcfg & 0xF0))
        print("    cover them, and they do not change which loader was selected)")
    if (hwcfg & 0xF) == 0x6:
        print()
        print("This is the answer to 'why does it never acknowledge over CAN'.  The ROM runs")
        print("one loader, this one is the UART loader, and it is not listening on CAN at all.")
        print("Frame timing, retry suppression and reset timing cannot change that -- the")
        print("choice was made from P10.[3:0] at power-on and nothing since then reads CAN.")


if __name__ == "__main__":
    main()
