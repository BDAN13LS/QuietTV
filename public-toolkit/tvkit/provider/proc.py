"""Bounded subprocess execution with own-tree cleanup on timeout.

Both the SDK and the isolated worker run through this helper. stdout and
stderr are drained through independent pipes into fixed-size buffers while the
child runs, so a runaway producer cannot fill memory or disk. Crossing either
cap kills only the process group/tree launched here. On timeout the same tree
is killed. No other process is signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


class ProcessError(RuntimeError):
    """A finite process refusal; it never carries command text or child output."""


class ProcessTimeout(ProcessError):
    pass


class ProcessLimit(ProcessError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _creation_flags() -> int:
    if os.name == "nt":
        return (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _system_executable(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def _kill_tree(process: subprocess.Popen, pgid: int | None = None) -> None:
    """Kill only this child's own process tree, then reap."""
    try:
        if os.name == "nt":
            subprocess.run(
                [_system_executable("taskkill.exe"), "/F", "/T", "/PID",
                 str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(pgid if pgid is not None else os.getpgid(process.pid),
                      signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


def _stream_reader(pipe, cap: int, out: bytearray, overflow: threading.Event) -> None:
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            room = cap - len(out)
            if room <= 0 or len(chunk) > room:
                out.extend(chunk[:max(room, 0)])
                overflow.set()
                return
            out.extend(chunk)
    except (OSError, ValueError):
        return
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _feed_stdin(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run(argv: list[str] | tuple[str, ...], *, timeout: float,
        max_stdout: int, max_stderr: int, env: dict[str, str] | None = None,
        cwd: str | None = None, input_bytes: bytes | None = None) -> ProcessResult:
    """Run one argv vector; enforce caps while it runs and kill its own tree."""
    if not argv or any(type(item) is not str or not item for item in argv):
        raise ProcessError("a child process command is malformed")
    if (type(timeout) not in (int, float) or timeout <= 0
            or type(max_stdout) is not int or max_stdout <= 0
            or type(max_stderr) is not int or max_stderr <= 0):
        raise ProcessError("a child process bound is malformed")
    try:
        process = subprocess.Popen(
            [str(item) for item in argv],
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=(os.name != "nt"),
            creationflags=_creation_flags() if os.name == "nt" else 0,
        )
    except (OSError, ValueError):
        raise ProcessError("a child process could not be started") from None

    pgid = None
    if os.name != "nt":
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = None
    out_buf, err_buf = bytearray(), bytearray()
    overflow = threading.Event()
    t_out = threading.Thread(target=_stream_reader,
                             args=(process.stdout, max_stdout, out_buf, overflow),
                             daemon=True)
    t_err = threading.Thread(target=_stream_reader,
                             args=(process.stderr, max_stderr, err_buf, overflow),
                             daemon=True)
    t_in = None
    if input_bytes is not None and process.stdin is not None:
        t_in = threading.Thread(target=_feed_stdin,
                                args=(process.stdin, input_bytes), daemon=True)
        t_in.start()
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + float(timeout)
    timed_out = False
    try:
        while True:
            if process.poll() is not None:
                break
            if overflow.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
    finally:
        if overflow.is_set() or timed_out or process.poll() is None:
            _kill_tree(process, pgid)
        if t_in is not None:
            t_in.join(timeout=5)
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        # A descendant can keep a pipe open after the direct child exits. If the
        # cap was crossed, still remove this run's own process tree.
        if overflow.is_set():
            _kill_tree(process, pgid)

    if overflow.is_set():
        raise ProcessLimit("a child process produced more output than the accepted bound")
    if timed_out:
        raise ProcessTimeout("a child process timed out")
    if process.returncode is None:
        raise ProcessError("a child process did not exit")
    return ProcessResult(returncode=process.returncode,
                         stdout=bytes(out_buf), stderr=bytes(err_buf))
