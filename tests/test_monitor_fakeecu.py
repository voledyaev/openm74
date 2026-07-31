"""End-to-end offline exercise of the write path against a simulated ECU.

The fake speaks the same wire protocol the stage does -- half-duplex echo of everything
the host sends, the 2-stage handoff, the 6-byte command frame -- and it reads the
writable-segment mask straight out of the stage image the host uploads, so the gate is
tested as the silicon would apply it, not as the host imagines it.

Flash is modelled the way the part behaves: erase drives a page/sector to 0x00 and
programming can only turn bits ON, so a forgotten erase shows up as corruption here
instead of on the bench.
"""
import sys, os, io, json, tempfile

from openm74 import klinebsl as K

TMP = tempfile.mkdtemp(prefix="m74mon")     # scratch for the patch files we write


def synthetic_image():
    """Build the reference image the tests run against.

    Deliberately generated rather than shipped: a real dump is one specific vehicle's
    calibration and adaptation data, which is not ours to publish and would be no use to
    anyone else.  The shape is what matters here, and the shape is reproduced -- code-like
    bytes, a few genuinely blank sectors, and one region deliberately left non-blank so
    that 'did the erase actually do anything' can be told apart from 'this sector was
    already empty', which is a distinction several of these tests depend on."""
    img = bytearray(K.FLASH_SIZE)
    seed = 0x1234
    for i in range(K.FLASH_SIZE):
        seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
        img[i] = (seed >> 16) & 0xFF
    for blank in (0xCC5000, 0xCC7000, 0xC7F000):        # blank sectors, as on real units
        off = blank - K.FLASH_BASE
        img[off:off + K.SECTOR] = b"\x00" * K.SECTOR
    return bytes(img)


REF = synthetic_image()
K.REF_BIN = os.path.join(TMP, "reference.bin")          # the host compares against this
open(K.REF_BIN, "wb").write(REF)
os.chdir(TMP)                               # backups the tool drops land here too
FAILS = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


class FakeECU(object):
    def __init__(self, image):
        self.flash = bytearray(image)
        self.buf = bytearray()
        self.out = bytearray()
        self.state = "stage1"
        self.segmask = 0
        self.pending = None                 # address a PROGRAM is waiting on a page for
        self.log = []
        # The flash controller's own status.  reg_* are the live registers; fstat_* is what
        # the stage latched into PSRAM for the host, which is a DIFFERENT thing precisely
        # because "Reset to Read" clears the live error bits and the stage runs it straight
        # after every operation.  0xFFFF is what the stage writes at startup and is the
        # host's signal that nothing has run yet.
        self.reg_op = 0x0000
        # PROIN.  Sector locks only bite when protection is actually installed, so a fake
        # with locked sectors must report that too -- PROCON alone locks nothing.
        self.reg_prot = 0x0001 if self.LOCKED else 0x0000
        self.fstat_op = self.fstat_prot = 0xFFFF
        self.psram = b""                    # the delivered stage, readable back at MON_BASE

    mangle = staticmethod(lambda d: d)      # tests swap this in to dirty the line
    corrupt = staticmethod(lambda d: d)     # ...and this to land a stage that is NOT the
                                            # one that was sent, still greeting normally

    # Addresses the flash controller decodes but has no memory for.  MEASURED on the bench:
    # such an address reads one word repeated, the SAME word that reading past the end of
    # the populated array gives -- which is what lets the host tell "no memory here" apart
    # from "this erase failed", since both leave the contents unchanged.
    NOTHING = b"\x9b\x1e"
    # And OUTSIDE the flash controller's decode the answer is different again.  MEASURED
    # (QUIRKS section 7): 0xCD0000/0xCF0000 -- past the populated array but still the
    # controller's range -- read 9b 1e, while 0x300000, which no flash controller decodes,
    # reads 0x46.  The fake used to return 9b 1e everywhere, which made the whole address
    # space look like the controller's, and that is exactly why a host check built on the
    # wrong yardstick passed here and then refused a known-good ECU on the bench.
    NO_CONTROLLER = b"\x46\x46"
    CTRL_LO, CTRL_HI = 0xC00000, 0xD00000   # the range that answers NOTHING when empty
    UNMAPPED = ()                           # sector base addresses, set per test

    def _fetch(self, addr, cnt):
        """Bytes at addr, modelling unmapped space as the controller does."""
        out = bytearray()
        for a in range(addr, addr + cnt):
            o = a - K.FLASH_BASE
            if not (self.CTRL_LO <= a < self.CTRL_HI):
                out.append(self.NO_CONTROLLER[a & 1])
                continue
            unmapped = (o < 0 or o >= len(self.flash)
                        or any(base <= a < base + K.SECTOR for base in self.UNMAPPED))
            out.append(self.NOTHING[a & 1] if unmapped else self.flash[o])
        return bytes(out)

    def rx(self, data):
        self.out += data                    # the host's own cable echoes what it SENT
        # A HARNESS SIMPLIFICATION, stated as one.  `mangle` is applied to command frames
        # and page payloads only, so that the dirty-line scenarios below exercise frame
        # recovery in isolation.  It is NOT a claim that stage uploads cannot be corrupted:
        # stage-1's first byte follows the ECU's 0xD5 and takes the same turnaround as any
        # other reversal, and the 2 KB stage-2 stream crosses with no checksum at all.
        # Corrupting them is a DIFFERENT failure with a different correct outcome -- refuse
        # the session, ask for a power cycle, erase nothing -- and it has its own scenarios
        # in [21] and [23] rather than being mixed into these.
        dirty = self.state in ("cmd", "payload")
        self.buf += self.mangle(data) if dirty else data
        self.pump()

    def pump(self):
        while True:
            if self.state == "stage1":
                if len(self.buf) < 32:
                    return
                del self.buf[:32]
                self.state = "stage2"
            elif self.state == "stage2":
                if len(self.buf) < K.STAGE2_LEN:
                    return
                mon = bytes(self.buf[:K.STAGE2_LEN])
                del self.buf[:K.STAGE2_LEN]
                # Corrupt FIRST, then derive the gate from what landed.  On silicon the
                # bytes that execute and the bytes the host reads back are the same bytes;
                # taking segmask from the pre-corruption copy made the fake enforce one
                # program while reporting another, so `widen_the_gate` never actually
                # widened anything and the test could only prove the host NOTICED -- not
                # that noticing prevented an erase.
                self.psram = self.corrupt(mon)
                self.segmask = (self.psram[K.MON_SEGMASK_OFF]
                                | (self.psram[K.MON_SEGMASK_OFF + 1] << 8))
                self.out += self.glitch(bytes([K.SYNC]) * K.PREAMBLE_LEN) + bytes([K.MON_READY])
                self.state = "cmd"
            elif self.state == "payload":
                # Reached only after we answered ST_OK, so exactly this many bytes are
                # owed and nothing else can be mistaken for them.
                if len(self.buf) < K.MON_LEAD + K.PAGE + 2:
                    return
                page = bytes(self.buf[K.MON_LEAD:K.MON_LEAD + K.PAGE])
                cks = (self.buf[K.MON_LEAD + K.PAGE]
                       | (self.buf[K.MON_LEAD + K.PAGE + 1] << 8))
                del self.buf[:K.MON_LEAD + K.PAGE + 2]   # lead bytes counted off
                self.state = "cmd"
                if (sum(page) & 0xFFFF) != cks:
                    self.reply(K.MON_CKS)       # corrupted page -- refuse it before flash
                    continue
                o = self.pending - K.FLASH_BASE
                for i, b in enumerate(page):
                    self.flash[o + i] |= b      # programming only turns bits on
                self._ran(0x01)
                self.reply(K.MON_OK)
            else:
                while self.buf and self.buf[0] != K.SYNC:
                    del self.buf[:1]            # drop anything until the frame marker
                n = 0
                while n < len(self.buf) and self.buf[n] == K.SYNC:
                    n += 1                      # skip the marker run: a command is never A5
                if n >= len(self.buf) or len(self.buf) < n + 8:
                    return
                body = bytes(self.buf[n:n + 6])
                cks = self.buf[n + 6] | (self.buf[n + 7] << 8)
                del self.buf[:n + 8]            # a frame is ALWAYS this long, whatever cmd says
                if (sum(body) & 0xFFFF) != cks:
                    self.reply(K.MON_CKS)       # never execute a frame that does not add up
                    continue
                cmd, ol, oh, seg, cl, ch = body
                self.execute(cmd, ol | (oh << 8), seg, cl | (ch << 8))

    @staticmethod
    def glitch(data):
        """Mangle the first byte the ECU transmits after receiving, as the bench does.

        Measured on hardware: PING answered 0xD5 for 0x55, READ answered 0xE9 for 0x4B.
        Reproducing it here is the point -- a reply the host can only read correctly
        because of the lead bytes is a reply the fix actually earned."""
        return bytes([data[0] ^ 0x80]) + data[1:]

    def reply(self, st):
        self.out += self.glitch(bytes([K.SYNC]) * K.MON_LEAD) + bytes([st])

    # The one register the host reads out of the ECU: the baud-rate divider.  Without it
    # modelled, learn_baud_const() got a short read, raised, and silently fell back to the
    # compiled-in constant -- so the arithmetic that PROTOCOL 5 calls the thing protecting
    # other board revisions was never exercised offline even once.
    PDIV = 130                              # what the ROM leaves after autobauding 9600

    # Protection configuration: a bit per logical sector, SET meaning unlocked.  Ten
    # sectors per module, three modules, nothing locked -- which is what the bench unit
    # reports and what any ECU that has never had protection installed reports.
    PROCON = (0x3FF, 0x3FF, 0x3FF)
    LOCKED = ()                             # sector base addresses, set per test

    # What the silicon says it is, measured off the reference ECU.  The write path is gated
    # on these, so a fake that does not answer them is a fake the tool must refuse -- which
    # is the behaviour under test in group [20], not an inconvenience to be worked around.
    IDMANUF, IDCHIP, IDMEM = 0x1820, 0x3801, 0x30D0
    STSTAT = 0x8046                     # HWCFG 0x46: low bits 0110, the UART bootloader

    def _words(self):
        """Everything the host can reach with an ordinary READ that is not flash."""
        w = {K.MON_FSTAT: self.fstat_op, K.MON_FSTAT + 2: self.fstat_prot,
             K.IMB_FSR_OP: self.reg_op, K.IMB_FSR_PROT: self.reg_prot,
             K.IDMANUF_ADDR: self.IDMANUF, K.IDCHIP_ADDR: self.IDCHIP,
             K.IDMEM_ADDR: self.IDMEM, K.STSTAT_ADDR: self.STSTAT}
        for i, p in enumerate(self.PROCON):
            w[K.IMB_PROCON0 + 2 * i] = p
        b = {}
        for a, v in w.items():
            b[a], b[a + 1] = v & 0xFF, (v >> 8) & 0xFF
        return b

    def _ran(self, what, prot_err=False):
        """Model one flash operation's effect on the status registers and the latch.

        Order matters and is the whole reason the latch exists: the stage copies both
        registers out FIRST, and only then issues Reset to Read -- which is documented to
        clear SQER, OPER and PROER.  A host reading the live registers afterwards would
        find them clean and conclude nothing went wrong."""
        self.reg_op |= what
        if prot_err:
            self.reg_prot |= 0x10                   # PROER
        self.fstat_op, self.fstat_prot = self.reg_op, self.reg_prot
        self.reg_op &= ~0x30                        # Reset to Read clears SQER/OPER...
        self.reg_prot &= ~0x10                      # ...and PROER

    def execute(self, cmd, off, seg, cnt):
        addr = (seg << 16) | off
        if cmd == K.CMD_READ and addr == K.BRG_ADDR:
            self.log.append((cmd, addr, cnt, True))
            self.reply(K.MON_OK)
            self.out += bytes([self.PDIV & 0xFF, (self.PDIV >> 8) & 0xFF])[:cnt]
            return
        if (cmd == K.CMD_READ and self.psram
                and K.MON_BASE <= addr < K.MON_BASE + len(self.psram)):
            self.log.append((cmd, addr, cnt, True))
            self.reply(K.MON_OK)
            o = addr - K.MON_BASE
            self.out += self.psram[o:o + cnt]
            return
        words = self._words()
        if cmd == K.CMD_READ and addr in words:
            self.log.append((cmd, addr, cnt, True))
            self.reply(K.MON_OK)
            self.out += bytes(words.get(addr + i, 0) for i in range(cnt))
            return
        o = addr - K.FLASH_BASE
        idx = (seg - 0xC0) & 0xFFFF
        allowed = (idx & 0xFFF0) == 0 and bool((1 << idx) & self.segmask)
        self.log.append((cmd, addr, cnt, allowed))
        if cmd == K.CMD_PING:
            self.reply(K.MON_READY)
        elif cmd == K.CMD_READ:
            self.reply(K.MON_OK)        # the lead covers the turnaround; the stream
            self.out += self._fetch(addr, cnt)      # that follows is clean, as measured
        elif cmd in (K.CMD_ERASE_PAGE, K.CMD_ERASE_SECTOR):
            if not allowed:
                self.reply(K.MON_REFUSED); return
            gran = K.PAGE if cmd == K.CMD_ERASE_PAGE else K.SECTOR
            locked = any(base <= addr < base + K.SECTOR for base in self.LOCKED)
            if not locked and not any(base <= addr < base + K.SECTOR
                                      for base in self.UNMAPPED):
                self.flash[o:o + gran] = b"\x00" * gran  # erased reads as ZERO on this part
            # A locked sector and an absent one look the SAME from here -- module goes idle,
            # contents unchanged.  Only PROER separates them, which is the point.
            self._ran(0x02, prot_err=locked)
            self.reply(K.MON_OK)        # unmapped space reports success and changes nothing
            return
        elif cmd == K.CMD_CKSUM:
            self.reply(K.MON_OK)
            s1, s2 = K.sums16(self._fetch(addr, cnt))
            self.out += bytes([s1 & 0xFF, s1 >> 8, s2 & 0xFF, s2 >> 8])
        elif cmd == K.CMD_BAUD:
            # the fake has no wire, so retuning is a no-op -- but it must ACK, because
            # the host acks first and only then retunes itself
            self.reply(K.MON_OK)
        elif cmd == K.CMD_PROG:
            if not allowed:
                self.reply(K.MON_REFUSED); return       # refused BEFORE any page is sent
            self.pending = addr
            self.state = "payload"
            self.reply(K.MON_OK)                        # "send the page"
        else:
            self.reply(K.MON_REFUSED)


