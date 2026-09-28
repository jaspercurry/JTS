# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read the live state used by audio-validation reports."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from . import audio_validation_artifacts as artifacts
from .audio_profile_state import (
    AEC_MODE_FILE_ENV,
    DEFAULT_AEC_MODE_PATH,
    MicProbe,
    probe_xvf_mic as _probe_xvf_mic,
)
from .platform import control_client as control
from .env_load import env_file_path, parse_env_file
from .log_event import log_event
from .systemd_probe import UNKNOWN as UNKNOWN_STATE, unit_states
from .output_hardware import published_dac_id
from .platform.status_socket import (
    OUTPUTD_STATUS_SOCKET,
    read_status_socket_or_none,
)


logger = logging.getLogger("jasper.audio_validation")

# Bound on the is-active probe in the validation report.
_SERVICE_PROBE_TIMEOUT_SEC = 2.0


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def read_mode_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(
        str(path or _env_path(AEC_MODE_FILE_ENV, DEFAULT_AEC_MODE_PATH))
    )


def read_system_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(str(path) if path else env_file_path())


def _mic_details(mic: MicProbe) -> dict[str, artifacts.JsonValue]:
    return {
        "id": "xvf3800" if mic.xvf_present else "unknown",
        "family": "xvf3800",
        "display_name": mic.display_name,
        "present": mic.xvf_present,
        "capture_channels": mic.capture_channels,
        "recommended_channels": mic.recommended_channels,
        "alsa_card_name": mic.alsa_card_name,
        "variant_id": mic.variant_id,
        "geometry": mic.geometry,
        "chip_beam_plan": mic.chip_beam_plan,
        "chip_aec_supported": mic.chip_aec_supported,
        "probe_error": mic.probe_error,
    }


def outputd_socket_path(system_env: Mapping[str, str]) -> Path:
    raw = (
        system_env.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or os.environ.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or OUTPUTD_STATUS_SOCKET
    )
    return Path(raw)


def query_outputd_status(
    socket_path: Path, timeout: float = 1.0
) -> dict[str, Any] | None:
    return read_status_socket_or_none(
        str(socket_path),
        timeout=timeout,
        event="audio_validation.outputd_status_unavailable",
    )


def service_state(unit: str) -> str:
    state = unit_states([unit], timeout=_SERVICE_PROBE_TIMEOUT_SEC)[unit]
    if state == UNKNOWN_STATE:
        log_event(
            logger,
            "audio_validation.service_probe_failed",
            unit=unit,
            level=logging.DEBUG,
        )
    return state


def read_voice_wake_legs(timeout: float = 1.0) -> set[str] | None:
    try:
        data = control.get_state(timeout=timeout)
    except (control.ControlError, ValueError) as e:
        log_event(
            logger,
            "audio_validation.voice_state_unavailable",
            error=str(e),
            level=logging.DEBUG,
        )
        return None
    voice = data.get("voice") if isinstance(data, dict) else None
    if not isinstance(voice, dict):
        return None
    legs = voice.get("wake_legs")
    if not isinstance(legs, list):
        return None
    return {str(leg) for leg in legs}


def _dac_details(
    system_env: Mapping[str, str],
    outputd_status: Mapping[str, Any] | None,
) -> dict[str, artifacts.JsonValue]:
    outputd_dac = (
        outputd_status.get("dac") if isinstance(outputd_status, dict) else None
    )
    dac_pcm = ""
    dac_card = ""
    sample_rate: artifacts.JsonValue = None
    if isinstance(outputd_dac, dict):
        dac_pcm = str(outputd_dac.get("pcm") or "")
        dac_card = str(outputd_dac.get("card") or "")
        raw_sample_rate = outputd_dac.get("sample_rate")
        if (
            isinstance(raw_sample_rate, (str, int, float, bool))
            or raw_sample_rate is None
        ):
            sample_rate = raw_sample_rate
    if not dac_pcm:
        dac_pcm = (
            system_env.get("JASPER_OUTPUTD_DAC_PCM")
            or os.environ.get("JASPER_OUTPUTD_DAC_PCM")
            or "outputd_dac"
        )
    if not dac_card:
        dac_card = (
            system_env.get("JASPER_AUDIO_DAC_CARD")
            or os.environ.get("JASPER_AUDIO_DAC_CARD")
            or ""
        )
    # The id is the reconciler's publication and nothing else — no process-env
    # fallback on purpose, unlike outputd's pcm/card/backend facts above.
    dac_id = published_dac_id(system_env)
    return {
        "id": dac_id,
        "pcm": dac_pcm,
        "card": dac_card,
        "backend": str(
            (outputd_status or {}).get("backend")
            or system_env.get("JASPER_OUTPUTD_BACKEND")
            or os.environ.get("JASPER_OUTPUTD_BACKEND")
            or "unknown"
        ),
        "sample_rate": sample_rate,
    }


def current_artifact_filter_kwargs(
    *,
    requested_profile: str | None = None,
    system_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
) -> dict[str, str | None]:
    """Build hardware-bound filters for status-surface artifact reads.

    Always include mic/dac identity, even when detection is unavailable.
    Passing ``unknown`` is intentional: it prevents a previous pass from a
    real mic/DAC from being accepted when the current hardware identity
    cannot be established.
    """

    env = dict(system_env) if system_env is not None else read_system_env()
    mic = _mic_details(mic_probe if mic_probe is not None else _probe_xvf_mic())
    dac = _dac_details(env, None)
    return {
        "requested_profile": requested_profile,
        "mic_id": str(mic.get("id") or "unknown"),
        "dac_id": str(dac.get("id") or "unknown"),
    }
