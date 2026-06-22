# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Phase 1.3 mic validation harness — single-file, throwaway.

Self-contained CLI for measuring wake-word reliability on the Pi-side
chip mic vs the AMOLED satellite mic across the 5-condition test grid
defined in docs/satellites.md "Validation methodology — measurement-
driven Phase 1.3 gate". Pass/fail thresholds and decision tree live
in that doc.

Containment rules — DO NOT VIOLATE:
- Single file. Do not extract helpers into jasper.audio_io,
  jasper.wake, or any "shared" module. If the gate passes, a
  separate PR promotes the satellite to a real MicSource. If
  the gate fails, this file gets `git rm`'d wholesale.
- Imports from jasper.* are forbidden — we mirror MicCapture and
  the existing capture-script patterns rather than coupling to
  production lifecycle. Zero dependency from production code on
  this file means deletion is safe.
- No new dependencies. Uses google-genai, pycamilladsp, sounddevice,
  pyserial, scipy, numpy, openwakeword — all already in pyproject.
- No console-script entry in pyproject.toml during validation.
  Run via `python -m jasper.cli.satellite_validation`. Promotion
  to a `jasper-satellite-validate` script happens only post-pass.
- Outputs go to ./phase1_3/ (gitignored). Never commit.

Subcommands (run on the Pi):
  gen-corpus [--regen]    render corpus.wav + manifest via Gemini TTS
  calibrate-spl           pink noise → user reads phone SPL meter
  run --condition NAME    capture both mics, score per utterance
  summarize DATE_DIR      aggregate CSVs into summary.md

Typical workflow:
  sudo /opt/jasper/.venv/bin/python -m jasper.cli.satellite_validation gen-corpus
  # (copy phase1_3/corpus.wav to phone)
  sudo /opt/jasper/.venv/bin/python -m jasper.cli.satellite_validation calibrate-spl
  sudo /opt/jasper/.venv/bin/python -m jasper.cli.satellite_validation run --condition near_quiet
  sudo /opt/jasper/.venv/bin/python -m jasper.cli.satellite_validation summarize phase1_3/20260509
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime
import io
import json
import logging
import os
import subprocess
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import serial
import sounddevice as sd
from scipy.signal import correlate, lfilter, resample_poly

logger = logging.getLogger("jasper-satellite-validation")

OUTPUT_DIR = Path("phase1_3")
SAMPLE_RATE = 16000
WAKE_FRAME_SAMPLES = 1280
WAKE_MODEL = "hey_jarvis"
WAKE_THRESHOLD = 0.5
SYNC_BEEP_HZ = 1000
SYNC_BEEP_SEC = 0.2
SYNC_LEAD_SEC = 1.0
UTTERANCE_SPACING_SEC = 6.0
UTTERANCE_WINDOW_SEC = 5.0
POST_WAKE_STT_SEC = 4.0
USER_DELAY_HEADROOM_SEC = 10.0
SATELLITE_PORT = "/dev/ttyACM0"
CHIP_DEVICE = "Array"
TTS_MODEL = "gemini-2.5-flash-preview-tts"
STT_MODEL = "gemini-2.5-flash"
TTS_VOICES = ["Aoede", "Charon", "Puck", "Sage", "Orus"]
COMMAND_TEMPLATES = [
    "what time is it",
    "what's the weather like",
    "play some music",
    "next track",
    "pause the music",
    "set the volume to fifty percent",
    "when does the next D train come",
    "is it going to rain today",
    "play some jazz",
    "what song is this",
]


@dataclass
class Condition:
    name: str
    satellite_distance_m: float
    music: bool
    music_target_dbspl: Optional[float]
    frr_pass_pct: float
    wer_pass_pct: float


CONDITIONS: dict[str, Condition] = {
    "near_quiet":     Condition("near_quiet",     1.0, False, None,  5.0,  10.0),
    "near_music_65":  Condition("near_music_65",  1.0, True,  65.0,  10.0, 15.0),
    "near_music_75":  Condition("near_music_75",  1.0, True,  75.0,  25.0, 25.0),
    "far_quiet":      Condition("far_quiet",      2.0, False, None,  15.0, 20.0),
    "far_music_65":   Condition("far_music_65",   2.0, True,  65.0,  30.0, 30.0),
}


# --- Audio helpers ---

