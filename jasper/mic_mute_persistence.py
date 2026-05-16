"""Persist + restore the user-driven mic mute flag across daemon
restarts.

Without this, every jasper-voice restart silently un-mutes the mic.
The daemon gets restarted often — deploys, web-wizard saves
(wake-word, voice provider, Gemini model, Spotify/Google creds), AEC
reconciler events, watchdog timeouts on a silent hang. So "I muted
the mic this morning" becomes "the mic is hot again by lunch" with
no user-visible event. Mute is a privacy promise; we keep it.

File format mirrors aec_mode.env / wake_model.env (env-var style):

    JASPER_MIC_MUTED=1

Written atomically (tempfile + rename) so a crash mid-write leaves
either the old value or the new one — never half a line. The atomic
rename also makes the file safe to read from a different process
(jasper-doctor) without holding a lock.

Failure mode is "treat as unmuted". A missing, unreadable, or
malformed file means the speaker listens — better than a corrupted
byte silently deafening the speaker until someone notices.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_PATH = "/var/lib/jasper/mic_mute.env"
_KEY = "JASPER_MIC_MUTED"


def read_mic_muted(path: str | os.PathLike) -> bool:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("mic mute persistence: read %s failed (%s)", p, e)
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != _KEY:
            continue
        v = value.strip().strip('"').strip("'")
        if v in ("1", "true", "True", "yes", "on"):
            return True
        if v in ("0", "false", "False", "no", "off", ""):
            return False
        logger.warning(
            "mic mute persistence: %s has unrecognised value %r — "
            "treating as unmuted",
            p, v,
        )
        return False
    return False


def write_mic_muted(path: str | os.PathLike, muted: bool) -> None:
    """Best-effort atomic write. Logs on failure but does not raise —
    losing the persistence write should not crash the mute toggle."""
    p = Path(path)
    body = f"{_KEY}={1 if muted else 0}\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".mic_mute.", suffix=".tmp", dir=str(p.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.warning(
            "mic mute persistence: write to %s failed (%s)", p, e,
        )
