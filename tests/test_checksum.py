"""Offline checks for the ROM checksum math -- no image, no ECU, no port.

The field-finding and the P0601 monitor mask, both read from machine code, need real
firmware: tools/check_checksum_corpus.py runs them over the 120-image corpus, which is
not in this repository.  Here we pin the arithmetic
the flasher relies on: the sum is independent of what the field holds, a correct
field verifies, a wrong one does not, and a non-M74 buffer is reported as
not-an-M74-image rather than guessed at.
"""
import sys

from openm74 import checksum as C

# Section [8] prints the GUI's Russian strings, and a Windows CI console is cp1252, which
# cannot encode Cyrillic -- the same trap tools/check_gui_platform.py fell into first.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
ok, field, _, p0601 = C.status(bytes(0x90000))
check("status() of zeros: ok is None", ok is None and field is None and p0601 is None)
ok, field, _, p0601 = C.status(b"\x00" * 100)
check("status() of a short buffer: ok is None", ok is None and p0601 is None)
check("no monitor dispatcher: P0601 state is None, not a guess",
      C.p0601_monitor(bytes(0x90000)) is None)

print("[5] a truncated image is not-checked, not a crash or a false MISMATCH")
# A one-shot read that stalls at ~530 KB still holds the code, so the routine is found;
# the bytes it sums and the field are not all there.  Pinned through a stubbed finder,
# because building a real routine into a synthetic image would test the stub, not this.
_real_find = C.find_params
C.find_params = lambda d: C.Params([(0x300 + n, 0x3FFF) for n in range(0x23)], 0x8A28A)
try:
    ok, field, detail, p0601 = C.status(bytes(0x85000))
    check("short image: ok is None, no exception", ok is None and field is None, detail)
    ok, *_ = C.status(bytes(0xD0000))
    check("full-length image with the same params is checked", ok is not None)
finally:
    C.find_params = _real_find

# ---------------------------------------------------------------------------------------
# What the engine SAYS about each verdict.  The fake-ECU suite uses a synthetic image whose
# status is always "not checked", so without these the OK / mismatch / P0601-on / P0601-off
# branches would ship having never run.
import io, json, os, sys, types
from openm74 import klinebsl as K

def engine(verdict, fn, *a, **kw):
    """Run fn with checksum.status stubbed to `verdict`; return (stdout, events)."""
    real, out, ev = C.status, io.StringIO(), io.StringIO()
    C.status = lambda d: verdict
    old_out, old_ev = sys.stdout, K._EVENT_OUT
    sys.stdout, K._EVENT_OUT = out, ev
    try:
        fn(*a, **kw)
    finally:
        C.status, sys.stdout, K._EVENT_OUT = real, old_out, old_ev
    return out.getvalue(), [json.loads(x) for x in ev.getvalue().splitlines()]

print("[6] the engine's report, per verdict")
txt, evs = engine((True, 0x8A28A, "d", True), K.rom_checksum_report, b"", "a.bin")
check("OK is a stage event, not a fifth kind",
      [(e["event"], e.get("stage")) for e in evs] == [("stage", "rom-checksum")])
check("OK says OK", "a.bin: OK" in txt)
txt, evs = engine((False, 0x8A28A, "d", True), K.rom_checksum_report, b"", "a.bin")
check("mismatch is a problem of kind rom_checksum",
      [(e["event"], e.get("kind")) for e in evs] == [("problem", "rom_checksum")])
check("monitor on: P0601 likely, and says it was observed once",
      "is likely" in txt and "observed once" in txt and "MISMATCH" in txt)
txt, evs = engine((False, 0x8A28A, "d", False), K.rom_checksum_report, b"", "a.bin")
check("monitor off: says not reported, and that it is from disassembly",
      "masked off" in txt and "not observed" in txt and evs[0]["p0601"] is False)
