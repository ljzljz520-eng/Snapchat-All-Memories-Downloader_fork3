import asyncio
import hashlib
import os
import stat
from pathlib import Path

import exif
import pytest

from memories_dl.index import IndexStore
from memories_dl.models import Enrichment, Memory, RawSource
from memories_dl.pipeline import download_all

from conftest import JPEG_BYTES, MP4_BYTES, make_raw_record


def run(memories, out, cdn, **kw):
    return asyncio.run(
        download_all(
            memories, out, max_concurrent=2, transport=cdn.transport(), **kw
        )
    )


def jpeg_memory(location="37.7749, -122.4194", date="2020-01-02 03:04:05 UTC"):
    return Memory(**make_raw_record(date=date, location=location))


def mp4_memory(date="2020-01-03 10:00:00 UTC"):
    return Memory(**make_raw_record(date=date, name="clip.mp4"))


def test_happy_jpeg_full_contract(tmp_path, cdn):
    memory = jpeg_memory()
    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1 and stats.failed == 0
    assert cdn.posts == 1 and cdn.gets == 1

    index = IndexStore(tmp_path).load()
    entry = index.get(memory.fingerprint)
    assert entry.enrichment == Enrichment.SUCCESS
    assert entry.raw_source == RawSource.CDN
    assert entry.raw_digest == hashlib.sha256(JPEG_BYTES).hexdigest()

    raw_file = tmp_path / entry.raw_path
    derived = tmp_path / entry.derived_path
    assert raw_file.read_bytes() == JPEG_BYTES
    assert stat.S_IMODE(raw_file.stat().st_mode) == 0o444
    assert raw_file.resolve() != derived.resolve()

    check = exif.Image(derived.read_bytes())
    assert check.datetime_original == "2020:01:02 03:04:05"
    assert check.gps_latitude_ref == "N" and check.gps_longitude_ref == "W"

    # Rerun: fully local, zero CDN traffic.
    stats2 = run([memory], tmp_path, cdn)
    assert stats2.skipped == 1
    assert cdn.posts == 1 and cdn.gets == 1


def test_no_exif_publishes_raw_bytes(tmp_path, cdn):
    memory = jpeg_memory(location="")
    stats = run([memory], tmp_path, cdn, add_exif=False)
    assert stats.downloaded == 1
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert entry.enrichment == Enrichment.NOT_REQUIRED
    derived = (tmp_path / entry.derived_path).read_bytes()
    assert hashlib.sha256(derived).hexdigest() == entry.raw_digest


def test_mp4_passthrough(tmp_path, cdn):
    memory = mp4_memory()
    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert entry.enrichment == Enrichment.NOT_REQUIRED
    assert (tmp_path / entry.derived_path).read_bytes() == MP4_BYTES


def test_exif_failure_then_offline_retry(tmp_path, cdn, monkeypatch):
    memory = jpeg_memory()

    monkeypatch.setenv("MEMORIES_FAULT", "exif_parse:error")
    stats1 = run([memory], tmp_path, cdn)
    assert stats1.downloaded == 0
    assert stats1.enrich_failed == 1
    assert cdn.gets == 1

    index = IndexStore(tmp_path).load()
    entry = index.get(memory.fingerprint)
    assert entry.enrichment == Enrichment.FAILED
    raw = tmp_path / entry.raw_path
    assert raw.read_bytes() == JPEG_BYTES
    assert entry.derived_path is None
    assert not (tmp_path / "2020-01-02_03-04-05.jpg").exists()

    monkeypatch.delenv("MEMORIES_FAULT")
    stats2 = run([memory], tmp_path, cdn)
    assert stats2.downloaded == 1
    assert cdn.posts == 1 and cdn.gets == 1  # no second CDN GET
    entry2 = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert entry2.enrichment == Enrichment.SUCCESS
    assert (tmp_path / entry2.derived_path).exists()


def test_derived_deleted_is_rebuilt_offline(tmp_path, cdn):
    memory = jpeg_memory(location="")
    run([memory], tmp_path, cdn, add_exif=False)
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    (tmp_path / entry.derived_path).unlink()

    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1 and cdn.gets == 1
    entry2 = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert (tmp_path / entry2.derived_path).exists()


def test_missing_raw_triggers_redownload(tmp_path, cdn):
    memory = jpeg_memory(location="")
    run([memory], tmp_path, cdn, add_exif=False)
    entry = IndexStore(tmp_path).load().get(memory.fingerprint)
    raw = tmp_path / entry.raw_path
    os.chmod(raw, stat.S_IWUSR | stat.S_IRUSR)
    raw.unlink()

    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1 and cdn.gets == 2
    assert IndexStore(tmp_path).load().get(memory.fingerprint).enrichment in (
        Enrichment.SUCCESS,
        Enrichment.NOT_REQUIRED,
    )


def test_network_failure_writes_nothing(tmp_path, cdn):
    cdn.fail_paths.add("/pixel.jpg")
    stats = run([jpeg_memory()], tmp_path, cdn)
    assert stats.failed == 1
    assert stats.downloaded == 0 and stats.enrich_failed == 0
    assert not (tmp_path / ".memories").exists() or not (
        tmp_path / ".memories" / "index.json"
    ).exists()
    assert not (tmp_path / "2020-01-02_03-04-05.jpg").exists()
