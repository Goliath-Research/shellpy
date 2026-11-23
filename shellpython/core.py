from __future__ import print_function
import numbers
import os
import sys
import subprocess
import threading
from os import environ as env
from shellpython import config

_colorama_intialized = False
_colorama_available = True
try:
    import colorama
    from colorama import Fore, Style
except ImportError:
    _colorama_available = False


def _is_colorama_enabled():
    return _colorama_available and config.COLORAMA_ENABLED


def _print_stdout(text):
    print(text)


def _print_stderr(text):
    print(text, file=sys.stderr)

# print all stdout of executed command
_PARAM_PRINT_STDOUT = 'p'

# print all stderr of executed command
_PARAM_PRINT_STDERR = 'e'

# runs command in interactive mode when user can read output line by line and send to stdin
_PARAM_INTERACTIVE = 'i'

# no throw mode. With this parameter user explicitly says that NonZeroReturnCodeError must not be thrown for this
# specific command. It may be useful if for some reason this command does not return 0 even for successful run
_PARAM_NO_THROW = 'n'

_READ_CHUNK_BYTES = 8192


def exe(cmd, params):
    """This function runs after preprocessing of code. It actually executes commands with subprocess

    :param cmd: command to be executed with subprocess
    :param params: parameters passed before ` character, i.e. p`echo 1 which means print result of execution
    :return: result of execution. It may be either Result or InteractiveResult
    """

    global _colorama_intialized
    if _is_colorama_enabled() and not _colorama_intialized:
        _colorama_intialized = True
        colorama.init()

    if config.PRINT_ALL_COMMANDS:
        if _is_colorama_enabled():
            _print_stdout(Fore.GREEN + '>>> ' + cmd + Style.RESET_ALL)
        else:
            _print_stdout('>>> ' + cmd)

    if _is_param_set(params, _PARAM_INTERACTIVE):
        return _create_interactive_result(cmd, params)
    else:
        return _create_result(cmd, params)


def _is_param_set(params, param):
    return True if params.find(param) != -1 else False


class ShellpyError(Exception):
    """Base error for shell python
    """
    pass


class NonZeroReturnCodeError(ShellpyError):
    """This is thrown when the executed command does not return 0
    """
    def __init__(self, cmd, result):
        self.cmd = cmd
        self.result = result

    def __str__(self):
        if _is_colorama_enabled():
            return 'Command {red}\'{cmd}\'{end} failed with error code {code}, stderr output is {red}{stderr}{end}'\
                .format(red=Fore.RED, end=Style.RESET_ALL, cmd=self.cmd, code=self.result.returncode,
                        stderr=self.result.stderr)
        else:
            return 'Command \'{cmd}\' failed with error code {code}, stderr output is {stderr}'.format(
                    cmd=self.cmd, code=self.result.returncode, stderr=self.result.stderr)


class OutputLimitExceededError(ShellpyError):
    """This is thrown when a command writes more than config.MAX_CAPTURED_STREAM_BYTES bytes to a captured stream,
    or a line longer than that in interactive mode. The command is killed, and result holds the output kept up to
    the limit. It is thrown regardless of the no throw parameter
    """
    def __init__(self, cmd, result, stream_name, limit):
        self.cmd = cmd
        self.result = result
        self.stream_name = stream_name
        self.limit = limit

    def __str__(self):
        return 'Command \'{cmd}\' wrote more than {limit} bytes to {stream} and was terminated'.format(
            cmd=self.cmd, limit=self.limit, stream=self.stream_name)


def _max_captured_stream_bytes():
    limit = config.MAX_CAPTURED_STREAM_BYTES
    if isinstance(limit, bool) or not isinstance(limit, numbers.Integral) or limit < 1:
        raise ValueError('config.MAX_CAPTURED_STREAM_BYTES must be a positive integer, got {0!r}'.format(limit))
    return limit


def _kill_process(process):
    try:
        process.kill()
    except OSError:
        pass


class Stream:
    def __init__(self, file, encoding, print_out_stream=False, color=None, name=None, on_limit_exceeded=None):
        self._file = file
        self._encoding = encoding
        self._print_out_stream = print_out_stream
        self._color = color
        self._name = name
        self._on_limit_exceeded = on_limit_exceeded

    def __iter__(self):
        return self

    def next(self):
        return self.sreadline()

    __next__ = next

    def sreadline(self):
        limit = _max_captured_stream_bytes()
        line = self._file.readline(limit + 1)
        if len(line) > limit:
            self._file.close()
            if self._on_limit_exceeded is not None:
                self._on_limit_exceeded(self._name, limit)
            raise OutputLimitExceededError(None, None, self._name, limit)

        if sys.version_info[0] == 3:
            line = line.decode(self._encoding)

        if line == '':
            raise StopIteration
        else:
            line = line.rstrip(os.linesep)
            if self._print_out_stream:
                if self._color is None:
                    _print_stdout(line)
                else:
                    _print_stdout(self._color + line + Style.RESET_ALL)

            return line

    def swriteline(self, text):
        text_with_linesep = text + os.linesep
        if sys.version_info[0] == 3:
            text_with_linesep = text_with_linesep.encode(self._encoding)

        self._file.write(text_with_linesep)
        self._file.flush()


