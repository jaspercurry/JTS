# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Optional AEC bridge legs built only for the wake-corpus recorder.

`jasper-voice` never asks for these: the reference leg, the XVF raw0
WebRTC/DTLN lanes, the USB raw/WebRTC/DTLN lanes, the AEC3 delay-sweep variants
and the DTLN observation leg all sit behind `JASPER_AEC_CORPUS_*` /
`JASPER_AEC_DTLN_ENABLED` flags that only `jasper.wake_corpus` sets. Ports come
from `jasper.wake_legs` via `BridgeConfig`; nothing here is on the production
wake path.

Imports run one way only: this module reads `jasper.cli.aec_bridge` at module
scope, so `aec_bridge` must keep its import of this module inside `_aec_loop`.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from queue import Queue
from typing import Any, Callable

from jasper.aec_sweep import (
    AEC3_SWEEP_ENV_FLAG,
    AEC3_SWEEP_SOURCE_USB,
    Aec3SweepVariant,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_CORPUS_OVERRIDES,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    USB_AEC3_SWEEP_BASELINE_OVERRIDES,
)
from jasper.log_event import log_event
from jasper.cli.aec_bridge import (
    BridgeConfig,
    _add_loop_emitter,
    _bridge_stats,
    env_bool,
    logger,
)
from jasper.cli.aec_bridge_engines import Aec3Engine, EngineSelector
from jasper.cli.aec_bridge_telemetry import LegEmitter


@dataclass(frozen=True, eq=False)
class SweepPath:
    """One configured AEC3 delay-sweep variant and its output leg."""

    variant: Aec3SweepVariant
    engine: Aec3Engine
    emitter: LegEmitter
    input_source: str


@dataclass(frozen=True)
class CorpusLanes:
    """Every optional lane built for one bridge configuration."""

    xvf_raw0_engine: Any | None
    xvf_raw0_webrtc_emitter: LegEmitter | None
    xvf_raw0_dtln_engine: Any | None
    xvf_raw0_dtln_emitter: LegEmitter | None
    ref_emitter: LegEmitter | None
    usb_raw_emitter: LegEmitter | None
    usb_webrtc_emitter: LegEmitter | None
    usb_engine: Any | None
    usb_dtln_engine: Any | None
    usb_dtln_emitter: LegEmitter | None
    aec3_sweep_paths: list[SweepPath]
    emit_aec3_sweep: Callable[[bytes, bytes], None]
    dtln_engine: Any | None
    dtln_emitter: LegEmitter | None


def build_corpus_lanes(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    *,
    select_engine: EngineSelector,
    xvf_raw0_webrtc_enabled: bool,
    xvf_raw0_dtln_enabled: bool,
    emit_ref: bool,
    production_chip_aec_enabled: bool,
    usb_raw_q: Queue | None,
) -> CorpusLanes:
    """Build every corpus-only lane and register its emitter in `emitters`.

    Lanes are built in `emitters` insertion order, which reaches the operator
    as the stats snapshot's `ports` map and as shutdown close order.
    """
    (
        xvf_raw0_engine,
        xvf_raw0_webrtc_emitter,
        xvf_raw0_dtln_engine,
        xvf_raw0_dtln_emitter,
    ) = _build_xvf_raw0_optional_paths(
        emitters,
        config,
        select_engine=select_engine,
        webrtc_enabled=xvf_raw0_webrtc_enabled,
        dtln_enabled=xvf_raw0_dtln_enabled,
    )
    ref_emitter = None
    if emit_ref:
        ref_emitter = _add_loop_emitter(emitters, config, "ref", config.out_port_ref)

    (
        usb_raw_emitter,
        usb_webrtc_emitter,
        usb_engine,
        usb_dtln_engine,
        usb_dtln_emitter,
    ) = _build_usb_optional_paths(
        emitters, config, select_engine=select_engine, usb_raw_q=usb_raw_q
    )
    aec3_sweep_paths, emit_aec3_sweep = _build_aec3_sweep_paths(
        emitters,
        config,
        select_engine=select_engine,
        production_chip_aec_enabled=production_chip_aec_enabled,
        usb_raw_q=usb_raw_q,
    )
    dtln_engine, dtln_emitter = _build_dtln_optional_path(
        emitters,
        config,
        production_chip_aec_enabled=production_chip_aec_enabled,
    )
    return CorpusLanes(
        xvf_raw0_engine=xvf_raw0_engine,
        xvf_raw0_webrtc_emitter=xvf_raw0_webrtc_emitter,
        xvf_raw0_dtln_engine=xvf_raw0_dtln_engine,
        xvf_raw0_dtln_emitter=xvf_raw0_dtln_emitter,
        ref_emitter=ref_emitter,
        usb_raw_emitter=usb_raw_emitter,
        usb_webrtc_emitter=usb_webrtc_emitter,
        usb_engine=usb_engine,
        usb_dtln_engine=usb_dtln_engine,
        usb_dtln_emitter=usb_dtln_emitter,
        aec3_sweep_paths=aec3_sweep_paths,
        emit_aec3_sweep=emit_aec3_sweep,
        dtln_engine=dtln_engine,
        dtln_emitter=dtln_emitter,
    )


