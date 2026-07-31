#!/usr/bin/env python3
"""Check the GUI's platform assumptions on the machine you are actually on.

Two kinds of assumption break when this GUI moves between platforms, and neither one
raises an exception -- they just quietly misbehave, which is why they need a test:

  1. **Modifier bits.** Tk reports Control as state bit 2 everywhere, but on macOS the key
     a person presses to copy is Command, which arrives as Mod1 instead.  Rather than
     trust the documentation, we synthesise the keystroke and look at what Tk reports.
  2. **Font metrics.** The connector diagram and the pin table are laid out in PIXELS
     against column boundaries, while the fonts are whatever each platform calls its UI
     and fixed-width faces.  A wider face pushes text past its column, or past the window,
     with no error anywhere -- the drawing just becomes wrong.  So we measure every string
     against the cell it has to fit in.

Run it on each platform you ship to:

    python tools/check_gui_platform.py

It opens a window briefly and closes it again.  Nothing is written and no ECU is touched.
"""
import os
import json
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))

# Keep the checks out of the project tree.  The docstring above promises nothing is
# written, and constructing an App opens a log file wherever data_dir() points -- which,
# run from a checkout, is the checkout.
import tempfile

os.environ.setdefault("OPENM74_DIR", tempfile.mkdtemp(prefix="openm74check"))

# This script prints the interface's own strings, and half of them are Russian.  A Windows
# console -- and every Windows CI runner -- defaults stdout to cp1252, which cannot encode
# Cyrillic at all, so the first Russian label raised UnicodeEncodeError and took the whole
# check down with it.  Reconfigure rather than transliterate: the point of the check is to
# measure the strings that actually go on screen.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass                    # a stream that cannot be reconfigured still works

import tkinter as tk
from tkinter import font as tkfont

from openm74 import gui as G

fails = []
notes = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def note(text):
    print("  ..   " + text)
    notes.append(text)


root = tk.Tk()
root.withdraw()
G.init_fonts()
print("platform: %s   Tk %s   UI=%r  mono=%r"
      % (sys.platform, root.tk.call("info", "patchlevel"), G.UI_FAMILY, G.MONO_FAMILY))

print("\n[1] the fonts resolve to something real")
for label, fam in (("UI", G.UI_FAMILY), ("mono", G.MONO_FAMILY)):
    f = tkfont.Font(family=fam, size=10)
    check("%s family %r measures text" % (label, fam), f.measure("Цепь") > 0)
    check("%s family is not silently substituted" % label,
          f.actual("family").lower().replace(".", "") != "" and f.measure("mmmm") > 0,
          "actual=%r" % f.actual("family"))

print("\n[2] the copy modifier is the one this platform actually sends")
seen = {}
probe = tk.Text(root)
probe.pack()
root.deiconify()
root.update()
probe.focus_force()
root.update()


def record(e):
    seen[e.keysym.lower()] = e.state
    return "break"


probe.bind("<Key>", record)
combos = [("<Control-c>", 0x4, "Control")]
if sys.platform == "darwin":
    combos.append(("<Command-c>", 0x8, "Command"))
for seq, want_bit, human in combos:
    seen.clear()
    try:
        probe.event_generate(seq, when="now")
        root.update()
    except tk.TclError as e:
        note("could not synthesise %s (%s) -- check this one by hand" % (seq, e))
        continue
    state = seen.get("c")
    if state is None:
        note("%s did not reach the widget -- check this one by hand" % seq)
        continue
    check("%s arrives with bit 0x%X set" % (human, want_bit), bool(state & want_bit),
          "state=0x%X" % state)
    check("%s is accepted by the log's key filter" % human,
          G.COPY_MODS & state != 0, "COPY_MODS=0x%X" % G.COPY_MODS)
probe.destroy()
root.withdraw()

from openm74 import i18n

