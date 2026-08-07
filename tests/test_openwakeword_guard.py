# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract for the single owner of the openWakeWord import guard.

Whether the stub actually keeps scikit-learn out of a real openWakeWord
import is proved in tests/test_lazy_imports.py (which needs the package
installed). These tests pin the parts that hold everywhere: the stub's
shape, idempotence, and the loud failure when the guard arrives late.
"""
from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator

import pytest

from jasper import openwakeword_guard
from jasper.openwakeword_guard import ensure_openwakeword_import_safe

_VERIFIER = "openwakeword.custom_verifier_model"


@pytest.fixture
def clean_verifier_slot() -> Iterator[None]:
    """Run with an empty sys.modules slot; restore whatever was there."""
    sentinel = object()
    previous = sys.modules.get(_VERIFIER, sentinel)
    sys.modules.pop(_VERIFIER, None)
    try:
        yield
    finally:
        sys.modules.pop(_VERIFIER, None)
        if previous is not sentinel:
            sys.modules[_VERIFIER] = previous  # type: ignore[assignment]


def test_guard_installs_stub_preserving_openwakeword_all(
    clean_verifier_slot: None,
) -> None:
    """openwakeword's __init__ does `from .custom_verifier_model import
    train_custom_verifier`, so the stub must carry that name or the import
    it is meant to make cheap fails outright."""
    ensure_openwakeword_import_safe()

    stub = sys.modules[_VERIFIER]
    assert isinstance(stub, types.ModuleType)
    assert hasattr(stub, "train_custom_verifier")
    assert stub.train_custom_verifier is None


def test_guard_is_idempotent_and_quiet(
    clean_verifier_slot: None, caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_openwakeword_import_safe()
    installed = sys.modules[_VERIFIER]

    with caplog.at_level(logging.WARNING, logger=openwakeword_guard.__name__):
        ensure_openwakeword_import_safe()

    assert sys.modules[_VERIFIER] is installed
    assert caplog.records == []


def test_guard_warns_when_openwakeword_was_already_imported(
    clean_verifier_slot: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """A late guard cannot recover the ~78 MiB, so it must not be silent.

    This is the exact state the guard exists to prevent: something imported
    openWakeWord first, so the real custom_verifier_model (and scikit-learn
    behind it) is already resident.
    """
    real = types.ModuleType(_VERIFIER)  # stands in for the *real* module
    sys.modules[_VERIFIER] = real

    with caplog.at_level(logging.WARNING, logger=openwakeword_guard.__name__):
        ensure_openwakeword_import_safe()

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "event=openwakeword.guard_late" in caplog.records[0].getMessage()
    # And it must leave the real module alone. Overwriting a live module with
    # the stub would break `openwakeword.custom_verifier_model` for anything
    # that imports it later, to buy back memory that is already spent.
    assert sys.modules[_VERIFIER] is real
