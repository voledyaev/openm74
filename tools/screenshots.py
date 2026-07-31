#!/usr/bin/env python3
"""Take the README's screenshots, so they can be retaken.

    uv run python tools/screenshots.py            # the ones that need no hardware
    uv run python tools/screenshots.py --grab docs/img/screenshot-reading.png

A screenshot is documentation that rots silently: rename a button and every picture in the
README is wrong, with nothing failing to say so.  Committing the pictures without the thing
that produces them makes retaking them a small research project, which is how a README ends
up showing an interface that no longer exists.  This drives the real window through the real
code path -- no mock-ups -- so retaking them is one command.

Windows on purpose: that is where the users are, so that is what the README should show.
The capture itself is the only platform-specific part.

`--grab` waits, then captures whichever openm74 window is on screen.  It exists for the one
picture this file cannot stage by itself: a read in progress needs an ECU, a power cycle and
a person to do it, so that shot is taken from a real session rather than faked from one.
"""
import ctypes
import os
import struct
import sys
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
IMG = os.path.join(ROOT, "docs", "img")

if sys.platform != "win32":
    sys.exit("Screenshots come from Windows: it is what most users see, and a README that\n"
             "shows a Mac window to a Windows audience answers the wrong question.")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
user32.SetProcessDPIAware()          # otherwise a scaled desktop hands back a blurry upscale

SRCCOPY = 0x00CC0020
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 2


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def write_png(path, rgb, width, height):
    """Minimal PNG writer.

    Not a missing dependency: adding an imaging library so a maintenance script can save a
    file would put it in the lock file that the shipped application is built from, and the
    small dependency list is a stated promise to people who are right to be wary of
    downloaded tuning binaries.  Filter 0 on every row, and let zlib do the work.
    """
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def read_png(path):
    """Read back a PNG this file wrote.

    Deliberately narrow: it understands the one encoding write_png produces (8-bit RGB,
    every row filtered 0) and refuses anything else rather than guessing.  A general PNG
    decoder is not the job -- redacting our own screenshots is.
    """
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s is not a PNG" % path)
    pos, idat, width, height = 8, [], 0, 0
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if (depth, colour) != (8, 2):
                raise ValueError("only 8-bit RGB is understood here")
        elif tag == b"IDAT":
            idat.append(body)
        pos += 12 + length
    raw = zlib.decompress(b"".join(idat))
    stride = width * 3
    rows = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        if raw[start]:
            raise ValueError("row %d is filtered; this reader only writes filter 0" % y)
        rows += raw[start + 1:start + 1 + stride]
    return rows, width, height


def redact(path, box, colour=(0x6b, 0x72, 0x80)):
    """Paint a flat block over part of a screenshot, in place.

    The window shows the image file that was chosen, and on a real bench that path runs
    through somebody's home directory -- their account name included.  The photographs in
    docs/img already have serial numbers covered for the same reason: a picture that
    documents the tool should not also publish who was standing at the bench.

    A block, not a blur: blurred text is recoverable often enough that treating it as
    removal is a mistake, and a solid block is honest about the fact that something was
    taken out.
    """
    rgb, width, height = read_png(path)
    x0, y0, w, h = box
    for y in range(max(0, y0), min(height, y0 + h)):
        row = y * width * 3
        for x in range(max(0, x0), min(width, x0 + w)):
            i = row + x * 3
            rgb[i], rgb[i + 1], rgb[i + 2] = colour
    write_png(path, bytes(rgb), width, height)
    print("  redacted %s at %s" % (os.path.relpath(path, ROOT), box))


def capture(hwnd, path):
    """Grab one window, cropped to what a person actually sees of it.

    Two rectangles matter and they are not the same one.  GetWindowRect includes the
    invisible resize border Windows has kept around every window since Vista -- capture that
    and every picture carries a few pixels of whatever was behind it down two edges, which
    reads as a dirty screenshot.  The frame the compositor actually draws is the DWM one.
    """
    full = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(full))
    frame = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                    ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
        frame = full                                  # no compositor: the rects coincide
    fw, fh = full.right - full.left, full.bottom - full.top
    if fw <= 0 or fh <= 0:
        raise RuntimeError("the window has no size (is it minimised?)")

    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, fw, fh)
    gdi32.SelectObject(memdc, bmp)
    # PrintWindow asks the window to draw itself, so it works even when something overlaps
    # it.  RENDERFULLCONTENT is what makes that true for anything drawn by the GPU rather
    # than by GDI; without it such windows come back as a black rectangle.
    if not user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT):
        gdi32.BitBlt(memdc, 0, 0, fw, fh, hdc, 0, 0, SRCCOPY)   # last resort: off the screen

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = fw, -fh              # negative: top-down, like everyone else
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(fw * fh * 4)
    gdi32.GetDIBits(memdc, bmp, 0, fh, buf, ctypes.byref(hdr), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)

    x0, y0 = frame.left - full.left, frame.top - full.top
    cw, ch = frame.right - frame.left, frame.bottom - frame.top
    src = buf.raw
    # BGRA -> RGB, one row at a time.  Slicing per channel beats walking pixels in Python
    # by enough to matter: these are a couple of megapixels each.
    out = bytearray()
    for y in range(y0, y0 + ch):
        line = src[(y * fw + x0) * 4:(y * fw + x0 + cw) * 4]
        b, g, r = line[0::4], line[1::4], line[2::4]
        row = bytearray(cw * 3)
        row[0::3], row[1::3], row[2::3] = r, g, b
        out += row

    write_png(path, bytes(out), cw, ch)
    print("  %-44s %d x %d" % (os.path.relpath(path, ROOT), cw, ch))