def _write_wav_mono(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(audio.astype(np.int16).tobytes())


def _write_wav_stereo(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(audio.astype(np.int16).tobytes())


def _generate_sync_beep() -> np.ndarray:
    n = int(SYNC_BEEP_SEC * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    sig = 0.7 * np.sin(2 * np.pi * SYNC_BEEP_HZ * t)
    return (sig * 32767).astype(np.int16)


def _generate_pink_noise(duration_s: float, sample_rate: int = 48000) -> np.ndarray:
    # Paul Kellet's IIR approximation; fine for SPL calibration where
    # exact 1/f shape isn't required (we just need broadband stationary
    # noise the user's phone meter can read steadily).
    n = int(duration_s * sample_rate)
    rng = np.random.default_rng(42)
    white = rng.standard_normal(n).astype(np.float32)
    b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
    a = [1.0, -2.494956002, 2.017265875, -0.522189400]
    pink = lfilter(b, a, white)
    pink = pink / max(np.max(np.abs(pink)), 1e-9) * 0.5
    int16 = (pink * 32767).astype(np.int16)
    return np.stack([int16, int16], axis=1)


def _downsample_24k_to_16k(pcm_24k_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(pcm_24k_bytes, dtype=np.int16).astype(np.float32)
    resampled = resample_poly(arr, up=2, down=3)
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def _find_beep_offset(audio: np.ndarray, search_s: float = 25.0) -> int:
    # Cross-correlate against a clean 1 kHz template. Even with reverb
    # and BT speaker coloration, the 1 kHz energy peak is unambiguous.
    n = int(SYNC_BEEP_SEC * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    template = (0.7 * np.sin(2 * np.pi * SYNC_BEEP_HZ * t)).astype(np.float32)
    search_n = min(int(search_s * SAMPLE_RATE), len(audio))
    audio_f = audio[:search_n].astype(np.float32) / 32768.0
    if len(audio_f) < len(template):
        return 0
    corr = correlate(audio_f, template, mode="valid", method="fft")
    return int(np.argmax(np.abs(corr)))


# --- Gemini API helpers ---

def _read_gemini_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        return api_key
    # WS1 Phase 4a — the key moved out of jasper.env into the
    # group-`jasper-secrets` voice_keys.env. Check both (run as root on the Pi);
    # a permission/read error on one file falls through to the next.
    for env_path in (
        Path("/var/lib/jasper-secrets/voice_keys.env"),
        Path("/etc/jasper/jasper.env"),
    ):
        try:
            text = env_path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    sys.exit(
        "GEMINI_API_KEY not set (env, "
        "/var/lib/jasper-secrets/voice_keys.env, or /etc/jasper/jasper.env)"
    )


def _gemini_tts(client, text: str, voice: str) -> bytes:
    from google.genai import types as gtypes
    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=gtypes.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(voice_name=voice),
                ),
            ),
        ),
    )
    parts = response.candidates[0].content.parts
    audio_part = next((p for p in parts if getattr(p, "inline_data", None)), None)
    if audio_part is None:
        raise RuntimeError(f"Gemini TTS returned no audio for {text!r}")
    return audio_part.inline_data.data


def _gemini_stt(client, audio_int16: np.ndarray) -> str:
    from google.genai import types as gtypes
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio_int16.astype(np.int16).tobytes())
    response = client.models.generate_content(
        model=STT_MODEL,
        contents=[
            "Transcribe this audio verbatim. Return only the transcription, no quotes or commentary.",
            gtypes.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
        ],
    )
    return (response.text or "").strip()


def _compute_wer(reference: str, hypothesis: str) -> float:
    # Word-level Levenshtein / reference length. Strips common punctuation.
    def tokens(s: str) -> list[str]:
        for ch in ",.?!\"'":
            s = s.replace(ch, "")
        return s.lower().split()
    ref, hyp = tokens(reference), tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return dp[n][m] / n


# --- CamillaDSP volume (only used when a music condition is active) ---

def _set_main_volume_db(vol_db: float) -> None:
    from camilladsp import CamillaClient
    client = CamillaClient("127.0.0.1", 1234)
    try:
        client.connect()
        client.volume.set_main_volume(float(vol_db))
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _predict_volume_for_spl(measurements: dict[str, float], target_spl: float) -> float:
    vols = sorted(float(k) for k in measurements.keys())
    spls = [measurements[str(v) if str(v) in measurements else f"{v:.1f}"] for v in vols]
    # Both axes are dB (log) — linear regression in dB-space is the right model.
    a, b = np.polyfit(spls, vols, 1)
    return float(a * target_spl + b)


# --- Mic capture (mirrors MicCapture and capture-satellite-amoled.sh
# patterns; deliberately not importing them — see containment rules) ---

