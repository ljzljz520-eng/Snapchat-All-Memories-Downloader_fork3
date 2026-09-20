"""Cross-process crash tests: MEMORIES_FAULT=<point>:exit uses os._exit(99).

A fresh interpreter performs recovery on the next run, which is the only way
to observe true post-crash state.
"""

import hashlib
import json
from pathlib import Path

import exif
import pytest

from conftest import (
    JPEG_BYTES,
    loopback_record,
    run_cli,
    write_json,
)

CRASH_EXIT = 99
TARGET_NAME = "2020-01-02_03-04-05.jpg"


def _raw_temps(out: Path):
    root = out / ".memories" / "raw"
    return list(root.rglob(".rawtmp-*")) if root.exists() else []


def _derived_temps(out: Path):
    return list(out.glob(".*.tmp-*"))


def _index(out: Path):
    return json.loads((out / ".memories" / "index.json").read_text())


def _assert_final_success(out: Path):
    target = out / TARGET_NAME
    assert target.exists()
    assert _raw_temps(out) == []
    assert _derived_temps(out) == []
    rows = _index(out)["entries"]
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["enrichment"] == "success"
    raw = out / row["raw_path"]
    assert raw.read_bytes() == JPEG_BYTES
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == row["raw_digest"]
    img = exif.Image(target.read_bytes())
    assert img.datetime_original == "2020:01:02 03:04:05"
    assert img.gps_latitude_ref == "N" and img.gps_longitude_ref == "W"


def test_crash_at_raw_commit(tmp_path, loopback_server):
    json_path = write_json(
        tmp_path / "mem.json",
        [loopback_record(loopback_server, location="37.7749, -122.4194")],
    )
    out = tmp_path / "dl"

    first = run_cli(json_path, out, fault="raw_commit:exit")
    assert first.returncode == CRASH_EXIT
    assert len(_raw_temps(out)) == 1
    assert not (out / TARGET_NAME).exists()

    second = run_cli(json_path, out)
    assert second.returncode == 0, second.stderr
    _assert_final_success(out)


def test_crash_at_derived_write(tmp_path, loopback_server):
    json_path = write_json(
        tmp_path / "mem.json",
        [loopback_record(loopback_server, location="37.7749, -122.4194")],
    )
    out = tmp_path / "dl"

    first = run_cli(json_path, out, fault="derived_write:exit")
    assert first.returncode == CRASH_EXIT
    assert not (out / TARGET_NAME).exists()
    assert len(_derived_temps(out)) == 1
    gets = loopback_server.gets

    second = run_cli(json_path, out)
    assert second.returncode == 0, second.stderr
    assert loopback_server.gets == gets  # raw present: enrichment retried offline
    _assert_final_success(out)


def test_crash_at_index_update_raw(tmp_path, loopback_server):
    json_path = write_json(
        tmp_path / "mem.json",
        [loopback_record(loopback_server, location="37.7749, -122.4194")],
    )
    out = tmp_path / "dl"

    first = run_cli(json_path, out, fault="index_update_raw:exit")
    assert first.returncode == CRASH_EXIT
    # Raw object exists, but the index never recorded it.
    raw_files = [
        p
        for p in (out / ".memories" / "raw").rglob("*")
        if p.is_file() and p.name.endswith(".jpg")
    ]
    assert len(raw_files) == 1
    assert not (out / ".memories" / "index.json").exists()

    second = run_cli(json_path, out)
    assert second.returncode == 0, second.stderr
    _assert_final_success(out)


def test_crash_at_index_update_derived(tmp_path, loopback_server):
    json_path = write_json(
        tmp_path / "mem.json",
        [loopback_record(loopback_server, location="37.7749, -122.4194")],
    )
    out = tmp_path / "dl"

    first = run_cli(json_path, out, fault="index_update_derived:exit")
    assert first.returncode == CRASH_EXIT
    # Both raw and derived durably exist; index row is still "pending".
    assert (out / TARGET_NAME).exists()
    row = next(iter(_index(out)["entries"].values()))
    assert row["enrichment"] == "pending"
    gets = loopback_server.gets

    second = run_cli(json_path, out)
    assert second.returncode == 0, second.stderr
    # Recovery reconciles the published derived file without any CDN traffic.
    assert loopback_server.gets == gets
    _assert_final_success(out)
