"""Terminal receipt writer (Phase 1: deliberately buggy).

BUG
---
Completion and watchdog record terminal outcomes on *separate* paths
(``{stage}.done`` vs ``{stage}.timeout``). There is no shared in-process
decision and no canonical stage-level claim, so both threads can succeed
and leave two terminal receipts for one stage.

Hooks
-----
``ReceiptHooks.before_create`` runs immediately before filesystem creation
and must never execute while holding a claim lock. Test barriers belong
here so a later locked implementation cannot deadlock two waiters.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

TERMINAL_OUTCOMES = frozenset({"done", "timeout"})


class ReceiptHooks:
    """Optional synchronization points for deterministic tests."""

    def __init__(self, before_create: Callable[[], None] | None = None) -> None:
        self.before_create = before_create or (lambda: None)


class ReceiptWriter:
    """Writes terminal stage receipts under a directory."""

    def __init__(
        self,
        directory: Path | str,
        hooks: ReceiptHooks | None = None,
    ) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._hooks = hooks or ReceiptHooks()

    def claim_terminal(self, stage: str, outcome: str) -> bool:
        """Attempt to record a terminal outcome for ``stage``.

        Returns True if this call created a receipt file, False if that
        outcome-specific path already existed.
        """
        _validate_stage(stage)
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"invalid terminal outcome: {outcome!r}")

        # BUG: outcome-specific path — done and timeout do not compete.
        path = self._directory / f"{stage}.{outcome}"

        self._hooks.before_create()

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY

        try:
            fd = os.open(os.fspath(path), flags)
        except FileExistsError:
            return False

        try:
            _write_all(fd, outcome.encode("utf-8"))
        except Exception:
            os.close(fd)
            # This call created the file exclusively; remove our partial claim.
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)
            return True


def list_terminal_receipts(directory: Path | str, stage: str) -> list[Path]:
    """Existing terminal receipt paths for ``stage``.

    Includes both the buggy outcome-specific names and a future canonical
    ``{stage}.terminal`` claim so the regression test stays stable across
    the fix without rewriting assertions.
    """
    root = Path(directory)
    candidates = [
        root / f"{stage}.done",
        root / f"{stage}.timeout",
        root / f"{stage}.terminal",
    ]
    return [path for path in candidates if path.is_file()]


def _validate_stage(stage: str) -> None:
    if not stage or stage in {".", ".."} or "/" in stage or "\\" in stage:
        raise ValueError(f"invalid stage name: {stage!r}")


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written == 0:
            raise OSError("short write while recording terminal receipt")
        offset += written
