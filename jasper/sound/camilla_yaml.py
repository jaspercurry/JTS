# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit CamillaDSP configs for sound curves and preference EQ.

The generated config preserves the base JTS audio path and any existing
room-correction PEQs, then appends preference filters. That ordering is
intentional: room correction fixes the room; preference EQ shapes what
the listener likes after that correction.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.multiroom.channel_split import ChannelSplit, weave_channel_split
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    PeqFilter,
    ensure_volume_limit_db,
    file_capture_resampler_yaml,
    is_async_resampler,
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
)
from jasper.camilla_emit import emit_master_gain_pipeline
from jasper.camilla_stereo_prefix import build_stereo_prefix

from .profile import (
    SoundProfile,
    build_sound_filters,
)

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = Path("/etc/camilladsp/outputd-cutover.yml")
SOUND_CONFIG_NAME = "sound_current.yml"
SOUND_AUDITION_CONFIG_NAME = "sound_audition.yml"
_JTS_GENERATED_RE = re.compile(
    r"^(?:correction_[A-Za-z0-9]+_\d+|sound_current|sound_audition"
    r"|sound_snapshot_[A-Za-z0-9]+_\d+|sound_reset_[A-Za-z0-9]+_\d+"
    r"|sound_lean_current"
    r"|correction_measurement_[A-Za-z0-9]+_\d+"
    r"|grouping_leader|grouping_solo_restore|grouping_follower)\.yml$"
)

# Lean-lane File-capture resampler vocabulary lives in the shared contract
# (jasper.camilla_config_contract): DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE /
# _PROFILE, is_async_resampler(), file_capture_resampler_yaml(). Imported above
# so the stereo and active-speaker emitters share one v4-schema definition.