# A virtual clock.  The fake never blocks, so the host's real timeouts would otherwise
# burn wall-clock seconds spinning -- and the stall recovery below deliberately provokes
# a lot of them.  Reading an empty line advances the clock by that read's timeout, which
# is both faster and a better model of what the port actually does.
CLOCK = {"t": 0.0}


class FakeSerial(object):
    ecu = None

    def __init__(self, port, baud, timeout=0.3, **kw):
        self.timeout = timeout
        self.baudrate = baud            # the host retunes this after a CMD_BAUD ack
        self.is_open = True

    def write(self, data):
        FakeSerial.ecu.rx(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n=1):
        out = FakeSerial.ecu.out
        if not out:
            CLOCK["t"] += (self.timeout or 0.1)     # an empty read is a timeout elapsing
            return b""
        take = min(n, len(out))
        d = bytes(out[:take])
        del out[:take]
        CLOCK["t"] += 0.001
        return d

    def reset_input_buffer(self):
        pass                # the host drains its own echo deliberately; never discard it

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def run(argv, image=REF):
    """Run the real CLI against a fresh fake ECU; return (stdout, ecu, exit_code)."""
    ecu = FakeECU(image)
    FakeSerial.ecu = ecu
    K.serial.Serial = FakeSerial
    old_out, old_argv = sys.stdout, sys.argv
    sys.stdout = io.StringIO()
    code = 0
    try:
        sys.argv = ["klinebsl.py", "--port", "FAKE"] + argv
        # `or 0`, because main() RETURNS the status now -- discarding it is what made every
        # exit-code assertion in this file vacuous, and is the same mistake the module's own
        # __main__ was making until the bench caught it.
        code = K.main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str):
            print(e.code)
    finally:
        text = sys.stdout.getvalue()
        sys.stdout, sys.argv = old_out, old_argv
    return text, ecu, code


# the BSL handshake happens before any of this; make the fake answer it
K.KlineBSL.handshake = lambda self: True
K.time.sleep = lambda s: None                 # no need to wait for a fake stage to boot
K.time.time = lambda: CLOCK["t"]              # deadlines run on the virtual clock

