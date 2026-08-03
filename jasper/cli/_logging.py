# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared logging setup for interactive maintenance CLIs."""
from __future__ import annotations

import logging

CLI_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_verbose_logging(*, verbose: bool) -> None:
    """Use DEBUG for ``--verbose`` and WARNING otherwise."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format=CLI_LOG_FORMAT,
    )
