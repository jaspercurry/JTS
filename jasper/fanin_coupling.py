# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in → CamillaDSP coupling selector (``JASPER_FANIN_CAMILLA_COUPLING``).

The single source of truth for HOW the fan-in mixer's summed program reaches
CamillaDSP's capture. Two transports:

- ``loopback`` (the **default**) — fan-in writes the ALSA snd-aloop substream
  (``hw:Loopback,0,7``); CamillaDSP captures ``plug:jasper_capture`` (a dsnoop
  on ``hw:Loopback,1,7``). This is exactly today's production topology. With the
  flag unset or set to ``loopback``, both the fan-in daemon and the emitted
  CamillaDSP capture block are **byte-for-byte** what shipped before this module
  existed — proven by tests on both sides.

- ``fifo`` — fan-in writes a bounded named pipe; CamillaDSP File-captures it
  with an async resampler + ``enable_rate_adjust`` (the real DAC clock
  disciplines the clockless File capture via async-resampler ratio correction,
  exactly as the lean lane already does — see
  :func:`jasper.sound.camilla_yaml.emit_sound_config`'s File-capture guards).
  This removes the snd-aloop ring + dsnoop hop from the SHARED capture path,
  trading ~64 ms of loopback-ring depth for a ~3-period pipe (the latency win).

This module is import-cheap (stdlib only) so socket-activated web surfaces and
the config emitters can resolve the coupling without pulling in NumPy/SciPy.

**Format split (load-bearing).** fan-in mixes and outputs S16_LE internally
(``mixer.rs`` ``FORMAT = Format::S16LE``). The shared CamillaDSP capture format
is S32_LE (``DEFAULT_CAPTURE_FORMAT``; the ``plug:`` widens the loopback's S16
to S32 today). So under ``fifo`` the fan-in writer MUST widen each i16 sample to
i32 before writing the pipe, and the emitted File capture declares S32_LE — the
same wire format the proven usbsink lean-lane writer already emits. The wire
format is pinned here as :data:`FIFO_WIRE_FORMAT` so the Rust producer and the
Python config consumer can never disagree.

**Why a separate flag from the lean lane.** The lean lane
(``JASPER_LEAN_LANE`` / ``stage_lean_capture_config`` /
``apply_lean_capture_config``) swaps CamillaDSP's capture to a File pipe fed by
ONE exclusive wired source (usbsink), bypassing the mixer entirely. This
coupling keeps the FULL fan-in mixer (all renderer lanes, TTS, ducking,
music-only tap) and only changes how the mixer's *output* reaches Camilla. They
are different points on the same convergence: once ``fifo`` soaks, it supersedes
the lean lane's separate File-capture path. **Do not delete the lean lane or the
adaptive output-buffer shrink yet** — they are superseded *after* this soaks,
not before.
"""

from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
    DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
)

# Environment selector. Read at config-emit time and at fan-in daemon startup.
COUPLING_ENV_VAR = "JASPER_FANIN_CAMILLA_COUPLING"

# The two accepted transports. ``loopback`` is the default and the
# byte-identical-to-today path.
COUPLING_LOOPBACK = "loopback"
COUPLING_FIFO = "fifo"
_VALID_COUPLINGS = frozenset({COUPLING_LOOPBACK, COUPLING_FIFO})

# The shared-capture named pipe written by fan-in under ``fifo`` and File-read by
# CamillaDSP. DISTINCT from the lean lane's FIFO (``DEFAULT_LEAN_CAPTURE_FIFO`` =
# /run/jasper-usbsink/lean.pipe), which is fed by usbsink, not the mixer. Lives
# under the fan-in daemon's own /run dir (tmpfs, recreated each boot) so the
# producer owns it (mirrors the snapserver SNAPFIFO / lean-lane single-owner
# idiom). Overridable via ``JASPER_FANIN_CAMILLA_FIFO`` for the soak.
FIFO_PATH_ENV_VAR = "JASPER_FANIN_CAMILLA_FIFO"
DEFAULT_FANIN_CAMILLA_FIFO = "/run/jasper-fanin/camilla.pipe"

# Wire format on the pipe. fan-in mixes S16 internally but widens to S32_LE on
# the wire so the File capture matches the SHARED capture format (S32_LE), the
# same width the proven usbsink lean writer emits. Pinned here so producer and
# consumer can never drift.
FIFO_WIRE_FORMAT = "S32_LE"


def resolve_coupling(raw: str | None) -> str:
    """Normalize a raw ``JASPER_FANIN_CAMILLA_COUPLING`` value to a transport.

    Fail-SAFE to ``loopback`` (the byte-identical-to-today path) on unset, empty,
    or any unrecognized value — a typo in the env file must never silently flip
    the shared realtime capture to a transport the operator did not intend, nor
    crash a config emit. The Rust daemon applies the same normalization so both
    sides agree. Case-insensitive; surrounding whitespace ignored.
    """
    if raw is None:
        return COUPLING_LOOPBACK
    value = raw.strip().lower()
    if value in _VALID_COUPLINGS:
        return value
    return COUPLING_LOOPBACK


def is_fifo_coupling(raw: str | None) -> bool:
    """True iff the resolved coupling is ``fifo``. Convenience predicate."""
    return resolve_coupling(raw) == COUPLING_FIFO


def capture_kwargs_for_coupling(
    raw: str | None,
    *,
    fifo_path: str | None = None,
) -> dict[str, object]:
    """Return the ``emit_sound_config`` capture kwargs for the resolved coupling.

    - ``loopback`` (default): returns ``{}`` so the caller's existing
      ``capture_device`` / ``capture_format`` defaults emit the dsnoop ALSA
      capture — **byte-identical** to today. This empty-dict contract is what
      keeps every existing caller unchanged when the flag is unset.

    - ``fifo``: returns the File-capture kwargs — ``capture_pipe_path`` (the
      pipe fan-in writes), ``resampler_type`` (AsyncSinc), and
      ``enable_rate_adjust=True``. These satisfy ``emit_sound_config``'s
      fail-loud File-capture guards (a clockless File capture REQUIRES BOTH the
      async resampler AND rate-adjust). ``capture_format`` is left to the
      emitter's S32_LE default (== :data:`FIFO_WIRE_FORMAT`).

    ``fifo_path`` overrides the pipe path (the env override is resolved by
    :func:`resolve_fifo_path`; pass its result here so the emitted config and the
    daemon point at the same pipe).
    """
    if resolve_coupling(raw) != COUPLING_FIFO:
        return {}
    return {
        "capture_pipe_path": fifo_path or DEFAULT_FANIN_CAMILLA_FIFO,
        "resampler_type": DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
        "resampler_profile": DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
        "enable_rate_adjust": True,
    }


def resolve_fifo_path(raw_path: str | None) -> str:
    """Resolve the shared-capture FIFO path from a raw env value.

    Empty / unset → :data:`DEFAULT_FANIN_CAMILLA_FIFO`. Trims whitespace. The
    Rust daemon resolves ``JASPER_FANIN_CAMILLA_FIFO`` the same way so the
    producer and the File-capture consumer always name the same pipe.
    """
    if raw_path is None:
        return DEFAULT_FANIN_CAMILLA_FIFO
    value = raw_path.strip()
    return value or DEFAULT_FANIN_CAMILLA_FIFO


def coupling_capture_kwargs_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Resolve the live ``emit_sound_config`` capture kwargs from the process env.

    The one call shape a config emitter uses to thread the SHARED fan-in→Camilla
    coupling into a live re-emit: reads :data:`COUPLING_ENV_VAR` +
    :data:`FIFO_PATH_ENV_VAR` together so the emitted File-capture config names
    the SAME pipe the Rust ``FifoWriter`` writes (the path env is resolved by
    :func:`resolve_fifo_path` on BOTH sides). Returns ``{}`` for the default
    ``loopback`` coupling (byte-identical to today). Read at emit time — a
    systemd ``EnvironmentFile`` flip takes effect on the next config regeneration
    without a code edit, exactly like the CamillaDSP latency knobs.
    """
    import os

    source = os.environ if env is None else env
    return capture_kwargs_for_coupling(
        source.get(COUPLING_ENV_VAR),
        fifo_path=resolve_fifo_path(source.get(FIFO_PATH_ENV_VAR)),
    )


def member_kwargs_are_pipe_sink(member_kwargs: dict[str, object] | None) -> bool:
    """True when the resolved grouping member kwargs are a SnapFIFO pipe sink.

    A bonded/grouped member (active-leader program bake, or a passive grouping
    follower leader) writes CamillaDSP's playback to the Snapcast pipe with
    ``enable_rate_adjust=False`` (snapclient is the sole rate-tracker — the
    multiroom inv-5). That is mutually exclusive with the FIFO COUPLING's File
    *capture*, which REQUIRES ``enable_rate_adjust=True`` to discipline the
    clockless pipe input. So when this is True, FIFO coupling must be a no-op for
    that emit (the grouped capture topology is the Distributed-Active track's
    concern, not this solo-speaker latency hop). The solo defaults
    (``enable_rate_adjust`` truthy / absent, no ``playback_pipe_path``) return
    False → coupling applies. Mirrors ``jasper.multiroom.member_config``'s
    leader-vs-solo distinction without importing it (keeps this module
    import-cheap for the socket-activated emitters).
    """
    if not member_kwargs:
        return False
    if member_kwargs.get("playback_pipe_path"):
        return True
    # An explicit enable_rate_adjust=False is the pipe-sink signal even if the
    # path resolution is deferred; treat it as a sink to stay fail-safe.
    return member_kwargs.get("enable_rate_adjust") is False
