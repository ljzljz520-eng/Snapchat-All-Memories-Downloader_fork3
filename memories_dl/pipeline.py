"""Download orchestration: network fetch, resume decisions, two-phase commit.

Resume is entirely index-driven. When a readable raw object exists locally,
stage B runs from raw bytes and never touches the CDN, even when the previous
enrichment failed or was interrupted.
"""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tqdm.asyncio import tqdm

from . import enrich, legacy as legacy_mod, store
from .index import IndexStore
from .models import (
    Enrichment,
    IndexEntry,
    Memory,
    Outcome,
    RawSource,
    Stats,
)


async def fetch_memory(
    memory: Memory, transport: httpx.BaseTransport | None = None
) -> tuple[bytes, str]:
    """POST the export link, then GET the CDN bytes. Returns (data, ext)."""
    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        response = await client.post(
            memory.download_link,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        cdn_url = response.text.strip()

    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, transport=transport
    ) as client:
        response = await client.get(cdn_url)
        response.raise_for_status()
        data = response.content

    ext = Path(cdn_url.split("?")[0]).suffix.lower() or ".jpg"
    return data, ext


def _new_entry(memory: Memory, digest: str, raw_relpath: str, ext: str) -> IndexEntry:
    return IndexEntry(
        fingerprint=memory.fingerprint,
        date=memory.date.strftime("%Y-%m-%d %H:%M:%S"),
        ext=ext,
        raw_digest=digest,
        raw_path=raw_relpath,
        raw_source=RawSource.CDN,
        derived_path=None,
        enrichment=Enrichment.PENDING,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


class Downloader:
    def __init__(
        self,
        output_dir: Path,
        index: IndexStore,
        *,
        add_exif: bool,
        skip_existing: bool,
        transport: httpx.BaseTransport | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.index = index
        self.add_exif = add_exif
        self.skip_existing = skip_existing
        self.transport = transport
        self.legacy_paths = {
            p.name for p in legacy_mod.scan_legacy(
                self.output_dir, index.tracked_derived_paths()
            )
        }

    async def process(
        self, memory: Memory, semaphore: asyncio.Semaphore
    ) -> tuple[Outcome, int]:
        async with semaphore:
            return await self._process(memory)

    async def _process(self, memory: Memory) -> tuple[Outcome, int]:
        entry = self.index.get(memory.fingerprint)

        if self.skip_existing and entry is not None:
            # Migrated, provenance-unknown rows: treated as tracked, never
            # overwritten or re-downloaded by a default run.
            if entry.raw_source == RawSource.UNKNOWN:
                derived = entry.derived_path and (
                    self.output_dir / entry.derived_path
                ).exists()
                if derived:
                    return Outcome.SKIPPED, 0
                # A migrated row whose file vanished: stay tracked and do not
                # silently rewrite provenance; require an explicit force.
                print(
                    f"\nMigrated (source=unknown) entry for {memory.filename} "
                    f"has no file on disk; left untouched. Re-run "
                    f"--migrate-legacy or use --no-skip-existing to re-download."
                )
                return Outcome.LEGACY_SKIPPED, 0
            if store.raw_exists(self.output_dir, entry.raw_path):
                outcome = await self._publish_from_raw(memory, entry)
                return outcome, 0
            # entry exists but its raw object vanished: re-download below

        if self.skip_existing:
            blocked = [
                name
                for name in self.legacy_paths
                if name.startswith(memory.filename + ".")
            ]
            if blocked:
                print(
                    f"\nLegacy file(s) {', '.join(sorted(blocked))} predate the "
                    f"raw/derived index; left untouched. Run with "
                    f"--migrate-legacy to register them (source=unknown), or "
                    f"--no-skip-existing to re-download."
                )
                return Outcome.LEGACY_SKIPPED, 0

        # ---- network stage --------------------------------------------------
        try:
            data, ext = await fetch_memory(memory, self.transport)
        except Exception as exc:
            print(f"\nDownload error for {memory.filename}: {exc}")
            return Outcome.FAILED, 0
        bytes_len = len(data)

        try:
            raw_relpath, digest = await asyncio.to_thread(
                store.commit_raw, self.output_dir, data, ext
            )
            entry = _new_entry(memory, digest, raw_relpath, ext)
            await self.index.upsert(entry, fault_point="index_update_raw")
        except Exception as exc:
            print(f"\nRaw commit error for {memory.filename}: {exc}")
            return Outcome.FAILED, bytes_len

        outcome = await self._publish_from_raw(memory, entry)
        return outcome, bytes_len

    async def _publish_from_raw(
        self, memory: Memory, entry: IndexEntry
    ) -> Outcome:
        target = self.output_dir / f"{memory.filename}{entry.ext}"
        already_published = entry.enrichment in (
            Enrichment.SUCCESS,
            Enrichment.NOT_REQUIRED,
        ) and target.exists()
        if already_published:
            return Outcome.SKIPPED

        mtime = memory.date.timestamp()
        use_exif = self.add_exif and enrich.is_jpeg(entry.ext)
        try:
            if use_exif:
                await asyncio.to_thread(
                    enrich.publish_exif,
                    output_dir=self.output_dir,
                    raw_relpath=entry.raw_path,
                    target=target,
                    memory=memory,
                    mtime=mtime,
                )
                new_state = Enrichment.SUCCESS
            else:
                await asyncio.to_thread(
                    enrich.publish_passthrough,
                    output_dir=self.output_dir,
                    raw_relpath=entry.raw_path,
                    target=target,
                    mtime=mtime,
                )
                new_state = Enrichment.NOT_REQUIRED
        except enrich.EnrichError as exc:
            failed = entry.model_copy(
                update={
                    "enrichment": Enrichment.FAILED,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self.index.upsert(failed)
            print(
                f"\nEnrichment failed for {memory.filename} "
                f"(raw retained at {entry.raw_path}): {exc}"
            )
            return Outcome.ENRICH_FAILED
        except Exception as exc:
            # Disk/IO failure at publication: raw is intact, retry offline.
            failed = entry.model_copy(
                update={
                    "enrichment": Enrichment.FAILED,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self.index.upsert(failed)
            print(f"\nDerived publish error for {memory.filename}: {exc}")
            return Outcome.ENRICH_FAILED

        published = entry.model_copy(
            update={
                "enrichment": new_state,
                "derived_path": target.relative_to(self.output_dir).as_posix(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            await self.index.upsert(published, fault_point="index_update_derived")
        except Exception as exc:
            print(f"\nIndex update error for {memory.filename}: {exc}")
            return Outcome.FAILED
        return Outcome.SUCCESS


async def download_all(
    memories: list[Memory],
    output_dir: Path,
    *,
    max_concurrent: int = 40,
    add_exif: bool = True,
    skip_existing: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> Stats:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    index = IndexStore(output_dir).load()
    downloader = Downloader(
        output_dir,
        index,
        add_exif=add_exif,
        skip_existing=skip_existing,
        transport=transport,
    )
    semaphore = asyncio.Semaphore(max_concurrent)
    stats = Stats()
    start = time.time()

    progress = tqdm(total=len(memories), desc="Downloading", unit="file")

    async def worker(memory: Memory) -> None:
        result = await downloader.process(memory, semaphore)
        outcome, bytes_len = result
        if outcome == Outcome.SUCCESS:
            stats.downloaded += 1
        elif outcome == Outcome.ENRICH_FAILED:
            stats.enrich_failed += 1
        elif outcome == Outcome.SKIPPED:
            stats.skipped += 1
        elif outcome == Outcome.LEGACY_SKIPPED:
            stats.legacy += 1
        else:
            stats.failed += 1
        stats.mb += bytes_len / 1024 / 1024

        elapsed = time.time() - start
        rate = stats.mb / elapsed if elapsed > 0 else 0
        progress.set_postfix({"MB/s": f"{rate:.2f}"}, refresh=False)
        progress.update(1)

    await asyncio.gather(*[worker(m) for m in memories])
    progress.close()

    elapsed = time.time() - start
    rate = stats.mb / elapsed if elapsed > 0 else 0
    print(
        f"\n{'=' * 60}\n"
        f"Downloaded: {stats.downloaded} ({stats.mb:.1f} MB @ {rate:.2f} MB/s) "
        f"| Enrichment failed: {stats.enrich_failed} "
        f"| Skipped: {stats.skipped} | Legacy: {stats.legacy} "
        f"| Failed: {stats.failed}\n"
        f"{'=' * 60}"
    )
    return stats
