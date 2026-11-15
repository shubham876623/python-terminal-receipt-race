# Terminal receipt race demo

Minimal Python 3.12 demonstration of a same-process race between a **completion** thread and a **watchdog** thread that both try to record a terminal stage outcome (`done` or `timeout`).

This repository is a standalone technical sample. It is **not** Wolverg production code.

## The bug

The Phase 1 writer stored outcomes on **separate paths** (`{stage}.done` vs `{stage}.timeout`) with **no shared decision**. Both threads could succeed, leaving two terminal receipts for one stage.

## Deterministic reproduction

`tests/test_terminal_receipt_race.py` forces the interleaving with `threading.Barrier` via `ReceiptHooks.before_create`:

1. Both threads reach the pre-create hook.
2. The barrier releases them together.
3. Both attempt to create a terminal receipt.

There are **no** `sleep()` calls, random delays, or “run it 1,000 times” loops.

The demonstrated usage constructs **one** `ReceiptWriter` and has both threads call `claim_terminal` on that instance, so they share the same `threading.Lock`.

## The fix

`ReceiptWriter.claim_terminal` is the single claim function for both outcomes:

| Mechanism | Role |
|-----------|------|
| Canonical path `{stage}.terminal` | `done` and `timeout` compete for one stage-level claim |
| `threading.Lock` | Serializes the in-process decision (per writer instance) |
| `O_CREAT \| O_EXCL` | Exclusive create; losers cannot overwrite an existing receipt |

`before_create` runs **outside** the lock so test barriers cannot deadlock two waiters.

## Failed-write cleanup

If this call’s exclusive `open` succeeds and the subsequent write fails, the writer closes the fd and **unlinks only that path**. That unlink is safe solely because **this call** just created the file with `O_EXCL`. The code never deletes a receipt on `FileExistsError` and never removes a claim created by another winner.

## Guarantees and limitations

**Guarantees (same process, shared `ReceiptWriter` instance):**

- At most one canonical terminal receipt file for a stage from successful claims.
- Exactly one winning `claim_terminal` call when threads contend through that shared writer.
- Existing terminal receipts are not overwritten.

**Not guaranteed:**

- Crash-safe or durable publication (no `fsync`; a crash after create and before a complete write can leave an incomplete claim that blocks retries).
- Exactly-once execution of surrounding business side effects.
- Coordination across multiple `ReceiptWriter` instances, processes, or hosts (`O_EXCL` still prevents overwrite; the lock is not shared across instances).

## Test-first history

| Commit | Intent | Expected pytest |
|--------|--------|-----------------|
| `e1b753b` | Buggy writer + failing race regression | **fail** |
| `5803567` | Lock + canonical `O_EXCL` claim | **pass** |
| later | README + GitHub Actions (+ shared-writer check) | **pass** |

Do not rewrite or amend the failing-test commit.

## Local development

```bash
py -3.12 -m pip install pytest
py -3.12 -m pytest -v --tb=short
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs pytest on Python 3.12 for push/PR.

### Obtaining genuine failing and passing Actions runs

After the repository is public on GitHub (not done until explicitly approved):

**Passing run (fix commit on `main`):**

1. Push `main` (includes this workflow file).
2. Open the Actions run for that push — pytest must be green on HEAD.

**Failing run (Phase 1 tree, without rewriting history):**

Preferred — `workflow_dispatch` from `main` (workflow file stays on `main`; checkout uses the old SHA):

1. Actions → **CI** → **Run workflow**.
2. Set `checkout_ref` to `e1b753b1a41319dfef0b59848999dc4be6b9acf9`.
3. Run. Pytest must fail on `test_competing_done_and_timeout_create_only_one_terminal_receipt`.

Alternative — temporary branch (Phase 1 code + workflow only):

```bash
git switch -c ci/phase1-fail e1b753b1a41319dfef0b59848999dc4be6b9acf9
git cherry-pick <docs-ci-commit-sha>   # adds .github/workflows/ci.yml + README only if clean
git push -u origin ci/phase1-fail
```

If cherry-pick conflicts because the docs commit also touches tests, copy only `.github/workflows/ci.yml` onto the branch in a new commit instead. Delete the temporary branch after capturing the Actions URL.

Never amend, rebase, or force-push to hide `e1b753b`.
