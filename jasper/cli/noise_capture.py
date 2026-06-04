"""jasper-noise-capture — passive noise corpus collection for training.

Captures long-form mic audio with NO human speech in the room, for use
as augmentation noise during wake-word model training. Two conditions
matter (`quiet` and `music`); each invocation captures all available
AEC legs simultaneously, producing 2-3 WAVs per session:

  data/noise/aec_on_quiet/noise_quiet_<UTC>_aec-on.wav
  data/noise/aec_off_quiet/noise_quiet_<UTC>_aec-off.wav
  data/noise/aec_dtln_quiet/noise_quiet_<UTC>_aec-dtln.wav   (when DTLN enabled)

Why two conditions and (up to) six output directories:
  - `aec_on_quiet`  — room ambient as it enters the wake detector with
                      WebRTC AEC3 passive. Augments quiet-condition
                      training positives.
  - `aec_off_quiet` — same condition through the chip-direct leg
                      (XVF3800 BF+NS+AGC+HPF only). Augments the same
                      positives as seen on the OFF leg.
  - `aec_on_music`  — **the AEC residual corpus.** Music plays from the
                      speaker, AEC3 actively cancels it, what leaks
                      through ends up in this file. The load-bearing
                      augmentation for the `aec_on_music` quadrant model.
  - `aec_off_music` — full music leakage on the chip-direct leg.
  - `aec_dtln_*` — DTLN-aec output (PR #253, opt-in via
                   JASPER_WAKE_LEG_DTLN=1). Captured by default;
                   suppress with `--no-dtln` on 2-stream Pis.

Mechanics: same as `jasper-wake-enroll` — stops jasper-voice for the
session (so we can bind the UDP receivers), records all available legs
to disk in parallel as the AEC bridge sends them, restarts
jasper-voice on exit. Writes are streamed directly into open WAV files
so the resident memory cost is bounded regardless of `--duration`.

Operator workflow for full coverage (do each twice or three times across
different music genres / volumes for diversity):

  # Make sure room is genuinely quiet — no AC, dishwasher, conversation.
  sudo /opt/jasper/.venv/bin/jasper-noise-capture --condition quiet --duration 1800

  # Start varied music playing through the speaker BEFORE running.
  # Aim for genre + volume diversity across multiple invocations.
  sudo /opt/jasper/.venv/bin/jasper-noise-capture --condition music --duration 1800

Each invocation is fully passive — once it starts, walk away. The
script prints progress every 30 seconds so a tail of the terminal
makes it obvious whether capture is still alive.
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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Reuse the shared primitives from wake_enroll: write_wav, the UDP
# port defaults, the systemctl + sudo guards. Avoids drift between
# two near-identical recording CLIs.
from jasper.cli.wake_enroll import (
    CHANNELS,
    DEFAULT_AEC_DTLN_PORT,
    DEFAULT_AEC_OFF_PORT,
    DEFAULT_AEC_ON_PORT,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    VOICE_UNIT,
    require_root,
    systemctl,
)

logger = logging.getLogger("jasper-noise-capture")


CONDITIONS = ("quiet", "music")

# 30 min default per session — matches the spec for "30 min per noise
# corpus" in the experiment plan. The operator can do shorter for
# spot-checking the chain, or longer for a single big session covering
# multiple music genres in one run.
DEFAULT_DURATION_SEC = 1800.0

# Print a heartbeat every 30 s so the operator's terminal stays
# informative across a long capture. Mostly to catch "the AEC bridge
# died and I've been recording silence for 25 minutes."
HEARTBEAT_SEC = 30.0


# ---------------------------------------------------------------------------
# Output-tree helpers — pure, tested
# ---------------------------------------------------------------------------


def noise_dirs(condition: str) -> tuple[str, str]:
    """Return (aec_on_dir, aec_off_dir) names for the given condition.

    Two-leg variant kept for backward compat — most new callers want
    `all_noise_dirs()` which also returns the dtln dir.
    """
    if condition not in CONDITIONS:
        raise ValueError(
            f"unknown condition {condition!r}; expected one of {CONDITIONS}"
        )
    state = "music" if condition == "music" else "nomusic"
    return (f"aec_on_{state}", f"aec_off_{state}")


def all_noise_dirs(condition: str) -> dict[str, str]:
    """Return `{leg_name: noise_dir}` for the given condition.

    Mirrors `wake_enroll.all_quadrant_dirs()` — same
    `aec_{on,off,dtln}_{state}` naming so the training pipeline can
    union enrollment positives, the wake-events corpus, and these
    noise WAVs by directory name.
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


