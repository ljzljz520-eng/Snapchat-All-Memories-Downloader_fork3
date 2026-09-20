"""Centralized fault injection points.

Production behaviour is a pure no-op unless the ``MEMORIES_FAULT`` environment
variable is set. Tests (including subprocess crash tests) arm points with::

    MEMORIES_FAULT="<point>:<mode>"
    MEMORIES_FAULT="<point>:<mode>;<other_point>:<mode>"

Modes:
    exit:    terminate the process immediately (``os._exit(99)``), simulating
             a hard crash with no cleanup;
    enospc:  raise ``OSError(ENOSPC)``, simulating a full disk;
    error:   raise a generic ``RuntimeError``.
"""

import errno
import os

ENV_VAR = "MEMORIES_FAULT"
EXIT_CODE = 99


class InjectedFault(RuntimeError):
    """Raised for the ``error`` mode; carries the fault point name."""


def _parse() -> list[tuple[str, str]]:
    raw = os.environ.get(ENV_VAR, "")
    faults = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        point, sep, mode = item.partition(":")
        faults.append((point.strip(), mode.strip() if sep else "error"))
    return faults


def inject(point: str) -> None:
    """Trigger a fault when ``point`` is armed. Does nothing otherwise."""
    for armed_point, mode in _parse():
        if armed_point != point:
            continue
        if mode == "exit":
            # Hard stop: no ``finally`` blocks, no flushing, no atexit hooks.
            os._exit(EXIT_CODE)
        if mode == "enospc":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        raise InjectedFault(f"injected failure at {point}")
