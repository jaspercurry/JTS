# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: launch-blocker governance docs must physically exist in the repo.

PRIVACY.md was authored (#636) but a merge silently dropped it from `main`, so
the repo went weeks with no privacy disclosure while believing one had shipped.
This pins the launch-blocker docs so a future merge can't drop one without
turning CI red. See the 2026-06 OSS due-diligence finding
"reported-done-but-not-landed".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LAUNCH_BLOCKER_DOCS = [
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "PRIVACY.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
]


@pytest.mark.parametrize("name", LAUNCH_BLOCKER_DOCS)
def test_launch_blocker_doc_exists_and_nonempty(name: str) -> None:
    path = ROOT / name
    assert path.is_file(), f"{name} is missing from the repo root (launch-blocker doc)"
    assert path.stat().st_size > 0, f"{name} exists but is empty"


def test_privacy_doc_is_linked_from_readme_atlas() -> None:
    """PRIVACY.md must be linked from README's documentation map — README is the
    doc atlas and ships no orphan docs. Match any markdown link whose target is
    PRIVACY.md so a reword of the link text doesn't false-fail."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\]\(PRIVACY\.md\)", readme), (
        "README documentation map must link to PRIVACY.md"
    )
