"""Terminal receipt writer with a single stage-level claim.

Guarantees (same process, shared ``ReceiptWriter`` instance)
-----------------------------------------------------------
* ``done`` and ``timeout`` compete for one canonical path:
  ``{stage}.terminal``.
* A ``threading.Lock`` serializes the in-process claim decision.
* ``O_CREAT | O_EXCL`` refuses to open an existing receipt, so a loser
  cannot overwrite a winner's file.

Non-guarantees
--------------
* Not crash-safe: a crash after exclusive create and before a complete
  write can leave a partial or empty claim. Cleanup ``unlink`` runs only
  on the exception path of the creating call; incomplete receipts are not
  repaired automatically and can block further claims.
* ``close`` without ``fsync`` is not a durability / power-loss guarantee.
* Exclusive create is an atomic *claim*; it is not exactly-once execution
  of arbitrary side effects around the claim.
* The lock is per writer instance. Two instances on the same directory
  do not share a decision point.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

TERMINAL_OUTCOMES = frozenset({"done", "timeout"})
TERMINAL_SUFFIX = ".terminal"


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
        self._lock = threading.Lock()

    def claim_terminal(self, stage: str, outcome: str) -> bool:
        """Attempt to record the sole terminal outcome for ``stage``.

        Returns True if this call created the canonical receipt, False if
        the stage was already claimed. Completion and watchdog must both
        call this function.
        """
        _validate_stage(stage)
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"invalid terminal outcome: {outcome!r}")

        path = self._directory / f"{stage}{TERMINAL_SUFFIX}"

        # Synchronization for tests must stay outside the lock. A barrier
        # here cannot deadlock two claimers; a barrier under the lock can.
        self._hooks.before_create()

        with self._lock:
            return self._create_exclusive(path, outcome)

    def _create_exclusive(self, path: Path, outcome: str) -> bool:
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
            # Safe only because this call exclusively created ``path``.
            # Never unlink a receipt we did not just create.
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

    Includes legacy outcome-specific names from the buggy Phase 1 writer
    and the canonical ``{stage}.terminal`` claim so the regression test
    stays stable and would still flag a regression that recreates dual
    outcome files.
    """
    root = Path(directory)
    candidates = [
        root / f"{stage}.done",
        root / f"{stage}.timeout",
        root / f"{stage}{TERMINAL_SUFFIX}",
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
