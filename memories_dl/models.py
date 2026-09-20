"""Domain models: source memories, index entries, outcomes and stats."""

import enum
import hashlib
import json
import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class Memory(BaseModel):
    date: datetime = Field(alias="Date")
    download_link: str = Field(alias="Download Link")
    location: str = Field(default="", alias="Location")
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S UTC")
        return v

    def model_post_init(self, __context):
        if self.location and not self.latitude:
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", self.location):
                self.latitude = float(match.group(1))
                self.longitude = float(match.group(2))

    @property
    def filename(self) -> str:
        return self.date.strftime("%Y-%m-%d_%H-%M-%S")

    @property
    def source_record(self) -> dict[str, str]:
        """Canonical source fields exactly as the export record describes them."""
        return {
            "Date": self.date.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Download Link": self.download_link,
            "Location": self.location,
        }

    @property
    def fingerprint(self) -> str:
        """Stable sha256 over the canonical source record fields."""
        return source_fingerprint(self.source_record)


def source_fingerprint(raw_record: dict) -> str:
    """sha256 of canonical JSON over Date / Download Link / Location.

    Field order is irrelevant; only the raw source values matter.
    """
    canonical = json.dumps(
        {
            "Date": raw_record["Date"],
            "Download Link": raw_record["Download Link"],
            "Location": raw_record.get("Location", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawSource(str, enum.Enum):
    CDN = "cdn"        # digest verified against the bytes actually fetched
    UNKNOWN = "unknown"  # migrated pre-upgrade file, provenance unverified


class Enrichment(str, enum.Enum):
    PENDING = "pending"            # raw committed, derived not published
    SUCCESS = "success"            # JPEG EXIF injected and verified
    NOT_REQUIRED = "not_required"  # --no-exif or non-JPEG: raw published as-is
    FAILED = "failed"              # attempted enrichment failed; raw retained
    UNKNOWN = "unknown"            # migrated legacy, never processed


class Outcome(str, enum.Enum):
    SUCCESS = "success"                  # raw + verified derived published
    ENRICH_FAILED = "enrich_failed"      # raw saved, enrichment failed
    FAILED = "failed"                    # network/raw failure
    SKIPPED = "skipped"                  # already complete, untouched
    LEGACY_SKIPPED = "legacy_skipped"    # untracked flat file blocked the path


class IndexEntry(BaseModel):
    """Minimal per-memory index row."""

    fingerprint: str
    date: str
    ext: str
    raw_digest: str | None = None
    raw_path: str | None = None
    raw_source: RawSource | None = None
    derived_path: str | None = None
    enrichment: Enrichment
    updated_at: str

    def derived_file(self, output_dir) -> "Path | None":
        if not self.derived_path:
            return None
        from pathlib import Path

        path = Path(output_dir) / self.derived_path
        return path if path.exists() else None

    @property
    def expected_stem(self) -> str:
        # Stored date ("YYYY-MM-DD HH:MM:SS") -> filename stem.
        return self.date.replace(" ", "_").replace(":", "-")

    def expected_derived_rel(self, memory=None) -> str:
        stem = memory.filename if memory is not None else self.expected_stem
        return f"{stem}{self.ext}"

    def expected_derived_file(self, output_dir, memory=None) -> "Path | None":
        """Where the derived artifact for this row must live, if it exists.

        Works even when ``derived_path`` was never committed (crash window
        between derived publish and the final index update).
        """
        from pathlib import Path

        path = Path(output_dir) / self.expected_derived_rel(memory)
        return path if path.exists() else None


class Stats(BaseModel):
    downloaded: int = 0
    enrich_failed: int = 0
    skipped: int = 0
    legacy: int = 0
    failed: int = 0
    mb: float = 0.0