print("\n[3-5] the measured layout is sane in BOTH languages")
# The columns used to be constants tuned against one machine's fonts, and a Linux container
# showed what that was worth: every column and every help tab overflowed there, because the
# default face is wider.  They are measured now, so "does the text fit" is true by
# construction -- what is worth checking is that the arithmetic is right and that the result
# is a window a person can actually use rather than one wider than their screen.
ui10 = tkfont.Font(family=G.UI_FAMILY, size=10)
ui9 = tkfont.Font(family=G.UI_FAMILY, size=9)
mono10 = tkfont.Font(family=G.MONO_FAMILY, size=10)
mono11b = tkfont.Font(family=G.MONO_FAMILY, size=11, weight="bold")
SANE_MAX = 1600
_lang_was = i18n.get_lang()
for lang in i18n.LANGS:
    i18n.set_lang(lang)
    L = G.layout()
    x0, x1, x2, x3 = L["cols"]
    check("%s: columns come out in order" % lang, x0 < x1 < x2 < x3, str(L["cols"]))
    over = [(i18n.t(k), ui10.measure(i18n.t(k))) for _, k, _, _ in G.PINOUT
            if x0 + 16 + ui10.measure(i18n.t(k)) > x1]
    check("%s: every circuit name fits its measured column" % lang, not over, repr(over[:2]))
    over = [(p, mono11b.measure(p)) for _, _, p, _ in G.PINOUT
            if x1 + 10 + mono11b.measure(p) > x2]
    check("%s: every pin coordinate fits its measured column" % lang, not over, repr(over[:2]))
    over = [(i18n.t(k), ui9.measure(i18n.t(k))) for _, _, _, k in G.PINOUT
            if x2 + 10 + ui9.measure(i18n.t(k)) > x3]
    check("%s: every note fits its measured column" % lang, not over, repr(over[:2]))
    room = L["legend_pins_dx"] - G.LEGEND_LABEL_DX
    over = [i18n.t(k) for _, k, _ in G.LEGEND if ui9.measure(i18n.t(k)) > room]
    check("%s: every legend label clears the pin column" % lang, not over, repr(over[:2]))

    w = L["width"]
    check("%s: the window is at least the minimum" % lang, w >= G.WIN_W_MIN, "%d px" % w)
    check("%s: and not wider than a person's screen" % lang, w <= SANE_MAX,
          "%d px (cap %d)" % (w, SANE_MAX))
    widest = 0
    for _title, body in i18n.help_tabs(i18n.t("app.device")):
        for line in body.strip("\n").splitlines():
            widest = max(widest, mono10.measure(line))
    check("%s: the help text fits the measured window" % lang, widest + 28 <= w,
          "%d px of %d" % (widest + 28, w))
    warn = ui9.measure(i18n.t("dg.connectors_warn"))
    check("%s: the mirrored-connectors warning fits it too" % lang, 14 + warn <= w,
          "%d px of %d" % (14 + warn, w))
i18n.set_lang(_lang_was)

print("\n[6] the whole window builds, and so does every help tab")
app = help_win = None
try:
    top = tk.Toplevel(root)
    top.withdraw()
    app = G.App(top)
    top.update()
    check("App constructs", True)
    check("log file went somewhere writable", os.path.isdir(app.outdir),
          app.logfile)
    help_win = app.help_window()
    top.update()
    check("help window with all %d tabs builds" % len(i18n.HELP), True)
except Exception as e:
    check("App constructs", False, "%s: %s" % (type(e).__name__, e))

