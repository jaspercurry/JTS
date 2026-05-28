#!/usr/bin/env python3
"""Offline waveform-fusion experiment for wake-corpus legs.

This is a research harness, not a production audio path. It takes paired
same-utterance recorder legs such as ``on`` + ``dtln`` or
``usb_webrtc`` + ``usb_dtln``, creates delay/weight-swept waveform mixes,
and optionally scores the originals and mixes with an openWakeWord ONNX
model.

The experiment is intentionally designed to answer one question:

    Does a mixed waveform beat ordinary per-leg score fusion?

If it does not beat max-score/OR fusion on the same clips, the mixed
waveform is not worth carrying into the real-time path.

Usage:

  python scripts/_waveform_fusion_experiment.py \\
      --corpus-dir ./data/enrollment_positives \\
      --session 20260528T184424Z-d205 \\
      --model ./models/jarvis_v2.onnx

  python scripts/_waveform_fusion_experiment.py \\
      --corpus-dir ./data/enrollment_positives \\
      --latest \\
      --no-score

Outputs go under ``captures/waveform-fusion/<session>/`` by default.
That tree is gitignored.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import types as _types
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


_cvm_stub = _types.ModuleType("openwakeword.custom_verifier_model")
_cvm_stub.train_custom_verifier = None
sys.modules.setdefault("openwakeword.custom_verifier_model", _cvm_stub)

SAMPLE_RATE_HZ = 16000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
FRAME_SAMPLES = 1280
THRESHOLD = 0.5

DEFAULT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("xvf_aec3_dtln", "on", "dtln"),
    ("usb_aec3_dtln", "usb_webrtc", "usb_dtln"),
)
DEFAULT_DELAYS_MS = (-40, -20, -10, 0, 10, 20, 40)
DEFAULT_WEIGHTS = ((0.75, 0.25), (0.50, 0.50), (0.25, 0.75))
DEFAULT_NORMALIZATIONS = ("native", "rms_match")


@dataclass(frozen=True)
class WavAudio:
    samples: np.ndarray
    sample_rate: int
    channels: int
    sample_width: int


@dataclass(frozen=True)
class ClipLeg:
    seq: int
    condition: str
    distance: str
    leg: str
    path: Path


def _dbfs_from_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    if rms <= 0.0:
        return -100.0
    return 20.0 * math.log10(rms / 32768.0)


def _dbfs_from_peak(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -100.0
    peak = int(np.max(np.abs(samples.astype(np.int32))))
    if peak <= 0:
        return -100.0
    return 20.0 * math.log10(peak / 32768.0)


def read_wav_int16_mono(path: Path) -> WavAudio:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    if sample_rate != SAMPLE_RATE_HZ or channels != CHANNELS or sample_width != SAMPLE_WIDTH_BYTES:
        raise ValueError(
            f"{path}: expected 16 kHz mono int16, "
            f"got sr={sample_rate} ch={channels} width={sample_width}"
        )
    return WavAudio(
        samples=np.frombuffer(raw, dtype=np.int16).copy(),
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )


def write_wav_int16_mono(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = np.clip(np.rint(samples), -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(out.tobytes())


def shift_samples(samples: np.ndarray, offset_samples: int) -> np.ndarray:
    """Return a length-preserving shifted copy.

    Positive offsets delay the signal: zeros are inserted at the front
    and the tail is trimmed. Negative offsets advance it.
    """
    if offset_samples == 0:
        return samples.copy()
    out = np.zeros_like(samples)
    if abs(offset_samples) >= len(samples):
        return out
    if offset_samples > 0:
        out[offset_samples:] = samples[:-offset_samples]
    else:
        out[:offset_samples] = samples[-offset_samples:]
    return out


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _match_pair_rms(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Scale both streams to the louder stream's RMS, with a sane cap.

    DTLN outputs are often much quieter than AEC3. Without RMS matching,
    a nominal 50/50 mix can effectively be "AEC3 plus a whisper of DTLN".
    The cap prevents a near-silent leg from being amplified into nonsense.
    """
    af = a.astype(np.float64)
    bf = b.astype(np.float64)
    ar = _rms(af)
    br = _rms(bf)
    target = max(ar, br, 1.0)
    max_gain = 10.0  # +20 dB
    if ar > 0.0:
        af *= min(target / ar, max_gain)
    if br > 0.0:
        bf *= min(target / br, max_gain)
    return af, bf


