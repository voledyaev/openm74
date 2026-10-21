"""Regression checks for offline failures and front-end session isolation."""
import io
from pathlib import Path
import queue
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
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
        worker.session = K.Session()
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
        state = K.Session()
        state.request_stop()
        with K._RUN_LOCK, self.assertRaisesRegex(RuntimeError, "already running"):
            K.main(["--help"], session=state)
        self.assertTrue(state.stop.is_set())
        self.assertFalse(state.started)

    def test_help_and_parse_errors_use_supplied_log(self):
        for argv, expected in ((["--help"], 0), (["--invalid-option"], 2)):
            output = io.StringIO()
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                K.main(argv, log=output)
            self.assertEqual(error.exception.code, expected)
            self.assertIn("usage:", output.getvalue())
            self.assertIsNot(K.current_session().log, output)
            self.assertFalse(K._RUN_LOCK.locked())


class StateTests(unittest.TestCase):
    def test_settings_and_output_do_not_leak_between_sessions(self):
        first, second = K.Session(log=io.StringIO()), K.Session(log=io.StringIO())
        with first.activate():
            first.baud_const = 999999
            first.reference = "first.bin"
            first.status_look = 0.01
            first.request_stop()
            K.print("first")
            with second.activate():
                self.assertNotEqual(K.current_session().baud_const, 999999)
                self.assertNotEqual(K.current_session().reference, "first.bin")
                self.assertFalse(K.stop_requested())
                K.print("second")
            self.assertIs(K.current_session(), first)
            self.assertTrue(K.stop_requested())
        self.assertEqual(first.log.getvalue(), "first\n")
        self.assertEqual(second.log.getvalue(), "second\n")

    def test_stop_requested_before_worker_entry_is_preserved(self):
        state = K.Session()
        state.request_stop()
        seen = []
        with patch.object(K, "_main", side_effect=lambda argv: seen.append(K.stop_requested())):
            self.assertEqual(K.main([], session=state), 0)
        self.assertEqual(seen, [True])

    def test_used_session_cannot_reuse_calibration(self):
        state = K.Session(log=io.StringIO())
        with patch.object(K, "_main", return_value=True):
            K.main([], session=state)
        with self.assertRaisesRegex(RuntimeError, "fresh Session"):
            K.main([], session=state)
        self.assertFalse(K._RUN_LOCK.locked())


class CloseTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app._closing = False
        app.busy = True
        app.last_was_write = True
        app.session = K.Session()
        app._worker = Mock()
        app._worker.is_alive.return_value = True
        app.root = Mock()
        app.logfh = Mock()
        app.status = Mock()
        return app

    def test_close_waits_without_deadline_and_drains_before_destroy(self):
        app = self.make_app()
        with patch("openm74.gui.messagebox.askokcancel", return_value=True) as prompt:
            app.on_close()
            app.on_close()                   # repeated clicks must not force exit
        prompt.assert_called_once()
        self.assertTrue(app.session.stop.is_set())
        for _ in range(10):
            app._poll_close()               # a slow sector can exceed any estimate
        app.root.destroy.assert_not_called()
        app.logfh.close.assert_not_called()
        app.busy = False                    # final events have been drained
        app._poll_close()
        app.root.destroy.assert_not_called()  # the worker has not returned yet
        app._worker.is_alive.return_value = False
        app._poll_close()
        app.logfh.close.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_cancel_close_keeps_operation_running(self):
        app = self.make_app()
        with patch("openm74.gui.messagebox.askokcancel", return_value=False):
            app.on_close()
        self.assertFalse(app._closing)
        self.assertFalse(app.session.stop.is_set())
        app.root.after.assert_not_called()
        app.root.destroy.assert_not_called()


class VerdictTests(unittest.TestCase):
    def test_success_prose_without_result_is_never_verified(self):
        from openm74.i18n import t
        for code in (0, 1):
            with self.subTest(code=code):
                app = App.__new__(App)
                app.results = []
                app.read_btn, app.write_btn = Mock(), Mock()
                app._input_controls = [app.read_btn, app.write_btn]
                app.last_was_write = False
                app.resume = Mock()
                app.resume.get.return_value = False
                app.log = Mock()
                app.log.get.return_value = "DUMP COMPLETE AND VERIFIED"
                app.status = Mock()
                app.bar = {"value": 0}
                app.finish(code)
                app.status.set.assert_called_with(t("st.not_finished"))
                self.assertEqual(app.bar["value"], 0)


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
