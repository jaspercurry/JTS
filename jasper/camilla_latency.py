# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a CamillaDSP graph's ``chunksize`` / ``target_level`` pair.

Sits above :mod:`jasper.camilla_config_contract`, which owns the vocabulary and
stays a leaf. Resolution is not vocabulary: it reads the process environment,
the active DAC's declared ``CamillaFloor``, the lab-override artifact and the
ring's capacity. Emitters take the constants from the contract and the
resolvers from here.
"""

from __future__ import annotations

import os
from typing import Mapping

from jasper.camilla_config_contract import DEFAULT_CHUNKSIZE, DEFAULT_TARGET_LEVEL
from jasper.fanin_coupling import RING_PCM_DEVICES, ring_capacity_frames


# Separates "caller passed no profile_floor -> auto-resolve the active DAC's
# codified floor" from an explicit ``profile_floor=None`` ("no floor, keep the
# global default" — the byte-identical path the emitters' own None-sentinel
# relies on). Auto-resolution reads the DacProfile registry directly, so the
# floor reaches every live generation path whether or not that path has
# outputd.env in its environment.
class _Unset:
    __slots__ = ()


_UNSET = _Unset()


def _active_camilla_floor(field: str) -> int | None:
    """The active output DAC's declared ``CamillaFloor.<field>``, or None.

    None when no DAC is resolved, the profile is unknown, or the DAC declares
    no floor: the caller keeps the global default, so a box whose record is not
    written yet still generates a config. Imported lazily so an emit-only
    surface never pays for the DAC registry.
    """
    try:
        from jasper.audio_hardware.dac import camilla_floor_for
        from jasper.output_hardware import active_dac_profile_id
    except ImportError:
        return None
    profile_id = active_dac_profile_id()
    if profile_id is None:
        return None
    floor = camilla_floor_for(profile_id)
    if floor is None:
        return None
    return int(getattr(floor, field))


def _lab_override_allows_below_floor(
    env_var: str,
    value: int,
    env: Mapping[str, str],
) -> bool:
    """Whether an explicit audio-runtime lab override owns ``value``.

    Lab tuning may probe below the DacProfile stability floor, but only when
    ``audio_runtime_overrides.json`` carries the same active value — a stale
    ``outputd.env`` value stays clamped.
    """
    try:
        from jasper.audio_runtime_overrides import (
            load_runtime_overrides,
            runtime_overrides_path,
        )
    except ImportError:
        return False
    overrides = load_runtime_overrides(runtime_overrides_path(env))
    raw = overrides.values().get(env_var)
    try:
        override_value = int(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return override_value == value


def _resolve_camilla_int(
    env_var: str,
    default: int,
    env: Mapping[str, str],
    profile_floor: int | None,
) -> int:
    """A positive-int CamillaDSP latency knob with floor precedence.

    max(explicit operator env, active DacProfile floor) > global default;
    ``profile_floor`` None keeps the default. A value below the floor is
    clamped back up, making the floor a real stability bound and not just a
    fresh-box default. Unset OR malformed (non-int, zero, negative) degrades to
    the fallback: a bad override must never emit a config that will not load.
    Read at emitter-call time, so an EnvironmentFile change lands on the next
    regeneration.
    """
    fallback = default if profile_floor is None else profile_floor
    raw = str(env.get(env_var, "")).strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    if value <= 0:
        return fallback
    if profile_floor is not None and value < profile_floor:
        if _lab_override_allows_below_floor(env_var, value, env):
            return value
        return profile_floor
    return value


def resolve_camilla_chunksize(
    env: Mapping[str, str] | None = None,
    profile_floor: int | None | _Unset = _UNSET,
) -> int:
    """``JASPER_CAMILLA_CHUNKSIZE`` or the active DAC's floor or 1024.

    ``profile_floor`` left unset (the live-emitter default) auto-resolves the
    active DAC profile's floor; pass ``None`` to force the no-floor path.
    """
    if isinstance(profile_floor, _Unset):
        profile_floor = _active_camilla_floor("chunksize")
    return _resolve_camilla_int(
        "JASPER_CAMILLA_CHUNKSIZE", DEFAULT_CHUNKSIZE,
        os.environ if env is None else env,
        profile_floor,
    )


def resolve_camilla_target_level(
    env: Mapping[str, str] | None = None,
    profile_floor: int | None | _Unset = _UNSET,
) -> int:
    """``JASPER_CAMILLA_TARGET_LEVEL`` or the active DAC's floor or 2048.

    See :func:`resolve_camilla_chunksize` for the sentinel's two meanings.
    """
    if isinstance(profile_floor, _Unset):
        profile_floor = _active_camilla_floor("target_level")
    return _resolve_camilla_int(
        "JASPER_CAMILLA_TARGET_LEVEL", DEFAULT_TARGET_LEVEL,
        os.environ if env is None else env,
        profile_floor,
    )


def resolve_camilla_latency_for_devices(
    *,
    capture_device: str,
    playback_device: str | None,
    chunksize: int | None = None,
    target_level: int | None = None,
) -> tuple[int, int]:
    """The ``(chunksize, target_level)`` a graph between these devices needs.

    A caller value passed here is returned untouched; only the half left
    ``None`` is resolved.

    WHY A DEVICE DECIDES THIS. Since ADR-0100 CamillaDSP's chunk crosses THE
    RING, whose capacity is a compile-time constant of the fan-in writer and
    the ioplug — identical on every box, unrelated to which DAC is fitted. A
    chunk larger than that cannot be negotiated: CamillaDSP exits ("Trying to
    set avail_min to N, must be smaller than or equal to device buffer size of
    256") and systemd restart-loops it, which is silent deafness (AGENTS.md
    #6). So a ring end CLAMPS the chunk to the ring's capacity. A floor that
    already fits is the box's own tuning and passes through untouched.

    ``target_level`` is bounded by CamillaDSP relative to the chunk, not by the
    ring, so a clamped chunk drags its ceiling down with it — see the clamp.

    ``playback_device=None`` is a CLOCKLESS sink (a ``File`` — the bonded
    leader's snapserver FIFO, the parked graph's ``/dev/null``): it declares no
    ALSA buffer, so a ring capture is the only ALSA end and governs. A non-ring
    ALSA playback device keeps the box's floor whole even when capture is Ring
    A, because that sink's own hardware buffer is what the process must feed.
    """

    resolved_target = target_level is None
    if target_level is None:
        target_level = resolve_camilla_target_level()
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
        governing_device = (
            capture_device if playback_device is None else playback_device
        )
        if governing_device in RING_PCM_DEVICES:
            capacity = ring_capacity_frames()
            if chunksize > capacity:
                # THE PAIR SCALES TOGETHER. CamillaDSP bounds target_level at
                # `chunksize x (queuelimit + 4)` — measured against 4.1.3,
                # exact across chunk 128/256/512 and queuelimit 1/2/4 — so the
                # ceiling falls with the chunk. Clamping the chunk alone pushed
                # a floor-declared target of 4096 over the new 2048 ceiling and
                # swapped one fatal config for another. The same ratio keeps
                # the pair valid without encoding CamillaDSP's formula here.
                if resolved_target:
                    target_level = max(1, target_level * capacity // chunksize)
                chunksize = capacity
    return chunksize, target_level
