# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The AEC bridge's config loading and startup resolution/validation.

`BridgeConfig.from_env` is the bridge's only env-reading surface: every
`JASPER_AEC_*` and `JASPER_USB_MIC_*` toggle main() and `_aec_loop` act on
resolves to a `BridgeConfig` field here, once, at startup. The two
device-presence checks main() runs before opening any capture device, and
the `ref_source` fallback that keeps a parked box's retired env value from
leaving jasper-voice deaf, sit behind the same surface.

Imports run one way only: nothing here reads `jasper.cli.aec_bridge`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import math
import os
from pathlib import Path

from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_XVF,
    Aec3SweepConfig,
    Aec3SweepConfigError,
    Aec3SweepVariant,
    current_aec3_sweep_source,
    load_aec3_sweep_config,
)
from jasper import wake_legs
from jasper.wake_corpus.capture_plan import (
    DAC_FINGERPRINT_ENV,
    EXPECTED_LEGS_ENV,
    MIC_FINGERPRINT_ENV,
    PLAN_ID_ENV,
)
from jasper.log_event import log_event
from jasper.cli.aec_bridge_telemetry import (
    BRIDGE_STATS_PATH,
    BRIDGE_STATS_PATH_ENV,
    logger,
)
from jasper.usb_mic import (
    INTENT_PATH as USB_MIC_INTENT_PATH,
    USB_HOST_MIC_UDP_PORT,
    USB_MIC_LEG_KEY,
    USB_MIC_PRIMARY_LEG,
    USB_MIC_RAW_XVF_LEG,
    usb_mic_enabled,
)
from ..mics import xvf3800 as _mic_profile

# `sounddevice` is imported inside the device validators, not here: the doctor
# reads the env keys below. tests/test_lazy_imports.py pins it.

# Output transport: UDP localhost. The bridge sends AEC'd mono int16 frames
# to `127.0.0.1:JASPER_AEC_UDP_PORT`; jasper-voice's `UdpMicCapture` binds
# the same port and receives.
OUT_HOST = "127.0.0.1"


def leg_default_port(token: str) -> int:
    return wake_legs.by_token(token).udp_port


OUTPUTD_REF_UDP_HOST = "127.0.0.1"
OUTPUTD_REF_UDP_PORT = 9891
# The bridge's only reference source. Software AEC3, chip-AEC, corpus,
# and diagnostics all consume outputd's final speaker monitor, so they
# all see the same reference contract.
REF_SOURCE = "outputd_udp"

# The env keys the reconciler writes for the above; this bridge is the
# only reader, so the value defaults and the key names live in one place.
OUTPUTD_REF_UDP_HOST_ENV = "JASPER_AEC_OUTPUTD_REF_UDP_HOST"
OUTPUTD_REF_UDP_PORT_ENV = "JASPER_AEC_OUTPUTD_REF_UDP_PORT"
REF_SOURCE_ENV = "JASPER_AEC_REF_SOURCE"
# Retired reference source: the summed snd-aloop tap, whose path and tap are
# both deleted. A box whose /etc/jasper/jasper.env still carries this value
# converges on the next `jasper-aec-reconcile` run, so the bridge warns and
# uses REF_SOURCE rather than refusing to start: a hard failure here would
# leave jasper-voice with an unfed UDP mic and no wake detection.
RETIRED_REF_SOURCE_ALSA = "alsa"
USB_MIC_DEVICE = "USB PnP Sound Device"
USB_MIC_RATE = 0
CAPTURE_LATENCY_MAX_SECONDS = 0.25


