# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The household's remembered measurement microphone (Wave-2 persistence).

Before this module, nothing about the measurement mic persisted across
correction sessions:

* the phone relay's "setup" (mic model + serial/upload calibration) is
  validated against a ``setup_binding_id`` that is a per-run
  ``session_id``/``context_id`` (see ``_validated_relay_setup_binding`` and
  ``_run_relay_level_match`` in ``jasper/web/correction_setup.py``), minted
  fresh on every run, so a phone-side "remembered setup" can never validate
  against a NEW session's binding; and
* an uploaded calibration is stored via
  ``store_calibration(provider="manual_upload", serial=None)`` — with no
  serial, so the vendor-lookup cache (``find_stored_calibration``, keyed by
  ``serial_hash`` + model + orientation) can never reach it again.

Every session made the household re-select a mic model and re-supply the
calibration (re-type a serial or re-upload a file) from scratch.

This module owns exactly ONE durable JSON record —
``/var/lib/jasper/correction/household_mic.json`` — recording the mic/
calibration that most recently succeeded. It is written after every
SUCCESSFUL calibration establishment (a vendor fetch or an accepted upload),
from both the phone-relay flow (room + crossover share
``_relay_calibration_from_setup`` in ``jasper/web/correction_setup.py``) and
the local/laptop flow (``_handle_calibration_fetch`` /
``_handle_calibration_upload`` in the same module). Downstream consumers —
the capture-spec ``default_setup`` prefill hint
(``jasper/capture_relay/spec.py``) and the room wizard's server-rendered mic
selection — read it so the household does not re-enter what it already told
JTS. A session that establishes a DIFFERENT mic is never blocked; the new
success simply replaces the record (see ``correction.household_mic_replaced``
in ``jasper/web/correction_setup.py``).

No secrets land in the record: ``serial_hash`` is the same one-way hash the
calibration record itself carries, and ``serial_display`` is at most the
raw serial's last 4 characters, purely for the UI. The full serial is never
persisted here (or anywhere else in the calibration registry — see
``jasper/audio_measurement/calibration.py``).

The phone page's one-tap "Using {label} · {serial_display} — one tap to
confirm" screen (``capture-page/js/main.js``'s ``renderCalibrationConfirm``,
2026-07 Wave-2 batch) reads the capture spec's ``default_setup`` field this
module feeds (``jasper/capture_relay/spec.py``'s ``_default_setup_calibration_
for_spec``) and renders it — the read path is live. Confirm does not yet
SUBMIT a resolvable setup, though: ``_relay_calibration_from_setup``
(``jasper/web/correction_setup.py``) has no code path that accepts a bare
``calibration_id`` (it needs either the raw serial or the full calibration
text, neither of which this module persists). An older capture page ignores
unknown spec fields (verified against
``capture-page/js/transport-integrity.js``), so shipping ``default_setup``
is safe against any deployed page.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_HOUSEHOLD_MIC_PATH = Path("/var/lib/jasper/correction/household_mic.json")
SCHEMA_VERSION = 1

_REQUIRED_STRING_FIELDS = (
    "model_key",
    "label",
    "calibration_id",
    "file_sha256",
    "orientation",
    "provider",
)


@dataclass(frozen=True)
class HouseholdMicRecord:
    """The household's remembered measurement mic + calibration.

    ``calibration_id``/``file_sha256`` point back at the persisted
    calibration in ``jasper/audio_measurement/calibration.py``'s registry —
    ``file_sha256`` is the same content hash as that module's
    ``CalibrationRecord.file_sha256``, kept under the canonical name so the
    field greps identically across both layers.
    """

    model_key: str
    label: str
    calibration_id: str
    file_sha256: str
    orientation: str
    provider: str
    serial_hash: str | None = None
    serial_display: str | None = None
    updated_at: float = 0.0
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "model_key": self.model_key,
            "label": self.label,
            "serial_hash": self.serial_hash,
            "serial_display": self.serial_display,
            "calibration_id": self.calibration_id,
            "file_sha256": self.file_sha256,
            "orientation": self.orientation,
            "provider": self.provider,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HouseholdMicRecord:
        """Strictly parse a persisted record. Raises ``ValueError`` on any
        drift — callers that want fail-soft behavior use ``read_household_mic``,
        which catches this and returns ``None``."""
        if not isinstance(data, Mapping):
            raise ValueError("household mic record must be an object")
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported household mic schema: {data.get('schema')!r}"
            )
        for key in _REQUIRED_STRING_FIELDS:
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"household mic record missing/invalid {key!r}")
        serial_hash = data.get("serial_hash")
        serial_display = data.get("serial_display")
        updated_at = data.get("updated_at")
        return cls(
            model_key=str(data["model_key"]),
            label=str(data["label"]),
            calibration_id=str(data["calibration_id"]),
            file_sha256=str(data["file_sha256"]),
            orientation=str(data["orientation"]),
            provider=str(data["provider"]),
            serial_hash=str(serial_hash) if serial_hash else None,
            serial_display=str(serial_display) if serial_display else None,
            updated_at=(
                float(updated_at)
                if isinstance(updated_at, (int, float))
                and not isinstance(updated_at, bool)
                else 0.0
            ),
        )


