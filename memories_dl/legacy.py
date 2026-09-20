"""Detection and explicit migration of pre-upgrade flat files."""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Enrichment, IndexEntry, RawSource

LEGACY_EXTS = {".jpg", ".jpeg", ".mp4"}
_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})")


def is_legacy_name(path: Path) -> bool:
    return path.is_file() and _STEM_RE.match(path.stem) and path.suffix.lower() in LEGACY_EXTS


def scan_legacy(output_dir: Path, tracked_derived: set[str]) -> list[Path]:
    """Flat timestamp-named media files that no index row points at."""
    found = []
    for child in Path(output_dir).iterdir():
        if not is_legacy_name(child):
            continue
        rel = child.relative_to(output_dir).as_posix()
        if rel not in tracked_derived:
            found.append(child)
    return sorted(found)


def _stem_date(stem: str) -> str | None:
    if not _STEM_RE.match(stem):
        return None
    m = _DATE_PREFIX_RE.match(stem)
    return f"{m.group(1)} {m.group(2).replace('-', ':')}"


def migrate_legacy(
    output_dir: Path,
    memories_by_stem: dict[str, object],
    index_entries: dict[str, IndexEntry],
) -> dict[str, IndexEntry]:
    """Register legacy files in place with provenance ``unknown``.

    Files are never moved or copied into the raw store: ``raw_path`` stays
    empty and the digest records only what the bytes factually are, without
    claiming CDN provenance.
    """
    tracked = {
        e.derived_path
        for e in index_entries.values()
        if e.derived_path
    }
    # CDN rows whose final index update never committed still own their
    # deterministic target path; migration must not adopt those artifacts as
    # provenance-unknown legacy files.
    cdn_fps = {
        fp
        for fp, e in index_entries.items()
        if e.raw_source == RawSource.CDN
    }
    inflight_targets = {
        e.expected_derived_rel()
        for e in index_entries.values()
        if e.raw_source == RawSource.CDN and not e.derived_path
    }
    now = datetime.now(timezone.utc).isoformat()
    migrated: dict[str, IndexEntry] = {}
    for path in scan_legacy(output_dir, tracked):
        rel = path.relative_to(output_dir).as_posix()
        if rel in tracked:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        memory = memories_by_stem.get(path.stem)
        if memory is not None:
            fingerprint = memory.fingerprint
            date = memory.date.strftime("%Y-%m-%d %H:%M:%S")
            if fingerprint in cdn_fps or rel in inflight_targets:
                print(
                    f"Skipped {rel}: belongs to an in-progress CDN-indexed "
                    f"entry; run a normal download (or recovery) to finish it, "
                    f"it is not treated as legacy."
                )
                continue
        elif rel in inflight_targets:
            print(
                f"Skipped {rel}: target path of an in-progress CDN-indexed "
                f"entry; not treated as legacy."
            )
            continue
        if memory is None:
            # Orphan file: no source record, synthetic identity.
            fingerprint = "legacy:" + hashlib.sha1(path.name.encode("utf-8")).hexdigest()
            date = _stem_date(path.stem) or ""
        migrated[fingerprint] = IndexEntry(
            fingerprint=fingerprint,
            date=date,
            ext=path.suffix.lower(),
            raw_digest=digest,
            raw_path=None,
            raw_source=RawSource.UNKNOWN,
            derived_path=rel,
            enrichment=Enrichment.UNKNOWN,
            updated_at=now,
        )
    return migrated