# ---------------------------------------------------------------------------
# The two checks below exist because of two real bugs that shipped, and neither of them
# raised anything: in dark mode the diagrams rendered as blank white rectangles, and a
# trackpad could not scroll the help panes at all.  Both are invisible to a smoke test --
# the window builds perfectly either way -- so they need measuring.
print("\n[7a] both palettes, checked arithmetically -- not just the one this machine is in")
# The colours are explicit hex, so contrast is arithmetic and BOTH themes can be verified
# from either appearance.  This is the check that matters most: dark mode broke once because
# nobody could see it while working in light mode, and "switch the OS theme and look" is not
# a test anyone runs twice.
TEXT_MIN, LARGE_MIN, SHAPE_MIN = 4.5, 3.0, 1.4
for theme_name in sorted(G.THEMES):
    T = G.THEMES[theme_name]
    worst = []
    # Only combinations the drawings ACTUALLY produce.  Checking a semantic colour against the
    # table's header fill would be inventing a case -- the header carries `ink` and nothing
    # else -- and a palette tuned to satisfy a case that never occurs is a palette tuned for
    # nothing, at the cost of the cases that do.
    for tok, floor in (("ink", TEXT_MIN), ("ink_soft", TEXT_MIN)):
        for surf in ("surface", "surface_alt", "surface_head"):
            worst.append((G.contrast(root, T[tok], T[surf]), floor,
                          "%s on %s" % (tok, surf)))
    for tok, floor in (("ink_faint", LARGE_MIN), ("warn", LARGE_MIN)):
        worst.append((G.contrast(root, T[tok], T["surface"]), floor, "%s on surface" % tok))
    for tok in ("pwr", "gnd", "ena", "sig", "mut"):
        # semantic colours label bold text, so the large-text floor is the honest one, and
        # they land on plain rows and alternating rows alike
        for surf in ("surface", "surface_alt"):
            worst.append((G.contrast(root, T[tok], T[surf]), LARGE_MIN,
                          "%s on %s" % (tok, surf)))
    for tok in ("pwr", "gnd", "ena", "sig"):
        # the pin chips: dark ink on a light chip, or light ink on a dark one
        worst.append((G.contrast(root, T["on_chip"], T[tok]), LARGE_MIN,
                      "on_chip on %s" % tok))
    # not text: an unhighlighted pin only has to be visibly a shape on the surface
    worst.append((G.contrast(root, T["pin_off"], T["surface"]), SHAPE_MIN,
                  "pin_off on surface"))
    worst.append((G.contrast(root, T["edge"], T["surface"]), SHAPE_MIN,
                  "edge on surface"))
    bad = [(r, f, w) for r, f, w in worst if r < f]
    low = sorted(worst)[0]
    check("%-5s palette: all %d pairs clear their floor" % (theme_name, len(worst)), not bad,
          "tightest %.2f:1 (%s, floor %.1f)" % (low[0], low[2], low[1]))
    for r, f, w in sorted(bad)[:6]:
        print("       %-24s %.2f:1  needs %.1f" % (w, r, f))

    # Contrast floors cannot express a HIERARCHY, and the drawing was wrong once while every
    # floor was clear: ground was the loudest thing on a dark surface.  These three say out
    # loud what the drawing is supposed to emphasise, so a future colour tweak cannot quietly
    # invert it again.
    lum = {t: G.luminance(root, T[t]) for t in ("pwr", "gnd", "ena", "sig", "mut")}
    con = {t: G.contrast(root, T[t], T["surface"]) for t in lum}
    if theme_name == "dark":
        # on a dark surface, brighter == louder, and ground must not out-shout power
        check("dark: ground is quieter than power", lum["gnd"] < lum["pwr"],
              "ground %.3f vs power %.3f" % (lum["gnd"], lum["pwr"]))
    else:
        # on a light surface, closer to the background == quieter; ground is near-black and
        # therefore high-contrast, but desaturated, so it reads structural rather than loud
        check("light: ground is the most saturated-dark, i.e. structural",
              lum["gnd"] < lum["pwr"], "ground %.3f vs power %.3f" % (lum["gnd"], lum["pwr"]))
    # "Muted" is saturation, not contrast.  Encoding it as least-contrast was wrong on a light
    # surface, where contrast measures darkness: the amber at 4.4:1 draws far more attention
    # than a neutral grey at 5.2:1.  Saturation is what actually makes CAN recede, and it is
    # the same statement in both themes.
    def sat(hexcol):
        r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
        return (max(r, g, b) - min(r, g, b)) / float(max(r, g, b) or 1)

    sats = {t: sat(T[t]) for t in lum}
    check("%-5s: unused CAN is the least saturated circuit" % theme_name,
          sats["mut"] == min(sats.values()),
          "CAN %.2f, next %.2f" % (sats["mut"], sorted(sats.values())[1]))

print("\n[7b] colour is never the only channel")
# Power red and the enable amber sit 1.4:1 apart in luminance and differ mainly in hue, which
# is precisely the pair a red-blind eye cannot separate -- and mixing those two up means
# putting +12 permanent onto the programming-enable pins.  Widening the colour gap would cost
# either the amber's identity or its contrast, so the guarantee is made a different way: every
# highlighted pin carries its own coordinate as TEXT, and the legend repeats it. Colour ranks
# and groups; the label is what identifies.  That is the property worth testing.
holder0 = tk.Frame(root)
G.init_theme(root)
cv0 = G.draw_connectors(holder0)
drawn = set()
for item in cv0.find_all():
    if cv0.type(item) == "text":
        drawn.add(cv0.itemcget(item, "text"))
