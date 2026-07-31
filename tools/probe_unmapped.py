#!/usr/bin/env python3
"""One-off bench probe: is 0xC0F000 a hole in the address map, or a dead flash sector?

READ-ONLY BY CONSTRUCTION. The monitor is started with a segment mask of ZERO, so the
uploaded stage physically cannot erase or program anything even if this script asked it to.
Reads are ungated because reading damages nothing.

The question this settles: docs/FINDINGS.md justified "hole in the address map" with "the
same constant appears at unimplemented register addresses". If that is right, addresses
outside flash should also read 9b1e. If they read something else, then 9b1e belongs to that
one sector -- which is a dead or locked flash sector, not a gap in the memory map.

One session, many reads, one power cycle.
"""
import os, sys

# Resolved from this file, not from where it was written: an absolute path baked into a
# committed tool works on exactly one machine, and carries that machine's user name into
# a public repository as a bonus.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))
from openm74 import klinebsl as K

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

PROBES = [
    ("the sector in question",      0xC0F000),
    ("its neighbour below",         0xC0E000),
    ("its neighbour above",         0xC10000),
    ("a blank sector (reads 0x00)", 0xC01000),
    ("last bytes of real flash",    0xCCFFE0),
    ("just past the end of flash",  0xCD0000),
    ("further past the end",        0xCE0000),
    ("further still",               0xCF0000),
    ("the baud divider register",   0x20401E),
    ("unimplemented segment 0x30",  0x300000),
]


def main():
    k = K.KlineBSL(PORT, 9600)
    k.open()
    try:
        if not k.handshake():
            sys.exit("no 0xD5 -- power-cycle the ECU (A4/B2 asserted) and run again")
        print("[probe] BSL alive")

        # segmask=0: a read-only stage.  Erase and program are refused inside the ECU itself.
        ok, note = k.start_monitor(0, K.pacing_delay(9600))
        print("[probe] handoff: %s" % note)
        if not ok:
            sys.exit("the stage did not land; power-cycle and try again")

        print("\n%-30s %-10s %s" % ("what", "address", "first 16 bytes"))
        print("-" * 78)
        results = {}
        for name, addr in PROBES:
            try:
                d = k.mon_read(addr, 16)
                results[addr] = bytes(d)
                uniform = len({d[i:i + 2] for i in range(0, len(d), 2)}) == 1
                print("%-30s 0x%06X   %s%s"
                      % (name, addr, d.hex(" "), "   <- one word repeating" if uniform else ""))
            except Exception as e:
                print("%-30s 0x%06X   read failed: %s" % (name, addr, e))
    finally:
        # ALWAYS, not just on the happy path.  Every sys.exit above used to leave the
        # port open mid-transfer -- the ECU may still be handing over a 4 KB answer,
        # and dropping the descriptor there is how a USB serial adapter ends up in a
        # state the next run cannot configure.
        k.close()

    print("\n---- how to read this ----")
    print("If addresses PAST THE END of the array read the same pattern as the sector in")
    print("question, that pattern is the flash controller answering for memory it does not")
    print("have.  If a segment with no controller at all reads something DIFFERENT, then the")
    print("pattern is not a generic bus default -- which is the distinction the first write-up")
    print("of this got wrong.  Compare all four rows before concluding anything; an automatic")
    print("verdict here compared two of them and was too crude to be worth printing.")