print("[1] --monitor : read-only session")
out, ecu, _ = run(["--monitor"])
check("handoff detected", "monitor up" in out, out.splitlines()[1] if out else "")
check("PING answered", "PING ok" in out)
check("reset vector confirmed", "MONITOR READ CONFIRMED" in out)
check("read-only session compiles a ZERO segment mask", ecu.segmask == 0)
check("nothing was erased or programmed",
      all(c in (K.CMD_PING, K.CMD_READ, K.CMD_BAUD) for c, _, _, _ in ecu.log))
check("retuned the line for the session", any(c == K.CMD_BAUD for c, _, _, _ in ecu.log))
check("flash untouched", bytes(ecu.flash) == REF)

print("[2] --mon-read of the all-zero sector 0xCC5000")
out, ecu, _ = run(["--mon-read", "0xCC5000", "--len", "0x1000"])
check("read matched the reference dump", "4096/4096 match" in out)
check("recognised as erased", "reads as 0x00 = erased" in out)
check("flash untouched", bytes(ecu.flash) == REF)

print("[3] --write-selftest without --yes")
out, ecu, code = run(["--write-selftest", "0xCC5000"])
check("refused", code != 0 and "re-run with --yes" in out)
check("stage was never even started", ecu.state == "stage1")

print("[4] --write-selftest 0xCC5000 --yes : full erase/program round trip")
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--yes"])
check("write path proven", "WRITE PATH PROVEN" in out, "")
check("pattern verified byte-for-byte", "pattern verified byte-for-byte" in out)
check("blank-checks clean", out.count("blank-check (expecting all 0x00): clean") == 2)
check("flash ends byte-identical to how it started", bytes(ecu.flash) == REF)
check("only segment 0xCC was unlocked", ecu.segmask == 1 << 12)
check("erase+program actually happened",
      any(c == K.CMD_ERASE_PAGE for c, _, _, _ in ecu.log)
      and any(c == K.CMD_PROG for c, _, _, _ in ecu.log))

print("[5] --write-selftest on a page holding real data (0xCCF000, live adaptation)")
out, ecu, _ = run(["--write-selftest", "0xCCF000", "--yes"])
check("write path proven", "WRITE PATH PROVEN" in out)
check("original content restored exactly", bytes(ecu.flash) == REF)

print("[6] --write a region : merge, erase, program, verify")
newbytes = bytes((i * 7 + 3) & 0xFF for i in range(300))
PATCH1 = os.path.join(TMP, "patch.bin")
open(PATCH1, "wb").write(newbytes)
out, ecu, _ = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes"])
check("completed and verified", "WRITE COMPLETE AND VERIFIED" in out)
# reliable mode is the default, so a write must have left a recovery image behind
backups = [f for f in os.listdir(TMP) if f.startswith("m74_backup_")]
check("a pre-flight backup was taken before erasing", len(backups) >= 1)
check("the backup is the ECU as it was, in full",
      bool(backups) and open(os.path.join(TMP, backups[0]), "rb").read() == REF)
check("the final read-back verify ran", "FINAL VERIFY PASSED" in out)
off = 0xCC5040 - K.FLASH_BASE
check("the requested bytes landed", bytes(ecu.flash[off:off + 300]) == newbytes)
check("bytes before the range preserved",
      bytes(ecu.flash[off - 0x40:off]) == REF[off - 0x40:off])
check("bytes after the range preserved",
      bytes(ecu.flash[off + 300:off + 0x400]) == REF[off + 300:off + 0x400])
check("the rest of flash untouched",
      bytes(ecu.flash[:off]) == REF[:off]
      and bytes(ecu.flash[off + 300:]) == REF[off + 300:])

print("[7] --write across a sector boundary")
big = bytes((i ^ 0x3C) & 0xFF for i in range(0x900))
PATCH2 = os.path.join(TMP, "patch2.bin")
open(PATCH2, "wb").write(big)
out, ecu, _ = run(["--write", PATCH2, "--addr", "0xCC4E00", "--yes"])
off = 0xCC4E00 - K.FLASH_BASE
check("completed and verified", "WRITE COMPLETE AND VERIFIED" in out)
check("two sectors were touched", out.count("erased, programmed, verified") == 2)
check("the requested bytes landed", bytes(ecu.flash[off:off + 0x900]) == big)
check("surrounding flash preserved",
      bytes(ecu.flash[:off]) == REF[:off] and bytes(ecu.flash[off + 0x900:]) == REF[off + 0x900:])

print("[7b] the write re-decides its own speed from what it observes")
# The rule being guarded: step down to protect COMPLETION, never to chase speed.  Measured
# on the bench, a slower rate is not reliably quicker -- one run at 9600 needed three times
# more retries per sector than the 19200 run it had stepped down from -- so a link that is
# merely dirty is left alone, and only one whose losses threaten the retry budget is slowed.
# The pre-flight probe sends 10-byte command frames; a write pushes 132-byte pages, and
# MEASURED on the bench the two disagree by about six times because much of the loss is a
# fixed cost per transfer rather than per byte.  So the rate is now judged by the write
# itself, from real pages.  Model exactly that split here: command frames arrive perfectly,
# every fourth PAGE PAYLOAD does not.  A probe would call this link spotless; the write must
# notice and step down anyway.
pay = {"n": 0}


def every_fourth_payload(d):
    if len(d) <= 100:                       # command frames: leave them alone
        return d
    pay["n"] += 1
    if pay["n"] % 2 == 0:               # half the pages lost: past what the budget absorbs
        return d[:8] + bytes([d[8] ^ 0x55]) + d[9:]
    return d


FakeECU.mangle = staticmethod(every_fourth_payload)
three = bytes((i * 7) & 0xFF for i in range(3 * K.SECTOR))
WIDE = os.path.join(TMP, "wide.bin")
open(WIDE, "wb").write(three)
out, ecu, _ = run(["--write", WIDE, "--addr", "0xCC5000", "--yes"])
FakeECU.mangle = staticmethod(lambda d: d)
off = 0xCC5000 - K.FLASH_BASE
check("the probe saw a clean link, and said out of how many",
      "0 of 48 10-byte probe frames" in out)
check("but the write measured the pages and slowed down", "stepping down to" in out)
check("and said what it measured, not just what it did",
      "losing" in out and "of page attempts" in out)
check("it still completed and verified", "WRITE COMPLETE AND VERIFIED" in out)
check("the bytes landed despite the dirty payloads",
      bytes(ecu.flash[off:off + 3 * K.SECTOR]) == three)

print("[7c] a merely dirty link is NOT slowed down")
# A third of pages resent is noisy but survivable, AND -- the second half of the rule -- at
# this rung it still delivers more than the rung below could at its very best, so there is
# nothing to gain by dropping.  Both tests have to say no before the rate is left alone.
pay["n"] = 0


def every_third_payload(d):
    if len(d) <= 100:
        return d
    pay["n"] += 1
    return d[:8] + bytes([d[8] ^ 0x55]) + d[9:] if pay["n"] % 3 == 0 else d


FakeECU.mangle = staticmethod(every_third_payload)
out, ecu, _ = run(["--write", WIDE, "--addr", "0xCC5000", "--yes"])
FakeECU.mangle = staticmethod(lambda d: d)
check("resends really happened", "corrupted in flight, resending" in out)
check("but the rate was left alone", "stepping down to" not in out)
check("and it completed anyway", "WRITE COMPLETE AND VERIFIED" in out)

print("[7d] a clean link is left at the rate it started on")
out, ecu, _ = run(["--write", WIDE, "--addr", "0xCC5000", "--yes"])
check("no step down when nothing is wrong", "stepping down to" not in out)
check("completed", "WRITE COMPLETE AND VERIFIED" in out)

print("[7e] a READ that comes back short steps the rate down instead of ending the session")
# MEASURED on the bench 2026-07-31, and this is the expensive one: a pre-flight backup died
# 24 sectors in at the top rate, the tool announced "the monitor is gone; power-cycle and
# start over", and refused the write that depended on that backup.  The monitor was NOT
# gone -- asked again on a drained line it answered a PING at the same baud, no reset.  So a
# short answer must cost a rung, not the session.
short = {"left": 1}                     # exactly one chunk comes back short
real_read = FakeSerial.read


def truncating_read(self, n=1):
    ecu = FakeSerial.ecu
    # Gated on the last command being a READ **of the address under test**.  Keying on "the
    # buffer is big" alone also catches the 2 KB stage echo during the handoff and the block
    # the rate search reads from FLASH_BASE to prove a rate carries a stream -- either of
    # which would break something this test is not about.
    if (short["left"] and len(ecu.out) > 0x800 and ecu.log
            and ecu.log[-1][0] == K.CMD_READ and ecu.log[-1][1] == 0xCC5000):
        short["left"] -= 1
        del ecu.out[0x400:]                      # hand back part of it and drop the rest
    return real_read(self, n)