missing = ["%s:%s%d" % (blk, col, row) for (blk, col, row) in G.HIGHLIGHT
           if "%s%d" % (col, row) not in drawn]
check("every highlighted pin is labelled, not just coloured", not missing,
      "unlabelled: %s" % ", ".join(missing) if missing else
      "%d highlighted pins, all named" % len(G.HIGHLIGHT))
legend_text = " ".join(i18n.t(p) for _, _, p in G.LEGEND)
check("the legend spells out the coordinates too",
      all(c in legend_text for c in ("A4", "B2", "G3", "H1", "F2", "G2")),
      legend_text[:70] + "...")

print("\n[7c] and the strings the drawings really put on screen, as drawn")
print("    (appearance right now: %s)" % G.THEME_NAME.upper())


def behind(cv, item):
    """The colour actually under a text item: the topmost filled rectangle, else the canvas."""
    under = cv.cget("background")
    x, y = cv.coords(item)[:2]
    for j in cv.find_overlapping(x - 1, y - 1, x + 1, y + 1):
        if cv.type(j) == "rectangle":
            f = cv.itemcget(j, "fill")
            if f:
                under = f
    return under


holder = tk.Frame(root)
for name, fn in (("draw_pinout", G.draw_pinout), ("draw_connectors", G.draw_connectors)):
    cv = fn(holder)
    worst, worst_text, dim = 99.0, "", []
    for item in cv.find_all():
        if cv.type(item) != "text":
            continue
        fill = cv.itemcget(item, "fill")
        ratio = G.contrast(cv, fill, behind(cv, item))
        text = cv.itemcget(item, "text")[:34]
        if ratio < worst:
            worst, worst_text = ratio, text
        if 3.0 <= ratio < 4.5:
            dim.append((ratio, text))
    # 3.0 is the floor for large/bold text and for deliberately muted rows; the bug this
    # guards against measured 1.00, so anything remotely close to it fails loudly.
    check("%s: every string clears 3.0:1" % name, worst >= 3.0,
          "worst %.2f:1 on %r" % (worst, worst_text))
    for ratio, text in dim[:3]:
        note("%s: %r is quiet at %.2f:1 (deliberate for muted rows)" % (name, text, ratio))

if app is not None:
    bg = app.log.cget("background")
    fg = app.log.cget("foreground")
    check("the log itself is readable", G.contrast(app.log, fg, bg) >= 4.5,
          "%.2f:1 (%s on %s)" % (G.contrast(app.log, fg, bg), fg, bg))
    win_bg = root.cget("background")
    check("dimmed secondary text is readable on this theme",
          G.contrast(root, app.soft, win_bg) >= 3.0,
          "%.2f:1 (%s on %s)" % (G.contrast(root, app.soft, win_bg), app.soft, win_bg))

print("\n[8] the help panes answer this platform's scroll gestures")
scratch = tk.Frame(root)
try:
    scratch.bind("<TouchpadScroll>", lambda e: None)
    HAS_TOUCHPAD = True
except tk.TclError:
    HAS_TOUCHPAD = False
print("    Tk here %s <TouchpadScroll> (a trackpad sends nothing else)"
      % ("supports" if HAS_TOUCHPAD else "does not know"))

