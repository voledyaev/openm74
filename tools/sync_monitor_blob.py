#!/usr/bin/env python3
"""Refresh the monitor blob embedded in klinebsl.py from the assembler's output.

BUILD-TIME tool -- not part of the flasher.  Run it after editing stages/stage2mon.asm:

    cd stages
    asw -cpu 80C167 -L stage2mon.asm  &&  p2bin stage2mon.p stage2mon.bin
    cd ..  &&  python tools/sync_monitor_blob.py

It rewrites MONITOR_HEX and the two patch offsets in src/openm74/klinebsl.py, reading the
offsets out of the listing rather than trusting a hand count -- every time the stage
grew, those immediates moved, and a stale offset would patch the middle of some other
instruction and hand the ECU a stage that does something nobody wrote.

It also refreshes the copy of the .bin that travels inside the package, because that is
what the running tool cross-checks its embedded bytes against; leaving the two apart makes
the check either warn about nothing or, worse, pass while comparing to a stale file.
"""
import os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGES = os.path.join(ROOT, "stages")
BIN = os.path.join(STAGES, "stage2mon.bin")
LST = os.path.join(STAGES, "stage2mon.lst")
HOST = os.path.join(ROOT, "src", "openm74", "klinebsl.py")
PKG_BIN = os.path.join(ROOT, "src", "openm74", "stages", "stage2mon.bin")

# The stage is ORG'd here, so a listing address maps to this offset in the .bin.
ORG = 0x20
# Immediates the host patches: listing pattern -> where the operand sits in the insn.
PATCHES = (("MON_DELAY_OFF", r"MOV\s+R9,#DELAY", 2),
           ("MON_SEGMASK_OFF", r"AND\s+R13,#SEGMASK", 2))


def listing_offsets(text):
    out = {}
    for name, pattern, operand in PATCHES:
        hits = list(re.finditer(
            r"^\s*\d+/\s*([0-9A-F]+)\s*:\s*((?:[0-9A-F]{2} )+)\s*.*?" + pattern,
            text, re.M))
        if len(hits) != 1:
            sys.exit("[sync] %s: expected 1 match in the listing, found %d"
                     % (name, len(hits)))
        addr = int(hits[0].group(1), 16)
        out[name] = addr - ORG + operand
    return out


def main():
    blob = open(BIN, "rb").read()
    offs = listing_offsets(open(LST, "r", errors="replace").read())
    src = open(HOST, "r", encoding="utf-8").read()

    # sanity: the patch sites must currently hold the .asm defaults
    for name, want in (("MON_DELAY_OFF", 0x4000), ("MON_SEGMASK_OFF", 0x1000)):
        o = offs[name]
        got = blob[o] | (blob[o + 1] << 8)
        if got != want:
            sys.exit("[sync] %s -> offset 0x%X holds 0x%04X, expected the .asm default "
                     "0x%04X; the listing scan is off" % (name, o, got, want))

    hx = blob.hex()
    lines = [hx[i:i + 64] for i in range(0, len(hx), 64)]
    body = "MONITOR_HEX = (\n" + "\n".join('    "%s"' % l for l in lines) + ")\n"
    body += ("MON_DELAY_OFF = 0x%X           # the MOV R9,#DELAY immediate inside putb\n"
             % offs["MON_DELAY_OFF"])
    body += ("MON_SEGMASK_OFF = 0x%X         # the AND R13,#SEGMASK immediate inside gate"
             % offs["MON_SEGMASK_OFF"])

    new, n = re.subn(r"MONITOR_HEX = \(.*?MON_SEGMASK_OFF = 0x[0-9A-F]+[^\n]*",
                     lambda m: body, src, count=1, flags=re.S)
    if n != 1:
        sys.exit("[sync] could not find the MONITOR_HEX block in klinebsl.py")
    open(HOST, "w", encoding="utf-8", newline="\n").write(new)
    shutil.copyfile(BIN, PKG_BIN)
    print("[sync] %d bytes -> klinebsl.py   DELAY@0x%X  SEGMASK@0x%X"
          % (len(blob), offs["MON_DELAY_OFF"], offs["MON_SEGMASK_OFF"]))
    print("[sync] and -> %s (what the tool checks itself against)"
          % os.path.relpath(PKG_BIN, ROOT))


if __name__ == "__main__":
    main()
