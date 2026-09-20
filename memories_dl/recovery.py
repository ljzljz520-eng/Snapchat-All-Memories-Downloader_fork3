"""Startup recovery: sweep recognizable temps and reconcile the index.

After any crash or ENOSPC, the observable state is always one of:
  * the last fully committed state;
  * recognizable temp items waiting to be swept.

Only files matching the fixed temp patterns are ever deleted. User files and
published derived artifacts are never removed merely for looking unfamiliar.
"""

from pathlib import Path

from . import layout
from .enrich import is_jpeg, inspect_derived
from .index import IndexStore
from .models import Enrichment, RawSource
from .store import read_raw, sha256_hex


def cleanup_temps(output_dir: Path) -> list[str]:
    """Delete raw/index/derived temp files. Returns removed names."""
    output_dir = Path(output_dir)
    removed = []

    raw_root = layout.raw_root(output_dir)
    if raw_root.exists():
        for tmp in raw_root.rglob(f"{layout.RAW_TMP_PREFIX}*"):
            if tmp.is_file() and layout.is_raw_tmp(tmp):
                tmp.unlink(missing_ok=True)
                removed.append(str(tmp))

    meta = layout.meta_dir(output_dir)
    if meta.exists():
        for tmp in meta.iterdir():
            if tmp.is_file() and layout.is_index_tmp(tmp):
                tmp.unlink(missing_ok=True)
                removed.append(str(tmp))

    if output_dir.exists():
        for tmp in output_dir.iterdir():
            if tmp.is_file() and layout.is_derived_tmp(tmp):
                tmp.unlink(missing_ok=True)
                removed.append(str(tmp))

    return removed


def _classify_derived(entry, data: bytes, memory) -> str | None:
    """``"success"`` / ``"not_required"`` / ``None`` (unverifiable)."""
    # Bytes identical to the raw object: a passthrough publish (--no-exif
    # JPEG, non-JPEG). Digest check first also covers JPEGs without an APP1
    # segment, which the EXIF library cannot present tags for.
    if entry.raw_digest and sha256_hex(data) == entry.raw_digest:
        return "not_required"
    if is_jpeg(entry.ext):
        return inspect_derived(data, True, memory)
    return None


def _reconcile_one(entry, output_dir: Path, memory) -> tuple[str | None, object]:
    """Return (updated fingerprint, new entry) or (fp, None) when unchanged."""
    fp = entry.fingerprint

    # Migration rows describe bytes we never processed; never judge them.
    if entry.raw_source == RawSource.UNKNOWN:
        return fp, None

    raw_rel = entry.raw_path
    raw_present = bool(raw_rel) and (output_dir / raw_rel).exists()

    if not raw_present:
        # Raw vanished (external deletion): forget raw claims, the pipeline
        # will re-download. A stale derived file is replaced afterwards.
        fixed = entry.model_copy(
            update={
                "raw_digest": None,
                "raw_path": None,
                "raw_source": None,
                "enrichment": Enrichment.PENDING,
            }
        )
        return fp, fixed

    derived = entry.derived_file(output_dir)
    crash_published = False
    if derived is None and not entry.derived_path:
        # Real crash window: the derived artifact may already be durably
        # published even though the index update recording its path never
        # committed. Locate it via the deterministic target name.
        candidate = entry.expected_derived_file(output_dir, memory)
        if candidate is not None:
            derived = candidate
            crash_published = True

    if derived is None:
        if entry.enrichment in (Enrichment.SUCCESS, Enrichment.NOT_REQUIRED):
            # Published row lost its artifact; raw allows an offline rebuild.
            fixed = entry.model_copy(update={"enrichment": Enrichment.PENDING})
            return fp, fixed
        return fp, None

    if crash_published or entry.enrichment in (Enrichment.PENDING, Enrichment.FAILED):
        # A derived file exists while the index never acknowledged it: the
        # crash happened between atomic publish and index update. Re-verify
        # the bytes actually on disk before claiming a terminal state.
        label = _classify_derived(entry, derived.read_bytes(), memory)
        if label in ("success", "not_required"):
            update = {
                "enrichment": (
                    Enrichment.SUCCESS
                    if label == "success"
                    else Enrichment.NOT_REQUIRED
                )
            }
            if crash_published:
                update["derived_path"] = derived.relative_to(
                    output_dir
                ).as_posix()
            return fp, entry.model_copy(update=update)
        # Unverifiable artifact: discard and rebuild from raw offline.
        derived.unlink()
        fixed = entry.model_copy(
            update={"derived_path": None, "enrichment": Enrichment.PENDING}
        )
        return fp, fixed

    return fp, None


async def run_recovery(
    output_dir: Path, index: IndexStore, memories_by_fp: dict | None = None
) -> list[str]:
    """Sweep temps and reconcile rows. Returns removed temp paths."""
    output_dir = Path(output_dir)
    memories_by_fp = memories_by_fp or {}
    removed = cleanup_temps(output_dir)

    updates = {}
    for fp, entry in list(index.entries.items()):
        _, new_entry = _reconcile_one(
            entry, output_dir, memories_by_fp.get(fp)
        )
        if new_entry is not None:
            updates[fp] = new_entry
    if updates:
        await index.replace_many(updates)
    return removed
