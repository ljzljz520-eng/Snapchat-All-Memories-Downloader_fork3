import asyncio
import json

import pytest

from memories_dl.index import IndexCorruptError, IndexStore
from memories_dl.models import Enrichment, IndexEntry, RawSource

EXPECTED_ROW_KEYS = {
    "date",
    "ext",
    "raw_digest",
    "raw_path",
    "raw_source",
    "derived_path",
    "enrichment",
    "updated_at",
}


def make_entry(fp: str, **over) -> IndexEntry:
    base = dict(
        fingerprint=fp,
        date="2020-01-02 03:04:05",
        ext=".jpg",
        raw_digest="a" * 64,
        raw_path=".memories/raw/aa/a.jpg",
        raw_source=RawSource.CDN,
        derived_path=None,
        enrichment=Enrichment.PENDING,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(over)
    return IndexEntry(**base)


def load_raw(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_upsert_roundtrip_and_minimal_schema(tmp_path):
    store = IndexStore(tmp_path).load()
    asyncio.run(store.upsert(make_entry("fp1")))

    reloaded = IndexStore(tmp_path).load()
    entry = reloaded.get("fp1")
    assert entry.enrichment == Enrichment.PENDING
    assert entry.raw_source == RawSource.CDN

    raw = load_raw(store.path)
    assert raw["version"] == 1
    row = raw["entries"]["fp1"]
    assert set(row.keys()) == EXPECTED_ROW_KEYS
    assert "fingerprint" not in row


def test_concurrent_upserts_lose_nothing(tmp_path):
    store = IndexStore(tmp_path).load()

    async def many():
        await asyncio.gather(
            *[store.upsert(make_entry(f"fp{i}")) for i in range(25)]
        )

    asyncio.run(many())
    assert len(IndexStore(tmp_path).load().entries) == 25


def test_main_file_always_parseable(tmp_path):
    store = IndexStore(tmp_path).load()

    async def run():
        for i in range(10):
            await store.upsert(make_entry(f"fp{i}"))
            json.loads(store.path.read_text(encoding="utf-8"))

    asyncio.run(run())


def test_missing_file_loads_empty(tmp_path):
    assert IndexStore(tmp_path).load().entries == {}


def test_corrupt_main_file_raises(tmp_path):
    store = IndexStore(tmp_path).load()
    asyncio.run(store.upsert(make_entry("fp1")))
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(IndexCorruptError):
        IndexStore(tmp_path).load()


def test_index_temp_uses_fixed_pattern(tmp_path, monkeypatch):
    store = IndexStore(tmp_path).load()
    seen = []
    real_replace = __import__("os").replace

    def spy(src, dst):
        seen.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr("memories_dl.index.os.replace", spy)
    asyncio.run(store.upsert(make_entry("fp1")))
    assert seen[0].name.startswith(".index.json.tmp-")
