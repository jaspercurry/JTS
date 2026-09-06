# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for *is usable voice input present?* — mic-agnostic.

The AEC reconciler (``jasper-aec-reconcile``) is the sole *writer* of the gate
verdict, and it is **not XVF-specific**: it selects whatever local input is
usable — the XVF3800 ``Array``, the ``L16K6Ch`` variant, or a custom
``JASPER_MIC_DEVICE`` such as a UMIK-2 — and maintains one generic gate marker
accordingly (``/var/lib/jasper/voice-input-absent``: created when no safe,
usable input exists — a managed XVF short of chip AEC keeps hearing and
discloses instead (ADR-0101); see
``jasper.voice.input_presence`` and ``deploy/bin/jasper-aec-reconcile``).

The verdict is an OR over two independently-owned inputs, because a paired
mic-bearing accessory is a usable push-to-talk microphone on its own. Note the
scope: this record answers *is the start gate satisfied*, never *is jasper-voice
running* — see ``jasper.voice.input_presence``.

* a **local** microphone, owned by ``jasper-aec-reconcile``;
* an **accessory** microphone, owned by ``jasper-accessory-reconcile`` and
  published as ``JASPER_MANUAL_MIC_SOURCES``
  (``jasper.accessories.mic_env``).

The gate marker is the AND of their absences, and the AEC reconciler derives it
from both. This module is the single *reader*: every status surface — the
doctor, ``/state``, the ``/system`` dashboard — should call
``read_mic_presence()`` and *display* the result rather than independently
re-probing ALSA / ``lsusb`` / PortAudio. That keeps "no microphone" a single
coherent fact instead of a scatter of contradicting checks.

Three layers, kept strictly separate so the next microphone needs no change here:

* **Input availability is generic** — driven by the gate marker.
  ``present`` is true whenever the reconciler has *not* parked voice, regardless
  of mic type or which half satisfied it. (Driving availability off the XVF
  profile would report a working non-XVF mic as "absent" — the bug this
  separation exists to prevent.)
* **Accessory sources are read from their owner's published file** — never from
  BlueZ and never from ``os.environ``. ``accessory_sources`` says *what push-to-
  talk inputs exist*; it deliberately does not claim anything about the local
  mic, because this module has no local probe. The doctor's ``mic ALSA card`` /
  ``mic capture`` checks own that half: they probe the device and use
  ``accessory_present`` to decide whether a missing local mic is a *failure* or
  an expected push-to-talk-only box. **``/state.microphone`` has no such
  sibling**: it is this record and nothing else, so it cannot separate "no
  local mic + remote paired" from "healthy non-XVF local mic + remote paired"
  either. ``summary`` says so in place rather than implying a distinction the
  data does not carry; a consumer that needs the local half must read the
  doctor.
* **XVF detail is enrichment** — the reconciler also publishes an XVF-specific
  runtime profile to ``/run/jasper-mic-profile/xvf3800.json`` (schema:
  ``xvf3800.RuntimeProfile``). When the present mic is a detected XVF, that
  enriches the record (card, channels, chip-AEC capability). A present non-XVF
  mic simply has no enrichment; the per-device doctor checks report its
  specifics. Enrichment stays XVF-specific until a second mic family needs
  its own — presence already does not care.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from jasper.accessories.mic_env import read_accessory_mic_sources
from jasper.atomic_io import read_json_mapping
from jasper.voice.input_presence import (
    voice_input_absent_marker_lines,
    voice_parked_no_mic,
)

# Keep in lockstep with jasper.cli.xvf_profile.DEFAULT_STATE_PATH and the
# reconciler's MIC_PROFILE_STATE_PATH default. JASPER_MIC_PROFILE_STATE_PATH
# overrides all three (tests / nonstandard layouts).
DEFAULT_MIC_PROFILE_STATE_PATH = "/run/jasper-mic-profile/xvf3800.json"


def mic_profile_state_path() -> str:
    """Resolved XVF-enrichment JSON path (env override wins, for tests)."""
    return os.environ.get(
        "JASPER_MIC_PROFILE_STATE_PATH", DEFAULT_MIC_PROFILE_STATE_PATH
    )


# The gate marker's ``reason=`` vocabulary. ``jasper-aec-reconcile``'s
# ``mark_voice_input_absent`` is its only writer and may write nothing outside
# this set; anything else read back (an older build, a hand-edited marker) is
# ``MIC_ABSENT_UNKNOWN``. The operator prose these codes replaced is the
# marker's ``detail=`` line — display text, matched on by nobody.
MIC_ABSENT_NO_LOCAL_OR_ACCESSORY = "no_local_or_accessory_mic"
MIC_ABSENT_ACCESSORY_UNKNOWN = "accessory_mic_unknown"
MIC_ABSENT_XVF_CAPTURE_ABSENT = "xvf_capture_absent"
MIC_ABSENT_CHIP_AEC_BRINGUP_FAILED = "chip_aec_bringup_failed"
MIC_ABSENT_CHIP_AEC_VALIDATING = "chip_aec_validating"
MIC_ABSENT_UNKNOWN = "unknown"