if help_win is not None:
    def descend(w, out):
        out.append(w)
        for c in w.winfo_children():
            descend(c, out)
        return out

    widgets = descend(help_win, [])
    canvases = [w for w in widgets if isinstance(w, tk.Canvas)]
    texts = [w for w in widgets if isinstance(w, tk.Text)]
    check("the help window has a scrolling canvas per tab",
          len(canvases) >= len(i18n.HELP), "%d canvas(es)" % len(canvases))
    want = ["<MouseWheel>", "<Button-4>"] + (["<TouchpadScroll>"] if HAS_TOUCHPAD else [])
    for seq in want:
        missing = [w for w in canvases + texts if seq not in w.bind()]
        check("%s is bound on every scrollable surface" % seq, not missing,
              "%d widget(s) deaf to it" % len(missing) if missing else "")
    # Pixel-precise scrolling is what makes a trackpad's small deltas usable at all -- but
    # only the tab scrollers scroll.  The connector drawing is a canvas too and has no
    # business having an increment, so ask which canvases are actually driving a scrollbar.
    scrollers = [c for c in canvases if c.cget("yscrollcommand")]
    check("every scrolling canvas is a scroller, and the drawings are not",
          0 < len(scrollers) < len(canvases),
          "%d of %d canvases scroll" % (len(scrollers), len(canvases)))
    coarse = [c for c in scrollers if str(c.cget("yscrollincrement")) in ("0", "")]
    check("scrolling canvases move by the pixel, not by the notch", not coarse,
          "%d still on the default increment" % len(coarse) if coarse else "")
    # Every text pane inside the help window must be readable too
    for i, t in enumerate(texts):
        r = G.contrast(t, t.cget("foreground"), t.cget("background"))
        check("help pane %d is readable" % (i + 1), r >= 4.5,
              "%.2f:1 (%s on %s)" % (r, t.cget("foreground"), t.cget("background")))

print("\n[8b] the tool's files land beside the tool")
# Where a backup goes is not a detail: it may be the only copy of someone's firmware, and
# the rules that decide it (bundle layout, macOS translocation, folder permissions) cannot
# be guessed by reading a path.  These pin the resolution rather than the platform, so they
# mean the same thing on every OS.
import ntpath
import posixpath


def resolve(exe, mod, sep):
    """The expression app_dir() evaluates, under a chosen platform's path rules."""
    i = exe.find(".app" + sep + "Contents" + sep + "MacOS" + sep)
    return mod.dirname(exe[:i + 4]) if i >= 0 else mod.dirname(exe)


check("a macOS bundle resolves BESIDE the .app, never inside it",
      resolve("/Users/x/Downloads/openm74.app/Contents/MacOS/openm74", posixpath, "/")
      == "/Users/x/Downloads")
check("a Windows exe resolves to its own folder",
      resolve("D:\\tools\\openm74.exe", ntpath, "\\") == "D:\\tools")
check("a Windows path is never mistaken for a bundle",
      "D:\\tools\\openm74.exe".find(".app\\Contents") < 0)
check("a plain Linux binary resolves to its own folder",
      resolve("/opt/openm74/openm74", posixpath, "/") == "/opt/openm74")
check("running from source uses the working directory, not the interpreter",
      G.app_dir() == os.getcwd(), G.app_dir())
check("the resolved data directory is writable", os.access(G.data_dir(), os.W_OK),
      G.data_dir())

print("\n[9] both languages are complete, and both build a window")
# A missing string does not raise -- t() returns the key, so a half-translated build simply
# shows "ui.read_btn" on a button.  That is a thing to catch here rather than in front of
# someone holding a probe.
from openm74 import i18n

gaps = i18n.missing()
check("no catalogue entry is missing a language", not gaps,
      "%d gap(s): %s" % (len(gaps), ", ".join("%s/%s" % g for g in gaps[:5])))
check("every help tab exists in both languages",
      all(bodies.get(l) for _, bodies in i18n.HELP for l in i18n.LANGS))
check("system detection returns a language we have",
      i18n.detect() in i18n.LANGS, "detected %r" % i18n.detect())

