import errno

import pytest

from memories_dl.faults import InjectedFault, inject


def test_inject_is_noop_without_env():
    inject("anything")
    inject("raw_commit")


def test_enospc_only_fires_at_armed_point(monkeypatch):
    monkeypatch.setenv("MEMORIES_FAULT", "raw_commit:enospc")
    inject("other_point")  # silent
    with pytest.raises(OSError) as exc:
        inject("raw_commit")
    assert exc.value.errno == errno.ENOSPC


def test_error_mode(monkeypatch):
    monkeypatch.setenv("MEMORIES_FAULT", "exif_parse:error")
    with pytest.raises(InjectedFault):
        inject("exif_parse")


def test_multiple_points(monkeypatch):
    monkeypatch.setenv(
        "MEMORIES_FAULT", "raw_commit:enospc;derived_write:error"
    )
    with pytest.raises(OSError):
        inject("raw_commit")
    with pytest.raises(InjectedFault):
        inject("derived_write")
