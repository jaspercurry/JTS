# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import yaml

from jasper.atomic_io import atomic_write_text

logger = logging.getLogger("jasper.active_speaker.camilla_yaml")


def _reserialize_keeping_header(text: str, payload: Mapping[str, Any]) -> str:
    """``payload`` re-serialised under ``text``'s own comment header.

    The header carries the source marker every graph door reads the emitter's
    identity from; CamillaDSP's dialect below it is what a decorated graph is
    then proved in.
    """
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    return header + "\n" + yaml.safe_dump(dict(payload), sort_keys=False)


def _atomic_write_text(path: Path, text: str) -> None:
    # Active-speaker configs are read by both root-owned CamillaDSP helpers and
    # the non-root jasper-web commissioning route. Keep them group-readable.
    atomic_write_text(path, text, mode=0o640)
