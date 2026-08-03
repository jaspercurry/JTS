# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Focused contracts for optional AEC bridge engine isolation."""

from __future__ import annotations

from unittest.mock import Mock

from jasper.cli import aec_bridge


def test_optional_engine_process_preserves_successful_engine():
    engine = Mock()
    engine.process.return_value = b"clean"

    active, clean, error = aec_bridge._process_optional_engine(
        engine,
        b"mic",
        b"ref",
        failure_message="optional path failed: %s",
    )

    assert active is engine
    assert clean == b"clean"
    assert error is None


def test_optional_engine_process_disables_only_failed_leg(caplog):
    engine = Mock()
    engine.process.side_effect = RuntimeError("inference failed")

    with caplog.at_level("ERROR", logger=aec_bridge.logger.name):
        active, clean, error = aec_bridge._process_optional_engine(
            engine,
            b"mic",
            b"ref",
            failure_message="optional path failed: %s",
        )

    assert active is None
    assert clean == b""
    assert isinstance(error, RuntimeError)
    assert "optional path failed: inference failed" in caplog.text
