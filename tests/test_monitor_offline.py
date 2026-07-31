"""Offline checks for the flash monitor: blob integrity, patching, gate, framing."""
import sys
from openm74 import klinebsl as K

fails = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


print("[1] embedded blob vs the assembler output")
check("MONITOR_HEX == the assembled blob shipped with the package", K.check_monitor_blob() is True)
check("monitor fits the 2048B stage-2 slot", len(bytes.fromhex(K.MONITOR_HEX)) <= K.STAGE2_LEN,
      "%d bytes" % len(bytes.fromhex(K.MONITOR_HEX)))

print("[2] patch sites land on the right immediates")
base = bytes.fromhex(K.MONITOR_HEX)
check("DELAY default is 0x4000",
      base[K.MON_DELAY_OFF] == 0x00 and base[K.MON_DELAY_OFF + 1] == 0x40)
check("SEGMASK default is 0x1000 (segment 0xCC only)",
      base[K.MON_SEGMASK_OFF] == 0x00 and base[K.MON_SEGMASK_OFF + 1] == 0x10)
m = K.build_monitor(delay=0x2000, segmask=0x0101)
check("build_monitor patches DELAY", m[K.MON_DELAY_OFF:K.MON_DELAY_OFF + 2] == b"\x00\x20")
check("build_monitor patches SEGMASK", m[K.MON_SEGMASK_OFF:K.MON_SEGMASK_OFF + 2] == b"\x01\x01")
check("build_monitor pads to 2048", len(m) == K.STAGE2_LEN)
check("only the two immediates move",
      sum(1 for i in range(len(base)) if m[i] != base[i]) == 3)   # 0x20 hi, mask lo+hi
check("read-only session compiles a zero mask",
      K.build_monitor(segmask=0)[K.MON_SEGMASK_OFF:K.MON_SEGMASK_OFF + 2] == b"\x00\x00")
check("pacing scales with baud", K.pacing_delay(19200) == 0x2000 and K.pacing_delay(9600) == 0x4000)

print("[3] segment gate (host side)")
check("0xCC0000 -> segment 0xCC", K.segmask_for(0xCC0000, 0x1000) == 1 << 12)
check("a range spanning C8..C9", K.segmask_for(0xC8FF00, 0x200) == (1 << 8) | (1 << 9))
check("mask_str reads back", K.mask_str(1 << 12) == "0xCC")
for bad, why in ((0xC00000, "boot sector"), (0xD00000, "past flash"), (0xBF0000, "before flash")):
    try:
        K.segmask_for(bad, 0x100)
        check("refuses 0x%06X (%s)" % (bad, why), False)
    except SystemExit:
        check("refuses 0x%06X (%s)" % (bad, why), True)
check("--allow-boot unlocks the boot sector", K.segmask_for(0xC00000, 0x100, True) == 1 << 0)

print("[4] command framing")
k = K.KlineBSL.__new__(K.KlineBSL)
check("READ 0xCC1234 x 0x0100", k._frame(K.CMD_READ, 0xCC1234, 0x100)
      == bytes([0x02, 0x34, 0x12, 0xCC, 0x00, 0x01]))
check("ERASE sector 0xC8A000", k._frame(K.CMD_ERASE_SECTOR, 0xC8A000, 0)
      == bytes([0x05, 0x00, 0xA0, 0xC8, 0x00, 0x00]))

print("[5] blank check follows the manual (erased reads 0x00)")
check("all-zero page is blank", K.blank_check(b"\x00" * 128)[0])
check("all-0xFF page is NOT blank", not K.blank_check(b"\xff" * 128)[0])

print("[6] in-stage gate logic mirrored (what SEGMASK actually permits)")


def stage_gate(seg, segmask):
    idx = (seg - 0xC0) & 0xFFFF
    if idx & 0xFFF0:
        return False
    return bool((1 << idx) & segmask)


check("mask 0x1000 permits 0xCC", stage_gate(0xCC, 0x1000))
check("mask 0x1000 refuses 0xC8", not stage_gate(0xC8, 0x1000))
check("mask 0x1000 refuses 0xC0 (boot)", not stage_gate(0xC0, 0x1000))
check("zero mask refuses everything", not any(stage_gate(s, 0) for s in range(0xC0, 0xCD)))
check("segments below 0xC0 refused", not stage_gate(0xBF, 0xFFFF))
check("segments above 0xCF refused", not stage_gate(0xD0, 0xFFFF))

print("[7] the streaming read path: stage-2 and where its payload starts")
# This whole path had no offline test at all, while the write path had eighteen groups of
# them -- and it is the path behind the GUI's Read button.


def capture(flash, echo_short=0, glitch=True, pad=b"\x00"):
    """A synthetic one-shot capture: both stages echoed, then the preamble, then flash."""
    echo = (bytes(32) + K.build_stage2()[:-1] + pad)[:K.ONESHOT_ECHO - echo_short]
    pre = bytes([0xE9 if glitch else K.SYNC]) + bytes([K.SYNC]) * (K.PREAMBLE_LEN - 1)
    return echo + pre + flash


s2 = K.build_stage2()
check("stage-2 is exactly the slot stage-1 delivers", len(s2) == K.STAGE2_LEN)
check("it is zero-padded, which is what anchors the payload offset", s2[-1] == 0x00)
check("pacing is patched into it", K.build_stage2(0x2000)[14:16] == b"\x00\x20")

body, note = K.split_oneshot(capture(b"\xde\xad\xbe\xef", glitch=False))
check("a clean capture splits at the nominal offset", body[:4] == b"\xde\xad\xbe\xef", str(note))
body, note = K.split_oneshot(capture(b"\xde\xad\xbe\xef"))
check("a glitched first preamble byte is absorbed", body[:4] == b"\xde\xad\xbe\xef", str(note))

# The trap: one byte swallowed in the echo AND flash beginning with 0xA5.  The fixed offset
# then lines up on seven preamble bytes plus that flash byte and used to report "clean".
tricky = capture(b"\xa5\x11\x22\x33", echo_short=1)
body, note = K.split_oneshot(tricky)
check("a shifted capture is NOT reported as clean", note != "clean", repr(note))
check("and it recovers the true first byte", body[:4] == b"\xa5\x11\x22\x33",
      body[:4].hex(" ") if body else "empty")

body, note = K.split_oneshot(bytes(K.ONESHOT_ECHO) + b"\x11" * 32)
check("no preamble at all is reported as such", note is None, repr(note))

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
