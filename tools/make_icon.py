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

THE DESIGN: a memory chip bearing a geometric 74, with blue pins. The digits are
paths, not a font, so every build produces the same mark without platform font files.
Render above the target resolution and downsample for smooth edges; omit the small
blue die marker below 32 px where it would blur into the digits.

Colours come from the application's own dark palette (see THEMES in gui.py) so the icon
and the window it opens are recognisably the same program.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "src", "openm74", "icons")

BG = (27, 34, 43)           # near the dark palette's surface, a shade deeper
CHIP = (232, 234, 237)      # the palette's ink: the chip body
LINE = (77, 160, 235)       # signal blue, shared by the pins and die marker

# Windows wants these in one .ico; macOS wants an .iconset of these; Linux takes the PNG.
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def draw(size):
    from PIL import Image, ImageDraw
    # Coordinates describe the approved 512 px master. Supersampling also keeps
    # diagonal digit strokes clean at taskbar sizes.
    scale = max(2, 2048 // size)
    unit = size * scale / 512
    im = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def line(points, fill, width):
        d.line([(x * unit, y * unit) for x, y in points], fill=fill,
               width=round(width * unit), joint="curve")

    def box(bounds, radius, fill):
        d.rounded_rectangle([v * unit for v in bounds], radius=radius * unit, fill=fill)

    box((0, 0, 511, 511), 108, BG)
    for y in (178, 256, 334):
        line([(96, y), (137, y)], LINE, 17)
        line([(375, y), (416, y)], LINE, 17)
    box((134, 123, 378, 389), 29, CHIP)
    line([(166, 194), (239, 194), (201, 316)], BG, 23)
    line([(300, 187), (262, 264), (334, 264)], BG, 22)
    line([(316, 220), (316, 319)], BG, 22)
    if size >= 32:
        box((171, 350, 224, 359), 4, LINE)
    return im.resize((size, size), Image.Resampling.LANCZOS)


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
    imgs[256].save(ico, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)],
                   append_images=[imgs[n] for n in (16, 24, 32, 48, 64, 128)])
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