@dataclass(frozen=True)
class BridgeConfig:
    mic_device: str
    capture_latency: str
    out_host: str
    out_port: int
    out_port_raw: int
    out_port_dtln: int
    out_port_raw0: int
    out_port_ref: int
    out_port_usb_raw: int
    out_port_usb_webrtc: int
    out_port_usb_dtln: int
    out_port_chip_aec_150: int
    out_port_chip_aec_210: int
    emit_chip_aec_150: bool
    emit_chip_aec_210: bool
    out_port_xvf_raw0_webrtc_aec3: int
    out_port_xvf_raw0_dtln: int
    out_port_usb_host_mic: int
    emit_usb_host_mic: bool
    usb_mic_leg: str
    outputd_ref_udp_host: str
    outputd_ref_udp_port: int
    ref_source: str
    out_port_aec3_sweep: dict[str, int]
    usb_mic_device: str
    usb_mic_rate: int
    bridge_stats_path: Path
    aec3_sweep_config: Aec3SweepConfig
    aec3_sweep_variants: tuple[Aec3SweepVariant, ...]
    aec3_sweep_input_source: str
    wake_corpus_plan_id: str
    wake_corpus_expected_legs: tuple[str, ...]
    wake_corpus_mic_fingerprint: str
    wake_corpus_dac_fingerprint: str

    @classmethod
    def from_env(
        cls,
        *,
        log_sweep: bool = False,
        logger_: logging.Logger | None = None,
    ) -> "BridgeConfig":
        log = logger_ or logger
        sweep_config = load_aec3_sweep_config(logger=log if log_sweep else None)
        try:
            sweep_input_source = current_aec3_sweep_source()
        except Aec3SweepConfigError as e:
            if log_sweep:
                log_event(
                    log,
                    "aec3_sweep_source_invalid",
                    error=str(e),
                    fallback=AEC3_SWEEP_SOURCE_XVF,
                    level=logging.WARNING,
                )
            sweep_input_source = AEC3_SWEEP_SOURCE_XVF

        if log_sweep:
            log_event(
                log,
                "aec3_sweep_config_loaded",
                source=sweep_config.source,
                path=sweep_config.path,
                hash=sweep_config.config_hash,
                input_source=sweep_input_source,
                variants=",".join(variant.leg for variant in sweep_config.variants),
            )

        def _env_leg_port(env_var: str, token: str) -> int:
            return int(os.environ.get(env_var, str(leg_default_port(token))))

        corpus_chip_aec_enabled = env_bool(
            _mic_profile.CORPUS_CHIP_AEC_ENABLED_ENV, "0",
        )
        capture_latency = os.environ.get("JASPER_AEC_CAPTURE_LATENCY", "").strip()
        if capture_latency and capture_latency.lower() != "low":
            try:
                capture_latency_seconds = float(capture_latency)
            except ValueError:
                capture_latency_seconds = 0.0
            if (
                not math.isfinite(capture_latency_seconds)
                or capture_latency_seconds <= 0
                or capture_latency_seconds > CAPTURE_LATENCY_MAX_SECONDS
            ):
                log_event(
                    log,
                    "aec.capture_latency_invalid",
                    value=capture_latency,
                    fallback="default",
                    level=logging.WARNING,
                )
                capture_latency = ""

        return cls(
            mic_device=os.environ.get(
                _mic_profile.AEC_MIC_DEVICE_ENV,
                _mic_profile.alsa_card_name(),
            ),
            capture_latency=capture_latency.lower(),
            out_host=os.environ.get("JASPER_AEC_UDP_HOST", OUT_HOST),
            out_port=_env_leg_port("JASPER_AEC_UDP_PORT", "on"),
            out_port_raw=_env_leg_port("JASPER_AEC_UDP_PORT_RAW", "off"),
            out_port_dtln=_env_leg_port("JASPER_AEC_UDP_PORT_DTLN", "dtln"),
            out_port_raw0=_env_leg_port("JASPER_AEC_UDP_PORT_RAW0", "raw0"),
            out_port_ref=_env_leg_port("JASPER_AEC_UDP_PORT_REF", "ref"),
            out_port_usb_raw=_env_leg_port("JASPER_AEC_UDP_PORT_USB_RAW", "usb_raw"),
            out_port_usb_webrtc=_env_leg_port(
                "JASPER_AEC_UDP_PORT_USB_WEBRTC",
                "usb_webrtc",
            ),
            out_port_usb_dtln=_env_leg_port(
                "JASPER_AEC_UDP_PORT_USB_DTLN",
                "usb_dtln",
            ),
            out_port_chip_aec_150=_env_leg_port(
                "JASPER_AEC_UDP_PORT_CHIP_AEC_150",
                "chip_aec_150",
            ),
            out_port_chip_aec_210=_env_leg_port(
                "JASPER_AEC_UDP_PORT_CHIP_AEC_210",
                "chip_aec_210",
            ),
            emit_chip_aec_150=(
                corpus_chip_aec_enabled
                or bool(
                    os.environ.get(
                        "JASPER_MIC_DEVICE_CHIP_AEC_150", "",
                    ).strip()
                )
            ),
            emit_chip_aec_210=(
                corpus_chip_aec_enabled
                or bool(
                    os.environ.get(
                        "JASPER_MIC_DEVICE_CHIP_AEC_210", "",
                    ).strip()
                )
            ),
            out_port_xvf_raw0_webrtc_aec3=_env_leg_port(
                "JASPER_AEC_UDP_PORT_XVF_RAW0_WEBRTC_AEC3",
                "xvf_raw0_webrtc_aec3",
            ),
            out_port_xvf_raw0_dtln=_env_leg_port(
                "JASPER_AEC_UDP_PORT_XVF_RAW0_DTLN",
                "xvf_raw0_dtln",
            ),
            # Product wiring, not an operator knob: the relay owns the paired
            # listener constant and accessories are regression-guarded from it.
            out_port_usb_host_mic=USB_HOST_MIC_UDP_PORT,
            emit_usb_host_mic=usb_mic_enabled(
                os.environ.get("JASPER_USB_MIC_INTENT_PATH", USB_MIC_INTENT_PATH)
            ),
            usb_mic_leg=(
                os.environ.get(USB_MIC_LEG_KEY, USB_MIC_PRIMARY_LEG).strip()
                or USB_MIC_PRIMARY_LEG
            ),
            outputd_ref_udp_host=os.environ.get(
                OUTPUTD_REF_UDP_HOST_ENV,
                OUTPUTD_REF_UDP_HOST,
            ),
            outputd_ref_udp_port=int(
                os.environ.get(
                    OUTPUTD_REF_UDP_PORT_ENV,
                    str(OUTPUTD_REF_UDP_PORT),
                )
            ),
            ref_source=os.environ.get(
                REF_SOURCE_ENV,
                REF_SOURCE,
            ).strip().lower(),
            out_port_aec3_sweep={
                variant.leg: variant.default_port
                for variant in sweep_config.variants
            },
            usb_mic_device=os.environ.get(
                "JASPER_AEC_USB_MIC_DEVICE",
                USB_MIC_DEVICE,
            ),
            usb_mic_rate=int(float(os.environ.get(
                "JASPER_AEC_USB_MIC_RATE",
                str(USB_MIC_RATE),
            ))),
            bridge_stats_path=Path(os.environ.get(
                BRIDGE_STATS_PATH_ENV,
                str(BRIDGE_STATS_PATH),
            )),
            aec3_sweep_config=sweep_config,
            aec3_sweep_variants=sweep_config.variants,
            aec3_sweep_input_source=sweep_input_source,
            wake_corpus_plan_id=os.environ.get(PLAN_ID_ENV, "").strip(),
            wake_corpus_expected_legs=tuple(
                leg.strip()
                for leg in os.environ.get(EXPECTED_LEGS_ENV, "").split(",")
                if leg.strip()
            ),
            wake_corpus_mic_fingerprint=os.environ.get(
                MIC_FINGERPRINT_ENV, "",
            ).strip(),
            wake_corpus_dac_fingerprint=os.environ.get(
                DAC_FINGERPRINT_ENV, "",
            ).strip(),
        )


