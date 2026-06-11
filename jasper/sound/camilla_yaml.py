"""Emit CamillaDSP configs for sound curves and preference EQ.

The generated config preserves the base JTS audio path and any existing
room-correction PEQs, then appends preference filters. That ordering is
intentional: room correction fixes the room; preference EQ shapes what
the listener likes after that correction.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from jasper.multiroom.channel_split import ChannelSplit, weave_channel_split
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    DEFAULT_VOLUME_LIMIT_DB,
    PeqFilter,
    ensure_volume_limit_db,
)
from jasper.camilla_emit import (
    emit_gain_filter,
    emit_master_gain_pipeline,
    emit_peaking_biquad,
    fmt,
)

from .profile import (
    FilterSpec,
    GAINLESS_BIQUAD_TYPES,
    SoundProfile,
    build_sound_filters,
)

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = Path("/etc/camilladsp/outputd-cutover.yml")
SOUND_CONFIG_NAME = "sound_current.yml"
SOUND_AUDITION_CONFIG_NAME = "sound_audition.yml"
_JTS_GENERATED_RE = re.compile(
    r"^(?:correction_[A-Za-z0-9]+_\d+|sound_current|sound_audition)\.yml$"
)


def _emit_filter_spec(spec: FilterSpec) -> list[str]:
    # Sound-specific: maps a FilterSpec (shelf / gainless / peaking) to a
    # Biquad. Leaf `fmt`/Peaking emission is shared (jasper.camilla_emit);
    # this shelf/gainless dispatch is sound's own assembly concern.
    lines = [
        f"  {spec.name}:",
        "    type: Biquad",
        "    parameters:",
        f"      type: {spec.biquad_type}",
        f"      freq: {fmt(spec.freq)}",
    ]
    if spec.biquad_type in {"Lowshelf", "Highshelf"}:
        lines.append(f"      slope: {fmt(spec.slope or 6.0)}")
        lines.append(f"      gain: {fmt(spec.gain)}")
    elif spec.biquad_type in GAINLESS_BIQUAD_TYPES:
        # Highpass/Lowpass/Notch shape the response without a gain term.
        lines.append(f"      q: {fmt(spec.q or 1.0)}")
    else:
        lines.append(f"      q: {fmt(spec.q or 1.0)}")
        lines.append(f"      gain: {fmt(spec.gain)}")
    return lines


def _emit_filter_definitions(
    profile: SoundProfile,
    room_peqs: Iterable[PeqFilter],
    *,
    room_peqs_right: Iterable[PeqFilter] | None = None,
    output_trim_db: float = 0.0,
) -> tuple[str, list[str], list[str] | None, float]:
    """Returns ``(filters_yaml, chain_names, chain_names_right, trim_db)``.

    ``chain_names_right`` is ``None`` when ``room_peqs_right`` is ``None``
    (solo — channel 1 duplicates channel 0, byte-identical to before this
    axis existed). When given, only the ROOM-correction segment differs
    per channel (``room_peq_r*`` — the per-seat part); the preference
    filters (taste, shared household EQ) and the optional preamp are the
    SAME named filters referenced by both chains — defined once.
    """
    lines: list[str] = []
    room_names: list[str] = []
    room_names_right: list[str] | None = None

    lines.extend(emit_gain_filter("flat", 0.0))

    room_list = list(room_peqs)
    for i, peq in enumerate(room_list, start=1):
        name = f"room_peq_{i}"
        lines.extend(emit_peaking_biquad(name, freq=peq.freq, q=peq.q, gain=peq.gain))
        room_names.append(name)

    if room_peqs_right is not None:
        room_names_right = []
        for i, peq in enumerate(room_peqs_right, start=1):
            name = f"room_peq_r{i}"
            lines.extend(
                emit_peaking_biquad(name, freq=peq.freq, q=peq.q, gain=peq.gain)
            )
            room_names_right.append(name)

    # Preference boosts apply at unity: a +N dB band raises only that band
    # and leaves the rest of the spectrum untouched, like a consumer EQ. The
    # one optional global attenuation is the caller-supplied output trim
    # (manual headroom and/or loudness matching, both opt-in, both default 0).
    # With trim 0 there is no preamp at all -- boosts boost. The master
    # volume_limit ceiling stays the hard clip guard regardless. The trim
    # only applies when the profile has filters; a flat profile can't clip
    # from EQ, so it plays at unity even if a headroom trim is configured.
    sound_filters = build_sound_filters(profile)
    trim_db = max(0.0, float(output_trim_db)) if sound_filters else 0.0
    tail_names: list[str] = []
    if trim_db > 0.0:
        lines.extend(emit_gain_filter("sound_preamp", -trim_db))
        tail_names.append("sound_preamp")
    for spec in sound_filters:
        lines.extend(_emit_filter_spec(spec))
        tail_names.append(spec.name)
    tail_names.append("flat")

    chain_names = room_names + tail_names
    chain_names_right = (
        None if room_names_right is None else room_names_right + tail_names
    )
    return "\n".join(lines), chain_names, chain_names_right, round(trim_db, 3)


def emit_sound_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    room_peqs_right: list[PeqFilter] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    playback_device: str = DEFAULT_PLAYBACK_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
    output_trim_db: float = 0.0,
    enable_rate_adjust: bool = True,
    channel_split: ChannelSplit | None = None,
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

    ``channel_split`` (a :class:`jasper.multiroom.channel_split.ChannelSplit`)
    is woven in for a bonded member that plays a single channel — the
    ``channel_select`` mixer + (for a sub) the crossover. ``None`` or a
    passthrough (``stereo``) split leaves the config untouched, so a solo
    speaker is byte-for-byte unchanged."""

    # Loud-output safety: refuse to emit a config whose master fader
    # could boost above full scale. Mirrors the active_speaker emitter.
    volume_limit_db = ensure_volume_limit_db(volume_limit_db)
    filter_yaml, chain_names, chain_names_right, trim_db = _emit_filter_definitions(
        profile,
        room_peqs or [],
        room_peqs_right=room_peqs_right,
        output_trim_db=output_trim_db,
    )
    # Structure is the shared primitive; this module owns only which
    # names go in each chain (room L/R segments + the shared tail).
    pipeline_yaml = emit_master_gain_pipeline(chain_names, chain_names_right)
    # inv-5: an active bond member runs rate_adjust off (snapclient is the sole
    # rate-tracker); default True keeps the solo path unchanged.
    rate_adjust_literal = "true" if enable_rate_adjust else "false"
    header_id = f" (id={profile_id})" if profile_id else ""
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
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: {rate_adjust_literal}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: 2
    device: "{playback_device}"
    format: {playback_format}

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
            len(build_sound_filters(profile)),
            trim_db,
        )
    return yaml


def _atomic_write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        tmp_name = f.name
    os.replace(tmp_name, path)


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


def validate_camilla_config(path: str | Path) -> bool:
    """Compatibility wrapper for older callers.

    New apply paths use :mod:`jasper.dsp_apply` directly so they can
    distinguish invalid configs from validator runner failures.
    """

    from jasper.dsp_apply import validate_camilla_config as _validate

    return _validate(path).ok_to_apply


def extract_room_peqs_from_config_text(text: str) -> list[PeqFilter]:
    """Extract generated room-correction PEQs from a CamillaDSP YAML.

    We intentionally avoid a YAML runtime dependency here. The parser is
    scoped to the deterministic config shapes emitted by
    jasper.correction.camilla_yaml and this module.

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
            "extract_room_peqs: leader-bake right-channel (*_r*) filters "
            "present in config text and NOT extracted — re-emitting from "
            "this extraction alone would drop the follower's correction; "
            "compose from stored profiles instead "
            "(HANDOFF-multiroom.md §2, Increment 5)"
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
