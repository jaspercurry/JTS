# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Re-export of the logging bootstrap, for the parked tuning-zone CLIs only.

The bootstrap and its redacting filter live in :mod:`jasper.logging_setup`.
Delete this module when the parked tuning-zone CLIs adopt
``configure_logging`` (#3769 wave 10, after #4138) — the list of them is
``_ALLOWLIST`` in ``tests/test_logging_setup.py``.
"""
from __future__ import annotations

from ..logging_setup import LOG_FORMAT as CLI_LOG_FORMAT, configure_verbose_logging

__all__ = ["CLI_LOG_FORMAT", "configure_verbose_logging"]
