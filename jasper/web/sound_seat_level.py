# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Household web surface for ``jasper-seat-level``'s closed-loop SPL leveling.

Wraps the CLI as a subprocess: the ramp, refusal vocabulary, and the
session-volume latch's crash-safe restore
(:mod:`jasper.active_speaker.session_volume_plan`) all stay owned there —
this module only starts one pass, reports its state, and stops it. Stopping
sends SIGINT, the one signal the CLI wires to its own graceful cancellation
(``_stoppable`` in :mod:`jasper.cli.seat_level`): it cuts the stimulus and
restores the household volume through the latch before the process exits. A
plain SIGTERM/SIGKILL would orphan the stimulus player mid-tone, so
:meth:`_SeatLevelSession.stop` only escalates to that after SIGINT gets no
response within :data:`SEAT_LEVEL_STOP_TIMEOUT_S`.

Owner ruling (#2761, 2026-08-20): the operator's SPL target is not
second-guessed here — the CLI's own physics-derived ceiling
(``unsegmented_stimulus_ceiling_db``) and the profile's
``max_commissioning_level_db_spl`` are the only bounds. This module adds no
target ceiling of its own.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import signal
import subprocess
import threading
from typing import Any, Mapping

from jasper.active_speaker.seat_level_reference import DEFAULT_TARGET_DB_SPL
from jasper.audio_measurement import household_mic
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

SEAT_LEVEL_CLI = "jasper-seat-level"

#: Bound on graceful SIGINT shutdown before escalating to SIGTERM/SIGKILL —
#: the CLI's own teardown (stop the stimulus, restore the fader, write
#: nothing) is a few CamillaDSP round trips, not a long operation.
SEAT_LEVEL_STOP_TIMEOUT_S = 5.0


#: status()'s written sentence for a pass that had to be force-stopped (SIGINT
#: got no response, so the CLI's own fade-out/latch-restore teardown never
#: ran) -- never the raw stderr_tail, which stays in the
#: sound.seat_level_stop_timeout log event only.
_FORCE_STOPPED_DETAIL = (
    "The leveling pass had to be force-stopped and the household volume may "
    "still be at the measurement level. Check the volume floor section on "
    "this page and adjust it if needed."
)


def _flatten_cli_document(
    body: dict[str, Any] | None,
) -> tuple[str | None, str, float | None]:
    """One ``jasper-seat-level`` stdout document, flattened for the card.

    A refusal nests its sentence and telemetry two levels down: ``main()``
    passes ``{**SeatLevelResult.to_dict(), "detail": <sentence>}`` as the
    ``detail`` argument to :func:`jasper.cli._refusal.failed`, which wraps
    that under the outer document's own ``detail`` key (``jasper/cli/
    seat_level.py``, ``jasper/cli/_refusal.py``) -- so the sentence lives at
    ``body["detail"]["detail"]``, not ``body["detail"]``. A converged run's
    document has no such wrapping; its ``detail`` is already the sentence.
    """
    if not isinstance(body, dict):
        return None, "The pass ended without a readable result.", None
    reason = body.get("reason")
    nested = body.get("detail")
    if isinstance(nested, Mapping):
        detail = nested.get("detail")
        measured = nested.get("measured_db_spl")
    else:
        detail = nested
        measured = body.get("measured_db_spl")
    if not isinstance(detail, str):
        detail = "The pass ended without a readable result."
    if not isinstance(measured, (int, float)):
        measured = None
    return reason, detail, measured


class _SeatLevelSession:
    """One in-flight (or just-finished) leveling pass this process's session
    lock allows at a time. The cross-process guard against two passes
    running at once -- including one started directly from the CLI -- is
    ``jasper-seat-level``'s own ``measurement_window`` lease
    (:mod:`jasper.measurement_window`), not this lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._target_db_spl: float | None = None
        self._result: dict[str, Any] | None = None
        self._graceful = True

    def _reap(self, proc: subprocess.Popen[str]) -> None:
        stdout, stderr = proc.communicate()
        try:
            payload = json.loads(stdout) if stdout else None
        except ValueError:
            payload = None
        stderr_tail = (stderr or "").strip()[-500:]
        force_stopped = False
        with self._lock:
            if self._process is proc:
                self._process = None
                force_stopped = not self._graceful
                self._result = {
                    "converged": proc.returncode == 0,
                    "payload": payload if isinstance(payload, dict) else None,
                    "stderr_tail": stderr_tail,
                    "force_stopped": force_stopped,
                }
        if force_stopped:
            log_event(
                logger,
                "sound.seat_level_stop_timeout",
                level=logging.WARNING,
                stderr_tail=stderr_tail,
            )
        log_event(
            logger,
            "sound.seat_level_finished",
            converged=proc.returncode == 0,
            returncode=proc.returncode,
        )

    def start(self, *, target_db_spl: float, calibration_file: str) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {
                    "status": "refused",
                    "reason": "already_running",
                    "detail": "A seat-level pass is already running. Stop it first.",
                }
            cli = shutil.which(SEAT_LEVEL_CLI) or SEAT_LEVEL_CLI
            proc = subprocess.Popen(
                [
                    cli,
                    "--calibration-file",
                    calibration_file,
                    "--target-db-spl",
                    str(target_db_spl),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._process = proc
            self._target_db_spl = target_db_spl
            self._result = None
            self._graceful = True
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        log_event(logger, "sound.seat_level_start", target_db_spl=target_db_spl)
        return {"status": "started", "target_db_spl": target_db_spl}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            proc = self._process
        if proc is None or proc.poll() is not None:
            return {"status": "idle"}
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=SEAT_LEVEL_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with self._lock:
                self._graceful = False
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass
        log_event(logger, "sound.seat_level_stop")
        return {"status": "stopping"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._process
            target = self._target_db_spl
            result = self._result
        if proc is not None and proc.poll() is None:
            return {"state": "running", "target_db_spl": target}
        if result is not None:
            if result["force_stopped"]:
                return {
                    "state": "refused",
                    "target_db_spl": target,
                    "reason": "force_stopped",
                    "detail": _FORCE_STOPPED_DETAIL,
                    "measured_db_spl": None,
                    "force_stopped": True,
                }
            state = "converged" if result["converged"] else "refused"
            reason, detail, measured = _flatten_cli_document(result["payload"])
            return {
                "state": state,
                "target_db_spl": target,
                "reason": reason,
                "detail": detail,
                "measured_db_spl": measured,
            }
        return {"state": "idle", "target_db_spl": None}


SEAT_LEVEL_SESSION = _SeatLevelSession()


def household_mic_summary() -> dict[str, Any]:
    """The household's remembered calibrated mic, or unavailable.

    Reuses :func:`household_mic.resolved_household_mic` — the same resolver
    the crossover capture's default-mic hint uses — so the operator never
    re-types a serial or calibration file for a leveling pass (#2761: "no
    new picker").
    """
    found = household_mic.resolved_household_mic()
    if found is None:
        return {"available": False}
    record, resolved = found
    return {
        "available": True,
        "label": record.label,
        "serial_display": record.serial_display or "",
        "calibration_file": resolved.raw_path,
    }


def seat_level_status_payload() -> dict[str, Any]:
    payload = SEAT_LEVEL_SESSION.status()
    payload["mic"] = household_mic_summary()
    payload["default_target_db_spl"] = DEFAULT_TARGET_DB_SPL
    return payload


def seat_level_start_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    raw_target = body.get("target_db_spl")
    if raw_target is None:
        return {
            "status": "refused",
            "reason": "invalid_target",
            "detail": "target_db_spl must be a number",
        }
    try:
        target = float(raw_target)
    except (TypeError, ValueError):
        return {
            "status": "refused",
            "reason": "invalid_target",
            "detail": "target_db_spl must be a number",
        }
    if not math.isfinite(target):
        return {
            "status": "refused",
            "reason": "invalid_target",
            "detail": "target_db_spl must be finite",
        }
    mic = household_mic_summary()
    if not mic["available"]:
        return {
            "status": "refused",
            "reason": "mic_calibration_unavailable",
            "detail": (
                "No calibrated measurement mic is set up for this household "
                "yet. Run the mic calibration step first."
            ),
        }
    return SEAT_LEVEL_SESSION.start(
        target_db_spl=target, calibration_file=mic["calibration_file"]
    )


def seat_level_stop_payload() -> dict[str, Any]:
    return SEAT_LEVEL_SESSION.stop()