txt, evs = engine((False, 0x8A28A, "d", None), K.rom_checksum_report, b"", "a.bin")
check("monitor unknown: says so", "could not be read" in txt)
txt, evs = engine((None, None, "why", None), K.rom_checksum_report, b"", "a.bin")
check("not checked: a problem with field None", evs[0]["kind"] == "rom_checksum"
      and evs[0]["field"] is None and "not checked" in txt)

print("[7] after the backup: the image checked is the ECU's with the write laid over it")
seen = []
real_report = K.rom_checksum_report
K.rom_checksum_report = lambda img, label, what="image", result=False: seen.append((img, result))
try:
    whole = bytes(range(256)) * (K.FLASH_SIZE // 256)
    patch = b"\xAA" * 300
    args = types.SimpleNamespace(addr=0xCC5040, flash=None)
    K.report_result_checksum(args, whole, patch)
    off = 0xCC5040 - K.FLASH_BASE
    want = whole[:off] + patch + whole[off + 300:]
    check("partial write: composed image checked, flagged result=True",
          len(seen) == 1 and seen[0] == (want, True))

    seen.clear()
    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ck_flash.bin")
    boot = K.BOOT_END - K.FLASH_BASE
    open(img_path, "wb").write(whole)
    try:
        args = types.SimpleNamespace(addr=K.BOOT_END, flash=img_path)
        K.report_result_checksum(args, whole, whole[boot:])
        check("--flash, same boot sector: nothing reported twice", seen == [])
        ecu = bytes([whole[0] ^ 0xFF]) + whole[1:]
        K.report_result_checksum(args, ecu, whole[boot:])
        check("--flash, different boot sector: the hybrid is checked",
              len(seen) == 1 and seen[0][0] == ecu[:boot] + whole[boot:])
    finally:
        os.remove(img_path)

    seen.clear()
    K.report_result_checksum(types.SimpleNamespace(addr=K.FLASH_BASE, flash=None),
                             whole, whole)
    check("whole image at the base: not reported twice", seen == [])
finally:
    K.rom_checksum_report = real_report

print("[8] the GUI renders every verdict in the chosen language")
try:
    from openm74 import gui, i18n
except ImportError as e:            # the engine CI job has no Tk; say so, do not pass silently
    print("  SKIP GUI rendering: %s" % e)
else:
    App = next(c for c in vars(gui).values()
               if isinstance(c, type) and hasattr(c, "on_event") and hasattr(c, "STAGE_LABEL"))
    for lang, ecu_word in (("ru", u"ЭБУ после"), ("en", u"the ECU after")):
        i18n.set_lang(lang)
        said = []
        fake = types.SimpleNamespace(
            say=said.append, status=types.SimpleNamespace(set=lambda x: None),
            bar={"value": 0}, STAGE_LABEL=App.STAGE_LABEL, results=[],
            _unmapped_regions=0, _unmapped_bytes=0, _rom_label=App._rom_label)
        for ev in ({"event": "stage", "stage": "rom-checksum", "ok": True, "label": "a.bin",
                    "field": 1, "p0601": True, "result": False},
                   {"event": "problem", "kind": "rom_checksum", "label": "x", "field": 1,
                    "p0601": True, "result": True},
                   {"event": "problem", "kind": "rom_checksum", "label": "a.bin", "field": 1,
                    "p0601": False},
                   {"event": "problem", "kind": "rom_checksum", "label": "a.bin", "field": 1,
                    "p0601": None},
                   {"event": "problem", "kind": "rom_checksum", "label": "a.bin",
                    "field": None, "p0601": None}):
            App.on_event(fake, ev)
        check("%s: one line per verdict" % lang, len(said) == 5, repr(said))
        check("%s: the composed image is named in this language, not the log's" % lang,
              ecu_word in said[1] and "x" not in said[1].split(":")[0])
        check("%s: the four mismatch texts differ" % lang, len(set(said[1:])) == 4)
    i18n.set_lang("en")

print()
if fails:
    print("FAILED: " + ", ".join(fails))
    raise SystemExit(1)
print("ALL CHECKSUM CHECKS PASSED")
