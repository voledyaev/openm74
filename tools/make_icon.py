#!/usr/bin/env python3
"""Draw the application icon, at every size the platforms ask for.

BUILD-TIME tool.  Run it after changing the design; the results are committed, so a
normal build needs neither this script nor Pillow:

    uv run --with pillow python tools/make_icon.py

Why generate rather than commit a drawing: this repository refuses to ship bytes nobody
can regenerate -- that is the entire argument of tools/build_stages.py -- and an icon is
no different in kind, just less dangerous.  It is also the only way to keep sixteen
pixels and a thousand pixels saying the same thing, because they have to be drawn
differently to do that.

THE DESIGN, and the constraint that produced it.  An icon is read at 16 px in a taskbar
far more often than at 512 in a gallery, so it gets ONE idea: a memory chip with a single
line running into it.  That is what this program is -- one wire, one chip, nothing else.
Everything that does not survive 16 px is drawn only above the size where it starts to
help: the pins appear at 32, the die outline at 64.  Nothing is scaled down into mush.

Colours come from the application's own dark palette (see THEMES in gui.py) so the icon
and the window it opens are recognisably the same program.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "src", "openm74", "icons")

BG = (27, 34, 43)           # near the dark palette's surface, a shade deeper
CHIP = (232, 234, 237)      # the palette's ink: the chip body
LINE = (77, 160, 235)       # the K-line, a brighter cousin of the palette's signal blue
PIN = (169, 178, 187)       # the palette's dimmed ink

# Windows wants these in one .ico; macOS wants an .iconset of these; Linux takes the PNG.
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def draw(size):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 16.0                          # one unit = 1/16 of the icon

    # Rounded square background.  Radius is a fraction of the size, so the silhouette is
    # the same shape at every scale -- a fixed radius would look square when small.
    r = max(2, int(size * 0.22))
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)

    # The chip body: a square, slightly right of centre, leaving room for the line.
    x0, y0 = 5.6 * s, 4.4 * s
    x1, y1 = 12.4 * s, 11.6 * s
    d.rounded_rectangle([x0, y0, x1, y1], radius=max(1, int(0.7 * s)), fill=CHIP)

    # Pins, only once they are more than one pixel wide.  Below 32 px they would merge
    # into the body and just thicken it, which reads as a blob rather than as a chip.
    if size >= 32:
        pw, ph = max(1, int(0.5 * s)), max(1, int(1.0 * s))
        for i in range(3):
            y = y0 + (1.6 + i * 2.2) * s
            d.rectangle([x1, y, x1 + ph, y + pw], fill=PIN)           # right side
            if i:                                                     # left side: the top
                d.rectangle([x0 - ph, y, x0, y + pw], fill=PIN)       # pin is the K-line's

    # The die: a hint of what is inside, and only where there is room to see it.
    if size >= 64:
        m = 1.7 * s
        d.rounded_rectangle([x0 + m, y0 + m, x1 - m, y1 - m], radius=max(1, int(0.4 * s)),
                            outline=BG, width=max(1, int(0.35 * s)))

    # The K-line: in from the left edge, into the chip's top-left pin.  Drawn LAST so it
    # is never covered, and thick enough to survive 16 px, where it is the only thing
    # besides the body that is still legible.
    w = max(2, int(1.0 * s))
    y = y0 + 1.6 * s + max(1, int(0.5 * s)) / 2.0
    d.line([1.4 * s, y, x0, y], fill=LINE, width=w)
    d.ellipse([1.4 * s - w * 0.9, y - w * 0.9, 1.4 * s + w * 0.9, y + w * 0.9], fill=LINE)
    return im


def main():
    try:
        from PIL import Image           # noqa: F401 -- imported to prove it is installed
    except ImportError:
        sys.exit("needs Pillow:  uv run --with pillow python tools/make_icon.py")
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    imgs = {n: draw(n) for n in SIZES}

    png = os.path.join(OUT, "openm74.png")
    imgs[512].save(png)                       # what Linux and the window manager use
    ico = os.path.join(OUT, "openm74.ico")    # Windows picks the size it needs from inside
    imgs[256].save(ico, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print("[icon] %s\n[icon] %s" % (png, ico))

    # .icns can only be built by macOS's own tool, so on anything else the committed file
    # simply stays as it is -- and the build script falls back to the .png, which is why
    # a Linux or Windows checkout can still produce a working bundle.
    if sys.platform == "darwin":
        iconset = os.path.join(OUT, "openm74.iconset")
        if not os.path.isdir(iconset):
            os.makedirs(iconset)
        for n in (16, 32, 128, 256, 512):
            imgs[n].save(os.path.join(iconset, "icon_%dx%d.png" % (n, n)))
            imgs[n * 2].save(os.path.join(iconset, "icon_%dx%d@2x.png" % (n, n)))
        icns = os.path.join(OUT, "openm74.icns")
        r = subprocess.call(["iconutil", "-c", "icns", iconset, "-o", icns])
        if r == 0:
            print("[icon] %s" % icns)
            for f in os.listdir(iconset):
                os.remove(os.path.join(iconset, f))
            os.rmdir(iconset)
        else:
            print("[icon] iconutil failed (%d); the .icns was left as it was" % r)
    else:
        print("[icon] not macOS: openm74.icns left untouched (only iconutil can write it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
