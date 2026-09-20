import pytest
from pydantic import ValidationError

from memories_dl.models import (
    Enrichment,
    IndexEntry,
    Memory,
    RawSource,
    source_fingerprint,
)

from conftest import make_raw_record


def test_fingerprint_stable_and_order_independent():
    a = make_raw_record()
    b = dict(reversed(list(a.items())))
    assert source_fingerprint(a) == source_fingerprint(b)


def test_fingerprint_changes_with_any_source_field():
    base = make_raw_record()
    assert source_fingerprint(base) != source_fingerprint(
        {**base, "Date": "2021-01-02 03:04:05 UTC"}
    )
    assert source_fingerprint(base) != source_fingerprint(
        {**base, "Download Link": "https://link.test/other"}
    )
    assert source_fingerprint(base) != source_fingerprint(
        {**base, "Location": "1.0, 2.0"}
    )


def test_memory_fingerprint_matches_raw_record():
    raw = make_raw_record(location="37.7749, -122.4194")
    memory = Memory(**raw)
    assert memory.fingerprint == source_fingerprint(raw)
    assert memory.latitude == pytest.approx(37.7749)
    assert memory.longitude == pytest.approx(-122.4194)
    assert memory.filename == "2020-01-02_03-04-05"


def test_memory_without_location():
    memory = Memory(**make_raw_record())
    assert memory.latitude is None and memory.longitude is None


def test_index_entry_enums_validated():
    kwargs = dict(
        fingerprint="f",
        date="2020-01-02 03:04:05",
        ext=".jpg",
        enrichment=Enrichment.PENDING,
        updated_at="now",
    )
    IndexEntry(**kwargs)
    with pytest.raises(ValidationError):
        IndexEntry(**{**kwargs, "enrichment": "bogus"})
    with pytest.raises(ValidationError):
        IndexEntry(**{**kwargs, "raw_source": RawSource.CDN.value + "x"})
