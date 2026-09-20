import asyncio
import json

from memories_dl.cli import build_parser
from memories_dl.models import Enrichment
from memories_dl.pipeline import download_all
from memories_dl.models import Memory

from conftest import (
    JPEG_BYTES,
    MP4_BYTES,
    loopback_record,
    make_raw_record,
    run_cli,
    write_json,
)


def test_help_lists_all_arguments():
    help_text = build_parser().format_help()
    for flag in [
        "--no-exif",
        "--no-skip-existing",
        "--migrate-legacy",
        "-c",
        "-o",
    ]:
        assert flag in help_text


def test_migrate_cli_registers_and_does_not_download(tmp_path, loopback_server):
    out = tmp_path / "dl"
    out.mkdir()
    (out / "2020-01-02_03-04-05.jpg").write_bytes(JPEG_BYTES)
    (out / "2019-12-31_23-59-59.mp4").write_bytes(MP4_BYTES)
    json_path = write_json(
        tmp_path / "mem.json",
        [loopback_record(loopback_server, location="37.7749, -122.4194")],
    )

    result = run_cli(json_path, out, "--migrate-legacy")
    assert result.returncode == 0, result.stderr
    assert "Migrated 2 legacy file(s)" in result.stdout
    assert loopback_server.gets == 0 and loopback_server.posts == 0

    rows = json.loads((out / ".memories" / "index.json").read_text())["entries"]
    assert len(rows) == 2
    assert all(row["raw_source"] == "unknown" for row in rows.values())
    assert all(row["enrichment"] == "unknown" for row in rows.values())


def test_concurrent_mixed_run_is_consistent(tmp_path, cdn):
    memories = []
    for i in range(3):
        memories.append(
            Memory(
                **make_raw_record(
                    date=f"2020-01-{2 + i:02d} 03:04:0{i} UTC",
                    location="37.7749, -122.4194",
                )
            )
        )
    for i in range(2):
        memories.append(
            Memory(**make_raw_record(date=f"2020-02-{2 + i:02d} 03:04:05 UTC"))
        )
    for i in range(3):
        memories.append(
            Memory(
                **make_raw_record(
                    date=f"2020-03-{2 + i:02d} 10:00:0{i} UTC",
                    name="clip.mp4",
                )
            )
        )

    stats = asyncio.run(
        download_all(
            memories, tmp_path, max_concurrent=8, transport=cdn.transport()
        )
    )
    assert stats.downloaded == 8
    assert stats.failed == 0 and stats.enrich_failed == 0

    index = json.loads((tmp_path / ".memories" / "index.json").read_text())
    assert len(index["entries"]) == 8
    states = {row["enrichment"] for row in index["entries"].values()}
    assert states <= {"success", "not_required"}
    assert not list(tmp_path.glob(".*.tmp-*"))
    assert not list((tmp_path / ".memories").glob(".index.json.tmp-*"))
    assert not list((tmp_path / ".memories" / "raw").rglob(".rawtmp-*"))
