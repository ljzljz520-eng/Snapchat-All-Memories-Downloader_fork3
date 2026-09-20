"""R2 regression: the real crash window (derived published, index stale).

State produced by a crash/ENOSPC at ``index_update_derived``:
  * the user-visible derived artifact is durably published;
  * the index row is still ``cdn/pending`` with ``derived_path=None``.

Recovery must reconcile that state purely from disk (zero CDN), and legacy
migration must never adopt the artifact as provenance-unknown.
"""

import asyncio
import hashlib
import json

import pytest

from memories_dl import legacy as legacy_mod
from memories_dl.index import IndexStore
from memories_dl.models import Enrichment, Memory, RawSource
from memories_dl.pipeline import download_all
from memories_dl.recovery import run_recovery
from memories_dl.store import sha256_hex

from conftest import (
    JPEG_BYTES,
    loopback_record,
    make_raw_record,
    run_cli,
    write_json,
)


def run(memories, out, cdn, **kw):
    return asyncio.run(
        download_all(
            memories, out, max_concurrent=2, transport=cdn.transport(), **kw
        )
    )


def recover(out, memories):
    index = IndexStore(out).load()
    by_fp = {m.fingerprint: m for m in memories}
    asyncio.run(run_recovery(out, index, by_fp))
    return IndexStore(out).load()


@pytest.mark.parametrize(
    "add_exif,name,expected",
    [
        (True, "pixel.jpg", Enrichment.SUCCESS),
        (False, "pixel.jpg", Enrichment.NOT_REQUIRED),
        (False, "clip.mp4", Enrichment.NOT_REQUIRED),
    ],
)
def test_recovery_reconciles_published_but_unindexed_artifact(
    tmp_path, cdn, monkeypatch, add_exif, name, expected
):
    loc = "37.7749, -122.4194" if add_exif else ""
    memory = Memory(**make_raw_record(name=name, location=loc))
    fp = memory.fingerprint
    target = tmp_path / f"{memory.filename}.{name.rsplit('.', 1)[1]}"

    # Stage A: inject a failure exactly at the final index update.
    monkeypatch.setenv("MEMORIES_FAULT", "index_update_derived:error")
    stats = run([memory], tmp_path, cdn, add_exif=add_exif)
    assert stats.failed == 1 and stats.downloaded == 0
    assert target.exists()
    row = IndexStore(tmp_path).load().get(fp)
    assert row.raw_source == RawSource.CDN
    assert row.enrichment == Enrichment.PENDING and row.derived_path is None
    assert cdn.gets == 1

    # Recovery alone (no pipeline follow-up): reconcile purely from disk.
    monkeypatch.delenv("MEMORIES_FAULT")
    fixed = recover(tmp_path, [memory]).get(fp)
    assert fixed.enrichment == expected
    assert fixed.derived_path == target.relative_to(tmp_path).as_posix()
    assert fixed.raw_source == RawSource.CDN
    raw = tmp_path / fixed.raw_path
    assert raw.exists() and sha256_hex(raw.read_bytes()) == fixed.raw_digest
    assert target.exists()
    assert cdn.gets == 1  # reconciliation touched no network

    # A default run afterwards has nothing to do and still no CDN traffic.
    stats2 = run([memory], tmp_path, cdn, add_exif=add_exif)
    assert stats2.skipped == 1 and cdn.gets == 1


def test_migrate_skips_inflight_cdn_target_without_recovery(tmp_path, cdn, monkeypatch, capsys):
    memory = Memory(**make_raw_record(location="37.7749, -122.4194"))
    monkeypatch.setenv("MEMORIES_FAULT", "index_update_derived:error")
    run([memory], tmp_path, cdn)

    # Migration invoked directly against the stale index (recovery skipped):
    # the published target must not be adopted as unknown provenance.
    index = IndexStore(tmp_path).load()
    before = index.get(memory.fingerprint)
    migrated = legacy_mod.migrate_legacy(
        tmp_path,
        {memory.filename: memory},
        dict(index.entries),
    )
    assert migrated == {}
    out = capsys.readouterr().out
    assert "Skipped" in out and "in-progress CDN-indexed" in out

    after = IndexStore(tmp_path).load().get(memory.fingerprint)
    assert after.raw_source == RawSource.CDN
    assert after.enrichment == Enrichment.PENDING
    assert after.raw_path == before.raw_path
    assert after.raw_digest == before.raw_digest
    assert (tmp_path / before.raw_path).exists()


def test_cli_migrate_after_exit_crash_keeps_cdn_row(tmp_path, loopback_server):
    record = loopback_record(
        loopback_server, "pixel.jpg", location="37.7749, -122.4194"
    )
    json_path = write_json(tmp_path / "memories.json", [record])
    out = tmp_path / "out"

    first = run_cli(
        json_path, out, fault="index_update_derived:exit"
    )
    assert first.returncode == 99

    second = run_cli(json_path, out, "--migrate-legacy")
    assert second.returncode == 0
    # Startup recovery finishes the row, so nothing remains to migrate.
    assert "No untracked legacy files found." in second.stdout

    index = json.loads((out / ".memories" / "index.json").read_text())
    rows = index["entries"]
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["raw_source"] == "cdn"
    assert row["enrichment"] == "success"
    assert row["raw_digest"] == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert (out / row["raw_path"]).exists()
    assert (out / row["derived_path"]).exists()
    assert loopback_server.gets == 1


