"""Stage B: derived artifacts.

The read-only raw object is never modified. Derived bytes are built in an
independent temp file in the target directory, and publication happens only
after the written temp file has been re-parsed and verified to carry the
expected tags. Publication itself is a single atomic ``os.replace``.
"""

import io
import math
import os
from pathlib import Path

import exif

from . import layout
from .faults import inject
from .store import fsync_dir, read_raw, write_all

JPEG_EXTS = {".jpg", ".jpeg"}


class EnrichmentLabel:
    SUCCESS = "success"
    NOT_REQUIRED = "not_required"


class EnrichError(RuntimeError):
    """Enrichment failed at ``kind`` (parse | write | verify | publish)."""

    def __init__(self, kind: str, cause: BaseException | None = None):
        super().__init__(f"EXIF enrichment failed at {kind}")
        self.kind = kind
        self.__cause__ = cause


def is_jpeg(ext: str) -> bool:
    return ext.lower() in JPEG_EXTS


def decimal_to_dms(decimal: float) -> tuple[float, float, float]:
    degrees = int(abs(decimal))
    minutes_decimal = (abs(decimal) - degrees) * 60
    minutes = int(minutes_decimal)
    seconds = (minutes_decimal - minutes) * 60
    return (float(degrees), float(minutes), float(seconds))


def _dms_close(actual: tuple, expected: tuple) -> bool:
    return len(actual) == 3 and all(
        isinstance(a, (int, float)) and math.isclose(float(a), float(e), abs_tol=1e-6)
        for a, e in zip(actual, expected)
    )


def _write_tmp(tmp: Path, data: bytes) -> None:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(tmp: Path, target: Path, mtime: float) -> None:
    os.utime(tmp, (mtime, mtime))
    # Crash after this point and before the rename leaves only a verified temp;
    # the user-visible name never holds a half-written file.
    inject("derived_commit")
    os.replace(tmp, target)
    fsync_dir(target.parent)


def _cleanup(tmp: Path) -> None:
    tmp.unlink(missing_ok=True)


def publish_passthrough(
    *, output_dir: Path, raw_relpath: str, target: Path, mtime: float
) -> None:
    """Publish raw bytes unchanged as the derived artifact."""
    data = read_raw(output_dir, raw_relpath)
    tmp = layout.new_derived_tmp(output_dir, target)
    try:
        _write_tmp(tmp, data)
        inject("derived_write")
        _publish(tmp, target, mtime)
    except BaseException:
        _cleanup(tmp)
        raise


def _build_exif_bytes(raw_bytes: bytes, memory) -> bytes:
    try:
        inject("exif_parse")
        img = exif.Image(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise EnrichError("parse", exc)

    dt_str = memory.date.strftime("%Y:%m:%d %H:%M:%S")
    try:
        inject("exif_write")
        img.datetime_original = dt_str
        img.datetime_digitized = dt_str
        img.datetime = dt_str
        if memory.latitude is not None and memory.longitude is not None:
            img.gps_latitude = decimal_to_dms(memory.latitude)
            img.gps_latitude_ref = "N" if memory.latitude >= 0 else "S"
            img.gps_longitude = decimal_to_dms(memory.longitude)
            img.gps_longitude_ref = "E" if memory.longitude >= 0 else "W"
        return img.get_file()
    except EnrichError:
        raise
    except Exception as exc:
        raise EnrichError("write", exc)


def _verify(data: bytes, memory) -> None:
    """Re-parse the freshly written derived bytes and assert expected tags."""
    dt_str = memory.date.strftime("%Y:%m:%d %H:%M:%S")
    try:
        check = exif.Image(io.BytesIO(data))
        assert check.datetime_original == dt_str
        assert check.datetime_digitized == dt_str
        if memory.latitude is not None and memory.longitude is not None:
            assert _dms_close(
                tuple(check.gps_latitude), decimal_to_dms(memory.latitude)
            )
            assert check.gps_latitude_ref == (
                "N" if memory.latitude >= 0 else "S"
            )
            assert _dms_close(
                tuple(check.gps_longitude), decimal_to_dms(memory.longitude)
            )
            assert check.gps_longitude_ref == (
                "E" if memory.longitude >= 0 else "W"
            )
        else:
            assert not hasattr(check, "gps_latitude")
            assert not hasattr(check, "gps_longitude")
    except EnrichError:
        raise
    except Exception as exc:
        raise EnrichError("verify", exc)


def publish_exif(
    *, output_dir: Path, raw_relpath: str, target: Path, memory, mtime: float
) -> None:
    """Build, verify, then atomically publish the EXIF-derived artifact."""
    raw_bytes = read_raw(output_dir, raw_relpath)
    tmp = layout.new_derived_tmp(output_dir, target)
    try:
        enriched = _build_exif_bytes(raw_bytes, memory)
        if enriched[:2] != b"\xff\xd8":
            raise EnrichError("write", ValueError("output is not a JPEG"))
        _write_tmp(tmp, enriched)
        # Verify the bytes that actually landed on disk, not the buffer.
        on_disk = tmp.read_bytes()
        if on_disk != enriched:
            raise EnrichError(
                "verify", ValueError("temp file bytes differ from enriched buffer")
            )
        _verify(on_disk, memory)
        inject("derived_write")
        _publish(tmp, target, mtime)
    except BaseException:
        _cleanup(tmp)
        raise


def _jpeg_structurally_complete(data: bytes) -> bool:
    """Walk JPEG markers: every declared segment fits and the stream ends EOI.

    This rejects truncated downloads / SOI-prefixed garbage even when their
    prefix happens to contain a parseable APP1 tag segment.
    """
    if data[:2] != b"\xff\xd8":
        return False
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            return False
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return False
        marker = data[i]
        i += 1
        if marker == 0xD9:  # EOI: nothing may follow
            return i == n
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # standalone markers without a length field
        if marker == 0xDA:  # SOS: skip entropy-coded data (FF00/RSTn stuffed)
            if i + 2 > n:
                return False
            seg_len = int.from_bytes(data[i : i + 2], "big")
            if seg_len < 2 or i + seg_len > n:
                return False
            i += seg_len
            while i + 1 < n:
                if data[i] == 0xFF and data[i + 1] != 0x00 and not (
                    0xD0 <= data[i + 1] <= 0xD7
                ):
                    break
                i += 1
            continue
        if i + 2 > n:
            return False
        seg_len = int.from_bytes(data[i : i + 2], "big")
        if seg_len < 2 or i + seg_len > n:
            return False
        i += seg_len
    return False


def inspect_derived(data: bytes, is_jpeg_file: bool, memory=None) -> str | None:
    """Classify derived bytes for recovery reconciliation.

    For JPEGs this is strict: bytes that are not byte-identical to the raw
    object only reach a terminal ``"success"`` state when the file is a
    structurally complete JPEG carrying the expected EXIF tags. Anything
    unreadable, tagless or truncated returns ``None`` so recovery discards
    it and rebuilds from the (verified) raw object. Tagless raw-passthrough
    JPEGs are recognised earlier via digest equality, not here.
    """
    if not is_jpeg_file:
        return EnrichmentLabel.NOT_REQUIRED
    if not _jpeg_structurally_complete(data):
        return None
    try:
        img = exif.Image(io.BytesIO(data))
        dto = img.datetime_original
    except Exception:
        return None
    if not dto:
        return None
    if memory is not None:
        try:
            _verify(data, memory)
        except EnrichError:
            return None
    return EnrichmentLabel.SUCCESS
