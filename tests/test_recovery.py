import asyncio
import json

import pytest

from memories_dl import layout, legacy as legacy_mod
from memories_dl.index import IndexStore
from memories_dl.models import Enrichment, Memory, RawSource
from memories_dl.pipeline import download_all
from memories_dl.recovery import run_recovery

from conftest import JPEG_BYTES, make_raw_record


def recover(out, memories=()):
    index = IndexStore(out).load()
    by_fp = {m.fingerprint: m for m in memories}
    asyncio.run(run_recovery(out, index, by_fp))
    return IndexStore(out).load()


def run(memories, out, cdn, **kw):
    return asyncio.run(
        download_all(
            memories, out, max_concurrent=2, transport=cdn.transport(), **kw
        )
    )


def test_cleanup_only_removes_recognizable_temps(tmp_path):
    (tmp_path / ".memories").mkdir()
    shard = tmp_path / ".memories" / "raw" / "ab"
    shard.mkdir(parents=True)
    h32 = "a" * 32
    temps = [
        shard / f".rawtmp-{h32}",
        tmp_path / ".memories" / f".index.json.tmp-{h32}",
        tmp_path / f".2020-01-02_03-04-05.jpg.tmp-{h32}",
    ]
    for t in temps:
        t.write_bytes(b"partial")
    user_file = tmp_path / "2019-12-31_23-59-59.jpg"
    user_file.write_bytes(JPEG_BYTES)
    note = tmp_path / ".memories" / "keepme"
    note.write_bytes(b"x")

    # Negative samples: dotfiles containing ".tmp-"/".rawtmp-" substrings but
    # NOT in the tool's exact temp shape must survive recovery.
    foreign = [
        tmp_path / ".vacation.photo.tmp-backup.jpg",
        tmp_path / ".my.tmp-999",
        shard / ".rawtmp-short",
        tmp_path / ".memories" / ".index.json.tmp-xyz",
    ]
    for f in foreign:
        f.write_bytes(b"user data")

    index = IndexStore(tmp_path).load()
    removed = asyncio.run(run_recovery(tmp_path, index, {}))
    assert len(removed) == 3
    assert all(not t.exists() for t in temps)
    assert user_file.exists() and note.exists()
    assert all(f.exists() for f in foreign)


def test_derived_write_enospc_then_offline_rerun(tmp_path, cdn, monkeypatch):
    memory = Memory(**make_raw_record(location="37.7749, -122.4194"))
    target = tmp_path / "2020-01-02_03-04-05.jpg"

    monkeypatch.setenv("MEMORIES_FAULT", "derived_write:enospc")
    stats1 = run([memory], tmp_path, cdn)
    assert stats1.enrich_failed == 1 and stats1.downloaded == 0
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp-*")) == []
    gets_after_first = cdn.gets

    # Recovery + rerun: raw is present, so the CDN must not be touched again.
    monkeypatch.delenv("MEMORIES_FAULT")
    recover(tmp_path, [memory])
    stats2 = run([memory], tmp_path, cdn)
    assert stats2.downloaded == 1
    assert cdn.gets == gets_after_first
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert entry.enrichment == Enrichment.SUCCESS


def test_reconcile_published_but_index_stale(tmp_path, cdn):
    """Crash between atomic derived publish and the index update."""
    memory = Memory(**make_raw_record(location="37.7749, -122.4194"))
    run([memory], tmp_path, cdn)
    gets = cdn.gets

    # Rewind index to pending while keeping the published derived artifact.
    index = IndexStore(tmp_path).load()
    fp = memory.fingerprint
    stale = index.get(fp).model_copy(update={"enrichment": Enrichment.PENDING})
    asyncio.run(index.upsert(stale))

    fixed = recover(tmp_path, [memory]).get(fp)
    assert fixed.enrichment == Enrichment.SUCCESS

    stats = run([memory], tmp_path, cdn)
    assert stats.skipped == 1
    assert cdn.gets == gets  # reconciliation needed no network


def test_reconcile_success_with_missing_derived(tmp_path, cdn):
    memory = Memory(**make_raw_record(location=""))
    run([memory], tmp_path, cdn, add_exif=False)
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    (tmp_path / entry.derived_path).unlink()

    fixed = recover(tmp_path, [memory]).get(memory.fingerprint)
    assert fixed.enrichment == Enrichment.PENDING
    stats = run([memory], tmp_path, cdn, add_exif=False)
    assert stats.downloaded == 1 and cdn.gets == 1


def test_reconcile_invalid_published_derived_is_rebuilt(tmp_path, cdn):
    memory = Memory(**make_raw_record(location="37.7749, -122.4194"))
    run([memory], tmp_path, cdn)
    gets = cdn.gets

    index = IndexStore(tmp_path).load()
    fp = memory.fingerprint
    target = tmp_path / index.get(fp).derived_path
    stale = index.get(fp).model_copy(update={"enrichment": Enrichment.PENDING})
    asyncio.run(index.upsert(stale))
    target.write_bytes(b"definitely not a jpeg")

    fixed = recover(tmp_path, [memory]).get(fp)
    assert fixed.enrichment == Enrichment.PENDING
    assert fixed.derived_path is None

    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1 and cdn.gets == gets


def test_reconcile_missing_raw_clears_claims(tmp_path, cdn):
    import os
    import stat

    memory = Memory(**make_raw_record(location=""))
    run([memory], tmp_path, cdn, add_exif=False)
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    raw = tmp_path / entry.raw_path
    os.chmod(raw, stat.S_IWUSR | stat.S_IRUSR)
    raw.unlink()

    fixed = recover(tmp_path, [memory]).get(memory.fingerprint)
    assert fixed.raw_path is None and fixed.raw_source is None
    assert fixed.enrichment == Enrichment.PENDING
