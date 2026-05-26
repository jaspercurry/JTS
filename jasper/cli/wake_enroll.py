"""jasper-wake-enroll — Active wake-word sample collection for training.

Run on the Pi with a household member present. The CLI walks them
through N "Jarvis" recordings via terminal prompts, captures all
available AEC legs simultaneously (UDP `:9876` AEC ON + `:9877` AEC
OFF / raw chip-direct + optionally `:9878` DTLN-aec), and saves the
paired WAVs into `data/enrollment_positives/<quadrant>/`. The
quadrants are (AEC leg) × (music / no-music) — run this command twice
per household member (once per music condition) to populate every
quadrant the bridge is configured for.

DTLN leg note: as of PR #253 (2026-05-23), JTS supports a third UDP
stream from the AEC bridge running DTLN-aec in parallel. The CLI
captures it by default; pass `--no-dtln` on Pis that don't have
`JASPER_WAKE_LEG_DTLN=1` set, otherwise the third leg's WAVs will be
empty (the UDP port binds but no packets arrive).

Mechanics:
  1. Stops jasper-voice (frees the UDP receiver ports — the AEC bridge
     keeps sending, so the audio is live the entire time).
  2. Binds two `UdpMicCapture` instances, one per port.
  3. Loops `--count` times: brief countdown → 3 s recording → save +
     peak-score feedback → 2 s pause.
  4. Restarts jasper-voice (always, via try/finally) on exit.

The household has to set up the music condition themselves (play
something on Spotify / AirPlay / Bluetooth before invoking
`--condition music`). The CLI doesn't drive the music source — it just
records what comes in the mic chain.

Filenames embed a UTC session-id + sequence so re-running the CLI
never collides with prior recordings:
  data/enrollment_positives/aec_on_nomusic/enroll_jasper_20260523T142000Z_01.aec-on.wav
  data/enrollment_positives/aec_off_nomusic/enroll_jasper_20260523T142000Z_01.aec-off.wav

The accompanying AEC OFF clip always shares the same session-id +
sequence as its paired AEC ON clip — downstream training treats them
as one event split across two legs (same convention as the
`/var/lib/jasper/wake-events/` corpus).

Usage:
  sudo /opt/jasper/.venv/bin/jasper-wake-enroll \\
      --member jasper --condition quiet --count 30

  sudo /opt/jasper/.venv/bin/jasper-wake-enroll \\
      --member jasper --condition music --count 30
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from jasper.wake_ports import (
    DEFAULT_AEC_DTLN_PORT,
    DEFAULT_AEC_OFF_PORT,
    DEFAULT_AEC_ON_PORT,
    DEFAULT_AEC_RAW0_PORT as DEFAULT_AEC_RAW0_PORT,
)

logger = logging.getLogger("jasper-wake-enroll")


# Same shape the wake loop + wake_events corpus use. Anything else
# would mean the enrollment clips don't load in the same training
# pipeline that consumes the production captures.
SAMPLE_RATE_HZ = 16000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz (matches MicCapture / UdpMicCapture)

# Default per-utterance recording window. 3 s is comfortable for a
# 1-2 s "Jarvis" utterance with margin for prompt-to-speech delay
# at either end.
DEFAULT_DURATION_SEC = 3.0

# Pause between recordings — long enough for the household member to
# reset and breathe, short enough that 30 utterances doesn't take
# forever. Skippable with ENTER (not yet implemented; just sleep).
INTER_CLIP_PAUSE_SEC = 2.0

# What we tell systemd to stop / start. The voice daemon owns both
# UDP receiver sockets in production, so it must be down for the
# enrollment CLI to bind.
VOICE_UNIT = "jasper-voice"


# Conditions the operator picks per session. Maps to the music_active
# axis of the quadrant matrix.
CONDITIONS = ("quiet", "music")


# ---------------------------------------------------------------------------
# Pure helpers — fully testable without audio hardware
# ---------------------------------------------------------------------------


def quadrant_dirs(condition: str) -> tuple[str, str]:
    """Return (aec_on_dir, aec_off_dir) for the given condition.

    Two-leg variant kept for backward compat — most callers prefer
    `all_quadrant_dirs()` which also returns the DTLN dir.
    """
    if condition not in CONDITIONS:
        raise ValueError(
            f"unknown condition {condition!r}; expected one of {CONDITIONS}"
        )
    state = "music" if condition == "music" else "nomusic"
    return (f"aec_on_{state}", f"aec_off_{state}")


def all_quadrant_dirs(condition: str) -> dict[str, str]:
    """Return `{leg_name: quadrant_dir}` for the given condition.

    Mirrors `_extract_wake_corpus.quadrant_for()`. Output directory
    naming stays consistent across the extraction + enrollment paths
    so downstream training can union the trees.
    """
    if condition not in CONDITIONS:
        raise ValueError(
            f"unknown condition {condition!r}; expected one of {CONDITIONS}"
        )
    state = "music" if condition == "music" else "nomusic"
    return {
        "on":   f"aec_on_{state}",
        "off":  f"aec_off_{state}",
        "dtln": f"aec_dtln_{state}",
    }


def make_session_id(member: str, now: datetime | None = None) -> str:
    """Generate a unique enrollment session ID.

    Format: `enroll_<member>_<UTC timestamp>`. The timestamp is per-run
    not per-clip, so a given session's clips all share a prefix —
    easier to delete or audit a single session post-hoc.
    """
    now = now or datetime.now(timezone.utc)
    safe_member = "".join(c for c in member.lower() if c.isalnum() or c == "_")
    if not safe_member:
        raise ValueError(f"member name has no usable chars: {member!r}")
    return f"enroll_{safe_member}_{now.strftime('%Y%m%dT%H%M%SZ')}"


def clip_basename(session_id: str, seq: int, leg: str) -> str:
    """Return `<session_id>_<seq>.aec-<leg>.wav`."""
    if leg not in ("on", "off", "dtln"):
        raise ValueError(f"leg must be 'on', 'off', or 'dtln'; got {leg!r}")
    return f"{session_id}_{seq:02d}.aec-{leg}.wav"


def write_wav(path: Path, pcm: bytes) -> None:
    """Write a 16 kHz mono int16 WAV. Atomic via tempfile + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE_HZ)
        w.writeframes(pcm)
    os.replace(tmp, path)


