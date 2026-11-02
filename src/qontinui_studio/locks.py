"""Exclusive per-site locks, so two publishes/unpublishes of one site never
interleave. A lock is an OS file lock on ``<publish_dir>/.studio.lock``: it is
released by the kernel if the holder dies, so a crash never leaves a stale lock.

A publish dir inside a repository CHECKOUT (a ``studio compile`` target) must
carry nothing but what the studio publishes, so its lock lives in a PER-USER
directory instead — ``$XDG_RUNTIME_DIR/qontinui-studio-locks``, else
``~/.cache/qontinui-studio/locks`` (mode 0700, never a world-shared temp dir)
— keyed by the dir's resolved path (:func:`lock_path`). Only processes on one
machine can share a checkout, so a machine-local lock is the scope that matters.
"""

from __future__ import annotations

import contextlib
import hashlib
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


def lock_path(directory: Path, *, in_checkout: bool = False) -> Path:
    """The lock file for ``directory``: inside it, or — for a directory inside
    a repository checkout — in this machine's temp directory."""
    if not in_checkout:
        return directory / LOCK_NAME
    key = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:32]
    return user_lock_dir() / f"{key}.lock"


def user_lock_dir() -> Path:
    """This user's own lock directory, created 0700."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = (
        Path(runtime) / "qontinui-studio-locks"
        if runtime and Path(runtime).is_dir()
        else Path.home() / ".cache" / "qontinui-studio" / "locks"
    )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        st = os.stat(base)
        if st.st_uid != os.getuid():
            raise OSError(f"lock directory {base} is not owned by this user; refusing it")
        if st.st_mode & 0o077:
            # Ours, but open to others: close it before trusting it.
            os.chmod(base, 0o700)
            if os.stat(base).st_mode & 0o077:
                raise OSError(f"lock directory {base} is open to other users; refusing it")
    return base


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