def mix_pair(
    a: np.ndarray,
    b: np.ndarray,
    *,
    delay_ms: float,
    weight_a: float,
    weight_b: float,
    normalization: str,
) -> tuple[np.ndarray, float]:
    """Mix two same-utterance legs and return (samples, guard_gain)."""
    n = min(len(a), len(b))
    af = a[:n].astype(np.float64)
    bf = b[:n].astype(np.float64)
    delay_samples = int(round(delay_ms * SAMPLE_RATE_HZ / 1000.0))
    bf = shift_samples(bf, delay_samples)
    if normalization == "rms_match":
        af, bf = _match_pair_rms(af, bf)
    elif normalization != "native":
        raise ValueError(f"unknown normalization: {normalization}")
    mixed = af * weight_a + bf * weight_b
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    guard_gain = 1.0
    if peak > 32767.0:
        guard_gain = 32767.0 / peak
        mixed *= guard_gain
    return mixed, guard_gain


def _resolve_wav_path(corpus_dir: Path, path_str: str) -> Path:
    raw = Path(path_str)
    marker = "enrollment_positives"
    if marker in raw.parts:
        idx = raw.parts.index(marker)
        rel_parts = raw.parts[idx + 1 :]
        if rel_parts:
            return corpus_dir.joinpath(*rel_parts)
    if raw.is_absolute():
        return raw
    return corpus_dir / raw


def _metadata_paths(corpus_dir: Path) -> list[Path]:
    return sorted((corpus_dir / "metadata").glob("enroll_*.json"))


def _session_id_from_metadata(path: Path, data: dict[str, Any]) -> str:
    value = data.get("session_id") or data.get("session")
    if isinstance(value, str) and value:
        return value
    name = path.stem
    parts = name.split("_", 2)
    return parts[-1] if parts else name


def load_session(corpus_dir: Path, session: str | None, *, latest: bool) -> tuple[Path, dict[str, Any]]:
    paths = _metadata_paths(corpus_dir)
    if not paths:
        raise FileNotFoundError(f"no metadata files found under {corpus_dir / 'metadata'}")
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        data = json.loads(path.read_text())
        sid = _session_id_from_metadata(path, data)
        if latest or session is None or session in sid or session in path.name:
            candidates.append((path, data))
    if not candidates:
        raise FileNotFoundError(f"session {session!r} not found under {corpus_dir / 'metadata'}")
    if latest or session is None:
        candidates.sort(key=lambda item: item[0].stat().st_mtime)
        return candidates[-1]
    if len(candidates) > 1:
        exact = [
            item for item in candidates
            if _session_id_from_metadata(item[0], item[1]) == session
        ]
        if len(exact) == 1:
            return exact[0]
        raise ValueError(
            f"session {session!r} matched multiple metadata files: "
            + ", ".join(str(path.name) for path, _ in candidates)
        )
    return candidates[0]


def iter_clip_legs(corpus_dir: Path, session_data: dict[str, Any]) -> Iterable[ClipLeg]:
    clips = session_data.get("clips") or []
    for idx, clip in enumerate(clips, start=1):
        if clip.get("deleted"):
            continue
        seq = int(clip.get("seq") or clip.get("sequence") or idx)
        condition = str(clip.get("condition") or "")
        distance = str(clip.get("distance") or "")
        files = clip.get("files") or {}
        if not isinstance(files, dict):
            continue
        for leg, value in files.items():
            if not isinstance(value, str) or not value:
                continue
            yield ClipLeg(
                seq=seq,
                condition=condition,
                distance=distance,
                leg=str(leg),
                path=_resolve_wav_path(corpus_dir, value),
            )


