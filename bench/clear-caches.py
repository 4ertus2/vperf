#!/usr/bin/env python3
"""Evict files from the page cache, without root.

    bench/clear-caches.py                    # $DATA_DIR, else ~/data
    bench/clear-caches.py /path /other/dir   # anything else, dirs included

Why not the obvious ``echo 3 > /proc/sys/vm/drop_caches``: that file is 0600
root-only, and a setuid/setgid bit on a *script* is ignored by the kernel
(only the interpreter is exec'd, and bash drops it anyway - the same trap that
makes ``setcap /usr/bin/perf`` a no-op on Debian). So the blunt drop needs
sudo and a password, and cannot run unattended. posix_fadvise() needs
neither: on a clean file POSIX_FADV_DONTNEED discards that file's pages. It
is also the more honest drop for a benchmark - only the dataset leaves the
cache, not every binary the box has touched since boot.

Every file is then measured with mincore(), so the run that follows is known
to be cold rather than assumed to be. That check is the point: the whole
difference between a cache-resident and a disk-bound ClickBench query is this,
and it is invisible in the report unless the eviction is verified.

What it cannot do: evict a page that is still dirty (a file being written),
or a page an open mmap still holds.
"""

from __future__ import annotations

import argparse
import ctypes
import mmap
import os
import sys

DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", 4)
PROT_READ, MAP_PRIVATE = 1, 0x02
MAP_FAILED = ctypes.c_void_p(-1).value

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_long]
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def expand(paths: list[str]):
    """Every regular file under *paths*, directories walked, order stable."""
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                for name in sorted(names):
                    yield os.path.join(root, name)
        else:
            yield path


def residency(path: str) -> tuple[int, int] | None:
    """(resident_bytes, size_bytes) per mincore, or None if unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        if not size:
            return (0, 0)
        addr = _libc.mmap(None, size, PROT_READ, MAP_PRIVATE, fd, 0)
        if addr is None or addr == MAP_FAILED:
            return None
        try:
            pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
            vector = (ctypes.c_ubyte * pages)()
            if _libc.mincore(ctypes.c_void_p(addr), ctypes.c_size_t(size), vector):
                return None
            return (sum(vector) * mmap.PAGESIZE, size)
        finally:
            _libc.munmap(ctypes.c_void_p(addr), size)
    except OSError:
        return None
    finally:
        os.close(fd)


def evict(path: str) -> str | None:
    """Drop *path* from the cache; the error text if it could not be read."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        return exc.strerror
    try:
        os.posix_fadvise(fd, 0, 0, DONTNEED)
        return None
    finally:
        os.close(fd)


def human(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evict files from the page cache, without root.",
        epilog="with no paths, $DATA_DIR is used, else ~/data",
    )
    parser.add_argument("paths", nargs="*", help="files or directories")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only report the total, not each file")
    args = parser.parse_args(argv)

    targets = args.paths or [os.environ.get("DATA_DIR")
                             or os.path.expanduser("~/data")]
    missing: list[str] = []
    skipped: list[tuple[str, str]] = []
    freed = 0

    for path in expand(targets):
        if not os.path.exists(path):
            missing.append(path)
            continue
        before = residency(path)
        error = evict(path)
        if error:
            skipped.append((path, error))
            continue
        after = residency(path)
        if after is None:
            continue
        freed += max(0, (before or (0, 0))[0] - after[0])
        if args.quiet or before is None or not after[1]:
            continue
        print(f"  {path}: {human(before[0])} of {human(before[1])} resident"
              f" -> {after[0] * 100.0 / after[1]:.1f}%")

    if freed:
        print(f"page cache: {human(freed)} released")
    for path in missing:
        print(f"error: {path}: no such file or directory", file=sys.stderr)
    for path, error in skipped:
        print(f"skipped {path}: {error}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
