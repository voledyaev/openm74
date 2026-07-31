"""openm74 — read and write the internal flash of an Itelma M74 CAN ECU over K-line.

The package is deliberately thin: `klinebsl` is the whole tool, `gui` is a front end that
drives it by building the same command line a person would type.  Nothing is duplicated
between them, so the button and the terminal cannot drift apart.
"""

__version__ = "1.0.0"

__all__ = ["gui", "klinebsl"]
