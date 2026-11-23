"""Spec for the grave-accent command runner pipe deadlock.

_create_result and interactive stdout iteration read one pipe to completion
before draining the other. A child that fills the unread pipe blocks forever,
and so does the runner.

These tests fail while that sequential read is in place. They pass when both
pipes are drained together, the captured bytes are intact, and the child is
reaped. Run:

    python -m unittest shellpython.tests.test_pipe_deadlock
"""
from __future__ import print_function

import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from shellpython.preprocessor import preprocess_file

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Longer than a successful collect of one pipe-buffer of data, short enough
# that a blocked readlines is obvious.
_COLLECT_TIMEOUT_SECONDS = 5


def _pipe_burst_size():
    """One byte past the OS pipe capacity, which is what stalls a blocking write."""
    if os.name == "posix":
        try:
            import fcntl
            read_fd, write_fd = os.pipe()
            try:
                if hasattr(fcntl, "F_GETPIPE_SZ"):
                    return fcntl.fcntl(read_fd, fcntl.F_GETPIPE_SZ) + 1
            finally:
                os.close(read_fd)
                os.close(write_fd)
        except (AttributeError, OSError, IOError):
            pass
    # Default pipe buffers on Linux, macOS, and Windows are all well under this.
    return 256 * 1024


PIPE_BURST = _pipe_burst_size()


def _write_all_child(body):
    return (
        "import os\n"
        "def write_all(fd, payload):\n"
        "    while payload:\n"
        "        written = os.write(fd, payload)\n"
        "        if written <= 0:\n"
        "            raise SystemExit(1)\n"
        "        payload = payload[written:]\n"
        + body
    )


