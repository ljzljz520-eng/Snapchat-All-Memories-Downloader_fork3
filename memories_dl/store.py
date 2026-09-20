"""Stage A: content-addressed, read-only raw objects.

Bytes are first written to a recognizable temp file **in the same shard
directory** as the future object, fsynced, then atomically ``os.replace``d
onto the digest-named path and made read-only (0o444). A crash before the
rename leaves only a sweepable ``.rawtmp-*`` file; a crash afterwards leaves a
complete, reusable raw object.
"""

import hashlib
import os
from pathlib import Path

from . import layout
from .faults import inject

RAW_MODE = 0o444


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``, tolerating short writes."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting file")
        view = view[written:]


def commit_raw(output_dir: Path | str, data: bytes, ext: str) -> tuple[str, str]:
    """Write ``data`` as a raw object.

    Returns ``(raw_relpath, digest)``. Objects with identical content are
    deduplicated by digest, so a repeat commit never rewrites anything.
    """
    output_dir = Path(output_dir)
    digest = sha256_hex(data)
    final = layout.raw_object_path(output_dir, digest, ext)
    rel = layout.raw_object_relpath(digest, ext)

    if final.exists():
        # Content-addressed reuse; normalize permissions in case an earlier
        # run crashed between rename and chmod.
        try:
            os.chmod(final, RAW_MODE)
        except FileNotFoundError:
            pass
        return rel, digest

    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = layout.new_raw_tmp(output_dir, digest)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Temp raw is fully durable; faults here must not publish the object.
        inject("raw_write")
        inject("raw_commit")
        os.replace(tmp, final)
        os.chmod(final, RAW_MODE)
        fsync_dir(final.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return rel, digest


def read_raw(output_dir: Path | str, raw_relpath: str) -> bytes:
    """Read a raw object strictly read-only (never opens it for writing)."""
    return (Path(output_dir) / raw_relpath).read_bytes()


def raw_exists(output_dir: Path | str, raw_relpath: str | None) -> bool:
    return bool(raw_relpath) and (Path(output_dir) / raw_relpath).exists()


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
