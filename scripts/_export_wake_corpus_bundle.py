#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Export browser-recorded wake corpus sessions into a training bundle.

This is the Phase 0 bridge between JTS's `/wake-corpus/` recorder and the
off-Pi custom wake-word training flow. It does not train a model and does not
extract openWakeWord features. It produces a deterministic, auditable bundle
shape that later feature extraction and LiveKit/openWakeWord injection tooling
can consume.

Input layout is the browser recorder output, typically copied from a Pi:

    data/enrollment_positives/
      metadata/enroll_<member>_<session>.json
      aec_<leg>_<condition>/*.wav

Output layout:

    <output>/
      bundle.json
      manifest.jsonl
      manifest.csv
      rejections.jsonl
      SHA256SUMS
      audio/<split>/<condition>/<distance>/<leg>/<utterance>/<filename>.wav

The split is assigned per utterance, not per leg, so all sibling legs from the
same spoken instance stay together in either train or eval.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
import sys
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_SOURCE_DIR = Path("data/enrollment_positives")
DEFAULT_OUTPUT_ROOT = Path("logs/wake-corpus-export")
TRAIN_SPLIT = "train"
EVAL_SPLIT = "eval"
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH_BYTES = 2

MANIFEST_CSV_FIELDS = (
    "utterance_id",
    "session_id",
    "clip_id",
    "seq",
    "member",
    "label_kind",
    "phrase",
    "transcript",
    "profile",
    "split",
    "condition",
    "distance",
    "leg",
    "leg_label",
    "device_id",
    "native_stream",
    "source_channel",
    "processing",
    "profile_role",
    "capture_status",
    "leg_capture_status",
    "duration_sec",
    "sample_rate_hz",
    "channels",
    "sample_width_bytes",
    "frames",
    "sha256",
    "src_path",
    "bundle_path",
)


@dataclass(frozen=True)
class WavInfo:
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    frames: int
    duration_sec: float


@dataclass(frozen=True)
class ClipRef:
    session_path: Path
    session: dict[str, Any]
    clip: dict[str, Any]

    @property
    def session_id(self) -> str:
        return str(self.session.get("session_id", ""))

    @property
    def clip_id(self) -> str:
        return str(self.clip.get("clip_id", ""))

    @property
    def seq(self) -> int:
        try:
            return int(self.clip.get("seq", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def utterance_id(self) -> str:
        session_id = self.session_id or "unknown-session"
        seq = self.seq
        if seq:
            return f"{session_id}:{seq:03d}"
        clip_id = self.clip_id or "unknown-clip"
        return f"{session_id}:{clip_id}"


def _read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _resolve_wav_path(corpus_dir: Path, path_str: str) -> Path:
    """Resolve recorder paths against a local corpus copy.

    Pi-side metadata stores absolute paths below
    `/var/lib/jasper/enrollment_positives/`. After rsync, those paths need to
    be remapped to the local `corpus_dir`.
    """
    raw = Path(path_str)
    parts = raw.parts
    marker = "enrollment_positives"
    if marker in parts:
        idx = parts.index(marker)
        rel_parts = parts[idx + 1:]
        if rel_parts:
            return corpus_dir.joinpath(*rel_parts)
    if raw.is_absolute():
        return raw
    return corpus_dir / raw


def _safe_path_component(value: object, fallback: str = "unknown") -> str:
    raw = str(value or "").strip().lower()
    out = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)
    return out.strip("_") or fallback


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _wav_info(path: Path) -> WavInfo:
    with wave.open(str(path), "rb") as w:
        sample_rate = int(w.getframerate())
        channels = int(w.getnchannels())
        sample_width = int(w.getsampwidth())
        frames = int(w.getnframes())
    duration = frames / sample_rate if sample_rate else 0.0
    return WavInfo(
        sample_rate_hz=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frames=frames,
        duration_sec=duration,
    )


def _validate_wav(path: Path) -> tuple[WavInfo | None, list[str]]:
    issues: list[str] = []
    try:
        info = _wav_info(path)
    except (wave.Error, EOFError, OSError) as e:
        return None, [f"wav_read_failed:{e}"]
    if info.sample_rate_hz != EXPECTED_SAMPLE_RATE:
        issues.append(f"sample_rate:{info.sample_rate_hz}")
    if info.channels != EXPECTED_CHANNELS:
        issues.append(f"channels:{info.channels}")
    if info.sample_width_bytes != EXPECTED_SAMPLE_WIDTH_BYTES:
        issues.append(f"sample_width:{info.sample_width_bytes}")
    if info.frames <= 0:
        issues.append("empty_wav")
    return info, issues


def _leg_detail(session: dict[str, Any], clip: dict[str, Any], leg: str) -> dict[str, Any]:
    clip_plan = clip.get("capture_plan")
    plans: list[Any] = []
    if isinstance(clip_plan, dict):
        plans.append(clip_plan)
    session_plan = session.get("capture_plan")
    if isinstance(session_plan, dict):
        plans.append(session_plan)

    for plan in plans:
        for detail in plan.get("legs") or []:
            if isinstance(detail, dict) and detail.get("token") == leg:
                return detail

    audio_context = clip.get("audio_context")
    if not isinstance(audio_context, dict):
        audio_context = session.get("audio_context")
    if isinstance(audio_context, dict):
        corpus = audio_context.get("corpus")
        if isinstance(corpus, dict):
            for detail in corpus.get("leg_details") or []:
                if isinstance(detail, dict) and detail.get("token") == leg:
                    return detail
    return {"token": leg}


def _leg_capture_health(clip: dict[str, Any], leg: str) -> tuple[str, dict[str, Any]]:
    health = clip.get("capture_health")
    if not isinstance(health, dict):
        return "unknown", {}
    legs = health.get("legs")
    if not isinstance(legs, dict):
        return str(health.get("status") or "unknown"), {}
    leg_health = legs.get(leg)
    if not isinstance(leg_health, dict):
        return "unknown", {}
    return str(leg_health.get("status") or "unknown"), leg_health


def _training_usable(
    *,
    capture_status: str,
    leg_capture_status: str,
    wav_issues: list[str],
) -> bool:
    return (
        capture_status in {"clean", "warning", "unknown", ""}
        and leg_capture_status in {"clean", "warning", "unknown", ""}
        and not wav_issues
    )


def _load_clips(
    corpus_dir: Path,
    *,
    session_ids: set[str] | None,
    latest: int | None,
    include_deleted: bool,
) -> tuple[list[dict[str, Any]], list[ClipRef]]:
    metadata_dir = corpus_dir / "metadata"
    if not metadata_dir.is_dir():
        raise ValueError(f"{metadata_dir} not found")
    session_paths = sorted(metadata_dir.glob("enroll_*.json"))
    sessions_with_paths: list[tuple[Path, dict[str, Any]]] = []
    for path in session_paths:
        data = _read_json(path)
        session_id = str(data.get("session_id", ""))
        if session_ids is not None and session_id not in session_ids:
            continue
        sessions_with_paths.append((path, data))
    if latest is not None:
        sessions_with_paths = sorted(
            sessions_with_paths,
            key=lambda item: str(item[1].get("session_id", "")),
        )[-latest:]
    sessions = [data for _, data in sessions_with_paths]
    clips: list[ClipRef] = []
    for path, session in sessions_with_paths:
        for clip in session.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            if clip.get("deleted") and not include_deleted:
                continue
            clips.append(ClipRef(session_path=path, session=session, clip=clip))
    return sessions, clips


def _split_clips(
    clips: Iterable[ClipRef],
    *,
    eval_fraction: float,
    seed: int,
) -> dict[str, str]:
    if not 0.0 < eval_fraction < 1.0:
        raise ValueError(f"eval_fraction must be in (0, 1); got {eval_fraction}")

    rng = random.Random(seed)
    by_condition: dict[str, list[ClipRef]] = {}
    for clip in clips:
        condition = str(clip.clip.get("condition") or "unknown")
        by_condition.setdefault(condition, []).append(clip)

    split_by_utterance: dict[str, str] = {}
    for values in by_condition.values():
        ordered = sorted(
            values,
            key=lambda c: (c.session_id, c.seq, c.clip_id),
        )
        rng.shuffle(ordered)
        if not ordered:
            continue
        if len(ordered) == 1:
            eval_count = 0
        else:
            eval_count = max(1, round(len(ordered) * eval_fraction))
            eval_count = min(eval_count, len(ordered) - 1)
        eval_ids = {clip.utterance_id for clip in ordered[:eval_count]}
        for clip in ordered:
            split_by_utterance[clip.utterance_id] = (
                EVAL_SPLIT if clip.utterance_id in eval_ids else TRAIN_SPLIT
            )
    return split_by_utterance


def _copy_audio(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _row_for_leg(
    *,
    corpus_dir: Path,
    output_dir: Path,
    clip_ref: ClipRef,
    leg: str,
    path_str: str,
    split: str,
    copy_audio: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    src = _resolve_wav_path(corpus_dir, path_str)
    base_common = {
        "schema_version": SCHEMA_VERSION,
        "utterance_id": clip_ref.utterance_id,
        "session_id": clip_ref.session_id,
        "clip_id": clip_ref.clip_id,
        "seq": clip_ref.seq,
        "member": str(clip_ref.clip.get("member") or clip_ref.session.get("member") or ""),
        "label_kind": str(
            clip_ref.clip.get("label_kind")
            or clip_ref.clip.get("target_kind")
            or clip_ref.session.get("label_kind")
            or clip_ref.session.get("target_kind")
            or ""
        ),
        "phrase": str(
            clip_ref.clip.get("phrase")
            or clip_ref.clip.get("target_phrase")
            or clip_ref.session.get("phrase")
            or clip_ref.session.get("target_phrase")
            or ""
        ),
        "transcript": str(
            clip_ref.clip.get("transcript")
            or clip_ref.clip.get("prompt")
            or ""
        ),
        "profile": str(clip_ref.session.get("corpus_profile") or "standard"),
        "split": split,
        "condition": str(clip_ref.clip.get("condition") or ""),
        "distance": str(clip_ref.clip.get("distance") or ""),
        "leg": leg,
        "src_path": str(src),
    }
    if not src.is_file():
        return None, {**base_common, "reason": "missing_wav"}

    info, wav_issues = _validate_wav(src)
    if info is None:
        return None, {**base_common, "reason": ";".join(wav_issues)}

    digest = _sha256(src)
    detail = _leg_detail(clip_ref.session, clip_ref.clip, leg)
    capture_health = clip_ref.clip.get("capture_health")
    capture_status = (
        str(capture_health.get("status") or "unknown")
        if isinstance(capture_health, dict) else "unknown"
    )
    leg_capture_status, leg_health = _leg_capture_health(clip_ref.clip, leg)
    training_usable = _training_usable(
        capture_status=capture_status,
        leg_capture_status=leg_capture_status,
        wav_issues=wav_issues,
    )
    if not training_usable:
        return None, {
            **base_common,
            "reason": "not_training_usable",
            "capture_status": capture_status,
            "leg_capture_status": leg_capture_status,
            "wav_issues": wav_issues,
        }

    safe_condition = _safe_path_component(base_common["condition"])
    safe_distance = _safe_path_component(base_common["distance"])
    safe_leg = _safe_path_component(leg)
    safe_utterance = _safe_path_component(
        str(base_common["utterance_id"]).replace(":", "_"),
    )
    dst_rel = (
        Path("audio")
        / split
        / safe_condition
        / safe_distance
        / safe_leg
        / safe_utterance
        / src.name
    )
    dst = output_dir / dst_rel
    if copy_audio:
        _copy_audio(src, dst)

    row = {
        **base_common,
        "leg_label": str(detail.get("label") or detail.get("name") or leg),
        "device_id": str(detail.get("device_id") or ""),
        "device_label": str(detail.get("device_label") or ""),
        "native_stream": str(detail.get("native_stream") or ""),
        "source_channel": str(detail.get("source_channel") or ""),
        "processing": str(detail.get("processing") or ""),
        "processing_label": str(detail.get("processing_label") or ""),
        "profile_role": str(detail.get("profile_role") or ""),
        "wake_input": bool(detail.get("wake_input", False)),
        "capture_status": capture_status,
        "leg_capture_status": leg_capture_status,
        "leg_capture_health": leg_health,
        "capture_health": capture_health if isinstance(capture_health, dict) else {},
        "duration_sec": info.duration_sec,
        "sample_rate_hz": info.sample_rate_hz,
        "channels": info.channels,
        "sample_width_bytes": info.sample_width_bytes,
        "frames": info.frames,
        "sha256": digest,
        "bundle_path": str(dst_rel) if copy_audio else "",
        "source_session_metadata": str(clip_ref.session_path),
    }
    return row, None


def export_bundle(
    corpus_dir: Path,
    output_dir: Path,
    *,
    session_ids: set[str] | None = None,
    latest: int | None = None,
    eval_fraction: float = 0.2,
    seed: int = 42,
    include_deleted: bool = False,
    copy_audio: bool = True,
) -> dict[str, Any]:
    sessions, clips = _load_clips(
        corpus_dir,
        session_ids=session_ids,
        latest=latest,
        include_deleted=include_deleted,
    )
    split_by_utterance = _split_clips(clips, eval_fraction=eval_fraction, seed=seed)

    rows: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for clip_ref in clips:
        files = clip_ref.clip.get("files")
        if not isinstance(files, dict):
            rejections.append({
                "schema_version": SCHEMA_VERSION,
                "utterance_id": clip_ref.utterance_id,
                "session_id": clip_ref.session_id,
                "clip_id": clip_ref.clip_id,
                "reason": "clip_files_missing",
            })
            continue
        split = split_by_utterance.get(clip_ref.utterance_id, TRAIN_SPLIT)
        for leg in sorted(files):
            path_str = files.get(leg)
            if not path_str:
                continue
            row, rejection = _row_for_leg(
                corpus_dir=corpus_dir,
                output_dir=output_dir,
                clip_ref=clip_ref,
                leg=str(leg),
                path_str=str(path_str),
                split=split,
                copy_audio=copy_audio,
            )
            if row is not None:
                rows.append(row)
            if rejection is not None:
                rejections.append(rejection)

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_jsonl = output_dir / "manifest.jsonl"
    with open(manifest_jsonl, "w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    manifest_csv = output_dir / "manifest.csv"
    with open(manifest_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MANIFEST_CSV_FIELDS})

    rejections_path = output_dir / "rejections.jsonl"
    with open(rejections_path, "w") as f:
        for rejection in rejections:
            f.write(json.dumps(rejection, sort_keys=True) + "\n")

    sha_path = output_dir / "SHA256SUMS"
    with open(sha_path, "w") as f:
        for row in sorted(rows, key=lambda r: str(r.get("bundle_path") or r["src_path"])):
            path = row.get("bundle_path") or row["src_path"]
            f.write(f"{row['sha256']}  {path}\n")

    summary = _bundle_summary(
        corpus_dir=corpus_dir,
        output_dir=output_dir,
        sessions=sessions,
        rows=rows,
        rejections=rejections,
        eval_fraction=eval_fraction,
        seed=seed,
        copy_audio=copy_audio,
    )
    bundle_json = output_dir / "bundle.json"
    bundle_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def _bundle_summary(
    *,
    corpus_dir: Path,
    output_dir: Path,
    sessions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    eval_fraction: float,
    seed: int,
    copy_audio: bool,
) -> dict[str, Any]:
    def count_by(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key) or "")
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    utterance_ids = {str(row["utterance_id"]) for row in rows}
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus_dir": str(corpus_dir),
        "output_dir": str(output_dir),
        "copy_audio": copy_audio,
        "eval_fraction": eval_fraction,
        "seed": seed,
        "session_count": len(sessions),
        "utterance_count": len(utterance_ids),
        "manifest_row_count": len(rows),
        "rejection_count": len(rejections),
        "counts": {
            "by_split": count_by("split"),
            "by_profile": count_by("profile"),
            "by_condition": count_by("condition"),
            "by_distance": count_by("distance"),
            "by_leg": count_by("leg"),
            "by_processing": count_by("processing"),
            "by_device": count_by("device_id"),
        },
        "sessions": [
            {
                "session_id": session.get("session_id"),
                "member": session.get("member"),
                "corpus_profile": session.get("corpus_profile", "standard"),
                "enabled_legs": session.get("enabled_legs", []),
                "metadata_schema_version": session.get("metadata_schema_version"),
            }
            for session in sessions
        ],
        "artifacts": {
            "bundle": "bundle.json",
            "manifest_jsonl": "manifest.jsonl",
            "manifest_csv": "manifest.csv",
            "rejections_jsonl": "rejections.jsonl",
            "sha256sums": "SHA256SUMS",
        },
    }


def _default_output_dir(now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return DEFAULT_OUTPUT_ROOT / now.strftime("%Y%m%dT%H%M%SZ")


def _print_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Wake corpus bundle export",
        "=" * 60,
        f"  output        : {summary['output_dir']}",
        f"  sessions      : {summary['session_count']}",
        f"  utterances    : {summary['utterance_count']}",
        f"  manifest rows : {summary['manifest_row_count']}",
        f"  rejections    : {summary['rejection_count']}",
        f"  copy audio    : {summary['copy_audio']}",
        "",
        "  by split:",
    ]
    for split, count in summary["counts"]["by_split"].items():
        lines.append(f"    {split:<8} {count}")
    lines.append("")
    lines.append("  by leg:")
    for leg, count in summary["counts"]["by_leg"].items():
        lines.append(f"    {leg:<24} {count}")
    return "\n".join(lines)


def _non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _safe_to_remove_output(path: Path, *, corpus_dir: Path) -> bool:
    resolved = path.expanduser().resolve()
    corpus_resolved = corpus_dir.expanduser().resolve()
    blocked = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        corpus_resolved,
    }
    if resolved in blocked:
        return False
    return corpus_resolved not in resolved.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export browser wake-corpus recordings into a training bundle.",
    )
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Source corpus directory (default: {DEFAULT_SOURCE_DIR})",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "Output bundle directory. Default: "
            "logs/wake-corpus-export/<UTC timestamp>."
        ),
    )
    parser.add_argument(
        "--session",
        action="append",
        dest="sessions",
        help="Export only this session id. May be repeated.",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=None,
        help="Export the latest N sessions by session id.",
    )
    parser.add_argument(
        "--eval-fraction",
        type=float,
        default=0.2,
        help="Fraction of utterances held out per condition (default 0.2).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for deterministic train/eval split (default 42).",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include clips marked deleted in session metadata.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Do not copy WAVs; write manifest rows pointing at source paths.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing non-empty output directory before export.",
    )
    args = parser.parse_args(argv)

    if args.sessions and args.latest is not None:
        parser.error("--session and --latest are mutually exclusive")
    output_dir = args.output_dir or _default_output_dir()
    if _non_empty(output_dir):
        if not args.force:
            print(
                f"ERROR: {output_dir} already exists and is non-empty. "
                "Use --force or choose a new output directory.",
                file=sys.stderr,
            )
            return 2
        if not _safe_to_remove_output(output_dir, corpus_dir=args.corpus_dir):
            print(
                f"ERROR: refusing to remove unsafe output directory: {output_dir}",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(output_dir)

    try:
        summary = export_bundle(
            args.corpus_dir,
            output_dir,
            session_ids=set(args.sessions) if args.sessions else None,
            latest=args.latest,
            eval_fraction=args.eval_fraction,
            seed=args.seed,
            include_deleted=args.include_deleted,
            copy_audio=not args.manifest_only,
        )
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(_print_summary(summary))
    return 1 if summary["manifest_row_count"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