def _capture_chip(duration_s: float) -> np.ndarray:
    n_samples = int(duration_s * SAMPLE_RATE)
    audio = sd.rec(n_samples, samplerate=SAMPLE_RATE, channels=1,
                   dtype="int16", device=CHIP_DEVICE)
    sd.wait()
    return audio.flatten()


def _capture_satellite(duration_s: float) -> np.ndarray:
    n_samples = int(duration_s * SAMPLE_RATE)
    target_bytes = n_samples * 2
    s = serial.Serial(SATELLITE_PORT, 115200, timeout=0.5)
    try:
        # RTS-toggle reset → guaranteed fresh stream-start marker in this run.
        s.dtr = False
        s.rts = True
        time.sleep(0.1)
        s.rts = False
        s.reset_input_buffer()
        marker = b"[stream-start]"
        buf = b""
        deadline = time.time() + 5
        while time.time() < deadline and buf.find(marker) < 0:
            c = s.read(512)
            if c:
                buf += c
            else:
                time.sleep(0.02)
        idx = buf.find(marker)
        if idx < 0:
            raise RuntimeError(
                f"satellite stream-start not seen on {SATELLITE_PORT} within 5s — "
                "is the firmware running? plugged into the Pi USB-C?"
            )
        eol = buf.find(b"\n", idx)
        binary = buf[eol + 1:] if eol >= 0 else buf[idx + len(marker):]
        deadline = time.time() + duration_s + 2
        while len(binary) < target_bytes and time.time() < deadline:
            c = s.read(8192)
            if c:
                binary += c
        binary = binary[:target_bytes]
    finally:
        s.close()
    if len(binary) < target_bytes:
        binary = binary + b"\x00" * (target_bytes - len(binary))
    return np.frombuffer(binary, dtype=np.int16).copy()


def _capture_both_mics(duration_s: float) -> tuple[np.ndarray, np.ndarray]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        chip_fut = pool.submit(_capture_chip, duration_s)
        sat_fut = pool.submit(_capture_satellite, duration_s)
        return chip_fut.result(), sat_fut.result()


# --- Per-utterance scoring ---

def _score_utterance(
    audio: np.ndarray,
    beep_offset: int,
    utt: dict,
    mic: str,
    model,
    stt_client,
) -> dict:
    window_start = beep_offset + int(utt["start_offset_s"] * SAMPLE_RATE)
    window_end = window_start + int(UTTERANCE_WINDOW_SEC * SAMPLE_RATE)
    window = audio[window_start:window_end]
    base = {
        "utterance_id": utt["id"], "text": utt["text"], "voice": utt["voice"],
        "mic": mic, "wake_fired": 0, "max_score": 0.0,
        "wake_latency_ms": "", "transcription": "", "wer": "",
        "rms_dbfs": "",
    }
    if len(window) < WAKE_FRAME_SAMPLES:
        return base
    max_score, wake_at = 0.0, None
    n_frames = len(window) // WAKE_FRAME_SAMPLES
    for i in range(n_frames):
        frame = window[i * WAKE_FRAME_SAMPLES:(i + 1) * WAKE_FRAME_SAMPLES]
        score = float(model.predict(frame).get(WAKE_MODEL, 0.0))
        if score > max_score:
            max_score = score
        if wake_at is None and score >= WAKE_THRESHOLD:
            wake_at = i
    base["max_score"] = round(max_score, 3)
    base["wake_fired"] = 1 if wake_at is not None else 0
    if wake_at is not None:
        base["wake_latency_ms"] = wake_at * 80
        stt_start = window_start + wake_at * WAKE_FRAME_SAMPLES
        stt_audio = audio[stt_start:stt_start + int(POST_WAKE_STT_SEC * SAMPLE_RATE)]
        try:
            text = _gemini_stt(stt_client, stt_audio)
            base["transcription"] = text
            base["wer"] = round(_compute_wer(utt["text"], text), 3)
        except Exception as e:  # noqa: BLE001
            logger.warning("STT failed for utt %d (%s): %s", utt["id"], mic, e)
    rms = float(np.sqrt(np.mean(window.astype(np.float32) ** 2)))
    if rms > 0:
        base["rms_dbfs"] = round(20 * np.log10(rms / 32768.0), 1)
    return base


# --- Subcommand: gen-corpus ---