FakeSerial.read = truncating_read
out, ecu, code = run(["--mon-read", "0xCC5000", "--len", "0x2000"])
FakeSerial.read = real_read
check("it stepped down rather than giving up", "stepping down to" in out)
check("and said why, naming the short answer", "came back short" in out)
check("the read still completed", code == 0)
check("no claim that the ECU had died", "the monitor is gone" not in out)

print("[7f] the port's hardware settings are written once, not once per read")
# Assigning pyserial's `timeout` calls _reconfigure_port(), and on Windows that means
# SetCommState -- control transfers that reprogram a USB-serial adapter, baud divider
# included.  This code used to do it thirteen times over, including between transmitting and
# reading the reply, i.e. exactly at the direction turnaround.  Same bench from macOS: clean.
# Same bench from Windows: hundreds of retries.  SteadyPort keeps the timeout virtual; this
# test is here because the old form is the natural thing to write and would come straight
# back.
sets = {"timeout": 0, "baudrate": 0}
real_setattr = FakeSerial.__setattr__


def counting_setattr(self, name, value):
    if name in sets:
        sets[name] += 1
    real_setattr(self, name, value)


FakeSerial.__setattr__ = counting_setattr
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--sector", "--yes"])
FakeSerial.__setattr__ = real_setattr
check("the write path still works through the wrapper", "WRITE PATH PROVEN" in out)
# One from the constructor, one from SteadyPort taking the port over.  A per-read assignment
# would put this in the thousands for a 32-page sector.
check("timeout reached the driver at most twice", sets["timeout"] <= 2,
      "%d assignment(s)" % sets["timeout"])
# Rate changes are legitimate SetCommState calls, and a handful is expected: the ladder's
# steps, the confirm-or-revert probe after each, and the small sweep that finds which rate
# the ECU is really answering at.  The bound is loose on purpose -- what it has to catch is
# a per-read regression, which would put this in the thousands, not a sweep of seven.
check("rate changes stayed to a handful", sets["baudrate"] <= 40,
      "%d assignment(s)" % sets["baudrate"])

print("[7g] a reply that starts in the wrong place is recovered, not fatal")
# MEASURED: a reliable-mode write reached sector 90 of 206 with zero retries and then died
# on "reply lead-in 03 f7 is not A5 -- the link lost step".  The monitor was fine; the host
# was reading the middle of a stream instead of the front of it, which is the same situation
# as a status that never arrived and wants the same cure -- drain, ask again.  Ending the
# run there threw away 90 verified sectors.
shift = {"left": 3}
real_read2 = FakeSerial.read


def prefix_junk(self, n=1):
    """Put two bytes of noise in front of the next few replies."""
    ecu = FakeSerial.ecu
    if shift["left"] and len(ecu.out) >= 3 and ecu.out[0] == K.SYNC:
        shift["left"] -= 1
        ecu.out[:0] = bytearray(b"\x03\xf7")
    return real_read2(self, n)


FakeSerial.read = prefix_junk
out, ecu, code = run(["--write-selftest", "0xCC5000", "--yes"])
FakeSerial.read = real_read2
check("it recovered instead of ending the run", "WRITE PATH PROVEN" in out)
check("and did not call the session lost", "lost step" not in out)
check("exit status says success", code == 0)

print("[8] the gate: a stage built for 0xCC refuses everything else")
ecu = FakeECU(REF)
FakeSerial.ecu = ecu
ecu.segmask = 1 << 12
ecu.state = "cmd"
for seg, want_ok in ((0xCC, True), (0xC8, False), (0xC0, False), (0xC2, False)):
    ecu.out = bytearray()
    ecu.execute(K.CMD_ERASE_SECTOR, 0x5000, seg, 0)
    got = ecu.out[K.MON_LEAD]                   # skip the lead bytes the stage prepends
    check("erase in segment 0x%02X -> %s" % (seg, "allowed" if want_ok else "REFUSED"),
          got == (K.MON_OK if want_ok else K.MON_REFUSED))
check("only segment 0xCC changed", bytes(ecu.flash[:0xC5000]) == REF[:0xC5000])

print("[9] host-side range guard")
for bad, why in ((["--write", PATCH1, "--addr", "0xC00100", "--yes"], "boot sector"),
                 (["--write", PATCH1, "--addr", "0xCF0000", "--yes"], "past flash")):
    out, ecu, code = run(bad)
    check("refuses %s" % why, code != 0 and ecu.state == "stage1")
out, ecu, code = run(["--write", PATCH1, "--addr", "0xC00100", "--yes", "--allow-boot"])
check("--allow-boot unlocks it deliberately", "0xC0" in out and ecu.segmask == 1)

print("[10] a dirty line: the turnaround mangles the first byte the host sends")
# This is the real failure that bit us on the bench: a mangled command byte turned a
# READ into a PROGRAM and hung the monitor.  With the marker in front, the mangled byte
# is simply not 0xA5 and gets dropped, and the frame behind it is untouched.
FakeECU.mangle = staticmethod(lambda d: bytes([d[0] ^ 0x80]) + d[1:])
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--yes"])
check("survives a mangled lead byte on every frame", "WRITE PATH PROVEN" in out)
check("flash still ends up exactly as found", bytes(ecu.flash) == REF)

print("[11] a dirtier line: the command byte itself is mangled")
# Here the marker is intact but the body is not, so the checksum has to catch it: the
# monitor answers ST_CKS without executing anything and the host resends.
state = {"n": 0}


def every_third(d):
    state["n"] += 1
    if state["n"] % 3 == 0 and len(d) > 3:
        return d[:2] + bytes([d[2] ^ 0x04]) + d[3:]     # 0x02 READ -> 0x06, as on the bench
    return d


FakeECU.mangle = staticmethod(every_third)
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--yes"])
check("checksum caught it and the host resent", "corrupted in flight, resending" in out)
check("still completed correctly", "WRITE PATH PROVEN" in out)
check("no wrong command ever executed", bytes(ecu.flash) == REF)
check("nothing outside segment 0xCC was touched",
      all(seg == 0xCC for c, a, _, _ in ecu.log
          for seg in [(a >> 16) & 0xFF] if c in (K.CMD_ERASE_PAGE, K.CMD_ERASE_SECTOR,
                                                 K.CMD_PROG)))
FakeECU.mangle = staticmethod(lambda d: d)

print("[12] --write-selftest --sector : 4KB sector erase + the 32-page programming loop")
# This is the piece --write is built on: sector erase plus the 32-page programming loop.
# It has since been run on hardware for whole images -- 205 sectors verified byte for byte,
# zero retries -- so this test guards a proven path rather than standing in for one.
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--sector", "--yes"])
check("sector write path proven", "WRITE PATH PROVEN" in out)
check("all 32 pages programmed", "PROGRAM pattern: ok (32 page(s))" in out)
check("sector erase was the one used",
      any(c == K.CMD_ERASE_SECTOR for c, _, _, _ in ecu.log)
      and not any(c == K.CMD_ERASE_PAGE for c, _, _, _ in ecu.log))
check("flash ends byte-identical to how it started", bytes(ecu.flash) == REF)

print("[13] a payload byte goes missing and the monitor is left counting")
# This scenario was written when the loss was believed to be a turnaround effect on the
# K-line.  It was not: the cause was the host reconfiguring its serial port between
# transmitting and reading the reply, which drops bytes on Windows -- see SteadyPort -- and
# it has not reproduced since that was fixed.  The bench was never updated to say so.
#
# The test stays, with the claim corrected.  What it exercises is the recovery, and a
# payload framed by COUNT rather than by a marker is stuck by any lost byte whatever loses
# it: a real dropout, a driver, an adapter, the next host bug of this kind.  Keeping it is
# cheap; what had to go is the sentence calling it a measured property of the line.
swallowed = {"n": 0}


def swallow_a_payload_byte(d):
    if len(d) == K.MON_LEAD + K.PAGE + 2:          # this write is a page payload
        swallowed["n"] += 1
        if swallowed["n"] % 2 == 0:
            return d[1:]                            # one byte never arrives
    return d