def emit_sound_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    room_peqs_right: list[PeqFilter] | None = None,
    channel_delays_ms: tuple[float, float] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    playback_device: str = DEFAULT_PLAYBACK_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    queuelimit: int = 4,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
    output_trim_db: float = 0.0,
    enable_rate_adjust: bool = True,
    channel_split: ChannelSplit | None = None,
    playback_pipe_path: str | None = None,
    capture_pipe_path: str | None = None,
    resampler_type: str | None = None,
    resampler_profile: str | None = DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
) -> str:
    """Build a CamillaDSP YAML config for the preference profile.

    ``room_peqs_right`` is the multi-room leader-bake axis
    (docs/HANDOFF-multiroom.md §2 "Canonical signal flow"): a DIFFERENT
    room correction per channel in ONE config — channel 0 gets
    ``room_peqs`` (the leader's seat), channel 1 gets ``room_peqs_right``
    (the follower's seat); preference EQ stays shared (taste, not seat).
    ``None`` (default — solo) duplicates ``room_peqs`` onto channel 1,
    **byte-identical** to before this parameter existed (the solo-impact
    contract). ``[]`` bakes a FLAT right room segment (an uncalibrated
    follower ships flat, never the wrong-room curve). Deliberately a
    2-channel axis — 2.1's 3-channel stream generalises it together with
    the stereo-pinned config contract (HANDOFF-multiroom.md §2); do not
    pre-generalise it alone.

    ``channel_delays_ms`` is the room/pair time-of-arrival axis that
    belongs with measured correction, not Snapcast transport sync. It is
    stereo-pinned (``(left_ms, right_ms)``), positive-only, and emitted as
    CamillaDSP ``Delay`` filters inside the per-room chain. ``None``
    (default — solo) and ``(0, 0)`` emit no delay filters, preserving the
    solo byte contract. Delays are for static acoustic alignment at the
    listening seat; Snapcast still owns distributed clock/transport sync.

    ``channel_split`` (a :class:`jasper.multiroom.channel_split.ChannelSplit`)
    is woven in for a bonded member that plays a single channel — the
    ``channel_select`` mixer + (for a sub) the crossover. ``None`` or a
    passthrough (``stereo``) split leaves the config untouched, so a solo
    speaker is byte-for-byte unchanged.

    ``room_peqs_right`` and ``channel_split`` are MUTUALLY EXCLUSIVE —
    they belong to different topology models (leader-bake pre-stream
    correction vs. the member-side channel-selection weave) and
    combining them raises ``ValueError`` (see the guard below).

    ``playback_pipe_path`` is the BONDED-LEADER playback axis
    (docs/HANDOFF-multiroom.md §2, Increment 5): when set, the playback
    device becomes a CamillaDSP ``File`` sink writing the corrected
    stereo program to that FIFO (snapserver's pipe source) instead of
    the ALSA loopback. ``None`` (default — solo) is **byte-identical**
    to before this parameter existed (the solo-impact contract). Two
    fail-loud guards: a pipe sink REQUIRES ``enable_rate_adjust=False``
    (a ``File`` backend has no output clock for rate_adjust to steer —
    Snapcast's sample-stuffing is the one rate-tracker on the synced
    chain, §2 invariant 5), and it never combines with
    ``channel_split`` (the member weave selects a channel for a LOCAL
    DAC; the pipe carries the SHARED two-channel program — members drop
    channels downstream, in outputd's ChannelPick)."""

    # Loud-output safety: refuse to emit a config whose master fader
    # could boost above full scale. Mirrors the active_speaker emitter.
    volume_limit_db = ensure_volume_limit_db(volume_limit_db)
    # CamillaDSP latency knobs (G7): None → env-or-default, resolved at call
    # time so a JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} systemd override applies
    # on the next regeneration. Unset env → the literal defaults (1024/2048), so
    # the emitted YAML is byte-identical absent an opt-in. An explicit caller
    # value still wins.
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
    if channel_delays_ms is not None:
        if len(channel_delays_ms) != 2:
            raise ValueError("channel_delays_ms must be a (left_ms, right_ms) pair")
        left_delay, right_delay = channel_delays_ms
        for label, value in (("left", left_delay), ("right", right_delay)):
            if not math.isfinite(float(value)):
                raise ValueError(f"{label} channel delay must be finite")
            if float(value) < 0.0:
                raise ValueError(f"{label} channel delay must be positive-only")
        channel_delays_ms = (float(left_delay), float(right_delay))
        if channel_delays_ms != (0.0, 0.0) and room_peqs_right is None:
            raise ValueError(
                "channel_delays_ms requires room_peqs_right so the two "
                "speaker channels have distinct room chains"
            )

    # Contract guard (fail LOUD at the API boundary, before Increment 5
    # wires real callers): room_peqs_right is the multi-room LEADER-BAKE
    # axis — a pre-stream config carrying a different per-seat correction
    # per channel — while channel_split is the member-side
    # channel-selection weave from the superseded self-correct model.
    # Combined, channel_select would run AHEAD of the per-channel filters,
    # duplicating one program channel onto both outputs and then
    # "correcting" the duplicate with the other seat's chain — nonsense
    # audio. No topology ever combines them (even a passthrough split:
    # the axes come from different call paths, so both-present indicates
    # a wiring bug). See HANDOFF-multiroom.md §2.
    if room_peqs_right is not None and channel_split is not None:
        raise ValueError(
            "room_peqs_right (leader-bake per-channel correction) and "
            "channel_split (member channel-selection weave) are mutually "
            "exclusive topology axes — see HANDOFF-multiroom.md §2"
        )
    # Stage 4 lean-lane File-CAPTURE guards (fail LOUD at the API boundary,
    # the MIRROR of the pipe-SINK guards below). A File capture has no clock,
    # so CamillaDSP cannot rate-tune the capture side (rate-adjust method 1 is
    # unavailable). The real DAC on playback IS the chain's clock; the only way
    # it can discipline a clockless File capture is rate-adjust method 2 —
    # async-resampler ratio correction. So a File capture REQUIRES BOTH
    # enable_rate_adjust=True AND an async resampler. Emitting either without
    # the other lets the solo lean lane free-run against the DAC (the Stage-1
    # AEC3-path drift hazard); refuse it rather than ship silent drift. Placed
    # ABOVE the pipe-sink block so the both-set case raises its own message
    # first (the sink block would otherwise raise on enable_rate_adjust).
    if capture_pipe_path is not None:
        if playback_pipe_path is not None:
            # A File-in/File-out config was the removed transport_pipe coupling;
            # no supported topology combines a File capture with a File sink.
            raise ValueError(
                "capture_pipe_path (File capture) and playback_pipe_path "
                "(File sink) cannot be combined"
            )
        if not enable_rate_adjust:
            raise ValueError(
                "capture_pipe_path (File capture) requires "
                "enable_rate_adjust=True — the real DAC playback clock "
                "disciplines the clockless File capture via CamillaDSP's "
                "async-resampler ratio correction; without it the lean lane "
                "free-runs (Stage-1 AEC3 drift hazard)"
            )
        if not is_async_resampler(resampler_type):
            raise ValueError(
                "capture_pipe_path (File capture) requires an async resampler "
                "(AsyncSinc/AsyncPoly — CamillaDSP v4 vocabulary) — "
                "enable_rate_adjust on a clockless File capture has nothing to "
                f"steer without one; got resampler_type={resampler_type!r}"
            )
    # Bonded-leader pipe-sink guards (fail LOUD at the API boundary,
    # same pattern as above). A File sink has no output clock, so
    # rate_adjust has nothing to steer — and the synced chain's one
    # rate-tracker must be snapclient's sample-stuffing (§2 invariant
    # 5); silently emitting `enable_rate_adjust: true` on a pipe config
    # would hide a wiring bug in the caller. And the pipe carries the
    # SHARED stereo program — a member's channel_split weave on it
    # would strip the other speaker's channel out of the stream.
    if playback_pipe_path is not None:
        if enable_rate_adjust:
            raise ValueError(
                "playback_pipe_path (bonded-leader pipe sink) requires "
                "enable_rate_adjust=False — snapclient is the sole "
                "rate-tracker on the synced chain; see "
                "HANDOFF-multiroom.md §2 invariant 5"
            )
        if channel_split is not None:
            raise ValueError(
                "playback_pipe_path (the shared-stream pipe sink) and "
                "channel_split (member channel-selection weave) are "
                "mutually exclusive — members drop channels downstream "
                "of the stream, never inside it; see "
                "HANDOFF-multiroom.md §2"
            )
    # The shared stereo-prefix builder (jasper.camilla_stereo_prefix) owns the
    # room-PEQ -> headroom -> preamp -> preference assembly. Build the active
    # preference filters once and pass them in (it drops inactive specs);
    # reuse the same list for the summary log below.
    sound_filters = build_sound_filters(profile)
    filter_yaml, chain_names, chain_names_right, trim_db = build_stereo_prefix(
        sound_filters,
        room_peqs or [],
        room_peqs_right=room_peqs_right,
        output_trim_db=output_trim_db,
        channel_delays_ms=channel_delays_ms,
    )
    # Structure is the shared primitive; this module owns only which
    # names go in each chain (room L/R segments + the shared tail).
    pipeline_yaml = emit_master_gain_pipeline(chain_names, chain_names_right)
    # inv-5: an active bond member runs rate_adjust off (snapclient is the sole
    # rate-tracker); default True keeps the solo path unchanged.
    rate_adjust_literal = "true" if enable_rate_adjust else "false"
    header_id = f" (id={profile_id})" if profile_id else ""
    # Playback sink: ALSA loopback (solo — the default, byte-identical)
    # or the bonded-leader File/pipe sink feeding snapserver. Identical
    # indentation so the surrounding template is sink-agnostic.
    if playback_pipe_path is not None:
        playback_yaml = f"""  playback:
    type: File
    channels: 2
    filename: "{playback_pipe_path}"
    format: {playback_format}"""
    else:
        playback_yaml = f"""  playback:
    type: Alsa
    channels: 2
    device: "{playback_device}"
    format: {playback_format}"""
    # Capture source: ALSA loopback (solo — the default, byte-identical) or
    # the Stage-4 lean-lane named-pipe capture fed by a
    # timing-preserving source (USB / shairport pipe).
    #
    # MUST be `RawFile`, NOT `File`. CamillaDSP v4 has NO `File` *capture*
    # variant (capture is Alsa/RawFile/WavFile/Stdin/SignalGenerator); `File`
    # is a *playback*-only type (the multiroom sink at playback_pipe_path above).
    # `type: File` here emitted a config CamillaDSP rejects with "unknown variant
    # `File`" — a silent capture outage that slipped past build, review AND CI
    # because no test ran `camilladsp --check`. Caught live on jts5 (CamillaDSP
    # 4.1.3) 2026-06-27. RawFile reads raw interleaved PCM from the pipe; the
    # apply's `--check` OPENS the pipe, so fan-in must already be writing it
    # before the reconcile loads this config (fan-in-first arm ordering).
    if capture_pipe_path is not None:
        capture_yaml = f"""  capture:
    type: RawFile
    channels: 2
    filename: "{capture_pipe_path}"
    format: {capture_format}"""
    else:
        capture_yaml = f"""  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}"""
    # The resampler block appears ONLY when requested (the lean lane), so every
    # existing ALSA-capture caller is byte-identical (no resampler key).
    resampler_line = (
        file_capture_resampler_yaml(resampler_type, resampler_profile)
        if resampler_type is not None
        else ""
    )
    yaml = f"""---
# Auto-generated JTS DSP config{header_id}.
# Source: jasper.sound.camilla_yaml.emit_sound_config
# DO NOT HAND-EDIT — update http://jts.local/correction/ or
# http://jts.local/sound/ instead.
#
# Structure mirrors deploy/camilladsp/outputd-cutover.yml.
# Room-correction PEQs, when present, run before sound-curve /
# preference-EQ filters. The `master_gain` mixer remains identity so
# the Ducker contract holds.
# output_trim_db={trim_db:.3f}

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: {rate_adjust_literal}{resampler_line}
{capture_yaml}
{playback_yaml}

filters:
{filter_yaml}

mixers:
  master_gain:
    channels: {{ in: 2, out: 2 }}
    mapping:
      - dest: 0
        sources: [{{ channel: 0, gain: 0, inverted: false }}]
      - dest: 1
        sources: [{{ channel: 1, gain: 0, inverted: false }}]

pipeline:
{pipeline_yaml}
"""

    # Weave the bonded-member channel-split (channel_select mixer + sub
    # crossover) BEFORE the out_path write so the written file is the woven
    # config. Passthrough (stereo) / None leaves `yaml` byte-for-byte.
    if channel_split is not None:
        yaml = weave_channel_split(yaml, channel_split)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        right_note = (
            f" room_peqs_right={len(room_peqs_right)}"
            if room_peqs_right is not None
            else ""
        )
        logger.info(
            "wrote sound config: %s (room_peqs=%d%s sound_filters=%d output_trim=%.3f)",
            out_path,
            len(room_peqs or []),
            right_note,
            len(sound_filters),
            trim_db,
        )
    return yaml


