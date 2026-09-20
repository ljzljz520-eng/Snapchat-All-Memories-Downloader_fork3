import errno
import hashlib
import os

import pytest

from memories_dl import layout
from memories_dl.store import RAW_MODE, commit_raw, read_raw, sha256_hex

from conftest import JPEG_BYTES


def test_commit_raw_content_addressed_readonly(tmp_path):
    rel, digest = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    final = tmp_path / rel
    assert digest == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert rel == f".memories/raw/{digest[:2]}/{digest}.jpg"
    assert final.read_bytes() == JPEG_BYTES
    assert (final.stat().st_mode & 0o777) == RAW_MODE
    assert list(layout.raw_root(tmp_path).rglob(".rawtmp-*")) == []


def test_commit_raw_deduplicates_same_bytes(tmp_path):
    rel1, d1 = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    inode1 = (tmp_path / rel1).stat().st_ino
    rel2, d2 = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    inode2 = (tmp_path / rel2).stat().st_ino
    assert (d1, rel1) == (d2, rel2)
    assert inode1 == inode2


def test_read_raw_byte_identical_and_write_denied(tmp_path):
    rel, _ = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    assert read_raw(tmp_path, rel) == JPEG_BYTES
    with pytest.raises(PermissionError):
        open(tmp_path / rel, "wb")


def test_reuse_branch_normalizes_permissions(tmp_path):
    # Pre-create the content-addressed object with overly permissive mode
    # (simulates a crash between rename and chmod in an earlier run).
    digest = sha256_hex(JPEG_BYTES)
    shard = tmp_path / ".memories" / "raw" / digest[:2]
    shard.mkdir(parents=True)
    final = shard / f"{digest}.jpg"
    final.write_bytes(JPEG_BYTES)
    os.chmod(final, 0o644)

    rel, d = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    assert d == digest and rel == f".memories/raw/{digest[:2]}/{digest}.jpg"
    assert (final.stat().st_mode & 0o777) == RAW_MODE


def test_write_all_retries_short_writes(tmp_path, monkeypatch):
    from memories_dl import store as store_mod

    real_write = os.write
    state = {"calls": 0}

    def short_first(fd, data):
        if state["calls"] == 0:
            state["calls"] += 1
            return real_write(fd, bytes(data[:40]))
        return real_write(fd, data)

    monkeypatch.setattr(store_mod.os, "write", short_first)
    target = tmp_path / "f.bin"
    payload = b"x" * 1000
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        store_mod.write_all(fd, payload)
    finally:
        os.close(fd)
    assert target.read_bytes() == payload


def test_raw_commit_enospc_leaves_no_object(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORIES_FAULT", "raw_commit:enospc")
    with pytest.raises(OSError) as exc:
        commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    assert exc.value.errno == errno.ENOSPC
    raw_files = [
        p
        for p in layout.raw_root(tmp_path).rglob("*")
        if p.is_file()
    ]
    assert raw_files == []  # no object, no leftover temp
    assert not (tmp_path / ".memories" / "index.json").exists()