FakeECU.mangle = staticmethod(swallow_a_payload_byte)
out, ecu, _ = run(["--write-selftest", "0xCC5000", "--sector", "--yes"])
check("the drop really happened", swallowed["n"] > 0)
check("host noticed and unstuck the monitor", "swallowed" in out)
check("recovered and finished", "WRITE PATH PROVEN" in out)
check("a short payload never reached flash", bytes(ecu.flash) == REF)
FakeECU.mangle = staticmethod(lambda d: d)

print("[14] --resume : re-running a finished write must skip, not rewrite")
# This is the whole answer to WinFlashECU dying partway through: the same command can be
# issued again and picks up where it stopped, because a sector that already holds the
# right bytes is left alone.
big2 = bytes((i ^ 0x5A) & 0xFF for i in range(2 * K.SECTOR))
PATCH3 = os.path.join(TMP, "patch3.bin")
open(PATCH3, "wb").write(big2)
out, ecu, _ = run(["--write", PATCH3, "--addr", "0xCC4000", "--yes"])
check("first pass wrote both sectors", out.count("erased, programmed, verified") == 2)
off = 0xCC4000 - K.FLASH_BASE
written = bytearray(REF)
written[off:off + 2 * K.SECTOR] = big2
check("the bytes landed", bytes(ecu.flash) == bytes(written))

# now re-run against an ECU that already holds the result
out2, ecu2, _ = run(["--write", PATCH3, "--addr", "0xCC4000", "--yes", "--resume"],
                    image=bytes(written))
check("second pass skipped everything", out2.count("already correct, skipped") == 2)
check("and erased nothing",
      not any(c in (K.CMD_ERASE_SECTOR, K.CMD_ERASE_PAGE) for c, _, _, _ in ecu2.log))
check("flash untouched by the resume", bytes(ecu2.flash) == bytes(written))

print("[15] --flash : the one-button path, and the self-calibration in front of it")
IMG = os.path.join(TMP, "image.bin")
open(IMG, "wb").write(REF)
out, ecu, _ = run(["--flash", IMG, "--yes"])
check("measured the link itself, no separate diagnostic", "link:" in out)
check("tuned itself to what it measured", "tuned to this bench" in out)
check("took a backup before erasing", "pre-flight backup" in out)
check("left the boot sector alone",
      all(a >= K.BOOT_END for c, a, _, _ in ecu.log
          if c in (K.CMD_ERASE_SECTOR, K.CMD_ERASE_PAGE, K.CMD_PROG)))
check("wrote every sector above it", out.count("erased, programmed, verified") == 206)
check("completed and verified", "WRITE COMPLETE AND VERIFIED" in out)
check("final read-back passed", "FINAL VERIFY PASSED" in out)
check("flash matches the image", bytes(ecu.flash) == REF)

check("and says the image's boot sector is the one already there",
      "boot sector matches the one already in the ECU" in out)

# --flash never writes the boot sector, so an image carrying a different one is only partly
# applied: that image's application ends up running under the ECU's existing loader.  It is
# usually the right default and occasionally not, and it used to happen in silence.
OTHER = os.path.join(TMP, "other-boot.bin")
_b = bytearray(REF)
_b[0:0x40] = bytes((x ^ 0x5A) for x in _b[0:0x40])
open(OTHER, "wb").write(bytes(_b))
out, ecu, _ = run(["--flash", OTHER, "--yes"])
check("a differing boot sector is reported, not passed over",
      "boot sector differs from this ECU's in 64 of 8192 bytes" in out)
check("and the report says it is not being written", "it is NOT being written" in out)
check("the boot sector really was left alone",
      bytes(ecu.flash[:0x2000]) == REF[:0x2000])
check("while everything above it took the new image",
      bytes(ecu.flash[0x2000:]) == bytes(_b)[0x2000:])

out, ecu, code = run(["--flash", IMG])
check("refuses without --yes", code != 0 and ecu.state == "stage1")
open(os.path.join(TMP, "short.bin"), "wb").write(REF[:1024])
out, ecu, code = run(["--flash", os.path.join(TMP, "short.bin"), "--yes"])
check("rejects an image that is not a full dump", code != 0)

print("[16] fast mode verifies by checksum, reliable mode byte-exact")
out, ecu, _ = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
check("completed", "WRITE COMPLETE AND VERIFIED" in out)
check("asked the ECU to add the sector up", any(c == K.CMD_CKSUM for c, _, _, _ in ecu.log))


def after_last_program(log):
    """The commands issued AFTER the final page went in -- i.e. the verify, and only it.

    Counting sector-sized reads over the whole session used to stand in for this, and it
    stopped meaning anything once the pre-flight backup ran in both modes: the backup reads
    the entire flash in sector-sized chunks, and the merge read and its own integrity check
    are also sector-sized.  What separates the two modes is not how many sector-sized
    commands they issue, it is what they do once the data is in."""
    last = max((i for i, e in enumerate(log) if e[0] == K.CMD_PROG), default=-1)
    return log[last + 1:]


tail = after_last_program(ecu.log)
check("fast verifies by asking the ECU to add it up",
      any(c == K.CMD_CKSUM and n == K.SECTOR for c, _, n, _ in tail))
check("and does NOT read the sector back byte by byte",
      not any(c == K.CMD_READ and n == K.SECTOR for c, _, n, _ in tail))
off = 0xCC5040 - K.FLASH_BASE
check("the bytes still landed", bytes(ecu.flash[off:off + 300]) == newbytes)

out, ecu, _ = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "reliable"])
tail = after_last_program(ecu.log)
check("reliable reads the sector back byte for byte",
      any(c == K.CMD_READ and n == K.SECTOR for c, _, n, _ in tail))
check("and uses no checksum shortcut to do it",
      not any(c == K.CMD_CKSUM and n == K.SECTOR for c, _, n, _ in tail))
check("the backup is still checked against the ECU in both",
      any(c == K.CMD_CKSUM for c, _, _, _ in ecu.log))

# 0xCC6000 on purpose: it HOLDS DATA in the reference.  A no-op erase on a sector that
# is already blank is indistinguishable from a real one, so the trap has to be set
# somewhere the difference can actually show.
check("fast mode takes a backup too, as the README has always promised",
      "pre-flight backup" in out)

print("[16b] a backup that did not read back intact stops the write")
# The backup is the copy everything else is predicated on, and the read path carries no
# per-chunk checksum -- a short read raises, but a wrong read of the right length is silent.
_r2 = FakeECU


class Liar(_r2):
    """Answers READ correctly but computes its CHECKSUM over different bytes."""

    def execute(self, cmd, off_, seg, cnt):
        if cmd == K.CMD_CKSUM:
            self.reply(K.MON_OK)
            s1, s2 = K.sums16(b"\x00" * cnt)          # deliberately not what it returned
            self.out += bytes([s1 & 0xFF, s1 >> 8, s2 & 0xFF, s2 >> 8])
            return
        _r2.execute(self, cmd, off_, seg, cnt)


FakeECU = Liar
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "reliable"])
FakeECU = _r2
check("a backup that does not verify is refused", "backup" in out.lower() and code != 0)
check("and nothing was erased after that refusal",
      not any(c in (K.CMD_ERASE_SECTOR, K.CMD_ERASE_PAGE) for c, _, _, _ in ecu.log))
check("flash untouched", bytes(ecu.flash) == REF)

print("[16c] the rate constant is learned from the ECU, not assumed")
# PROTOCOL 5: the divider is read back from the unit in front of us so the arithmetic is
# calibrated to THAT board's clock.  A different PLL setting would otherwise send every
# subsequent rate change to the wrong place.
_before = K.BAUD_CONST
out, ecu, _ = run(["--monitor"])
check("the divider register was actually read",
      any(c == K.CMD_READ and a == K.BRG_ADDR for c, a, _, _ in ecu.log))
check("and the constant was derived from it, not left at the default",
      "rate constant from this ECU" in out, out.splitlines()[-1][:60])
check("9600 x (130+1) is what it computes", "1257600" in out or "1258000" in out
      or ("%d" % (9600 * 131)) in out, "expected %d" % (9600 * 131))
check("and it does not leak into the next session", _before == K.BAUD_CONST,
      "%.0f vs %.0f" % (K.BAUD_CONST, _before))

print("[17] checksum still catches a sector that did not take")
_real = FakeECU              # bound BEFORE the name is rebound, or the call recurses


class Sticky(_real):
    def execute(self, cmd, off_, seg, cnt):
        if cmd == K.CMD_ERASE_SECTOR and ((seg << 16) | off_) == 0xCC6000:
            self.reply(K.MON_OK)          # claim success, change nothing -- the real bug
            return
        _real.execute(self, cmd, off_, seg, cnt)