class MicDeviceUnavailable(RuntimeError):
    """The configured PortAudio mic device is not currently present."""


class UsbMicUnavailable(RuntimeError):
    """The configured corpus USB mic device is not currently present."""


class UnsupportedReferenceSource(RuntimeError):
    """JASPER_AEC_REF_SOURCE names a source this bridge cannot read."""


def env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _chip_beam_plan() -> _mic_profile.ChipBeamPlan | None:
    return _mic_profile.chip_beam_plan_from_env(os.environ)


def _chip_aec_primary_leg(
    plan: _mic_profile.ChipBeamPlan | None,
) -> str:
    allowed = set(plan.leg_tokens if plan else ("chip_aec_150", "chip_aec_210"))
    fallback = next(iter(plan.leg_tokens), "chip_aec_150") if plan else "chip_aec_150"
    value = os.environ.get(
        _mic_profile.CHIP_AEC_PRIMARY_LEG_ENV, fallback,
    ).strip()
    if value in allowed:
        return value
    log_event(
        logger,
        "chip_aec_primary_invalid",
        value=repr(value),
        fallback=fallback,
        level=logging.WARNING,
    )
    return fallback


def resolve_usb_mic_source(
    requested: str,
    *,
    plan: _mic_profile.ChipBeamPlan | None,
    production_chip_aec_enabled: bool,
    chip_aec_primary_leg: str,
) -> dict[str, object]:
    """Resolve the configured selector to the physical stream being emitted."""

    allowed = {USB_MIC_PRIMARY_LEG, *(plan.leg_tokens if plan else ())}
    if plan is not None:
        allowed.add(USB_MIC_RAW_XVF_LEG)
    selection = requested if requested in allowed else USB_MIC_PRIMARY_LEG
    if selection != requested:
        log_event(
            logger,
            "usb_mic.leg_invalid",
            value=repr(requested),
            fallback=USB_MIC_PRIMARY_LEG,
            beam_plan=plan.plan_id if plan else "none",
            level=logging.WARNING,
        )
    if selection == USB_MIC_RAW_XVF_LEG:
        return {
            "selection": selection,
            "mode": "raw",
            "leg": USB_MIC_RAW_XVF_LEG,
            "fallback_active": False,
        }
    if not production_chip_aec_enabled:
        fallback_active = selection != USB_MIC_PRIMARY_LEG
        if fallback_active:
            log_event(
                logger,
                "usb_mic.leg_unavailable",
                leg=selection,
                fallback="clean",
                mode="software_aec3",
                level=logging.WARNING,
            )
        return {
            "selection": selection,
            "mode": "software_aec3",
            "leg": "clean",
            "fallback_active": fallback_active,
        }
    return {
        "selection": selection,
        "mode": "chip_aec",
        "leg": (
            chip_aec_primary_leg
            if selection == USB_MIC_PRIMARY_LEG
            else selection
        ),
        "fallback_active": False,
    }


