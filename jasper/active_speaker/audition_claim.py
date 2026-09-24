# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether the running graph is an audition's, and forgetting the audition when
another writer replaces it.

A leaf, so the CamillaDSP controller can consult it on every graph swap without
importing the audition machinery (see ADR-0193 and ADR-0329).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path

from jasper.log_event import log_event

from .state_paths import audition_state_path

logger = logging.getLogger(__name__)

#: Set while an audition writes its own graph, so the swap does not retire it.
AUDITION_WRITE = ContextVar("audition_write", default=False)


def graph_replaced() -> None:
    """A graph swap retires any audition it did not come from."""
    if not AUDITION_WRITE.get():
        clear_audition_state()


def clear_audition_state(path: str | Path | None = None) -> None:
    try:
        audition_state_path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        # The graph is already back; a stranded record only mis-reports. Loud
        # rather than silent, because `jasper-audition status` would keep
        # claiming an audition.
        log_event(
            logger,
            "active_speaker.audition",
            level=logging.WARNING,
            action="clear_state",
            result="failed",
            error=type(exc).__name__,
        )
