"""Failing spec for unbounded grave-accent output capture.

shellpython stores every byte of a captured command's stdout and stderr.
These tests require each stream to be aborted at or before
MAX_CAPTURED_STREAM_BYTES, including a single line with no newline.
They fail while that cap is absent.

Run:
    python3 -m unittest shellpython.tests.test_unbounded_output_capture -v
"""
import os
import shlex
import sys
import tempfile
import threading
import tracemalloc
import unittest

from shellpython import core
from shellpython.preprocessor import preprocess_file

# Short command output in the rest of the suite is far below this.
# Oversized captures must not keep this many bytes from one stream.
MAX_CAPTURED_STREAM_BYTES = 64 * 1024
# Peak traced growth allowed while the runner is reading a stream.
# Large enough for a capped read, smaller than the full oversized body
# plus the decoded copy readlines()/readline() retain today.
MEMORY_BUDGET_BYTES = 8 * MAX_CAPTURED_STREAM_BYTES
OVERSIZED_BYTES = 8 * MAX_CAPTURED_STREAM_BYTES
UNDER_CAP_BYTES = 4 * 1024
MARKER = b"PASTTHECAP"
CAPTURE_TIMEOUT_SECONDS = 5


def _write_blob(path, total, lines):
    """Write `total` bytes ending in MARKER. `lines` inserts newlines."""
    if total <= len(MARKER):
        raise ValueError("payload shorter than marker")
    body = total - len(MARKER)
    with open(path, "wb") as handle:
        left = body
        while left:
            if lines:
                take = min(63, left)
                handle.write(b"A" * take)
                if left - take > 0:
                    handle.write(b"\n")
                    left -= take + 1
                    continue
                left -= take
            else:
                take = min(4096, left)
                handle.write(b"A" * take)
                left -= take
        handle.write(MARKER)


def _kill_child_processes():
    parent = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == parent:
            continue
        try:
            stat = open("/proc/%s/stat" % name).read()
        except OSError:
            continue
        ppid = int(stat.split()[3])
        if ppid == parent:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


class UnboundedOutputCaptureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="shellpy-cap-")

    def _payload(self, name, total, lines):
        path = os.path.join(self._tmp, name)
        _write_blob(path, total, lines)
        return path

    def _capture(self, fn):
        box = {}

        def run():
            tracemalloc.start()
            tracemalloc.clear_traces()
            baseline = tracemalloc.get_traced_memory()[0]
            try:
                try:
                    box["value"] = fn()
                except Exception as exc:
                    box["exc"] = exc
            finally:
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                box["growth"] = peak - baseline

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(CAPTURE_TIMEOUT_SECONDS)
        if thread.is_alive():
            _kill_child_processes()
            thread.join(CAPTURE_TIMEOUT_SECONDS)
            self.fail(
                "capture did not finish within %ss; the runner blocked "
                "instead of aborting the stream" % CAPTURE_TIMEOUT_SECONDS
            )
        return box

    def _texts_from_box(self, box):
        value = box.get("value")
        exc = box.get("exc")
        if exc is not None and hasattr(exc, "result"):
            value = exc.result
        if isinstance(value, core.Result):
            return value.stdout, value.stderr
        if isinstance(value, str):
            return value, ""
        return "", ""

    def assert_capture_aborted(self, fn):
        box = self._capture(fn)
        stdout_text, stderr_text = self._texts_from_box(box)
        growth = box.get("growth", 0)
        problems = []
        if "exc" not in box:
            problems.append("call returned instead of aborting")
        if MARKER.decode("ascii") in stdout_text or MARKER.decode("ascii") in stderr_text:
            problems.append("retained the byte range past the cap")
        if len(stdout_text) > MAX_CAPTURED_STREAM_BYTES:
            problems.append("stdout kept %s bytes" % len(stdout_text))
        if len(stderr_text) > MAX_CAPTURED_STREAM_BYTES:
            problems.append("stderr kept %s bytes" % len(stderr_text))
        if growth > MEMORY_BUDGET_BYTES:
            problems.append(
                "peak allocation grew by %s bytes (budget %s)"
                % (growth, MEMORY_BUDGET_BYTES)
            )
        if problems:
            self.fail("; ".join(problems))

    def test_under_cap_stdout_is_still_returned(self):
        path = self._payload("small-out", UNDER_CAP_BYTES, lines=True)
        result = core.exe("cat %s" % shlex.quote(path), "")
        self.assertIn(MARKER.decode("ascii"), result.stdout)
        self.assertLessEqual(len(result.stdout), MAX_CAPTURED_STREAM_BYTES)

    def test_grave_accent_stdout_lines_are_capped(self):
        path = self._payload("grave-out", OVERSIZED_BYTES, lines=True)
        spy_path = os.path.join(self._tmp, "cap.spy")
        with open(spy_path, "w") as handle:
            handle.write("result = `cat %s\n" % path)
        compiled = preprocess_file(spy_path, is_root_script=False)
        code = open(compiled).read()
        self.assertIn("exe(", code)
        namespace = {"__name__": "spy_cap"}

        def run():
            exec(compile(code, compiled, "exec"), namespace)
            return namespace.get("result")

        self.assert_capture_aborted(run)

    def test_stdout_without_newline_is_capped(self):
        path = self._payload("one-out", OVERSIZED_BYTES, lines=False)
        self.assert_capture_aborted(
            lambda: core.exe("cat %s" % shlex.quote(path), "")
        )

    def test_stderr_is_capped(self):
        path = self._payload("err", OVERSIZED_BYTES, lines=True)
        # exec replaces the shell and sends the body to stderr, so the
        # runner reaches the stderr readlines loop.
        cmd = "exec cat %s >&2" % shlex.quote(path)
        self.assert_capture_aborted(lambda: core.exe(cmd, ""))

    def test_stderr_without_newline_is_capped(self):
        path = self._payload("err-one", OVERSIZED_BYTES, lines=False)
        cmd = "exec cat %s >&2" % shlex.quote(path)
        self.assert_capture_aborted(lambda: core.exe(cmd, ""))

    def test_stderr_with_stdout_left_open_does_not_block(self):
        path = self._payload("err-open", OVERSIZED_BYTES, lines=False)
        # Same bytes, but the shell keeps the stdout pipe open. A runner
        # that drains stdout to EOF before reading stderr blocks once the
        # stderr pipe fills, and never applies a cap.
        cmd = "cat %s >&2" % shlex.quote(path)
        self.assert_capture_aborted(lambda: core.exe(cmd, ""))

    def test_no_throw_flag_does_not_disable_the_cap(self):
        path = self._payload("no-throw", OVERSIZED_BYTES, lines=True)
        self.assert_capture_aborted(
            lambda: core.exe("cat %s" % shlex.quote(path), "n")
        )

    def test_interactive_stdout_line_is_capped(self):
        path = self._payload("i-out", OVERSIZED_BYTES, lines=False)

        def run():
            result = core.exe("cat %s" % shlex.quote(path), "i")
            return result.stdout.sreadline()

        self.assert_capture_aborted(run)

    def test_interactive_stderr_line_is_capped(self):
        path = self._payload("i-err", OVERSIZED_BYTES, lines=False)
        cmd = "exec cat %s >&2" % shlex.quote(path)

        def run():
            result = core.exe(cmd, "i")
            return result.stderr.sreadline()

        self.assert_capture_aborted(run)

    def test_both_streams_in_one_command_are_capped(self):
        stdout_path = self._payload("both-out", OVERSIZED_BYTES, lines=True)
        stderr_path = self._payload("both-err", OVERSIZED_BYTES, lines=True)
        code = (
            "import sys\n"
            "sys.stdout.buffer.write(open(%r,'rb').read())\n"
            "sys.stdout.close()\n"
            "sys.stderr.buffer.write(open(%r,'rb').read())\n"
        ) % (stdout_path, stderr_path)
        cmd = "exec %s -c %s" % (sys.executable, shlex.quote(code))
        self.assert_capture_aborted(lambda: core.exe(cmd, ""))


if __name__ == "__main__":
    unittest.main()