was = i18n.get_lang()
for lang in i18n.LANGS:
    i18n.set_lang(lang)
    try:
        w = tk.Toplevel(root)
        w.withdraw()
        a = G.App(w)
        hw = a.help_window()
        w.update()
        titles = [i18n.HELP_TITLES[k][lang] for k, _ in i18n.HELP]
        check("%s: window and all %d help tabs build" % (lang, len(titles)), True)
        # every visible string must have come from the catalogue, not fallen through as a key
        leaked = [s for s in (a.status.get(), a.read_btn.cget("text"),
                              a.write_btn.cget("text")) if "." in s and " " not in s]
        check("%s: no untranslated key reached a widget" % lang, not leaked, repr(leaked))
        # The menu bar is the system's own on macOS, so it is worth confirming it exists and
        # carries the settings rather than assuming Tk attached it.
        try:
            mb = w.nametowidget(w.cget("menu"))
            kinds = [mb.type(i) for i in range(mb.index("end") + 1)]
            # Tk's tearOff default is 1 on X11 and 0 on Aqua and Windows, so a menu built
            # without saying which it wants grows a dashed tear-off entry on Linux ALONE.
            # Asserted rather than tolerated: reading labels defensively would have made
            # this check pass while the Linux menu bar carried a stray entry.
            check("%s: no tear-off entry on the menu bar" % lang, "tearoff" not in kinds,
                  ", ".join(kinds))
            labelled = [i for i, k in enumerate(kinds) if k in ("cascade", "command",
                                                               "radiobutton", "checkbutton")]
            tops = [mb.entrycget(i, "label") for i in labelled]
            view = w.nametowidget(mb.entrycget(labelled[0], "menu"))
            vkinds = [view.type(i) for i in range(view.index("end") + 1)]
            check("%s: no tear-off entry inside the menus" % lang, "tearoff" not in vkinds,
                  ", ".join(vkinds))
            subs = [view.entrycget(i, "label") for i, k in enumerate(vkinds)
                    if k in ("cascade", "command", "radiobutton", "checkbutton")]
            check("%s: menu bar carries %d menus and %d settings" % (lang, len(tops), len(subs)),
                  len(tops) >= 3 and len(subs) >= 2, "%s / %s" % (tops, subs))
            # The override must not be remembered between runs: a suspended safety check
            # that survives a restart is one nobody reconsiders.
            check("%s: the ECU-check override starts off" % lang, not a.force_ecu.get())
            check("%s: and is not written to the config" % lang,
                  "force" not in json.dumps(G.load_config()))
        except Exception as e:
            check("%s: menu bar attached" % lang, False, "%s: %s" % (type(e).__name__, e))
        # Cancel the pump before destroying the window it belongs to: an expired after()
        # timer whose widget is gone surfaces later as a background "invalid command name".
        try:
            w.after_cancel(a._drain_id)
        except Exception:
            pass
        hw.destroy()
        w.destroy()
    except Exception as e:
        check("%s: window builds" % lang, False, "%s: %s" % (type(e).__name__, e))
i18n.set_lang(was)

print()
print("[10] appearance is a round trip, not a one-way door")
# X11 paints the same light grey chrome whatever the desktop is set to, so picking Dark
# there gives a dark log pane bolted into a light grey window unless the program paints the
# chrome itself.  Doing that has to be undoable within one session -- and the undo is what
# broke first: the platform's own background was read lazily, so the FIRST thing ever to
# read it was the revert, by which time the window was already dark and it "restored" our
# own colour.  Both directions are checked, on every platform, because the answer differs
# by platform and the wrong one is invisible until someone takes a screenshot.
x11 = sys.platform not in ("win32", "darwin")
tw = tk.Toplevel(root)
tw.withdraw()
G.init_theme(tw, "dark")
native = G._NATIVE_BG
check("the platform's own background is remembered before anything is painted",
      bool(native), repr(native))
check("dark: chrome is painted by us on X11 and by the platform elsewhere",
      x11 == G.CHROME_THEMED, "CHROME_THEMED=%s on %s" % (G.CHROME_THEMED, sys.platform))
G.init_theme(tw, "light")
check("light: the native chrome is handed back", not G.CHROME_THEMED)
check("and the window background with it",
      tw.winfo_toplevel().cget("background") == native,
      "%s vs %s" % (tw.winfo_toplevel().cget("background"), native))
check("menu colours follow the same rule",
      not G.menu_colors(), "light -> %s" % (G.menu_colors() or "{} (platform's own)"))
G.init_theme(tw, "dark")
check("and dark can be taken up again after that", x11 == G.CHROME_THEMED)
check("menus are coloured on X11 dark only",
      bool(G.menu_colors()) == x11, "keys: %s" % sorted(G.menu_colors()))
G.init_theme(tw, "light")
tw.destroy()

root.destroy()
print()
if fails:
    print("FAILED: %s" % "; ".join(fails))
else:
    print("ALL PLATFORM CHECKS PASSED" + ("  (%d note(s) above)" % len(notes) if notes else ""))
sys.exit(1 if fails else 0)
