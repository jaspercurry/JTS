# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State aggregation helpers for jasper-control."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, Sequence

from .. import identity_state
from ..accessories import supervisor as accessory_bridges
from ..audio_quality import (
    DEFAULT_CONVERTER as _default_audio_converter,
    converter_options as _audio_converter_options,
    read_active_converter as _read_active_audio_converter,
    read_state as _read_audio_quality_state,
)
from ..music_sources import MUSIC_SOURCE_SPECS
from ..fanin.status import (
    FANIN_INPUT_SOURCE_DIRECT,
    fanin_usbsink_input,
)
from ..source_state import (
    usbsink_direct_audible,
    usbsink_direct_muted,
    usbsink_direct_rms_dbfs,
)
from ..usbgadget import DEFAULT_UDC_CLASS_DIR, udc_host_connected
from ..usb_network import (
    DEFAULT_PENDING_PATH as USB_NETWORK_PENDING_PATH,
    IPv4Observation,
    IPv4ObservationState,
    UsbNetworkPlanError,
    attest_plan as attest_usb_network_plan,
    load_plan as load_usb_network_plan,
    observe_ipv4_cidr,
)
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom import cascade_timeline
from ..multiroom.state import read_grouping_state
from ..transit.state import read_state as read_transit_state
from ..log_event import log_event
from ..volume_diagnostics import (
    build_volume_policy_snapshot,
    read_diagnostics as _read_volume_diagnostics,
)
from . import (
    bootloop_guard_state,
    camilla_recover_state,
    debug_control,
    grouping_supervisor,
    measurement_hold,
    mpris,
    shairport_supervisor,
    system_supervisor,
    transport_park,
    wifi_guardian_state,
)
from .aec_endpoints import _aec_full_status
from .uds import _local_status_json, _mux_socket_command, _voice_socket_command

logger = logging.getLogger(__name__)

SOURCE_AVAILABILITY_TTL_SEC = 10.0
_source_availability_cache: tuple[float, dict[str, Any]] | None = None
_source_availability_lock = threading.Lock()
OUTPUTD_BASE_CAMILLA_CONFIG = "/etc/camilladsp/outputd-cutover.yml"

# Per-probe ceiling for the CamillaDSP /state probe. Every other probe in
# _get_state already self-bounds (voice/mpris 2 s, mux 1 s,
# fan-in/outputd 2 s); the CamillaDSP probe did not, so a wedged-but-
# listening DSP — TCP accepted, websocket read stalled — could hang the
# whole aggregate indefinitely. On timeout the probe fails soft to its
# all-None section, exactly like its siblings.
_CAMILLA_PROBE_TIMEOUT_SEC = 2.0

# Liveness backstop for the entire cross-daemon fan-out. This is NOT a
# latency control — the normal path completes in ~200 ms, with HA's cached
# network probe (~8 s worst case) the slow outlier. It only fires if a
# probe blows past its own ceiling (e.g. a future probe added without
# one), converting an unbounded hang into a logged, bounded failure so the
# bounded-worker control plane can never be parked indefinitely on /state.
_STATE_AGGREGATE_BUDGET_SEC = 20.0
_default_ha_status_cache: Any | None = None

_VOICE_STATUS_DIRECT_KEYS = (
    "endpointer",
    "spend_allowed",
    "usage_tracking_degraded",
    "connection_paused",
    "connection_error",
    "mic_muted",
    "measurement_active",
    "duck_active",
    "camilla_volume_locked",
    "music_dbfs",
    "wake_legs",
    "push_to_talk_only",
    "tool_packs",
)
_VOICE_STATUS_NESTED_FIELDS = {
    "last_at": "barge_in_last_at",
    "count_session": "barge_in_count_session",
    "last_leg": "barge_in_last_leg",
}
_VOICE_STATUS_PUBLISHED_KEYS = (
    frozenset(_VOICE_STATUS_DIRECT_KEYS)
    | frozenset(_VOICE_STATUS_NESTED_FIELDS.values())
)
_VOICE_STATUS_WITHHELD_KEYS = frozenset({
    "state",
    "input_ended",
    "assistant_output",
    "manual_mic_sources",
    "active_manual_mic_source",
    "barge_in_reconcile",
    "research",
})


def _ha_failed_status(error: str = "probe failed") -> dict[str, Any]:
    return {
        "configured": False,
        "connected": False,
        "url": "",
        "instance_name": None,
        "version": None,
        "error": error,
    }


def _default_ha_status_snapshot() -> dict[str, Any]:
    """Child-process HA status snapshot for direct state-aggregate callers."""

    global _default_ha_status_cache
    if _default_ha_status_cache is None:
        from .ha_status_cache import HomeAssistantStatusCache

        _default_ha_status_cache = HomeAssistantStatusCache()
    return _default_ha_status_cache.snapshot()


def _safe_audio_quality_state() -> dict[str, Any]:
    try:
        return _read_audio_quality_state()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio quality state read failed")
        converter = _default_audio_converter
        options = _audio_converter_options()
        meta = next(
            option for option in options if option["converter"] == converter
        )
        try:
            active = _read_active_audio_converter()
        except Exception:  # noqa: BLE001
            active = None
        return {
            "converter": converter,
            "active_converter": active,
            "label": meta["label"],
            "summary": meta["summary"],
            "options": options,
            "error": str(e),
        }


def _build_usbsink_renderer_state(
    fanin_status: dict[str, Any] | None,
    *,
    host_connected: bool,
) -> dict[str, Any] | None:
    """Build ``/state.renderers.usbsink`` from its two live owners.

    Fan-in's identity-bound DIRECT lane owns activity, level, and mix-mute.
    ConfigFS/UDC sysfs owns host connection.  The section is absent when fan-in
    does not expose the DIRECT lane, preserving ``null == off/unavailable``
    without a copied state file or a resident compatibility daemon.
    """

    input_state = fanin_usbsink_input(fanin_status)
    if not input_state or input_state.get("source") != FANIN_INPUT_SOURCE_DIRECT:
        return None
    return {
        "combo": True,
        "playing": usbsink_direct_audible(fanin_status),
        # Compatibility fields retained for lightweight dashboard consumers.
        # Fan-in's `muted` is the actual arbitration state; there is no second
        # bridge process with an independent preemption state or timestamp.
        "preempted": False,
        "muted": usbsink_direct_muted(fanin_status),
        "host_connected": bool(host_connected),
        "rms_dbfs": usbsink_direct_rms_dbfs(fanin_status),
        "updated_at": None,
    }