def test_cli_default_rerun_after_exit_crash_uses_zero_gets(tmp_path, loopback_server):
    record = loopback_record(
        loopback_server, "pixel.jpg", location="37.7749, -122.4194"
    )
    json_path = write_json(tmp_path / "memories.json", [record])
    out = tmp_path / "out"

    first = run_cli(json_path, out, fault="index_update_derived:exit")
    assert first.returncode == 99
    gets_after_crash = loopback_server.gets

    second = run_cli(json_path, out)
    assert second.returncode == 0
    assert loopback_server.gets == gets_after_crash  # no re-GET
    # Startup recovery already reconciled the published artifact.
    assert "Skipped: 1" in second.stdout

    index = json.loads((out / ".memories" / "index.json").read_text())
    row = next(iter(index["entries"].values()))
    assert row["enrichment"] == "success" and row["raw_source"] == "cdn"


def test_strict_classifier_rejects_nonraw_broken_jpeg(tmp_path, cdn):
    """Digest-mismatched JPEGs only reach a terminal state via strict EXIF."""
    import exif

    from memories_dl import enrich

    memory = Memory(**make_raw_record(location="37.7749, -122.4194"))
    enriched = enrich._build_exif_bytes(JPEG_BYTES, memory)

    assert enrich.inspect_derived(enriched, True, memory) == "success"
    # SOI-prefixed garbage, tagless prefixes and truncated payloads all fail.
    for broken in (
        b"\xff\xd8",
        b"\xff\xd8\xff\xd9",
        b"\xff\xd8garbage",
        JPEG_BYTES[:10],
        enriched[: len(enriched) // 2],  # APP1 tags present, image truncated
    ):
        assert enrich.inspect_derived(broken, True, memory) is None

    # End-to-end through recovery: a crash-window target holding foreign
    # truncated bytes is deleted and the row returns to pending.
    run([memory], tmp_path, cdn)
    target = tmp_path / f"{memory.filename}.jpg"
    index = IndexStore(tmp_path).load()
    stale = index.get(memory.fingerprint).model_copy(
        update={"enrichment": Enrichment.PENDING, "derived_path": None}
    )
    asyncio.run(index.upsert(stale))
    target.write_bytes(b"\xff\xd8" + b"\x00" * 38)

    fixed = recover(tmp_path, [memory]).get(memory.fingerprint)
    assert fixed.enrichment == Enrichment.PENDING and fixed.derived_path is None
    assert not target.exists()

    stats = run([memory], tmp_path, cdn)
    assert stats.downloaded == 1 and cdn.gets == 1
    img = exif.Image(target.read_bytes())
    assert img.datetime_original == "2020:01:02 03:04:05"


def test_cli_broken_legacy_file_rebuilt_after_injected_failure(tmp_path, loopback_server):
    """Three real CLI rounds: truncated pre-upgrade file must never be adopted.

    1) --no-skip-existing with exif_parse failure: raw is saved, the old
       truncated bytes remain in place and the row is failed;
    2) plain default run: recovery rejects the bytes and rebuilds offline;
    total CDN GETs stay at one.
    """
    import exif

    record = loopback_record(
        loopback_server, "pixel.jpg", location="37.7749, -122.4194"
    )
    json_path = write_json(tmp_path / "memories.json", [record])
    out = tmp_path / "out"
    out.mkdir()
    target = out / "2020-01-02_03-04-05.jpg"
    truncated = JPEG_BYTES[:40]
    target.write_bytes(truncated)

    first = run_cli(
        json_path, out, "--no-skip-existing", fault="exif_parse:error"
    )
    assert first.returncode == 0
    assert "Enrichment failed: 1" in first.stdout
    assert target.read_bytes() == truncated  # foreign bytes untouched
    gets = loopback_server.gets

    second = run_cli(json_path, out)
    assert second.returncode == 0
    assert loopback_server.gets == gets  # rebuilt offline, zero extra GETs
    final_bytes = target.read_bytes()
    assert final_bytes != truncated and final_bytes[:2] == b"\xff\xd8"
    img = exif.Image(final_bytes)
    assert img.datetime_original == "2020:01:02 03:04:05"
    row = json.loads((out / ".memories" / "index.json").read_text())
    entry = next(iter(row["entries"].values()))
    assert entry["enrichment"] == "success" and entry["raw_source"] == "cdn"


def test_passthrough_jpeg_without_app1_reconciles_not_required(tmp_path, cdn):
    # --no-exif publishes raw bytes verbatim; a stale FAILED row must not
    # cause the tagless (no APP1) derived JPEG to be deleted/rebuilt.
    memory = Memory(**make_raw_record())
    run([memory], tmp_path, cdn, add_exif=False)
    target = tmp_path / f"{memory.filename}.jpg"
    assert target.read_bytes() == JPEG_BYTES

    index = IndexStore(tmp_path).load()
    stale = index.get(memory.fingerprint).model_copy(
        update={"enrichment": Enrichment.FAILED}
    )
    asyncio.run(index.upsert(stale))

    fixed = recover(tmp_path, [memory]).get(memory.fingerprint)
    assert fixed.enrichment == Enrichment.NOT_REQUIRED
    assert target.exists() and target.read_bytes() == JPEG_BYTES
    assert cdn.gets == 1