def find_window(match="openm74", exclude=None):
    """The topmost visible window whose title matches. Titles are all we have to go on --
    the frozen application is another process, so there is no object to ask."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if match.lower() in title.lower() and (not exclude or exclude.lower()
                                                   not in title.lower()):
                found.append((hwnd, title))
        return True

    user32.EnumWindows(each, 0)
    return found


def settle(root, seconds=0.6):
    """Let Tk finish drawing before the shutter.  update() returns once the events are
    processed, not once the compositor has put the result on screen."""
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def staged():
    """The pictures that need no ECU: the window at rest, and the wiring help page.

    Both languages, because the README has both halves and a Russian reader landing on a
    Russian page to be shown an English window learns nothing about what they will see.
    """
    if not os.path.isdir(IMG):
        os.makedirs(IMG)
    # The first log line names the folder the tool keeps its files in, so without this the
    # README would publish whatever the maintainer's home directory is called.
    os.environ["OPENM74_DIR"] = os.environ.get("OPENM74_DIR") or r"C:\openm74"
    if not os.path.isdir(os.environ["OPENM74_DIR"]):
        try:
            os.makedirs(os.environ["OPENM74_DIR"])
        except OSError:
            del os.environ["OPENM74_DIR"]           # not writable here; keep going regardless
    for lang in ("ru", "en"):
        os.environ["OPENM74_THEME"] = "light"       # light: it prints, and it is the default
        for mod in [m for m in list(sys.modules) if m.startswith("openm74")]:
            del sys.modules[mod]
        import tkinter as tk
        from openm74 import gui as G
        from openm74 import i18n

        # Pinned, not left to detection: the catalogue is only consulted through t(), and
        # main() -- which this script deliberately does not call -- is what normally decides.
        i18n.set_lang(lang)
        root = tk.Tk()
        G.init_fonts()
        app = G.App(root)
        settle(root, 1.0)
        capture(int(root.frame(), 16), os.path.join(IMG, "screenshot-main-%s.png" % lang))

        # Tab 1 is Wiring in both catalogues -- the page a first-timer actually needs, and
        # the one carrying the connector drawing.
        helpwin = app.help_window(open_tab=1)
        settle(root, 1.0)
        capture(int(helpwin.frame(), 16),
                os.path.join(IMG, "screenshot-wiring-%s.png" % lang))
        # The interface reschedules itself to drain the worker's output.  Tearing the root
        # down with one of those still pending makes Tk report a dead callback -- noise that
        # looks like a failure in a script whose whole output is meant to be read.
        for aid in root.tk.splitlist(root.tk.call("after", "info")):
            try:
                root.after_cancel(aid)
            except Exception:
                pass
        root.destroy()


def grab(path, delay=8.0):
    """Capture a window that is already open, after a countdown.

    For the read-in-progress picture.  Staging it here would mean faking a session, and a
    picture of a fake session is the one kind of screenshot that can mislead: it is offered
    as evidence that the thing works.
    """
    print("bring the openm74 window forward; capturing in %.0f s" % delay)
    for left in range(int(delay), 0, -1):
        print("  %d" % left, end="\r")
        time.sleep(1)
    wins = find_window("openm74")
    if not wins:
        sys.exit("no openm74 window found -- is it running?")
    hwnd, title = wins[0]
    print("capturing: %s" % title)
    capture(hwnd, path if os.path.isabs(path) else os.path.join(ROOT, path))


if __name__ == "__main__":
    if "--redact" in sys.argv:
        i = sys.argv.index("--redact")
        f = sys.argv[i + 1]
        x, y, w, h = (int(v) for v in sys.argv[i + 2:i + 6])
        redact(f if os.path.isabs(f) else os.path.join(ROOT, f), (x, y, w, h))
    elif "--grab" in sys.argv:
        i = sys.argv.index("--grab")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else "docs/img/grab.png"
        grab(target)
    else:
        staged()
        print("\nStill needed, and it cannot be staged: a read in progress.\n"
              "  start one, then:  uv run python tools/screenshots.py "
              "--grab docs/img/screenshot-reading-ru.png")
