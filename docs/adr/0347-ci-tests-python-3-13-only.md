# ADR-0347: CI tests Python 3.13 only

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** The Pi runs Python 3.13, the interpreter PiOS Trixie ships.
  The 2026-08 right-sizing cut CI's 3.11/3.12/3.13 test matrix to 3.13 as a
  speed measure and left restoring it as an owner call. It left behind a
  single-entry matrix, a `pytest` aggregate job that only re-checked the
  policy preflight and that matrix, and a 3.11 floor (`requires-python`,
  ruff's target, mypy's `python_version`) that nothing tested. The owner
  decided on #5643 (D-32).
- **Decision:** CI runs the suite on Python 3.13 only, the interpreter the Pi
  runs; there is no version matrix. `requires-python`, ruff's
  `target-version` and mypy's `python_version` all floor at 3.13.
- **Consequences:** One `pytest` job runs `scripts/test-merge` after the
  routing-policy preflight, and `ci` stays the only required check. Code may
  use 3.13 language and stdlib features, and guidance that held only on
  CPython ≤ 3.11 (`asyncio.wait_for` swallowing a same-tick cancellation) no
  longer binds. A development venv needs 3.13 or newer. Needing another
  interpreter re-opens this with a new ADR. Rejected: keeping the 3.11 floor
  under 3.13-only CI (it claims support no check backs) and restoring the
  matrix (runner time for interpreters no speaker runs).
