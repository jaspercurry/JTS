# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State aggregation helpers for jasper-control."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple, Sequence, TypeVar

from .. import identity_state
from ..accessories import status as accessory_status
from ..memory_policy import disk_usage
from ..fanin.status import (
    FANIN_INPUT_SOURCE_DIRECT,
    fanin_usbsink_input,
)
from ..source_state import (
    usbsink_direct_audible,
    usbsink_direct_muted,
    usbsink_direct_rms_dbfs,
)
from ..usbgadget import (
    DEFAULT_UDC_CLASS_DIR,
    network_wanted,
    udc_host_connected,
)
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
from ..route_latency.status_socket import (
    FANIN_STATUS_SOCKET,
    OUTPUTD_STATUS_SOCKET,
)
from .. import outputd_failure_reconcile_state
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
    shairport_supervisor,
    system_supervisor,
    transport_park,
)
from .aec_endpoints import _aec_full_status
from .uds import _local_status_json, _mux_socket_command, _voice_socket_command

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

OUTPUTD_BASE_CAMILLA_CONFIG = "/etc/camilladsp/outputd-cutover.yml"

# Per-probe ceiling for the CamillaDSP /state probe: a wedged-but-listening
# DSP (TCP accepted, websocket read stalled) would otherwise hang the whole
# aggregate indefinitely. On timeout the probe fails soft to its all-None
# section, like its self-bounding siblings (voice 2 s, mux 1 s,
# fan-in/outputd 2 s).
_CAMILLA_PROBE_TIMEOUT_SEC = 2.0

# Bump when the key sets pinned in tests/test_wire_contracts.py change shape,
# so a consumer can branch on the number instead of probing for keys.
# See ADR-0233 rule 2.
STATE_SCHEMA_VERSION = 1

# Liveness backstop for the entire cross-daemon fan-out. NOT a latency
# control — the normal path completes in ~200 ms, with HA's cached network
# probe (~8 s worst case) the slow outlier. It fires only if a probe blows
# past its own ceiling, converting an unbounded hang into a logged, bounded
# failure so the bounded-worker control plane is never parked on /state.
_STATE_AGGREGATE_BUDGET_SEC = 20.0
_default_ha_status_cache: Any | None = None