def emit_flat_outputd_cutover_config(*, out_path: str | Path | None = None) -> str:
    """Emit the flat outputd startup graph through the production generator.

    Fresh plain-flat installs boot through this graph. Keeping it on the same
    emitter as ordinary sound configs means the active DAC profile's latency
    floor reaches first boot without adding a second Camilla/outputd path.
    """

    return emit_sound_config(SoundProfile(enabled=False), out_path=out_path)


# The ring flat startup graph — the ``shm_ring`` sibling of
# ``outputd-cutover.yml``. A ring-armed box's statefile seeding
# (``jasper.active_speaker.runtime_contract.safe_graph_for_current_topology``)
# must re-seed a RING config on a camilla restart/deploy, not revert to
# ``outputd-cutover.yml`` (loopback/direct) — that revert is audit finding 5's
# "built-in revert" (a hand-placed ring config that dies on any camilla restart).
# This emitter is the product path that produces the ring flat config so seeding
# has a legal ring graph to select. Named alongside the loopback flat config; the
# statefile seeder picks between them by the persisted coupling.
RING_FLAT_CONFIG_NAME = "outputd-cutover-ring.yml"


def emit_flat_ring_config(*, out_path: str | Path | None = None) -> str:
    """Emit the flat outputd startup graph coupled to the SHM rings (shm_ring).

    Identical to :func:`emit_flat_outputd_cutover_config` except the CamillaDSP
    capture device is ``jts_ring_capture`` (Ring A) and the playback device is
    ``jts_ring_playback`` (Ring B), both S16_LE — the end-to-end ring topology the
    ``shm_ring`` coupling arms. The ring graph uses the hardware-validated
    low-latency geometry (chunk 128 / target 128 / queue 1 / rate_adjust off).
    This is the config the statefile seeder re-seeds on a ring-armed box so a
    deploy / camilla restart keeps the rings instead of reverting to loopback.
    """

    from jasper.fanin_coupling import (
        RING_CAPTURE_DEVICE,
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
        RING_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT,
    )

    return emit_sound_config(
        SoundProfile(enabled=False),
        capture_device=RING_CAPTURE_DEVICE,
        capture_format=RING_WIRE_FORMAT,
        playback_device=RING_PLAYBACK_DEVICE,
        playback_format=RING_WIRE_FORMAT,
        chunksize=RING_CAMILLA_CHUNKSIZE,
        target_level=RING_CAMILLA_TARGET_LEVEL,
        queuelimit=RING_CAMILLA_QUEUELIMIT,
        enable_rate_adjust=RING_CAMILLA_ENABLE_RATE_ADJUST,
        out_path=out_path,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    # Sound configs (including the bonded-leader pipe config grouping_leader.yml)
    # are read off-disk by the non-root jasper-control /state leader-pipe health
    # check (active_leader_pipe_path scans the active config for the snapserver
    # pipe sink). Keep them group-readable (0640, group jasper via the setgid
    # configs dir) or that check goes blind under the WS1 non-root drop and the
    # leader falsely reports the bond "degraded — stream is silent" while audio
    # flows. Mirrors jasper.active_speaker.camilla_yaml._atomic_write_text (the
    # active-speaker emitter already widens for the same non-root reason); this
    # is the sibling writer that was missed. The canonical atomic_write_text also
    # replaces the hand-rolled tempfile+rename (no wider-permission window).
    atomic_write_text(path, text, mode=0o640)


def sound_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / SOUND_CONFIG_NAME


def sound_audition_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / SOUND_AUDITION_CONFIG_NAME


def is_base_config(path: str | Path | None) -> bool:
    return Path(path) == BASE_CONFIG_PATH if path else False


def is_jts_generated_config(
    path: str | Path | None,
    *,
    config_dir: str | Path,
) -> bool:
    if not path:
        return False
    cfg_path = Path(path)
    return cfg_path.parent == Path(config_dir) and bool(
        _JTS_GENERATED_RE.match(cfg_path.name)
    )


def extract_room_peqs_from_config_text(text: str) -> list[PeqFilter]:
    """Extract generated room-correction PEQs from a CamillaDSP YAML.

    We intentionally avoid a YAML runtime dependency here. The parser is
    scoped to historical correction configs and the deterministic shapes
    emitted by this module.

    SCOPE: extracts the SOLO/left chain only (``peq_*`` / ``room_peq_*``);
    right-channel leader-bake filters (``peq_r*`` / ``room_peq_r*``) are
    deliberately NOT matched — this extractor serves the solo re-emit
    path. The multi-room leader apply path must compose from STORED
    per-speaker profiles, never by re-extracting a woven config (see
    docs/HANDOFF-multiroom.md §2, Increment 5).
    """

    try:
        filters_text = text.split("\nfilters:\n", 1)[1].split("\nmixers:\n", 1)[0]
    except IndexError:
        return []

    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in filters_text.splitlines():
        match = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if match:
            if current_name is not None:
                blocks.append((current_name, "\n".join(current_lines)))
            current_name = match.group(1)
            current_lines = []
            continue
        if current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current_lines)))

    # No silent failure paths: if this config carries leader-bake
    # right-channel filters, extraction alone CANNOT reproduce it — a
    # re-emit from just this result would silently DROP the follower's
    # correction. Warn loudly; the leader apply path must compose from
    # stored profiles (HANDOFF-multiroom.md §2, Increment 5).
    if any(
        re.fullmatch(r"(?:room_)?peq_r\d+", name) for name, _ in blocks
    ):
        logger.warning(
            "event=sound.extract_room_peqs result=right_channel_ignored "
            "detail=leader-bake right-channel (*_r*) filters present and "
            "NOT extracted; re-emitting from this extraction alone would "
            "drop the follower's correction — compose from stored "
            "profiles (HANDOFF-multiroom.md §2, Increment 5)"
        )

    peqs: list[PeqFilter] = []
    for name, block in blocks:
        if not (re.fullmatch(r"peq_\d+", name) or re.fullmatch(r"room_peq_\d+", name)):
            continue
        if "type: Biquad" not in block or not re.search(
            r"^\s+type:\s+Peaking\s*$", block, re.M
        ):
            continue
        values: dict[str, float] = {}
        for key in ("freq", "q", "gain"):
            match = re.search(rf"^\s+{key}:\s+([-+]?\d+(?:\.\d+)?)\s*$", block, re.M)
            if not match:
                break
            values[key] = float(match.group(1))
        else:
            peqs.append(
                PeqFilter(freq=values["freq"], q=values["q"], gain=values["gain"])
            )
    return peqs


def extract_room_peqs_from_config(path: str | Path | None) -> list[PeqFilter]:
    if not path:
        return []
    cfg_path = Path(path)
    try:
        return extract_room_peqs_from_config_text(cfg_path.read_text())
    except FileNotFoundError:
        logger.info("active CamillaDSP config path not readable: %s", cfg_path)
    except OSError as e:
        logger.warning("could not inspect CamillaDSP config %s: %s", cfg_path, e)
    return []