def cmd_gen_corpus(args) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = OUTPUT_DIR / "corpus.wav"
    manifest_path = OUTPUT_DIR / "corpus_manifest.json"
    if wav_path.exists() and not args.regen:
        print(f"{wav_path} exists — pass --regen to rebuild")
        return
    from google import genai
    client = genai.Client(api_key=_read_gemini_api_key())
    utterances: list[dict] = []
    for v_idx, voice in enumerate(TTS_VOICES):
        for c_idx, command in enumerate(COMMAND_TEMPLATES):
            utterances.append({
                "id": v_idx * len(COMMAND_TEMPLATES) + c_idx,
                "text": f"Hey Jarvis, {command}",
                "voice": voice,
            })
    print(f"Rendering {len(utterances)} utterances via Gemini TTS...")
    beep = _generate_sync_beep()
    segments: list[tuple[int, np.ndarray]] = [(0, beep)]
    cursor = len(beep) + int(SYNC_LEAD_SEC * SAMPLE_RATE)
    manifest_entries: list[dict] = []
    for u in utterances:
        print(f"  [{u['id'] + 1:2}/{len(utterances)}] {u['voice']:7} {u['text']}")
        try:
            pcm_24k = _gemini_tts(client, u["text"], u["voice"])
        except Exception as e:  # noqa: BLE001
            sys.exit(f"TTS failed for utterance {u['id']}: {e}")
        pcm_16k = _downsample_24k_to_16k(pcm_24k)
        segments.append((cursor, pcm_16k))
        manifest_entries.append({
            "id": u["id"], "text": u["text"], "voice": u["voice"],
            "start_offset_s": cursor / SAMPLE_RATE,
            "duration_s": len(pcm_16k) / SAMPLE_RATE,
        })
        cursor += int(UTTERANCE_SPACING_SEC * SAMPLE_RATE)
    cursor += int(2.0 * SAMPLE_RATE)  # tail silence
    total = np.zeros(cursor, dtype=np.int16)
    for offset, segment in segments:
        end = min(offset + len(segment), len(total))
        total[offset:end] = segment[:end - offset]
    _write_wav_mono(wav_path, total, SAMPLE_RATE)
    manifest_path.write_text(json.dumps({
        "sync_beep": {"freq_hz": SYNC_BEEP_HZ, "duration_s": SYNC_BEEP_SEC,
                      "lead_silence_s": SYNC_LEAD_SEC},
        "utterances": manifest_entries,
        "total_duration_s": cursor / SAMPLE_RATE,
    }, indent=2))
    print(f"\nWrote {wav_path} ({cursor / SAMPLE_RATE:.1f} s)")
    print(f"Wrote {manifest_path}")
    print("\nNext: copy corpus.wav to your phone (AirDrop / SCP / whatever).")


# --- Subcommand: calibrate-spl ---

def cmd_calibrate_spl(args) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "spl_calibration.json"
    pink_path = Path("/tmp/jts_pink_noise.wav")
    _write_wav_stereo(pink_path, _generate_pink_noise(30.0), 48000)
    print("=" * 60)
    print("SPL calibration (phone meter)")
    print("=" * 60)
    print()
    print("Place your phone SPL meter at the listening position (where")
    print("you'll sit during validation runs). Use SLOW response if your")
    print("app supports it. We'll play 30 s of pink noise at 3 levels")
    print("through the JTS music chain — read each into the prompt.")
    print()
    levels = [-30.0, -20.0, -10.0]
    measurements: dict[str, float] = {}
    try:
        for vol_db in levels:
            _set_main_volume_db(vol_db)
            time.sleep(0.5)
            input(f"\nPress ENTER to play pink noise at main_volume={vol_db:.0f} dB...")
            proc = subprocess.Popen(
                ["aplay", "-D", "correction_substream", "-q", str(pink_path)],
            )
            time.sleep(30.5)
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            while True:
                try:
                    spl = float(input(f"Measured dB SPL at main_volume={vol_db:.0f} dB: "))
                    break
                except ValueError:
                    print("  Enter a number.")
            measurements[f"{vol_db:.1f}"] = spl
    finally:
        _set_main_volume_db(-30.0)
        pink_path.unlink(missing_ok=True)
    out.write_text(json.dumps({
        "calibrated_at": datetime.datetime.now().isoformat(),
        "main_volume_db_to_dbspl": measurements,
    }, indent=2))
    print(f"\nWrote {out}")
    print("\nPredicted main_volume settings:")
    for target_spl in (65.0, 75.0):
        vol = _predict_volume_for_spl(measurements, target_spl)
        print(f"  {target_spl:.0f} dB SPL → main_volume = {vol:.1f} dB")


