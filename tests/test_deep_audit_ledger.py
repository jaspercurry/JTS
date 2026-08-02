# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integrity checks for the live deep-audit findings ledger."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "REVIEW-deep-audit-ledger.md"
SUMMARY_STATUSES = (
    "open",
    "fixed",
    "in-progress",
    "mooted",
    "reversed",
    "deferred",
)


def _normalise_status(status: str) -> str:
    if status.startswith("open"):
        return "open"
    return status


def _ledger_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| DA-"):
            continue
        fields = [field.strip() for field in line.split("|")]
        finding_id = fields[1]
        status = fields[5].removeprefix("**").removesuffix("**")
        rows.append((finding_id, status))
    return rows


def test_status_summary_matches_live_ledger_rows():
    text = LEDGER.read_text(encoding="utf-8")
    rows = _ledger_rows(text)
    actual = Counter(_normalise_status(status) for _, status in rows)
    reported = {
        status: int(
            re.search(
                rf"^- \*\*{re.escape(status)}\*\*: (\d+)",
                text,
                flags=re.MULTILINE,
            ).group(1)
        )
        for status in SUMMARY_STATUSES
    }

    assert reported == {status: actual[status] for status in SUMMARY_STATUSES}


def test_finding_ids_are_unique():
    rows = _ledger_rows(LEDGER.read_text(encoding="utf-8"))
    ids = [finding_id for finding_id, _ in rows]

    assert len(ids) == len(set(ids))