FakeECU = Sticky
out, ecu, code = run(["--write", PATCH2, "--addr", "0xCC6000", "--yes", "--mode", "fast"])
FakeECU = _real
# This sector IS memory -- it just did not erase.  That has to stop the run, and it must not
# be confused with a region that holds no memory: both leave the contents unchanged, and only
# one of them is survivable.
check("the failure was noticed", "CHANGED NOTHING" in out or "VERIFY FAILED" in out)
check("did NOT claim a clean write", "WRITE COMPLETE AND VERIFIED" not in out)
check("called it a failing erase, not an unwritable region",
      "failing erase" in out or "VERIFY FAILED" in out)
check("and the shell is told", code != 0)

print("[17b] a region with NO MEMORY behind it is survivable, and is not silent")
# The distinction the bench forced: 0xC0F000 on the real ECU reads one word repeated, the
# same word an address past the end of flash gives.  Nothing can be stored there by any
# tool, so failing every run over it would make the failure signal meaningless -- but
# saying nothing would hide a part of the image that did not land.


class Holed(_real):
    UNMAPPED = (0xCC6000,)


FakeECU = Holed
out, ecu, code = run(["--write", PATCH2, "--addr", "0xCC6000", "--yes", "--mode", "fast"])
FakeECU = _real
check("recognised as having no memory behind it", "no memory behind this address" in out)
check("measured against the ECU's own answer, not a constant",
      "answers 9b 1e" in out)
check("the run completes rather than failing", "WRITE COMPLETE AND VERIFIED" in out
      and code == 0)
check("but the bytes that could not be stored are stated",
      "cannot be put there" in out or "nothing can store them" in out)
check("and an event carries it for a front end",
      True)   # exercised by group [18] below

print("[18] --progress json : the contract a front end is allowed to depend on")
# The GUI used to recover its progress bar with a regex over prose and decide SUCCESS by
# searching the log for a sentence.  Both are now events, so both are testable -- and a
# contract without a test is not a contract, it is a habit.


def run_json(argv, image=REF):
    """Run with machine progress on; return (events, human_log, ecu, exit_code)."""
    ecu = FakeECU(image)
    FakeSerial.ecu = ecu
    K.serial.Serial = FakeSerial
    old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
    # Held by name, not read back off sys.*: the tool deliberately points sys.stdout at the
    # log stream while json progress is on, so `sys.stdout.getvalue()` afterwards would hand
    # back the human log and the events would look empty.
    out_s, err_s = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out_s, err_s
    code = 0
    try:
        sys.argv = ["klinebsl.py", "--port", "FAKE", "--progress", "json"] + argv
        code = K.main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        raw, human = out_s.getvalue(), err_s.getvalue()
        sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
    events = []
    for line in raw.splitlines():
        if line.strip():
            events.append(json.loads(line))          # must parse; that is the contract
    return events, human, ecu, code


ev, human, ecu, _ = run_json(["--write", PATCH1, "--addr", "0xCC5040", "--yes",
                              "--mode", "fast"])
check("stdout is pure JSON, one object per line", bool(ev), "%d event(s)" % len(ev))
check("the human log went to stderr instead", "[kline]" in human)
check("no prose leaked onto the machine stream",
      all(isinstance(e, dict) and "event" in e for e in ev))
kinds = {e["event"] for e in ev}
check("emits stage, progress and result", {"stage", "progress", "result"} <= kinds,
      ", ".join(sorted(kinds)))
prog = [e for e in ev if e["event"] == "progress" and e.get("op") == "write"]
check("write progress counts sectors, with a total to divide by",
      bool(prog) and all(e.get("unit") == "sectors" and e.get("total") for e in prog))
check("progress never exceeds its own total",
      all(e["done"] <= e["total"] for e in prog))
check("progress only moves forward",
      [e["done"] for e in prog] == sorted(e["done"] for e in prog))
res = [e for e in ev if e["event"] == "result"]
check("a successful write reports exactly that",
      res and all(r.get("ok") for r in res), repr(res[-1]) if res else "no result")
check("the verdict needs no log reading at all",
      all("ok" in r and "what" in r for r in res))

# ...and the case the old log-searching verdict got wrong: a failure must SAY so
FakeECU = Sticky
ev, human, ecu, _ = run_json(["--write", PATCH2, "--addr", "0xCC6000", "--yes",
                              "--mode", "fast"])
FakeECU = _real
res = [e for e in ev if e["event"] == "result"]
check("a write that did not land reports ok=false", res and not all(r.get("ok") for r in res),
      repr(res[-1]) if res else "no result at all")
check("and names the sector it stopped on",
      any(r.get("addr") == 0xCC6000 for r in res), repr(res[-1]) if res else "")

# and the other outcome, which must NOT look like a failure to a front end
FakeECU = Holed
ev, human, ecu, code = run_json(["--write", PATCH2, "--addr", "0xCC6000", "--yes",
                                 "--mode", "fast"])
FakeECU = _real
res = [e for e in ev if e["event"] == "result"]
probs = [e for e in ev if e["event"] == "problem" and e.get("kind") == "unmapped"]
check("a region with no memory is reported as a problem, not a failure",
      probs and all(r.get("ok") for r in res), repr(probs[:1]))
check("the event says how many image bytes could not be stored",
      probs and probs[0].get("image_bytes", 0) > 0, repr(probs[:1]))
check("and the result carries the same count for a summary",
      any(r.get("unmapped_image_bytes") for r in res), repr(res[-1]) if res else "")
check("exit status stays zero, because nothing failed", code == 0)

# ...and the same must hold in reliable mode, where a full read-back follows.  On the bench
# the write said "complete and verified" and the final verify said "FAILED, 4096 bytes
# differ" about the same sector: two halves of one run contradicting each other.
FakeECU = Holed
ev, human, ecu, code = run_json(["--write", PATCH2, "--addr", "0xCC6000", "--yes",
                                 "--mode", "reliable"])
FakeECU = _real
res = [e for e in ev if e["event"] == "result"]
verify = [r for r in res if r.get("what") == "verify"]
check("the final read-back agrees with the write about unmapped memory",
      verify and all(r.get("ok") for r in verify), repr(verify[:1]))
check("no half of the run contradicts the other", all(r.get("ok") for r in res) and code == 0,
      "exit=%s results=%s" % (code, [(r.get("what"), r.get("ok")) for r in res]))
check("and it still says which bytes could not be stored",
      any(r.get("unmapped_image_bytes") for r in verify), repr(verify[:1]))

# Progress must be reported for sectors that are SKIPPED, not only for sectors written.
# Resume exists to skip work, so the run it was built for is the run that would otherwise
# report almost nothing -- 200 skips out of 206 and a progress bar that never moves.
ev, human, ecu, _ = run_json(["--write", PATCH3, "--addr", "0xCC4000", "--yes", "--resume"],
                             image=bytes(written))
prog = [e for e in ev if e["event"] == "progress" and e.get("op") == "write"]
check("a resume run still reports its position", len(prog) == 2,
      "%d progress event(s) for 2 sectors" % len(prog))
check("and marks which sectors were skipped", all(e.get("skipped") for e in prog))
check("position still reaches the end", prog and prog[-1]["done"] == prog[-1]["total"])

# a read, because its result carries the hash a user is told to keep
ev, human, ecu, _ = run_json(["--mon-read", "0xCC0000", "--len", "0x2000"])
check("a read session still emits a clean event stream",
      all("event" in e for e in ev) and any(e["event"] == "stage" for e in ev))

# and the flag must be OFF by default: the human path is unchanged for everyone else
out, ecu, _ = run(["--monitor"])
check("without the flag nothing machine-readable is emitted", "[kline]" in out
      and not any(l.startswith("{") for l in out.splitlines()))

print("[19] a write refuses an ECU that is not the one the stages were built for")
# The handshake proves nothing here: 0x00 -> 0xD5 belongs to the whole C166 mask-ROM family,
# so a stranger answers it perfectly and then accepts a stage assembled for somebody else's
# memory map.  What the gate reads instead is the silicon identifying itself.


class Stranger(_real):
    """Same family, same handshake, different device and a different flash size."""
    IDCHIP = 0x4201                     # CHIPID 0x42, not the 0x38 the stages were built for
    IDMEM = 0x3040                      # 64 blocks x 4 KB = 256 KB, not 832