class InteractiveResult:
    """Result of a shell command execution.

    To get the result as string use str(Result)
    To get lines use the Result.lines field
    You can also iterate over lines of result like this: for line in Result:
    You can compare two results that will mean compare of result strings
    """
    def __init__(self, process, params, cmd=None):
        self._process = process
        self._params = params
        self._cmd = cmd
        self.stdin = Stream(process.stdin, sys.stdin.encoding)

        print_stdout = _is_param_set(params, _PARAM_PRINT_STDOUT) or config.PRINT_STDOUT_ALWAYS
        self.stdout = Stream(process.stdout, sys.stdout.encoding, print_stdout,
                             name='stdout', on_limit_exceeded=self._on_limit_exceeded)

        print_stderr = _is_param_set(params, _PARAM_PRINT_STDERR) or config.PRINT_STDERR_ALWAYS
        color = None if not _is_colorama_enabled() else Fore.RED
        self.stderr = Stream(process.stderr, sys.stderr.encoding, print_stderr, color,
                             name='stderr', on_limit_exceeded=self._on_limit_exceeded)

    def _on_limit_exceeded(self, stream_name, limit):
        _kill_process(self._process)
        raise OutputLimitExceededError(self._cmd, self, stream_name, limit)

    def sreadline(self):
        return self.stdout.sreadline()

    def swriteline(self, text):
        self.stdin.swriteline(text)

    @property
    def returncode(self):
        self._process.wait()
        return self._process.returncode

    def __iter__(self):
        return iter(self.stdout)

    def __bool__(self):
        return self.returncode == 0

    __nonzero__ = __bool__


class Result:
    """Result of a shell command execution.

    To get the result stdout as string use str(Result) or Result.stdout or print Result
    To get output of stderr use Result.stderr()

    You can also iterate over lines of stdout like this: for line in Result:

    You can access underlying lines of result streams as Result.stdout_lines Result.stderr_lines.
    E.g. line_two = Result.stdout_lines[2]

    You can also compare two results that will mean compare of result stdouts
    """
    def __init__(self):
        self._stdout_lines = []
        self._stderr_lines = []
        self.returncode = None

    @property
    def stdout(self):
        """Stdout of Result as text
        """
        return os.linesep.join(self._stdout_lines)

    @property
    def stderr(self):
        """Stderr of Result as text
        """
        return os.linesep.join(self._stderr_lines)

    @property
    def stdout_lines(self):
        """List of all lines from stdout
        """
        return self._stdout_lines

    @property
    def stderr_lines(self):
        """List of all lines from stderr
        """
        return self._stderr_lines

    def _add_stdout_line(self, line):
        line = line.rstrip(os.linesep)
        self._stdout_lines.append(line)

    def _add_stderr_line(self, line):
        line = line.rstrip(os.linesep)
        self._stderr_lines.append(line)

    def __str__(self):
        return self.stdout

    def __iter__(self):
        return iter(self._stdout_lines)

    def __eq__(self, other):
        return self.__str__() == other.__str__()

    def __bool__(self):
        return self.returncode == 0

    __nonzero__ = __bool__


def _read_capped(stream, limit, chunks, on_limit_exceeded):
    """Reads stream until EOF into chunks, keeping at most limit bytes. Reading stops, the stream is closed and
    on_limit_exceeded is called as soon as more than limit bytes arrive
    """
    fd = stream.fileno()
    total = 0
    try:
        while True:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, limit + 1 - total))
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                chunks.append(chunk[:len(chunk) - (total - limit)])
                on_limit_exceeded()
                return
            chunks.append(chunk)
    finally:
        stream.close()


def _decode_lines(chunks, encoding, truncated):
    data = b''.join(chunks)
    del chunks[:]
    if sys.version_info[0] == 3:
        data = data.decode(encoding, 'replace' if truncated else 'strict')

    lines = data.split('\n')
    if lines[-1] == '':
        lines.pop()
    return lines


def _create_result(cmd, params):
    limit = _max_captured_stream_bytes()

    p = subprocess.Popen(cmd,
                         shell=True,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         env=os.environ)

    result = Result()

    exceeded_streams = []
    exceeded_lock = threading.Lock()

    def on_limit_exceeded(stream_name):
        with exceeded_lock:
            exceeded_streams.append(stream_name)
        _kill_process(p)

    # both pipes are drained at the same time, otherwise a command filling one pipe while the other is read blocks
    stdout_chunks = []
    stderr_chunks = []
    readers = [
        threading.Thread(target=_read_capped,
                         args=(p.stdout, limit, stdout_chunks, lambda: on_limit_exceeded('stdout'))),
        threading.Thread(target=_read_capped,
                         args=(p.stderr, limit, stderr_chunks, lambda: on_limit_exceeded('stderr'))),
    ]
    for reader in readers:
        reader.daemon = True
        reader.start()
    for reader in readers:
        reader.join()

    p.wait()
    result.returncode = p.returncode

    for line in _decode_lines(stdout_chunks, sys.stdout.encoding, 'stdout' in exceeded_streams):
        result._add_stdout_line(line)

    for line in _decode_lines(stderr_chunks, sys.stderr.encoding, 'stderr' in exceeded_streams):
        result._add_stderr_line(line)

    if exceeded_streams:
        raise OutputLimitExceededError(cmd, result, exceeded_streams[0], limit)

    if (_is_param_set(params, _PARAM_PRINT_STDOUT) or config.PRINT_STDOUT_ALWAYS) and len(result.stdout) > 0:
        _print_stdout(result.stdout)

    if (_is_param_set(params, _PARAM_PRINT_STDERR) or config.PRINT_STDERR_ALWAYS) and len(result.stderr) > 0:
        if _is_colorama_enabled():
            _print_stderr(Fore.RED + result.stderr + Style.RESET_ALL)
        else:
            _print_stderr(result.stderr)

    if p.returncode != 0 and not _is_param_set(params, _PARAM_NO_THROW):
        raise NonZeroReturnCodeError(cmd, result)

    return result


def _create_interactive_result(cmd, params):
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)

    result = InteractiveResult(p, params, cmd)

    return result