def _camilla_unit_state(
    service_states: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """systemd's verdict on the DSP graph owner, for ``/state.audio_graph``.

    fan-in and outputd publish their own STATUS into this block; CamillaDSP
    has no such endpoint, and a STOPPED CamillaDSP is invisible to every other
    field here — fan-in free-run-drops on an absent ring reader and outputd
    zero-fills an absent writer, so both keep reporting healthy while the
    speaker emits nothing. Same cached snapshot jasper-control already samples
    for /system and for audio-health's ``path.camilla_stopped``, so this adds
    no probe; ``None`` means "not observed", never "running".
    """
    from .airplay_health import CAMILLA_UNIT_FULL

    state = (service_states or {}).get(CAMILLA_UNIT_FULL)
    if not isinstance(state, dict):
        return None
    return {
        "unit": CAMILLA_UNIT_FULL,
        "load_state": state.get("load_state"),
        "active_state": state.get("active_state"),
        "sub_state": state.get("sub_state"),
        "result": state.get("result"),
    }


def _audio_graph_state(
    *,
    fanin_status: dict[str, Any] | None,
    outputd_status: dict[str, Any] | None,
    service_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    try:
        from ..audio_runtime_plan import build_audio_runtime_plan_from_system

        plan = build_audio_runtime_plan_from_system()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio graph route plan read failed")
        # The camilla row survives an unreadable plan: an absent DAC or an
        # unreadable env file is a plausible cause of BOTH the throw here and a
        # stopped CamillaDSP, so this is the degraded case the row exists for.
        return {
            "route": {"status": "unavailable", "error": str(e)},
            "camilla": _camilla_unit_state(service_states),
        }

    fanin_usbsink = fanin_usbsink_input(fanin_status)
    outputd_dac = (
        outputd_status.get("dac")
        if isinstance(outputd_status, dict)
        and isinstance(outputd_status.get("dac"), dict)
        else None
    )
    outputd_aec_clock = (
        outputd_status.get("aec_clock")
        if isinstance(outputd_status, dict)
        and isinstance(outputd_status.get("aec_clock"), dict)
        else None
    )
    outputd_latency = (
        outputd_aec_clock.get("latency")
        if isinstance(outputd_aec_clock, dict)
        and isinstance(outputd_aec_clock.get("latency"), dict)
        else None
    )
    coupling_block = _coupling_state(
        fanin_status=fanin_status,
        outputd_status=outputd_status,
        outputd_env=plan.outputd_env,
    )
    return {
        "route": {
            "id": plan.route_profile.route_id,
            "source_id": plan.route_profile.source_id,
            "low_latency_claim": plan.route_profile.low_latency_claim,
            "route_config_hash": plan.route_config_hash,
            "contract": plan.route_profile.to_dict(),
        },
        "fanin": {
            "usbsink_input": fanin_usbsink,
            "resampler": (
                fanin_usbsink.get("resampler")
                if isinstance(fanin_usbsink, dict)
                else None
            ),
            # Combo-mode host-slaved USB clock (fan-in owns the gadget capture
            # under JASPER_FANIN_USB_DIRECT + JASPER_FANIN_HOST_CLOCK). fan-in
            # STATUS carries a top-level `host_clock` block byte-identical to
            # usbsink's solo-mode block, so combo boxes get the same /state
            # ladder/DLL/probe telemetry solo boxes get from usbsink. `None` when
            # the fan-in STATUS is unavailable or has no host_clock key (pre-combo
            # build) — a definite "no evidence" rather than a guessed default.
            "host_clock": (
                fanin_status.get("host_clock")
                if isinstance(fanin_status, dict)
                else None
            ),
        },
        "camilla": _camilla_unit_state(service_states),
        "outputd": {
            "dac_delay_ms": (
                outputd_dac.get("snd_pcm_delay_ms")
                if isinstance(outputd_dac, dict)
                else None
            ),
            "dac_delay_frames": (
                outputd_dac.get("snd_pcm_delay_frames")
                if isinstance(outputd_dac, dict)
                else None
            ),
            "final_reference_health": outputd_aec_clock,
            "route_latency_components": outputd_latency,
        },
        "coupling": coupling_block,
    }


def _coupling_state(
    *,
    fanin_status: dict[str, Any] | None,
    outputd_status: dict[str, Any] | None = None,
    outputd_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The resolved fan-in -> CamillaDSP coupling (audio-graph consolidation P2/P4).

    Surfaces the persisted token (``JASPER_FANIN_CAMILLA_COUPLING``), the outputd
    content bridge, whether those two are a coherent pair, and the live fan-in
    STATUS transport. Since ADR-0100 the live transport has one possible answer
    — a running fan-in is on the ring, and refuses anything else at parse — so
    what these fields are FOR is the migration: naming a box whose persisted
    files still carry the retired token, and the partial-flip window where one
    of the two has been rewritten and the other has not. Read fresh from the env
    files (never os.environ — jasper-control isn't restarted on a coupling
    change). Fail-soft: any read error degrades to ``None`` (see the except
    below) rather than erroring the whole /state call.

    ``intent_coherent`` is named for what it compares: the persisted coupling
    against the content source outputd resolved from its env. It was published
    as ``coherent`` until R5b, which reads as a verdict on the ring itself — and
    the two can agree perfectly while the rings shear on format, channels,
    period or slots. ``content_bridge`` names the resolved source: ``shm_ring``
    (the central post-DSP ring, including the undeclared default),
    ``dac_content_ring`` (a dumb bonded member, off the round-trip return ring),
    ``direct`` (nothing serves it), or ``contradicted`` — the marker declared
    beside a bridge, which outputd refuses at startup.
    ``observed`` is where the ring's actual
    wire lives: both daemons read their attached header back and publish it
    (fan-in as ``output.ring.wire_format``/``channels``, outputd as
    ``shm_ring.format``/``channels``), which is a fact about the running
    transport rather than about what somebody wrote in a file. Both are ``None``
    when the daemon is unreachable or the ring is not armed — absence here means
    "not observed", never "observed to agree"."""
    try:
        from pathlib import Path

        from ..audio_runtime_plan import TRANSPORT_DAC_CONTENT_RING
        from ..fanin.ring_health import FANIN_ENV_PATH, persisted_coupling_feeds_ring
        from ..fanin_coupling import (
            COUPLING_ENV_VAR,
            OUTPUTD_CONTENT_BRIDGE_SHM_RING,
            dac_content_marker_contradicted,
            dac_content_ring_served,
            outputd_content_is_central_ring,
        )
        from ..env_file import read_value
        from ..env_load import outputd_reconciled_env

        # A file the reconciler has not written yet is a declared absence
        # (undeclared is the ring); any other read failure is not a diagnosis
        # and falls to the except below.
        try:
            fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
        except FileNotFoundError:
            fanin_text = ""
        # The token AS WRITTEN, not a resolved transport: this block exists to
        # name a migrating box's retired value, which a resolver answering
        # "the ring or nothing" cannot spell.
        coupling = (read_value(fanin_text, COUPLING_ENV_VAR) or "").strip().lower()
        # The runtime plan already merged outputd's two env layers this /state
        # build; take that rather than reading both files again. ``None`` is the
        # standalone path (a focused caller, or a plan that failed to build).
        outputd_values = (
            dict(outputd_env) if outputd_env is not None else outputd_reconciled_env()
        )
        # What outputd IS RUNNING, through the predicates that own that
        # question. An UNDECLARED bridge is the ring (config.rs), so this
        # reports `shm_ring` for a box that named nothing — which is what it
        # runs. Reporting the raw absence instead made a healthy box's pair read
        # as incoherent on this surface. An armed dac-content marker OVERRIDES
        # that default: outputd resolves the bonded return ring as its sole
        # content source, which is the third value this field can take.
        if dac_content_marker_contradicted(outputd_values):
            # The pair outputd refuses at startup: neither source is running,
            # and calling it either one would report a silent box as served.
            content_bridge = "contradicted"
        elif dac_content_ring_served(outputd_values):
            content_bridge = TRANSPORT_DAC_CONTENT_RING
        elif outputd_content_is_central_ring(outputd_values):
            content_bridge = OUTPUTD_CONTENT_BRIDGE_SHM_RING
        else:
            content_bridge = "direct"
        live_transport = None
        if isinstance(fanin_status, dict):
            output = fanin_status.get("output")
            if isinstance(output, dict):
                live_transport = output.get("transport")
        return {
            "persisted": coupling or None,
            "content_bridge": content_bridge,
            # COHERENT means "outputd is running a content source this coupling
            # implies", not "both strings say shm_ring". Each end asked its own
            # daemon's accept set, so UNDECLARED is the ring on both — the pair
            # this used to call incoherent on every box the reconciler had not
            # written. A dumb bonded member is the second served source: its
            # fan-in hop is Ring A like every other box's and its post-DSP hop is
            # the bonded return ring BY DESIGN, so calling that pair incoherent
            # would report a correctly-configured speaker as mid-flip. `direct`
            # remains the one incoherent value — nothing serves it.
            "intent_coherent": (
                persisted_coupling_feeds_ring(text=fanin_text)
                and content_bridge not in ("direct", "contradicted")
            ),
            "live_transport": live_transport,
            "observed": {
                "ring_a": _observed_ring_wire(
                    fanin_status, ("output", "ring"), format_key="wire_format"
                ),
                "ring_b": _observed_ring_wire(
                    outputd_status, ("shm_ring",), format_key="format"
                ),
            },
            "combo": _combo_state(fanin_text=fanin_text),
        }
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as e:
        # Fail-soft so a transient issue never breaks the whole /state call, but
        # NOT to a value: this used to degrade to "persisted": "loopback" with
        # "intent_coherent": True, which named the RETIRED transport and called
        # it healthy — a read failure reported as a diagnosis. ``None`` says what
        # actually happened. Concrete exception set (no blind except): an import
        # miss, an unreadable env file, or a malformed value.
        logger.debug("coupling state read failed: %s", e)
        return {
            "persisted": None,
            "content_bridge": None,
            "intent_coherent": None,
            "live_transport": None,
            "observed": {"ring_a": None, "ring_b": None},
            "combo": {"state": "disarmed"},
        }


def _observed_ring_wire(
    status: dict[str, Any] | None,
    path: tuple[str, ...],
    *,
    format_key: str,
) -> dict[str, Any] | None:
    """The wire a daemon read back off the ring header it ATTACHED to.

    ``None`` when the daemon is unreachable, the block is absent (outputd
    publishes its ``shm_ring`` block only on the ring content bridge), or
    neither axis is present — all of which mean "not observed", which a reader
    must not confuse with "observed to be correct".

    ``format_key`` differs per daemon because the two chose different spellings
    for the same field (fan-in ``wire_format``, outputd ``format``); this is the
    one place that reconciles them, so /state publishes one shape.
    """
    node: Any = status
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, dict):
        return None
    sample_format = node.get(format_key)
    channels = node.get("channels")
    if sample_format is None and channels is None:
        return None
    return {
        "sample_format": sample_format,
        "channels": channels,
        "slots": node.get("slots"),
    }


def _combo_state(*, fanin_text: str) -> dict[str, Any]:
    """The resolved USB DIRECT state for ``/state.audio_graph.coupling.combo``.

    Read fresh from ``fanin.env`` (never ``os.environ`` — jasper-control is not
    restarted on a combo change). The source/coupling coordinators are the only
    owners that arm or disarm it from canonical user intent and hardware
    eligibility; capture self-heal telemetry cannot change this state.
    """
    try:
        from ..env_file import read_value
        from ..fanin.coupling_auto import (
            USB_COMBO_ENABLED_VALUE,
            USB_DIRECT_ENV_VAR,
        )

        armed = read_value(fanin_text, USB_DIRECT_ENV_VAR) == USB_COMBO_ENABLED_VALUE
        return {"state": "armed" if armed else "disarmed"}
    except (ImportError, OSError, ValueError, TypeError) as e:
        logger.debug("combo state read failed: %s", e)
        return {"state": "disarmed"}


def _conversation_history_state() -> dict[str, Any] | None:
    """Read /state.chat fresh from the conversation-history SSOT + store."""
    from datetime import datetime, timezone

    from ..conversation_history import ConversationStore, read_settings

    settings = read_settings()
    store = ConversationStore(
        settings.db_path,
        read_only=True,
        warn_unavailable=False,
    )
    try:
        stats = store.stats()
        if stats is None:
            if settings.capture_enabled:
                return None
            return {
                "capture_enabled": False,
                "turn_count": None,
                "last_write_age_seconds": None,
                "retention": settings.retention,
            }
        age_seconds = None
        if stats.last_write_ts_utc:
            raw = stats.last_write_ts_utc.strip()
            parse_value = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
            try:
                ts = datetime.fromisoformat(parse_value)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, round(time.time() - ts.timestamp(), 1))
            except ValueError:
                age_seconds = None
        return {
            "capture_enabled": settings.capture_enabled,
            "turn_count": stats.turn_count,
            "last_write_age_seconds": age_seconds,
            "retention": settings.retention,
        }
    finally:
        store.close()


def _research_state(
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read privacy-safe async-research state."""
    from ..research.state import snapshot

    return snapshot(runtime=runtime)


def _active_speaker_parked_snapshot() -> dict[str, Any]:
    """Whether the speaker is PARKED silent for incomplete speaker setup.

    An unconfigured topology, or a roleful/protected topology without its
    startup graph, is seeded a proven-silent parked graph so the deploy can
    complete. Nothing is audible until the household saves the next valid
    layout. Two keys only — the config path is already in ``/state.audio``, so
    it is not restated here.

    Keyed on the persisted STATEFILE, not on the live CamillaDSP config path,
    deliberately: the two other surfaces that report this state
    (``jasper-doctor``'s ``active speaker runtime graph`` and
    ``audio_health._parked_graph_transport``) both read the statefile, and with
    CamillaDSP down the live path is empty — so keying on it would have made
    ``/state`` report ``parked: false`` on a parked box while the doctor said
    otherwise. The statefile is the box's durable intent; the live path is a
    liveness fact and belongs to ``/state.audio``.

    ``detail`` names only the exits that are reachable on this DAC.

    Fail-soft like every other resilience section, and the fail-soft lives in the
    readers: ``read_camilla_statefile_config_path`` returns None on any read
    problem and ``active_graph_is_parked`` is total, so this needs no guard of
    its own.
    """
    from ..active_speaker.environment import read_camilla_statefile_config_path
    from ..active_speaker.runtime_contract import (
        active_graph_is_parked,
        parked_muted_exits,
    )
    from ..audio_runtime_plan import DEFAULT_CAMILLA_STATEFILE_PATH
    from ..output_topology import OutputTopologyError, load_output_topology_strict

    config_path = read_camilla_statefile_config_path(DEFAULT_CAMILLA_STATEFILE_PATH)
    parked = active_graph_is_parked(config_path)
    if not parked:
        return {"parked": False, "detail": None}
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        return {
            "parked": True,
            "detail": "saved speaker layout is unavailable or invalid; run jasper-doctor",
        }
    return {"parked": True, "detail": parked_muted_exits(topology)}


def _disk_snapshot(path: str = "/") -> dict[str, Any] | None:
    """Root-filesystem fullness for /state.resilience — fail-soft.

    Returns ``{path, percent_used, free_gib, total_gib}`` or ``None`` on
    any error (non-POSIX dev host, statvfs failure), mirroring the
    fail-soft contract every other resilience-block section follows: a
    broken read leaves this section null and the rest of /state intact.
    jasper-doctor's ``check_disk_space`` owns the actionable warn/fail
    thresholds; this is the always-visible dashboard number that makes a
    filling SD card observable before the doctor is run. Uses f_bavail
    (non-root-available blocks) for free space so the figure matches what
    the daemons can actually write, but derives percent-used from
    total-vs-free so reserved blocks don't read as headroom."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        st = statvfs(path)
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        free = st.f_bavail * st.f_frsize
        gib = 1024 ** 3
        return {
            "path": path,
            "percent_used": ((total - free) * 100) // total,
            "free_gib": round(free / gib, 1),
            "total_gib": round(total / gib, 1),
        }
    except Exception:  # noqa: BLE001
        logger.debug("disk snapshot read failed", exc_info=True)
        return None


USB_NETWORK_IFACE = "usb0"


def _usb_network_wanted() -> bool:
    """Mirror ``jasper-usbgadget-up``'s network kill-switch read.

    Read fresh from ``os.environ`` on every call (never cached) — this
    daemon is not restarted when the kill switch flips, so a cached read
    would go stale exactly like the voice-provider bug this convention
    exists to avoid. Unless ``JASPER_USB_NETWORK`` is the exact literal
    ``disabled`` (case-insensitive), network is wanted — same convention
    as ``JASPER_SHAIRPORT_SUPERVISOR`` / ``JASPER_SYSTEM_SUPERVISOR``. NOT
    stripped, to match ``jasper-usbgadget-up``'s raw comparison so a
    whitespace-decorated ``" disabled"`` stays enabled in both (review
    core-7)."""
    raw = os.environ.get("JASPER_USB_NETWORK", "enabled")
    return raw.lower() != "disabled"


def _usb_network_snapshot() -> dict[str, Any]:
    """USB management-network summary for /state — fail-soft, uncached.

    ``enabled`` reflects
    the kill-switch intent (not composition — jasper-doctor's
    check_usbgadget_composition/check_usbnet_* own the actionable
    composed-vs-intent mismatch story); ``iface_present``/``carrier`` are
    read fresh from ``/sys/class/net/usb0`` every call, never cached, so
    plug/unplug shows up on the next poll. ``carrier=False`` (or
    ``iface_present=False``) is the normal "nothing plugged in" state, not
    an error — the dashboard should not alarm on it. The observed address is
    read from the interface while desired address/subnet/version/fingerprint
    come only from the validated installer-owned plan. A missing or corrupt
    plan reports those desired fields as null rather than fabricating an
    address; jasper-doctor owns the actionable failure."""
    enabled = _usb_network_wanted()
    iface_root = Path("/sys/class/net") / USB_NETWORK_IFACE
    iface_present = False
    carrier = False
    try:
        iface_present = iface_root.is_dir()
        if iface_present:
            carrier = (iface_root / "carrier").read_text().strip() == "1"
    except OSError:
        logger.debug("usb_network sysfs read failed", exc_info=True)
    observation = (
        observe_ipv4_cidr(USB_NETWORK_IFACE)
        if iface_present
        else IPv4Observation(IPv4ObservationState.ABSENT)
    )
    observed_cidr = observation.cidr
    observed_address = observed_cidr.split("/", 1)[0] if observed_cidr else None
    try:
        plan = attest_usb_network_plan(load_usb_network_plan())
    except UsbNetworkPlanError:
        plan = None
        logger.debug("usb_network plan read failed", exc_info=True)
    try:
        migration_pending = USB_NETWORK_PENDING_PATH.exists()
    except OSError:
        migration_pending = False
    return {
        "enabled": enabled,
        "iface_present": iface_present,
        "carrier": carrier,
        "address": observed_address,
        "cidr": observed_cidr,
        "observation_status": observation.state.value,
        "observation_error": observation.error,
        "desired_address": plan.device_address if plan else None,
        "subnet": plan.subnet if plan else None,
        "plan_version": plan.version if plan else None,
        "identity_fingerprint": plan.identity_fingerprint if plan else None,
        "migration_pending": migration_pending,
    }


def _multiroom_cascade_snapshot() -> dict[str, Any] | None:
    try:
        return cascade_timeline.snapshot()
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.debug("multiroom cascade timeline snapshot failed", exc_info=True)
        return None


def _same_config_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _sound_apply_target(last_apply: Any) -> str | None:
    if not isinstance(last_apply, dict):
        return None
    for key in ("active_config_path", "candidate_config_path"):
        value = last_apply.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _sound_runtime_status(
    sound_profile: dict[str, Any],
    active_config_path: str | None,
) -> dict[str, Any]:
    """Describe whether the desired sound profile is actually loaded.

    ``sound_profile["enabled"]`` is the persisted preference. The
    runtime truth is CamillaDSP's active config path, which can differ
    after rollback, install repair, or a manual Camilla reload. Keep the
    distinction explicit so status surfaces do not imply EQ is active
    when the daemon is running the flat outputd base config.
    """

    last_apply_path = _sound_apply_target(sound_profile.get("last_dsp_apply"))
    try:
        filter_count = int(sound_profile.get("filter_count") or 0)
    except (TypeError, ValueError):
        filter_count = 0
    desired_has_filters = bool(sound_profile.get("enabled")) and filter_count > 0
    runtime = {
        "active_config_path": active_config_path,
        "last_apply_config_path": last_apply_path,
        "matches_last_apply": None,
        "state": "unknown",
        "active": None,
        "warning": None,
    }
    if not active_config_path:
        return runtime

    if last_apply_path:
        runtime["matches_last_apply"] = _same_config_path(
            active_config_path,
            last_apply_path,
        )

    if _same_config_path(active_config_path, OUTPUTD_BASE_CAMILLA_CONFIG):
        runtime["state"] = "base"
        runtime["active"] = not desired_has_filters
    elif runtime["matches_last_apply"] is True:
        runtime["state"] = "applied"
        runtime["active"] = True
    elif last_apply_path:
        runtime["state"] = "mismatch"
        runtime["active"] = False
    else:
        runtime["state"] = "custom"
        runtime["active"] = None

    if desired_has_filters and runtime["active"] is not True:
        runtime["warning"] = (
            "Desired sound profile is not the active CamillaDSP config."
        )
    return runtime


async def _outputd_status(
    *,
    local_status_json: Callable[..., Any] = _local_status_json,
) -> dict | None:
    """Probe jasper-outputd's STATUS endpoint.

    Missing socket is fail-soft here so /state remains available while
    jasper-doctor owns the actionable cutover failure.
    """
    return await local_status_json("/run/jasper-outputd/control.sock")


async def _wifi_guardian_snapshot() -> dict[str, Any]:
    """Run bounded nmcli/journal probes off the aggregate event loop."""

    return await asyncio.to_thread(wifi_guardian_state.snapshot)


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add on/off wizard availability to mux source status.

    Mux knows audio policy; `/sources/` knows whether each renderer is
    enabled/available. The landing selector needs both, but keeping the
    merge here avoids teaching mux about systemd/DBus source toggles.
    """
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return payload
    global _source_availability_cache
    now = time.monotonic()
    with _source_availability_lock:
        cached = _source_availability_cache
        if cached is not None and now - cached[0] < SOURCE_AVAILABILITY_TTL_SEC:
            wizard_state = cached[1]
        else:
            wizard_state = None
    if wizard_state is None:
        try:
            from ..web.sources_setup import _gather_state as _sources_state
            fresh_state = _sources_state()
        except Exception as e:  # noqa: BLE001
            logger.debug("source availability read failed: %s", e)
            return payload
        with _source_availability_lock:
            _source_availability_cache = (now, fresh_state)
        wizard_state = fresh_state
    for spec in MUSIC_SOURCE_SPECS:
        wizard_key = spec.wizard_key
        mux_key = spec.id.value
        state = wizard_state.get(wizard_key)
        if not isinstance(state, dict):
            continue
        slot = sources.setdefault(mux_key, {})
        if isinstance(slot, dict):
            slot["available"] = bool(state.get("available", True))
            slot["enabled"] = bool(state.get("enabled", False))
    return payload


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    voice_socket_command: Callable[..., Any] = _voice_socket_command,
    mux_socket_command: Callable[..., Any] = _mux_socket_command,
    local_status_json: Callable[..., Any] = _local_status_json,
    aec_full_status: Callable[[], dict] = _aec_full_status,
    read_transit_state_func: Callable[[], dict] = read_transit_state,
    ha_status_snapshot: Callable[[], dict[str, Any]] | None = None,
    transport_park_snapshot: Callable[[], dict[str, Any]] = transport_park.snapshot,
    service_states_snapshot: (
        Callable[[], dict[str, dict[str, Any]]] | None
    ) = None,
) -> dict[str, Any]:
    """Aggregate state across daemons for GET /state. Each section
    fails soft — voice unreachable or Camilla restarting reports null
    in the affected section instead of erroring out
    the whole response. Slow probes fan out in parallel so the call
    completes in ~200 ms typical."""
    from datetime import datetime, timezone

    from .. import librespot_state
    from ..camilla import CamillaController
    from ..output_hardware import load_state as _load_output_hardware_state
    from ..speaker_name import read_state as _read_speaker_name_state
    from ..voice.provider_state import (
        read_active_model_from_env_files,
        read_active_provider_state,
        read_barge_in_enabled,
    )

    # Provider: re-read the wizard-owned SSOT file fresh on every call.
    # jasper-control is NOT restarted on a provider switch (only
    # jasper-voice is), so reading os.environ here pins the value to
    # whatever it was at this daemon's start and shows a stale provider
    # after every switch — the /system/ bug this fixes. Same fresh-read
    # rationale as the home_assistant block in /system/snapshot below.
    # ("", None) when unconfigured; never a guessed default.
    active_provider = read_active_provider_state()

    listening_level: int | None = None
    persisted_main_volume_db: float | None = None
    try:
        from ..volume_coordinator import VolumeState
        from ..volume_persistence import VolumePersistence

        path = os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        )
        record = VolumePersistence(path).load()
        if record is not None:
            listening_level = VolumeState.from_record(record).effective_percent
            if math.isfinite(record.main_volume_db):
                persisted_main_volume_db = round(record.main_volume_db, 2)
    except (OSError, ValueError):
        pass

    sound_profile: dict[str, Any] | None
    try:
        from ..dsp_apply import last_dsp_apply_state
        from ..sound.profile import (
            build_sound_filters,
            estimate_headroom_db,
            load_profile,
        )
        from ..sound.settings import load_sound_settings, output_trim_db

        profile = load_profile()
        sound_settings = load_sound_settings()
        sound_profile = {
            "enabled": profile.enabled,
            "curve_id": profile.curve_id,
            "simple_eq": profile.simple_eq.to_dict(),
            "parametric_band_count": len(profile.parametric_bands),
            "filter_count": len(build_sound_filters(profile)),
            "headroom_db": estimate_headroom_db(profile),
            # Global output settings + the effective trim they apply, so the
            # dashboard can explain why a profile sounds quieter/level-matched.
            "match_loudness": sound_settings.match_loudness,
            "headroom_trim_db": sound_settings.headroom_trim_db,
            "output_trim_db": output_trim_db(profile, sound_settings),
            "updated_at": profile.updated_at or None,
            "last_dsp_apply": last_dsp_apply_state(),
        }
    except Exception:  # noqa: BLE001
        logger.exception("sound profile state probe failed")
        sound_profile = None

    # Slow probes — fan out in parallel.
    def _round_db(value: float | None) -> float | None:
        if value is None:
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, 2)

    def _round_levels(
        levels: Sequence[float] | None,
    ) -> list[float | None] | None:
        """Every channel the running graph carries, not just the front pair.

        An active-crossover box plays four (or more) physical outputs; a
        stereo readout hides entire drivers, which is exactly what a
        commissioning ramp needs to see. The width comes from CamillaDSP.
        """
        if levels is None:
            return None
        return [_round_db(v) for v in levels]

    async def _camilla_status() -> dict[str, Any]:
        status: dict[str, Any] = {
            "main_volume_db": None,
            "playback_rms_dbfs": None,
            "playback_peak_dbfs": None,
            "clipped_samples": None,
            "active_config_path": None,
        }

        async def _no_config_path() -> None:
            return None

        try:
            cam = CamillaController(host=camilla_host, port=camilla_port)
            config_path_probe = (
                cam.get_config_file_path(best_effort=True)
                if hasattr(cam, "get_config_file_path")
                else _no_config_path()
            )
            vol, rms, peak, clipped, active_config_path = await asyncio.wait_for(
                asyncio.gather(
                    cam.get_volume_db(best_effort=True),
                    cam.get_playback_rms_all(best_effort=True),
                    cam.get_playback_peak_all(best_effort=True),
                    cam.get_clipped_samples(best_effort=True),
                    config_path_probe,
                ),
                timeout=_CAMILLA_PROBE_TIMEOUT_SEC,
            )
            status["main_volume_db"] = _round_db(vol)
            status["playback_rms_dbfs"] = _round_levels(rms)
            status["playback_peak_dbfs"] = _round_levels(peak)
            status["clipped_samples"] = clipped
            status["active_config_path"] = active_config_path
            return status
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                "state.camilla_probe_failed",
                error=exc,
                level=logging.DEBUG,
            )
            return status

    async def _airplay_playing() -> bool | None:
        # Shared probe owns the subprocess hygiene (kill-on-timeout so a
        # DBus stall can't leak one busctl per /state poll; spawn OSError
        # → None instead of 500ing the whole fail-soft aggregate).
        return await mpris.shairport_playing(timeout=2.0)

    async def _voice_status() -> dict | None:
        try:
            return await voice_socket_command(
                voice_socket_path, "STATUS", timeout=2.0,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError, RuntimeError):
            return None

    async def _ha_status() -> dict:
        """HA status for /state via the child-process cache boundary.

        The cache reads the wizard env-file signature fresh, so saves are
        reflected without restarting jasper-control, while HA/httpx imports
        stay in the short-lived probe child instead of the control daemon.
        """
        snapshot = ha_status_snapshot or _default_ha_status_snapshot
        try:
            return snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("home assistant state snapshot failed")
            return _ha_failed_status()

    async def _fanin_status() -> dict | None:
        """Probe the jasper-fanin daemon's UDS STATUS endpoint.

        Returns None when:
          - the daemon isn't running yet or is unhealthy
          - the socket doesn't exist (daemon not yet bound)
          - the probe times out (work loop wedged, ALSA blocked)
          - the response isn't valid JSON

        Fan-in is mandatory for renderer audio, but /state is fail-soft
        like _voice_status. jasper-doctor owns the actionable failure.
        """
        return await local_status_json("/run/jasper-fanin/control.sock")

    async def _mux_status() -> dict | None:
        try:
            return await mux_socket_command("STATUS", timeout=1.0)
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    async def _aec_status() -> dict | None:
        """Additive mirror of GET /aec for one-shot /state consumers."""
        try:
            return await asyncio.to_thread(aec_full_status)
        except Exception:  # noqa: BLE001
            logger.exception("AEC/profile state probe failed")
            return None

    try:
        (
            camilla_st,
            airplay,
            voice_st,
            ha_status,
            fanin_st,
            outputd_st,
            mux_st,
            aec_status,
            wifi_guardian,
        ) = await asyncio.wait_for(
            asyncio.gather(
                _camilla_status(),
                _airplay_playing(),
                _voice_status(),
                _ha_status(),
                _fanin_status(),
                _outputd_status(local_status_json=local_status_json),
                _mux_status(),
                _aec_status(),
                _wifi_guardian_snapshot(),
            ),
            timeout=_STATE_AGGREGATE_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        # A probe blew past its own ceiling. Fail loud (the handler turns
        # this into a 502) rather than hang a bounded worker forever; the
        # cheap /healthz probe stays answerable so this can't manufacture a
        # T5.2 reboot. Greppable so the offending probe is diagnosable.
        log_event(
            logger,
            "state.aggregate_timeout",
            budget_sec=_STATE_AGGREGATE_BUDGET_SEC,
            level=logging.WARNING,
        )
        raise

    spotify_blob = librespot_state.read(librespot_state.configured_path())
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            camilla_st.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Keep these top-level aliases for lightweight consumers that
        # only need the running truth and do not want to parse the nested
        # runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]
    speaker_name_state = _read_speaker_name_state()
    spotify = {
        "playing": bool(spotify_blob.get("playing", False)),
        "track_id": spotify_blob.get("track_id"),
        "uri": spotify_blob.get("uri"),
        "session_active": bool(spotify_blob.get("session_active", False)),
    }

    # USB Audio Input — fourth renderer. Fan-in owns the live DIRECT lane;
    # kernel UDC state owns host connection. No copied state file or resident
    # bridge helper sits between those owners and this projection.
    usbsink_state = _build_usbsink_renderer_state(
        fanin_st,
        host_connected=udc_host_connected(
            os.environ.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR),
        ),
    )

    voice_status = voice_st or {}
    voice_session = bool(voice_st) and voice_status.get("state") == "SESSION"
    # Active-source picks. Mux owns the effective audible source in
    # both manual and auto mode. Fall back to raw renderer probes only
    # when mux is unavailable or has no selected winner yet.
    mux_effective_source = None
    if isinstance(mux_st, dict):
        raw_selected = mux_st.get("selected_source")
        if isinstance(raw_selected, str):
            mux_effective_source = raw_selected
        else:
            raw_winner = mux_st.get("winner")
            if isinstance(raw_winner, str):
                mux_effective_source = raw_winner

    if voice_session:
        active_source: str = "voice"
    elif mux_effective_source:
        active_source = mux_effective_source
    elif spotify["playing"]:
        active_source = "spotify"
    elif airplay:
        active_source = "airplay"
    elif usbsink_state is not None and usbsink_state.get("playing"):
        # Fallback (only when mux is unavailable / has no winner yet). The
        # `playing` flag is authoritative on both box shapes now: solo reads the
        # bridge's RMS-gated flag; combo derives it from the fan-in DIRECT lane's
        # level (audible above the shared -60 dBFS gate — never a stale idle
        # default). A combo box streaming silence reads playing=false and does
        # not fire this branch, matching solo.
        active_source = "usbsink"
    else:
        active_source = "idle"

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=camilla_st["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=mux_st,
        diagnostics=_read_volume_diagnostics(),
    )

    # Multiroom grouping. Re-reads /var/lib/jasper/grouping.env fresh
    # (never os.environ — jasper-control isn't restarted on a wizard
    # save). read_grouping_state is itself total, but guard the section
    # so any future read change can't take the whole /state down: a
    # broken read leaves grouping null and the rest of /state intact.
    # enabled=False means grouping is off (solo); enabled=True with a
    # non-null error is the fail-LOUD "configured but broken" state.
    try:
        grouping_state: dict | None = read_grouping_state(
            local_outputd_reader=lambda: outputd_st,
        )
    except Exception:  # noqa: BLE001
        logger.exception("grouping state read failed")
        grouping_state = None

    # Bonded-leader AirPlay latency fit (Stage D observability — see
    # jasper/multiroom/airplay_latency.py). The shared composer (also used by
    # /rooms.json) attaches it non-mutatingly; read_grouping_state stays a pure
    # config projection and the gated, cached journal read lives behind the
    # helper. Total (returns {"applicable": False} on solo without touching the
    # journal), so the grouping section survives a broken read.
    grouping_state = with_airplay_latency_fit(grouping_state)

    try:
        active_speaker_setup = read_active_speaker_setup_status(
            active_config_path=camilla_st.get("active_config_path"),
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        logger.exception("active speaker setup status read failed")
        active_speaker_setup = None

    # Null means the speaker is on its applied graph. Non-null means somebody is
    # auditioning a reduced one, and every other reading of this speaker's sound
    # is about THAT graph. `stale` marks a record whose owner died without
    # restoring: the graph is still reduced, but nothing is going to put it back.
    try:
        from ..active_speaker.audition import read_audition_state

        audition_state: dict | None = read_audition_state()
        if audition_state is not None:
            audition_state = dict(audition_state)
            audition_state["stale"] = (
                float(audition_state.get("deadline_at") or 0.0) <= time.time()
            )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("audition state read failed")
        audition_state = None

    try:
        from ..bass_extension.profile import bass_extension_state_summary

        bass_extension_state = bass_extension_state_summary()
    except (
        ImportError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ):
        logger.exception("bass extension profile state read failed")
        bass_extension_state = None

    # Transit city packs. Re-reads /var/lib/jasper/transit.env fresh (never
    # os.environ — jasper-control isn't restarted on a /transit/ save, only
    # jasper-voice is). read_transit_state is itself total, but guard the
    # section so a future read change can't take the whole /state down: a
    # broken read leaves transit null and the rest of /state intact.
    try:
        transit_state: dict | None = read_transit_state_func()
    except Exception:  # noqa: BLE001
        logger.exception("transit state read failed")
        transit_state = None
    try:
        output_hardware = _load_output_hardware_state()
        output_hardware_state = (
            output_hardware.to_dict()
            if output_hardware is not None
            else None
        )
    except Exception:  # noqa: BLE001
        logger.exception("output hardware state read failed")
        output_hardware_state = None

    try:
        service_states = (
            service_states_snapshot() if service_states_snapshot else None
        )
    except Exception:  # noqa: BLE001
        logger.exception("service state snapshot read failed")
        service_states = None

    audio_graph_state = _audio_graph_state(
        fanin_status=fanin_st,
        outputd_status=outputd_st,
        service_states=service_states,
    )

    # Tool catalog summary. Fresh read of /run/jasper/tools.json (written by
    # jasper-voice) + the wizard-owned disabled-set — never os.environ, since
    # jasper-control isn't restarted on a /tools/ toggle. Light view module
    # (json + tool_state only). Guarded so a read change can't take /state down.
    try:
        from ..tool_catalog_view import summary as _tool_summary
        tools_state: dict | None = _tool_summary()
    except Exception:  # noqa: BLE001
        logger.exception("tool catalog state read failed")
        tools_state = None

    # Conversation history is a read-only Feature surface. Settings are
    # wizard-owned and read fresh; the SQLite store is opened read-only so
    # jasper-control cannot create or mutate jasper-voice's DB.
    try:
        chat_state = _conversation_history_state()
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.exception("conversation history state read failed")
        chat_state = None

    try:
        research_state = _research_state(voice_status.get("research"))
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.exception("research state read failed")
        research_state = None

    # Lazy import (mirrors read_active_provider_state above) so jasper-control
    # doesn't pull jasper.voice.* at module load. mic_presence reads the
    # reconciler's SSOT (one JSON read + a marker stat per /state) — cheap, and
    # it composes voice.input_presence's tiny marker reader.
    from ..mic_presence import read_mic_presence
    mic_presence = read_mic_presence()

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            # active_provider.model only sees the wizard file; a model
            # pinned solely in jasper.env would show the catalog default.
            # read_active_model_from_env_files merges jasper.env + the
            # wizard file (same set jasper-voice sources) — same drift
            # class as the doctor's pricing row, issue #3133.
            "model": (
                read_active_model_from_env_files(active_provider.provider)
                if active_provider.configured
                else None
            ),
            "provider_status": active_provider.status,
            "provider_error": active_provider.detail or None,
            "session_active": voice_session,
            **{key: voice_status.get(key) for key in _VOICE_STATUS_DIRECT_KEYS},
            "barge_in": {
                "enabled": (
                    read_barge_in_enabled(active_provider.provider)
                    if active_provider.provider else False
                ),
                **{
                    field: voice_status.get(status_key)
                    for field, status_key in _VOICE_STATUS_NESTED_FIELDS.items()
                },
            },
            "reachable": voice_st is not None,
            # Disambiguates reachable:false. True when the AEC reconciler
            # parked voice for a missing microphone (its ConditionPathExists
            # marker is present) — i.e. "intentionally idle, no mic", NOT
            # "crashed". Read fresh from the marker each call (jasper-control
            # isn't restarted on a mic plug/unplug).
            # Derived from the same read as the top-level `microphone` block
            # below, so the boolean and the rich record can never disagree.
            "parked_no_mic": mic_presence.parked,
        },
        # Single source of truth for mic presence (jasper.mic_presence): the
        # reconciler's one canonical record, surfaced so the dashboard / any
        # client renders "no microphone" as one fact (present + reason + card +
        # variant + channels + a ready-made `summary`) instead of inferring it
        # from voice.reachable:false.
        "microphone": mic_presence.as_dict(),
        "audio": {
            "main_volume_db": camilla_st["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": camilla_st["playback_rms_dbfs"],
            "playback_peak_dbfs": camilla_st["playback_peak_dbfs"],
            "clipped_samples": camilla_st["clipped_samples"],
            "camilla_active_config_path": camilla_st["active_config_path"],
            "sound": sound_profile,
            "output_hardware": output_hardware_state,
        },
        "audio_graph": audio_graph_state,
        "active_speaker_setup": active_speaker_setup,
        "audition": audition_state,
        "bass_extension": bass_extension_state,
        "renderers": {
            "spotify": spotify,
            "airplay": (
                None if airplay is None else {"playing": airplay}
            ),
            # null when the feature is disabled (no state file). The
            # /system dashboard and any other consumer can show
            # "off" vs "idle" based on this.
            "usbsink": usbsink_state,
        },
        "speaker_name": {
            "name": speaker_name_state.name,
            "source": speaker_name_state.source,
        },
        "active_source": active_source,
        # Fan-in daemon. null only when the daemon/socket is unavailable.
        # When running, the UDS STATUS endpoint emits a JSON snapshot
        # with per-input frame counts, output xrun counts, and watchdog
        # metrics — surfaced verbatim here.
        "fanin": fanin_st,
        # Final-output owner on current main. null when the daemon/socket
        # is unavailable; jasper-doctor owns the actionable failure.
        "outputd": outputd_st,
        # Additive mirror of GET /aec so one-shot /state consumers can see
        # requested intent vs observed mic/profile runtime truth without a
        # second control-plane request. null only when the probe itself fails.
        "aec": aec_status,
        "source_selection": mux_st,
        "resilience": {
            "shairport": shairport_supervisor.snapshot(),
            # Bonded-member runtime liveness: dac_content starvation
            # watch (kicks the grouping reconciler, rate-limited) +
            # continuous snapcast binding read-repair on the leader.
            # Off via JASPER_GROUPING_SUPERVISOR=disabled.
            "grouping_supervisor": grouping_supervisor.snapshot(),
            # T5.2 — userspace-liveness supervisor. Probes sshd / our
            # own HTTP / /proc/loadavg every 30 s; clean-reboots after
            # 3 consecutive failures (rate-limited 1/24h). Off via
            # JASPER_SYSTEM_SUPERVISOR=disabled.
            "system_supervisor": system_supervisor.snapshot(),
            # WiFi profile guardian: self-heal of the NM keyfile after
            # dirty shutdown. Synthesised from the on-disk stash + the
            # most recent `event=wifi_guardian.*` journal line — there's
            # no resident daemon to ask (the guardian is Type=oneshot).
            # Fail-soft inside the snapshot itself; never raises.
            "wifi_guardian": wifi_guardian,
            # Boot-loop guard (cross-boot circuit breaker for the T5.1
            # StartLimitAction=reboot ladder). Fresh marker read per
            # call; {"ran": false} when the oneshot hasn't run this
            # boot. tripped=true means reboot escalation is disarmed
            # for this boot via runtime drop-ins — fix the failing
            # daemon, then reboot to re-arm.
            "bootloop_guard": bootloop_guard_state.snapshot(),
            # jasper-camilla-recover's core-graph park record (ADR-0175).
            # parked=true means one bounded recovery pass could not bring the
            # DSP graph back, so CamillaDSP was stopped out-of-band: the
            # speaker emits NOTHING and nothing re-arms it automatically — the
            # record's own `action`/`re_arm` are the remedy. Fresh /run read
            # per call; {"status": "absent"} on a healthy boot. Same reader
            # jasper-doctor's check_camilla_recover_park uses, so the two
            # surfaces cannot disagree.
            "camilla_recover": camilla_recover_state.snapshot(),
            # The four named parks of the one-audio-transport rule
            # (ADR-0178). Read from the audio-health sampler's cached verdict
            # when there is one, so this field and the household rows built
            # from it in the same payload cannot disagree in time. Same reader
            # jasper-doctor's check_ring_transport_park uses, so the three
            # surfaces cannot disagree either.
            "transport_park": transport_park_snapshot(),
            # Bounded after-the-fact timeline for multiroom restart cascades:
            # existing event=multiroom.reconcile.*, restart_broker.*, and
            # grouping_supervisor.* journal lines, scanned into a tiny ring so
            # /state can answer "what kicked what recently?" without a raw log
            # bundle.
            "multiroom_cascade": _multiroom_cascade_snapshot(),
            # Effective mDNS identity (jasper-identity-reconcile, boot
            # + 5-min timer). status=collision means Avahi renamed us —
            # another device owns our hostname; the management
            # allowlist self-heals from the same file, but the
            # household should pick a unique name. Fresh file read per
            # call (reconciler-owned, this daemon is never restarted on
            # identity changes); {"status": "absent"} pre-first-run.
            "identity": identity_state.snapshot(),
            # Root-filesystem fullness ({path, percent_used, free_gib,
            # total_gib}). A full SD card is the corruption hazard the
            # whole resilience ladder exists to survive, yet nothing made
            # it observable until writes failed. Fail-soft: null on a
            # non-POSIX host or statvfs error. jasper-doctor's
            # check_disk_space owns the warn(≥85%)/fail(≥95%) thresholds.
            "disk": _disk_snapshot(),
            # Speaker-setup PARKED state (#2135): an unconfigured topology or
            # declared-but-uncommissioned roleful topology holds silence instead
            # of allowing an inferred flat graph. {"parked": bool, "detail":
            # <the reachable next action, or null>}. Read from the STATEFILE,
            # like the doctor and audio_health surfaces, so a down CamillaDSP
            # cannot make a parked box read as not-parked.
            "active_speaker_parked": _active_speaker_parked_snapshot(),
            # Per-bridge health inside jasper-input (ADR-0225), which stays
            # `active` while one bridge loops in restart backoff. `last_error`
            # is an exception class name, never a message — a bridge fault can
            # name the device.
            "accessory_bridges": accessory_bridges.snapshot(),
        },
        "home_assistant": ha_status,
        # Multiroom grouping (off by default). null only if the fresh
        # read itself errored; otherwise a JSON-able snapshot of the
        # wizard-owned grouping.env (enabled / role / channel / bond_id /
        # leader_addr / buffer_ms / codec / error), PLUS airplay_latency_fit:
        # the bonded-leader AirPlay tight-regime observability ({applicable:
        # false} unless this speaker is an active bonded leader). See
        # jasper/multiroom/state.py + jasper/multiroom/airplay_latency.py.
        "grouping": grouping_state,
        # Transit city packs (which cities' transit is enabled). null only
        # if the fresh read itself errored; otherwise {packs: [{id, label,
        # enabled}]} read fresh from the wizard-owned transit.env. Mirrors
        # the daemon's enabled_pack_ids on both absent (all) and
        # present-empty (none). See jasper/transit/state.py.
        "transit": transit_state,
        # Runtime debug-logging toggle (the /system Debug card): which
        # subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
        # Tool catalog summary ({catalog_present, count, disabled,
        # disabled_count, pending}). null only if the fresh read itself
        # errored. Read fresh from /run/jasper/tools.json + the wizard-owned
        # tool_state.env by jasper.tool_catalog_view (never os.environ).
        # jasper-doctor's check_tool_catalog owns the actionable warn.
        "tools": tools_state,
        # Conversation-history summary. null only if the read-side store
        # is unavailable while capture is enabled, or if the state read
        # itself fails. See jasper.conversation_history.
        "chat": chat_state,
        # Async research summary. Counts and timestamps only; no prompt or
        # answer text leaves the local store through /state.
        "research": research_state,
        # The open measurement window, as jasper-control sees it
        # ({active, owner, mode, expires_in_s, held_for_s}). This process holds
        # one of the three self-expiring copies of that fact — the copy that
        # makes it decline source-observed volume writes — so the projection is
        # a plain in-memory read of jasper.control.measurement_hold, not a
        # probe. `active` is what a household surface renders as "measurement
        # in progress"; `held_for_s` is what jasper-doctor's
        # check_measurement_hold reads, since `expires_in_s` resets on every
        # renewal and so can never reveal a stuck hold.
        "measurement": measurement_hold.snapshot(),
        # USB management network: the default-on, hardware-gated NCM link
        # on usb0 that lets http://<JASPER_HOSTNAME>/
        # work with WiFi off when the resolved USB role permits gadget mode.
        # Observed link/address plus the validated desired plan — read fresh from
        # /sys/class/net/usb0 and the kill-switch env every call, never
        # cached; carrier=False/absent is normal (nothing plugged in), never
        # an error. jasper-doctor's check_usbnet_* own the actionable
        # composed-vs-intent mismatch story.
        "usb_network": _usb_network_snapshot(),
    }
