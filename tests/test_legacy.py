import asyncio

from memories_dl.index import IndexStore
from memories_dl.legacy import migrate_legacy, scan_legacy
from memories_dl.models import Enrichment, Memory, RawSource
from memories_dl.pipeline import download_all

from conftest import JPEG_BYTES, MP4_BYTES, make_raw_record


def run(memories, out, cdn, **kw):
    return asyncio.run(
        download_all(
            memories, out, max_concurrent=2, transport=cdn.transport(), **kw
        )
    )


def test_default_run_leaves_legacy_untracked(tmp_path, cdn, capsys):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    mp4 = tmp_path / "2020-01-03_10-00-00.mp4"
    jpg.write_bytes(JPEG_BYTES)
    mp4.write_bytes(MP4_BYTES)
    inodes = {p: p.stat().st_ino for p in (jpg, mp4)}

    memories = [
        Memory(**make_raw_record(location="37.7749, -122.4194")),
        Memory(**make_raw_record(date="2020-01-03 10:00:00 UTC", name="clip.mp4")),
    ]
    stats = run(memories, tmp_path, cdn)
    assert stats.legacy == 2
    assert cdn.posts == 0 and cdn.gets == 0

    assert not (tmp_path / ".memories" / "index.json").exists()
    raw_root = tmp_path / ".memories" / "raw"
    assert not raw_root.exists() or not any(raw_root.rglob("*"))
    for p, ino in inodes.items():
        assert p.stat().st_ino == ino
    out = capsys.readouterr().out
    assert "--migrate-legacy" in out


def test_scan_excludes_index_tracking(tmp_path):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    jpg.write_bytes(JPEG_BYTES)
    assert {p.name for p in scan_legacy(tmp_path, set())} == {jpg.name}
    assert scan_legacy(tmp_path, {jpg.name}) == []


def _do_migrate(out, memories):
    index = IndexStore(out).load()
    by_stem = {m.filename: m for m in memories}
    migrated = migrate_legacy(out, by_stem, dict(index.entries))
    asyncio.run(index.replace_many(migrated))
    return IndexStore(out).load()


def test_migrate_registers_unknown_in_place(tmp_path):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    orphan = tmp_path / "2019-12-31_23-59-59.mp4"
    jpg.write_bytes(JPEG_BYTES)
    orphan.write_bytes(MP4_BYTES)
    inodes = {p.name: p.stat().st_ino for p in (jpg, orphan)}

    memories = [Memory(**make_raw_record(location="37.7749, -122.4194"))]
    index = _do_migrate(tmp_path, memories)

    matched = index.get(memories[0].fingerprint)
    assert matched is not None
    assert matched.raw_source == RawSource.UNKNOWN
    assert matched.enrichment == Enrichment.UNKNOWN
    assert matched.raw_path is None
    assert matched.derived_path == "2020-01-02_03-04-05.jpg"
    assert matched.raw_digest == __import__("hashlib").sha256(JPEG_BYTES).hexdigest()
    assert jpg.stat().st_ino == inodes[jpg.name]

    orphan_entries = [
        e
        for e in index.entries.values()
        if e.derived_path == "2019-12-31_23-59-59.mp4"
    ]
    assert len(orphan_entries) == 1
    oe = orphan_entries[0]
    assert oe.fingerprint.startswith("legacy:")
    assert oe.raw_source == RawSource.UNKNOWN
    assert orphan.stat().st_ino == inodes[orphan.name]
    # No raw objects are created by migration.
    assert not (tmp_path / ".memories" / "raw").exists()


def test_post_migration_run_treats_as_tracked(tmp_path, cdn):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    jpg.write_bytes(JPEG_BYTES)
    memories = [Memory(**make_raw_record(location="37.7749, -122.4194"))]
    _do_migrate(tmp_path, memories)

    stats = run(memories, tmp_path, cdn)
    assert stats.skipped == 1 and stats.legacy == 0
    assert cdn.posts == 0 and cdn.gets == 0


def test_migrated_row_with_missing_file_stays_untracked_source(tmp_path, cdn):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    jpg.write_bytes(JPEG_BYTES)
    memories = [Memory(**make_raw_record(location="37.7749, -122.4194"))]
    index = _do_migrate(tmp_path, memories)
    fp = memories[0].fingerprint
    assert index.get(fp).raw_source == RawSource.UNKNOWN

    jpg.unlink()  # migrated file vanished

    stats = run(memories, tmp_path, cdn)
    assert stats.legacy == 1 and stats.skipped == 0
    assert cdn.posts == 0 and cdn.gets == 0
    row = IndexStore(tmp_path).load().get(fp)
    assert row.raw_source == RawSource.UNKNOWN
    assert row.enrichment == Enrichment.UNKNOWN

    stats2 = run(memories, tmp_path, cdn, skip_existing=False)
    assert stats2.downloaded == 1 and cdn.gets == 1
    row2 = IndexStore(tmp_path).load().get(fp)
    assert row2.raw_source == RawSource.CDN
    assert row2.enrichment == Enrichment.SUCCESS


def test_force_redownload_replaces_legacy(tmp_path, cdn):
    jpg = tmp_path / "2020-01-02_03-04-05.jpg"
    jpg.write_bytes(JPEG_BYTES)
    old_inode = jpg.stat().st_ino
    memories = [Memory(**make_raw_record(location="37.7749, -122.4194"))]

    stats = run(memories, tmp_path, cdn, skip_existing=False)
    assert stats.downloaded == 1 and cdn.gets == 1
    assert jpg.exists() and jpg.stat().st_ino != old_inode
    entry = IndexStore(tmp_path).load().get(memories[0].fingerprint)
    assert entry.raw_source == RawSource.CDN
    assert entry.enrichment == Enrichment.SUCCESS
