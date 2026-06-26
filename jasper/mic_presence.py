# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for *is a usable microphone present?* — mic-agnostic.

The AEC reconciler (``jasper-aec-reconcile``) is the sole authority on mic
presence, and it is **not XVF-specific**: it selects whatever mic is present —
the XVF3800 ``Array``, the ``L16K6Ch`` variant, or a custom ``JASPER_MIC_DEVICE``
such as a UMIK-2 — and maintains one generic gate marker accordingly
(``/var/lib/jasper/voice-input-absent``: created on "no usable mic", cleared on
any mic present, including custom; see ``jasper.voice.input_presence`` and
``deploy/bin/jasper-aec-reconcile``).

This module is the single *reader* of that verdict. Every status surface — the
doctor, ``/state``, the ``/system`` dashboard — should call
``read_mic_presence()`` and *display* the result rather than independently
re-probing ALSA / ``lsusb`` / PortAudio. That keeps "no microphone" a single
coherent fact instead of a scatter of contradicting checks.

Two layers, kept strictly separate so the next microphone needs no change here:

* **Presence is generic** — driven by the gate marker. ``present`` is true
  whenever the reconciler has *not* parked voice for a missing mic, regardless
  of mic type. (Driving presence off the XVF profile would report a working
  non-XVF mic as "absent" — the bug this separation exists to prevent.)
* **XVF detail is enrichment** — the reconciler also publishes an XVF-specific
  runtime profile to ``/run/jasper-mic-profile/xvf3800.json`` (schema:
  ``xvf3800.RuntimeProfile``). When the present mic is a detected XVF, that
  enriches the record (card, channels, chip-AEC capability). A present non-XVF
  mic simply has no enrichment; the per-device doctor checks report its
  specifics. When a second mic profile lands and ``jasper/mics/base.py`` is
  extracted (per ``docs/HANDOFF-mic-fusion-architecture.md``), enrichment
  generalises there — presence already does not care.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from jasper.voice.input_presence import (
    voice_input_absent_marker_path,
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


@dataclass(frozen=True)
class MicPresence:
    """Unified, display-ready microphone status.

    ``present`` is the generic verdict (any mic type). The ``is_xvf`` block is
    XVF-specific enrichment, populated only when the present mic is a detected
    XVF3800.
    """

    present: bool
    reason: str = ""  # why absent (generic); "" when present
    is_xvf: bool = False  # present mic is a detected XVF3800 -> enrichment below
    alsa_card: str = ""
    variant: str = ""
    display_name: str = ""
    capture_channels: int | None = None
    recommended_profile: str = ""
    chip_aec_supported: bool = False

    @property
    def parked(self) -> bool:
        """jasper-voice is parked for no usable mic — the inverse of present."""
        return not self.present

    @property
    def absent_confirmed(self) -> bool:
        """No usable mic of any type (the reconciler's generic gate). The single
        case status surfaces render as one expected line, never a red failure."""
        return not self.present

    @property
    def summary(self) -> str:
        """One-line, human-facing status for headlines / dashboards."""
        if not self.present:
            detail = self.reason or "no usable microphone detected"
            return (
                f"not detected — {detail}; jasper-voice is parked and "
                "auto-starts when a mic is reconnected"
            )
        if self.is_xvf:
            bits = [self.alsa_card or "XVF3800"]
            if self.capture_channels:
                bits.append(f"{self.capture_channels}ch")
            bits.append(
                "chip-AEC capable" if self.chip_aec_supported else "software AEC"
            )
            return f"present ({', '.join(bits)})"
        # Present non-XVF mic: the per-device mic checks report its specifics.
        return "present"

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly projection for ``/state`` and other API surfaces."""
        return {
            "present": self.present,
            "parked": self.parked,
            "reason": self.reason,
            "is_xvf": self.is_xvf,
            "alsa_card": self.alsa_card,
            "variant": self.variant,
            "display_name": self.display_name,
            "capture_channels": self.capture_channels,
            "recommended_profile": self.recommended_profile,
            "chip_aec_supported": self.chip_aec_supported,
            "summary": self.summary,
        }


def _marker_reason() -> str:
    """Best-effort reason text from the marker body (``reason=<text>``)."""
    try:
        body = Path(voice_input_absent_marker_path()).read_text()
    except OSError:
        return ""
    for line in body.splitlines():
        if line.startswith("reason="):
            return line[len("reason="):].strip()
    return ""


def _read_profile_json(state_path: str | None) -> dict | None:
    path = state_path or mic_profile_state_path()
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_mic_presence(state_path: str | None = None) -> MicPresence:
    """Resolve current microphone status from the reconciler's SSOT.

    Presence is generic (the gate marker); XVF detail is enrichment. Never
    raises — a missing/corrupt enrichment JSON just means "present, no XVF
    detail" when a mic is up.
    """
    if voice_parked_no_mic():
        # The reconciler positively determined there is no usable mic of ANY
        # type. One generic absent verdict.
        return MicPresence(
            present=False,
            reason=_marker_reason() or "no usable microphone present",
        )
    # A usable mic is present. Enrich with XVF detail iff this mic is a detected
    # XVF (its profile JSON says present); a present non-XVF mic has no XVF JSON
    # and is reported simply as present (the per-device checks show specifics).
    payload = _read_profile_json(state_path)
    if isinstance(payload, dict) and payload.get("present"):
        chan = payload.get("capture_channels")
        return MicPresence(
            present=True,
            is_xvf=True,
            alsa_card=str(payload.get("alsa_card_name") or ""),
            variant=str(payload.get("variant_id") or ""),
            display_name=str(payload.get("display_name") or ""),
            capture_channels=chan if isinstance(chan, int) else None,
            recommended_profile=str(payload.get("recommended_profile") or ""),
            chip_aec_supported=bool(payload.get("chip_aec_supported")),
        )
    return MicPresence(present=True)
