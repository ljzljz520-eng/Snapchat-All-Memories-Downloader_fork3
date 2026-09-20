"""Atomic minimal index: ``.memories/index.json``.

The index is the single source of truth for resume/skip/retry decisions.
Every update goes through temp-file + fsync + atomic ``os.replace``, so the
main index file is always either the previous complete version or the new
complete version, never a half-written one.
"""

import asyncio
import json
import os
from pathlib import Path

from . import layout
from .faults import inject
from .models import Enrichment, IndexEntry, RawSource
from .store import fsync_dir, write_all


class IndexCorruptError(RuntimeError):
    """The main index file exists but cannot be parsed."""


class IndexStore:
    def __init__(self, output_dir: Path | str):
        self.output_dir = Path(output_dir)
        self.path = layout.index_path(self.output_dir)
        self.entries: dict[str, IndexEntry] = {}
        self._lock = asyncio.Lock()

    # ---- loading / saving -------------------------------------------------

    def load(self) -> "IndexStore":
        if not self.path.exists():
            self.entries = {}
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw["entries"]
            if not isinstance(rows, dict):
                raise ValueError("entries is not an object")
            self.entries = {
                fp: _entry_from_row(fp, row) for fp, row in rows.items()
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise IndexCorruptError(
                f"index is corrupt and will not be silently reset: {self.path}: {exc}"
            ) from exc
        return self

    async def replace_many(self, entries: dict[str, IndexEntry], fault_point: str | None = None) -> None:
        """Replace several rows and persist atomically (used by migration)."""
        async with self._lock:
            staged = dict(self.entries)
            staged.update(entries)
            self._commit(staged, fault_point)

    async def upsert(self, entry: IndexEntry, fault_point: str | None = None) -> None:
        async with self._lock:
            staged = dict(self.entries)
            staged[entry.fingerprint] = entry
            self._commit(staged, fault_point)

    def _commit(self, staged: dict[str, IndexEntry], fault_point: str | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = layout.new_index_tmp(self.output_dir)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                write_all(fd, self._serialize_bytes(staged))
                os.fsync(fd)
            finally:
                os.close(fd)
            if fault_point:
                inject(fault_point)
            os.replace(tmp, self.path)
            fsync_dir(self.path.parent)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # In-memory state advances only after the durable commit.
        self.entries = staged

    @staticmethod
    def _serialize_bytes(entries: dict[str, IndexEntry]) -> bytes:
        payload = {
            "version": layout.INDEX_VERSION,
            "entries": {
                fp: _row_for(entry) for fp, entry in sorted(entries.items())
            },
        }
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    # ---- accessors --------------------------------------------------------

    def get(self, fingerprint: str) -> IndexEntry | None:
        return self.entries.get(fingerprint)

    def tracked_derived_paths(self) -> set[str]:
        return {
            e.derived_path for e in self.entries.values() if e.derived_path
        }


def _row_for(entry: IndexEntry) -> dict:
    # fingerprint is the map key; it must not be duplicated inside the row.
    row = entry.model_dump(mode="json")
    row.pop("fingerprint", None)
    return row


def _entry_from_row(fp: str, row: dict) -> IndexEntry:
    return IndexEntry(
        fingerprint=fp,
        date=row["date"],
        ext=row["ext"],
        raw_digest=row.get("raw_digest"),
        raw_path=row.get("raw_path"),
        raw_source=RawSource(row["raw_source"]) if row.get("raw_source") else None,
        derived_path=row.get("derived_path"),
        enrichment=Enrichment(row["enrichment"]),
        updated_at=row["updated_at"],
    )
