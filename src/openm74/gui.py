#!/usr/bin/env python3
"""Minimal GUI for openm74: read and write the M74 CAN ECU flash.

Deliberately Tkinter and nothing else.  This is shipped to people who are right to be
suspicious of downloaded chip-tuning binaries, so the dependency surface stays at
"Python's own standard library plus pyserial" -- small enough that anyone can read it,
and small enough that building your own copy is a one-liner instead of a project.

The GUI does not reimplement anything: it builds the same command line the CLI takes and
runs klinebsl's own main(), capturing its output.  So the button and the terminal do
exactly the same thing, and the tested core stays untouched.
"""
import io, json, os, queue, sys, threading, time

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

from . import klinebsl as K
from . import i18n
from .i18n import t

# Resolved once a root exists.  Hard-coding "Segoe UI"/"Consolas" would look wrong and
# measure wrong off Windows -- and the connector diagram is laid out in pixels, so a
# substituted font with different metrics can push text out of its cells.  Tk's named
# fonts already point at whatever each platform considers its UI and fixed-width face.
UI_FAMILY = "TkDefaultFont"
MONO_FAMILY = "TkFixedFont"


# On X11 the named fonts point at the ancient X core aliases, which is not a cosmetic
# problem: rendered in a container they gave spaced-out bitmap Cyrillic, and an em dash or an
# ellipsis -- both of which this interface uses -- came out as replacement blobs, so English
# buttons read "Save to file®f".  They also measure very wide, which pushed the measured
# window out to 1300 px.  So on X11 we ASK for a real family and only fall back to the named
# font if none is installed.  macOS and Windows already point their named fonts at the right
# place and are left alone.
X11_UI_FAMILIES = ("DejaVu Sans", "Noto Sans", "Liberation Sans", "FreeSans", "Arial")
X11_MONO_FAMILIES = ("DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono",
                     "FreeMono", "Courier New")


def init_fonts():
    global UI_FAMILY, MONO_FAMILY
    try:
        UI_FAMILY = tkfont.nametofont("TkDefaultFont").cget("family")
        MONO_FAMILY = tkfont.nametofont("TkFixedFont").cget("family")
    except Exception:
        UI_FAMILY, MONO_FAMILY = "Helvetica", "Courier"
    if sys.platform in ("win32", "darwin"):
        return
    try:
        have = set(tkfont.families())
    except Exception:
        return
    for want, families in ((0, X11_UI_FAMILIES), (1, X11_MONO_FAMILIES)):
        for fam in families:
            if fam in have:
                if want == 0:
                    UI_FAMILY = fam
                else:
                    MONO_FAMILY = fam
                break


WHEEL_STEP_PX = 40                  # one wheel notch, in pixels of view movement


def bind_scroll(widget, scroll):
    """Wire every way this platform reports a scroll gesture to `scroll(pixels)`.

    Three mechanisms, and the one a Mac trackpad uses is the one easiest to forget:

      * ``<MouseWheel>``     Windows and macOS wheels.  The delta magnitudes differ wildly
                             between the two, so only the sign is trusted.
      * ``<Button-4/5>``     X11 wheels, which carry no delta at all -- the button number
                             *is* the direction.
      * ``<TouchpadScroll>`` Tk 8.7+/9 on macOS and Windows, and the ONLY event a
                             two-finger trackpad gesture produces.

    That last one is why the log scrolled by trackpad and these panels did not: Tk's own
    Text class binds <TouchpadScroll>, but the Canvas class binds no scroll events
    whatsoever, so a canvas gets precisely what we bind to it and nothing more.  Binding
    only <MouseWheel> therefore works perfectly with a mouse and is completely dead under
    the fingers of anyone on a laptop.
    """
    def on_wheel(e):
        scroll(-WHEEL_STEP_PX if e.delta > 0 else WHEEL_STEP_PX)
        return "break"

    def on_button(e):
        scroll(-WHEEL_STEP_PX if e.num == 4 else WHEEL_STEP_PX)
        return "break"

    def on_touchpad(e):
        # %D packs both axes into one integer; Tk ships the unpacker because the packing is
        # a platform detail.  Sign convention copied from Tk's own Text binding, which
        # scrolls by -deltaY, so a gesture goes the way the content should go.
        try:
            _, dy = widget.tk.call("tk::PreciseScrollDeltas", e.delta)   # horizontal unused
            dy = int(dy)
        except Exception:
            dy = -1 if e.delta < 0 else 1
        if dy:
            scroll(-dy)
        return "break"

    def wire(w):
        w.bind("<MouseWheel>", on_wheel)
        w.bind("<Button-4>", on_button)
        w.bind("<Button-5>", on_button)
        try:
            w.bind("<TouchpadScroll>", on_touchpad)
        except tk.TclError:
            pass                        # Tk 8.6 and older do not know the event at all
        for child in w.winfo_children():
            wire(child)

    wire(widget)


def _channel(v):
    """One sRGB channel, linearised the way the contrast standard defines it."""
    v = v / 65535.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(widget, colour):
    """Relative luminance of a Tk colour -- a name, a system colour, or #rrggbb."""
    r, g, b = widget.winfo_rgb(colour)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(widget, a, b):
    """Contrast ratio between two Tk colours, 1.0 (identical) to 21.0 (black on white).

    Gamma-corrected on purpose: a naive average makes mid greys look far worse than they
    are and buries the real failures in false alarms.  4.5 is the usual floor for body
    text, 3.0 for large text."""
    la, lb = luminance(widget, a) + 0.05, luminance(widget, b) + 0.05
    return max(la, lb) / min(la, lb)


_NATIVE_BG = None


def native_bg(widget):
    """The background the PLATFORM chose, captured before we ever repaint anything.

    Read once and remembered, and that is not an optimisation.  On X11 this program paints
    the chrome itself when a dark palette is asked for (style_chrome below), so asking the
    toplevel afterwards returns our own colour -- and detection built on that answers "the
    desktop is dark" because we made it dark.  Switching back to Light would then find a
    dark window and stay dark, which is a setting that cannot be undone."""
    global _NATIVE_BG
    if _NATIVE_BG is None:
        try:
            _NATIVE_BG = widget.winfo_toplevel().cget("background")
        except Exception:
            return None
    return _NATIVE_BG


def dark_theme(widget):
    """Is the system appearance dark?  Asked of Tk, not of the OS.

    The point is not what macOS thinks -- it is what colour Tk is actually going to paint
    a default widget, which is the thing our own hard-coded colours have to survive next to.
    """
    try:
        return luminance(widget, native_bg(widget)) < 0.18
    except Exception:
        return False


# Tk reports Control as state bit 2 on every platform.  On macOS the key a person will
# actually press to copy is Command, which arrives as Mod1 -- bit 3 -- instead, so a
# handler that only looks for Control makes the log unselectable by keyboard on the one
# platform where Ctrl+C is not the shortcut.  Verified by generating the event rather
# than trusting the documentation: see tools/check_gui_platform.py.
COPY_MODS = 0x4 | (0x8 if sys.platform == "darwin" else 0)


def app_dir():
    """Where the application itself lives, so its files can be kept beside it.

    Three platform facts shape this, and getting any of them wrong puts a user's backup
    somewhere they will never find it:

      * **A macOS .app is a signed directory.** Writing inside the bundle invalidates the
        signature this project applies -- and an invalid signature on Apple Silicon is not a
        warning, it will not launch.  So the answer is the folder CONTAINING the .app, never
        `Contents/MacOS` where the executable actually sits.
      * **macOS may run the app from a copy.** A quarantined bundle opened from Finder can be
        "app translocated": executed from a randomised read-only path that vanishes
        afterwards.  Files written next to it would disappear with it, so that case is
        detected and refused rather than silently honoured.
      * **Not frozen means not installed.** Running from source, `sys.executable` is the
        Python interpreter, which is nobody's idea of "next to the program".  The working
        directory is what a person means then.
    """
    if not getattr(sys, "frozen", False):
        return os.getcwd()
    exe = os.path.abspath(sys.executable)
    if "/AppTranslocation/" in exe:
        return None                      # a disposable copy; anything written here is lost
    i = exe.find(".app" + os.sep + "Contents" + os.sep + "MacOS" + os.sep)
    if i >= 0:
        return os.path.dirname(exe[:i + 4])          # the folder holding the .app
    return os.path.dirname(exe)


def data_dir():
    """Where logs, backups and settings go: beside the application when that is possible.

    Falls back rather than failing, because "beside the application" is not always writable:
    Program Files and /Applications are not, and a translocated bundle is not even permanent.
    The GUI prints the resolved path on startup precisely so this is never a guess."""
    env = os.environ.get("OPENM74_DIR")
    if env:
        return writable_dir(os.path.expanduser(env))
    return writable_dir(app_dir(), os.getcwd())


def writable_dir(*candidates):
    """The first of `candidates` we can actually create a file in.

    A bundled GUI launched from a desktop -- Finder, a dock icon -- starts with its
    working directory at the filesystem ROOT, not next to the application.  Anything
    built from getcwd() therefore fails: the log silently goes nowhere and, worse, the
    pre-flight backup cannot be written, so a write is refused outright.  Running the
    same code from a terminal never shows this, which is exactly why it survived until
    there was a bundle to launch.

    Falls back to the home directory, which is writable on every platform this runs on."""
    home = os.path.expanduser("~")
    for d in list(candidates) + [home]:
        if not d:
            continue
        probe = os.path.join(d, ".openm74_write_test")
        try:
            with io.open(probe, "w", encoding="utf-8") as f:
                f.write(u"")
            os.remove(probe)
            return d
        except Exception:
            continue
    return home