MIC_ABSENT_REASONS: frozenset[str] = frozenset(
    {
        MIC_ABSENT_NO_LOCAL_OR_ACCESSORY,
        MIC_ABSENT_ACCESSORY_UNKNOWN,
        MIC_ABSENT_XVF_CAPTURE_ABSENT,
        MIC_ABSENT_CHIP_AEC_BRINGUP_FAILED,
        MIC_ABSENT_CHIP_AEC_VALIDATING,
        MIC_ABSENT_UNKNOWN,
    }
)

#: Whether a park is a round trip the reconciler itself ends is a property of
#: the code, not a second field on the wire: these parks are the ones the
#: daemon's mic-loss cue (ADR-0239) must stay silent for.
TRANSIENT_MIC_ABSENT_REASONS: frozenset[str] = frozenset(
    {MIC_ABSENT_CHIP_AEC_VALIDATING}
)


@dataclass(frozen=True)
class MicPresence:
    """Unified, display-ready voice-input status.

    ``present`` is the generic verdict (any input). ``accessory_sources`` names
    the push-to-talk inputs published by ``jasper-accessory-reconcile``. The
    ``is_xvf`` block is XVF-specific enrichment, populated only when the present
    local mic is a detected XVF3800.
    """

    present: bool
    #: Why absent, as a MIC_ABSENT_REASONS code; "" when present.
    reason: str = ""
    #: Operator prose for that code (a card name, a probe status, the unit to
    #: inspect). Display only — switch on ``reason``, never on this.
    detail: str = ""
    # Push-to-talk source ids from jasper-accessory-reconcile. Non-empty means
    # a paired accessory mic satisfies the gate on its own — it does NOT imply
    # anything about the local mic (see the module docstring).
    accessory_sources: tuple[str, ...] = ()
    is_xvf: bool = False  # present mic is a detected XVF3800 -> enrichment below
    alsa_card: str = ""
    variant: str = ""
    display_name: str = ""
    capture_channels: int | None = None
    recommended_profile: str = ""
    chip_aec_supported: bool = False

    @property
    def parked(self) -> bool:
        """jasper-voice is parked for no usable input — the inverse of present."""
        return not self.present

    @property
    def absent_confirmed(self) -> bool:
        """No usable voice input of any kind (the reconciler's generic gate). The
        single case status surfaces render as one expected line, never a red
        failure."""
        return not self.present

    @property
    def accessory_present(self) -> bool:
        """A paired accessory microphone is published for push-to-talk.

        The doctor's local-device checks read this to tell "this box has no
        local mic, but a push-to-talk accessory is paired" (expected) from
        "this box's configured mic is missing and nothing replaces it"
        (a failure). *Paired*, not in use: nothing here observes the daemon."""
        return bool(self.accessory_sources)

    @property
    def accessory_summary(self) -> str:
        """``"push-to-talk accessory paired: a, b"``, or ``""`` when none is.

        Deliberately says *paired*, not *running*/*in use*. This record is
        derived from the gate marker and the accessory owner's published file;
        neither says anything about whether jasper-voice is up, so a
        present-tense claim about the daemon would be one this module cannot
        support."""
        if not self.accessory_sources:
            return ""
        return "push-to-talk accessory paired: " + ", ".join(
            self.accessory_sources
        )

    @property
    def summary(self) -> str:
        """One-line, human-facing status for headlines / dashboards."""
        if not self.present:
            why = self.detail or self.reason or "no usable microphone detected"
            return (
                f"input unavailable — {why}; jasper-voice is parked and "
                "reconciles automatically when the condition is resolved"
            )
        if self.is_xvf:
            bits = [self.alsa_card or "XVF3800"]
            if self.capture_channels:
                bits.append(f"{self.capture_channels}ch")
            bits.append(
                "chip-AEC capable" if self.chip_aec_supported else "software AEC"
            )
            local = f"present ({', '.join(bits)})"
            return f"{local}; {self.accessory_summary}" if (
                self.accessory_sources
            ) else local
        if self.accessory_sources:
            # No XVF enrichment, so — unlike the branch above — there is no
            # local evidence to compose with. Two shapes land here and this
            # record cannot tell them apart: a box with NO local mic and a
            # paired remote, and a box with a healthy non-XVF local mic (a
            # custom JASPER_MIC_DEVICE, a plain USB mic) that also has one.
            #
            # So two things this must NOT say. Not "present": on the first
            # shape that is the lie an operator would act on. And not that
            # voice is *running* on the accessory: the marker reports the start
            # gate only. The first shape CAN answer now (issue #2205's daemon
            # half plans zero wake legs and serves the button), which makes the
            # runtime claim tempting and no less unfounded — nothing here looks
            # at the daemon. State the gate, and hand the local half to the
            # checks that actually probe the device.
            return (
                f"voice-input gate open — {self.accessory_summary}; "
                "this record cannot see the local mic — the doctor's "
                "`mic ALSA card` / `mic capture` checks own that half"
            )
        # Present non-XVF mic: the per-device mic checks report its specifics.
        return "present"

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly projection for ``/state`` and other API surfaces."""
        return {
            "present": self.present,
            "parked": self.parked,
            "reason": self.reason,
            "detail": self.detail,
            "accessory_sources": list(self.accessory_sources),
            "accessory_present": self.accessory_present,
            "is_xvf": self.is_xvf,
            "alsa_card": self.alsa_card,
            "variant": self.variant,
            "display_name": self.display_name,
            "capture_channels": self.capture_channels,
            "recommended_profile": self.recommended_profile,
            "chip_aec_supported": self.chip_aec_supported,
            "summary": self.summary,
        }


def _marker_fields() -> tuple[str, str]:
    """``(reason code, detail prose)`` from the marker body.

    An unrecognised or missing ``reason=`` is ``MIC_ABSENT_UNKNOWN``, never the
    raw token: the code is a closed wire vocabulary, so prose from an older
    build must not pass through as one.
    """
    code = ""
    detail = ""
    for line in voice_input_absent_marker_lines():
        if not code and line.startswith("reason="):
            code = line[len("reason="):].strip()
        if not detail and line.startswith("detail="):
            detail = line[len("detail="):].strip()
    return (
        code if code in MIC_ABSENT_REASONS else MIC_ABSENT_UNKNOWN,
        detail,
    )


def voice_park_is_transient() -> bool:
    """True when the current park is a round trip the reconciler itself ends
    — the chip-AEC validation bounce (ADR-0239) — rather than a real absence
    of voice input.

    Meaningless unless ``voice_parked_no_mic()`` is also true, and fail-safe to
    False (an unknown code is not transient) so a real absence can never be
    misread as transient and lose its shutdown cue.
    """
    return _marker_fields()[0] in TRANSIENT_MIC_ABSENT_REASONS


def read_mic_presence(state_path: str | None = None) -> MicPresence:
    """Resolve current voice-input status from the reconcilers' SSOTs.

    Presence is generic (the gate marker); accessory sources come from their
    owner's published env file; XVF detail is enrichment. Never raises — a
    missing/corrupt enrichment JSON just means "present, no XVF detail", and an
    unreadable accessory file just means "no accessory".
    """
    # This function is a display surface and must never raise, so the two cases
    # read_accessory_mic_sources deliberately propagates — an unreadable file
    # (OSError/UnicodeDecodeError) and an unparsable one (ManualMicSourcesError,
    # a ValueError subclass) — both degrade to "no accessory" here. The gate
    # writer, which must tell all three facts apart, does NOT catch them.
    try:
        accessory_sources = read_accessory_mic_sources()
    except (OSError, UnicodeDecodeError, ValueError):
        accessory_sources = ()
    if voice_parked_no_mic():
        # The reconciler positively determined there is no safe, usable input
        # of any kind. One generic unavailable verdict. accessory_sources is
        # carried through rather than assumed empty: if the two ever disagree
        # (a marker written before the accessory half was published, then a
        # reconcile that has not run yet), the record shows both facts instead
        # of hiding one.
        reason, detail = _marker_fields()
        return MicPresence(
            present=False,
            reason=reason,
            detail=detail,
            accessory_sources=accessory_sources,
        )
    # Usable voice input is present. Enrich with XVF detail iff a detected XVF
    # is the local mic (its profile JSON says present); a present non-XVF mic
    # has no XVF JSON and is reported simply as present (the per-device checks
    # show specifics).
    payload = read_json_mapping(Path(state_path or mic_profile_state_path()))
    if payload and payload.get("present"):
        chan = payload.get("capture_channels")
        return MicPresence(
            present=True,
            accessory_sources=accessory_sources,
            is_xvf=True,
            alsa_card=str(payload.get("alsa_card_name") or ""),
            variant=str(payload.get("variant_id") or ""),
            display_name=str(payload.get("display_name") or ""),
            capture_channels=chan if isinstance(chan, int) else None,
            recommended_profile=str(payload.get("recommended_profile") or ""),
            chip_aec_supported=bool(payload.get("chip_aec_supported")),
        )
    return MicPresence(present=True, accessory_sources=accessory_sources)
