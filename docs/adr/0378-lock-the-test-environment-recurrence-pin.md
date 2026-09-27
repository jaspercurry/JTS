# ADR-0378: Lock the test-environment recurrence pin

- **Date:** 2026-09-27
- **Status:** Accepted. Supersedes (partial)
  [ADR-0351](0351-a-source-scan-test-stays-only-with-a-non-negotiable-tie.md)
  Decision §2, the locked source-test set.

## Context

The existing test below records a repeated failure: test-environment setup
commands omitted runtime extras, so a clean checkout failed during test
collection. Fixing only one instruction left the same failure in other setup
paths.

The test checks the contributor quick start, the wrong-Python rebuild hint,
and the test lanes' interpreter-error help. It also checks the pip fallback.
Its docstring records the incident and recurrence, but ADR-0351's locked set
omitted it.

## Decision

The locked set contains 21 tests: retain all 20 listed in ADR-0351 Decision §2
and add:

- **Recurrence:** `tests/test_build_and_ci_contracts.py::test_documented_venv_build_commands_install_test_runtime_extras`.

The same lock applies: agents must not weaken or delete these tests without
a superseding ADR. A change that moves a checked surface carries its assertions
with it.

All other provisions of ADR-0351 remain in force. This decision locks the
existing test and adds no checks or runtime code.
