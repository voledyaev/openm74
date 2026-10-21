"""State owned by one ECU operation, shared explicitly with its front end."""
import argparse
import builtins
from contextlib import contextmanager
from contextvars import ContextVar
import os
import sys
import threading

_CURRENT = ContextVar("openm74_session", default=None)


class Session:
    """One operation's settings, output and cooperative cancellation.

    Create a fresh instance for each operation. A front end may request cancellation
    before its worker starts; entering the engine must not clear that request.
    """

    def __init__(self, *, log=None, events=None):
        self.log = log
        self.events = events
        self.reference = os.environ.get("OPENM74_REFERENCE", "")
        self.settle = 0.002
        self.status_look = 0.8
        self.baud_const = 1243200.0
        self.stop = threading.Event()
        self.started = False

    def request_stop(self):
        self.stop.set()

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)


def current_session():
    state = _CURRENT.get()
    if state is None:
        # Low-level helpers are also used directly by offline probes and tests. They
        # get a context-local state without becoming the next main() call's state.
        state = Session()
        _CURRENT.set(state)
    return state


def print_log(*args, **kwargs):
    if "file" not in kwargs:
        kwargs["file"] = current_session().log or sys.stdout
    builtins.print(*args, **kwargs)


class SessionParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            (current_session().log or file or sys.stderr).write(message)
