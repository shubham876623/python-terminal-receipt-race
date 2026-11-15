"""Deterministic regression for duplicate terminal receipts."""

from __future__ import annotations

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