def _build_xvf_raw0_optional_paths(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    *,
    select_engine: EngineSelector,
    webrtc_enabled: bool,
    dtln_enabled: bool,
) -> tuple[Any | None, LegEmitter | None, Any | None, LegEmitter | None]:
    """Build the two optional XVF raw0 corpus-processing legs."""
    xvf_raw0_engine = None
    xvf_raw0_webrtc_emitter = None
    if webrtc_enabled:
        xvf_raw0_engine = select_engine(label="xvf_raw0_webrtc_aec3")
        xvf_raw0_webrtc_emitter = _add_loop_emitter(
            emitters,
            config,
            "xvf_raw0_webrtc_aec3",
            config.out_port_xvf_raw0_webrtc_aec3,
        )

    xvf_raw0_dtln_engine = None
    xvf_raw0_dtln_emitter = None
    if dtln_enabled:
        try:
            from jasper.aec_engines import dtln_models
            from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
            xvf_raw0_dtln_size = int(os.environ.get(
                "JASPER_AEC_XVF_RAW0_DTLN_SIZE",
                os.environ.get(
                    "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
                ),
            ))
            xvf_raw0_dtln_engine = DTLNEngine(
                model_dir=default_model_dir(), model_size=xvf_raw0_dtln_size,
            )
            xvf_raw0_dtln_emitter = _add_loop_emitter(
                emitters,
                config,
                "xvf_raw0_dtln",
                config.out_port_xvf_raw0_dtln,
            )
            logger.info(
                "XVF raw0 DTLN-aec corpus output enabled: size=%d, udp out=%s:%d",
                xvf_raw0_dtln_size,
                config.out_host,
                config.out_port_xvf_raw0_dtln,
            )
        except (FileNotFoundError, ImportError) as e:
            logger.warning(
                "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED set but XVF raw0 "
                "DTLN couldn't load: %s. Continuing without xvf_raw0_dtln.",
                e,
            )
    return (
        xvf_raw0_engine,
        xvf_raw0_webrtc_emitter,
        xvf_raw0_dtln_engine,
        xvf_raw0_dtln_emitter,
    )


