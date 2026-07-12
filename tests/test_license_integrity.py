# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard the packaged Apache terms against substantive text drift."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_TERMS_START = "TERMS AND CONDITIONS"
_TERMS_END = "END OF TERMS AND CONDITIONS"


def _terms_tokens(path: Path) -> list[str]:
    text = path.read_text()
    start = text.index(_TERMS_START)
    end = text.index(_TERMS_END, start) + len(_TERMS_END)
    return text[start:end].split()


def test_packaged_apache_terms_match_canonical_spdx_reference() -> None:
    assert _terms_tokens(_ROOT / "LICENSE") == _terms_tokens(
        _ROOT / "LICENSES" / "Apache-2.0.txt",
    )
