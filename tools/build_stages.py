#!/usr/bin/env python3
"""Assemble every byte this project uploads into an ECU, and check it against what ships.

BUILD-TIME tool -- not part of the flasher, and not needed to use it.  The assembled
bytes are committed precisely so that nobody needs a C166 assembler to flash an ECU; this
script is for the other question, which is whether those committed bytes are really what
the committed source says.  A repository that ships an opaque blob and its alleged source,
with no way to connect the two, is asking to be taken on trust -- and these blobs are
uploaded into somebody's engine controller.

Eight of them: the three stages that do the work, plus five bring-up diagnostics.  The
diagnostics were literals with a prose description and no source at all until publication
review, which is exactly the state this file exists to make impossible.

    python tools/build_stages.py            # assemble and compare
    python tools/build_stages.py --write     # ...and update the .bin files if they differ

The assembler is ASL, Alfred Arnold's macro cross-assembler, which covers this core with
`CPU 80C167`.  It is free software (GPL) and builds from source on macOS, Linux and
Windows:

    http://john.ccac.rwth-aachen.de:8000/as/         (asl-current.tar.gz)
    tar xzf asl-current.tar.gz && cd asl-current
    cp Makefile.def-samples/Makefile.def-<your-platform> Makefile.def
    make                                             # produces ./asl and ./p2bin

The executable is `asl` on Unix and `asw` on Windows; both are accepted below.  MEASURED:
a build of ASL 1.42 Bld 311 on macOS/arm64 reproduced all three committed blobs byte for
byte, including one produced years earlier by a different build on Windows -- so the
comparison this script performs is one that has actually been passed, not an aspiration.

After changing stages/stage2mon.asm, run tools/sync_monitor_blob.py as well: that is what
copies the new bytes into klinebsl.py and re-derives the two host-patched offsets.
"""
import os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGES = os.path.join(ROOT, "stages")
# The three stages that do the work: assembled bytes are committed beside the source.
STAGE_NAMES = ("stage1recv", "stage2dump", "stage2mon")
# The bring-up diagnostics.  Their bytes live as literals in klinebsl.py because that is
# what gets uploaded, so THAT is what the assembled output is checked against -- a
# committed .bin would be a third copy with nothing keeping it honest.
DIAG = {"diag-txprobe": "TX_PROBE", "diag-flashprobe": "FLASH_PROBE",
        "diag-echo": "ECHO_SERVER", "diag-rxread": "RX_READ",
        "diag-diswdt": "DISWDT_TEST"}


def find(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def main():
    write = "--write" in sys.argv[1:]
    asm, bin2 = find("asl", "asw"), find("p2bin")
    if not asm or not bin2:
        print("[stages] no C166 assembler found (looked for asl/asw and p2bin on PATH)")
        print("[stages] see the top of this file for where to get ASL and how to build it")
        print("[stages] the committed .bin files are usable as they are; this check is")
        print("[stages] about proving they match the .asm, so skipping it is not a failure")
        return 0
    print("[stages] using %s and %s" % (asm, bin2))

    def assemble(name):
        """Run the toolchain on stages/<name>.asm and return the bytes, or None."""
        src = os.path.join(STAGES, name + ".asm")
        obj = os.path.join(STAGES, name + ".p")
        out = os.path.join(STAGES, name + ".fresh.bin")
        if os.path.exists(obj):
            os.remove(obj)      # ASL appends to an existing .p rather than replacing it
        # -L writes the listing sync_monitor_blob.py reads the patch offsets out of.
        r = subprocess.run([asm, "-cpu", "80C167", "-L", os.path.basename(src)],
                           cwd=STAGES, capture_output=True, text=True)
        if r.returncode != 0:
            print("[stages] %s: ASSEMBLY FAILED" % name)
            print(r.stdout.strip() or r.stderr.strip())
            return None
        r = subprocess.run([bin2, os.path.basename(obj), os.path.basename(out)],
                           cwd=STAGES, capture_output=True, text=True)
        if r.returncode != 0:
            print("[stages] %s: p2bin FAILED: %s" % (name, r.stderr.strip()))
            return None
        data = open(out, "rb").read()
        os.remove(out)
        return data

    bad = []
    for name in STAGE_NAMES:
        have = os.path.join(STAGES, name + ".bin")
        fresh = assemble(name)
        if fresh is None:
            bad.append(name)
            continue
        if not os.path.exists(have):
            open(have, "wb").write(fresh)
            print("[stages] %-11s %4d bytes  CREATED" % (name, len(fresh)))
            continue
        same = fresh == open(have, "rb").read()
        print("[stages] %-11s %4d bytes  %s"
              % (name, len(fresh), "matches the committed blob" if same
                 else ("UPDATED" if write else "DIFFERS from the committed blob")))
        if not same:
            if write:
                open(have, "wb").write(fresh)
            else:
                bad.append(name)

    # The diagnostics, against the literals in the host rather than against a file.
    sys.path.insert(0, os.path.join(ROOT, "src"))
    try:
        from openm74 import klinebsl as K
    except Exception as e:
        print("[stages] could not import klinebsl to check the diagnostics: %s" % e)
        K = None
    for name in sorted(DIAG) if K else ():
        fresh = assemble(name)
        if fresh is None:
            bad.append(name)
            continue
        want = getattr(K, DIAG[name], None)
        same = fresh == want
        print("[stages] %-16s %4d bytes  %s"
              % (name, len(fresh), "matches klinebsl.%s" % DIAG[name] if same
                 else "DIFFERS from klinebsl.%s" % DIAG[name]))
        if not same:
            print("[stages]   assembled: %s" % fresh.hex(" "))
            print("[stages]   in host:   %s" % (want.hex(" ") if want else "not defined"))
            bad.append(name)

    if bad:
        print("[stages] %s did not match; re-run with --write to accept the new bytes,"
              % ", ".join(bad))
        print("[stages] then run tools/sync_monitor_blob.py so klinebsl.py agrees")
        return 1
    print("[stages] every committed stage is exactly what its source assembles to")
    return 0


if __name__ == "__main__":
    sys.exit(main())
