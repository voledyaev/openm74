#!/usr/bin/env python3
"""Bench probe: ask THIS ECU's flash controller what it has to say for itself.

READ-ONLY BY CONSTRUCTION. The monitor is started with a segment mask of ZERO, so the
uploaded stage physically cannot erase or program anything even if this script asked it
to. Reads are ungated because reading damages nothing.

    python tools/probe_protection.py /dev/cu.usbserial-XXXX

Two things this answers that nothing else on the bench can:

1. Is anything on this unit write-protected?  PROIN says whether protection is installed
   at all, and the PROCON registers say which logical sectors it would cover.  Until this
   existed, "the sector will not erase" and "the sector is locked" were indistinguishable
   from outside, and docs/FINDINGS.md said so.

2. Does the IMB register block read back sensibly through the monitor?  A block of
   plausible-looking zeros could mean "nothing is protected" or "we are reading the wrong
   place", and those are not the same answer.  So every register is read TWICE and the two
   passes must agree, and the verdict leans on registers whose correct value is known and
   is not zero-by-default: IMB_FSR_OP should have POWER set and nothing else after a reset
   (the flash modules went through their startup and nobody has issued Clear Status), while
   BUSY and the read margin should both be zero on an idle part.

   Do NOT use IMB_IMBCTRL for this.  It is the one register in the block that software
   configures -- wait states, prefetch -- so it sits at whatever the ROM left rather than
   at its documented reset value of 0x558C.  MEASURED on the bench unit: 0xA54C.  An
   earlier version of this probe told the reader to check exactly that, which would have
   made a correct reading look like a failed one.

What it does NOT answer: whether 0xC0F000 is unpopulated or locked.  That one turned out
not to need measuring -- Infineon documents it.  XC2000 System Units UM V1.0, Table 3-1
note 3: "The 4 KB sector from C0'F000H to C0'FFFFH is not accessible to the software", and
Figure 3-6 places it as physical sector 15 of flash module 0, which is why the same manual
lists Flash 0 as 252 KB rather than 256.  This probe is still worth running on any unit
before writing it, because protection is a per-unit setting and that one is not.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))
from openm74 import klinebsl as K

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

# The IMB register block, straight out of Table 3-7 of the manual.  Reset values are quoted
# where the manual gives one, because a register that reads its reset value is a register we
# are addressing correctly.
REGS = [
    ("IMB_IMBCTRL   control low",     K.IMB_BASE + 0x00, "software-configured, not at reset"),
    ("IMB_IMBCTRH   control high",    K.IMB_BASE + 0x02, ""),
    ("IMB_INTCTR    interrupt ctl",   K.IMB_BASE + 0x04, ""),
    ("IMB_FSR_BUSY  busy",            K.IMB_BASE + 0x06, "0x0000 when idle, one bit/module"),
    ("IMB_FSR_OP    last operation",  K.IMB_FSR_OP,      "expect 0x0004 (POWER) after reset"),
    ("IMB_FSR_PROT  protection",      K.IMB_FSR_PROT,    "bit0 PROIN, bit4 PROER"),
    ("IMB_MAR       read margin",     K.IMB_BASE + 0x0C, "0x0000 = normal read"),
    ("IMB_PROCON0   module 0 locks",  K.IMB_PROCON0 + 0, "bit s SET = sector s unlocked"),
    ("IMB_PROCON1   module 1 locks",  K.IMB_PROCON0 + 2, ""),
    ("IMB_PROCON2   module 2 locks",  K.IMB_PROCON0 + 4, ""),
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

        def word(addr):
            d = k.mon_read(addr, 2)
            return d[0] | (d[1] << 8)

        # Read the whole block once, then again.  A value that is not the same both times is
        # a framing artefact rather than a register, and a probe that cannot tell those apart
        # is worse than none: this is the reading everything downstream is judged against.
        first = {}
        for _, addr, _ in REGS:
            try:
                first[addr] = word(addr)
            except Exception:
                first[addr] = None

        print("\n%-30s %-10s %-8s %s" % ("register", "address", "value", "notes"))
        print("-" * 92)
        seen, unstable = {}, []
        for name, addr, note in REGS:
            try:
                again = word(addr)
            except Exception as e:
                print("%-30s 0x%06X   read failed: %s" % (name, addr, e))
                continue
            if first[addr] != again:
                unstable.append(name)
                print("%-30s 0x%06X   0x%04X   UNSTABLE -- read 0x%04X the first time"
                      % (name, addr, again, first[addr]))
                continue
            seen[addr] = again
            print("%-30s 0x%06X   0x%04X   %s" % (name, addr, again, note))

        # And the stage's own latch, which is a different thing from the live registers: it
        # holds what they said at the end of the last erase or program, captured before the
        # monitor's Reset to Read wiped the error bits.  Nothing has run in this session, so
        # the honest answer here is "nothing yet" -- and seeing that is the check that the
        # latch is being initialised rather than read out of uninitialised PSRAM.
        st = k.flash_status()
        print("\n[probe] the stage's latch: %s"
              % ("nothing has run in this session (as expected for a read-only probe)"
                 if st is None else K.flash_status_note(st)))

    finally:
        # ALWAYS, not just on the happy path.  Every sys.exit above used to leave the
        # port open mid-transfer -- the ECU may still be handing over a 4 KB answer,
        # and dropping the descriptor there is how a USB serial adapter ends up in a
        # state the next run cannot configure.
        k.close()

    print("\n---- how to read this ----")
    # The addressing check comes FIRST, because every other line here is worthless without
    # it: zeros read from the wrong place look exactly like "nothing is protected".
    anchor = (seen.get(K.IMB_BASE + 0x06) == 0x0000        # BUSY: idle
              and seen.get(K.IMB_BASE + 0x0C) == 0x0000    # MAR: normal read margin
              and seen.get(K.IMB_FSR_OP, 0) & ~0x0004 == 0)  # FSR_OP: POWER, nothing else
    if unstable:
        print("UNSTABLE: %s did not read the same twice.  Nothing below is trustworthy;"
              % ", ".join(unstable))
        print("power-cycle and run again before drawing any conclusion.")
    elif not anchor:
        print("The registers whose value after a reset is known did NOT read as expected")
        print("(BUSY and MAR zero, FSR_OP holding POWER and nothing else).  That points at")
        print("the addressing rather than at the ECU -- do not read protection off this.")
    else:
        print("Addressing confirmed: BUSY and the read margin are zero and FSR_OP holds")
        print("POWER alone, which is what an idle part reports after a reset with no Clear")
        print("Status.  So the protection rows mean what they say:")
    prot = seen.get(K.IMB_FSR_PROT)
    if prot is None:
        print("FSR_PROT did not read back, so nothing can be concluded about protection.")
    elif prot & 0x01:
        print("PROIN is SET: this unit HAS flash protection installed.  Read the PROCON")
        print("rows above -- a CLEARED bit is a LOCKED logical sector, not an unlocked one.")
    else:
        print("PROIN is CLEAR: no protection is installed on this unit, so no sector is")
        print("locked and no erase can be refused for that reason.  A sector that still")
        print("refuses to change has nothing behind it; it is not being protected.")
        print("(The PROCON registers reading all-zero is not alarming here: they are only")
        print("loaded from the security pages when protection is installed, and zero is")
        print("their reset value.  Bits there mean nothing while PROIN is clear.)")


if __name__ == "__main__":
    main()
