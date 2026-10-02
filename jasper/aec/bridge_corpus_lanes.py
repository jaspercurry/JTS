# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Optional AEC bridge legs built and run only for the wake-corpus recorder.

`jasper-voice` never asks for these: the reference leg, the XVF raw0
WebRTC/DTLN lanes, the USB raw/WebRTC/DTLN lanes, the AEC3 delay-sweep variants
and the DTLN observation leg all sit behind `JASPER_AEC_CORPUS_*` /
`JASPER_AEC_DTLN_ENABLED` flags that only `jasper.wake_corpus` sets. Ports come
from `jasper.playback_state.wake_legs` via `BridgeConfig`; nothing here is on the production
wake path.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from queue import Empty, Queue
from typing import Any, Callable

from jasper.audio_routes.aec_sweep import (
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    Aec3SweepVariant,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_CORPUS_OVERRIDES,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    USB_AEC3_SWEEP_BASELINE_OVERRIDES,
)
from jasper.platform.log_event import log_event
from jasper.aec.bridge_config import BridgeConfig
from jasper.aec.bridge_engines import (
    Aec3Engine,
    EngineSelector,
)
from jasper.aec.bridge_telemetry import (
    LegEmitter,
    BridgeStats,
    add_loop_emitter,
    logger,
)


@dataclass(frozen=True, eq=False)
class SweepPath:
    """One configured AEC3 delay-sweep variant and its output leg."""

    variant: Aec3SweepVariant
    engine: Aec3Engine
    emitter: LegEmitter
    input_source: str


def _process_optional_engine(
    engine: Any,
    input_bytes: bytes,
    ref_bytes: bytes,
    *,
    failure_message: str | None,
) -> tuple[Any | None, bytes, Exception | None]:
    """Process one optional leg and disable it after its first failure.

    The primary AEC engine deliberately does not use this helper: a primary
    failure must still escape and trigger the bridge's systemd recovery path.
    """
    try:
        return engine, engine.process(input_bytes, ref_bytes), None
    except Exception as exc:  # noqa: BLE001
        if failure_message is not None:
            # stacklevel=2: see jasper/runtime/flight_recorder.py — the auto-dump key
            # is the record's file:line, so the caller's must survive.
            logger.exception(failure_message, exc, stacklevel=2)
        return None, b"", exc


