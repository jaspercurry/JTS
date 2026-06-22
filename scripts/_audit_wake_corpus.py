# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Audit the browser-recorded wake-word gold corpus.

Reads the recorder layout produced by `jasper-wake-corpus-web`:

    enrollment_positives/
      aec_on_nomusic/*.wav
      aec_off_nomusic/*.wav
      ...
      metadata/enroll_<member>_<session>.json

The audit is deliberately hardware-free and laptop-side. Run it after
rsyncing `/var/lib/jasper/enrollment_positives/` from the Pi to catch
missing raw0 legs, stale pre-raw0 sessions, silent WAVs, bad formats,
and uneven condition/distance coverage before the corpus drives Phase
0a/0c decisions.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import wave
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper import wake_legs
from jasper.aec_sweep import AEC3_SWEEP_VARIANTS


SAMPLE_RATE_HZ = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
CONDITIONS = ("quiet", "ambient", "music")
DISTANCES = ("near", "mid", "far")
BASE_LEGS = ("on", "off", "dtln")
RAW0_LEG = "raw0"
USB_CORPUS_LEGS = ("ref", "usb_raw", "usb_webrtc")
USB_DTLN_LEG = "usb_dtln"
AEC3_SWEEP_LEGS = tuple(variant.leg for variant in AEC3_SWEEP_VARIANTS)
LEGACY_AEC3_SWEEP_LEGS = (
    "aec3_ns_off",
    "aec3_default_gain_08",
    "aec3_hf_relaxed",
    "aec3_hf_mask_upstream",
    "aec3_hf_wide_open",
    "aec3_nearend_fast",
    "aec3_slow_attack",
)
KNOWN_LEGS = (
    tuple(leg.token for leg in wake_legs.REGISTRY)
    + AEC3_SWEEP_LEGS
    + LEGACY_AEC3_SWEEP_LEGS
)
CHIP_AEC_PROFILE_BASE_LEGS = (
    "chip_aec_150",
    "chip_aec_210",
    "raw0",
    "xvf_raw0_webrtc_aec3",
    "ref",
)
SILENCE_RMS = 30.0
MAX_EXPECTED_DURATION_SEC = 30.5


@dataclass(frozen=True)
class WavStats:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    rms: float
    peak: int

    @property
    def duration_sec(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def rms_dbfs(self) -> float:
        if self.rms <= 0.0:
            return -100.0
        return 20.0 * math.log10(self.rms / 32768.0)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: failed to read JSON: {e}") from e


def _load_wav(path: Path) -> WavStats:
    with wave.open(str(path)) as w:
        channels = w.getnchannels()
        sample_width = w.getsampwidth()
        sample_rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)
    if sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16)
        rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2))) if data.size else 0.0
        peak = int(np.max(np.abs(data.astype(np.int32)))) if data.size else 0
    else:
        rms = 0.0
        peak = 0
    return WavStats(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
        rms=rms,
        peak=peak,
    )


def _resolve_wav_path(corpus_dir: Path, path_str: str) -> Path:
    """Resolve recorder metadata paths against a local corpus copy.

    Production metadata contains absolute Pi paths such as
    `/var/lib/jasper/enrollment_positives/aec_on_nomusic/foo.wav`.
    After rsync, those same WAVs live under the caller's local
    `corpus_dir`, so map any path below `enrollment_positives/` back
    onto the local root before falling back to the literal path.
    """
    raw = Path(path_str)
    marker = "enrollment_positives"
    if marker in raw.parts:
        idx = raw.parts.index(marker)
        rel_parts = raw.parts[idx + 1:]
        if rel_parts:
            return corpus_dir.joinpath(*rel_parts)
    if raw.is_absolute():
        return raw
    return corpus_dir / raw


def _expected_legs(session: dict[str, Any]) -> tuple[str, ...]:
    enabled = session.get("enabled_legs")
    if isinstance(enabled, list):
        legs = tuple(str(leg) for leg in enabled if str(leg) in KNOWN_LEGS)
        if legs:
            return legs

    ports = session.get("ports") or {}
    if session.get("corpus_profile") == "chip_aec_comparison_v1":
        legs = [
            leg for leg in CHIP_AEC_PROFILE_BASE_LEGS
            if not ports or leg in ports
        ]
        if session.get("include_usb_mic"):
            legs.extend(
                leg for leg in ("usb_raw", "usb_webrtc")
                if not ports or leg in ports
            )
        if session.get("include_usb_dtln"):
            legs.extend(
                leg for leg in ("usb_raw", USB_DTLN_LEG)
                if not ports or leg in ports
            )
        return tuple(dict.fromkeys(legs))

    legs = ["on", "off"]
    # Older metadata may not have a useful ports map. Treat that as the
    # normal 3-leg base recorder shape.
    if session.get("include_dtln", True) and (not ports or "dtln" in ports):
        legs.append("dtln")
    if session.get("include_raw_mic_0"):
        legs.append(RAW0_LEG)
    if session.get("include_usb_mic"):
        legs.extend(leg for leg in USB_CORPUS_LEGS if not ports or leg in ports)
    if session.get("include_usb_dtln"):
        legs.extend(
            leg for leg in ("ref", "usb_raw", USB_DTLN_LEG)
            if not ports or leg in ports
        )
    if session.get("include_aec3_sweep"):
        legs.extend(leg for leg in AEC3_SWEEP_LEGS if not ports or leg in ports)
    # Preserve recorder order while de-duping shared ref/usb_raw
    # companion legs.
    legs = list(dict.fromkeys(legs))
    return tuple(legs)


def _matrix_counts(clips: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for clip in clips:
        if clip.get("deleted"):
            continue
        counts[(str(clip.get("distance")), str(clip.get("condition")))] += 1
    return counts


def _print_matrix(counts: dict[tuple[str, str], int], *, indent: str = "  ") -> None:
    header = "distance  " + "  ".join(f"{c:>7}" for c in CONDITIONS)
    print(f"{indent}{header}")
    print(f"{indent}{'-' * len(header)}")
    for distance in DISTANCES:
        vals = "  ".join(f"{counts.get((distance, c), 0):>7}" for c in CONDITIONS)
        print(f"{indent}{distance:<8}  {vals}")


def audit(
    corpus_dir: Path,
    *,
    expect_raw0: bool = False,
    expect_legs: tuple[str, ...] = (),
    min_per_cell: int | None = None,
) -> int:
    issues: list[str] = []
    warnings: list[str] = []

    metadata_dir = corpus_dir / "metadata"
    if not corpus_dir.is_dir():
        print(f"ERROR: {corpus_dir} is not a directory", file=sys.stderr)
        return 1
    if not metadata_dir.is_dir():
        print(f"ERROR: {metadata_dir} is not a directory", file=sys.stderr)
        return 1

    session_paths = sorted(metadata_dir.glob("enroll_*.json"))
    print("=" * 72)
    print(f"Wake-corpus audit: {corpus_dir}")
    print("=" * 72)
    print(f"\n[1] Sessions ({len(session_paths)} metadata file(s))")

    sessions: list[dict[str, Any]] = []
    for path in session_paths:
        try:
            data = _read_json(path)
        except ValueError as e:
            issues.append(str(e))
            continue
        data["_metadata_path"] = path
        sessions.append(data)

    if not sessions:
        issues.append("no readable session metadata files found")

    all_alive_clips: list[dict[str, Any]] = []
    all_wav_stats: list[WavStats] = []
    leg_counts: Counter[str] = Counter()
    health_counts: Counter[str] = Counter()
    session_raw0_count = 0
    session_audio_context_count = 0

    for data in sessions:
        session_id = str(data.get("session_id", "?"))
        member = str(data.get("member", "?"))
        include_raw0 = bool(data.get("include_raw_mic_0", False))
        if include_raw0:
            session_raw0_count += 1
        clips = list(data.get("clips") or [])
        alive = [c for c in clips if not c.get("deleted")]
        all_alive_clips.extend(alive)
        expected_legs = _expected_legs(data)
        ports = data.get("ports") or {}
        audio_context = data.get("audio_context")
        if isinstance(audio_context, dict):
            session_audio_context_count += 1
        else:
            audio_context = {}

        if include_raw0 and RAW0_LEG not in ports:
            issues.append(
                f"session {session_id}: include_raw_mic_0=true but "
                "metadata ports omit raw0; production path may not have "
                "subscribed to :9879"
            )
        if expect_raw0 and not include_raw0:
            issues.append(f"session {session_id}: raw0 expected but session flag is false")
        for leg in expect_legs:
            if leg not in expected_legs:
                issues.append(
                    f"session {session_id}: expected leg {leg!r} not enabled "
                    f"(enabled={', '.join(expected_legs) or 'none'})"
                )

        conds = Counter(str(c.get("condition", "?")) for c in alive)
        print(
            f"  {session_id} member={member} clips={len(alive)} "
            f"deleted={len(clips) - len(alive)} raw0={include_raw0} "
            f"legs={list(expected_legs)} "
            f"conditions={dict(sorted(conds.items()))}"
        )
        profile = audio_context.get("production_audio_profile")
        if not isinstance(profile, dict):
            profile = {}
        microphone = audio_context.get("microphone")
        if not isinstance(microphone, dict):
            microphone = {}
        dac_reference = audio_context.get("dac_reference")
        if not isinstance(dac_reference, dict):
            dac_reference = {}
        validation = dac_reference.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        if audio_context:
            firmware = microphone.get("firmware")
            if not isinstance(firmware, dict):
                firmware = {}
            print(
                "    "
                f"profile={profile.get('active') or profile.get('requested') or 'unknown'} "
                f"state={profile.get('state') or 'unknown'} "
                f"mic={microphone.get('name') or 'unknown'} "
                f"firmware={firmware.get('label') or 'unknown'} "
                f"validation={validation.get('status') or 'unknown'}"
            )

        seen_seq: set[int] = set()
        for clip in alive:
            clip_id = str(clip.get("clip_id", "?"))
            seq = clip.get("seq")
            if isinstance(seq, int):
                if seq in seen_seq:
                    issues.append(f"session {session_id}: duplicate seq {seq}")
                seen_seq.add(seq)
            condition = str(clip.get("condition"))
            distance = str(clip.get("distance"))
            if condition not in CONDITIONS:
                issues.append(f"{clip_id}: unknown condition {condition!r}")
            if distance not in DISTANCES:
                issues.append(f"{clip_id}: unknown distance {distance!r}")
            capture_health = clip.get("capture_health")
            if isinstance(capture_health, dict):
                health_status = str(capture_health.get("status", "unknown"))
                health_counts[health_status] += 1
                if health_status == "compromised":
                    issues.append(f"{clip_id}: capture health compromised")
                elif health_status in ("warning", "unknown"):
                    warnings.append(f"{clip_id}: capture health {health_status}")
            clip_selected_legs = clip.get("selected_legs")
            if isinstance(clip_selected_legs, list):
                clip_legs = tuple(
                    str(leg)
                    for leg in clip_selected_legs
                    if str(leg) in KNOWN_LEGS
                )
                if clip_legs and clip_legs != expected_legs:
                    warnings.append(
                        f"{clip_id}: selected_legs differ from session "
                        "enabled_legs"
                    )

            files = clip.get("files") or {}
            missing = [leg for leg in expected_legs if leg not in files]
            extra = [leg for leg in files if leg not in expected_legs]
            if missing:
                issues.append(f"{clip_id}: missing expected leg(s): {', '.join(missing)}")
            if extra:
                warnings.append(f"{clip_id}: unexpected leg(s): {', '.join(extra)}")

            for leg, path_str in files.items():
                leg_counts[str(leg)] += 1
                wav_path = _resolve_wav_path(corpus_dir, str(path_str))
                if not wav_path.exists():
                    issues.append(f"{clip_id}: missing WAV for {leg}: {wav_path}")
                    continue
                try:
                    stats = _load_wav(wav_path)
                except (OSError, wave.Error, ValueError) as e:
                    issues.append(f"{clip_id}: failed to read {wav_path}: {e}")
                    continue
                all_wav_stats.append(stats)
                if stats.sample_rate != SAMPLE_RATE_HZ:
                    issues.append(
                        f"{wav_path.name}: sample rate {stats.sample_rate}, "
                        f"expected {SAMPLE_RATE_HZ}"
                    )
                if stats.channels != CHANNELS:
                    issues.append(f"{wav_path.name}: channels {stats.channels}, expected {CHANNELS}")
                if stats.sample_width != SAMPLE_WIDTH_BYTES:
                    issues.append(
                        f"{wav_path.name}: sample width {stats.sample_width}, "
                        f"expected {SAMPLE_WIDTH_BYTES}"
                    )
                if stats.frames <= 0:
                    issues.append(f"{wav_path.name}: empty WAV")
                elif stats.duration_sec > MAX_EXPECTED_DURATION_SEC:
                    warnings.append(
                        f"{wav_path.name}: duration {stats.duration_sec:.1f}s "
                        "exceeds recorder cap"
                    )
                if stats.rms < SILENCE_RMS:
                    warnings.append(
                        f"{wav_path.name}: near-silent rms={stats.rms:.1f} "
                        f"({stats.rms_dbfs:.1f} dBFS)"
                    )

    print(f"  raw0-enabled sessions: {session_raw0_count}/{len(sessions)}")
    print(
        "  audio-context sessions: "
        f"{session_audio_context_count}/{len(sessions)}"
    )
    print(f"  leg WAV counts: {dict(sorted(leg_counts.items()))}")
    if health_counts:
        print(f"  capture health: {dict(sorted(health_counts.items()))}")

    print("\n[2] Condition x distance coverage")
    counts = _matrix_counts(all_alive_clips)
    _print_matrix(counts)
    if min_per_cell is not None:
        for distance in DISTANCES:
            for condition in CONDITIONS:
                n = counts.get((distance, condition), 0)
                if n < min_per_cell:
                    issues.append(
                        f"coverage {distance}/{condition}: {n} clip(s), "
                        f"expected at least {min_per_cell}"
                    )

    print("\n[3] WAV integrity")
    print(f"  referenced WAVs read: {len(all_wav_stats)}")
    if all_wav_stats:
        durations = [s.duration_sec for s in all_wav_stats]
        rms_dbfs = [s.rms_dbfs for s in all_wav_stats]
        peaks = [s.peak for s in all_wav_stats]
        print(
            "  duration sec: "
            f"min={min(durations):.2f} median={statistics.median(durations):.2f} "
            f"max={max(durations):.2f}"
        )
        print(
            "  rms dBFS:     "
            f"min={min(rms_dbfs):.1f} median={statistics.median(rms_dbfs):.1f} "
            f"max={max(rms_dbfs):.1f}"
        )
        print(
            "  peak int16:   "
            f"min={min(peaks)} median={statistics.median(peaks):.0f} "
            f"max={max(peaks)}"
        )

    print("\n[4] Findings")
    if issues:
        print("  Issues:")
        for item in issues:
            print(f"  - {item}")
    else:
        print("  Issues: none")
    if warnings:
        print("  Warnings:")
        for item in warnings:
            print(f"  - {item}")
    else:
        print("  Warnings: none")

    return 1 if issues else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        type=Path,
        default=Path("data/enrollment_positives"),
        help="Corpus root copied from /var/lib/jasper/enrollment_positives.",
    )
    parser.add_argument(
        "--expect-raw0",
        action="store_true",
        help="Fail if any session is not raw0-enabled.",
    )
    parser.add_argument(
        "--expect-leg",
        action="append",
        choices=KNOWN_LEGS,
        default=[],
        help="Fail if any session does not enable this leg. May be repeated.",
    )
    parser.add_argument(
        "--min-per-cell",
        type=int,
        default=None,
        help="Fail if any condition/distance cell has fewer clips.",
    )
    args = parser.parse_args(argv)
    return audit(
        args.corpus_dir,
        expect_raw0=args.expect_raw0,
        expect_legs=tuple(args.expect_leg),
        min_per_cell=args.min_per_cell,
    )


if __name__ == "__main__":
    raise SystemExit(main())
