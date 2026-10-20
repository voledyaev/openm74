#!/usr/bin/env python3
"""Run the ROM checksum and the P0601 monitor mask over a corpus of factory images.

BUILD-TIME tool -- not part of the flasher.  The numbers the README gives for the checksum
("routine found in 119 of 120", "equals AvtoVAZ's published checksum in 105 of the 109 it
lists", "11 factory versions ship with P0601 masked off") come from this run, and the corpus they come
from is not in this repository: 120 firmware files are not ours to redistribute.  Point it
at a directory of .bin files, with an inventory.csv beside them that carries the published
checksum per file.  Without one this script says so and exits 0.

    python tools/check_checksum_corpus.py DIR
    OPENM74_CORPUS=DIR python tools/check_checksum_corpus.py

Exit status is nonzero when a result contradicts the claims above, so the README cannot
drift from the measurement without this saying so.
"""
import csv, glob, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from openm74 import checksum as C  # noqa: E402

DEFAULT = os.environ.get("OPENM74_CORPUS", "")


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    files = sorted(glob.glob(os.path.join(root, "*.bin"))) if root else []
    if not files:
        print("no corpus at %s -- skipped (it is not part of this repository)"
              % (root or "DIR / $OPENM74_CORPUS"))
        return 0
    official = {}
    inv = os.path.join(root, "inventory.csv")
    if os.path.exists(inv):
        with open(inv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("official_cs"):
                    official[row["file"]] = int(row["official_cs"], 16)

    found = field_ok = off_n = off_eq = 0
    computed = {}
    stale = {True: [], False: [], None: []}
    for path in files:
        name = os.path.basename(path)
        data = open(path, "rb").read()
        p = C.find_params(data)
        if p is None:
            print("  no routine   %s" % name)
            continue
        found += 1
        lo, hi = C.calc(data, p)
        computed.setdefault(bytes([lo, hi]), []).append(name)
        if name in official:
            off_n += 1
            if official[name] == (hi << 8 | lo):
                off_eq += 1
            else:
                print("  != official  %s  table %04X, computed %04X, field %s"
                      % (name, official[name], hi << 8 | lo, C.stored(data, p).hex()))
        if C.verify(data, p):
            field_ok += 1
        else:
            stale[C.p0601_monitor(data)].append((name, C.stored(data, p)[:2]))

    print()
    print("images                      %d" % len(files))
    print("checksum routine found      %d" % found)
    print("computed = published table  %d of %d listed" % (off_eq, off_n))
    print("computed = image's field    %d of %d" % (field_ok, found))
    for k, what in ((False, "masked "), (True, "enabled"), (None, "unknown")):
        print("field stale, P0601 %s  %d  %s"
              % (what, len(stale[k]), " ".join(n for n, _ in stale[k])))
    # Where a stale field came from: the computed checksum of another version, or nothing
    # in the corpus.  "Left over from the version it derives from" holds only for the first.
    borrowed = [(n, computed[f]) for n, f in stale[False] if f in computed]
    for n, src in borrowed:
        print("  %s carries the checksum of %s" % (n, ", ".join(src)))
    print("masked + stale, field is another version's checksum: %d of %d"
          % (len(borrowed), len(stale[False])))

    # The README's claims, checked against what was just measured.
    if os.path.abspath(root) == os.path.abspath(DEFAULT):
        claims = [("routine found in 119 of 120", (len(files), found) == (120, 119)),
                  ("published checksum matched in 105 of 109", (off_eq, off_n) == (105, 109)),
                  ("11 stale fields with P0601 masked off", len(stale[False]) == 11),
                  ("7 of them carry another version's checksum", len(borrowed) == 7),
                  ("2 stale fields with P0601 enabled", len(stale[True]) == 2),
                  ("no stale field with an unreadable mask", not stale[None])]
        bad = [c for c, ok in claims if not ok]
        for c, ok in claims:
            print(("  OK   " if ok else "  FAIL ") + c)
        if bad:
            print("README claims no longer match the corpus: " + "; ".join(bad))
            return 1
        print("ALL README CHECKSUM CLAIMS HOLD ON THE CORPUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
