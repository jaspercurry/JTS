# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement-microphone model registry: which mics JTS knows, and how
to recognise one as measurement-class hardware.

Split out of :mod:`jasper.audio_measurement.calibration` (which re-exports
everything here) with one hard constraint: **this module imports nothing
beyond the stdlib**. It backs ``python -m jasper.cli.measurement_mic``, which
``deploy/bin/jasper-aec-reconcile`` spawns from the hotplug path on every
managed-XVF pass; pulling numpy in here re-inflates that spawn to a 190-module
interpreter (~85 ms measured on the dev host). Keep it pure data plus dict
lookups.
"""
from __future__ import annotations

from typing import Any

# Single source of truth for supported measurement mics. Adding a mic here
# wires the vendor lookup, the model picker, the wrong-mic guard, AND the
# wizard's label-based auto-inference (see model_label_aliases in
# jasper.audio_measurement.calibration). Optional `label_aliases` overrides the
# default (the vendor_model) when a mic's OS device label does not contain its
# vendor model string.
#
# `tier` is the correction-envelope trust tier — vocabulary owned by
# jasper.active_speaker.linearization_envelope.MIC_TIERS ("reference" /
# "consumer" / "phone"), duplicated here as plain literals because
# audio_measurement never imports upward into active_speaker.
#
# `usb_ids` is the device's USB `vid:pid` as the kernel spells it in
# `/proc/asound/<card>/usbid`, so the rest of JTS can recognise measurement
# hardware — chiefly so `deploy/bin/jasper-aec-reconcile` never selects one as
# the voice/wake input (see `measurement_mic_usb_ids`). USB id, not serial: a
# UMIK-2's USB serial descriptor is the literal "00000" on every unit. An empty
# tuple means "this project has not measured this model's id", never a guess —
# a wrong id could exclude somebody's voice array.
#
# `sign_convention` is what the VENDOR's file states, and therefore how
# `fetch_vendor_calibration` must parse it; per-entry rather than one hardcode
# in the fetcher, which stored every vendor record as an already-negated
# correction and made the pipeline ADD what it should have SUBTRACTED (an error
# of exactly twice the file's value, |4.85| dB worst case on the live JTS3
# UMIK-2 file across 20 Hz-20 kHz). Every entry today is "response": miniDSP by
# physical proof (one UMIK-2's 0° and 90° files differ ~9.4 dB at 20 kHz with
# the 90° file MORE negative, which only a response can be), Dayton by REW's
# documented cal-file semantics, which both vendors publish for.
SUPPORTED_MODELS: dict[str, dict[str, Any]] = {
    "dayton_imm6": {
        "provider": "dayton_audio",
        "vendor_model": "iMM-6",
        "label": "Dayton Audio iMM-6 / iMM-6C",
        "tier": "consumer",
        "sign_convention": "response",
        # Not measured: the iMM-6 is a TRRS mic that enumerates through
        # whatever interface it is plugged into; the iMM-6C is USB-C.
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
        # mics, so vid:pid would admit a stranger to the stored calibration;
        # and its stream is S24_3LE, which the S32_LE-only wired recorder
        # cannot open. Resolving it needs the USB product string and a
        # per-model capture format.
        "usb_ids": (),
    },
    "minidsp_umik2": {
        "provider": "minidsp",
        "vendor_model": "umik-2",
        "label": "miniDSP UMIK-2",
        "tier": "reference",
        "sign_convention": "response",
        # Read off the live JTS3 unit: ALSA card `UMIK2`,
        # `/proc/asound/UMIK2/usbid` = 2752:002b.
        "usb_ids": ("2752:002b",),
    },
}

# What a measurement-mic calibration file states when nothing says otherwise:
# the microphone's own response, which JTS negates into `correction_db`. Used
# for a registry entry that declared none (kept explicit by
# `test_supported_models_declare_a_sign_convention`) and for an upload whose
# page has no sign control.
DEFAULT_SIGN_CONVENTION = "response"


def measurement_mic_usb_ids() -> tuple[str, ...]:
    """Every USB ``vid:pid`` this registry declares for a measurement mic.

    ``jasper.cli.measurement_mic`` prints this list so
    ``deploy/bin/jasper-aec-reconcile`` can keep a calibrated measurement mic
    out of the voice-input candidate set — a measurement mic has no wake/AEC
    contract. Lower-cased and de-duplicated, matching how the kernel writes
    ``/proc/asound/<card>/usbid`` (``%04x:%04x``); order follows the registry.
    """
    ids: list[str] = []
    for spec in SUPPORTED_MODELS.values():
        for usb_id in spec.get("usb_ids") or ():
            normalized = str(usb_id).strip().lower()
            if normalized and normalized not in ids:
                ids.append(normalized)
    return tuple(ids)


def mic_tier_for_model(model_key: str | None) -> str:
    """Resolve a calibration model key to its correction-envelope trust tier.

    ``None`` (no measurement mic selected/known) resolves to "phone", the most
    conservative tier — absence of mic information must never read as "trust it
    like a reference mic". ``"other"`` (the wizard's bring-your-own-curve
    option) and an unrecognized non-``None`` key both resolve to "consumer":
    this is a display/trust-tier seam, not a safety gate, so a conservative
    answer beats a KeyError.
    """
    if model_key is None:
        return "phone"
    if model_key == "other":
        return "consumer"
    spec = SUPPORTED_MODELS.get(model_key)
    if spec is None:
        return "consumer"
    return str(spec["tier"])