def serial_display_from_raw(serial: str | None) -> str | None:
    """A privacy-safe last-4-characters display form of a raw serial.

    The raw serial is never persisted — the calibration record itself only
    ever stores a one-way hash of it (``jasper.audio_measurement.calibration
    .serial_hash``); this is the matching posture for the household record's
    UI label.
    """
    if not serial:
        return None
    stripped = re.sub(r"\s+", "", serial.strip())
    if not stripped:
        return None
    return stripped[-4:] if len(stripped) > 4 else stripped


def household_mic_from_calibration(
    record: Any,
    *,
    serial: str | None = None,
) -> HouseholdMicRecord:
    """Build a ``HouseholdMicRecord`` from a just-established
    ``CalibrationRecord`` (``jasper.audio_measurement.calibration``).

    ``serial`` is the RAW serial that produced a vendor lookup, when
    available (never available for an upload) — used only to derive
    ``serial_display``; it is not itself stored.
    """
    return HouseholdMicRecord(
        model_key=str(record.model),
        label=str(record.label),
        calibration_id=str(record.calibration_id),
        file_sha256=str(record.file_sha256),
        orientation=str(record.orientation),
        provider=str(record.provider),
        serial_hash=record.serial_hash,
        serial_display=serial_display_from_raw(serial),
        updated_at=time.time(),
    )


def read_household_mic(
    *, path: Path = DEFAULT_HOUSEHOLD_MIC_PATH,
) -> HouseholdMicRecord | None:
    """Read the durable household mic record.

    Fail-soft: a missing file returns ``None`` silently (the normal state
    for a fresh install or a household that has never measured). A PRESENT
    but malformed file also returns ``None`` — never raises — but logs one
    WARN event so an operator can notice a corrupted state file; the wizard
    degrades to "no remembered mic" rather than crashing.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_event(
            logger,
            "correction.household_mic_invalid",
            level=logging.WARNING,
            path=str(path),
            reason=type(exc).__name__,
        )
        return None
    try:
        data = json.loads(raw)
        return HouseholdMicRecord.from_dict(data)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        log_event(
            logger,
            "correction.household_mic_invalid",
            level=logging.WARNING,
            path=str(path),
            reason=type(exc).__name__,
        )
        return None


def write_household_mic(
    record: HouseholdMicRecord, *, path: Path = DEFAULT_HOUSEHOLD_MIC_PATH,
) -> None:
    """Persist the household mic record.

    Atomic tempfile+rename (``jasper.atomic_io.atomic_write_text``), mode
    0644 — the record carries no secrets (a hash plus an optional last-4
    serial display), so it is world-readable like the rest of
    ``/var/lib/jasper``. Raises ``OSError`` on failure; callers that want
    fail-soft behavior (a save must never block the calibration that
    triggered it) wrap this themselves.
    """
    atomic_write_text(
        path,
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        mode=0o644,
    )


def clear_household_mic(*, path: Path = DEFAULT_HOUSEHOLD_MIC_PATH) -> None:
    """Forget the household mic record, if one exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def resolve_household_mic_calibration(
    record: HouseholdMicRecord,
    *,
    root: Path | None = None,
) -> Any | None:
    """Resolve the household record's ``calibration_id`` back to the stored
    ``CalibrationRecord``.

    Tries the direct ID lookup first (works for both vendor- and
    content-derived upload IDs); falls back to a content-hash scan
    (``find_stored_calibration_by_content_hash`` — additive, upload-safe,
    see that function's docstring) if the ID lookup misses, e.g. a future
    ID-scheme change. Fail-soft: returns ``None`` rather than raising when
    neither resolves, so a stale/rotated calibration on disk degrades to
    "no prefill" instead of breaking the spec builder or the wizard render.
    """
    from jasper.audio_measurement.calibration import (
        DEFAULT_CALIBRATION_DIR,
        find_stored_calibration_by_content_hash,
        load_calibration_record,
    )

    calibration_root = root if root is not None else DEFAULT_CALIBRATION_DIR
    try:
        return load_calibration_record(record.calibration_id, root=calibration_root)
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        # OSError/ValueError: unreadable or malformed metadata file.
        # KeyError/TypeError: a corrupt file missing/mistyping a required
        # field (CalibrationRecord.from_dict indexes required keys
        # directly). Any of these means "can't use this ID" — fall through
        # to the content-hash lookup rather than raising into a caller that
        # documented this function as fail-soft.
        pass
    return find_stored_calibration_by_content_hash(
        file_sha256=record.file_sha256, root=calibration_root,
    )