def noise_filename(condition: str, leg: str, now: datetime | None = None) -> str:
    """Return `noise_<condition>_<UTC timestamp>_aec-<leg>.wav`.

    The condition is embedded in the filename in addition to the parent
    directory so a misplaced WAV (rsync mishap, manual move) still
    self-identifies — important if someone hand-mixes the corpora and
    we need to trace a clip back to its source.
    """
    if leg not in ("on", "off", "dtln"):
        raise ValueError(f"leg must be 'on', 'off', or 'dtln'; got {leg!r}")
    if condition not in CONDITIONS:
        raise ValueError(
            f"unknown condition {condition!r}; expected one of {CONDITIONS}"
        )
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return f"noise_{condition}_{ts}_aec-{leg}.wav"


# ---------------------------------------------------------------------------
# Streaming capture — frames-to-WAV-on-disk, constant memory
# ---------------------------------------------------------------------------


async def stream_to_wav(
    udp_capture,
    wav_path: Path,
    duration_sec: float,
    *,
    on_heartbeat=None,
) -> int:
    """Drain `udp_capture.frames()` for `duration_sec`, writing each
    frame directly into an open WAV file at `wav_path`.

    Constant-memory: only one ~80 ms frame is live in RAM at any
    moment. Returns the total sample count written so the caller can
    diff against `duration_sec * SAMPLE_RATE_HZ` to detect underflow
    (typically: AEC bridge died mid-capture).

    `on_heartbeat(elapsed_sec, samples_so_far)` is invoked roughly
    every `HEARTBEAT_SEC` seconds — used by the CLI to print progress.
    """
    tmp = wav_path.with_suffix(wav_path.suffix + ".tmp")
    samples_written = 0
    last_heartbeat = time.monotonic()
    start = last_heartbeat
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE_HZ)
        try:
            async with asyncio.timeout(duration_sec):
                async for frame in udp_capture.frames():
                    w.writeframes(frame.astype(np.int16).tobytes())
                    samples_written += int(frame.size)
                    now = time.monotonic()
                    if on_heartbeat and (now - last_heartbeat) >= HEARTBEAT_SEC:
                        on_heartbeat(now - start, samples_written)
                        last_heartbeat = now
        except asyncio.TimeoutError:
            pass
    os.replace(tmp, wav_path)
    return samples_written


# ---------------------------------------------------------------------------
# Interactive session
# ---------------------------------------------------------------------------


