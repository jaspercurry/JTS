# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The household's remembered measurement microphone.

This module owns exactly ONE durable JSON record —
``/var/lib/jasper/correction/household_mic.json`` — recording the mic and
calibration that most recently succeeded. It is written at the two points a
calibration is ESTABLISHED (``_handle_calibration_fetch`` /
``_handle_calibration_upload`` in ``jasper/web/correction_setup.py``), and
nowhere else: without it every session made the household re-select a mic
model and re-supply the calibration (re-type a serial or re-upload a file)
from scratch. A session that establishes a DIFFERENT mic is never blocked;
the new success simply replaces the record (see
``correction.household_mic_replaced`` in ``jasper/web/correction_setup.py``).

No secrets land in the record: ``serial_hash`` is the same one-way hash the
calibration record itself carries, and ``serial_display`` is at most the
raw serial's last 4 characters, purely for the UI. The full serial is never
persisted here (or anywhere else in the calibration registry — see
``jasper/audio_measurement/calibration.py``).

The record reaches a measurement two ways, and both start here:

* the capture spec's OPTIONAL ``default_setup`` prefill hint
  (``jasper/active_speaker/crossover_v2/sweep_spec.py``, built by
  ``correction_setup._default_setup_calibration_for_spec``), whose
  ``resolvable`` flag is minted fresh at spec-build time — a second,
  independent :func:`resolve_household_mic_calibration` call rather than an
  inference from the hint existing — so a record whose calibration has gone
  missing from disk still ships the other hint fields without the marker;
* the capture's own ``setup.calibration`` REFERENCE, which the measurement
  source mints from that hint
  (``wired_capture.mint_wired_answer``) and
  :func:`resolve_setup_calibration` materializes back into a
  ``CalibrationRecord`` for the analysis.

The room wizard's server-rendered mic selection reads the record directly.
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


def _label_token(value: str) -> str:
    """Lowercase, punctuation-stripped comparison key for a device label.

    Shared normalization so ``"UMIK-2 (2752:002b)"`` and an alias of
    ``"umik-2"`` compare equal regardless of casing, hyphenation, or the
    parenthetical USB descriptor suffix the OS appends.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _wrong_mic(record: Any, device: Mapping[str, Any] | None) -> str | None:
    """Whether ``record``'s calibration is for a DIFFERENT mic than ``device``.

    Calibration is frequency-magnitude, so applying the wrong mic's curve
    corrupts a same-frequency measurement (the 2026-07-20 incident: a Dayton
    iMM-6C capture silently carried a remembered UMIK-2 calibration).

    Conservative and anchored, never a fuzzy label guess: it reuses the SAME
    curated ``SUPPORTED_MODELS``/``model_label_aliases`` registry the wizard's
    own label-based auto-inference uses. It trips only when the device's
    reported label positively matches a DIFFERENT registered model's aliases
    than the record's own. An unrecognized model on either side, or a label
    that matches nothing (or matches its OWN model), is not a mismatch —
    there is nothing concrete to contradict the remembered pairing with.
    """
    from jasper.audio_measurement.calibration import (
        SUPPORTED_MODELS,
        model_label_aliases,
    )

    model_key = str(getattr(record, "model", "") or "")
    if model_key not in SUPPORTED_MODELS:
        return None
    label = ""
    if isinstance(device, Mapping):
        label = str(device.get("label") or device.get("browser_label") or "")
    if not label:
        return None
    token = _label_token(label)
    if any(_label_token(alias) in token for alias in model_label_aliases(model_key)):
        return None  # matches its own model — fine
    for other_key, spec in SUPPORTED_MODELS.items():
        if other_key == model_key:
            continue
        if any(
            _label_token(alias) in token
            for alias in model_label_aliases(other_key)
        ):
            return (
                f'stored calibration is for "{SUPPORTED_MODELS[model_key]["label"]}" '
                f'but the captured device "{label}" looks like a '
                f'"{spec["label"]}"'
            )
    return None


def resolve_setup_calibration(
    setup: Mapping[str, Any] | None,
    *,
    device: Mapping[str, Any] | None = None,
    root: Path | None = None,
    path: Path = DEFAULT_HOUSEHOLD_MIC_PATH,
) -> Any | None:
    """Materialize a capture's ``setup.calibration`` reference as a record.

    The reference is minted from this module's own record
    (``wired_capture.mint_wired_answer``) and carries
    ``{"mode": "stored", "calibration_id", "model"}``; resolution turns it
    back into the stored ``CalibrationRecord`` the analysis applies. No
    reference, or one declaring no calibration, answers ``None`` — the
    analysis then runs annotated-uncalibrated rather than blocked.

    ``device`` is the capture's REALIZED input device. A reference is a
    passive carry-over the household never re-selects a mic for, so it is the
    one place a different physical mic can ride a stale pairing: a detected
    mismatch is journalled and answered ``None``, so no caller applies it.

    A reference naming a calibration that is no longer on disk raises
    ``ValueError`` — it is a stale claim about this household's own record,
    not a household that chose nothing.
    """
    calibration = setup.get("calibration") if isinstance(setup, Mapping) else None
    if not isinstance(calibration, Mapping):
        return None
    mode = str(calibration.get("mode") or "none").strip()
    if mode in ("", "none"):
        return None
    if mode != "stored":
        raise ValueError(f"unknown calibration mode: {mode}")
    calibration_id = str(calibration.get("calibration_id") or "").strip()
    if not calibration_id:
        raise ValueError("calibration_id is required for a stored calibration")
    # The reference echoes only calibration_id + model. Thread the household's
    # own file_sha256 through when the id still matches the current record, so
    # resolution gets the same content-hash fallback a full record carries.
    previous = read_household_mic(path=path)
    if previous is not None and previous.calibration_id != calibration_id:
        previous = None
    candidate = HouseholdMicRecord(
        model_key=str(calibration.get("model") or "").strip(),
        label=str(calibration.get("model") or "").strip() or "Stored microphone",
        calibration_id=calibration_id,
        file_sha256=previous.file_sha256 if previous is not None else "",
        orientation="unknown",
        provider="stored",
    )
    resolved = resolve_household_mic_calibration(candidate, root=root)
    if resolved is None:
        raise ValueError(
            "the remembered microphone calibration is no longer available; "
            "set it up again"
        )
    mismatch = _wrong_mic(resolved, device)
    if mismatch is None:
        return resolved
    device_label = (
        str(device.get("label") or device.get("browser_label") or "")
        if isinstance(device, Mapping) else ""
    )
    log_event(
        logger,
        "correction.calibration_device_identity_mismatch",
        level=logging.WARNING,
        stored_model=str(getattr(resolved, "model", "") or ""),
        device_label=device_label[:160],
        reason=mismatch,
    )
    return None