# --- Subcommand: run ---

def cmd_run(args) -> None:
    cond = CONDITIONS[args.condition]
    corpus_path = OUTPUT_DIR / "corpus.wav"
    manifest_path = OUTPUT_DIR / "corpus_manifest.json"
    if not corpus_path.exists() or not manifest_path.exists():
        sys.exit(f"missing {corpus_path} or {manifest_path} — run 'gen-corpus' first")
    manifest = json.loads(manifest_path.read_text())
    main_vol_db: Optional[float] = None
    if cond.music:
        spl_path = OUTPUT_DIR / "spl_calibration.json"
        if not spl_path.exists():
            sys.exit(f"missing {spl_path} — run 'calibrate-spl' first")
        spl_cal = json.loads(spl_path.read_text())["main_volume_db_to_dbspl"]
        main_vol_db = _predict_volume_for_spl(spl_cal, cond.music_target_dbspl)
        _set_main_volume_db(main_vol_db)
    capture_duration_s = manifest["total_duration_s"] + USER_DELAY_HEADROOM_SEC
    today = datetime.datetime.now().strftime("%Y%m%d")
    date_dir = OUTPUT_DIR / today
    raw_dir = date_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print(f"Condition: {cond.name}")
    print("=" * 60)
    print(f"  Satellite: ~{cond.satellite_distance_m:.1f} m from JTS speaker")
    if cond.music:
        print(f"  Music: ON, target {cond.music_target_dbspl:.0f} dB SPL "
              f"(main_volume={main_vol_db:.1f} dB)")
        print("    → Start music on laptop AirPlay NOW if not already playing.")
    else:
        print("  Music: OFF")
    print("  BT speaker at your listening position, ready to play corpus.wav")
    print(f"  Capture: {capture_duration_s:.0f} s (~{capture_duration_s / 60:.1f} min)")
    print()
    input("Press ENTER, then start corpus.wav on your phone within ~5 seconds...")
    print(f"Capturing for {capture_duration_s:.0f} s...")
    chip_audio, sat_audio = _capture_both_mics(capture_duration_s)
    _write_wav_mono(raw_dir / f"{cond.name}_chip.wav", chip_audio, SAMPLE_RATE)
    _write_wav_mono(raw_dir / f"{cond.name}_satellite.wav", sat_audio, SAMPLE_RATE)
    chip_beep = _find_beep_offset(chip_audio)
    sat_beep = _find_beep_offset(sat_audio)
    print(f"Sync beep: chip @ {chip_beep / SAMPLE_RATE:.2f}s, "
          f"satellite @ {sat_beep / SAMPLE_RATE:.2f}s")
    from google import genai
    from openwakeword.model import Model
    stt_client = genai.Client(api_key=_read_gemini_api_key())
    chip_model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    sat_model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    rows: list[dict] = []
    for utt in manifest["utterances"]:
        for mic_name, audio, beep_off, model in [
            ("chip",      chip_audio, chip_beep, chip_model),
            ("satellite", sat_audio,  sat_beep,  sat_model),
        ]:
            row = _score_utterance(audio, beep_off, utt, mic_name, model, stt_client)
            rows.append(row)
            print(f"  utt {utt['id'] + 1:2}/{len(manifest['utterances'])} {mic_name:9} "
                  f"fired={row['wake_fired']} max={row['max_score']:.2f} "
                  f"wer={row['wer'] if row['wer'] != '' else '-'}")
        chip_model.reset()
        sat_model.reset()
    csv_path = date_dir / f"{cond.name}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (date_dir / f"{cond.name}.json").write_text(json.dumps({
        "condition": asdict(cond),
        "wake_model": WAKE_MODEL,
        "wake_threshold": WAKE_THRESHOLD,
        "main_volume_db": main_vol_db,
        "capture_duration_s": capture_duration_s,
        "chip_beep_offset_s": chip_beep / SAMPLE_RATE,
        "satellite_beep_offset_s": sat_beep / SAMPLE_RATE,
        "started_at": datetime.datetime.now().isoformat(),
    }, indent=2))
    chip_rows = [r for r in rows if r["mic"] == "chip"]
    sat_rows = [r for r in rows if r["mic"] == "satellite"]

    def frr(rs):
        return (sum(1 for r in rs if r["wake_fired"] == 0) / len(rs) * 100) if rs else 0.0
    print(f"\nResults for {cond.name}:")
    print(f"  chip      FRR: {frr(chip_rows):.0f}% (pass: <{cond.frr_pass_pct:.0f}%)")
    print(f"  satellite FRR: {frr(sat_rows):.0f}% (pass: <{cond.frr_pass_pct:.0f}%)")
    print(f"\nWrote {csv_path}")