FakeECU = Stranger
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
check("the write is refused", "NOT THE ECU THIS TOOL WAS BUILT FOR" in out)
check("and says which numbers disagree",
      "CHIPID 0x42" in out and "256 KB" in out)
check("nothing was erased", not any(c in (K.CMD_ERASE_PAGE, K.CMD_ERASE_SECTOR)
                                    for c, _, _, _ in ecu.log))
check("and the shell is told", code != 0)

# Reading is deliberately NOT gated: it cannot damage anything, and a read from an
# unrecognised unit is exactly the report that would let it be supported one day.
out, ecu, _ = run(["--mon-read", "0xCC0000", "--len", "0x1000"])
check("but reading an unknown ECU still works", "0xCC0000" in out
      and "NOT THE ECU" not in out)

out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast",
                      "--force-unknown-ecu"])
FakeECU = _real
check("--force-unknown-ecu writes anyway", "WRITE COMPLETE AND VERIFIED" in out)
check("and says it is overriding its own advice", "against the tool's own advice" in out)

# The reference part must of course still pass, or the gate is just a wall.
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
# The regression this class of bug deserves: a CORRECT part must not be refused.  The
# check that was here probed 0xD00000 -- outside the flash controller's decode -- and
# compared it against the controller's own no-memory word, which it can never equal.  The
# suite passed because the fake answered that word everywhere; the bench refused a
# known-good ECU, Infineon, CHIPID 0x38, 832 KB, all four modules answering.
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
check("a correct ECU is NOT refused by the layout check",
      "NOT THE ECU" not in out and code == 0)
check("and the address outside the controller's decode is left out of it",
      "0xD00000" not in out)

check("the real part is recognised and not obstructed",
      "CHIPID 0x38" in out and "NOT THE ECU" not in out and code == 0)
# A silicon respin must not become a support ticket: same device, later step.


class Respin(_real):
    IDCHIP = 0x3805                     # CHIPID 0x38, revision 0x05 instead of 0x01


class WrongMap(_real):
    """The trap the identification registers alone cannot catch.

    XC2768X and XC228xI answer the same handshake, report 832 KB, and put their fourth
    module at 0xD00000 with 0xCC0000 empty.  Same size, same 0xD5, wrong addresses -- and
    IDMEM is a trimmed `rw` field that some datasheets print at the die's capacity rather
    than the part's, so it cannot be trusted to tell them apart on its own.  What settles it
    is where the memory answers from."""
    UNMAPPED = (0xCC0000,)              # empty where ours is populated

    CTRL_HI = 0xD10000                  # its controller decodes the fourth module too

    def _fetch(self, addr, cnt):
        if 0xD00000 <= addr < 0xD10000:                       # populated where ours is not
            return bytes((addr + i) & 0xFF for i in range(cnt))
        return _real._fetch(self, addr, cnt)


FakeECU = Respin
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
FakeECU = _real
check("a later revision of the same device is accepted", code == 0
      and "NOT THE ECU" not in out)

FakeECU = WrongMap
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
FakeECU = _real
check("a part with the same IDs but a different flash map is refused",
      "NOT THE ECU THIS TOOL WAS BUILT FOR" in out and code != 0)
check("and it names the module that should have been there", "0xCC0000" in out)
check("and points at the layout it looks like", "XC2768X" in out)
check("nothing was erased there either",
      not any(c in (K.CMD_ERASE_PAGE, K.CMD_ERASE_SECTOR) for c, _, _, _ in ecu.log))

print("[20] asking the flash controller WHY, instead of inferring it from the symptom")
# Until the stage latched these two registers, an erase that reported success and changed
# nothing had exactly one story the host could tell: "changed nothing".  A sector that is
# write-protected, a sector with no memory behind it and a sector that genuinely failed all
# produce that same story.  The controller distinguishes them; these checks are that the
# distinction survives the trip to the host and reaches what the run decides.


class Locked(_real):
    """A unit with write protection installed over one sector, as PROCON would express it."""
    LOCKED = (0xCC6000,)
    PROCON = (0x3FF, 0x3FF, 0x3FD)          # module 2, logical sector 1 locked


FakeECU = Locked
out, ecu, code = run(["--write", PATCH2, "--addr", "0xCC6000", "--yes", "--mode", "fast"])
FakeECU = _real
check("a locked sector is called locked, not empty", "LOCKED" in out)
check("and NOT mistaken for a region with no memory",
      "no memory behind this address" not in out)
check("the reason is the controller's own, quoted", "protection error" in out.lower())
check("protection installed is announced before the first erase",
      "flash protection IS INSTALLED" in out)
check("the locked sector is named from PROCON", "module 2 sector 1" in out)
check("and a locked sector stops the run", code != 0
      and "WRITE COMPLETE AND VERIFIED" not in out)

# The opposite, and the one that matters for the bench unit: nothing installed at all.
out, ecu, code = run(["--write", PATCH1, "--addr", "0xCC5040", "--yes", "--mode", "fast"])
check("an unprotected ECU says so, once, up front", "no sector is locked" in out)
check("and the write proceeds normally", code == 0 and "WRITE COMPLETE AND VERIFIED" in out)

# The reason the stage latches rather than the host reading the live registers: Reset to
# Read clears exactly the bits worth reading, and the monitor runs it after every operation.
ecu = Locked(REF)
ecu.reg_op = ecu.reg_prot = 0
ecu._ran(0x02, prot_err=True)
check("the live register is clean by the time the host could look", ecu.reg_prot & 0x10 == 0)
check("but the latch kept the protection error", ecu.fstat_prot & 0x10 == 0x10)
check("and the latch says an erase is what ran", ecu.fstat_op & 0x02 == 0x02)

# The sentences themselves, because "no error reported" is a POSITIVE finding here -- it is
# what an address with nothing behind it looks like, and it must not read as an absence.
check("a clean status is described, not left blank",
      "no error reported" in (K.flash_status_note((0x02, 0x0000)) or ""))
check("a protection error is named", "PROTECTION ERROR"
      in (K.flash_status_note((0x02, 0x0010)) or ""))
check("a sequence error is named", "SEQUENCE ERROR"
      in (K.flash_status_note((0x12, 0x0000)) or ""))
check("nothing latched yields no claim at all", K.flash_status_note(None) is None)
check("installed-but-not-violated is not reported as an error",
      not any("ERROR" in s for s in K.describe_flash_status(0x02, 0x0001)))

# The documented sector, named.  The name is a label on a measurement, never a substitute
# for one: the run still has to prove the region is unwritable the same way as any other.
check("the reserved sector is known by address", 0xC0F000 in K.RESERVED)
check("and described as the device's own", "reserved by the device" in K.RESERVED[0xC0F000])

print("[21] the delivered stage is read back, because the write gate lives inside it")
# The monitor crosses with no checksum and no retry, and everything this tool claims about
# what a session can physically reach rests on one immediate buried in it:
#     AND R13,#SEGMASK      at image offset MON_SEGMASK_OFF
# A flipped bit there yields a monitor that greets normally and permits erases nobody asked
# for.  A greeting is not evidence that the right program landed; reading it back is.


def widen_the_gate(mon):
    """Deliver a stage whose gate allows every segment, while still greeting normally."""
    b = bytearray(mon)
    b[K.MON_SEGMASK_OFF] = 0xFF
    b[K.MON_SEGMASK_OFF + 1] = 0xFF
    return bytes(b)


def flip_one_far_byte(mon):
    """Corruption nowhere near the gate is still corruption: it is all executable code."""
    b = bytearray(mon)
    b[0x100] ^= 0x01
    return bytes(b)


FakeECU.corrupt = staticmethod(widen_the_gate)
out, ecu, rc = run(["--monitor"])
check("a stage that does not match what was sent is refused", "is NOT what was sent" in out)
check("and the write gate is named as the reason to refuse", "write gate" in out)
# "PING ok" is what the engine prints (klinebsl.py).  The first version of this line
# looked for "PING answered", which is this file's own check LABEL and appears in no
# program output at all -- so it passed unconditionally, including on runs that DID
# proceed.  An assertion that cannot fail is worse than no assertion.
check("the session does not proceed on it", "PING ok" not in out)
check("and it exits non-zero", rc != 0)

FakeECU.corrupt = staticmethod(flip_one_far_byte)
out, ecu, rc = run(["--monitor"])
check("a single flipped bit anywhere in the stage is caught",
      "1 of" in out and "bytes differ" in out)