def _score_key_for_model(model_path: str) -> str:
    if "/" in model_path or model_path.endswith((".onnx", ".tflite")):
        return Path(model_path).stem
    return model_path


def _make_wake_model(model_path: str):
    from openwakeword.model import Model

    return Model(wakeword_models=[model_path], inference_framework="onnx")


def score_samples(model: Any, score_key: str, samples: np.ndarray) -> float:
    if hasattr(model, "reset"):
        model.reset()
    scores: list[float] = []
    complete = len(samples) // FRAME_SAMPLES
    for idx in range(complete):
        frame = samples[idx * FRAME_SAMPLES : (idx + 1) * FRAME_SAMPLES].astype(np.int16)
        preds = model.predict(frame)
        scores.append(float(preds.get(score_key, 0.0)))
    return max(scores) if scores else 0.0


def _parse_pairs(values: list[str]) -> tuple[tuple[str, str, str], ...]:
    if not values:
        return DEFAULT_PAIRS
    pairs: list[tuple[str, str, str]] = []
    for value in values:
        parts = value.split(":")
        if len(parts) != 3:
            raise argparse.ArgumentTypeError(
                f"{value!r}: expected name:leg_a:leg_b, e.g. xvf_aec3_dtln:on:dtln"
            )
        pairs.append((parts[0], parts[1], parts[2]))
    return tuple(pairs)


def _parse_float_csv(value: str) -> tuple[float, ...]:
    return tuple(float(part.strip()) for part in value.split(",") if part.strip())


def _parse_weights(value: str) -> tuple[tuple[float, float], ...]:
    weights: list[tuple[float, float]] = []
    for part in value.split(","):
        left, sep, right = part.partition("/")
        if not sep:
            raise argparse.ArgumentTypeError(f"{part!r}: expected A/B")
        weights.append((float(left), float(right)))
    return tuple(weights)


def _row(
    *,
    session_id: str,
    seq: int,
    condition: str,
    distance: str,
    pair: str,
    kind: str,
    leg_or_variant: str,
    path: Path,
    samples: np.ndarray,
    score: float | None,
    delay_ms: float | None = None,
    weight_a: float | None = None,
    weight_b: float | None = None,
    normalization: str | None = None,
    guard_gain: float | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "seq": seq,
        "condition": condition,
        "distance": distance,
        "pair": pair,
        "kind": kind,
        "leg_or_variant": leg_or_variant,
        "delay_ms": "" if delay_ms is None else f"{delay_ms:g}",
        "weight_a": "" if weight_a is None else f"{weight_a:g}",
        "weight_b": "" if weight_b is None else f"{weight_b:g}",
        "normalization": normalization or "",
        "score": "" if score is None else f"{score:.6f}",
        "hit_05": "" if score is None else int(score >= THRESHOLD),
        "rms_dbfs": f"{_dbfs_from_rms(samples):.2f}",
        "peak_dbfs": f"{_dbfs_from_peak(samples):.2f}",
        "guard_gain_db": "" if guard_gain in (None, 1.0) else f"{20.0 * math.log10(guard_gain):.2f}",
        "path": str(path),
    }


