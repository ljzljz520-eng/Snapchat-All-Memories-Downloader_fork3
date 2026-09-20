import errno
import hashlib
import io
import os

import exif
import pytest

from memories_dl import enrich
from memories_dl.models import Memory
from memories_dl.store import commit_raw

from conftest import JPEG_BYTES, MP4_BYTES, make_raw_record


@pytest.fixture
def raw_jpeg(tmp_path):
    rel, digest = commit_raw(tmp_path, JPEG_BYTES, ".jpg")
    return tmp_path, rel, digest


def gps_memory(location="37.7749, -122.4194", date="2020-01-02 03:04:05 UTC"):
    return Memory(**make_raw_record(date=date, location=location))


def test_publish_exif_tags_and_separation(raw_jpeg):
    out, rel, digest = raw_jpeg
    memory = gps_memory()
    target = out / "2020-01-02_03-04-05.jpg"
    mtime = memory.date.timestamp()

    enrich.publish_exif(
        output_dir=out, raw_relpath=rel, target=target,
        memory=memory, mtime=mtime,
    )

    assert target.exists()
    check = exif.Image(target.read_bytes())
    assert check.datetime_original == "2020:01:02 03:04:05"
    assert check.gps_latitude_ref == "N"
    assert check.gps_longitude_ref == "W"
    assert tuple(check.gps_latitude) == pytest.approx(
        enrich.decimal_to_dms(37.7749), abs=1e-6
    )
    assert tuple(check.gps_longitude) == pytest.approx(
        enrich.decimal_to_dms(-122.4194), abs=1e-6
    )

    # Raw untouched, separate path and inode.
    assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest
    assert (out / rel).resolve() != target.resolve()
    assert (out / rel).stat().st_ino != target.stat().st_ino
    assert abs(target.stat().st_mtime - mtime) < 1


def test_passthrough_jpeg_and_mp4(tmp_path):
    out = tmp_path
    cases = [
        (JPEG_BYTES, ".jpg", "2020-01-02_03-04-05", "2020-01-02 03:04:05 UTC"),
        (MP4_BYTES, ".mp4", "2020-01-03_10-00-00", "2020-01-03 10:00:00 UTC"),
    ]
    for payload, ext, stem, date in cases:
        rel, digest = commit_raw(out, payload, ext)
        target = out / f"{stem}{ext}"
        memory = Memory(**make_raw_record(date=date))
        mtime = memory.date.timestamp()
        enrich.publish_passthrough(
            output_dir=out, raw_relpath=rel, target=target, mtime=mtime
        )
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
        assert (out / rel).stat().st_ino != target.stat().st_ino
        assert abs(target.stat().st_mtime - mtime) < 1


def test_jpeg_without_gps_has_no_gps_tags(raw_jpeg):
    out, rel, _ = raw_jpeg
    memory = Memory(**make_raw_record())
    target = out / "2020-01-02_03-04-05.jpg"
    enrich.publish_exif(
        output_dir=out, raw_relpath=rel, target=target,
        memory=memory, mtime=memory.date.timestamp(),
    )
    check = exif.Image(target.read_bytes())
    assert check.datetime_original == "2020:01:02 03:04:05"
    assert not hasattr(check, "gps_latitude")
    assert not hasattr(check, "gps_longitude")


def test_exif_parse_failure_isolates_raw(raw_jpeg, monkeypatch):
    out, rel, digest = raw_jpeg
    target = out / "2020-01-02_03-04-05.jpg"
    monkeypatch.setenv("MEMORIES_FAULT", "exif_parse:error")
    with pytest.raises(enrich.EnrichError) as exc:
        enrich.publish_exif(
            output_dir=out, raw_relpath=rel, target=target,
            memory=gps_memory(), mtime=0,
        )
    assert exc.value.kind == "parse"
    assert not target.exists()
    assert list(out.glob(".*.tmp-*")) == []
    assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest


def test_derived_commit_enospc_never_publishes(raw_jpeg, monkeypatch):
    out, rel, _ = raw_jpeg
    target = out / "2020-01-02_03-04-05.jpg"
    monkeypatch.setenv("MEMORIES_FAULT", "derived_commit:enospc")
    with pytest.raises(OSError) as exc:
        enrich.publish_exif(
            output_dir=out, raw_relpath=rel, target=target,
            memory=gps_memory(), mtime=0,
        )
    assert exc.value.errno == errno.ENOSPC
    assert not target.exists()
    assert list(out.glob(".*.tmp-*")) == []