# --- Subcommand: summarize ---

def cmd_summarize(args) -> None:
    date_dir = Path(args.date_dir)
    if not date_dir.is_dir():
        sys.exit(f"not a directory: {date_dir}")
    csvs = sorted(date_dir.glob("*.csv"))
    if not csvs:
        sys.exit(f"no .csv files in {date_dir}")
    lines = [f"# Phase 1.3 mic validation — {date_dir.name}", ""]
    for csv_path in csvs:
        cond_name = csv_path.stem
        if cond_name not in CONDITIONS:
            continue
        cond = CONDITIONS[cond_name]
        with csv_path.open() as f:
            rows = list(csv.DictReader(f))

        def stats(rs):
            if not rs:
                return None, None, None, None
            n = len(rs)
            n_fired = sum(1 for r in rs if r["wake_fired"] == "1")
            frr = (n - n_fired) / n * 100
            wers = sorted(float(r["wer"]) for r in rs if r["wer"])
            wer_mean = (sum(wers) / len(wers) * 100) if wers else None
            wer_p50 = (wers[len(wers) // 2] * 100) if wers else None
            wer_p90 = (wers[int(len(wers) * 0.9)] * 100) if wers else None
            return frr, wer_mean, wer_p50, wer_p90
        chip_st = stats([r for r in rows if r["mic"] == "chip"])
        sat_st = stats([r for r in rows if r["mic"] == "satellite"])
        music_label = (f"music at {cond.music_target_dbspl:.0f} dB SPL"
                       if cond.music else "no music")
        lines.append(
            f"## {cond.name} (satellite ~{cond.satellite_distance_m} m, {music_label})"
        )
        lines.append("")
        lines.append("| Mic | FRR | WER mean | WER p50 | WER p90 | Pass |")
        lines.append("|---|---|---|---|---|---|")
        for mic_name, st in (("chip", chip_st), ("satellite", sat_st)):
            if st[0] is None:
                lines.append(f"| {mic_name} | n/a | n/a | n/a | n/a | — |")
                continue
            frr_v, wer_m, wer_p50, wer_p90 = st
            passed = (frr_v <= cond.frr_pass_pct
                      and (wer_m is None or wer_m <= cond.wer_pass_pct))
            verdict = "✓" if passed else "✗"
            wer_m_s = f"{wer_m:.1f}%" if wer_m is not None else "n/a"
            wer_p50_s = f"{wer_p50:.1f}%" if wer_p50 is not None else "n/a"
            wer_p90_s = f"{wer_p90:.1f}%" if wer_p90 is not None else "n/a"
            lines.append(
                f"| {mic_name} | {frr_v:.1f}% | {wer_m_s} | {wer_p50_s} | "
                f"{wer_p90_s} | {verdict} |"
            )
        lines.append("")
        lines.append(
            f"_Pass: FRR < {cond.frr_pass_pct:.0f}%, WER < {cond.wer_pass_pct:.0f}%_"
        )
        lines.append("")
    summary_path = date_dir / "summary.md"
    summary_path.write_text("\n".join(lines))
    print(f"Wrote {summary_path}\n")
    print("\n".join(lines))


# --- Entry point ---

def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(
        prog="python -m jasper.cli.satellite_validation",
        description="Phase 1.3 mic validation harness (throwaway).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_gen = sub.add_parser("gen-corpus", help="render corpus.wav via Gemini TTS")
    p_gen.add_argument("--regen", action="store_true",
                       help="re-render even if corpus.wav exists")
    p_gen.set_defaults(func=cmd_gen_corpus)
    p_cal = sub.add_parser("calibrate-spl",
                            help="interactive SPL calibration (phone meter)")
    p_cal.set_defaults(func=cmd_calibrate_spl)
    p_run = sub.add_parser("run", help="run one condition cell")
    p_run.add_argument("--condition", required=True, choices=list(CONDITIONS),
                       help="which condition to run")
    p_run.set_defaults(func=cmd_run)
    p_sum = sub.add_parser("summarize", help="aggregate CSVs into summary.md")
    p_sum.add_argument("date_dir", help="path to phase1_3/<YYYYMMDD>/")
    p_sum.set_defaults(func=cmd_summarize)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
