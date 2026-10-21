"""Regression checks for offline failures and front-end session isolation."""
import io
from pathlib import Path
import queue
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import importlib.util

from openm74 import klinebsl as K
from openm74.gui import App


class SessionTests(unittest.TestCase):
    def test_verify_requires_readable_nonempty_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.bin"
            image.write_bytes(b"")
            for reference in (Path(tmp) / "missing.bin", image, Path(tmp)):
                with self.subTest(reference=reference):
                    rc = K.main(["--verify", str(image), "--reference", str(reference)],
                                log=io.StringIO())
                    self.assertEqual(rc, 1)

    def test_failed_open_restores_signal_and_closes_transport(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            with patch.object(K.KlineBSL, "open", side_effect=OSError("failed")), \
                    patch.object(K.KlineBSL, "close") as close:
                rc = K.main(["--port", "FAKE", "--write", "unused.bin", "--yes"],
                            log=io.StringIO())
            self.assertEqual(rc, 1)
            self.assertIs(signal.getsignal(signal.SIGINT), previous)
            close.assert_called_once()
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_failed_close_still_restores_signal(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            with patch.object(K.KlineBSL, "open", side_effect=OSError("open failed")), \
                    patch.object(K.KlineBSL, "close", side_effect=OSError("close failed")), \
                    self.assertRaisesRegex(OSError, "close failed"):
                K.main(["--port", "FAKE", "--write", "unused.bin", "--yes"],
                       log=io.StringIO())
            self.assertIs(signal.getsignal(signal.SIGINT), previous)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_worker_does_not_replace_process_streams_or_arguments(self):
        worker = App.__new__(App)
        worker.q = queue.Queue()
        original = (sys.stdout, sys.stderr, sys.argv)

        def fail_open(transport):
            self.assertIs(sys.stdout, original[0])
            self.assertIs(sys.stderr, original[1])
            self.assertIs(sys.argv, original[2])
            raise OSError("simulated open failure")

        with patch.object(K.KlineBSL, "open", fail_open), \
                patch.object(K.KlineBSL, "close"):
            worker.worker(["openm74", "--port", "FAKE", "--monitor", "--progress", "json"])
        messages = []
        while not worker.q.empty():
            messages.append(worker.q.get_nowait())
        self.assertIn(("done", 1), messages)
        self.assertTrue(any(tag == "event" and '"ok": false' in value
                            for tag, value in messages if isinstance(value, str)))
        self.assertTrue(any(tag == "log" and "simulated open failure" in value
                            for tag, value in messages if isinstance(value, str)))

    def test_overlapping_session_rejected_without_resetting_stop(self):
        with K._RUN_LOCK:
            K.request_stop()
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    K.main(["--help"], log=io.StringIO())
                self.assertTrue(K.stop_requested())
            finally:
                K.clear_stop()

    def test_help_and_parse_errors_use_supplied_log(self):
        for argv, expected in ((["--help"], 0), (["--invalid-option"], 2)):
            output = io.StringIO()
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                K.main(argv, log=output)
            self.assertEqual(error.exception.code, expected)
            self.assertIn("usage:", output.getvalue())
            self.assertIsNone(K._LOG_OUT.get())
            self.assertFalse(K._RUN_LOCK.locked())


class BuildTests(unittest.TestCase):
    def test_invalid_signature_stops_packaging(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "build_exe.py"
        spec = importlib.util.spec_from_file_location("build_exe", script)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with patch.object(builder.subprocess, "call", return_value=1), \
                self.assertRaisesRegex(SystemExit, "refusing to package"):
            builder.check_signature("test.app")


if __name__ == "__main__":
    unittest.main()