# Shown in the picker in their OWN language, never translated.  Someone hunting for their
# language in a window they cannot read is looking for the word they recognise.
LANG_NAMES = {"ru": u"Русский", "en": u"English"}
CONFIG_NAME = "openm74.json"


def load_config():
    """Remembered preferences.  Absent, unreadable or corrupt all mean the same: defaults."""
    try:
        with io.open(os.path.join(data_dir(), CONFIG_NAME), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(**kw):
    """Merge and write.  A preference that cannot be stored must never stop the tool."""
    cfg = load_config()
    cfg.update(kw)
    try:
        with io.open(os.path.join(data_dir(), CONFIG_NAME), "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        pass

# The streaming read runs at the handshake rate; 19200 measured clean here and the ROM's
# own speed detection starts missing above it.
READ_BAUD = 19200

# ---------------------------------------------------------------------------
# PALETTE
#
# The diagram is drawn, not written out as monospace text: a text table reads as a console
# window inside a desktop app, and this is the one screen someone will have open with a probe
# in their hand.  Canvas keeps it dependency-free -- no image files to ship -- and it stays
# sharp at any scaling.  Colour carries the meaning, so the meaning has to survive the theme.
#
# TWO COMPLETE PALETTES, not one palette plus adjustments.  The first attempt pinned the
# drawing to a white "printed sheet" in both appearances, and it was wrong twice over: a white
# card in a dark window is a glaring hole with a visible seam where it meets the pane below
# it, and the ONLY reason it was pinned white in the first place is that #2c3e50 for ground
# would otherwise be invisible.  That is not a reason to keep a white card -- it is the
# requirement to give every semantic colour a second value.  So each one has a light variant
# chosen against a light surface and a dark variant chosen against a dark one, and the
# identities are preserved across both: power is red, ground is the neutral one, the
# apply-BEFORE-power instruction is amber, signal is blue, and CAN is deliberately muted
# because this tool never speaks it.
#
# Dark mode is NOT black.  The system's own text background is near-#1e1e1e, which reads as a
# hole rather than a surface; these panes sit a few steps up from it, slightly blue, the way
# an elevated surface should.  And the diagram shares the pane's exact background, which is
# what removes the seam: there is no card any more, just a drawing on the page.
#
# Every value below is checked by tools/check_gui_platform.py -- for BOTH themes at once,
# regardless of which one the machine running the check happens to be in, because these are
# explicit colours and contrast is arithmetic.  Nothing here is "looks about right".
THEMES = {
    "light": dict(
        surface="#ffffff", surface_alt="#f4f7f9", surface_head="#eaeef2",
        ink="#1b1f24", ink_soft="#525c65", ink_faint="#78828b",
        rule="#e3e8ec", edge="#c6ced7", pin_off="#262b31", on_chip="#ffffff",
        warn="#a92e21",
        # The amber is a two-sided constraint and the middle is the only answer: it labels
        # text ON the sheet (wants to be dark) and it fills a chip with white text on it
        # (wants to be light).  Chasing contrast alone drove it to #8f5300, which passed every
        # floor and came out brown -- indistinguishable at a glance from the power red beside
        # it, in the one drawing where telling those two apart is the whole point.  This value
        # gives 4.4:1 in BOTH directions and is still recognisably amber.
        pwr="#b32a1e", gnd="#2c3e50", ena="#a86a00", sig="#1a5f9c", mut="#666e77",
    ),
    "dark": dict(
        surface="#252a31", surface_alt="#2b3138", surface_head="#323944",
        ink="#e8eaed", ink_soft="#a9b2bb", ink_faint="#828b95",
        rule="#343b45", edge="#4a5360", pin_off="#3f4854", on_chip="#191c21",
        warn="#ff9a90",
        # Attention is driven by saturation and filled area, NOT by contrast, which is why
        # the same token needs opposite treatment in the two themes.  On a light sheet the
        # near-black navy for ground reads as structural and stays out of the way even though
        # it has the HIGHEST contrast of any colour here.  On a dark surface the first attempt
        # gave ground a near-white value: every contrast check passed, and the drawing was
        # still wrong -- three big pale chips became the loudest thing on screen and pulled
        # the eye off the power pins.  Contrast is a floor; the ranking has to be chosen.
        # Wanted, loudest first: enable (the order that decides whether the ECU answers at
        # all), then power and signal, then ground, and CAN last because this tool never
        # speaks it.  Ground stays blue-tinted so it reads apart from CAN's neutral grey.
        pwr="#ff7063", gnd="#8595a8", ena="#f0a83c", sig="#5eabf0", mut="#7a838d",
    ),
}
# Resolved once a root exists -- see init_theme().  Light is the safe default: if detection
# ever fails, a light palette on an unexpectedly dark window is merely ugly, while a dark
# palette on a light window is unreadable.
THEME = THEMES["light"]
THEME_NAME = "light"


def init_theme(widget, prefer=None):
    """Pick the palette that matches what Tk is actually going to paint around us.

    Three sources, in order.  OPENM74_THEME wins because it is a debugging override and has
    to work regardless of what is stored; then the user's own choice, because someone who
    picked has said something more specific than their desktop did; then detection.

    The override exists at all because the alternative way to see the other palette is to
    change the whole machine's appearance and restart, which is slow enough that in practice
    nobody checks the theme they are not sitting in -- and a palette nobody looks at is how a
    window full of invisible text ships."""
    global THEME, THEME_NAME
    # Remember the platform's own background FIRST, unconditionally.  Doing it lazily inside
    # dark_theme() is not enough: an explicit preference short-circuits detection, so the
    # very first thing that ever read it could be the revert path -- by which time the
    # window is already painted dark and "restore what the platform had" restores our own
    # colour.  Measured on Linux: starting in Dark and switching to Light left the frame
    # dark around a light window.
    native_bg(widget)
    want = (os.environ.get("OPENM74_THEME") or prefer or "auto").strip().lower()
    THEME_NAME = want if want in THEMES else ("dark" if dark_theme(widget) else "light")
    THEME = THEMES[THEME_NAME]
    style_chrome(widget)
    return THEME_NAME


CHROME_THEMED = False       # did style_chrome actually repaint the chrome this time?
_NATIVE_TTK = None          # the ttk theme the platform started with


def style_chrome(widget):
    """Paint the ttk chrome ourselves -- on X11 only, and only when the palettes disagree.

    Everywhere else this is deliberately not done: macOS and Windows paint their widgets to
    match the system appearance, natively and better than we would, and a hand-coloured
    button on those platforms looks like a port of somebody else's toolkit.

    X11 is the one that cannot be left alone.  Its default ttk theme paints the same light
    grey whatever the desktop is set to, so a user who picks Dark gets a dark log pane
    bolted into a light grey window -- which is not "a theme we did not apply", it is a
    window that looks broken, and it is what a Linux screenshot of this program showed.
    The fix is 'clam', the one built-in ttk theme that honours the colours it is given.

    Only for dark.  With the light palette the native chrome already agrees, and swapping
    the theme engine would trade a correct native look for an approximation of it -- so this
    also has to be able to switch BACK, because the theme can be changed inside one session
    and clam holding dark colours under the light palette is worse than what it fixed."""
    global CHROME_THEMED, _NATIVE_TTK
    CHROME_THEMED = False
    if sys.platform in ("win32", "darwin"):
        return
    T = THEME
    try:
        st = ttk.Style(widget)
        if _NATIVE_TTK is None:
            _NATIVE_TTK = st.theme_use()        # remembered before we ever change it
        # The dropdown of a combobox is a plain Tk listbox behind the ttk widget, reachable
        # only through the option database -- and the option database is not undone by
        # changing the ttk theme, so it is set for BOTH palettes rather than only for dark.
        for opt, key in (("*TCombobox*Listbox.background", "surface"),
                         ("*TCombobox*Listbox.foreground", "ink"),
                         ("*TCombobox*Listbox.selectBackground", "sig"),
                         ("*TCombobox*Listbox.selectForeground", "on_chip")):
            widget.option_add(opt, T[key])
        if THEME_NAME != "dark" or "clam" not in st.theme_names():
            st.theme_use(_NATIVE_TTK)
            if native_bg(widget):
                widget.winfo_toplevel().configure(background=native_bg(widget))
            return
        st.theme_use("clam")
        # '.' is the root style every other one inherits from, so the bulk of the window is
        # covered by this alone; the rest are the widgets whose parts are named separately.
        st.configure(".", background=T["surface_alt"], foreground=T["ink"],
                     fieldbackground=T["surface"], troughcolor=T["surface_head"],
                     bordercolor=T["edge"], lightcolor=T["surface_head"],
                     darkcolor=T["surface_head"], focuscolor=T["sig"],
                     selectbackground=T["sig"], selectforeground=T["on_chip"],
                     insertcolor=T["ink"])
        st.configure("TButton", background=T["surface_head"], padding=4)
        st.map("TButton",
               background=[("pressed", T["edge"]), ("active", T["rule"]),
                           ("disabled", T["surface_alt"])],
               foreground=[("disabled", T["ink_faint"])])
        st.configure("TNotebook.Tab", background=T["surface_alt"], padding=(10, 4))
        st.map("TNotebook.Tab", background=[("selected", T["surface"])],
               foreground=[("selected", T["ink"])])
        # A read-only combobox draws its text through the LIST colours, not the field ones,
        # so leaving these alone gives dark text on a dark field -- invisible, and invisible
        # in the one control that says which adapter is about to be talked to.
        st.map("TCombobox", fieldbackground=[("readonly", T["surface"])],
               foreground=[("readonly", T["ink"])],
               selectbackground=[("readonly", T["surface"])],
               selectforeground=[("readonly", T["ink"])])
        # Radio and check indicators.  clam fills them from -indicatorbackground, whose
        # default is a pale grey it keeps in both states, so an UNSELECTED control reads as
        # a filled light disc and a selected one as a ring -- backwards, on the control that
        # picks between a verified write and a quick one.  Its own try: these option names
        # belong to clam's element definitions, and a Tk that spells them differently must
        # cost the indicators, not everything configured above them.
        try:
            for w in ("TCheckbutton", "TRadiobutton"):
                st.configure(w, indicatorbackground=T["surface"],
                             indicatorforeground=T["on_chip"])
                st.map(w, indicatorbackground=[("selected", T["sig"]),
                                               ("disabled", T["surface_alt"])])
        except tk.TclError:
            pass
        widget.winfo_toplevel().configure(background=T["surface_alt"])
        CHROME_THEMED = True
    except tk.TclError:
        pass                            # a themed window is not worth failing to open over


def menu_colors():
    """Colours for tk.Menu, which is not a ttk widget and so misses style_chrome entirely.

    Empty on the platforms that paint their own menus -- and that is not only a matter of
    taste on macOS: the menu bar there belongs to the system, and colouring it would make
    this one application's menus disagree with every other application's."""
    if sys.platform in ("win32", "darwin") or THEME_NAME != "dark":
        return {}
    T = THEME
    return dict(background=T["surface_alt"], foreground=T["ink"],
                activebackground=T["sig"], activeforeground=T["on_chip"],
                selectcolor=T["ink"], borderwidth=1)

# Window width, and the column boundaries of the pin table -- module level so that
# tools/check_gui_platform.py measures the geometry the drawing actually uses instead of
# keeping its own copy of these numbers and going stale the first time one of them moves.
#
# The pin column is the one under pressure: "X2 : F2   и   X1 : J1" measures 189 px in
# macOS's UI face, against the 190 px the boundary at 500 used to leave it.  One pixel of
# margin is not a layout, it is a coincidence, and it was found by measuring rather than by
# looking.  The note column has slack to spare (173 px used of 320), so the boundary moves
# instead of the text -- and Windows, where these strings are narrower, only gains room.
# Minimums, not the layout.  The layout is MEASURED -- see layout() below.
#
# These were fixed numbers, tuned by hand against macOS's UI face, and a run in a Linux
# container found what that is worth: the same drawing overflowed every column and every help
# tab, because the default font there is wider.  Hand-tuning against one machine's fonts
# produces a layout that is correct on one machine's fonts.  Two languages made it worse
# again -- "Разрешение программирования" and "Programming enable" are not the same width.
#
# So the boundaries now come from measuring the strings that have to fit inside them, in the
# language actually selected, with the font this platform actually resolved.  The checks then
# verify the RESULT is sane rather than that a constant happened to be big enough.
WIN_W_MIN = 880
LEGEND_LABEL_DX = 26
# Semantic NAMES, not colour values: these tables are built at import time, long before a
# Tk root exists to ask which appearance we are in, so baking values in here is what forced
# a single fixed palette in the first place.  The drawing resolves them through THEME.
PINOUT = (
    # (colour token, circuit key, pin coordinates, note key).  The coordinates are the one
    # column that stays literal: they are identifiers, not prose, and "X2 : H1, H2" reads the
    # same in every language.  Translating them would only introduce a way to get them wrong.
    ("pwr", "pin.pwr_k30", u"X2 : H1, H2", "note.main_power"),
    # J1 removed on purpose, and the note says so.  Written sources list ignition as
    # "F2 AND J1, both", the connector drawing below has always omitted J1 because the bench
    # this was proven on has it unconnected -- and this row went on demanding it anyway, in
    # the same file.  A table that sends someone hunting a pin the diagram does not show, for
    # a wire the documentation says is unnecessary, is worse than no table.
    ("pwr", "pin.pwr_k15", u"X2 : F2", "note.j1"),
    ("gnd", "pin.gnd", u"X2 : G2, G3, G4", "note.not_g1"),
    ("ena", "pin.ena", u"X1 : A4   и   X1 : B2", "note.before_power"),
    ("sig", "pin.sig", u"X1 : G3", "note.obd7"),
    ("mut", "pin.can", u"X1 : E3 / E2", "note.can_unused"),
)


# Connector geometry, taken from the factory pinout drawing rather than guessed: the two
# housings are mirror images of each other, which is exactly the trap this diagram exists
# to prevent -- the same coordinate means a different hole depending on the connector.
# X1: 12 columns lettered M..A left to right (no I), rows 4..1 top to bottom.
# X2:  8 columns lettered A..H left to right,        rows 1..4 top to bottom.
X1_COLS = ("M", "L", "K", "J", "H", "G", "F", "E", "D", "C", "B", "A")
X1_ROWS = (4, 3, 2, 1)
X2_COLS = ("A", "B", "C", "D", "E", "F", "G", "H")
X2_ROWS = (1, 2, 3, 4)

# X1:J1 is deliberately absent.  Written sources list ignition as "F2 AND J1, both",
# but the bench this tool was built and proven on has J1 unconnected and works -- so the
# extra wire is not needed and showing it would send people chasing a pin for nothing.
HIGHLIGHT = {
    ("X2", "H", 1): "pwr", ("X2", "H", 2): "pwr",      # +12 K30
    ("X2", "F", 2): "pwr",                             # +12 K15
    ("X2", "G", 2): "gnd", ("X2", "G", 3): "gnd", ("X2", "G", 4): "gnd",
    ("X1", "A", 4): "ena", ("X1", "B", 2): "ena",      # program enable, BEFORE power
    ("X1", "G", 3): "sig",                             # K-line
}
# CAN is deliberately absent: this program never speaks CAN, so showing those pins would
# only invite people to wire something that plays no part in flashing.
LEGEND = (
    ("pwr", "pin.pwr_k30", "lg.pwr_k30"),
    ("pwr", "pin.pwr_k15", "lg.pwr_k15"),
    ("gnd", "pin.gnd_short", "lg.not_g1"),
    ("ena", "pin.ena", "lg.ena"),
    ("sig", "pin.sig_long", "lg.sig"),
)


_FONTS = {}


def font(family, size, weight="normal"):
    """A cached measuring font.  Tk named fonts are never freed, and layout() runs on every
    drawing, every help tab and every rebuild -- minting fresh ones each time is a slow leak
    for no benefit, since the metrics cannot change without a restart."""
    key = (family, size, weight)
    if key not in _FONTS:
        _FONTS[key] = tkfont.Font(family=family, size=size, weight=weight)
    return _FONTS[key]


def layout():
    """Column boundaries and widths, measured from the text that must fit in them.

    Returns a dict so the two drawings and the window sizing all read the same numbers, in
    the current language, with the fonts this platform actually resolved."""
    ui10 = font(UI_FAMILY, 10)
    ui9 = font(UI_FAMILY, 9)
    mono10 = font(MONO_FAMILY, 10)
    mono11b = font(MONO_FAMILY, 11, "bold")
    mono9b = font(MONO_FAMILY, 9, "bold")

    GAP = 24                                   # breathing room between columns
    x0 = 14
    x1 = x0 + 16 + max(ui10.measure(t(k)) for _, k, _, _ in PINOUT) + GAP
    x2 = x1 + 10 + max(mono11b.measure(p) for _, _, p, _ in PINOUT) + GAP
    x3 = x2 + 10 + max(ui9.measure(t(k)) for _, _, _, k in PINOUT) + 14

    legend_pins_dx = LEGEND_LABEL_DX + max(ui9.measure(t(k)) for _, k, _ in LEGEND) + GAP
    legend_w = legend_pins_dx + max(mono9b.measure(t(k)) for _, _, k in LEGEND) + 24

    # The window has to hold the widest of: the pin table, the connector legend, the warning
    # lines, and the help text -- which is monospace and sized in characters.
    widest_help = 0
    for _title, body in i18n.help_tabs(t("app.device")):
        for line in body.strip("\n").splitlines():
            widest_help = max(widest_help, mono10.measure(line))
    warn = max(ui9.measure(t("dg.connectors_warn")), ui9.measure(t("dg.pinout_warn")))
    need = max(x3, legend_w, 14 + warn, widest_help + 28) + 40      # +40 for the scrollbar
    return {"cols": (x0, x1, x2, x3), "legend_pins_dx": legend_pins_dx,
            "width": max(WIN_W_MIN, need)}


def draw_connectors(parent):
    """Front view of both connectors, as you see them on the ECU.

    Drawn from the coordinates in the factory diagram.  Worth the effort over a plain
    table because the two housings mirror each other: 'G3' is a different hole on X1 than
    on X2, and someone counting holes by eye on the wrong one puts +12 into a signal pin.
    """
    pw, ph, gap = 22, 13, 4                    # pin body and spacing
    # `top` is where the pin grid starts; the block name sits 30 above it and its subtitle
    # 14 above.  At 78 the name landed 10px under the mirrored-connectors warning, so the
    # heading read as part of the warning instead of as a label for the drawing.  There is
    # plenty of room here -- everything below shifts with it, since the legend and the canvas
    # height are both derived from this one number.
    top, x1x, x2x = 102, 24, 470
    rowsh = len(X1_ROWS) * (ph + gap)
    legend_top = top + rowsh + 52
    h = legend_top + len(LEGEND) * 22 + 14
    T, L = THEME, layout()
    c = tk.Canvas(parent, height=h, highlightthickness=0, bg=T["surface"])

    c.create_text(14, 16, anchor="w", fill=T["ink"], font=(UI_FAMILY, 11, "bold"),
                  text=t("dg.connectors_title"))
    c.create_text(14, 38, anchor="w", fill=T["warn"], font=(UI_FAMILY, 9),
                  text=t("dg.connectors_warn"))

    def block(ox, name, cols, rows, subtitle):
        w = len(cols) * (pw + gap) + gap
        hh = rowsh + gap
        c.create_text(ox + w / 2, top - 30, text=name, fill=T["ink"],
                      font=(UI_FAMILY, 10, "bold"))
        c.create_text(ox + w / 2, top - 14, text=subtitle, fill=T["ink_faint"],
                      font=(UI_FAMILY, 8))
        c.create_rectangle(ox - 6, top - 6, ox + w + 6, top + hh + 6,
                           outline=T["edge"], width=2)
        for ci, col in enumerate(cols):
            cx = ox + gap + ci * (pw + gap) + pw / 2
            for ri, row in enumerate(rows):
                y = top + gap + ri * (ph + gap)
                colour = T.get(HIGHLIGHT.get((name, col, row)) or "", None)
                c.create_rectangle(cx - pw / 2, y, cx + pw / 2, y + ph,
                                   fill=colour or T["pin_off"],
                                   outline=colour or T["pin_off"])
                if colour:                      # name the pins that matter
                    c.create_text(cx, y + ph / 2, text="%s%d" % (col, row),
                                  fill=T["on_chip"], font=(UI_FAMILY, 7, "bold"))
                if ci == 0:                     # row numbers down the left
                    c.create_text(ox - 16, y + ph / 2, text=str(row),
                                  fill=T["ink_soft"], font=(UI_FAMILY, 8))
            c.create_text(cx, top - 2, text=col, fill=T["ink_soft"], font=(UI_FAMILY, 8))
            c.create_text(cx, top + hh + 2, text=col, fill=T["ink_soft"],
                          font=(UI_FAMILY, 8))
        return w

    block(x1x, "X1", X1_COLS, X1_ROWS, t("dg.x1_sub"))
    block(x2x, "X2", X2_COLS, X2_ROWS, t("dg.x2_sub"))
    # Inside a scrolling container nothing stretches this for us, and a canvas defaults
    # to a width far narrower than the drawing -- which silently cut the right-hand
    # connector off.  Size it to what was actually drawn.
    c.update_idletasks()
    c.configure(width=max(x[2] for x in (c.bbox(i) for i in c.find_all())) + 16)

    for i, (name, what, pins) in enumerate(LEGEND):
        colour = T[name]
        y = legend_top + i * 22
        c.create_rectangle(x1x, y + 4, x1x + 16, y + 15, fill=colour, outline=colour)
        c.create_text(x1x + LEGEND_LABEL_DX, y + 10, anchor="w", text=t(what),
                      fill=T["ink"], font=(UI_FAMILY, 9))
        c.create_text(x1x + L["legend_pins_dx"], y + 10, anchor="w", text=t(pins),
                      fill=colour,
                      font=(MONO_FAMILY, 9, "bold"))
    return c


def draw_pinout(parent):
    """Draw the pin table.  Returns the canvas so the caller can pack it."""
    rows, rh, top = len(PINOUT), 34, 78        # same breathing room under the warning line
    # +1 for the header row: leaving it out clipped the table and the text below it
    # rode up over the last lines
    h = top + (rows + 1) * rh + 14
    T, L = THEME, layout()
    c = tk.Canvas(parent, height=h, highlightthickness=0, bg=T["surface"])

    c.create_text(14, 16, anchor="w", text=t("dg.pinout_title"), fill=T["ink"],
                  font=(UI_FAMILY, 11, "bold"))
    c.create_text(14, 38, anchor="w", fill=T["warn"], font=(UI_FAMILY, 9),
                  text=t("dg.pinout_warn"))

    x0, x1, x2, x3 = L["cols"]
    c.create_rectangle(x0, top, x3, top + rh, fill=T["surface_head"], outline=T["edge"])
    for x, key in ((x0 + 10, "dg.col_circuit"), (x1 + 10, "dg.col_pins"),
                   (x2 + 10, "dg.col_note")):
        c.create_text(x, top + rh / 2, anchor="w", text=t(key), fill=T["ink"],
                      font=(UI_FAMILY, 9, "bold"))

    for i, (name, what, pins, note) in enumerate(PINOUT):
        colour = T[name]
        y = top + (i + 1) * rh
        if i % 2:
            c.create_rectangle(x0, y, x3, y + rh, fill=T["surface_alt"], outline="")
        c.create_rectangle(x0, y + 6, x0 + 5, y + rh - 6, fill=colour, outline="")
        c.create_text(x0 + 16, y + rh / 2, anchor="w", text=t(what), fill=T["ink"],
                      font=(UI_FAMILY, 10))
        c.create_text(x1 + 10, y + rh / 2, anchor="w", text=pins, fill=colour,
                      font=(MONO_FAMILY, 11, "bold"))
        c.create_text(x2 + 10, y + rh / 2, anchor="w", text=t(note), fill=T["ink_soft"],
                      font=(UI_FAMILY, 9))
        c.create_line(x0, y + rh, x3, y + rh, fill=T["rule"])
    c.create_rectangle(x0, top, x3, top + (rows + 1) * rh, outline=T["edge"])
    c.configure(width=x3 + 14)
    return c


# Raw exception text is useless to the person holding the cable.  Every failure we have
# actually hit on the bench is translated into the action that fixes it.
# Raw failure text from the engine -> the action that fixes it.  The needle stays English
# because it matches the engine's own log, which is English by design; only the advice is
# translated, because the advice is the part a person acts on.
WHY = (
    (u"Write timeout", "why.write_timeout"),
    # Ahead of the generic "could not open port", which pyserial also puts in this message:
    # first match wins, and on Linux a refused open is almost never a port held by another
    # program.  It is group membership, and "close the other program" sends someone hunting
    # for a program that does not exist.
    # Ahead of the permission and busy needles: a wedged adapter opens fine and then
    # refuses every setting, so the message mentions neither permission nor a busy port.
    (u"refuses every setting", "why.adapter_wedged"),
    (u"Permission denied", "why.no_permission"),
    (u"could not open port", "why.port_busy"),
    (u"Access is denied", "why.access_denied"),
    (u"no 0xD5", "why.no_ack"),
    (u"pre-flight backup FAILED", "why.backup_failed"),
    (u"VERIFY FAILED", "why.verify_failed"),
    (u"LINK LOST", "why.link_lost"),
)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.last_was_write = False
        self.results = []
        self._unmapped_regions = self._unmapped_bytes = 0
        self._baud = None           # last rate the engine reported; see on_close()
        self._evbuf = ""
        root.title(t("app.title"))
        self.set_window_icon(root)
        root.geometry("%dx660" % layout()["width"])
        root.minsize(760, 560)

        # Resolve the palette before building anything with it.  ttk's own chrome -- buttons,
        # tabs, the progress bar -- is left to the platform, which draws it correctly and
        # natively; what we theme is the CONTENT: the log, the help panes and the drawings.
        cfg = load_config()
        # Type-checked, not just present: a config holding {"theme": 5} used to raise inside
        # __init__, and in a windowed build that means the application simply never opens,
        # with nothing on screen to explain why.
        prefer = cfg.get("theme")
        init_theme(root, prefer if isinstance(prefer, str) else None)
        # Dimmed text sits on the CHROME, so its colour has to match whatever painted the
        # chrome -- which is not always the palette above.  Taking it from THEME makes it
        # dark-on-dark the moment the two disagree, which is exactly what forcing the light
        # palette on a dark desktop does: the override changes what WE paint, and on macOS
        # and Windows it cannot change what the platform paints.  On X11 it now can, so ask
        # style_chrome whether it did rather than assuming either way.
        self.soft = THEMES["dark" if (CHROME_THEMED or dark_theme(root))
                           else "light"]["ink_soft"]

        self.build_menu(root)

        # say plainly which ECU this is for -- the pin coordinates in the help are
        # worthless, and dangerous, if someone assumes they fit a different unit
        head = ttk.Frame(root, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text=t("app.device"), foreground=self.soft).pack(side="left")


        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text=t("ui.adapter")).grid(row=0, column=0, sticky="w")
        self.port = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port, width=44, state="readonly")
        self.port_box.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(top, text=t("ui.refresh"),
                   command=self.refresh_ports).grid(row=0, column=2)

        ttk.Label(top, text=t("ui.image_file")).grid(row=1, column=0, sticky="w",
                                                    pady=(8, 0))
        self.path = tk.StringVar()
        ttk.Entry(top, textvariable=self.path, width=47).grid(row=1, column=1, sticky="w",
                                                              padx=6, pady=(8, 0))
        ttk.Button(top, text=t("ui.choose"), command=self.pick).grid(row=1, column=2,
                                                                    pady=(8, 0))

        # The one setting worth exposing.  Everything else the tool measures for itself;
        # this is a genuine choice about how much time to trade for how much checking.
        mode = ttk.LabelFrame(root, text=t("ui.mode_frame"), padding=(10, 6))
        mode.pack(fill="x", padx=10, pady=(6, 0))
        self.mode = tk.StringVar(value="reliable")
        ttk.Radiobutton(mode, variable=self.mode, value="reliable",
                        text=t("ui.mode_reliable")).pack(anchor="w")
        ttk.Radiobutton(mode, variable=self.mode, value="fast",
                        text=t("ui.mode_fast")).pack(anchor="w")
        ttk.Label(mode, foreground=self.soft, font=(UI_FAMILY, 8),
                  text=t("ui.backup_note")).pack(anchor="w", pady=(4, 0))

        # Off by default because on a FIRST write it is pure cost: it reads every sector
        # before writing it, which is another pass over the whole image and buys nothing
        # when there is nothing to skip.  Turned on automatically the moment a write does
        # not finish -- that is when it is worth its price, and expecting the user to know
        # that in advance is how the promise in the help got made and not kept.
        # Remembered, because the moment it is armed is the moment the process is most
        # likely to end: it is set after a write that did NOT finish.  Losing it on a
        # language switch -- or on the next launch after a crash -- contradicts what the UI
        # just promised and costs a full re-erase of every sector that was already correct.
        self.resume = tk.BooleanVar(value=bool(cfg.get("resume")))
        ttk.Checkbutton(mode, variable=self.resume,
                        text=t("ui.resume_check")).pack(anchor="w", pady=(2, 0))

        btns = ttk.Frame(root, padding=(10, 0))
        btns.pack(fill="x")
        self.read_btn = ttk.Button(btns, text=t("ui.read_btn"), command=self.do_read)
        self.read_btn.pack(side="left", ipadx=10, ipady=6)
        self.write_btn = ttk.Button(btns, text=t("ui.write_btn"), command=self.do_write)
        self.write_btn.pack(side="left", padx=10, ipadx=10, ipady=6)
        # one door, since every button opened the same window anyway
        ttk.Button(btns, text=t("ui.help_btn"), command=self.help_window).pack(side="right")

        self.status = tk.StringVar(value=t("st.ready"))
        # wraplength under the minimum window width, not over it: at 840 against a
        # 760 minsize the verdict -- the single most important line here -- ran off
        # the edge of a window shrunk to its own minimum.
        ttk.Label(root, textvariable=self.status, padding=(10, 8),
                  wraplength=720, justify="left").pack(fill="x")
        self.bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=10)

        logbar = ttk.Frame(root, padding=(10, 0))
        logbar.pack(fill="x")
        ttk.Label(logbar, text=t("ui.log")).pack(side="left")
        ttk.Button(logbar, text=t("ui.copy"), command=self.copy_log).pack(side="right")
        ttk.Button(logbar, text=t("ui.save_as"),
                   command=self.save_log).pack(side="right", padx=6)
        ttk.Button(logbar, text=t("ui.clear"), command=self.clear_log).pack(side="right")

        # left editable on purpose: a disabled Text refuses to be selected, and the one
        # thing anyone asks for when something goes wrong is the log text
        # Themed rather than left to the system: the system's dark text background is
        # near-black, which reads as a hole punched in the window rather than a surface, and
        # it does not match the drawings.  One surface everywhere is what makes the app look
        # like one app.
        self.log = tk.Text(root, height=20, wrap="none", font=(MONO_FAMILY, 9),
                           undo=False, exportselection=True,
                           bg=THEME["surface"], fg=THEME["ink"],
                           insertbackground=THEME["ink"],
                           highlightthickness=1, highlightbackground=THEME["edge"],
                           highlightcolor=THEME["edge"], borderwidth=0)
        self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log.bind("<Key>", self._readonly_keys)
        self.log_menu = self.build_log_menu()

        # ...and written to a file as it goes, so a support question needs no copying at
        # all and a crash mid-write does not take the evidence with it
        # Held open, not reopened per line.  The worker thread shares the GIL with this
        # one, and this protocol is measurably sensitive to pauses -- opening and closing
        # a file for every one of the hundreds of retry messages a write produces is
        # exactly the kind of self-inflicted latency that makes the link look worse.
        self.outdir = data_dir()
        # The engine drops a few diagnostics relative to the working directory (a failed
        # one-shot read saves its raw capture there), and for a bundle launched from a
        # desktop that directory is the filesystem root.  Moving there once fixes the whole
        # class rather than each call site.
        try:
            os.chdir(self.outdir)
        except Exception:
            pass
        self.logfile = os.path.join(self.outdir, "openm74_log.txt")
        try:
            self.logfh = io.open(self.logfile, "a", encoding="utf-8")
        except Exception:
            self.logfh = None
        # Say where, always.  "Somewhere sensible" is not an answer when the file in
        # question may be the only copy of someone's firmware.  Once per session, though:
        # rebuild() re-runs __init__ to change language or appearance and carries the old
        # log across, so saying it unconditionally added a duplicate line on every switch.
        if not getattr(self, "_said_where", False):
            beside = app_dir()
            self.say(t("st.files_here" if beside and os.path.abspath(beside) ==
                       os.path.abspath(self.outdir) else "st.files_elsewhere", self.outdir))
            self._said_where = True

        # The one destructive click that was unguarded.  Everything else checks `busy` --
        # re-runs, language, appearance -- while the close button killed a daemon thread
        # mid-erase without a word.  A user whose window looks stuck reaches for it first.
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.refresh_ports()
        # Held so a rebuild can cancel it.  Re-running __init__ without this
        # leaves the previous loop alive and two drains race for the queue.
        self._drain_id = self.root.after(80, self.drain)

    @staticmethod
    def _readonly_keys(event):
        """Read-only without disabling: let copy and navigation through, block edits."""
        allowed = ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next")
        # A keysym follows the KEYBOARD LAYOUT: with a Russian layout active the C key
        # reports `cyrillic_es`, not `c`, so matching on the letter alone silently disables
        # copy for exactly the audience the Russian catalogue exists for.  The control
        # character in event.char is layout-independent, which is why it is checked too.
        # (tools/check_gui_platform.py cannot catch this: event_generate("<Control-c>")
        # forces the keysym to `c` no matter what layout is installed.)
        if event.state & COPY_MODS and (
                event.keysym.lower() in ("c", "a", "insert", "cyrillic_es", "cyrillic_ef")
                or event.char in ("\x03", "\x01")):
            return None                      # Ctrl+C / Ctrl+A, or Cmd+C / Cmd+A on macOS
        if event.keysym in allowed:
            return None
        return "break"

    def set_window_icon(self, root):
        """The icon the window manager and the taskbar show.

        Separate from the one baked into the executable: that one is what the FILE looks
        like, this one is what the running WINDOW looks like, and on Linux and Windows they
        come from different places.  Kept on self because Tk holds only a weak reference to
        an image -- a PhotoImage dropped on the floor here leaves the window iconless a
        moment later, which is a confusing bug to meet.

        Silent on failure by design: an application that will not open because it could not
        find a picture would be a poor trade.
        """
        try:
            png = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "icons", "openm74.png")
            if os.path.exists(png):
                self._icon = tk.PhotoImage(file=png)     # Tk 8.6+ reads PNG natively
                root.iconphoto(True, self._icon)
        except Exception:
            pass

    def toggle_force_ecu(self):
        """Turn the identity check off, once, after saying what it is for.

        Deliberately NOT remembered between runs.  Everything else this window remembers --
        language, appearance, resume -- is a preference; this is a suspended safety check,
        and a suspended safety check that quietly survives a restart is one nobody
        reconsiders.  It lasts until the program is closed, and the log says so."""
        if not self.force_ecu.get():
            self.say(t("st.force_off"))
            return
        if not messagebox.askyesno(t("dlg.force_title"), t("dlg.force_body"),
                                   icon="warning", default="no"):
            self.force_ecu.set(False)
            return
        self.say(t("st.force_on"))

    def build_log_menu(self):
        """Right-click on the log: copy, select all, save, clear.

        Every one of these is already a button under the log, so this adds no capability --
        it adds the gesture people reach for first.  Right-clicking a text pane and getting
        nothing reads as a dead pane, and the one thing a person is most likely to want from
        this window is the text in it, to paste into a question about their ECU.

        Button-3 everywhere, plus Button-2 on macOS: a one-button trackpad sends the
        Control-click through as Button-2 there, so binding only Button-3 leaves the menu
        unreachable on the platform with the fewest mouse buttons.
        """
        m = tk.Menu(self.root, tearoff=0, **menu_colors())
        m.add_command(label=t("ui.copy"), command=self.copy_log)
        m.add_command(label=t("menu.select_all"), command=self.select_all_log)
        m.add_separator()
        m.add_command(label=t("ui.save_as"), command=self.save_log)
        m.add_command(label=t("ui.clear"), command=self.clear_log)
        for seq in ("<Button-3>", "<Button-2>") if sys.platform == "darwin" else ("<Button-3>",):
            self.log.bind(seq, self._popup_log_menu)
        return m

    def _popup_log_menu(self, event):
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            # Documented requirement of tk_popup on X11: without it the menu keeps the
            # pointer grabbed and the rest of the window stops responding to clicks.
            self.log_menu.grab_release()
        return "break"

    def select_all_log(self):
        self.log.tag_add("sel", "1.0", "end-1c")
        self.log.focus_set()

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))
        self.status.set(t("st.copied"))

    def save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt",
                                         initialfile="openm74_log.txt",
                                         initialdir=self.outdir,
                                         filetypes=[(t("ui.text_files"), "*.txt")])
        if p:
            with io.open(p, "w", encoding="utf-8") as f:
                f.write(self.log.get("1.0", "end-1c"))
            self.status.set(t("st.saved", p))

    def clear_log(self):
        self.log.delete("1.0", "end")

    # --- helpers ------------------------------------------------------------
    def on_close(self):
        """Confirm before stopping a write, then stop it at a sector boundary if we can.

        The worker is a daemon thread, so destroying the window would end the process and
        the thread with it -- mid-erase if that is where it happened to be, with the port
        dropped by the operating system rather than closed by us.  Asking the engine to
        stop instead means the write ends between sectors, where everything behind it is
        verified and nothing ahead has been touched, and the port is drained and closed on
        the way out.  Bounded: if the engine does not reach a boundary in time (a sector is
        about two seconds), we go anyway rather than refusing to close the window."""
        if self.busy and not messagebox.askokcancel(t("dl.quit_title"), t("dl.quit_body")):
            return
        if self.busy:
            K.request_stop()
            self.status.set(t("st.stopping"))
            self.root.update_idletasks()
            # How long to allow is NOT a constant.  A sector takes about two seconds at
            # 115200 and twelve at 19200, and the engine steps down that ladder by itself on
            # a poor link -- which is the link where someone is most likely to reach for the
            # close button.  A fixed eight seconds therefore expired mid-sector at exactly
            # the rates that need it most, killing the thread between an erase and its
            # programming: the state this whole mechanism exists to avoid.  Ask the engine
            # instead, from the rate it last reported, and fall back to the slowest rung.
            baud = self._baud or min(K.RUN_LADDER)
            deadline = time.time() + K.sector_seconds(baud) * 1.5 + 2.0
            while self.busy and time.time() < deadline:
                self.root.update()
                time.sleep(0.05)
        try:
            if self.logfh:
                self.logfh.close()
        except Exception:
            pass
        self.root.destroy()

    def build_menu(self, root):
        """A real menu bar: the system one on macOS, an in-window one elsewhere.

        Tk maps a menu attached to the root window onto whatever the platform's own menu bar
        is, which on macOS is the strip along the top of the screen.  That is where settings
        belong on every platform this ships to -- and it is why the language picker is no
        longer a combobox wedged into the header, where it competed for width with the line
        identifying the ECU and had to be read before it could be used.
        """
        # tearoff=0 on the BAR as well as on every menu under it.  Tk's default for tearOff
        # is 1 on X11 and 0 on Aqua and Windows, so leaving it off costs nothing on the two
        # platforms this was developed on and puts a dashed "tear this off" entry at the
        # front of the menu bar on the third.  Caught by tools/check_gui_platform.py running
        # under a Linux X server, which is the entire reason that file exists.
        # X11 draws menus itself and misses style_chrome, which only reaches ttk widgets;
        # empty on macOS and Windows, where the platform's own menu colours are the right
        # ones and overriding them would make this program disagree with every other.
        mc = menu_colors()
        bar = tk.Menu(root, tearoff=0, **mc)

        view = tk.Menu(bar, tearoff=0, **mc)
        lang_menu = tk.Menu(view, tearoff=0, **mc)
        self.lang = tk.StringVar(value=i18n.get_lang())
        for code in i18n.LANGS:
            # Each language is named in ITSELF and never translated: someone looking for
            # their language in a window they cannot read is looking for a word they know.
            lang_menu.add_radiobutton(label=LANG_NAMES[code], value=code,
                                      variable=self.lang, command=self.change_lang)
        view.add_cascade(label=t("menu.language"), menu=lang_menu)

        theme_menu = tk.Menu(view, tearoff=0, **mc)
        self.theme = tk.StringVar(value=load_config().get("theme") or "auto")
        for value, key in (("auto", "menu.theme_auto"), ("light", "menu.theme_light"),
                           ("dark", "menu.theme_dark")):
            theme_menu.add_radiobutton(label=t(key), value=value, variable=self.theme,
                                       command=self.change_theme)
        view.add_cascade(label=t("menu.theme"), menu=theme_menu)
        bar.add_cascade(label=t("menu.view"), menu=view)

        # The danger zone gets its own menu rather than hiding in View.  A setting that can
        # ruin somebody's ECU should not sit one line below the colour scheme.
        adv = tk.Menu(bar, tearoff=0, **mc)
        self.force_ecu = tk.BooleanVar(value=False)
        adv.add_checkbutton(label=t("menu.force_ecu"), variable=self.force_ecu,
                            command=self.toggle_force_ecu)
        bar.add_cascade(label=t("menu.advanced"), menu=adv)

        helpm = tk.Menu(bar, tearoff=0, name="help", **mc)
        helpm.add_command(label=t("ui.help_btn"), command=self.help_window)
        bar.add_cascade(label=t("menu.help"), menu=helpm)

        root.config(menu=bar)
        self.menubar = bar

    def change_theme(self):
        """Switch palette.  Same rebuild as a language change, for the same reason."""
        if self.busy:
            self.theme.set(load_config().get("theme") or "auto")
            return
        save_config(theme=self.theme.get())
        self.rebuild()

    def change_lang(self, _event=None):
        """Switch language, remember it, and rebuild the window in the new one.

        A rebuild rather than a re-label: every widget takes its text at construction, and
        so do the two drawings, which are canvases full of positioned items rather than
        anything with a `text` property to update.  Chasing every string would be a second
        place for a string to live, and the one that got missed would be the one nobody
        noticed.  Rebuilding has one cost -- the log widget is recreated -- so its contents
        are carried across; the file on disk was never interrupted anyway."""
        want = self.lang.get()
        if want not in i18n.LANGS or want == i18n.get_lang():
            return
        if self.busy:                       # never re-lay the window out mid-flash
            self.lang.set(i18n.get_lang())
            return
        i18n.set_lang(want)
        save_config(lang=want)
        self.rebuild()

    def rebuild(self):
        """Tear the window down and put it back up in the current language."""
        text = self.log.get("1.0", "end-1c")
        try:
            self.root.after_cancel(self._drain_id)
        except Exception:
            pass
        if self.logfh:
            try:
                self.logfh.close()
            except Exception:
                pass
        for w in self.root.winfo_children():
            w.destroy()
        self.__init__(self.root)
        if text.strip():
            self.log.insert("end", text)
            self.log.see("end")

    def refresh_ports(self):
        """List the ports, adapters first, and never guess a non-adapter into the box.

        Picking ports[0] blind was worse than picking nothing: on macOS the first entry is
        usually Bluetooth-Incoming-Port or debug-console, the run then fails its handshake,
        and the verdict pipeline confidently tells the user their +12 V reached A4/B2 in the
        wrong order -- sending someone off to rewire a bench that was never wrong.
        """
        try:
            from serial.tools import list_ports
            found = list(list_ports.comports())
        except Exception:
            found = []

        def rank(p):
            if (p.vid, p.pid) == K.FTDI_VIDPID:
                return 0                      # the adapter this tool is built around
            return 1 if p.vid else 2          # then any USB serial device, then the rest

        found.sort(key=rank)
        ports = ["%s  %s" % (p.device, p.description) for p in found]
        self.port_box["values"] = ports
        current = self.port.get()
        if current and current not in ports:
            self.port.set("")                 # the chosen adapter was unplugged
            current = ""
        if ports and not current and found and rank(found[0]) < 2:
            self.port.set(ports[0])           # only when it actually looks like an adapter
        if not ports:
            self.port.set("")
            self.status.set(t("st.no_adapter"))

    def pick(self):
        p = filedialog.askopenfilename(filetypes=[(t("ui.image_files"), "*.bin"),
                                                  (t("ui.all_files"), "*.*")])
        if p:
            self.path.set(p)

    LOG_LIMIT = 4000                            # lines kept on screen; the file keeps all

    def say(self, text):
        self.log.insert("end", text)
        # trimming matters: a full write emits thousands of lines, and inserting into an
        # ever-growing Text with autoscroll gets slower exactly when timing matters most
        if int(self.log.index("end-1c").split(".")[0]) > self.LOG_LIMIT:
            self.log.delete("1.0", "%d.0" % (self.LOG_LIMIT // 4))
        self.log.see("end")
        if self.logfh:
            try:
                self.logfh.write(text)
                self.logfh.flush()              # survive a crash mid-write
            except Exception:
                pass                            # a log we cannot write must not stop work

    def port_name(self):
        return self.port.get().split()[0] if self.port.get() else ""

    def power_cycle_prompt(self):
        """The 'insert disk 2' moment.  The ROM loader only arms on a real power-on
        reset and autobauds once, so polling for it would burn the very thing we are
        waiting for -- asking plainly is both simpler and more reliable."""
        return messagebox.askokcancel(t("dl.power_title"), t("dl.power_body"))

    # --- running the real tool ---------------------------------------------
    def run(self, argv, title):
        if self.busy:
            return
        if not self.port_name():
            messagebox.showerror(t("app.title"), t("dl.no_adapter"))
            return
        if not self.power_cycle_prompt():
            return
        self.busy = True
        for b in (self.read_btn, self.write_btn):
            b.state(["disabled"])
        self.bar["value"] = 0
        self.results = []           # this run's verdicts; finish() decides from these alone
        # Counted from the per-region problem events, which are emitted once each.
        self._unmapped_regions = self._unmapped_bytes = 0
        self._baud = None           # last rate the engine reported; see on_close()
        self._evbuf = ""
        self.status.set(title)
        import datetime
        self.say(u"\n===== %s — %s =====\n"
                 % (title, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        # Where THIS run's output starts.  A Tk mark, not an index string: the log is
        # trimmed from the top once it grows past LOG_LIMIT, which would silently slide a
        # saved "line.column" onto different text, and Tk maintains marks across edits for
        # exactly this reason.  Without an anchor, finish() searches the whole buffer and a
        # success from an EARLIER operation gets reported for this one -- a read that worked
        # makes a write that failed say "готово, проверено", which is the single worst thing
        # this interface could say.
        self.log.mark_set("runstart", "end-1c")
        self.log.mark_gravity("runstart", "left")
        threading.Thread(target=self.worker, args=(argv,), daemon=True).start()

    def worker(self, argv):
        """Run the CLI's own main() so the button cannot drift from the terminal.

        TWO pipes, not one: with --progress json the tool puts machine events on stdout and
        the human log on stderr, and the whole point is that this front end reads the events
        instead of the prose.  Merging the streams here would throw that distinction away
        and put us straight back to regexing sentences."""
        class Pipe(io.TextIOBase):
            def __init__(self, q, tag):
                self.q, self.tag = q, tag

            def write(self, s):
                if s:
                    self.q.put((self.tag, s))
                return len(s)

            def flush(self):
                pass

        old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout = Pipe(self.q, "event")
        sys.stderr = Pipe(self.q, "log")
        code = 0
        try:
            sys.argv = argv
            # main() RETURNS the status; it does not raise.  Dropping the return value left
            # `code` at 0 for every failure signalled that way -- which is most of them --
            # so the `died` guard below, whose comment says a nonzero status is a failure
            # whatever the events said, could never fire.  exit_status() is the same
            # normaliser the console entry point uses, so the button and the terminal agree.
            code = K.exit_status(K.main())
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                self.q.put(("log", e.code + "\n"))
        except Exception as e:
            code = 1
            self.q.put(("log", t("dl.error", e)))
        finally:
            sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
            self.q.put(("done", code))

    def drain(self):
        """Move the worker's log into the widget and its EVENTS into the progress bar."""
        # Batch everything waiting into ONE widget update per tick instead of one per
        # line, for the same reason the log file is held open: the worker is on the other
        # side of the GIL and this loop must not become the thing that slows it down.
        chunks, events, done = [], [], None
        try:
            while True:
                tag, payload = self.q.get_nowait()
                if tag == "done":
                    done = payload
                elif tag == "event":
                    events.append(payload)
                else:
                    chunks.append(payload)
        except queue.Empty:
            pass
        try:
            if chunks:
                self.say("".join(chunks))
            if events:
                # events can split across writes, so keep the tail until its newline arrives
                self._evbuf += "".join(events)
                lines = self._evbuf.split("\n")
                self._evbuf = lines.pop()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.on_event(json.loads(line))
                    except Exception:
                        # ANY failure, not just unparseable JSON.  An event with a string
                        # where a number belongs raises TypeError deep in on_event, and this
                        # used to escape the callback entirely -- Tk then never reschedules
                        # the pump, so the window freezes with the buttons disabled, the
                        # verdict never arrives, and the log stops updating mid-write.  The
                        # help tells a user with a frozen progress bar to unplug the USB
                        # cable, which would abort a write that was in fact proceeding
                        # perfectly well in the worker thread.  One malformed line must cost
                        # one line, so it is shown verbatim and the loop carries on.
                        self.say(line + "\n")
            if done is not None:
                self.finish(done)
        finally:
            # In a finally, always: the pump rescheduling itself on the last line of the
            # body means any escape from that body stops it forever.
            self._drain_id = self.root.after(80, self.drain)

    # What the progress bar should call each operation while it is running
    # Keys, not strings: resolved when an event is rendered, so switching language
    # changes what the next event says without rebuilding anything.
    OP_LABEL = {"read": "op.read", "backup": "op.backup",
                "verify": "op.verify", "write": "op.write"}
    STAGE_LABEL = {
        "handshake": "sg.handshake", "handoff": "sg.handoff",
        "calibrated": "sg.calibrated", "backup": "sg.backup",
        "backup-saved": "sg.backup_saved", "writing": "sg.writing",
        "verifying": "sg.verifying", "power-cycled": "sg.power_cycled",
    }

    def on_event(self, ev):
        """One machine event.  No prose is parsed here, and that is the entire point."""
        kind = ev.get("event")
        if kind == "progress":
            done, total = ev.get("done", 0), ev.get("total", 0) or 1
            pct = 100.0 * done / total
            self.bar["value"] = pct
            what = t(self.OP_LABEL.get(ev.get("op"), ev.get("op", "")))
            if ev.get("unit") == "sectors":
                tail = t("pr.retries", ev["retries"]) if "retries" in ev else u""
                self.status.set(t("pr.sectors", what, done, total, tail, self._eta(ev)))
            else:
                self.status.set(t("pr.bytes", what, pct, done // 1024, total // 1024,
                                  self._eta(ev)))
        elif kind == "stage":
            if ev.get("stage") == "backup-saved" and ev.get("path"):
                self.say(t("st.backup_at", ev["path"]))
            key = self.STAGE_LABEL.get(ev.get("stage"))
            if ev.get("ok") is False:
                key = None            # a stage that failed must not announce that it worked
            if key:
                self.status.set(t(key))
            self.bar["value"] = 0 if ev.get("stage") in ("backup", "writing", "verifying") \
                else self.bar["value"]
        elif kind == "problem":
            # The engine calls this kind "unmapped" and reports how many bytes of the image
            # could not be stored in `image_bytes`.  This branch matched "unwritable" and
            # read a field called `matches_image`, neither of which is emitted anywhere --
            # so it was dead, and a whole-image write silently dropped the one warning this
            # project's charter says must never be dropped.  It fires on EVERY full write,
            # because the device-reserved sector at 0xC0F000 is inside --flash's range.
            k = ev.get("kind")
            if k == "unmapped":
                # Counted here, from the per-region events, and NOT by summing the result
                # events: `unmapped_image_bytes` rides on BOTH the write result and the
                # final-verify result, which independently re-derive it, so summing those
                # doubled the figure in reliable mode and left it right in fast -- the same
                # ECU reporting a different number depending on how hard it was checked.
                # A problem event is emitted once per region, so counting them is the
                # arithmetic the status line was claiming to do all along.
                self._unmapped_regions += 1
                self._unmapped_bytes += ev.get("image_bytes") or 0
                self.say(t("st.unwritable", ev.get("addr", 0))
                         + (t("st.unwritable_bad") if ev.get("image_bytes") else u"")
                         + u"\n")
            elif k == "protection":
                self.say(t("st.protection_installed", ev.get("addr", 0)) + u"\n")
            elif k == "link_below_target":
                self.say(t("st.link_below_target") + u"\n")
            elif k == "boot_sector_differs":
                # The one problem that changes what the ECU ends up RUNNING: --flash never
                # writes the boot sector, so an image carrying a different one is only
                # partly applied.  Reaching only the log pane meant it could be scrolled
                # past in the language the user may not read.
                self.say(t("st.boot_differs", ev.get("differing", 0)) + u"\n")
        elif kind == "result":
            self.results.append(ev)

    @staticmethod
    def _eta(ev):
        eta = ev.get("eta")
        return t("pr.eta", eta // 60, eta % 60) if eta else u""

    def finish(self, code):
        self.busy = False
        for b in (self.read_btn, self.write_btn):
            b.state(["!disabled"])
        # A nonzero exit code is a failure whatever the events said.  The engine can emit a
        # passing result and then die -- final_verify raising something that is not IOError,
        # or any later sys.exit() -- and taking the success branch there would print
        # "Done, verified." directly above an ERROR line in the same window.  The events
        # decide WHICH failure to describe; they do not get to overrule the exit code.
        died = bool(code)
        # THE VERDICT.  Taken from the engine's own result events, per the contract in
        # klinebsl: succeeded iff at least one result arrived and none of them said no.
        # "No results" is explicitly NOT success -- that is the case where the run died
        # before finishing anything, and it is exactly what a log search used to misread.
        if self.results:
            failed = [r for r in self.results if not r.get("ok")]
            if not failed and not died:
                self.bar["value"] = 100
                if self.last_was_write and self.resume.get():
                    self.resume.set(False)    # the job is done; do not tax the next one
                    save_config(resume=False)
                # A write is verified; a read is not, and saying so is the difference
                # between reporting and reassuring.  And a write that could not place part
                # of the image says THAT instead: it succeeded at everything it was able to
                # do, which is not the same as having done what was asked.
                if self.last_was_write and self._unmapped_bytes:
                    self.status.set(t("st.done_unplaced", self._unmapped_regions,
                                      self._unmapped_bytes))
                else:
                    self.status.set(t("st.done_verified" if self.last_was_write
                                      else "st.read_done"))
                return
            # `failed` can be EMPTY here and still be a failure: that is the case where
            # every result passed and the run then died anyway, which is precisely what the
            # exit code is for.  Reaching for failed[0] unconditionally turned that case
            # into an IndexError inside the handler that exists to report failures.
            self.status.set(self._verdict_text(failed[0]) if failed else t("vd.generic"))
            # Do NOT offer to resume something that never started.  A refused self-test or a
            # stage that never landed erases nothing, so "run it again and the sectors
            # already written will be skipped" is both false and, after a refusal that
            # exists because writing looked unsafe, exactly the wrong advice.
            nothing_written = any(r.get("wrote_nothing") for r in self.results)
            if self.last_was_write and not nothing_written and not self.resume.get():
                self.resume.set(True)
                save_config(resume=True)
                self.status.set(self.status.get() + t("st.resume_armed"))
            return
        # Nothing structured arrived (an older engine, or a crash before the first event):
        # fall back to reading this run's log, never the whole buffer -- see run()
        try:
            text = self.log.get("runstart", "end")
        except tk.TclError:
            text = self.log.get("1.0", "end")       # no run has started yet
        if ("BYTE-IDENTICAL" in text or "WRITE COMPLETE AND VERIFIED" in text
                or "DUMP COMPLETE" in text):
            self.bar["value"] = 100
            self.status.set(t("st.done_verified"))
            return
        # A write that did not finish is the one case where the next attempt should behave
        # differently, so arm it here rather than expecting the user to know that.
        if self.last_was_write and not self.resume.get():
            self.resume.set(True)
            save_config(resume=True)
            for needle, why in WHY:
                if needle in text:
                    self.status.set(t(why) + t("st.resume_armed"))
                    return
            self.status.set(t("st.write_unfinished"))
            return
        for needle, why in WHY:
            if needle in text:
                self.status.set(t(why))
                return
        self.status.set(t("st.not_finished"))

    def _verdict_text(self, bad):
        """Turn a failing result event into the sentence the person in front of us needs.

        The engine's message is never shown: it is written for a log, in English, and
        'what do I do now' is a different question from 'what happened'.  Everything here
        comes from the catalogue, so the person reads their own language whatever the
        engine happened to print."""
        for needle, why in WHY:
            if needle.lower() in (bad.get("message") or "").lower():
                return t(why)
        what = bad.get("what")
        if what == "handshake":
            return t("vd.handshake")
        if what == "link":
            return t("vd.link")
        if what == "read":
            return t("vd.read")
        # There was a `bad.get("unplaced")` branch here.  It was dead twice over: the engine
        # emits no such field, and this function only ever sees a FAILED result, while
        # unplaced bytes ride on a successful one.  They are reported by finish() from the
        # per-region problem events instead.
        return t("vd.generic")

    # --- operations ---------------------------------------------------------
    def do_read(self):
        if not self.port_name():              # checked BEFORE the save dialog, not after
            messagebox.showerror(t("app.title"), t("dl.no_adapter"))
            return
        out = filedialog.asksaveasfilename(defaultextension=".bin",
                                           initialfile="m74_dump.bin",
                                           initialdir=self.outdir,
                                           filetypes=[(t("ui.image_files"), "*.bin")])
        if not out:
            return
        self.path.set(out)
        # The streaming reader cannot change speed mid-session, so its rate is whatever
        # the ROM locks onto at the handshake.  19200 measured zero autobaud misses here
        # and is twice the speed of the safe default; above it the ROM starts missing.
        self.last_was_write = False
        self.run(["openm74", "--port", self.port_name(), "--baud", str(READ_BAUD),
                  "--progress", "json", "--oneshot", "--out", out],
                 t("st.reading"))

    def do_write(self):
        p = self.path.get()
        if not p or not os.path.isfile(p):
            messagebox.showerror(t("app.title"), t("dl.pick_image"))
            return
        if os.path.getsize(p) != K.FLASH_SIZE:
            messagebox.showerror(t("app.title"),
                                 t("dl.wrong_size", K.FLASH_SIZE, os.path.getsize(p)))
            return
        fast = self.mode.get() == "fast"
        # Everything the tool produces on its own -- log, settings, backups -- goes to one
        # place beside the application, so there is a single folder to look in and a single
        # folder to copy away.  The image being flashed may live on a memory stick that is
        # gone by the time the backup matters; where the tool lives, it stays.
        bdir = self.outdir
        if not messagebox.askokcancel(
                t("dl.confirm_title"),
                t("dl.confirm_body",
                  t("dl.mode_fast") if fast else t("dl.mode_reliable"), bdir,
                  t("dl.write_time_fast") if fast else t("dl.write_time_reliable"))):
            return
        # --backup on purpose: fast mode would skip it, but the confirmation above
        # promises a backup, and an interface must not promise a net it does not deploy.
        argv = ["openm74", "--port", self.port_name(), "--mode", self.mode.get(),
                "--progress", "json", "--backup", "--backup-dir", bdir,
                "--flash", p, "--yes"]
        if self.resume.get():
            argv.append("--resume")
        if self.force_ecu.get():
            argv.append("--force-unknown-ecu")
        self.last_was_write = True
        self.run(argv, t("st.writing"))

    def help_window(self, open_tab=0):
        """One window, a tab per question, and buttons that jump straight to the right
        one.

        Each tab is a single scrolling surface holding the diagram AND the text, so the
        connector drawing scrolls with the words that explain it instead of being pinned
        above a separate scrollbox."""
        existing = getattr(self, "_help", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()                  # raise the one that is already open
            existing.lift()
            existing.focus_force()
            return existing
        w = self._help = tk.Toplevel(self.root)
        w.title(t("ui.help_btn") + u" — " + t("app.device"))
        w.geometry("%dx680" % layout()["width"])
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        for title, body in i18n.help_tabs(t("app.device")):
            tab = ttk.Frame(nb)
            nb.add(tab, text=title)

            # yscrollincrement=1 makes one "unit" exactly one pixel, which is what lets a
            # trackpad's precise deltas move the view by the amount the fingers actually
            # moved instead of a notch at a time.
            canvas = tk.Canvas(tab, highlightthickness=0, yscrollincrement=1,
                               bg=THEME["surface"])
            bar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)

            inner = tk.Frame(canvas, bg=THEME["surface"])
            canvas.create_window((0, 0), window=inner, anchor="nw")

            def fit(e=None, c=canvas):
                """Keep the scrollregion at least as tall as the viewport.

                When it is SHORTER, Tk centres the content in the window and lets the
                view drift both ways by the leftover -- which is exactly the phantom
                overscroll at the top, equal to the empty space at the bottom."""
                box = c.bbox("all")
                if not box:
                    return
                c.configure(scrollregion=(0, 0, box[2],
                                          max(box[3], c.winfo_height())))
                c.yview_moveto(0.0) if box[3] <= c.winfo_height() else None
            inner.bind("<Configure>", fit)
            canvas.bind("<Configure>", fit)

            if title == i18n.HELP_TITLES["help.wiring"][i18n.get_lang()]:
                draw_connectors(inner).pack(fill="x", padx=4, pady=(6, 0))

            lines = body.strip("\n").splitlines()
            # width from the longest line for the same reason: nothing stretches it here,
            # and wrap="none" would otherwise clip the ends of the long ones.
            #
            # BOTH colours pinned, from the same palette as the drawing above it.  Pinning
            # the background and letting the foreground follow the system is exactly how
            # this panel once rendered as a blank rectangle in dark mode -- pin both or pin
            # neither.  And sharing the drawing's surface is what removes the seam: there is
            # no card sitting on a page any more, the drawing IS the page.
            txt = tk.Text(inner, wrap="none", padx=14, pady=10, borderwidth=0,
                        highlightthickness=0,
                        height=len(lines) + 2, width=max(len(l) for l in lines) + 2,
                        font=(MONO_FAMILY, 10),
                        bg=THEME["surface"], fg=THEME["ink"])
            txt.insert("1.0", "\n".join(lines))
            txt.configure(state="disabled")       # sized to its content: the tab scrolls,
            txt.pack(fill="x")                    # not the text box inside it

            def scroll(px, c=canvas):
                c.yview_scroll(px, "units")     # one unit == one pixel, set above

            # Every child, or the gesture dies wherever the pointer happens to be -- hovering
            # the connector drawing used to leave the page stuck.  Binding on the children
            # also has to swallow the event (the handlers return "break"), because Tk's own
            # Text bindings would otherwise scroll the text box inside the page instead of
            # the page, which is what let a tab drift above its own top.
            bind_scroll(canvas, scroll)
            bind_scroll(inner, scroll)

        nb.select(open_tab)
        return w        # returned so tools/check_gui_platform.py can inspect the real thing


def main():
    # Remembered choice wins over detection, because a person who has picked a language has
    # said something more specific than their operating system did.
    i18n.set_lang(load_config().get("lang") or i18n.detect())
    root = tk.Tk()
    init_fonts()            # needs a root; picks each platform's own UI and fixed faces
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