@dataclass(eq=False)
class CorpusLanes:
    """Every optional lane built for one bridge configuration.

    The per-frame methods disable a lane in place when its engine fails. An
    emitter exists whenever its engine or input queue does.
    """

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

    def emit_reference(self, ref_bytes: bytes) -> None:
        if self.ref_emitter is not None:
            self.ref_emitter.emit(ref_bytes)

    def process_raw0(self, raw0_bytes: bytes, ref_bytes: bytes) -> None:
        """Run the XVF raw0 WebRTC and DTLN lanes on the frame `raw0` just emitted."""
        if self.xvf_raw0_engine is not None:
            self.xvf_raw0_engine, clean, _error = _process_optional_engine(
                self.xvf_raw0_engine,
                raw0_bytes,
                ref_bytes,
                failure_message=(
                    "XVF raw0 WebRTC process() crashed; disabling "
                    "xvf_raw0_webrtc_aec3 path: %s"
                ),
            )
            if clean:
                assert self.xvf_raw0_webrtc_emitter is not None
                self.xvf_raw0_webrtc_emitter.emit(clean)
        if self.xvf_raw0_dtln_engine is not None:
            self.xvf_raw0_dtln_engine, clean, _error = _process_optional_engine(
                self.xvf_raw0_dtln_engine,
                raw0_bytes,
                ref_bytes,
                failure_message=(
                    "XVF raw0 DTLN process() crashed; disabling "
                    "xvf_raw0_dtln path: %s"
                ),
            )
            if clean:
                assert self.xvf_raw0_dtln_emitter is not None
                self.xvf_raw0_dtln_emitter.emit(clean)

    def process_frame(
        self,
        mic_bytes: bytes,
        ref_bytes: bytes,
        usb_raw_q: Queue | None,
        *,
        sweep_source: str,
        emitters: dict[str, LegEmitter],
        stats: BridgeStats,
    ) -> None:
        """Run the DTLN observation, AEC3 sweep and USB lanes after the primary.

        DTLN runs AFTER engine.process so the wake loop's primary mic stream
        keeps its normal critical path: the extra ~1.5 ms of DTLN inference
        per frame spends the slack in the 20 ms frame budget.
        """
        if self.dtln_engine is not None:
            self._process_dtln(self.dtln_engine, mic_bytes, ref_bytes, emitters, stats)
        if sweep_source == AEC3_SWEEP_SOURCE_XVF:
            self.emit_aec3_sweep(mic_bytes, ref_bytes)
        if usb_raw_q is not None:
            self._process_usb(usb_raw_q, ref_bytes, sweep_source)

    def _process_dtln(
        self,
        engine: Any,
        mic_bytes: bytes,
        ref_bytes: bytes,
        emitters: dict[str, LegEmitter],
        stats: BridgeStats,
    ) -> None:
        self.dtln_engine, dtln_clean, dtln_error = _process_optional_engine(
            engine,
            mic_bytes,
            ref_bytes,
            failure_message=None,
        )
        if dtln_error is not None:
            # DTLN is observational: preserve the primary AEC3 path
            # and make this transition authoritative for the stats
            # writer and doctor. Nulling the engine keeps it to one
            # event rather than one warning per audio frame.
            with suppress(Exception):
                engine.close()
            failed_dtln_emitter = emitters.pop("dtln", None)
            if failed_dtln_emitter is not None:
                with suppress(Exception):
                    failed_dtln_emitter.close()
            self.dtln_emitter = None
            stats.mark_leg_unavailable("dtln", error=str(dtln_error))
            log_event(
                logger,
                "aec_bridge.leg_degraded",
                leg="dtln",
                phase="process",
                action="disable",
                error_type=type(dtln_error).__name__,
                error=str(dtln_error),
                level=logging.WARNING,
                exc_info=(
                    type(dtln_error),
                    dtln_error,
                    dtln_error.__traceback__,
                ),
            )
        if dtln_clean:
            assert self.dtln_emitter is not None
            self.dtln_emitter.emit(dtln_clean)

    def _process_usb(
        self, usb_raw_q: Queue, ref_bytes: bytes, sweep_source: str,
    ) -> None:
        try:
            usb_bytes = usb_raw_q.get_nowait()
        except Empty:
            usb_bytes = b""
        if not usb_bytes:
            return
        assert self.usb_raw_emitter is not None
        self.usb_raw_emitter.emit(usb_bytes)

        if self.usb_engine is not None:
            self.usb_engine, usb_clean, _error = _process_optional_engine(
                self.usb_engine,
                usb_bytes,
                ref_bytes,
                failure_message=(
                    "USB WebRTC process() crashed; disabling "
                    "usb_webrtc path: %s"
                ),
            )
            if usb_clean:
                assert self.usb_webrtc_emitter is not None
                self.usb_webrtc_emitter.emit(usb_clean)

        if self.usb_dtln_engine is not None:
            self.usb_dtln_engine, usb_dtln_clean, _error = _process_optional_engine(
                self.usb_dtln_engine,
                usb_bytes,
                ref_bytes,
                failure_message=(
                    "USB DTLN process() crashed; disabling "
                    "usb_dtln path: %s"
                ),
            )
            if usb_dtln_clean:
                assert self.usb_dtln_emitter is not None
                self.usb_dtln_emitter.emit(usb_dtln_clean)
        if sweep_source == AEC3_SWEEP_SOURCE_USB:
            self.emit_aec3_sweep(usb_bytes, ref_bytes)

    def close_engines(self) -> None:
        """Close the lane engines still running; `emitters` owns the sockets."""
        for engine in (
            self.xvf_raw0_engine,
            self.xvf_raw0_dtln_engine,
            self.usb_engine,
            self.usb_dtln_engine,
        ):
            if engine is not None:
                engine.close()
        for path in self.aec3_sweep_paths:
            with suppress(Exception):
                path.engine.close()


