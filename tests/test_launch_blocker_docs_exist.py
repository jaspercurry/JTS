# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin launch-blocker documents after incident #636: a merge dropped PRIVACY.md."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        "LICENSE",
        "NOTICE",
        "SECURITY.md",
        "docs/privacy.md",
        ".github/CODE_OF_CONDUCT.md",
        ".github/CONTRIBUTING.md",
    ],
)
def test_launch_blocker_doc_exists_and_is_nonempty(relative_path: str) -> None:
    path = ROOT / relative_path
    assert path.is_file()
    assert path.stat().st_size > 0


def test_privacy_doc_is_linked_from_readme_atlas() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\]\(docs/privacy\.md\)", readme)