def compute_peak_score(detector, pcm_bytes: bytes) -> float:
    """Run `detector.score_frame` over the PCM and return the peak score.

    Iterates 80 ms frames. Bytes that don't divide cleanly into
    `FRAME_SAMPLES * 2` are truncated — the tail is shorter than the
    model's input window and would distort the score.
    """
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    n_frames = len(samples) // FRAME_SAMPLES
    if n_frames == 0:
        return 0.0
    peak = 0.0
    for i in range(n_frames):
        frame = samples[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        s = detector.score_frame(frame)
        if s > peak:
            peak = s
    return peak


# ---------------------------------------------------------------------------
# systemctl + sudo guards
# ---------------------------------------------------------------------------


def require_root() -> None:
    """Bail early if not running as root. Cleaner than letting
    systemctl fail mid-flight with a less-helpful permission error."""
    if os.geteuid() != 0:
        raise SystemExit(
            "ERROR: jasper-wake-enroll must run as root (stops/starts "
            "jasper-voice via systemctl + binds UDP receiver ports).\n"
            "       Re-run with: sudo /opt/jasper/.venv/bin/jasper-wake-enroll ..."
        )


def systemctl(action: str, unit: str = VOICE_UNIT) -> None:
    """Run `systemctl <action> <unit>`. Raises on non-zero exit."""
    cmd = ["systemctl", action, unit]
    logger.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Async capture — both legs in parallel from the AEC bridge UDP feeds
# ---------------------------------------------------------------------------


async def _collect_for(udp_capture, duration_sec: float) -> bytes:
    """Drain `udp_capture.frames()` for `duration_sec`. Returns the
    concatenated PCM bytes."""
    frames: list[np.ndarray] = []
    try:
        async with asyncio.timeout(duration_sec):
            async for frame in udp_capture.frames():
                frames.append(frame)
    except asyncio.TimeoutError:
        pass
    if not frames:
        return b""
    return np.concatenate(frames).astype(np.int16).tobytes()


async def record_legs(
    captures: dict[str, "UdpMicCapture"],  # noqa: F821 — forward type
    duration_sec: float,
) -> dict[str, bytes]:
    """Record arbitrarily many legs concurrently for `duration_sec`.

    Returns `{leg_name: pcm_bytes}` for each provided capture. Stops
    every leg at the same `asyncio.timeout`, so per-leg lengths match
    within one packet (~80 ms).

    This is the general API; `record_window` below is a 2-leg wrapper
    kept for backward compat with the original tests + call sites.
    """
    leg_names = list(captures.keys())

    async def _one(name: str) -> tuple[str, bytes]:
        return name, await _collect_for(captures[name], duration_sec)

    results = await asyncio.gather(*(_one(n) for n in leg_names))
    return dict(results)


async def record_window(
    udp_on, udp_off, duration_sec: float,
) -> tuple[bytes, bytes]:
    """Two-leg convenience wrapper around `record_legs`.

    Returns `(on_bytes, off_bytes)` for callers that pre-date the
    triple-leg refactor. New code should use `record_legs` directly.
    """
    res = await record_legs({"on": udp_on, "off": udp_off}, duration_sec)
    return res["on"], res["off"]


# ---------------------------------------------------------------------------
# Per-clip stats accumulator
# ---------------------------------------------------------------------------


@dataclass
class SessionStats:
    """Aggregate per-clip peak scores per leg.

    Generalized to any subset of legs so the same code handles 2-leg
    (on + off) and 3-leg (on + off + dtln) sessions. `peaks_on` /
    `peaks_off` accessors are kept as backward-compat shims for the
    original test fixtures.
    """

    peaks_per_leg: dict[str, list[float]] = field(default_factory=dict)

    # ----- backward-compat accessors used by the 2-leg test fixtures
    @property
    def peaks_on(self) -> list[float]:
        return self.peaks_per_leg.setdefault("on", [])

    @property
    def peaks_off(self) -> list[float]:
        return self.peaks_per_leg.setdefault("off", [])

    def record(self, peak_on: float, peak_off: float,
               peak_dtln: float | None = None) -> None:
        self.peaks_per_leg.setdefault("on", []).append(peak_on)
        self.peaks_per_leg.setdefault("off", []).append(peak_off)
        if peak_dtln is not None:
            self.peaks_per_leg.setdefault("dtln", []).append(peak_dtln)

    def weak_count(self, threshold: float = 0.10) -> int:
        """A clip is 'weak' if ALL captured legs have peaks below the
        threshold. With more legs, a clip is harder to call weak —
        which matches operator intuition (any one leg firing means the
        recording is usable for that leg's training)."""
        legs = list(self.peaks_per_leg.values())
        if not legs or not legs[0]:
            return 0
        n_clips = len(legs[0])
        return sum(
            1 for i in range(n_clips)
            if all(leg[i] < threshold for leg in legs if i < len(leg))
        )

    def summary(self) -> str:
        if not self.peaks_per_leg or not next(iter(self.peaks_per_leg.values()), []):
            return "  (no clips captured)"
        lines = []
        for leg in ("on", "off", "dtln"):
            peaks = self.peaks_per_leg.get(leg)
            if not peaks:
                continue
            ordered = sorted(peaks)
            mid = len(ordered) // 2
            label = {"on": "AEC ON  ", "off": "AEC OFF ", "dtln": "DTLN    "}[leg]
            lines.append(
                f"  {label} peak: median={ordered[mid]:.2f}  "
                f"min={ordered[0]:.2f}  max={ordered[-1]:.2f}"
            )
        n_total = len(self.peaks_per_leg.get("on", [])) or len(next(iter(self.peaks_per_leg.values())))
        lines.append(
            f"  weak clips (all captured legs <0.10): "
            f"{self.weak_count()}/{n_total}"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive session — the orchestration that touches systemd + UDP
# ---------------------------------------------------------------------------


def _countdown(seconds: int = 3) -> None:
    """Print 3, 2, 1, GO! at 1 Hz on stdout — single-line, no newline."""
    for n in range(seconds, 0, -1):
        sys.stdout.write(f"\r  Recording in {n}... ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r  Say JARVIS now!     \n")
    sys.stdout.flush()


async def run_session(args: argparse.Namespace) -> int:
    """The interactive recording loop. Returns process exit code."""
    # Lazy imports so the module is importable without the Pi-side deps
    # (openwakeword for the detector, UdpMicCapture for the audio I/O).
    from jasper.audio_io import UdpMicCapture
    from jasper.wake import WakeWordDetector

    leg_dirs = all_quadrant_dirs(args.condition)
    output_root: Path = args.output / "enrollment_positives"
    for d in leg_dirs.values():
        (output_root / d).mkdir(parents=True, exist_ok=True)

    # Build the legs map: always on + off; dtln is opt-out via --no-dtln.
    leg_ports: dict[str, int] = {
        "on": args.aec_on_port,
        "off": args.aec_off_port,
    }
    if not args.no_dtln:
        leg_ports["dtln"] = args.aec_dtln_port
    active_legs = list(leg_ports.keys())

    session_id = make_session_id(args.member)
    print("\nJTS Wake-Word Enrollment")
    print("=" * 40)
    print(f"  Member    : {args.member}")
    print(f"  Condition : {args.condition}")
    print(f"  Count     : {args.count} utterances of \"Jarvis\"")
    print(f"  Duration  : {args.duration:.1f} s per recording")
    print(f"  Output    : {output_root}/")
    print(f"  Session   : {session_id}")
    print(f"  Legs      : {', '.join(active_legs)} (ports: "
          f"{', '.join(str(p) for p in leg_ports.values())})")
    print(f"  Wake model: {args.wake_model} (peak-score feedback only)")
    print()
    if args.condition == "music":
        print(
            "  >>> Start music playing through the speaker BEFORE pressing\n"
            "      ENTER. Music can be any source (Spotify / AirPlay / BT).\n"
        )
    else:
        print(
            "  >>> Make sure the room is QUIET (no music, TV, dishwasher)\n"
            "      before pressing ENTER.\n"
        )
    if "dtln" in active_legs:
        print(
            "  Note: dtln leg is included. If your Pi doesn't have\n"
            "  JASPER_WAKE_LEG_DTLN=1, the bridge isn't sending on port\n"
            f"  {args.aec_dtln_port} — pass --no-dtln to suppress empty WAVs.\n"
        )
    input("Press ENTER to start, or Ctrl+C to quit.")

    detector = WakeWordDetector(args.wake_model, threshold=0.0)
    stats = SessionStats()

    # Build the captures dict. AsyncExitStack lets us open a dynamic
    # number of context managers without a chained `async with` for
    # the optional DTLN leg.
    from contextlib import AsyncExitStack
    async with AsyncExitStack() as stack:
        captures: dict[str, UdpMicCapture] = {}
        for leg, port in leg_ports.items():
            cap = await stack.enter_async_context(UdpMicCapture(port=port))
            captures[leg] = cap

        # Drain any backlog so the first capture window is clean.
        await asyncio.sleep(0.3)

        for seq in range(1, args.count + 1):
            print(f"\n[{seq:2d}/{args.count}]")
            _countdown(3)
            recordings = await record_legs(captures, args.duration)

            # Any leg with zero bytes is a soft failure (bridge issue
            # for that leg) — DTLN especially likely on a Pi without
            # JASPER_WAKE_LEG_DTLN=1. Log per-leg + skip only if
            # nothing came in at all.
            empty_legs = [leg for leg, b in recordings.items() if not b]
            if len(empty_legs) == len(active_legs):
                print(
                    f"  ✗ no audio captured on any leg "
                    f"({', '.join(f'{leg}={len(recordings[leg])}B' for leg in active_legs)}).\n"
                    f"    Is jasper-aec-bridge running? Skipping clip."
                )
                continue
            if empty_legs:
                print(
                    f"  ⚠ no audio on leg(s): {','.join(empty_legs)} — saving "
                    f"the legs that did capture."
                )

            # Compute peak scores per captured leg.
            peaks: dict[str, float] = {}
            for leg, pcm in recordings.items():
                if pcm:
                    peaks[leg] = compute_peak_score(detector, pcm)
                else:
                    peaks[leg] = 0.0
            # Backward-compat 2-arg record + optional 3rd
            stats.record(
                peaks.get("on", 0.0),
                peaks.get("off", 0.0),
                peaks.get("dtln") if "dtln" in active_legs else None,
            )

            # Write WAVs for each captured leg into its quadrant dir.
            written: list[Path] = []
            for leg, pcm in recordings.items():
                if not pcm:
                    continue
                quadrant_dir = leg_dirs[leg]
                path = output_root / quadrant_dir / clip_basename(session_id, seq, leg)
                write_wav(path, pcm)
                written.append(path)

            marker = "✓" if max(peaks.values()) >= 0.10 else "⚠"
            peak_str = "  ".join(
                f"{leg.upper()}={peaks[leg]:.2f}" for leg in active_legs if leg in peaks
            )
            print(f"  {marker} captured  {peak_str}")
            for p in written:
                print(f"    {p.name}")

            if seq < args.count:
                await asyncio.sleep(INTER_CLIP_PAUSE_SEC)

    print(f"\nSession complete. {len(stats.peaks_on)}/{args.count} clips saved.")
    print(stats.summary())
    if stats.weak_count() > 0:
        print(
            "\n  Note: weak clips have both peak scores below 0.10. They're\n"
            "  saved (the model couldn't fire on them, which is exactly\n"
            "  the case retraining is supposed to fix) — but if MOST of\n"
            "  your clips are weak, check mic placement / room noise."
        )
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-enroll",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--member", required=True,
        help='Household member name, e.g. "jasper" or "brittany". '
             'Embedded in filenames so per-member subsets are easy to '
             'filter post-hoc. Lowercased + alnum-stripped.',
    )
    parser.add_argument(
        "--condition", required=True, choices=CONDITIONS,
        help='"quiet" = no music/TV in room; "music" = music playing '
             'through the speaker (forces AEC actively cancelling). '
             'Run this CLI twice per member to cover both.',
    )
    parser.add_argument(
        "--count", type=int, default=30,
        help="Number of utterances per session (default 30).",
    )
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_SEC,
        help=f"Per-utterance recording window in seconds (default {DEFAULT_DURATION_SEC}).",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(os.environ.get("JASPER_WAKE_TRAIN_DATA", "data")),
        help="Output root. Clips land at "
             "<output>/enrollment_positives/<quadrant>/<session>_<seq>.aec-{on,off}.wav. "
             "Default: ./data/ (or $JASPER_WAKE_TRAIN_DATA).",
    )
    parser.add_argument(
        "--wake-model",
        default=os.environ.get(
            "JASPER_WAKE_MODEL",
            "/var/lib/jasper/wake/jarvis_v2.onnx",
        ),
        help="Wake model path/name for peak-score feedback only (does "
             "NOT gate saving — every recording is saved regardless of "
             "score, since the silent-miss case is exactly the data "
             "retraining needs).",
    )
    parser.add_argument(
        "--aec-on-port", type=int, default=DEFAULT_AEC_ON_PORT,
        help=f"UDP port for the AEC ON leg (default {DEFAULT_AEC_ON_PORT}).",
    )
    parser.add_argument(
        "--aec-off-port", type=int, default=DEFAULT_AEC_OFF_PORT,
        help=f"UDP port for the AEC OFF leg / raw chip-direct "
             f"(default {DEFAULT_AEC_OFF_PORT}).",
    )
    parser.add_argument(
        "--aec-dtln-port", type=int, default=DEFAULT_AEC_DTLN_PORT,
        help=f"UDP port for the DTLN-aec leg (default {DEFAULT_AEC_DTLN_PORT}). "
             "Only meaningful when the bridge has JASPER_WAKE_LEG_DTLN=1.",
    )
    parser.add_argument(
        "--no-dtln", action="store_true",
        help="Skip the DTLN leg entirely. Use on Pis with the 2-stream "
             "architecture (pre-PR-#253) or with JASPER_WAKE_LEG_DTLN=0.",
    )
    parser.add_argument(
        "--skip-service-toggle", action="store_true",
        help="Don't stop/start jasper-voice. Useful for testing on a Pi "
             "where the daemon is already down, or in dev environments "
             "where the audio comes from a different source.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose systemctl logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.skip_service_toggle:
        require_root()
        systemctl("stop")

    try:
        return asyncio.run(run_session(args))
    finally:
        if not args.skip_service_toggle:
            # Always try to restart, even on Ctrl+C / exception. If
            # we leave the speaker with jasper-voice down it stops
            # responding to wake until the operator notices.
            try:
                systemctl("start")
            except subprocess.CalledProcessError as e:
                # Don't mask the original error; log + continue. The
                # operator can `sudo systemctl start jasper-voice`
                # manually if this fails.
                print(
                    f"\nWARNING: failed to restart {VOICE_UNIT}: {e}\n"
                    f"         Run `sudo systemctl start {VOICE_UNIT}` manually.",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
