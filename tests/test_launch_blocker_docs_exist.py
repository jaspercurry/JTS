# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""See #636: a merge dropped the privacy disclosure after it was authored."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_privacy_doc_is_linked_from_readme_atlas() -> None:
    path = ROOT / "docs/privacy.md"
    assert path.is_file()
    assert path.stat().st_size > 0
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\]\(docs/privacy\.md\)", readme)