class TestSequentialPipeReads(unittest.TestCase):
    def setUp(self):
        self._temps = []

    def tearDown(self):
        for path in self._temps:
            try:
                os.remove(path)
            except OSError:
                pass

    def _temp(self, suffix, contents):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
        try:
            handle.write(contents)
        finally:
            handle.close()
        self._temps.append(handle.name)
        return handle.name

    def _env(self):
        env = os.environ.copy()
        previous = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = REPO_ROOT if not previous else REPO_ROOT + os.pathsep + previous
        return env

    def _kill(self, proc):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def _run_python(self, args):
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._env(),
        }
        if hasattr(subprocess, "DEVNULL"):
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            devnull = open(os.devnull, "rb")
            self.addCleanup(devnull.close)
            kwargs["stdin"] = devnull
        if os.name == "posix":
            if sys.version_info >= (3, 2):
                kwargs["start_new_session"] = True
            else:
                kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen([sys.executable] + args, **kwargs)
        done = threading.Event()
        timed_out = []

        def watch():
            if not done.wait(_COLLECT_TIMEOUT_SECONDS):
                timed_out.append(True)
                self._kill(proc)

        watcher = threading.Thread(target=watch)
        watcher.start()
        out, err = proc.communicate()
        done.set()
        watcher.join()
        if sys.version_info[0] >= 3:
            out = out.decode("utf-8", "replace")
            err = err.decode("utf-8", "replace")
        if timed_out:
            self.fail(
                "command runner was still blocked after %ss. "
                "Both pipes have to be drained together, and the child has to be reaped.\n"
                "stdout=%r\nstderr=%r"
                % (_COLLECT_TIMEOUT_SECONDS, out[-800:], err[-800:])
            )
        self.assertEqual(
            proc.returncode,
            0,
            "runner exited %s\nstdout=%s\nstderr=%s" % (proc.returncode, out, err),
        )
        return out, err

    def _run_spy(self, spy_source):
        spy_path = self._temp(".spy", spy_source)
        generated = preprocess_file(spy_path, is_root_script=False)
        with open(generated, "r") as handle:
            generated_source = handle.read()
        self.assertIn("exe(", generated_source)
        self._run_python([generated])
        return generated_source

    def _read_summary(self, path):
        with open(path, "r") as handle:
            rows = {}
            for line in handle:
                key, value = line.rstrip("\n").split("=", 1)
                rows[key] = value
        return rows

    def _summary_writer(self, summary_path, expression):
        return (
            "open(%r, 'w').write(%s)\n" % (summary_path, expression)
        )

    def test_grave_accent_stderr_burst_before_stdout_returns_both_streams(self):
        """Attack path: stderr fills the pipe while stdout is still open.

        A grave-accent command must return the child's stdout and the full
        stderr payload. Sequential readlines on stdout never reaches stderr,
        so this times out until both pipes are read together.
        """
        child = self._temp(
            ".py",
            _write_all_child(
                "write_all(2, b'e' * %d)\nwrite_all(1, b'OUT\\n')\n" % PIPE_BURST
            ),
        )
        summary = self._temp(".txt", "")
        command = sys.executable + " " + child
        report = (
            "'RC=%s\\nSTDOUT=%s\\nSTDERR_LEN=%d\\nSTDERR_OK=%s\\n' % ("
            "result.returncode, result.stdout, len(result.stderr), "
            "result.stderr == ('e' * " + str(PIPE_BURST) + "))"
        )
        spy = "result = `" + command + "\n" + self._summary_writer(summary, report)
        generated = self._run_spy(spy)
        self.assertIn(command, generated)
        rows = self._read_summary(summary)
        self.assertEqual(rows["RC"], "0")
        self.assertEqual(rows["STDOUT"], "OUT")
        self.assertEqual(rows["STDERR_LEN"], str(PIPE_BURST))
        self.assertEqual(rows["STDERR_OK"], "True")

    def test_exe_both_pipes_exceeding_capacity_returns_both_streams(self):
        """Stdout filled first, then stderr, each past the pipe capacity.

        Reading either pipe to completion before the other still deadlocks:
        the shell keeps the unread pipe open until the child exits, and the
        child cannot exit while that write is blocked. Swapping the two
        readlines calls does not make this pass.
        """
        child = self._temp(
            ".py",
            _write_all_child(
                "write_all(1, b'o' * %d)\nwrite_all(2, b'e' * %d)\n"
                % (PIPE_BURST, PIPE_BURST)
            ),
        )
        summary = self._temp(".txt", "")
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from shellpython import core\n"
            "result = core.exe(%r, '')\n"
            "open(%r, 'w').write('RC=%%s\\nSTDOUT_OK=%%s\\nSTDERR_OK=%%s\\n' %% (\n"
            "    result.returncode,\n"
            "    result.stdout == ('o' * %d),\n"
            "    result.stderr == ('e' * %d)))\n"
            % (REPO_ROOT, sys.executable + " " + child, summary, PIPE_BURST, PIPE_BURST)
        )
        driver = self._temp(".py", script)
        self._run_python([driver])
        rows = self._read_summary(summary)
        self.assertEqual(rows["RC"], "0")
        self.assertEqual(rows["STDOUT_OK"], "True")
        self.assertEqual(rows["STDERR_OK"], "True")

    def test_exe_stderr_burst_after_child_closes_stdout_returns_both_streams(self):
        """Closing the command's own stdout does not unblock the runner.

        shell=True leaves the shell holding the pipe until the child exits.
        A full stderr write still stalls readlines on stdout.
        """
        child = self._temp(
            ".py",
            _write_all_child(
                "write_all(1, b'OUT\\n')\nos.close(1)\nwrite_all(2, b'e' * %d)\n"
                % PIPE_BURST
            ),
        )
        summary = self._temp(".txt", "")
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from shellpython import core\n"
            "result = core.exe(%r, 'pen')\n"
            "open(%r, 'w').write('RC=%%s\\nSTDOUT=%%s\\nSTDERR_OK=%%s\\n' %% (\n"
            "    result.returncode, result.stdout, result.stderr == ('e' * %d)))\n"
            % (REPO_ROOT, sys.executable + " " + child, summary, PIPE_BURST)
        )
        driver = self._temp(".py", script)
        self._run_python([driver])
        rows = self._read_summary(summary)
        self.assertEqual(rows["RC"], "0")
        self.assertEqual(rows["STDOUT"], "OUT")
        self.assertEqual(rows["STDERR_OK"], "True")

    def test_interactive_grave_accent_iteration_survives_stderr_burst(self):
        """for line in result reads stdout to EOF and never drains stderr.

        The same pipe stall happens for an interactive grave-accent command.
        Iteration must finish, stderr must still be readable in full, and
        returncode must be collected.
        """
        child = self._temp(
            ".py",
            _write_all_child(
                "write_all(2, b'e' * %d)\nwrite_all(1, b'OUT\\n')\n" % PIPE_BURST
            ),
        )
        summary = self._temp(".txt", "")
        command = sys.executable + " " + child
        report = (
            "'RC=%s\\nSTDOUT=%s\\nSTDERR_OK=%s\\n' % ("
            "result.returncode, '\\n'.join(out_lines), "
            "err_text == ('e' * " + str(PIPE_BURST) + "))"
        )
        spy = (
            "result = i`" + command + "\n"
            "out_lines = []\n"
            "for line in result:\n"
            "    out_lines.append(line)\n"
            "err_lines = []\n"
            "for line in result.stderr:\n"
            "    err_lines.append(line)\n"
            "err_text = ''.join(err_lines)\n"
            + self._summary_writer(summary, report)
        )
        generated = self._run_spy(spy)
        self.assertIn("'" + command + "'", generated.replace("\\'", "'"))
        self.assertIn(",'i')", generated)
        rows = self._read_summary(summary)
        self.assertEqual(rows["RC"], "0")
        self.assertEqual(rows["STDOUT"], "OUT")
        self.assertEqual(rows["STDERR_OK"], "True")

    def test_stdout_burst_with_small_stderr_still_returns(self):
        """A large stdout and a short stderr is the order the current reader handles.

        A fix that only swaps the two reads would hang here. Concurrent reads
        still return both streams.
        """
        child = self._temp(
            ".py",
            _write_all_child(
                "write_all(1, b'o' * %d)\nwrite_all(2, b'ERR\\n')\n" % PIPE_BURST
            ),
        )
        summary = self._temp(".txt", "")
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from shellpython import core\n"
            "result = core.exe(%r, '')\n"
            "open(%r, 'w').write('RC=%%s\\nSTDOUT_OK=%%s\\nSTDERR=%%s\\n' %% (\n"
            "    result.returncode, result.stdout == ('o' * %d), result.stderr))\n"
            % (REPO_ROOT, sys.executable + " " + child, summary, PIPE_BURST)
        )
        driver = self._temp(".py", script)
        self._run_python([driver])
        rows = self._read_summary(summary)
        self.assertEqual(rows["RC"], "0")
        self.assertEqual(rows["STDOUT_OK"], "True")
        self.assertEqual(rows["STDERR"], "ERR")
