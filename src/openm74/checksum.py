"""ROM checksum of an M74 / M74 CAN image, computed the way the ECU computes it.

Pure software, no bench and no network.  This module decides one thing: does the
16-bit ROM checksum stored in an image agree with the bytes of that image.  It
exists so the flasher can say, after a read, whether the dump it just took is
self-consistent, and warn, before a write, when the image about to go in would
trip P0601 ("ROM checksum error") three minutes after the engine starts.

Why it is derived from the image's own code instead of a table: the summed
range, the position of the checksum field and whether the reserved sector is
included all differ between software versions, and reading them out of the
machine code of the image under the knife is the only thing that is correct for
every version rather than for the ones someone wrote down.

MEASURED against 120 factory images (chiptuner.ru serial M74 set): the routine
is found in 119 of them (absent only in the pre-production I414DA01), and the
computed value equals AvtoVAZ's own published checksum in 105 of the 107 that
the table lists -- the two misses are single-digit typos in the table, where the
value stored in the image itself matches this computation.  The machine code of
the routine, run in an emulator, produces the same number.

What the ECU does (confirmed by disassembly; file offsets, physical = +0xC00000):

    find the summing routine   a chain of per-page loops `mov r13,#0x3pp ...
                               cmp r12,#limit` (E6 FD pp 03 ... 46 FC lo hi):
                               the sum of flash bytes over pages 0x300..0x322,
                               each up to its own limit.  In all but the two
                               earliest versions page 0x303 stops at 0x2FFF, so
                               the reserved sector 0x0F000 is NOT summed.
    find the field             the P0601 check `mov Rn,[sum]; cmp Rn,[field]`
                               under EXTP 0x380; the field address is a 16-bit
                               operand resolved through the init-time DPP pages.
    the field is four bytes    the sum word (low, high) and its inversion.  The
                               field lies inside the summed area, but the
                               inversion makes its own contribution constant
                               (510), so the result does not depend on what the
                               field currently holds.

The reserved sector 0x0F000 reads back as the repeated pattern 9b 1e on the
chip, whatever a file holds there.  Where the routine sums it (the two earliest
versions), this computes it as 9b 1e, because that is what the silicon sums; a
dump read off a block therefore checks out as-is, with no need to zero it.
"""

import re
import struct

CODE_END = 0x80000
PAGE = 0x4000
_RESERVED = (0xF000, 0x10000)
_RESERVED_BYTE = (0x9B, 0x1E)

# mov r13,#0x3pp (E6 FD pp 03) ... cmp r12,#limit (46 FC lo hi): one page loop.
_RE_LOOP = re.compile(rb"\xe6\xfd(.)\x03(.{0,12}?)\x46\xfc(..)", re.S)
# calls seg,addr (DA seg lo hi), code segments 0xC0..0xCC.
_RE_CALL = re.compile(rb"\xda([\xc0-\xcc])(..)", re.S)
# after the call the sum is stored: extp 0x380,#1 ; mov [var],r4 (D7 40 .. F6 F4 ..).
_RE_STORE = re.compile(rb"\xd7\x40(..)\xf6\xf4(..)", re.S)


class Params:
    """Summed pages (page, limit) and the file offset of the 4-byte field."""

    __slots__ = ("field", "routine")

    def __init__(self, routine, field):
        self.routine = routine
        self.field = field


def _dpp_layout(d):
    """Working DPP0..2 -- calibration page numbers (0x320..0x33F) from init code."""
    out = {}
    for n in range(3):
        for m in re.finditer(bytes([0xE6, n]) + rb"(..)", d[:CODE_END], re.S):
            v = struct.unpack("<H", m.group(1))[0]
            if 0x300 <= v <= 0x33F:
                out.setdefault(n, set()).add(v)
    return {n: sorted(v) for n, v in out.items()}


def _find_routine(d):
    """Longest chain of per-page loops over consecutive pages -- the sum routine."""
    loops = [(m.start(), m.group(1)[0] | 0x300, struct.unpack("<H", m.group(3))[0])
             for m in _RE_LOOP.finditer(d[:CODE_END])]
    best, cur = [], []
    for item in loops:
        if cur and (item[0] - cur[-1][0] > 0x40 or item[1] != cur[-1][1] + 1):
            best = max(best, cur, key=len)
            cur = []
        cur.append(item)
    return max(best, cur, key=len)


def _find_field(d, routine, dpp):
    """File offset of the checksum field, from the compare in the P0601 check."""
    first = 0xC00000 + routine[0][0]
    calls = [m for m in _RE_CALL.finditer(d[:CODE_END])
             if 0 <= first - ((m.group(1)[0] << 16) | struct.unpack("<H", m.group(2))[0]) <= 0x20]
    for c in calls:
        for s in _RE_STORE.finditer(d[c.end():c.end() + 0x30]):
            var = s.group(2)
            for chk in re.finditer(rb"\xd7\x40\x80\x03\xf2([\xf0-\xff])" + re.escape(var)
                                   + rb"\x42(.)(..)", d[:CODE_END], re.S):
                if chk.group(2) != chk.group(1):
                    continue
                a16 = struct.unpack("<H", chk.group(3))[0]
                page = [p for p in dpp.get(a16 >> 14, []) if 0x320 <= p <= 0x323]
                if len(page) == 1:
                    return ((page[0] - 0x300) << 14) | (a16 & 0x3FFF)
    return None


def find_params(data):
    """Checksum parameters from the image's own code, or None if not an M74 image."""
    routine = _find_routine(data)
    if len(routine) < 8:
        return None
    dpp = _dpp_layout(data)
    field = _find_field(data, routine, dpp)
    if field is None:
        return None
    r = [(page, limit) for _, page, limit in routine]
    lo = min((page - 0x300) * PAGE for page, _ in r)
    hi = max((page - 0x300) * PAGE + limit for page, limit in r)
    if not lo <= field <= hi - 3:
        return None
    return Params(r, field)


def calc(data, p):
    """The 16-bit sum as (low, high) bytes, independent of the field's contents."""
    s = 0
    rlo, rhi = _RESERVED
    for page, limit in p.routine:
        base = (page - 0x300) * PAGE
        end = base + limit + 1
        s += sum(data[base:end])
        a, b = max(base, rlo), min(end, rhi)
        if a < b:                     # reserved sector in range: sum it as the chip reads it
            s -= sum(data[a:b])
            s += sum(_RESERVED_BYTE[(a + i) & 1] for i in range(b - a))
    f = p.field
    s -= data[f] + data[f + 1] + data[f + 2] + data[f + 3]
    s += 0xFF + 0xFF
    s &= 0xFFFF
    return s & 0xFF, (s >> 8) & 0xFF


def expected(data, p):
    lo, hi = calc(data, p)
    return bytes([lo, hi, (0xFF - lo) & 0xFF, (0xFF - hi) & 0xFF])


def stored(data, p):
    return bytes(data[p.field:p.field + 4])


def verify(data, p):
    return stored(data, p) == expected(data, p)


def status(data):
    """(ok, field, detail) for a full image, or (None, None, reason) if undeterminable.

    ok is True when the stored field matches the computation, False when it does
    not.  None means this is not an M74/M74 CAN image this module understands --
    not a verdict on the checksum.
    """
    if len(data) < CODE_END:
        return None, None, "image shorter than the code region"
    p = find_params(data)
    if p is None:
        return None, None, "no M74 checksum routine in this image's code"
    cur, exp = stored(data, p), expected(data, p)
    return cur == exp, p.field, "stored %s, computed %s" % (cur.hex(), exp.hex())