def _print_summary(rows: list[dict[str, Any]], *, scored: bool) -> str:
    lines: list[str] = []
    if not scored:
        lines.append("Scoring skipped; generated waveform mixes only.")
        return "\n".join(lines)

    def score(row: dict[str, Any]) -> float:
        return float(row["score"] or 0.0)

    by_variant: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["pair"], row["kind"], row["leg_or_variant"])
        by_variant.setdefault(key, []).append(row)

    lines.append("Hit counts at threshold 0.5")
    for key in sorted(by_variant):
        values = by_variant[key]
        hits = sum(1 for row in values if score(row) >= THRESHOLD)
        lines.append(f"  {key[0]} {key[1]} {key[2]}: {hits}/{len(values)}")

    # Compare each mix against max-score fusion of the pair's two originals.
    originals_by_pair_seq: dict[tuple[str, int], list[dict[str, Any]]] = {}
    mixes_by_variant: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["kind"] == "original":
            originals_by_pair_seq.setdefault((row["pair"], int(row["seq"])), []).append(row)
        elif row["kind"] == "mix":
            mixes_by_variant.setdefault((row["pair"], row["leg_or_variant"]), []).append(row)

    lines.append("")
    lines.append("Mixes versus same-pair max-score fusion")
    for key in sorted(mixes_by_variant):
        mix_rows = mixes_by_variant[key]
        beats_pair_fusion = 0
        mix_hits = 0
        pair_hits = 0
        for row in mix_rows:
            originals = originals_by_pair_seq.get((row["pair"], int(row["seq"])), [])
            pair_best = max((score(item) for item in originals), default=0.0)
            row_score = score(row)
            if row_score >= THRESHOLD:
                mix_hits += 1
            if pair_best >= THRESHOLD:
                pair_hits += 1
            if row_score >= THRESHOLD and pair_best < THRESHOLD:
                beats_pair_fusion += 1
        lines.append(
            f"  {key[0]} {key[1]}: mix_hits={mix_hits}/{len(mix_rows)} "
            f"pair_fusion_hits={pair_hits}/{len(mix_rows)} "
            f"new_saves={beats_pair_fusion}"
        )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    corpus_dir = Path(args.corpus_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None

    metadata_path, session_data = load_session(corpus_dir, args.session, latest=args.latest)
    session_id = _session_id_from_metadata(metadata_path, session_data)
    if out_dir is None:
        out_dir = (Path("captures") / "waveform-fusion" / session_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = _parse_pairs(args.pair)
    delays_ms = _parse_float_csv(args.delays_ms)
    weights = _parse_weights(args.weights)
    normalizations = tuple(args.normalization or DEFAULT_NORMALIZATIONS)

    score_model = None
    score_key = ""
    scored = False
    if not args.no_score:
        if not args.model:
            print("No --model supplied; scoring skipped.", file=sys.stderr)
        elif "/" in args.model and not Path(args.model).expanduser().is_file():
            print(f"Model file not found; scoring skipped: {args.model}", file=sys.stderr)
        else:
            try:
                score_model = _make_wake_model(str(Path(args.model).expanduser()) if "/" in args.model else args.model)
                score_key = _score_key_for_model(args.model)
                scored = True
            except Exception as exc:  # noqa: BLE001
                print(f"Could not initialize openWakeWord scoring; scoring skipped: {exc}", file=sys.stderr)

    legs_by_seq: dict[int, dict[str, ClipLeg]] = {}
    for item in iter_clip_legs(corpus_dir, session_data):
        legs_by_seq.setdefault(item.seq, {})[item.leg] = item

    rows: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "session_id": session_id,
        "metadata_path": str(metadata_path),
        "corpus_dir": str(corpus_dir),
        "pairs": pairs,
        "delays_ms": delays_ms,
        "weights": weights,
        "normalizations": normalizations,
        "scored": scored,
        "model": args.model or None,
        "threshold": THRESHOLD,
        "outputs": [],
    }

    for pair_name, leg_a, leg_b in pairs:
        for seq, legs in sorted(legs_by_seq.items()):
            if leg_a not in legs or leg_b not in legs:
                continue
            a_info = legs[leg_a]
            b_info = legs[leg_b]
            if not a_info.path.is_file() or not b_info.path.is_file():
                print(
                    f"skip seq={seq} pair={pair_name}: missing "
                    f"{leg_a if not a_info.path.is_file() else leg_b} WAV",
                    file=sys.stderr,
                )
                continue
            a = read_wav_int16_mono(a_info.path).samples
            b = read_wav_int16_mono(b_info.path).samples

            for leg_name, info, samples in ((leg_a, a_info, a), (leg_b, b_info, b)):
                score = score_samples(score_model, score_key, samples) if scored else None
                rows.append(
                    _row(
                        session_id=session_id,
                        seq=seq,
                        condition=info.condition,
                        distance=info.distance,
                        pair=pair_name,
                        kind="original",
                        leg_or_variant=leg_name,
                        path=info.path,
                        samples=samples,
                        score=score,
                    )
                )

            for normalization in normalizations:
                for delay_ms in delays_ms:
                    for weight_a, weight_b in weights:
                        mixed, guard_gain = mix_pair(
                            a,
                            b,
                            delay_ms=delay_ms,
                            weight_a=weight_a,
                            weight_b=weight_b,
                            normalization=normalization,
                        )
                        variant = (
                            f"{normalization}_delay{delay_ms:g}_"
                            f"w{weight_a:g}-{weight_b:g}"
                        )
                        mix_path = out_dir / pair_name / f"clip{seq:03d}_{variant}.wav"
                        write_wav_int16_mono(mix_path, mixed)
                        score = score_samples(score_model, score_key, mixed) if scored else None
                        rows.append(
                            _row(
                                session_id=session_id,
                                seq=seq,
                                condition=a_info.condition,
                                distance=a_info.distance,
                                pair=pair_name,
                                kind="mix",
                                leg_or_variant=variant,
                                path=mix_path,
                                samples=mixed,
                                score=score,
                                delay_ms=delay_ms,
                                weight_a=weight_a,
                                weight_b=weight_b,
                                normalization=normalization,
                                guard_gain=guard_gain,
                            )
                        )
                        manifest["outputs"].append(str(mix_path))

    if not rows:
        print("No paired clips found. Check --corpus-dir, --session, and --pair.", file=sys.stderr)
        return 1

    csv_path = out_dir / "waveform_fusion_scores.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    summary = _print_summary(rows, scored=scored)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(
        f"# Wake Waveform Fusion Experiment\n\n"
        f"- Session: `{session_id}`\n"
        f"- Metadata: `{metadata_path}`\n"
        f"- Rows: {len(rows)}\n"
        f"- Scored: {scored}\n\n"
        f"```text\n{summary}\n```\n"
    )

    print(f"session: {session_id}")
    print(f"metadata: {metadata_path}")
    print(f"rows: {len(rows)}")
    print(f"csv: {csv_path}")
    print(f"summary: {summary_path}")
    print("")
    print(summary)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--corpus-dir", required=True, help="local enrollment_positives directory")
    parser.add_argument("--session", help="session id or unique substring")
    parser.add_argument("--latest", action="store_true", help="use latest metadata file")
    parser.add_argument("--out-dir", help="output directory; default captures/waveform-fusion/<session>")
    parser.add_argument("--model", help="openWakeWord model path/name for scoring")
    parser.add_argument("--no-score", action="store_true", help="generate mixes without wake scoring")
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="pair as name:leg_a:leg_b; repeatable. Defaults to XVF and USB AEC3+DTLN pairs.",
    )
    parser.add_argument(
        "--delays-ms",
        default=",".join(str(v) for v in DEFAULT_DELAYS_MS),
        help="comma-separated delay sweep for leg_b relative to leg_a",
    )
    parser.add_argument(
        "--weights",
        default=",".join(f"{a:g}/{b:g}" for a, b in DEFAULT_WEIGHTS),
        help="comma-separated A/B weights, e.g. 0.75/0.25,0.5/0.5",
    )
    parser.add_argument(
        "--normalization",
        action="append",
        choices=("native", "rms_match"),
        default=None,
        help="normalization mode; repeatable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.latest and not args.session:
        parser.error("provide --session or --latest")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
