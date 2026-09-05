# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement flow's household-facing vocabulary: the actor is the microphone.

Issue #1941 R4, design principle 6: *"The actor is the microphone. Never 'the
phone' in household-facing copy."* The instrument a household holds in front of
the speaker is whatever browser can reach the wizard — the 2026-07-30 bench ran a
**UMIK-2** while the wizard told the household its *phone* had drifted (#1924).
A laptop is equally legal. So the copy names the microphone, or the measurement
page, and never the shape of the device.

**This guard is a curated constant list, deliberately NOT a repo-wide grep.**
Roughly 800 occurrences of "phone" across the tree are internal and must stay:
protocol keys (``phone-event``), wire error strings (``phone never armed``),
element ids (``phone-mic-select``), mic-tier vocabulary (``MIC_TIERS``), and
every source-of-a-phone surface that genuinely means a phone
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

_REPO = Path(__file__).resolve().parents[1]

# Standalone "phone"/"phones" only — never the "phone" inside "microphone",
# "headphone", "iPhone", or "smartphone".
PHONE_WORD_RE = re.compile(r"(?:^|[^A-Za-z])[Pp]hones?(?:[^A-Za-z]|$)")


# --- The swept surfaces -------------------------------------------------------
# Every file whose household-facing copy was swept for #1941 R4, grouped by the
# four clusters the requirement names. A file listed here is asserted to carry no
# un-exempt "phone" in whatever this guard can read of it:
#
#   * ``.py``  — string literals via AST, docstrings excluded;
#   * ``.js``  — string literals, with comment bodies blanked first;
#   * ``.html``— *rendered* text (element text plus the user-visible attributes),
#                because a page's copy lives between its tags, not in a literal.
#
# That third mode exists because the first review of this guard proved a real
# hole: run against ``deploy/index.html`` as if it were JS, the extractor found
# 919 string literals and ZERO "phone" hits while the landing page was rendering
# "Phone measurement" in plain sight. Element text is simply not a string
# literal. Reading such a file with the JS extractor is worse than not listing it
# — it reports clean and means nothing.

SWEPT_SURFACES: tuple[str, ...] = (
    # Cluster 1 — wizard verdict lines (the §5.10 reason registry and the
    # screen envelopes that render its verdicts).
    "jasper/active_speaker/crossover_v2_flow.py",
    "jasper/active_speaker/crossover_v2/refusal_copy.py",
    # Two homes for household copy wave 3 moved OUT of the flow above. Without
    # these rows the sweep would still pass and would cover neither: the group
    # close's geometry guidance, null-classification sentences and carve-out
    # disclosure, and the prompt table — the largest single block of household
    # copy this flow has.
    "jasper/active_speaker/crossover_v2/spatial.py",
    "jasper/active_speaker/crossover_v2/capture_plan.py",
    "jasper/active_speaker/crossover_v2/intervention.py",
    # Not verdict copy -- swept because it is the closed-vocabulary SOURCE of
    # the mic-trust tier keys (MIC_TIERS, _SIGMA_TOLERABLE_DB,
    # _MIC_TRUST_TABLE_HZ) that make "phone" a legitimate literal here; see
    # ALLOWED_PHONE_LITERALS["phone"]. intervention.py above imports these
    # directly rather than holding its own copy, so this is their only home.
    "jasper/active_speaker/linearization_envelope.py",
    "jasper/active_speaker/crossover_envelope_v2.py",
    "jasper/active_speaker/baseline_profile.py",
    # Cluster 2 — failure / refusal copy.
    "jasper/correction/failures.py",
    "jasper/correction/level_match.py",
    "jasper/web/correction_setup.py",
    "jasper/web/correction_room_flow.py",
    "jasper/web/sync_flow.py",
    # Cluster 3 — the measurement pages' own chrome. The landing page is the
    # ENTRY POINT to /correction/room/, so its row label and that page's
    # subtitle have to agree; they did not until #1959. The labels live in the
    # site-map manifest, the page template holds the rest of the chrome.
    "jasper/web/nav.py",
    "deploy/index.html",
    "jasper/web/correction_crossover_flow.py",
    "deploy/assets/correction/js/main.js",
    "deploy/assets/correction/js/crossover/main.js",
    "deploy/assets/sync/js/main.js",
    "deploy/assets/rooms/js/main.js",
    "deploy/assets/sound-profile/js/active-speaker-ui.js",
    # Cluster 4 — the capture page's setup screens.
    "jasper/active_speaker/crossover_v2/sweep_spec.py",
    # The placement and acknowledgement sentences the spec above renders INTO
    # those screens are composed here, so the copy leaves the swept set the
    # moment it crosses this module boundary (#1978). It is clean today; listing
    # it is what keeps the consent screen's own words inside the guard.
    "jasper/active_speaker/capture_geometry.py",
)


# --- Fragments that legitimately keep "phone" ---------------------------------
# Each key is an exact substring; an occurrence of "phone" is exempt only when it
# sits inside one of these. Fragments rather than whole literals because a
# wizard's page template is ONE multi-kilobyte literal — exempting the literal
# would exempt the page. The value is the reason the fragment is not copy.

ALLOWED_PHONE_FRAGMENTS: dict[str, str] = {
    # --- Wire / protocol vocabulary. Renaming these is a wire protocol change,
    # not a copy change, and both sides of the transport already agree on them.
    "phone_feed_lost": "canonical snake_case refusal code (reason= log key)",
    "phone_never_armed": "canonical snake_case refusal code",
    "cancelled_before_phone_armed": "canonical snake_case refusal code",
    "phone_aborted": "abort reason the capture page posts",
    # --- Raw ramp error strings: the KEYS of the refusal tables, i.e. the
    # verbatim `error` the audio-measurement ramp emits, pinned by
    # tests/test_audio_measurement_ramp.py. Only their VALUES are household copy.
    "no usable phone samples": "raw ramp error string (refusal-table key)",
    # The ONE fragment with reach beyond a single string: it also covers the
    # CaptureTimeout detail "phone never armed within {n}s". Both are non-copy
    # (table key / log detail), so one entry is the honest shape — see
    # FRAGMENT_REACH_EXCEPTIONS and the test that pins it.
    "phone never armed": "raw ramp error string (refusal-table key)",
    "phone feed lost": "raw ramp error string (refusal-table-prefix key)",
}


# Whole-literal exemptions, matched by EQUALITY rather than by substring. A bare
# token like "phone" cannot be a fragment — removing it as a substring would
# exempt every sentence that contains the word, which is the whole point of the
# guard. A literal whose entire value is one token is a key or an id, never copy.
ALLOWED_PHONE_LITERALS: dict[str, str] = {
    "phone": "mic-trust TIER key -- MIC_TIERS / _SIGMA_TOLERABLE_DB / "
    "_MIC_TRUST_TABLE_HZ in jasper.active_speaker.linearization_envelope "
    "(also swept, above) are a closed set of keys (reference / consumer / "
    "phone), never rendered as a sentence",
}


# How many distinct readable strings each fragment is allowed to exempt. A
# fragment is meant to excuse ONE string; anything broader is how an exemption
# quietly grows into a licence. Only entries listed here may exempt more than
# one, and the count is pinned so growth has to be deliberate.
FRAGMENT_REACH_EXCEPTIONS: dict[str, int] = {
    # Also covers the CaptureTimeout detail "phone never armed within {n}s" —
    # the ramp's refusal-table key is a prefix of it. Both are non-copy.
    "phone never armed": 2,
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


_HTML_INERT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_HTML_TAG_RE = re.compile(r"(?s)<[^>]+>")
_HTML_VISIBLE_ATTR_RE = re.compile(
    r"""(?:aria-label|title|placeholder|alt)=(["'])(.*?)\1""", re.S
)


def _html_rendered_text(source: str) -> list[str]:
    """What a household actually reads on the page.

    Element text with ``<script>``/``<style>`` bodies dropped, plus the
    attributes a browser surfaces (``aria-label``, ``title``, ``placeholder``,
    ``alt``) — a screen-reader label is household copy as surely as a heading.
    Inline script string literals are deliberately NOT scanned here: a page that
    needs them scanned belongs in the JS list as its own module.
    """
    body = _HTML_INERT_RE.sub(" ", source)
    return [
        _HTML_TAG_RE.sub(" ", body),
        *(match.group(2) for match in _HTML_VISIBLE_ATTR_RE.finditer(body)),
    ]


def _excerpt(literal: str) -> str:
    """A readable pointer at the offending word, not the whole page template."""
    match = PHONE_WORD_RE.search(literal)
    assert match is not None
    flat = " ".join(literal[max(0, match.start() - 70) : match.end() + 70].split())
    return f"…{flat}…"


def _readable_strings(name: str) -> list[str]:
    """Everything this guard can read out of ``name``, per its extension."""
    source = (_REPO / name).read_text(encoding="utf-8")
    if name.endswith(".py"):
        return _python_string_literals(source)
    if name.endswith(".html"):
        return _html_rendered_text(source)
    return _js_string_literals(source)


def _offending_excerpts(name: str) -> list[str]:
    """Every un-exempt standalone "phone" in what ``name`` shows a household."""
    literals = _readable_strings(name)
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
    missing = [name for name in SWEPT_SURFACES if not (_REPO / name).is_file()]
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
        name: found for name in SWEPT_SURFACES if (found := _offending_excerpts(name))
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
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_REGISTRY,
        REASON_SNR_FLOOR,
        REASON_VERIFY_LEVEL_SHIFT,
    )

    assert "microphone" in REASON_REGISTRY[REASON_SNR_FLOOR].message
    # R4 owns the noun here; #1924's routing half (the remedy clause) landed
    # separately and is pinned in tests/test_crossover_v2_conductor.py. Both
    # halves of the sentence have to survive, so this keeps asserting the noun.
    assert "microphone" in REASON_REGISTRY[REASON_VERIFY_LEVEL_SHIFT].message


def test_the_fixed_axis_placement_copy_keeps_the_conditional_aim():
    """#1978. Same shape of hole as the pin above, for the other half of that
    issue: the "phone" sweep cannot catch a revert to "Aim it according to its
    calibration file", because that sentence contains no "phone" — listing
    capture_geometry.py in SWEPT_SURFACES guards the noun, not this claim.

    What the claim is: a phone mic has no calibration file, so all three
    fixed-axis instructions name the physical aim direction and keep the
    calibration clause conditional.
    """
    from jasper.active_speaker.capture_geometry import (
        cloud_walk_placement_instruction,
        reference_axis_driver_placement_instruction,
        summed_placement_instruction,
    )

    for text in (
        reference_axis_driver_placement_instruction("woofer"),
        summed_placement_instruction(),
        cloud_walk_placement_instruction(),
    ):
        assert "pointed at the speaker" in text
        assert "unless its calibration file says otherwise" in text


def test_the_driver_levels_pointer_names_the_tab_as_its_owner_labels_it():
    """The `/sound/` driver-levels copy tells the household to choose a tab BY
    NAME, so that name has to track the module that owns it.

    ``correction_hub.SECTIONS`` is the single owner of the household-facing tab
    labels ("Active speaker" for the still-internally-``crossover`` slug); the
    pointer in ``active-speaker-ui.js`` is a second copy of one of them. Rename
    the tab in Python and the pointer silently sends the household looking for a
    tab that no longer exists, with every other test green — the #1959 shape (a
    row label and the surface it points at drifting apart), which is why that
    page is swept here at all.

    Read the LITERALS, not the file text: the comment above the copy also names
    the tab, so a plain substring search would stay green if the copy dropped it.
    """
    from jasper.web.correction_hub import SECTIONS

    label = next(text for key, text, _ in SECTIONS if key == "crossover")
    literals = _readable_strings("deploy/assets/sound-profile/js/active-speaker-ui.js")
    assert any(label in literal for literal in literals), (
        f'The /sound/ driver-levels pointer must name the "{label}" tab exactly '
        "as correction_hub.SECTIONS labels it — update "
        "NEARFIELD_LEVEL_MATCH_GUIDANCE in "
        "deploy/assets/sound-profile/js/active-speaker-ui.js to match the "
        "renamed tab, or the copy sends the household to a tab that is not there"
    )


def test_allowlist_entries_are_still_used():
    """An exemption that no longer matches anything is stale — it outlived the
    string it excused, and a stale exemption is how a real offender slips in
    later under a name someone already agreed to ignore.

    Matched against the EXTRACTED strings, not the raw file text: an exemption
    whose only remaining home is a comment exempts nothing a household can read,
    and should read as stale rather than as still-earning-its-keep.
    """
    corpus = "\n".join(
        text for name in SWEPT_SURFACES for text in _readable_strings(name)
    )
    unused = [
        fragment for fragment in ALLOWED_PHONE_FRAGMENTS if fragment not in corpus
    ]
    assert not unused, (
        "ALLOWED_PHONE_FRAGMENTS entries no longer present in any swept "
        f"surface's readable strings; drop them: {unused}"
    )
    orphan_literals = [
        literal
        for literal in ALLOWED_PHONE_LITERALS
        if not any(
            literal in _readable_strings(name) for name in SWEPT_SURFACES
        )
    ]
    assert not orphan_literals, (
        "ALLOWED_PHONE_LITERALS entries that match no literal on any swept "
        f"surface; drop them: {orphan_literals}"
    )


def test_no_fragment_exempts_more_than_it_was_granted():
    """Fragment reach is bounded and recorded, so an exemption cannot quietly
    widen into a licence. A fragment excuses ONE string unless
    FRAGMENT_REACH_EXCEPTIONS says otherwise, with the count pinned."""
    readable = [
        text
        for name in SWEPT_SURFACES
        for text in _readable_strings(name)
        if PHONE_WORD_RE.search(text)
    ]
    over = {}
    for fragment in ALLOWED_PHONE_FRAGMENTS:
        reach = len({text for text in readable if fragment in text})
        allowed = FRAGMENT_REACH_EXCEPTIONS.get(fragment, 1)
        if reach > allowed:
            over[fragment] = f"exempts {reach} strings, granted {allowed}"
    assert not over, (
        "fragment exemptions reaching further than granted — narrow the "
        f"fragment, or record the wider reach with its reason: {over}"
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
