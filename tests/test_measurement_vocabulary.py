# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement flow's household-facing vocabulary: the actor is the microphone.

Issue #1941 R4, design principle 6: *"The actor is the microphone. Never 'the
phone' in household-facing copy."* The instrument a household holds in front of
the speaker is whatever browser can reach the relay — the 2026-07-30 bench ran a
**UMIK-2** while the wizard told the household its *phone* had drifted (#1924).
A laptop is equally legal. So the copy names the microphone, or the measurement
page, and never the shape of the device.

**This guard is a curated constant list, deliberately NOT a repo-wide grep.**
Roughly 800 occurrences of "phone" across the tree are internal and must stay:
protocol keys (``phone-event``), wire error strings (``phone never armed``),
relay endpoints (``/phone-status``), element ids (``phone-mic-select``), mic-tier
vocabulary (``MIC_TIERS``), the ``phone-mic capture relay`` subsystem name in
docstrings, and every source-of-a-phone surface that genuinely means a phone
(Spotify Connect, Bluetooth pairing, AirPlay senders, OAuth hand-offs). A blanket
scan would either fail on all of those or be silenced into uselessness.

What it does instead: over an enumerated set of swept files it extracts **string
literals only** — never comments, never docstrings, never identifiers — and fails
on any standalone "phone"/"phones" that is not in an explicit allowlist of
non-copy literals. Adding household copy that says "phone" to a swept surface
therefore fails; renaming an internal identifier does not.

Extending this: a new measurement surface with household copy belongs in
``SWEPT_SURFACES``. A new protocol key or element id that must contain "phone"
belongs in ``ALLOWED_PHONE_LITERALS`` *with a reason*.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Standalone "phone"/"phones" only — never the "phone" inside "microphone",
# "headphone", "iPhone", or "smartphone".
PHONE_WORD_RE = re.compile(r"(?:^|[^A-Za-z])[Pp]hones?(?:[^A-Za-z]|$)")


# --- The swept surfaces -------------------------------------------------------
# Every file whose household-facing copy was swept for #1941 R4, grouped by the
# four clusters the requirement names. A file listed here is asserted to contain
# no "phone" string literal outside ALLOWED_PHONE_LITERALS below.

SWEPT_SURFACES: tuple[str, ...] = (
    # Cluster 1 — wizard verdict lines (the §5.10 reason registry and the
    # screen envelopes that render its verdicts).
    "jasper/active_speaker/crossover_v2_flow.py",
    "jasper/active_speaker/crossover_envelope_v2.py",
    "jasper/active_speaker/crossover_envelope.py",
    "jasper/active_speaker/baseline_profile.py",
    # Cluster 2 — failure / refusal copy.
    "jasper/capture_relay/session.py",
    "jasper/correction/failures.py",
    "jasper/correction/level_match.py",
    "jasper/web/correction_setup.py",
    "jasper/web/balance_flow.py",
    "jasper/web/sync_flow.py",
    # Cluster 3 — relay hand-off chrome (the speaker-side pages that hand the
    # household off to the measurement page, and the shared QR affordance).
    "jasper/web/correction_crossover_flow.py",
    "deploy/assets/correction/js/main.js",
    "deploy/assets/correction/js/crossover/main.js",
    "deploy/assets/shared/js/qr.js",
    "deploy/assets/sync/js/main.js",
    "deploy/assets/rooms/js/main.js",
    "deploy/assets/sound-profile/js/active-speaker-ui.js",
    # Cluster 4 — the capture page's setup screens (its Pi-side spec, and the
    # page module that renders them).
    "jasper/capture_relay/spec.py",
    "capture-page/js/main.js",
    "capture-page/js/constraints.js",
)


# --- Fragments that legitimately keep "phone" ---------------------------------
# Each key is an exact substring; an occurrence of "phone" is exempt only when it
# sits inside one of these. Fragments rather than whole literals because a
# wizard's page template is ONE multi-kilobyte literal — exempting the literal
# would exempt the page. The value is the reason the fragment is not copy.

ALLOWED_PHONE_FRAGMENTS: dict[str, str] = {
    # --- Wire / protocol vocabulary. Renaming these is a relay protocol change,
    # not a copy change, and both sides of the transport already agree on them.
    "awaiting_phone": "relay session state on the wire",
    "phone_capture_unavailable": "failure code in the /correction/ code table",
    "phone_feed_lost": "canonical snake_case refusal code (reason= log key)",
    "phone_never_armed": "canonical snake_case refusal code",
    "cancelled_before_phone_armed": "canonical snake_case refusal code",
    "phone_aborted": "capture-page abort reason posted to the relay",
    "capture_relay.phone_event_integrity_failed": "stable event= log name",
    # --- Raw ramp error strings: the KEYS of the refusal tables, i.e. the
    # verbatim `error` the audio-measurement ramp emits, pinned by
    # tests/test_audio_measurement_ramp.py. Only their VALUES are household copy.
    "no usable phone samples": "raw ramp error string (refusal-table key)",
    "phone never armed": "raw ramp error string (refusal-table key)",
    "phone feed lost": "raw ramp error string (refusal-table-prefix key)",
    # --- Diagnostic detail on CaptureTimeout / CaptureFailed, and internal
    # guard messages. These ride logs and support reads, not a household screen;
    # the household-facing sentence for the same conditions is
    # CAPTURE_INCOMPATIBLE_USER_MESSAGE and the §5.10 reason registry.
    "phone capture ended before host playback completed": "CaptureFailed detail",
    "phone recorder is no longer armed": "CaptureFailed detail",
    "phone aborted the capture (": "CaptureTimeout/CaptureFailed detail prefix",
    "phone never uploaded within ": "CaptureTimeout detail prefix",
    "phone never began the next capture within ": "CaptureTimeout detail prefix",
    "their phone ended the walk": "CaptureTimeout detail (same expression)",
    "authenticated phone event sequence moved backwards": "integrity guard message",
    "phone capture ownership changed during registration": "internal guard message",
    "phone capture ownership changed before evidence commit": "internal guard message",
    "phone capture ownership changed before upload": "internal guard message",
    "no matching phone capture is running": "internal guard message",
    "this phone capture cannot be stopped safely": "internal guard message",
    "phone capture is not ready to commit evidence": "internal guard message",
    "phone capture is not ready to finish": "internal guard message",
    "phone capture is not configured": "internal guard message",
    "a phone-mic relay capture is already in progress": "internal guard message",
    "phone capture failed": "console.warn diagnostic in the /correction/ module",
    # --- Operator-facing configuration text naming the subsystem by its name.
    "phone-mic relay capture is not configured": "operator remediation, names the "
    "subsystem",
    # --- DOM identity. Renaming it would break the page's own querySelector and
    # is invisible to the household.
    "phone-mic-select": "element id on the capture page",
    # --- The TWO deliberate household-facing exceptions, both argued where they
    # live. Each names a real phone-shaped affordance, not the instrument.
    "Scan with your phone's camera": "a QR code's only affordance is a camera — a "
    "laptop or UMIK-2 host clicks the link beside it; the canvas aria-label "
    "carries the device-agnostic noun",
    "If it's a phone, lay it flat screen up, point the bottom edge toward the "
    "speakers, and remove its case.": "a CONDITIONAL form-factor tip after the "
    "actor is already named as the microphone (#1941 R5/R7 territory)",
}


# Whole-literal exemptions, matched by EQUALITY rather than by substring. A bare
# token like "phone" cannot be a fragment — removing it as a substring would
# exempt every sentence that contains the word, which is the whole point of the
# guard. A literal whose entire value is one token is a key or an id, never copy.
ALLOWED_PHONE_LITERALS: dict[str, str] = {
    "phone": "mic-trust TIER key, mirrored from "
    "jasper.active_speaker.linearization_envelope.MIC_TIERS — a closed set of "
    "keys (reference / consumer / phone), never rendered as a sentence",
}


# --- Literal extraction -------------------------------------------------------


def _python_string_literals(source: str) -> list[str]:
    """Every ``str`` constant in ``source`` except module/class/function docstrings.

    Comments never reach the AST at all, so this cannot trip on decision
    archaeology in a ``#`` block — only on something a surface can render.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


_JS_STRING_RE = re.compile(
    r'"((?:[^"\\\n]|\\.)*)"' r"|'((?:[^'\\\n]|\\.)*)'" r"|`((?:[^`\\]|\\.)*)`",
    re.DOTALL,
)


def _blank_js_comments(source: str) -> str:
    """Replace comment bodies with spaces, preserving every other character.

    A character scanner rather than a line filter, because these modules carry
    long ``//`` rationale blocks *and* trailing comments, and a naive filter
    would either miss the trailing ones or eat code.
    """
    out = list(source)
    i, n = 0, len(source)
    state: str | None = None
    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state is None:
            if char == "/" and nxt in {"/", "*"}:
                state = "line" if nxt == "/" else "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if char in "\"'`":
                state = char
            i += 1
            continue
        if state == "line":
            if char == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = None
                i += 2
                continue
            if char != "\n":
                out[i] = " "
            i += 1
            continue
        if char == "\\":
            i += 2
            continue
        if char == state:
            state = None
        i += 1
    return "".join(out)


def _js_string_literals(source: str) -> list[str]:
    return [
        next(group for group in match.groups() if group is not None)
        for match in _JS_STRING_RE.finditer(_blank_js_comments(source))
    ]


def _excerpt(literal: str) -> str:
    """A readable pointer at the offending word, not the whole page template."""
    match = PHONE_WORD_RE.search(literal)
    assert match is not None
    flat = " ".join(literal[max(0, match.start() - 70) : match.end() + 70].split())
    return f"…{flat}…"


def _offending_excerpts(path: Path) -> list[str]:
    """Every un-exempt standalone "phone" in ``path``'s string literals."""
    source = path.read_text(encoding="utf-8")
    literals = (
        _python_string_literals(source)
        if path.suffix == ".py"
        else _js_string_literals(source)
    )
    offenders = []
    for literal in literals:
        if literal in ALLOWED_PHONE_LITERALS:
            continue
        residue = literal
        for fragment in ALLOWED_PHONE_FRAGMENTS:
            residue = residue.replace(fragment, " ")
        if PHONE_WORD_RE.search(residue):
            offenders.append(_excerpt(residue))
    return offenders


# --- The guards ---------------------------------------------------------------


def test_swept_surfaces_exist():
    """A renamed or deleted file must not silently drop out of the sweep."""
    missing = [name for name in SWEPT_SURFACES if not Path(name).is_file()]
    assert not missing, (
        "SWEPT_SURFACES names files that no longer exist; re-point the guard at "
        f"their new home rather than deleting the entry: {missing}"
    )


def test_swept_surfaces_never_call_the_instrument_a_phone():
    """#1941 R4: no household-facing "phone" survives on a swept surface.

    If this fails on copy you just wrote, the fix is the word — "the microphone"
    for the instrument, "the measurement page" for the browser surface. If it
    fails on a protocol key or element id, add it to ALLOWED_PHONE_LITERALS with
    the reason it is not copy.
    """
    offenders = {
        name: found
        for name in SWEPT_SURFACES
        if (found := _offending_excerpts(Path(name)))
    }
    assert not offenders, (
        "The measurement flow's household-facing copy must call the instrument "
        '"the microphone" (or the browser surface "the measurement page"), '
        "never a phone — the capturing device may be a laptop or a UMIK-2 in a "
        f"browser (#1941 R4, #1924): {offenders}"
    )


def test_the_swept_verdict_and_refusal_copy_says_microphone():
    """Positive pins: the specific strings the sweep rewrote still name the
    microphone. Catches a revert that keeps the file free of "phone" by deleting
    the sentence instead of fixing it."""
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_REGISTRY,
        REASON_SNR_FLOOR,
        REASON_VERIFY_LEVEL_SHIFT,
    )
    from jasper.capture_relay.session import CAPTURE_INCOMPATIBLE_USER_MESSAGE
    from jasper.correction.failures import PHONE_CAPTURE_UNAVAILABLE, public_failure

    assert "microphone" in REASON_REGISTRY[REASON_SNR_FLOOR].message
    # #1924 owns the ROUTING half of this reason ("re-verify to try again"
    # cannot succeed against a deterministic shift). R4 owns only the noun.
    assert "microphone" in REASON_REGISTRY[REASON_VERIFY_LEVEL_SHIFT].message
    assert "measurement page" in CAPTURE_INCOMPATIBLE_USER_MESSAGE
    assert "measurement page" in public_failure(PHONE_CAPTURE_UNAVAILABLE)["text"]


def test_allowlist_entries_are_still_used():
    """An exemption that no longer matches anything is stale — it outlived the
    string it excused, and a stale exemption is how a real offender slips in
    later under a name someone already agreed to ignore."""
    corpus = "\n".join(
        Path(name).read_text(encoding="utf-8") for name in SWEPT_SURFACES
    )
    unused = [
        fragment for fragment in ALLOWED_PHONE_FRAGMENTS if fragment not in corpus
    ]
    assert not unused, (
        "ALLOWED_PHONE_FRAGMENTS entries no longer present on any swept surface; "
        f"drop them so the exemption list stays honest: {unused}"
    )


def test_every_exemption_is_actually_about_a_phone():
    """Guards the guard: an exemption that contains no standalone "phone" exempts
    nothing and is either a typo or scope creep."""
    inert = [
        text
        for text in (*ALLOWED_PHONE_FRAGMENTS, *ALLOWED_PHONE_LITERALS)
        if not PHONE_WORD_RE.search(text)
    ]
    assert not inert, f"exemptions that do not contain a standalone 'phone': {inert}"
