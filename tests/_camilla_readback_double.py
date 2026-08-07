# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A CamillaDSP double that default-fills its readback the way the real DSP does.

**Why this module exists.** CamillaDSP never echoes the config file back. Both
``GetConfig`` (the running-graph readback behind
:meth:`jasper.camilla.CamillaController.get_active_config_raw`) and
``ReadConfig`` (the canonicalizer behind
:meth:`jasper.camilla.CamillaController.normalize_config_raw`) return
CamillaDSP's OWN serialization, in which every key the file leaves out is
present and explicit. JTS's emitters leave those keys out.

A fake that echoes the file hides that asymmetry completely — and it did. Every
test of the live-graph boundary in
:func:`jasper.active_speaker.runtime_contract.classify_active_bass_extension_graph`
used an echoing fake, so the boundary looked proven while on real hardware it
compared JTS-authored text against a default-filled readback and refused
everything it was asked to prove. That gate silently blocked commissioning,
correction, bass extension and both grouping roles until the 2026-08-06 bonded
pair went quiet and it was traced back. This module exists so a fake cannot be
politer than the DSP again.

**Provenance.** The default set below is transcribed from a real capture:
CamillaDSP 4.1.3 on jts5, 2026-08-06, ``GetConfig`` against a JTS-emitted
active-speaker baseline, which added 36 distinct key paths the file omitted
(counting each list entry once; 49 if every list entry is counted separately —
the fixture pair in ``tests/fixtures/camilla_readback/`` is the actual evidence,
and the number depends on how you count).

**What it models, and what it does not.** It models the one property the
boundary depends on: CamillaDSP returns a normalized *superset* of the file, so
raw-file text and readback text never compare equal. It is deliberately NOT a
byte-exact replica of any one CamillaDSP version's schema — pinning that here
would make this a second source of truth for someone else's config format, and
it would rot at the next upgrade. Tests that need byte-exact CamillaDSP
behaviour need the real binary on the Pi, not this.

**The bound on what these tests prove — read before trusting a green run.**
``camilla_canonicalize`` (``ReadConfig``) and ``camilla_readback``
(``GetConfig``) are the SAME function here, and the committed fixture pair
captures ``GetConfig`` only. So every test using this double *assumes*
``ReadConfig(f) ≡ GetConfig(running f)`` — it does not demonstrate it, and no
committed fixture does either. That equality is the premise the whole
canonicalize seam rests on: if the two ever diverge, the boundary compares two
different normalizations and refuses a graph that is in fact correct.

The evidence for it is a read-only hardware probe, not a test: jts.local,
CamillaDSP 4.1.3, 2026-08-05, which measured ``ReadConfig``'s output *exactly
equal* to ``GetConfig``'s readback for identical content — recorded in
``docs/HANDOFF-crossover-measurement-v2.md`` ("So CamillaDSP canonicalizes for
us"). One box, one version, one day. Re-measure it rather than assume it after
a CamillaDSP upgrade; a fixture pair capturing ``ReadConfig`` alongside
``GetConfig`` would turn the premise into a pin, and is worth capturing on the
next hardware pass.
"""

from __future__ import annotations

from typing import Any

import yaml

#: Keys CamillaDSP 4.1.3 adds at each level of a JTS-emitted config.
_TOP_LEVEL_DEFAULTS = {"title": None, "description": None, "processors": None}
_DEVICE_DEFAULTS = {
    "adjust_period": None,
    "capture_samplerate": None,
    "multithreaded": None,
    "rate_measure_interval": None,
    "resampler": None,
    "silence_threshold": None,
    "silence_timeout": None,
    "stop_on_rate_change": None,
    "volume_ramp_time": None,
    "worker_threads": None,
}
#: Capture-side only. Both endpoints in the capture are ``type: Alsa``, yet the
#: capture side gains these four and the playback side gains none — so the rule
#: is which side of the graph it is, NOT the device type. Applying them to both
#: over-fills by four keys, which the fixture pin catches.
_CAPTURE_DEFAULTS = {
    "labels": None,
    "link_mute_control": None,
    "link_volume_control": None,
    "stop_on_inactive": None,
}
_MIXER_MAPPING_DEFAULTS = {"mute": None}
_MIXER_SOURCE_DEFAULTS = {"mute": None, "scale": None}
_PIPELINE_STEP_DEFAULTS = {"description": None, "bypassed": None}
_FILTER_DEFAULTS = {"description": None}
#: Parameter defaults are per filter TYPE, not blanket — a ``Gain`` gains
#: ``scale`` and a ``Delay`` gains ``subsample``, while ``Limiter`` and
#: ``BiquadCombo`` gain neither. Filling them blindly would hand the graph
#: classifiers keys the real DSP never emits, which is its own kind of lie.
_FILTER_PARAMETER_DEFAULTS_BY_TYPE = {
    "Gain": {"scale": None},
    "Delay": {"subsample": None},
}


def _fill(target: Any, defaults: dict[str, Any]) -> None:
    if isinstance(target, dict):
        for key, value in defaults.items():
            target.setdefault(key, value)


def camilla_default_filled(text: str) -> str:
    """Return ``text`` as CamillaDSP itself would serialize it.

    Parses, adds the keys CamillaDSP fills in, and re-serializes — which also
    drops comments and normalizes quoting, exactly as the real readback does.
    """

    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        return text
    _fill(parsed, _TOP_LEVEL_DEFAULTS)
    devices = parsed.get("devices")
    _fill(devices, _DEVICE_DEFAULTS)
    if isinstance(devices, dict):
        _fill(devices.get("capture"), _CAPTURE_DEFAULTS)
    mixers = parsed.get("mixers")
    if isinstance(mixers, dict):
        for mixer in mixers.values():
            mapping = mixer.get("mapping") if isinstance(mixer, dict) else None
            for entry in mapping if isinstance(mapping, list) else ():
                _fill(entry, _MIXER_MAPPING_DEFAULTS)
                sources = entry.get("sources") if isinstance(entry, dict) else None
                for source in sources if isinstance(sources, list) else ():
                    _fill(source, _MIXER_SOURCE_DEFAULTS)
    pipeline = parsed.get("pipeline")
    for step in pipeline if isinstance(pipeline, list) else ():
        _fill(step, _PIPELINE_STEP_DEFAULTS)
    filters = parsed.get("filters")
    if isinstance(filters, dict):
        for spec in filters.values():
            _fill(spec, _FILTER_DEFAULTS)
            if isinstance(spec, dict):
                _fill(
                    spec.get("parameters"),
                    _FILTER_PARAMETER_DEFAULTS_BY_TYPE.get(spec.get("type"), {}),
                )
    return yaml.safe_dump(parsed, sort_keys=True)


async def camilla_canonicalize(text: str) -> str:
    """Stand in for ``ReadConfig`` — the canonicalizer the boundary injects."""

    return camilla_default_filled(text)


def camilla_readback(text: str):
    """Build a ``GetConfig`` stand-in returning the default-filled ``text``."""

    async def _read() -> str:
        return camilla_default_filled(text)

    return _read
