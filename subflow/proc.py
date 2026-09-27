"""Subprocess helpers that never leave orphaned process trees behind.

Children start in their own process group so a timeout, an exception in the
caller, or server shutdown can kill grandchildren too (for example the FFmpeg
that yt-dlp spawns). Because a separate group no longer receives the
terminal's Ctrl+C, every live child is tracked and `terminate_all()` is
called on shutdown and at interpreter exit.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
from typing import Any, Sequence

_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()


def popen(args: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """Start a tracked child in a new process group; pair with `release()`."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("errors", "replace")
    process = subprocess.Popen(list(args), start_new_session=True, **kwargs)
    with _live_lock:
        _live.add(process)
    return process


def release(process: subprocess.Popen) -> None:
    with _live_lock:
        _live.discard(process)


def kill_tree(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group and reap the child."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def terminate_all() -> None:
    with _live_lock:
        processes = list(_live)
    for process in processes:
        kill_tree(process)


atexit.register(terminate_all)


def run_captured(
    args: Sequence[str],
    *,
    timeout: float | None,
    input: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like `subprocess.run(capture_output=True, text=True)` but tree-safe.

    Raises `subprocess.TimeoutExpired` after killing the process group.
    """
    process = popen(
        args,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except BaseException:
        kill_tree(process)
        try:
            # A descendant that escaped the group could keep the pipes open.
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        release(process)
    return subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr)
