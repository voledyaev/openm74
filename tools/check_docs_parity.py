#!/usr/bin/env python3
"""Catch the two halves of a bilingual document drifting apart.

`docs/` used to be English only, and the reason was written down: two copies of measured
claims drift, and the drifted one lies.  That reason has not gone away -- the audience for
this ECU is mostly Russian-speaking and needs the wiring in a language it reads, which is a
better reason, so the rule changed and this check is what pays for it.

**Prose is not compared, and could not be.** What is compared is everything a translation
has no business changing, which is also exactly what went wrong every time this project got
it wrong:

  * numbers written as digits -- `2.05`, `206`, `115200`, `0.59`
  * hex addresses and constants -- `0xC0F000`, `0x38`
  * anything in `backticks` -- identifiers, flags, filenames, register names
  * link targets

Every drift this project actually suffered was one of those.  The reliable-mode sector cost
said 2.05 s in one place and 2.64 in another.  The probe count said 48 in the code and 16 in
the prose.  The English help said "about an hour" while the Russian said thirteen minutes.
The divider landed on 38850 and one doc still said 39000.  A human reading either half alone
would not have caught any of them; a set difference catches all four.

Spelled-out numbers are deliberately ignored: "eight minutes" and "восьми минут" carry the
same fact in words this cannot compare, and demanding digits would push writing into a shape
nobody wants to read.  The rule is: **if a figure is worth writing as a numeral, it is worth
having in both halves.**

    uv run python tools/check_docs_parity.py

Exit status is 0 when every pair agrees.  A page with NO counterpart is a failure, not a
note: the arrangement is that every page in `docs/` exists in both languages, and a missing
half passing quietly is the same silent skip this project refuses everywhere else.  Adding a
new page therefore means adding both halves in the same commit.
"""
import os
import re
import sys
from collections import Counter

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# Ordered: hex first, so 0xC0F000 is taken whole rather than as "0" and "C0F000".
HEX = re.compile(r"0x[0-9A-Fa-f]+")
# Digits with an optional decimal part, and optional thousands separators of either style.
# The trailing guard excludes a following DIGIT only, not a following period: "section 1."
# ending a sentence has to count the same as "section 1 first" mid-sentence, and the first
# version of this line -- which excluded periods to keep "V1.0" whole -- reported exactly
# that as a difference between two identical references.  A leading word character still
# blocks the match, so "V1.0" is skipped on both sides rather than read as two numbers on
# one side and one on the other.
NUM = re.compile(r"(?<![\w.])\d[\d ',]*(?:\.\d+)?(?!\d)")
# Deliberately line-bounded.  Matching across newlines was tried and is wrong: markdown
# prose is full of backticks, so a dot-all pattern pairs the closing tick of one span with
# the opening tick of the next and reports whole sentences as identifiers.  A code span that
# genuinely needs to wrap should be rewrapped instead -- it reads better anyway.
TICK = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)")

# Backticked things a translation legitimately differs on: none, so far.  Kept as a named
# empty set rather than dropped, because the first exception will want a comment beside it.
TICK_IGNORE = set()


def tidy(n):
    """A numeral reduced to the digits that carry the meaning.

    Written as a named function on purpose.  It used to be a chain of .replace() calls
    inline, and one of them silently held a RUN of spaces rather than a single space --
    invisible when reading the line, and it meant every number kept its trailing separator
    and compared unequal against the same number written the other way round.  A check that
    reports differences it invented is worse than no check.
    """
    return "".join(c for c in n if c.isdigit() or c == ".")


def facts(text):
    """The parts of a document a translation must reproduce exactly."""
    hexes = HEX.findall(text)
    stripped = HEX.sub(" ", text)
    return {
        "hex": Counter(h.lower() for h in hexes),
        "number": Counter(tidy(n) for n in NUM.findall(stripped)),
        "code": Counter(" ".join(t.split()) for t in TICK.findall(text)
                        if " ".join(t.split()) not in TICK_IGNORE),
        "link": Counter(LINK.findall(text)),
    }


def compare(name, en, ru):
    """Report every fact present in one half and missing from the other."""
    bad = []
    a, b = facts(en), facts(ru)
    # The link each half carries to the other is the pairing itself, not a discrepancy: the
    # Russian page opens by pointing at the English one and naming it the source of truth.
    # Counting that would make every correctly-paired file fail.
    # Each half points at its own language's counterpart: the Russian page opens with a
    # pointer to the English one and names it the source of truth, and each links the README
    # a reader of that language wants.  Those are the pairing, not a discrepancy.
    a["link"].pop(name + ".ru.md", None)
    b["link"].pop(name + ".md", None)
    a["link"].pop("../README.md", None)
    b["link"].pop("../README.ru.md", None)
    for kind in ("hex", "number", "code", "link"):
        only_en = a[kind] - b[kind]
        only_ru = b[kind] - a[kind]
        for item, n in sorted(only_en.items()):
            bad.append("  %-6s %-34s in %s.md x%d, not in the Russian"
                       % (kind, item[:34], name, n))
        for item, n in sorted(only_ru.items()):
            bad.append("  %-6s %-34s in %s.ru.md x%d, not in the English"
                       % (kind, item[:34], name, n))
    return bad


def main():
    pairs, lonely = [], []
    for f in sorted(os.listdir(DOCS)):
        if not f.endswith(".md") or f.endswith(".ru.md"):
            continue
        name = f[:-3]
        ru = os.path.join(DOCS, name + ".ru.md")
        (pairs if os.path.exists(ru) else lonely).append(name)

    fails = 0
    for name in lonely:
        print("%-14s NO RUSSIAN COUNTERPART (docs/%s.ru.md does not exist)" % (name, name))
        fails += 1
    for name in pairs:
        en = open(os.path.join(DOCS, name + ".md"), encoding="utf-8").read()
        ru = open(os.path.join(DOCS, name + ".ru.md"), encoding="utf-8").read()
        bad = compare(name, en, ru)
        print("%-14s %s" % (name, "agree" if not bad else "%d DIFFERENCE(S)" % len(bad)))
        for line in bad:
            print(line)
        fails += len(bad)

    print()
    if fails:
        print("FAILED: %d fact(s) appear in one language and not the other." % fails)
        print("A number, address, identifier or link that moved in one half and not the")
        print("other is the failure this check exists for -- fix the stale half.")
    else:
        print("ALL DOCUMENT PAIRS AGREE ON EVERY NUMBER, ADDRESS, IDENTIFIER AND LINK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
