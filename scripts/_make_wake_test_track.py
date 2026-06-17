#!/usr/bin/env python3
"""Generate a wake-rate test audio track: N × <PHRASE> with fixed gaps.

Runs on the Pi (uses OpenAI TTS via the API key in /etc/jasper/jasper.env).
Pulled there by `scripts/make-wake-test-track.sh`.

Output: <out-dir>/{<slug>.wav, wake-test-track.wav, .m4a}
  e.g. /tmp/wake-test-track/{jarvis.wav, wake-test-track.wav, ...}
  or   /tmp/wake-test-track-hey-buddy/{hey-buddy.wav, wake-test-track.wav, ...}

The track is fed to your phone (AirDrop) and played back during the
wake-rate test. Same recorded utterance every time eliminates the
'how loud was your voice this time' confound. Compare wake counts
across chip/AEC configurations or across wake models.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path


DEFAULT_OUT_DIR = Path("/tmp/wake-test-track")


def _slug(phrase: str) -> str:
    """Filename-safe slug for a phrase. 'Hey Buddy' -> 'hey-buddy'."""
    return phrase.strip().lower().replace(" ", "-")


def load_env(path: str = "/etc/jasper/jasper.env") -> None:
    """Parse a simple KEY=VAL file into os.environ (does NOT overwrite)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip("'\"")
            os.environ.setdefault(k.strip(), v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reps", type=int, default=20,
                    help="Number of utterances (default 20)")
    ap.add_argument("--gap-sec", type=float, default=4.0,
                    help="Silence after each utterance, seconds (default 4)")
    ap.add_argument("--word", default="Jarvis",
                    help="Text to synthesize (default 'Jarvis')")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"Output directory (default {DEFAULT_OUT_DIR})")
    args = ap.parse_args()
    OUT_DIR = args.out_dir
    SLUG = _slug(args.word)

    load_env()
    # Also load the wizard-written overlays if present (voice provider may be
    # set in voice_provider.env; WS1 Phase 4a moved the OPENAI_API_KEY itself
    # into the group-jasper-secrets voice_keys.env). Reading as root on the Pi.
    load_env("/var/lib/jasper/voice_provider.env")
    load_env("/var/lib/jasper-secrets/voice_keys.env")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY missing in /etc/jasper/jasper.env or "
              "/var/lib/jasper-secrets/voice_keys.env",
              file=sys.stderr)
        return 1

    voice = os.environ.get("JASPER_OPENAI_TTS_VOICE", "alloy")
    model = os.environ.get("JASPER_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT_DIR, 0o777)

    print(f"Generating '{args.word}' via OpenAI TTS")
    print(f"  model={model} voice={voice}")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    with client.audio.speech.with_streaming_response.create(
        model=model, voice=voice, input=args.word, response_format="pcm",
    ) as response:
        pcm = response.read()

    if not pcm:
        print("ERROR: TTS returned empty PCM", file=sys.stderr)
        return 1

    # 24 kHz mono int16 LE → wrap in WAV
    utt_wav = OUT_DIR / f"{SLUG}.wav"
    with wave.open(str(utt_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    dur = len(pcm) / 2 / 24000
    print(f"  → {utt_wav.name}: {dur:.2f}s ({len(pcm)} bytes)")

    # Build the test track via sox: jarvis + gap × reps
    silence_wav = OUT_DIR / f"silence-{args.gap_sec}s.wav"
    subprocess.run([
        "sox", "-n", "-r", "24000", "-c", "1", str(silence_wav),
        "trim", "0", str(args.gap_sec),
    ], check=True)

    track_wav = OUT_DIR / "wake-test-track.wav"
    sox_inputs: list[str] = []
    for _ in range(args.reps):
        sox_inputs.extend([str(utt_wav), str(silence_wav)])
    subprocess.run(["sox", *sox_inputs, str(track_wav)], check=True)

    track_dur = (dur + args.gap_sec) * args.reps
    print(f"  → wake-test-track.wav: ~{track_dur:.1f}s, {args.reps} "
          f"'{args.word}' every {dur + args.gap_sec:.2f}s")

    # Encode to m4a for phones (AAC, ~12 KB/s instead of ~48 KB/s WAV).
    track_m4a = OUT_DIR / "wake-test-track.m4a"
    if shutil.which("ffmpeg"):
        subprocess.run([
            "ffmpeg", "-y", "-i", str(track_wav),
            "-c:a", "aac", "-b:a", "128k",
            str(track_m4a),
        ], check=True, capture_output=True)
        print("  → wake-test-track.m4a: AAC 128 kbps")
    else:
        print("  (ffmpeg not present — skip m4a, .wav works on phone)")

    silence_wav.unlink(missing_ok=True)

    print(f"\nFiles in {OUT_DIR}:")
    for f in sorted(OUT_DIR.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size / 1024:.1f} KB)")
    print(f"\nTrack length: {track_dur:.1f}s — feed this to wake-rate-test.sh as DURATION")
    return 0


if __name__ == "__main__":
    sys.exit(main())