FakeECU.corrupt = staticmethod(lambda d: d)
out, ecu, rc = run(["--monitor"])
check("an intact stage verifies and the session runs", "image verified" in out)
check("and that session still succeeds", rc == 0)

# Now that the fake enforces the gate it actually HOLDS, the refusal can be shown to have
# prevented something rather than merely to have been printed.
FakeECU.corrupt = staticmethod(widen_the_gate)
out, ecu, rc = run(["--flash", IMG, "--yes"])
check("a widened gate is caught before any erase is issued",
      not any(c in (K.CMD_ERASE_SECTOR, K.CMD_ERASE_PAGE, K.CMD_PROG) for c, _, _, _ in ecu.log))
check("and the ECU is left exactly as found", bytes(ecu.flash) == REF)
check("and a write refused this way exits non-zero", rc != 0)
FakeECU.corrupt = staticmethod(lambda d: d)

print("[23] a line dirty enough to corrupt the STAGE, not just the frames")
# The interaction the harness simplification in rx() deliberately leaves out of [10]/[11]/
# [13]: a link bad enough to mangle command frames is bad enough to mangle the 2 KB stage,
# which crosses with no checksum.  The correct outcome is not recovery -- there is no
# retry for a stage -- it is to refuse the session and ask for a power cycle, having
# erased nothing.


def flip_a_stage_byte(mon):
    b = bytearray(mon)
    b[0x40] ^= 0x20
    return bytes(b)


FakeECU.corrupt = staticmethod(flip_a_stage_byte)
out, ecu, rc = run(["--flash", IMG, "--yes"])
check("the run stops at the handoff", "did not arrive intact" in out)
check("it says a power cycle is what fixes it", "Power-cycle" in out)
check("nothing was erased or programmed",
      not any(c in (K.CMD_ERASE_SECTOR, K.CMD_ERASE_PAGE, K.CMD_PROG) for c, _, _, _ in ecu.log))
check("flash is untouched", bytes(ecu.flash) == REF)
check("the shell is told", rc != 0)
check("and a result event says nothing was written",
      any(e.get("event") == "result" and e.get("ok") is False and e.get("wrote_nothing")
          for e in run_json(["--flash", IMG, "--yes"])[0]))
FakeECU.corrupt = staticmethod(lambda d: d)

print("[24] closing the port is a wind-down, not a yank")
# A bare ser.close() drops the descriptor while the ECU may still be mid-answer -- a READ
# hands over up to 4 KB -- and leaves an interrupted write's monitor counting payload bytes
# that will never arrive.  close() now drains, resets both buffers, and only then closes.


out, ecu, code = run(["--monitor"])
check("the ECU has nothing left to send once the session ends", len(ecu.out) == 0)
check("and the session still reports success", code == 0)

# And a write can now be STOPPED between sectors instead of only by killing the process.
# The GUI used to close the window on a daemon thread, which ended the run mid-erase with
# the port dropped by the operating system.  There is no safe point inside a sector -- an
# erase that has begun must be followed by its programming or the old contents are gone --
# so the flag is checked at the boundary, where what is behind is verified and what is
# ahead is untouched.


class StopAfterFew(_real):
    """Ask for a stop once a few sectors have really been programmed."""

    def execute(self, cmd, off_, seg, cnt):
        _real.execute(self, cmd, off_, seg, cnt)
        if sum(1 for c, _, _, _ in self.log if c == K.CMD_ERASE_SECTOR) >= 3:
            K.request_stop()


FakeECU = StopAfterFew
out, ecu, code = run(["--flash", IMG, "--yes", "--mode", "fast"])
FakeECU = _real
K.clear_stop()
check("a stop request ends the write", "STOPPED at the operator's request" in out)
check("and it says the image is part-written", "PART-WRITTEN" in out)
check("and points at --resume", "--resume" in out)
check("the shell is told it did not finish", code != 0)
check("it stopped at a sector boundary, not inside one",
      ecu.pending is None or ecu.state == "cmd")
check("and a later run is not poisoned by the stale flag", not K.stop_requested())

# A REAL SIGINT, delivered while a sector is being programmed.  Both READMEs promise that
# Ctrl-C stops at a sector boundary; before the handler existed that was false -- the
# interrupt was raised wherever the interpreter stood, most often between an erase and the
# 32 programs that refill it, leaving 4 KB erased and part written.
import signal as _signal


class SigintMidSector(_real):
    """One SIGINT, delivered while a sector is half programmed.  Exactly one: the second
    press is meant to raise, and this scenario is about the first."""

    def __init__(self, image):
        _real.__init__(self, image)
        self.fired = False

    def execute(self, cmd, off_, seg, cnt):
        _real.execute(self, cmd, off_, seg, cnt)
        if (not self.fired and cmd == K.CMD_PROG
                and sum(1 for c, _, _, _ in self.log if c == K.CMD_ERASE_SECTOR) == 3):
            self.fired = True
            # raise_signal, NOT os.kill(getpid(), SIGINT).  On Windows os.kill can only
            # deliver CTRL_C_EVENT/CTRL_BREAK_EVENT; asked for SIGINT it terminates the
            # process instead, which is how this test silently killed the whole suite on
            # the Windows runner while passing on macOS.  raise_signal is portable.
            _signal.raise_signal(_signal.SIGINT)


FakeECU = SigintMidSector
out, ecu, code = run(["--flash", IMG, "--yes", "--mode", "fast"])
FakeECU = _real
K.clear_stop()
check("Ctrl-C is taken as a request to stop, not raised where it lands",
      "STOPPED at the operator's request" in out and "INTERRUPTED" not in out)

check("and it says a second press stops immediately", "Press again" in out)
check("the shell is told the write did not finish", code != 0)
check("no sector was left erased and only part written",
      all(sum(1 for b in ecu.flash[a - K.FLASH_BASE:a - K.FLASH_BASE + K.SECTOR] if b) > 0
          for a in {addr & ~(K.SECTOR - 1)
                    for c, addr, _, _ in ecu.log if c == K.CMD_ERASE_SECTOR}))

# Every command that can pass or fail must emit a result: the published contract is
# "succeeded iff at least one result arrived and none said ok:false", so a command that
# emits nothing reads as failure however well it went.  Three did exactly that, including
# the one that erases and programs flash to prove the write path.
for argv, what in ((["--monitor"], "selftest"),
                   (["--mon-read", "0xCC5000", "--len", "256"], "read"),
                   (["--write-selftest", "0xCC5000", "--yes"], "write-selftest")):
    evs, _, _, rc = run_json(argv)
    res = [e for e in evs if e.get("event") == "result"]
    check("%-14s emits exactly one result, ok:true" % what,
          len(res) == 1 and res[0].get("what") == what and res[0].get("ok") is True)
    check("%-14s and its exit status agrees" % what, rc == 0)

print("[25] exit status: what the shell is told, on every path that can fail")
# Not a hypothetical.  _main() has around a dozen exit points written in three conventions,
# and `return ok` -- the natural way to write any of them -- reports every success as a
# failure.  Pinned here because the next branch added will be written the same way.
check("success is 0", K.exit_status(True) == 0 and K.exit_status(None) == 0)
check("failure is 1", K.exit_status(False) == 1)
check("an explicit status is passed through", K.exit_status(2) == 2)

# The paths that used to report a failure as a success.  Each of these was measured
# exiting 0 while printing its own failure.
TRUNC = os.path.join(TMP, "truncated.bin")
open(TRUNC, "wb").write(REF[:1024])
out, _, rc = run(["--verify", TRUNC])
check("--verify refuses a file that is not the reference's length", rc != 0)
check("and does not call a partial comparison BYTE-IDENTICAL", "BYTE-IDENTICAL" not in out)
check("and says which lengths disagree", "LENGTH MISMATCH" in out)

out, _, rc = run(["--verify", K.REF_BIN])
check("but the real thing still passes", rc == 0 and "BYTE-IDENTICAL" in out)

# --stitch with segments missing: the old code wrote a shifted image over whatever was at
# --out, which is most likely the user's previous COMPLETE dump.
STITCH = os.path.join(TMP, "stitched.bin")
open(STITCH, "wb").write(REF)                       # a good file, already there
out, _, rc = run(["--stitch", "--out", STITCH])
check("--stitch refuses to write an incomplete image", rc != 0 and "NOT writing" in out)
check("and the good file that was already there survives",
      open(STITCH, "rb").read() == REF)

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL END-TO-END CHECKS PASSED")
sys.exit(1 if FAILS else 0)
