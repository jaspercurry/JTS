# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement-microphone model registry: which mics JTS knows, and how
to recognise one as measurement-class hardware.

Split out of :mod:`jasper.audio_measurement.calibration` (which re-exports
everything here for its established consumers) with one hard constraint:
**this module imports nothing beyond the stdlib**. It is the vocabulary
behind ``python -m jasper.cli.measurement_mic``, which
``deploy/bin/jasper-aec-reconcile`` spawns from the hotplug path on any box
with a USB capture card — and the reconciler's managed XVF *is* a USB card,
so the spawn happens on every managed-XVF pass that primes the exclusion.
``calibration``'s ``import numpy`` made that bridge a 190-module interpreter
(~85 ms of import work measured on the dev host, dominated by numpy); this
leaf keeps it in ``jasper.cli.xvf_profile`` territory. A numpy import added
here would silently re-inflate every such pass — keep this module pure data
plus dict lookups.
"""
from __future__ import annotations

from typing import Any

# Single source of truth for supported measurement mics. Adding a mic here
# wires the vendor lookup, the model picker, the wrong-mic guard, AND the
# wizard's label-based auto-inference (see model_label_aliases in
# jasper.audio_measurement.calibration). Optional `label_aliases` overrides
# the default (the vendor_model) when a mic's OS device label doesn't contain
# its vendor model string.
#
# `tier` is the correction-envelope trust tier (#1668 PR-B) — see
# `mic_tier_for_model` below and
# jasper.active_speaker.linearization_envelope.MIC_TIERS for the vocabulary
# ("reference" / "consumer" / "phone"). Not imported from here: audio_measurement
# is a lower architectural layer than active_speaker (every existing import
# between the two packages runs active_speaker -> audio_measurement, never
# the reverse), so the tier vocabulary is duplicated as plain string
# literals rather than imported upward.
#
# `usb_ids` is the device's USB `vid:pid` as the kernel spells it in
# `/proc/asound/<card>/usbid`. It exists so the rest of JTS can recognise a
# measurement microphone as measurement-class *hardware* — chiefly so
# `deploy/bin/jasper-aec-reconcile` never selects one as the voice/wake
# input (see `measurement_mic_usb_ids` below). USB id, not serial: a UMIK-2's
# USB serial descriptor is the literal "00000" on every unit, so serial can
# never identify the device. Mirrors `jasper.audio_hardware.dac.DacProfile`'s
# `usb_ids` and `jasper.mics.xvf3800.USB_VID_PIDS` — same vocabulary, one
# tuple of "vid:pid" per registry entry.
#
# Only the UMIK-2 declares one, measured on the live JTS3 unit
# (2026-08-18: `/proc/asound/UMIK2/usbid` = 2752:002b). The other three
# entries declare an EMPTY tuple, which reads as "this project has not
# measured this model's id" — never a guess. An unmeasured model is simply
# not recognised as measurement-class hardware, which is the same position
# every model was in before this field existed; guessing an id would be
# worse, because a wrong id could exclude somebody's voice array.
#
# `sign_convention` is what the VENDOR's file states, and therefore how
# `fetch_vendor_calibration` must parse it. It is per-entry rather than a
# single hardcode in the fetcher because that hardcode was the 2026-07-27
# bug: one literal `"correction"` covered every provider, so every
# vendor-fetched record stored the mic's response as if it were already a
# correction and the measurement pipeline ADDED what it should have
# SUBTRACTED — an error of exactly twice the file's value. Measured on the
# live JTS3 UMIK-2 file: mean +1.71 dB, max +1.84 dB of over-cut across
# 2.8-8 kHz, reversing to a mean -1.14 dB (max 2.56 dB) under-cut over
# 11-16 kHz, and |4.85| dB worst case across the full 20 Hz-20 kHz span.
# Declaring the convention beside the provider is what makes the next mic's
# convention a deliberate registry decision instead of an inherited default.
#
# Every entry today is "response", on two evidence classes:
#
#   * miniDSP (direct physical proof, owner-measured 2026-07-27): for one
#     UMIK-2 the 0-degree and 90-degree files differ by ~9.4 dB at 20 kHz,
#     the 90-degree file being the MORE negative. A microphone is less
#     sensitive off-axis at HF, so those numbers can only be the mic's
#     response; a 90-degree *correction* would have to be strongly positive
#     at HF to add the lost treble back. Recorded in
#     captures/iloud-comparison-20260727/LINEARIZATION-AGENT-PROMPT.md.
#   * Dayton (documented ecosystem contract, no Dayton file inspected by
#     this project): both vendors publish these files for REW, whose own
#     help states a cal file "should contain the actual gain (and optionally
#     phase) response of the meter or microphone at the frequencies given,
#     these will then be subtracted from subsequent measurements"
#     (roomeqwizard.com/help/help_en-GB/html/meter.html). REW has one mic-cal
#     semantics with no per-vendor sign switch, and Dayton's product pages
#     direct owners to REW with exactly this file.
#
# If a real Dayton file ever contradicts that, the registry edit fixes every
# FUTURE fetch — but it does not fix the past by itself: the migration in
# jasper.audio_measurement.calibration runs one way (correction -> response)
# and the vendor cache serves stored records ahead of any re-fetch. Reversing
# a declaration therefore costs a registry edit plus a NEW opposite-direction
# migration.
SUPPORTED_MODELS: dict[str, dict[str, Any]] = {
    "dayton_imm6": {
        "provider": "dayton_audio",
        "vendor_model": "iMM-6",
        "label": "Dayton Audio iMM-6 / iMM-6C",
        "tier": "consumer",
        "sign_convention": "response",
        # Not measured by this project (the iMM-6 is a TRRS mic that
        # enumerates through whatever interface it is plugged into; the
        # iMM-6C is USB-C). Empty until a real unit is read.
        "usb_ids": (),
    },
    "dayton_umm6": {
        "provider": "dayton_audio",
        "vendor_model": "UMM-6",
        "label": "Dayton Audio UMM-6",
        "tier": "consumer",
        "sign_convention": "response",
        "usb_ids": (),  # not measured by this project
    },
    "minidsp_umik1": {
        "provider": "minidsp",
        "vendor_model": "umik-1",
        "label": "miniDSP UMIK-1",
        "tier": "reference",
        "sign_convention": "response",
        # Deliberately none: the UMIK-1 enumerates as 0d8c:0134, C-Media's
        # generic "USB PnP Audio Device" pair shared with uncalibrated USB
        # mics, so vid:pid would admit a stranger to the stored calibration
        # (the wrong-mic incident class); and its stream is S24_3LE, which
        # the S32_LE-only wired recorder cannot open. Resolving it needs the
        # USB product string ("Umik-1 ...") and a per-model capture format.
        "usb_ids": (),
    },
    "minidsp_umik2": {
        "provider": "minidsp",
        "vendor_model": "umik-2",
        "label": "miniDSP UMIK-2",
        "tier": "reference",
        "sign_convention": "response",
        # Read off the live JTS3 unit 2026-08-18: the mic enumerates as ALSA
        # card `UMIK2` and `/proc/asound/UMIK2/usbid` holds this value.
        "usb_ids": ("2752:002b",),
    },
}

# What a measurement-mic calibration file states when nothing says otherwise:
# the microphone's own response, which JTS negates into `correction_db`. Used
# for a registry entry that forgot to declare one (a missing declaration must
# not resurrect the old wrong default — `test_supported_models_declare_a_sign_convention`
# keeps the registry explicit) and for the phone-relay upload, whose page has
# no sign control (see `_relay_calibration_from_setup`).
DEFAULT_SIGN_CONVENTION = "response"


def measurement_mic_usb_ids() -> tuple[str, ...]:
    """Every USB ``vid:pid`` this registry declares for a measurement mic.

    The one place the "is this hardware a measurement microphone?" vocabulary
    lives. ``jasper.cli.measurement_mic`` prints this list so
    ``deploy/bin/jasper-aec-reconcile`` can keep a calibrated measurement mic
    out of the voice-input candidate set without carrying its own copy of the
    ids — a measurement mic has no wake/AEC contract, so selecting one would
    silently swap the household's room microphone for an instrument.

    Lower-cased and de-duplicated, matching how the kernel writes
    ``/proc/asound/<card>/usbid`` (``%04x:%04x``). Order follows the registry.
    Models that declare no id (see ``SUPPORTED_MODELS``) contribute nothing.
    """
    ids: list[str] = []
    for spec in SUPPORTED_MODELS.values():
        for usb_id in spec.get("usb_ids") or ():
            normalized = str(usb_id).strip().lower()
            if normalized and normalized not in ids:
                ids.append(normalized)
    return tuple(ids)


def mic_tier_for_model(model_key: str | None) -> str:
    """Resolve a calibration model key to its correction-envelope trust
    tier ("reference" / "consumer" / "phone" —
    jasper.active_speaker.linearization_envelope.MIC_TIERS).

    ``None`` (no measurement mic selected/known) resolves to "phone", the
    most conservative tier — absence of mic information must never read as
    "trust it like a reference mic." ``"other"`` (the wizard's "Other
    calibrated mic" bring-your-own-curve option, see
    ``jasper/web/correction_setup.py``) resolves to "consumer": a
    calibrated but uncatalogued mic gets the middle trust level, never
    "reference" (product-taste call ratified 2026-07-23, revisit under
    #1672 once transfer-calibration gives a real basis for trusting a
    specific BYO mic at reference level). An unrecognized non-``None`` key
    (a stale or renamed registry entry) also resolves to "consumer" rather
    than raising — this is a display/trust-tier seam, not a safety gate,
    and a KeyError here would be a worse failure mode than a conservative
    guess.

    Consumed by the production analyze path: ``bind_production_analyze`` in
    ``jasper/web/correction_crossover_v2.py`` resolves the tier from the
    calibration record it already looked up and threads it onto
    ``MeasurementPriors.mic_tier``, which ``analyze_program_capture`` carries
    through to ``ProgramAnalysis.mic_tier``.
    """
    if model_key is None:
        return "phone"
    if model_key == "other":
        return "consumer"
    spec = SUPPORTED_MODELS.get(model_key)
    if spec is None:
        return "consumer"
    return str(spec["tier"])