_VOICE_STATUS_DIRECT_KEYS = (
    "endpointer",
    "last_turn_ms",
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
    "silent_responses_session",
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

    A STOPPED CamillaDSP is invisible to every other field here — fan-in
    free-run-drops on an absent ring reader and outputd zero-fills an absent
    writer, so both keep reporting healthy while the speaker emits nothing.
    Reads the cached service snapshot jasper-control already samples, so it
    adds no probe; ``None`` means "not observed", never "running".
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
        # The camilla row survives an unreadable plan: one cause (absent DAC,
        # unreadable env file) explains both this throw and a stopped DSP.
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
    outputd_reference_outputs = (
        outputd_status.get("reference_outputs")
        if isinstance(outputd_status, dict)
        else None
    )
    # outputd nests aec_clock inside reference_outputs
    # (rust/jasper-outputd/src/state.rs), as jasper-doctor reads it.
    outputd_aec_clock = (
        outputd_reference_outputs.get("aec_clock")
        if isinstance(outputd_reference_outputs, dict)
        and isinstance(outputd_reference_outputs.get("aec_clock"), dict)
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
            # Combo-mode host-slaved USB clock: fan-in owns the gadget capture
            # under JASPER_FANIN_USB_DIRECT + JASPER_FANIN_HOST_CLOCK and
            # publishes a `host_clock` block byte-identical to usbsink's
            # solo-mode one. `None` is "no evidence", never a guessed default.
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
    """The resolved fan-in -> CamillaDSP coupling for ``/state.audio_graph``.

    Surfaces the persisted token (``JASPER_FANIN_CAMILLA_COUPLING``), the
    outputd content bridge, whether those two are a coherent pair, and the live
    fan-in STATUS transport. Since ADR-0100 the live transport has one possible
    answer — a running fan-in is on the ring, and refuses anything else at parse
    — so what these fields are FOR is the migration: naming a box whose
    persisted files still carry the retired token, and the partial-flip window
    where one of the two has been rewritten and the other has not. Read fresh
    from the env files (never ``os.environ`` — jasper-control is not restarted
    on a coupling change); any read error degrades to ``None``.

    ``intent_coherent`` compares the persisted coupling against the content
    source outputd resolved from its env; agreement there does not prove the
    rings match on format, channels, period or slots, which is what
    ``observed`` is for. ``content_bridge`` names that source:
    ``shm_ring`` (the central post-DSP ring, including the undeclared default),
    ``dac_content_ring`` (a dumb bonded member, off the round-trip return ring),
    ``direct`` (nothing serves it), or ``contradicted`` — the marker declared
    beside a bridge, which outputd refuses at startup. ``observed`` is where the
    ring's actual wire lives: both daemons read their attached header back and
    publish it (fan-in as ``output.ring.wire_format``/``channels``, outputd as
    ``shm_ring.format``/``channels``). Both are ``None`` when the daemon is
    unreachable or the ring is not armed — absence means "not observed", never
    "observed to agree"."""
    try:
        from pathlib import Path

        from ..fanin.ring_health import FANIN_ENV_PATH, persisted_coupling_feeds_ring
        from ..fanin_coupling import (
            COUPLING_ENV_VAR,
            OUTPUTD_CONTENT_BRIDGE_SHM_RING,
            TRANSPORT_DAC_CONTENT_RING,
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
        # The token AS WRITTEN, not a resolved transport: a resolver answering
        # "the ring or nothing" cannot spell a migrating box's retired value.
        coupling = (read_value(fanin_text, COUPLING_ENV_VAR) or "").strip().lower()
        # The runtime plan already merged outputd's two env layers; ``None`` is
        # the standalone path (a focused caller, or a plan that failed to build).
        outputd_values = (
            dict(outputd_env) if outputd_env is not None else outputd_reconciled_env()
        )
        # What outputd IS RUNNING, through the predicates that own that
        # question. An UNDECLARED bridge is the ring (config.rs); an armed
        # dac-content marker overrides that default, resolving the bonded
        # return ring as outputd's sole content source.
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
            # implies", not "both strings say shm_ring": UNDECLARED is the ring
            # on both ends, and a dumb bonded member's post-DSP hop is the
            # bonded return ring BY DESIGN. `direct` is the one incoherent
            # value — nothing serves it.
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
        # Fail-soft so a transient issue never breaks the whole /state call,
        # but NOT to a value: a read failure must not read as a diagnosis.
        # Concrete exception set: import miss, unreadable env file, bad value.
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
    layout. Two keys only — the config path is already in ``/state.audio``.

    Keyed on the persisted STATEFILE, not on the live CamillaDSP path, so this
    agrees with the two other surfaces that report the state
    (``jasper-doctor``'s ``active speaker runtime graph`` and
    ``audio_health._parked_graph_transport``): with CamillaDSP down the live
    path is empty, and a parked box would read unparked. ``detail`` names only
    the exits reachable on this DAC.

    Needs no guard of its own: ``read_camilla_statefile_config_path`` returns
    None on any read problem and ``active_graph_is_parked`` is total.
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

    Returns ``{path, percent_used, free_gib, total_gib}``, or ``None`` when the
    filesystem cannot be measured (non-POSIX dev host, statvfs failure,
    zero-sized). jasper-doctor's ``check_disk_space`` owns the actionable
    warn/fail thresholds."""
    try:
        usage = disk_usage(path)
    except Exception:  # noqa: BLE001
        logger.debug("disk snapshot read failed", exc_info=True)
        return None
    if usage is None or usage.total_bytes <= 0:
        return None
    gib = 1024 ** 3
    return {
        "path": usage.path,
        "percent_used": int(usage.percent_used),
        "free_gib": round(usage.free_bytes / gib, 1),
        "total_gib": round(usage.total_bytes / gib, 1),
    }


USB_NETWORK_IFACE = "usb0"


def _usb_network_snapshot() -> dict[str, Any]:
    """USB management-network summary for /state — fail-soft, uncached.

    ``enabled`` reflects the kill-switch intent, not composition —
    jasper-doctor's check_usbgadget_composition/check_usbnet_* own the
    composed-vs-intent mismatch story. ``iface_present``/``carrier`` are read
    fresh from ``/sys/class/net/usb0`` every call, never cached;
    ``carrier=False`` (or ``iface_present=False``) is the normal "nothing
    plugged in" state, not an error. The observed address is read from the
    interface while desired address/subnet/version/fingerprint come only from
    the validated installer-owned plan; a missing or corrupt plan reports those
    as null rather than fabricating an address."""
    enabled = network_wanted()
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
    return await local_status_json(OUTPUTD_STATUS_SOCKET)


def _soft_read(
    label: str,
    reader: Callable[[], _T],
    *,
    exc: tuple[type[BaseException], ...] = (Exception,),
) -> _T | None:
    try:
        return reader()
    except exc:
        logger.exception(label)
        return None


def _read_persisted_volume() -> tuple[int | None, float | None]:
    """The persisted listening level and main volume, in that order."""
    from ..volume_coordinator import VolumeState
    from ..volume_persistence import VolumePersistence
    from ..volume_persistence import configured_path as volume_state_path

    record = VolumePersistence(volume_state_path()).load()
    if record is None:
        return None, None
    return (
        VolumeState.from_record(record).effective_percent,
        round(record.main_volume_db, 2)
        if math.isfinite(record.main_volume_db)
        else None,
    )


def _read_sound_profile() -> dict[str, Any]:
    from ..dsp_apply import last_dsp_apply_state
    from ..sound.profile import (
        build_sound_filters,
        estimate_headroom_db,
        load_profile,
    )
    from ..sound.settings import load_sound_settings, output_trim_db

    profile = load_profile()
    sound_settings = load_sound_settings()
    return {
        "enabled": profile.enabled,
        "curve_id": profile.curve_id,
        "simple_eq": profile.simple_eq.to_dict(),
        "parametric_band_count": len(profile.parametric_bands),
        "filter_count": len(build_sound_filters(profile)),
        "headroom_db": estimate_headroom_db(profile),
        "match_loudness": sound_settings.match_loudness,
        "headroom_trim_db": sound_settings.headroom_trim_db,
        "output_trim_db": output_trim_db(profile, sound_settings),
        "updated_at": profile.updated_at or None,
        "last_dsp_apply": last_dsp_apply_state(),
    }


def _spotify_state() -> dict[str, Any]:
    from .. import librespot_state

    blob = librespot_state.read(librespot_state.configured_path())
    return {
        "playing": bool(blob.get("playing", False)),
        "track_id": blob.get("track_id"),
        "uri": blob.get("uri"),
        "session_active": bool(blob.get("session_active", False)),
    }


def _active_source(
    *,
    voice_session: bool,
    mux_status: dict | None,
    spotify_playing: bool,
    airplay_playing: bool | None,
    usbsink_playing: bool,
) -> str:
    """Pick ``/state.active_source``.

    Mux owns the effective audible source in both manual and auto mode; the
    raw renderer probes are a fallback for when mux is unavailable or has no
    selected winner yet.
    """
    mux_effective_source = None
    if isinstance(mux_status, dict):
        raw_selected = mux_status.get("selected_source")
        if isinstance(raw_selected, str):
            mux_effective_source = raw_selected
        else:
            raw_winner = mux_status.get("winner")
            if isinstance(raw_winner, str):
                mux_effective_source = raw_winner

    if voice_session:
        return "voice"
    if mux_effective_source:
        return mux_effective_source
    if spotify_playing:
        return "spotify"
    if airplay_playing:
        return "airplay"
    if usbsink_playing:
        # `playing` is authoritative on both box shapes: solo reads the
        # bridge's RMS-gated flag, combo derives it from the fan-in DIRECT
        # lane's level (audible above the shared -60 dBFS gate), so a combo
        # box streaming silence reads false exactly like solo.
        return "usbsink"
    return "idle"


def _read_audition_state() -> dict[str, Any] | None:
    """Reduced-graph audition record for ``/state.audition``.

    Non-null means somebody is auditioning a reduced graph, so every other
    reading of this speaker's sound is about THAT graph. ``stale`` marks a
    record whose owner died without restoring the applied graph.
    """
    from ..active_speaker.audition import read_audition_state

    state = read_audition_state()
    if state is None:
        return None
    state = dict(state)
    state["stale"] = float(state.get("deadline_at") or 0.0) <= time.time()
    return state


def _read_bass_extension() -> dict[str, Any] | None:
    from ..bass_extension.profile import bass_extension_state_summary

    return bass_extension_state_summary()


def _read_output_hardware() -> dict[str, Any] | None:
    from ..output_hardware import load_state

    hardware = load_state()
    return hardware.to_dict() if hardware is not None else None


def _read_tool_catalog() -> dict[str, Any]:
    from ..tool_catalog_view import summary

    return summary()


def _round_db(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, 2)


def _round_levels(levels: Sequence[float] | None) -> list[float | None] | None:
    """Every channel the running graph carries, not just the front pair.

    An active-crossover box plays four or more physical outputs, and a
    stereo readout would hide entire drivers. The width comes from
    CamillaDSP.
    """
    if levels is None:
        return None
    return [_round_db(v) for v in levels]


async def _camilla_status(*, host: str, port: int) -> dict[str, Any]:
    from ..camilla import CamillaController

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
        cam = CamillaController(host=host, port=port)
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


async def _voice_status(cmd: Callable[..., Any], socket_path: str) -> dict | None:
    try:
        return await cmd(socket_path, "STATUS", timeout=2.0)
    except (OSError, RuntimeError):
        return None


def _ha_status(snapshot: Callable[[], dict[str, Any]] | None) -> dict:
    """HA status for /state via the child-process cache boundary.

    The cache reads the wizard env-file signature fresh, so saves are
    reflected without restarting jasper-control, while HA/httpx imports
    stay in the short-lived probe child instead of the control daemon.
    """
    read = snapshot or _default_ha_status_snapshot
    try:
        return read()
    except Exception:  # noqa: BLE001
        logger.exception("home assistant state snapshot failed")
        return _ha_failed_status()


async def _mux_status(cmd: Callable[..., Any]) -> dict | None:
    try:
        return await cmd("STATUS", timeout=1.0)
    except (OSError, RuntimeError, ValueError):
        return None


async def _aec_status(full_status: Callable[[], dict]) -> dict | None:
    """Additive mirror of GET /aec for one-shot /state consumers."""
    try:
        return await asyncio.to_thread(full_status)
    except Exception:  # noqa: BLE001
        logger.exception("AEC/profile state probe failed")
        return None


class _Probes(NamedTuple):
    """Built positionally: field order IS the gather order, ``ha_status`` last."""

    camilla: dict[str, Any]
    voice: dict | None
    fanin: dict | None
    outputd: dict | None
    mux: dict | None
    aec: dict | None
    ha_status: dict[str, Any]


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
    airplay_playing_snapshot: Callable[[], bool | None] | None = None,
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

    from ..speaker_name import read_state as _read_speaker_name_state
    from ..voice.provider_state import (
        read_active_model_from_env_files,
        read_active_provider_state,
        read_barge_in_enabled,
    )

    # Re-read the wizard-owned SSOT file fresh on every call: jasper-control
    # is NOT restarted on a provider switch (only jasper-voice is), so
    # os.environ here would pin the value to this daemon's start and show a
    # stale provider. ("", None) when unconfigured; never a guessed default.
    active_provider = read_active_provider_state()

    listening_level, persisted_main_volume_db = _soft_read(
        "persisted volume state read failed",
        _read_persisted_volume,
        exc=(OSError, ValueError),
    ) or (None, None)
    sound_profile = _soft_read("sound profile state probe failed", _read_sound_profile)

    ha_status = _ha_status(ha_status_snapshot)
    # The AirPlay health sampler's held MPRIS PlaybackStatus, so no `busctl`
    # runs per request (ADR-0233 rule 2). None when the sampler is absent or
    # has no sample yet; freshness is bounded by its own interval.
    airplay_playing = (
        None if airplay_playing_snapshot is None
        else _soft_read("airplay playing snapshot failed", airplay_playing_snapshot)
    )
    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(
                _camilla_status(host=camilla_host, port=camilla_port),
                _voice_status(voice_socket_command, voice_socket_path),
                local_status_json(FANIN_STATUS_SOCKET),
                _outputd_status(local_status_json=local_status_json),
                _mux_status(mux_socket_command),
                _aec_status(aec_full_status),
            ),
            timeout=_STATE_AGGREGATE_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        # A probe blew past its own ceiling. Fail loud (the handler turns this
        # into a 502) rather than hang a bounded worker forever; the cheap
        # /healthz probe stays answerable so this can't manufacture a reboot.
        log_event(
            logger,
            "state.aggregate_timeout",
            budget_sec=_STATE_AGGREGATE_BUDGET_SEC,
            level=logging.WARNING,
        )
        raise
    probes = _Probes(*gathered, ha_status=ha_status)

    spotify = _spotify_state()
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            probes.camilla.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Top-level aliases for consumers that need only the running truth
        # and do not want to parse the nested runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]
    speaker_name_state = _read_speaker_name_state()

    # USB Audio Input — fourth renderer. Fan-in owns the live DIRECT lane;
    # kernel UDC state owns host connection.
    usbsink_state = _build_usbsink_renderer_state(
        probes.fanin,
        host_connected=udc_host_connected(
            os.environ.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR),
        ),
    )

    voice_status = probes.voice or {}
    voice_session = bool(probes.voice) and voice_status.get("state") == "SESSION"
    active_source = _active_source(
        voice_session=voice_session,
        mux_status=probes.mux,
        spotify_playing=spotify["playing"],
        airplay_playing=airplay_playing,
        usbsink_playing=bool(usbsink_state and usbsink_state.get("playing")),
    )

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=probes.camilla["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=probes.mux,
        diagnostics=_read_volume_diagnostics(),
    )

    grouping_state = with_airplay_latency_fit(_soft_read(
        "grouping state read failed",
        lambda: read_grouping_state(local_outputd_reader=lambda: probes.outputd),
    ))
    active_speaker_setup = _soft_read(
        "active speaker setup status read failed",
        lambda: read_active_speaker_setup_status(
            active_config_path=probes.camilla.get("active_config_path"),
        ),
        exc=(OSError, RuntimeError, TypeError, ValueError, KeyError),
    )
    audition_state = _soft_read(
        "audition state read failed",
        _read_audition_state,
        exc=(ImportError, OSError, RuntimeError, TypeError, ValueError),
    )
    bass_extension_state = _soft_read(
        "bass extension profile state read failed",
        _read_bass_extension,
        exc=(
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
        ),
    )
    transit_state = _soft_read("transit state read failed", read_transit_state_func)
    output_hardware_state = _soft_read(
        "output hardware state read failed", _read_output_hardware,
    )
    service_states = (
        _soft_read("service state snapshot read failed", service_states_snapshot)
        if service_states_snapshot
        else None
    )

    audio_graph_state = _audio_graph_state(
        fanin_status=probes.fanin,
        outputd_status=probes.outputd,
        service_states=service_states,
    )
    tools_state = _soft_read("tool catalog state read failed", _read_tool_catalog)

    # Conversation history is a read-only Feature surface. Settings are
    # wizard-owned and read fresh; the SQLite store is opened read-only so
    # jasper-control cannot create or mutate jasper-voice's DB.
    chat_state = _soft_read(
        "conversation history state read failed",
        _conversation_history_state,
        exc=(ImportError, OSError, RuntimeError, ValueError),
    )
    research_state = _soft_read(
        "research state read failed",
        lambda: _research_state(voice_status.get("research")),
        exc=(ImportError, OSError, RuntimeError, ValueError),
    )

    # Lazy import (mirrors read_active_provider_state above) so jasper-control
    # doesn't pull jasper.voice.* at module load.
    from ..mic_presence import read_mic_presence
    mic_presence = read_mic_presence()

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            # active_provider.model sees only the wizard file; the merged
            # reader adds jasper.env, the same set jasper-voice sources.
            # See issue #3133.
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
            "reachable": probes.voice is not None,
            # Disambiguates reachable:false: true means the AEC reconciler
            # parked voice for a missing microphone ("intentionally idle, no
            # mic", NOT "crashed"). Same read as the `microphone` block below,
            # so the boolean and the rich record cannot disagree.
            "parked_no_mic": mic_presence.parked,
        },
        # The reconciler's one canonical mic record (jasper.mic_presence), so a
        # client renders "no microphone" as a fact rather than inferring it
        # from voice.reachable:false.
        "microphone": mic_presence.as_dict(),
        "audio": {
            "main_volume_db": probes.camilla["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": probes.camilla["playback_rms_dbfs"],
            "playback_peak_dbfs": probes.camilla["playback_peak_dbfs"],
            "clipped_samples": probes.camilla["clipped_samples"],
            "camilla_active_config_path": probes.camilla["active_config_path"],
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
                None if airplay_playing is None else {"playing": airplay_playing}
            ),
            # null when the feature is disabled (no state file), so a
            # consumer can show "off" as distinct from "idle".
            "usbsink": usbsink_state,
        },
        "speaker_name": {
            "name": speaker_name_state.name,
            "source": speaker_name_state.source,
        },
        "active_source": active_source,
        # Fan-in's UDS STATUS snapshot, verbatim. null only when the
        # daemon/socket is unavailable.
        "fanin": probes.fanin,
        # Final-output owner; jasper-doctor owns the actionable failure.
        "outputd": probes.outputd,
        # Additive mirror of GET /aec, so a one-shot /state consumer sees
        # requested intent vs observed runtime without a second request.
        "aec": probes.aec,
        "source_selection": probes.mux,
        "resilience": {
            "shairport": shairport_supervisor.snapshot(),
            # Bonded-member runtime liveness: dac_content starvation watch
            # + snapcast binding read-repair. Off via
            # JASPER_GROUPING_SUPERVISOR=disabled.
            "grouping_supervisor": grouping_supervisor.snapshot(),
            # Userspace-liveness supervisor: probes sshd / our own HTTP /
            # /proc/loadavg every 30 s and clean-reboots after 3 consecutive
            # failures (rate-limited 1/24h). Off via
            # JASPER_SYSTEM_SUPERVISOR=disabled.
            "system_supervisor": system_supervisor.snapshot(),
            # Cross-boot circuit breaker for the StartLimitAction=reboot
            # ladder. {"ran": false} when the oneshot hasn't run this boot;
            # tripped=true means reboot escalation is disarmed for this boot —
            # fix the failing daemon, then reboot to re-arm.
            "bootloop_guard": bootloop_guard_state.snapshot(),
            # jasper-camilla-recover's core-graph park record (ADR-0175).
            # parked=true means CamillaDSP was stopped out-of-band after a
            # failed recovery pass: the speaker emits NOTHING and nothing
            # re-arms it — the record's own `action`/`re_arm` are the remedy.
            # {"status": "absent"} on a healthy boot. Same reader
            # jasper-doctor's check_camilla_recover_park uses.
            "camilla_recover": camilla_recover_state.snapshot(),
            # jasper-outputd's ExecStopPost park record. parked=true means the
            # stop helper judged the failure terminal: outputd owns the DAC
            # write loop, so the speaker emits NOTHING until the output env is
            # fixed and the unit restarted. Same reader jasper-doctor's
            # check_outputd_failure_reconcile_park uses.
            "outputd_failure_reconcile": outputd_failure_reconcile_state.snapshot(
                (service_states or {}).get(outputd_failure_reconcile_state.UNIT),
            ),
            # The four named parks of the one-audio-transport rule (ADR-0178).
            # Read from the audio-health sampler's cached verdict, and by the
            # same reader jasper-doctor's check_ring_transport_park uses, so
            # the three surfaces cannot disagree.
            "transport_park": transport_park_snapshot(),
            # Bounded after-the-fact timeline for multiroom restart cascades,
            # scanned from event=multiroom.reconcile.*, restart_broker.* and
            # grouping_supervisor.* journal lines.
            "multiroom_cascade": _multiroom_cascade_snapshot(),
            # Effective mDNS identity (jasper-identity-reconcile, boot + 5-min
            # timer). status=collision means Avahi renamed us because another
            # device owns our hostname; the household should pick a unique
            # name. {"status": "absent"} pre-first-run.
            "identity": identity_state.snapshot(),
            # Root-filesystem fullness; jasper-doctor's check_disk_space owns
            # the warn(>=85%)/fail(>=95%) thresholds.
            "disk": _disk_snapshot(),
            # Speaker-setup PARKED state (#2135): an unconfigured or
            # declared-but-uncommissioned topology holds silence rather than
            # allow an inferred flat graph.
            "active_speaker_parked": _active_speaker_parked_snapshot(),
            # jasper-input stays `active` while one bridge loops in restart
            # backoff (ADR-0225); this is the only non-journal sign of it.
            "accessory_bridges": accessory_status.snapshot(),
        },
        "home_assistant": probes.ha_status,
        # Snapshot of the wizard-owned grouping.env plus airplay_latency_fit
        # ({applicable: false} unless this speaker is an active bonded leader).
        # enabled=True with a non-null error is the fail-LOUD "configured but
        # broken" state. See jasper/multiroom/state.py + airplay_latency.py.
        "grouping": grouping_state,
        # {packs: [{id, label, enabled}]} read fresh from the wizard-owned
        # transit.env. Mirrors the daemon's enabled_pack_ids on both absent
        # (all) and present-empty (none). See jasper/transit/state.py.
        "transit": transit_state,
        # Which subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
        # Read fresh from /run/jasper/tools.json + the wizard-owned
        # tool_state.env by jasper.tool_catalog_view (never os.environ).
        # jasper-doctor's check_tool_catalog owns the actionable warn.
        "tools": tools_state,
        # null when the read-side store is unavailable while capture is
        # enabled, or the read itself failed. See jasper.conversation_history.
        "chat": chat_state,
        # Async research summary. Counts and timestamps only; no prompt or
        # answer text leaves the local store through /state.
        "research": research_state,
        # The open measurement window as this process sees it — an in-memory
        # read of its own self-expiring copy, not a probe. `held_for_s` is
        # what jasper-doctor's check_measurement_hold reads: `expires_in_s`
        # resets on every renewal and so can never reveal a stuck hold.
        "measurement": measurement_hold.snapshot(),
        # The default-on, hardware-gated NCM link on usb0 that keeps
        # http://<JASPER_HOSTNAME>/ reachable with WiFi off when the resolved
        # USB role permits gadget mode.
        "usb_network": _usb_network_snapshot(),
    }