async def run_capture(args: argparse.Namespace) -> int:
    from contextlib import AsyncExitStack

    from jasper.audio_io import UdpMicCapture

    leg_dirs = all_noise_dirs(args.condition)
    output_root: Path = args.output / "noise"
    for d in leg_dirs.values():
        (output_root / d).mkdir(parents=True, exist_ok=True)

    # Decide which legs to capture based on --no-dtln.
    leg_ports: dict[str, int] = {
        "on": args.aec_on_port,
        "off": args.aec_off_port,
    }
    if not args.no_dtln:
        leg_ports["dtln"] = args.aec_dtln_port

    now = datetime.now(timezone.utc)
    leg_paths: dict[str, Path] = {
        leg: output_root / leg_dirs[leg] / noise_filename(args.condition, leg, now=now)
        for leg in leg_ports
    }

    print("\nJTS Noise Corpus Capture")
    print("=" * 40)
    print(f"  Condition : {args.condition}")
    print(f"  Duration  : {args.duration:.0f} s "
          f"({args.duration / 60:.1f} min)")
    print(f"  Output    : {output_root}/")
    for leg, path in leg_paths.items():
        print(f"            : [{leg}] {path.relative_to(output_root)}")
    print()
    if args.condition == "music":
        print(
            "  >>> Make sure VARIED music is playing through the speaker.\n"
            "      Mix genres + volumes for diversity (this is what gets\n"
            "      augmented into the wake-word training negatives).\n"
            "      No human speech in the room while capturing.\n"
        )
    else:
        print(
            "  >>> Make sure the room is GENUINELY quiet — no music, TV,\n"
            "      conversation, dishwasher, AC fan transient.\n"
        )
    if "dtln" in leg_ports:
        print(
            "  Note: dtln leg is included. If your Pi doesn't have\n"
            "  JASPER_WAKE_LEG_DTLN=1 the dtln WAV will be empty —\n"
            "  pass --no-dtln to suppress it.\n"
        )
    input("Press ENTER to start, or Ctrl+C to quit.")

    def make_heartbeat(leg_name: str):
        prefix = f"[{leg_name:>4}] "
        def _cb(elapsed: float, samples: int) -> None:
            print(
                f"  {prefix}t={elapsed:5.0f}s  "
                f"captured={samples / SAMPLE_RATE_HZ:6.1f}s of audio"
            )
        return _cb

    async with AsyncExitStack() as stack:
        captures: dict[str, UdpMicCapture] = {}
        for leg, port in leg_ports.items():
            cap = await stack.enter_async_context(UdpMicCapture(port=port))
            captures[leg] = cap

        await asyncio.sleep(0.3)
        print(f"\nCapturing... heartbeat every {HEARTBEAT_SEC:.0f}s\n")
        leg_samples = await asyncio.gather(*[
            stream_to_wav(
                captures[leg], leg_paths[leg], args.duration,
                on_heartbeat=make_heartbeat(leg),
            )
            for leg in leg_ports
        ])
        samples_by_leg = dict(zip(leg_ports.keys(), leg_samples))

    expected = int(args.duration * SAMPLE_RATE_HZ)
    print("\nCapture complete.")
    for leg in leg_ports:
        n = samples_by_leg[leg]
        print(
            f"  [{leg:>4}] {n / SAMPLE_RATE_HZ:6.1f}s  →  {leg_paths[leg]}"
        )

    # Underflow check: if we got materially less audio than expected on
    # the always-present legs (on/off), something starved the UDP feed.
    # DTLN underflow is informational only — it's expected when the
    # bridge isn't running DTLN.
    underflow_threshold = 0.95
    underflow_legs = [
        leg for leg in ("on", "off")
        if samples_by_leg.get(leg, 0) < expected * underflow_threshold
    ]
    if underflow_legs:
        print(
            f"\n  ⚠  Capture underflow on always-present leg(s): "
            f"{','.join(underflow_legs)} — got less than 95% of expected "
            f"~{expected / SAMPLE_RATE_HZ:.0f}s.\n"
            "     Check that jasper-aec-bridge is running with the\n"
            "     6-channel XVF firmware, and that ports 9876/9877 are\n"
            "     reachable (see HANDOFF-wake-telemetry.md)."
        )
        return 1
    if "dtln" in leg_ports and samples_by_leg["dtln"] < expected * underflow_threshold:
        print(
            f"\n  ⚠  DTLN leg captured {samples_by_leg['dtln'] / SAMPLE_RATE_HZ:.1f}s "
            f"(expected ~{expected / SAMPLE_RATE_HZ:.0f}s). Probable cause: "
            "JASPER_WAKE_LEG_DTLN=0 on this Pi. The on/off WAVs are still "
            "valid; re-run with --no-dtln to drop the empty DTLN WAV."
        )
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-noise-capture",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--condition", required=True, choices=CONDITIONS,
        help='"quiet" or "music". "music" captures the AEC RESIDUAL '
             "(the load-bearing augmentation source for the music-condition "
             "wake-word model).",
    )
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_SEC,
        help=f"Capture duration in seconds (default {DEFAULT_DURATION_SEC:.0f} = 30 min). "
             "Run multiple times with varied music for diversity rather than "
             "one mega-session — the augmentation pipeline samples randomly "
             "across all files in the dir.",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(os.environ.get("JASPER_WAKE_TRAIN_DATA", "data")),
        help="Output root. WAVs land at <output>/noise/aec_{on,off}_{nomusic,music}/. "
             "Default: ./data/ (or $JASPER_WAKE_TRAIN_DATA).",
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
        help="Skip the DTLN leg entirely. Use on 2-stream Pis or with "
             "JASPER_WAKE_LEG_DTLN=0.",
    )
    parser.add_argument(
        "--skip-service-toggle", action="store_true",
        help="Don't stop/start jasper-voice. Useful for dev/test runs where "
             "the daemon is already down.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose systemctl + capture logging.",
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
        return asyncio.run(run_capture(args))
    finally:
        if not args.skip_service_toggle:
            try:
                systemctl("start")
            except subprocess.CalledProcessError as e:
                print(
                    f"\nWARNING: failed to restart {VOICE_UNIT}: {e}\n"
                    f"         Run `sudo systemctl start {VOICE_UNIT}` manually.",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
