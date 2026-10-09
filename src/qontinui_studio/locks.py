"""Exclusive per-site locks, so two publishes/unpublishes of one site never
interleave. A lock is an OS file lock on ``<publish_dir>/.studio.lock``: it is
released by the kernel if the holder dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

LOCK_NAME = ".studio.lock"


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            # LK_LOCK retries for ~10 s then raises; loop for a blocking lock.
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def site_locks(publish_dirs: list[Path]) -> Iterator[None]:
    """Hold the lock of every given publish dir, acquired in a fixed (sorted)
    order so two callers locking overlapping sets cannot deadlock."""
    with contextlib.ExitStack() as stack:
        for d in sorted({p.resolve() for p in publish_dirs}):
            stack.enter_context(_locked(d / LOCK_NAME))
        yield
