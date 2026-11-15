"""Deterministic regression for duplicate terminal receipts."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from receipt_race import (
    TERMINAL_OUTCOMES,
    ReceiptHooks,
    ReceiptWriter,
    list_terminal_receipts,
)


def test_single_claim_creates_one_receipt(tmp_path: Path) -> None:
    writer = ReceiptWriter(tmp_path)
    assert writer.claim_terminal("stage-a", "done") is True

    receipts = list_terminal_receipts(tmp_path, "stage-a")
    assert len(receipts) == 1
    assert receipts[0].read_text(encoding="utf-8") == "done"


def test_duplicate_same_outcome_second_claim_loses(tmp_path: Path) -> None:
    writer = ReceiptWriter(tmp_path)
    assert writer.claim_terminal("stage-a", "timeout") is True
    assert writer.claim_terminal("stage-a", "timeout") is False

    receipts = list_terminal_receipts(tmp_path, "stage-a")
    assert len(receipts) == 1
    assert receipts[0].read_text(encoding="utf-8") == "timeout"


def test_cross_outcome_second_claim_loses_without_overwrite(tmp_path: Path) -> None:
    writer = ReceiptWriter(tmp_path)
    assert writer.claim_terminal("stage-a", "done") is True
    assert writer.claim_terminal("stage-a", "timeout") is False

    receipts = list_terminal_receipts(tmp_path, "stage-a")
    assert len(receipts) == 1
    assert receipts[0].name == "stage-a.terminal"
    assert receipts[0].read_text(encoding="utf-8") == "done"


def test_existing_terminal_receipt_cannot_be_overwritten(tmp_path: Path) -> None:
    preexisting = tmp_path / "stage-a.terminal"
    preexisting.write_text("timeout", encoding="utf-8")

    writer = ReceiptWriter(tmp_path)
    assert writer.claim_terminal("stage-a", "done") is False
    assert preexisting.read_text(encoding="utf-8") == "timeout"
    assert list_terminal_receipts(tmp_path, "stage-a") == [preexisting]


def test_failed_write_releases_claim_so_retry_can_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ReceiptWriter(tmp_path)

    def boom(_fd: int, _data: bytes) -> int:
        raise OSError("simulated partial/failed write")

    with monkeypatch.context() as patched:
        patched.setattr(os, "write", boom)
        with pytest.raises(OSError, match="simulated partial/failed write"):
            writer.claim_terminal("stage-a", "done")

    assert list_terminal_receipts(tmp_path, "stage-a") == []
    assert writer.claim_terminal("stage-a", "timeout") is True
    receipts = list_terminal_receipts(tmp_path, "stage-a")
    assert len(receipts) == 1
    assert receipts[0].read_text(encoding="utf-8") == "timeout"


def test_completion_and_watchdog_share_one_writer_and_lock(tmp_path: Path) -> None:
    """Demonstrated usage: both threads close over one ReceiptWriter / Lock."""
    writer = ReceiptWriter(tmp_path)
    barrier = threading.Barrier(parties=2, timeout=5)
    observed: list[tuple[int, int]] = []
    guard = threading.Lock()

    def worker(outcome: str) -> None:
        barrier.wait()
        with guard:
            observed.append((id(writer), id(writer._lock)))
        writer.claim_terminal("stage-a", outcome)

    threads = [
        threading.Thread(target=worker, args=("done",), name="completion"),
        threading.Thread(target=worker, args=("timeout",), name="watchdog"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{thread.name} did not finish"

    assert len(observed) == 2
    assert observed[0] == observed[1]
    assert observed[0] == (id(writer), id(writer._lock))
    assert len(list_terminal_receipts(tmp_path, "stage-a")) == 1


def test_competing_done_and_timeout_create_only_one_terminal_receipt(
    tmp_path: Path,
) -> None:
    """Force both threads past the pre-create hook, then both create.

    On the buggy writer, ``done`` and ``timeout`` use different paths, so
    both creations succeed and two terminal receipts remain.

    ``before_create`` is outside any claim lock so this barrier remains
    deadlock-free after a locked fix.
    """
    barrier = threading.Barrier(parties=2, timeout=5)
    writer = ReceiptWriter(tmp_path, hooks=ReceiptHooks(before_create=barrier.wait))

    results: list[tuple[str, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(outcome: str) -> None:
        try:
            won = writer.claim_terminal("stage-a", outcome)
            with lock:
                results.append((outcome, won))
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("done",), name="completion"),
        threading.Thread(target=worker, args=("timeout",), name="watchdog"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{thread.name} did not finish"

    assert errors == []
    assert sorted(outcome for outcome, _ in results) == ["done", "timeout"]

    receipts = list_terminal_receipts(tmp_path, "stage-a")
    assert len(receipts) == 1, (
        "expected exactly one terminal receipt for the stage; "
        f"found {len(receipts)}: {[path.name for path in receipts]}"
    )
    content = receipts[0].read_text(encoding="utf-8")
    assert content in TERMINAL_OUTCOMES
    assert sum(1 for _, won in results if won) == 1


@pytest.mark.parametrize("outcome", sorted(TERMINAL_OUTCOMES))
def test_invalid_stage_rejected(tmp_path: Path, outcome: str) -> None:
    writer = ReceiptWriter(tmp_path)
    with pytest.raises(ValueError):
        writer.claim_terminal("../x", outcome)
