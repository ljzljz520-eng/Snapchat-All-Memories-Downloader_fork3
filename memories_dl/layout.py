"""On-disk layout and the recognizable temp-file naming patterns.

Layout under the output directory::

    <output>/
      <YYYY-MM-DD_HH-MM-SS><ext>          user-visible derived artifact
      .memories/
        index.json                        minimal index
        .index.json.tmp-<uuid>            index temp
        raw/<xx>/<sha256><ext>            read-only content-addressed raw
        raw/<xx>/.rawtmp-<uuid>           raw temp (same dir as the object)
      .<YYYY-MM-DD_HH-MM-SS><ext>.tmp-<uuid>   derived temp (same dir as target)

Every temp file matches one fixed pattern that recovery sweeps; nothing else
may carry these names.
"""

import re
import uuid
from pathlib import Path

META_DIRNAME = ".memories"
RAW_DIRNAME = "raw"
INDEX_FILENAME = "index.json"

INDEX_TMP_PREFIX = ".index.json.tmp-"
RAW_TMP_PREFIX = ".rawtmp-"
DERIVED_TMP_MARK = ".tmp-"

INDEX_VERSION = 1


def meta_dir(output_dir: Path) -> Path:
    return output_dir / META_DIRNAME


def raw_root(output_dir: Path) -> Path:
    return meta_dir(output_dir) / RAW_DIRNAME


def index_path(output_dir: Path) -> Path:
    return meta_dir(output_dir) / INDEX_FILENAME


def new_index_tmp(output_dir: Path) -> Path:
    return meta_dir(output_dir) / f"{INDEX_TMP_PREFIX}{uuid.uuid4().hex}"


_UUID_HEX = r"[0-9a-f]{32}"
_INDEX_TMP_RE = re.compile(rf"^\.index\.json\.tmp-{_UUID_HEX}$")
_RAW_TMP_RE = re.compile(rf"^{RAW_TMP_PREFIX}{_UUID_HEX}$")
_DERIVED_TMP_RE = re.compile(
    rf"^\.\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}"
    rf"\.(?:jpg|jpeg|mp4)\.tmp-{_UUID_HEX}$",
    re.IGNORECASE,
)


def is_index_tmp(path: Path) -> bool:
    return path.parent.name == META_DIRNAME and bool(
        _INDEX_TMP_RE.match(path.name)
    )


def raw_object_relpath(digest: str, ext: str) -> str:
    """Project-relative path (relative to output dir) of a raw object."""
    return f"{META_DIRNAME}/{RAW_DIRNAME}/{digest[:2]}/{digest}{ext}"


def raw_object_path(output_dir: Path, digest: str, ext: str) -> Path:
    return output_dir / raw_object_relpath(digest, ext)


def new_raw_tmp(output_dir: Path, digest: str) -> Path:
    shard = raw_root(output_dir) / digest[:2]
    return shard / f"{RAW_TMP_PREFIX}{uuid.uuid4().hex}"


def is_raw_tmp(path: Path) -> bool:
    return bool(_RAW_TMP_RE.match(path.name))


def derived_target(output_dir: Path, stem: str, ext: str) -> Path:
    return output_dir / f"{stem}{ext}"


def new_derived_tmp(output_dir: Path, target: Path) -> Path:
    """Temp file in the very same directory as the publication target."""
    return target.parent / f".{target.name}{DERIVED_TMP_MARK}{uuid.uuid4().hex}"


def is_derived_tmp(path: Path) -> bool:
    # Strict inverse of ``new_derived_tmp``: a dotfile alone is never enough.
    return bool(_DERIVED_TMP_RE.match(path.name))
