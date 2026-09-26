# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The open wake-corpus session's clip records and their WAV files.

The store shares ``RecordingBackend``'s state lock: its plain methods take
it, and a ``*_locked`` method runs inside a critical section its caller
already holds.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from jasper.cli.wake_enroll import write_wav
from jasper.wake_conditions import CORPUS_DIR_BY_CONDITION

from .session_store import ClipMetadata

logger = logging.getLogger("jasper-wake-corpus-web")


class ClipStore:
    """The open session's clip records, in recording order."""

    def __init__(self, lock: threading.Lock, output_dir: Path) -> None:
        self._lock = lock
        self._output_dir = output_dir
        self._clips: list[ClipMetadata] = []

    def replace_locked(self, clips: list[ClipMetadata]) -> None:
        self._clips = clips

    def live_count_locked(self) -> int:
        return sum(1 for c in self._clips if not c.deleted)

    def to_json_locked(self) -> list[dict[str, Any]]:
        return [c.to_json() for c in self._clips]

    def next_seq(self) -> int:
        # Sequence is per-session, not per-condition, so filenames stay
        # unique across the whole session. Include deleted clips in the
        # max() so a later clip never reuses a previous filename after the
        # operator deletes one bad take.
        with self._lock:
            return max((c.seq for c in self._clips), default=0) + 1

    def write_wavs(
        self,
        *,
        member: str | None,
        session_id: str | None,
        seq: int,
        condition: str,
        pcm_per_leg: dict[str, bytes],
    ) -> dict[str, str]:
        """Write one clip's non-empty legs; return leg → absolute WAV path."""
        files: dict[str, str] = {}
        condition_dir = CORPUS_DIR_BY_CONDITION[condition]
        for leg, pcm in pcm_per_leg.items():
            if not pcm:
                continue
            filename = f"enroll_{member}_{session_id}_{seq:03d}.aec-{leg}.wav"
            full_path = self._output_dir / f"aec_{leg}_{condition_dir}" / filename
            full_path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(full_path, pcm)
            files[leg] = str(full_path)
        return files

    def append(self, clip: ClipMetadata) -> None:
        with self._lock:
            self._clips.append(clip)

    def delete(self, clip_id: str) -> bool:
        """Unlink a live clip's WAVs and mark its record deleted.

        Returns False when no live clip has this id.
        """
        with self._lock:
            clip = next(
                (c for c in self._clips
                 if c.clip_id == clip_id and not c.deleted),
                None,
            )
            if clip is None:
                return False
            for path_str in clip.files.values():
                p = Path(path_str)
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning("failed to delete %s: %s", p, e)
            clip.deleted = True
        return True

    def list_clips(self, include_deleted: bool = False) -> list[ClipMetadata]:
        with self._lock:
            return [
                c for c in self._clips
                if include_deleted or not c.deleted
            ]

    def clip(self, clip_id: str) -> ClipMetadata | None:
        with self._lock:
            return next(
                (c for c in self._clips if c.clip_id == clip_id),
                None,
            )