def resolved_reference_source(config: BridgeConfig) -> BridgeConfig:
    """Return `config` with a supported `ref_source`, or reject it.

    `RETIRED_REF_SOURCE_ALSA` is converged, not rejected: a parked box can
    still carry it on disk, and refusing to start would leave jasper-voice
    with an unfed UDP mic. Anything else is a typo or a source this bridge
    genuinely cannot read, and stays a hard failure.

    Call this before anything reads `config.ref_source` — the bridge-stats
    snapshot publishes it as runtime provenance that `jasper-doctor` trusts,
    so the retired spelling must never reach it.
    """
    if config.ref_source == REF_SOURCE:
        return config
    if config.ref_source == RETIRED_REF_SOURCE_ALSA:
        log_event(
            logger,
            "aec_ref_source_retired",
            level=logging.WARNING,
            retired=config.ref_source,
            using=REF_SOURCE,
            detail=(
                "the ALSA reference fallback is gone; run "
                "`sudo systemctl start jasper-aec-reconcile` to converge "
                "/etc/jasper/jasper.env"
            ),
        )
        return replace(config, ref_source=REF_SOURCE)
    raise UnsupportedReferenceSource(
        f"unsupported JASPER_AEC_REF_SOURCE={config.ref_source!r} "
        f"(expected {REF_SOURCE!r})"
    )


def validate_mic_device(config: BridgeConfig | None = None) -> None:
    """Fail before opening the far-end reference if the mic is absent.

    Ordering matters: missing hardware must fail before the reference thread
    and its UDP socket start.
    """
    import sounddevice as sd  # Pi-side dep, lazy — see module top.

    config = config or BridgeConfig.from_env()
    try:
        sd.query_devices(config.mic_device, "input")
    except Exception as e:  # noqa: BLE001
        raise MicDeviceUnavailable(
            f"mic device {config.mic_device!r} unavailable: {e}"
        ) from e


def validate_usb_mic_device(config: BridgeConfig | None = None) -> None:
    """Fail fast when corpus USB capture is explicitly enabled but absent."""
    import sounddevice as sd  # Pi-side dep, lazy — see module top.

    config = config or BridgeConfig.from_env()
    try:
        sd.query_devices(config.usb_mic_device, "input")
    except Exception as e:  # noqa: BLE001
        raise UsbMicUnavailable(
            f"USB corpus mic device {config.usb_mic_device!r} unavailable: {e}"
        ) from e
