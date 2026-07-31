#!/usr/bin/env python3
"""Entry point for the frozen application.

PyInstaller runs its entry script as __main__ at the top level, not as a module inside a
package.  Pointing it straight at gui.py therefore strips the file of its parent package,
`from . import klinebsl` has nothing to resolve against, and the build dies on import
before a window ever appears.

Importing the package by name instead makes the executable follow exactly the path a
`pip install` follows through the openm74-gui entry point: one code path to test, not a
second one that only exists inside the .exe and only breaks after shipping.
"""
import sys

# At module level on purpose, and not inside main(): reaching this line is what proves the
# package, the GUI module and Tk's Python bindings all resolved inside the bundle.  Moving
# it into a function would quietly remove that from --selftest's coverage.
from openm74 import __version__
from openm74.gui import main


def selftest():
    """Prove the things that can ONLY break inside a bundle, and nothing else.

    A windowed application cannot report anything readable when it is broken -- a frozen
    import error stops at a modal dialog that waits forever -- but it can still return an
    exit code, which is what makes this worth running from the build script.

    Three failure modes are possible here and all three are invisible until first launch:

      * **imports not resolved.** A missing hidden import; reaching this function at all
        already proves the package and its serial dependency came along.
      * **data files that did not travel.** The assembled stages are shipped so the tool
        can check the bytes it is about to push into an ECU against the assembler's own
        output.  If they silently fail to ship, that cross-check degrades to a no-op and
        says nothing -- so a build without them must not pass.
      * **libraries loaded by path at runtime rather than imported.** pyserial finds serial
        ports on macOS through ctypes against IOKit and CoreFoundation, which the bundler
        explicitly declines to trace ("only basenames are supported with ctypes imports").
        Enumerating ports is therefore the one thing most likely to work on the build
        machine and fail in the bundle -- and if it fails, every adapter is invisible and
        the application is useless while looking perfectly healthy.
      * **the GUI toolkit's own runtime files.** Importing tkinter proves nothing; Tcl/Tk
        only loads its script library when a root window is created.  So create one, and
        throw it away.  (This needs a desktop session: build on the machine you are sitting
        at, not over a bare ssh connection.)
    """
    ok = True

    from openm74 import klinebsl as K
    print("openm74 %s" % __version__)

    blob = K.check_monitor_blob()
    if blob is True:
        print("  ok   assembled stages shipped, and the embedded monitor matches them")
    elif blob is None:
        print("  FAIL assembled stages did not travel into the bundle (%s)" % K.MON_ASM_BIN)
        ok = False
    else:
        print("  FAIL embedded monitor does not match the shipped stage2mon.bin")
        ok = False

    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        print("  ok   serial enumeration works (%d port(s) visible)" % len(ports))
    except Exception as e:
        print("  FAIL serial enumeration is broken in the bundle: %s: %s"
              % (type(e).__name__, e))
        ok = False

    # Where this build will keep a user's files -- worth printing, because it is decided by
    # platform rules (bundle layout, translocation, folder permissions) that nobody can guess
    # by looking, and because the build script runs this on the real artifact.
    from openm74 import gui as G
    beside = G.app_dir()
    where = G.data_dir()
    print("  ok   files go to %s%s"
          % (where, "" if beside and beside == where else "  (NOT beside the app)"))

    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        ver = root.tk.call("info", "patchlevel")
        root.destroy()
        print("  ok   Tk starts (%s)" % ver)
    except Exception as e:
        print("  FAIL Tk cannot start in the bundle: %s: %s" % (type(e).__name__, e))
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
