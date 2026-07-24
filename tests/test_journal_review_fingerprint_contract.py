# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the shared log-message `fingerprint()` awk function.

`scripts/journal-review.sh` reuses the exact `fingerprint()` awk function
that `scripts/fetch-pi-logs.sh`'s `write_log_noise_summary()` defines — the
timestamp/PID/number-normalizing fingerprinter — rather than re-implementing
it (AGENTS.md anti-duplication rule). Extraction to a shared awk lib is not
warranted (a 6-line pure function used in two different awk-embedding
contexts), so the copy carries a "kept byte-for-byte in sync" comment. This
pins that comment: if either copy's transformation logic drifts, the two
would silently produce different fingerprints (and the digests would
disagree), so this fails CI instead.

Mirrors the multi-writer contract shape of
tests/test_wifi_profile_hardening_contract.py: static text analysis only,
normalizing away the incidental indentation each embedding context forces
while pinning the tokens (regexes, replacements, operations) exactly.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The two scripts that must carry byte-identical fingerprint() logic.
_FETCH = ROOT / "scripts/fetch-pi-logs.sh"
_REVIEW = ROOT / "scripts/journal-review.sh"

# The full awk function, signature through closing brace. The body contains
# `{4}`/`{2}` interval quantifiers (curly braces mid-line) but no line-leading
# `}` until the function closes, so anchoring the end on `return line` + a
# brace-only line is unambiguous.
_FINGERPRINT_RE = re.compile(
    r"function fingerprint\(line\)\s*\{.*?return line\s*\n\s*\}",
    re.S,
)


def _extract_fingerprint(path: Path) -> str:
    """Extract the fingerprint() awk function and normalize away the
    incidental per-line indentation the two embedding contexts force, so the
    comparison pins the transformation tokens, not whitespace."""
    text = path.read_text(encoding="utf-8")
    m = _FINGERPRINT_RE.search(text)
    assert m is not None, f"fingerprint() awk function not found in {path}"
    lines = [ln.strip() for ln in m.group(0).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def test_fingerprint_awk_bodies_are_byte_identical():
    fetch = _extract_fingerprint(_FETCH)
    review = _extract_fingerprint(_REVIEW)
    assert fetch == review, (
        "The fingerprint() awk function has DRIFTED between "
        "scripts/fetch-pi-logs.sh and scripts/journal-review.sh. "
        "journal-review.sh reuses it verbatim (see its comment); keep the two "
        "byte-identical (modulo indentation) or their log-noise fingerprints "
        "will silently disagree.\n\n"
        f"fetch-pi-logs.sh:\n{fetch}\n\njournal-review.sh:\n{review}"
    )


def test_fingerprint_extraction_is_nonempty_and_complete():
    """Guard the guard: prove the extractor actually captured the real body
    (all five normalizing operations), so a regex that silently matched
    nothing can't make the equality assertion vacuously pass."""
    body = _extract_fingerprint(_REVIEW)
    for needle in (
        "function fingerprint(line) {",
        'gsub(/\\[[0-9]+\\]/, "[#]", line)',
        'gsub(/[0-9]+/, "#", line)',
        'gsub(/[[:space:]]+/, " ", line)',
        "return line",
    ):
        assert needle in body, f"extractor missed `{needle}` — regex too loose"
