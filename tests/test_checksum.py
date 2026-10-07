"""Offline checks for the ROM checksum math -- no image, no ECU, no port.

The field-finding from machine code is exercised against real firmware elsewhere
(the calibration side keeps the 120-image corpus).  Here we pin the arithmetic
the flasher relies on: the sum is independent of what the field holds, a correct
field verifies, a wrong one does not, and a non-M74 buffer is reported as
not-an-M74-image rather than guessed at.
"""
from openm74 import checksum as C

fails = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# A synthetic image: one summed page 0x300 (file 0x00000) up to a small limit, with
# the 4-byte field inside it.  Enough to pin the arithmetic without real firmware.
LIMIT = 0x3F
FIELD = 0x10
data = bytearray(0x90000)
for i in range(LIMIT + 1):
    data[i] = (i * 7 + 3) & 0xFF
p = C.Params([(0x300, LIMIT)], FIELD)

print("[1] the sum does not depend on the field's current contents")
lo0, hi0 = C.calc(data, p)
for junk in (0x00, 0xFF, 0x5A):
    for k in range(4):
        data[FIELD + k] = junk
    check("calc stable with field = 0x%02X" % junk, C.calc(data, p) == (lo0, hi0))

print("[2] a correct field verifies; a wrong one does not")
# openm74 never recomputes a checksum (it writes bytes as given), so there is no
# fix(): the correct field is expected() written into place by hand.
check("expected is sum word + its inversion",
      C.expected(data, p) == bytes([lo0, hi0, 0xFF - lo0, 0xFF - hi0]))
data[FIELD:FIELD + 4] = C.expected(data, p)
check("a correct field verifies", C.verify(data, p))
data[FIELD] ^= 0x01
check("flipped field no longer verifies", not C.verify(data, p))

print("[3] the reserved sector is summed as the chip reads it (9b 1e)")
# A page that spans the reserved sector: its content in the file must not matter,
# only the 9b 1e the silicon returns.
big = bytearray(0x90000)
pr = C.Params([(0x303, 0x3FFF)], 0x0C010)   # page 0x303 = file 0x0C000..0x0FFFF
a = C.calc(big, pr)
big[0xF000:0x10000] = b"\xde\xad" * 0x800    # different file bytes in the reserved sector
check("file bytes in 0x0F000 do not change the sum", C.calc(big, pr) == a)

print("[4] a non-M74 buffer is reported as not-an-image, not a verdict")
ok, field, _ = C.status(bytes(0x90000))
check("status() of zeros: ok is None", ok is None and field is None)
ok, field, _ = C.status(b"\x00" * 100)
check("status() of a short buffer: ok is None", ok is None)

print()
if fails:
    print("FAILED: " + ", ".join(fails))
    raise SystemExit(1)
print("ALL CHECKSUM CHECKS PASSED")