def _build_usb_optional_paths(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    *,
    select_engine: EngineSelector,
    usb_raw_q: Queue | None,
) -> tuple[
    LegEmitter | None,
    LegEmitter | None,
    Any | None,
    Any | None,
    LegEmitter | None,
]:
    """Build optional USB raw, WebRTC, and DTLN corpus legs."""
    usb_raw_emitter = None
    usb_webrtc_emitter = None
    usb_engine = None
    usb_dtln_engine = None
    usb_dtln_emitter = None
    if usb_raw_q is not None:
        usb_raw_emitter = _add_loop_emitter(
            emitters, config, "usb_raw", config.out_port_usb_raw
        )
        usb_webrtc_emitter = _add_loop_emitter(
            emitters,
            config,
            "usb_webrtc",
            config.out_port_usb_webrtc,
        )
        usb_webrtc_overrides = USB_AEC3_CORPUS_OVERRIDES
        usb_webrtc_label = "usb_webrtc/aec3_edge_combo_80"
        usb_webrtc_display_label = USB_AEC3_CORPUS_LABEL
        if (
            env_bool(AEC3_SWEEP_ENV_FLAG, "0")
            and config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB
        ):
            # In USB AEC3 sweep mode the normal usb_webrtc leg becomes the
            # 40 ms member of the delay sweep; the three variant slots carry
            # the same edge-combo tuning at longer stream-delay hints. Four
            # same-utterance USB AEC3 candidates, no extra sockets.
            usb_webrtc_overrides = USB_AEC3_SWEEP_BASELINE_OVERRIDES
            usb_webrtc_label = "usb_webrtc/aec3_sweep_delay_40"
            usb_webrtc_display_label = USB_AEC3_SWEEP_BASELINE_LABEL
        usb_engine = select_engine(
            overrides=usb_webrtc_overrides,
            label=usb_webrtc_label,
        )
        logger.info(
            "USB corpus outputs enabled: raw=%s:%d webrtc=%s:%d label=%s",
            config.out_host,
            config.out_port_usb_raw,
            config.out_host,
            config.out_port_usb_webrtc,
            usb_webrtc_display_label,
        )
        if env_bool("JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "0"):
            try:
                from jasper.aec_engines import dtln_models
                from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
                usb_dtln_size = int(os.environ.get(
                    "JASPER_AEC_USB_DTLN_SIZE",
                    os.environ.get(
                        "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
                    ),
                ))
                usb_dtln_engine = DTLNEngine(
                    model_dir=default_model_dir(), model_size=usb_dtln_size,
                )
                usb_dtln_emitter = _add_loop_emitter(
                    emitters,
                    config,
                    "usb_dtln",
                    config.out_port_usb_dtln,
                )
                logger.info(
                    "USB DTLN-aec corpus output enabled: size=%d, udp out=%s:%d",
                    usb_dtln_size, config.out_host, config.out_port_usb_dtln,
                )
            except (FileNotFoundError, ImportError) as e:
                logger.warning(
                    "JASPER_AEC_CORPUS_USB_DTLN_ENABLED set but USB DTLN "
                    "couldn't load: %s. Continuing without usb_dtln.",
                    e,
                )

    return (
        usb_raw_emitter,
        usb_webrtc_emitter,
        usb_engine,
        usb_dtln_engine,
        usb_dtln_emitter,
    )


def _build_aec3_sweep_paths(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    *,
    select_engine: EngineSelector,
    production_chip_aec_enabled: bool,
    usb_raw_q: Queue | None,
) -> tuple[list[SweepPath], Callable[[bytes, bytes], None]]:
    """Build configured sweep variants and their per-frame dispatcher."""
    aec3_sweep_paths: list[SweepPath] = []
    if (not production_chip_aec_enabled) and env_bool(AEC3_SWEEP_ENV_FLAG, "0"):
        if (
            config.aec3_sweep_input_source == AEC3_SWEEP_SOURCE_USB
            and usb_raw_q is None
        ):
            logger.warning(
                "AEC3 sweep requested with input_source=usb but USB corpus "
                "capture is disabled; continuing without sweep variants",
            )
        else:
            for variant in config.aec3_sweep_variants:
                try:
                    variant_engine = select_engine(
                        overrides=variant.env_overrides,
                        label=(
                            f"aec3_sweep/{config.aec3_sweep_input_source}/"
                            f"{variant.leg}"
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "AEC3 sweep variant %s couldn't load: %s. "
                        "Continuing without this variant.",
                        variant.leg, e,
                    )
                    continue
                variant_port = config.out_port_aec3_sweep[variant.leg]
                variant_emitter = _add_loop_emitter(
                    emitters, config, variant.leg, variant_port
                )
                aec3_sweep_paths.append(SweepPath(
                    variant=variant,
                    engine=variant_engine,
                    emitter=variant_emitter,
                    input_source=config.aec3_sweep_input_source,
                ))
                logger.info(
                    "AEC3 corpus sweep variant enabled: leg=%s label=%s "
                    "input_source=%s udp out=%s:%d overrides=%s",
                    variant.leg,
                    variant.label,
                    config.aec3_sweep_input_source,
                    config.out_host,
                    variant_port,
                    variant.env_overrides,
                )

    def emit_aec3_sweep(input_bytes: bytes, ref_bytes: bytes) -> None:
        for path in list(aec3_sweep_paths):
            try:
                variant_clean = path.engine.process(input_bytes, ref_bytes)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "AEC3 sweep variant %s process() crashed; "
                    "disabling this path: %s",
                    path.variant.leg, e,
                )
                try:
                    path.engine.close()
                except Exception:  # noqa: BLE001
                    pass
                path.emitter.close()
                emitters.pop(path.variant.leg, None)
                aec3_sweep_paths.remove(path)
                continue
            path.emitter.emit(variant_clean)

    return aec3_sweep_paths, emit_aec3_sweep


def _build_dtln_optional_path(
    emitters: dict[str, LegEmitter],
    config: BridgeConfig,
    *,
    production_chip_aec_enabled: bool,
) -> tuple[Any | None, LegEmitter | None]:
    """Build the optional DTLN observation leg without gating primary AEC3."""
    dtln_engine = None
    dtln_emitter = None
    dtln_wanted = (
        not production_chip_aec_enabled
    ) and env_bool("JASPER_AEC_DTLN_ENABLED", "0")
    _bridge_stats.set_leg_engine("dtln", enabled=dtln_wanted, loaded=False)
    if dtln_wanted:
        try:
            from jasper.aec_engines import dtln_models
            from jasper.aec_engines.dtln import DTLNEngine, default_model_dir
            dtln_size = int(os.environ.get(
                "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE),
            ))
            dtln_engine = DTLNEngine(
                model_dir=default_model_dir(), model_size=dtln_size,
            )
            dtln_emitter = _add_loop_emitter(
                emitters, config, "dtln", config.out_port_dtln
            )
            _bridge_stats.set_leg_engine("dtln", enabled=True, loaded=True)
            logger.info(
                "DTLN-aec engine enabled: size=%d, udp out=%s:%d",
                dtln_size, config.out_host, config.out_port_dtln,
            )
        except Exception as e:  # noqa: BLE001
            # DTLN is an optional tertiary leg: bad config, malformed ONNX or
            # any other initialization failure must not crash-loop the
            # healthy primary AEC3 bridge into systemd's reboot ladder.
            if dtln_emitter is not None:
                with suppress(Exception):
                    dtln_emitter.close()
                emitters.pop("dtln", None)
                dtln_emitter = None
            if dtln_engine is not None:
                with suppress(Exception):
                    dtln_engine.close()
                dtln_engine = None
            # Degraded state lands in the stats snapshot so the doctor can
            # flag it after this line ages out of the journal window: voice
            # otherwise keeps listening on a permanently unfed leg with no
            # surface anywhere.
            _bridge_stats.set_leg_engine(
                "dtln", enabled=True, loaded=False, error=str(e),
            )
            log_event(
                logger,
                "aec_bridge.leg_degraded",
                leg="dtln",
                phase="initialize",
                action="continue_aec3",
                error_type=type(e).__name__,
                error=str(e),
                note=(
                    f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {e}. "
                    "Continuing with AEC3 only."
                ),
                level=logging.WARNING,
            )

    return dtln_engine, dtln_emitter
