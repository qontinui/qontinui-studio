"""Exclusive per-site locks, so two publishes/unpublishes of one site never
interleave. A lock is an OS file lock on ``<publish_dir>/.studio.lock``: it is
released by the kernel if the holder dies, so a crash never leaves a stale lock.

A publish dir inside a repository CHECKOUT (a ``studio compile`` target) must
carry nothing but what the studio publishes, so its lock lives in this
machine's temp directory instead, keyed by the dir's resolved path
(:func:`lock_path`). Only processes on one machine can share a checkout, so a
machine-wide lock is the scope that matters.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import tempfile
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


def lock_path(directory: Path, *, in_checkout: bool = False) -> Path:
    """The lock file for ``directory``: inside it, or — for a directory inside
    a repository checkout — in this machine's temp directory."""
    if not in_checkout:
        return directory / LOCK_NAME
    key = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / "qontinui-studio-locks" / f"{key}.lock"


@contextlib.contextmanager
def file_locks(lock_files: list[Path]) -> Iterator[None]:
    """Hold every given lock file, acquired in a fixed (sorted) order so two
    callers locking overlapping sets cannot deadlock."""
    with contextlib.ExitStack() as stack:
        for path in sorted({p.parent.resolve() / p.name for p in lock_files}):
            stack.enter_context(_locked(path))
        yield


def site_locks(publish_dirs: list[Path]) -> contextlib.AbstractContextManager[None]:
    """Hold the lock of every given publish dir (each inside its dir)."""
    return file_locks([d / LOCK_NAME for d in publish_dirs])