def build_corpus_lanes(
    emitters: dict[str, LegEmitter],
    stats: BridgeStats,
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
        stats,
        config,
        select_engine=select_engine,
        webrtc_enabled=xvf_raw0_webrtc_enabled,
        dtln_enabled=xvf_raw0_dtln_enabled,
    )
    ref_emitter = None
    if emit_ref:
        ref_emitter = add_loop_emitter(
            emitters, stats, config.out_host, "ref", config.out_port_ref
        )

    (
        usb_raw_emitter,
        usb_webrtc_emitter,
        usb_engine,
        usb_dtln_engine,
        usb_dtln_emitter,
    ) = _build_usb_optional_paths(
        emitters, stats, config, select_engine=select_engine, usb_raw_q=usb_raw_q
    )
    aec3_sweep_paths, emit_aec3_sweep = _build_aec3_sweep_paths(
        emitters,
        stats,
        config,
        select_engine=select_engine,
        production_chip_aec_enabled=production_chip_aec_enabled,
        usb_raw_q=usb_raw_q,
    )
    dtln_engine, dtln_emitter = _build_dtln_optional_path(
        emitters,
        stats,
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
    stats: BridgeStats,
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
        xvf_raw0_webrtc_emitter = add_loop_emitter(
            emitters,
            stats,
            config.out_host,
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
            xvf_raw0_dtln_emitter = add_loop_emitter(
                emitters,
                stats,
                config.out_host,
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
    stats: BridgeStats,
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
        usb_raw_emitter = add_loop_emitter(
            emitters, stats, config.out_host, "usb_raw", config.out_port_usb_raw
        )
        usb_webrtc_emitter = add_loop_emitter(
            emitters,
            stats,
            config.out_host,
            "usb_webrtc",
            config.out_port_usb_webrtc,
        )
        usb_webrtc_overrides = USB_AEC3_CORPUS_OVERRIDES
        usb_webrtc_label = "usb_webrtc/aec3_edge_combo_80"
        usb_webrtc_display_label = USB_AEC3_CORPUS_LABEL
        if (
            config.corpus_aec3_sweep_enabled
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
        if config.corpus_usb_dtln_enabled:
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
                usb_dtln_emitter = add_loop_emitter(
                    emitters,
                    stats,
                    config.out_host,
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
    stats: BridgeStats,
    config: BridgeConfig,
    *,
    select_engine: EngineSelector,
    production_chip_aec_enabled: bool,
    usb_raw_q: Queue | None,
) -> tuple[list[SweepPath], Callable[[bytes, bytes], None]]:
    """Build configured sweep variants and their per-frame dispatcher."""
    aec3_sweep_paths: list[SweepPath] = []
    if (not production_chip_aec_enabled) and config.corpus_aec3_sweep_enabled:
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
                variant_emitter = add_loop_emitter(
                    emitters, stats, config.out_host, variant.leg, variant_port
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
    stats: BridgeStats,
    config: BridgeConfig,
    *,
    production_chip_aec_enabled: bool,
) -> tuple[Any | None, LegEmitter | None]:
    """Build the optional DTLN observation leg without gating primary AEC3."""
    dtln_engine = None
    dtln_emitter = None
    dtln_wanted = (
        not production_chip_aec_enabled
    ) and config.dtln_enabled
    stats.set_leg_engine("dtln", enabled=dtln_wanted, loaded=False)
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
            dtln_emitter = add_loop_emitter(
                emitters, stats, config.out_host, "dtln", config.out_port_dtln
            )
            stats.set_leg_engine("dtln", enabled=True, loaded=True)
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
            stats.set_leg_engine(
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
