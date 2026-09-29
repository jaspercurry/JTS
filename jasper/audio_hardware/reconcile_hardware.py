# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the reconcile pass starts from: the classifier's record of the
attached output hardware, the management-transport marker it publishes, and
the role policy that turns the record into the pass's output DAC.

Each step takes the pass and leaves its verdict in the pass's state for the
steps after it; ``Pass.execute`` decides the order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jasper.audio_hardware.output_probe import observe
from jasper.audio_hardware.reconcile_common import _ensure_dir, _log_token
from jasper.audio_routes.output_hardware import ObservedOutput
from jasper.dsp_control.output_topology_observation import observed_output

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass

#: ALSA card ids are not stable across a re-enumeration, so the Apple mixer
#: units bake in "resolve it yourself" rather than a card this pass observed.
APPLE_SERVICE_CARD_AUTO = "auto"


def observe_output_hardware_state(run: Pass, *, write: bool) -> None:
    action = "written" if write else "observed"
    try:
        state, cards, record_changed = observe(write=write)
        observed = observed_output(state, cards, record_changed=record_changed)
    # noqa reason: the classifier walks sysfs, /proc and an `aplay` spawn; a
    # failure of ANY shape must still leave the DAC-role policy below to run.
    except Exception:  # noqa: BLE001
        run.mark_degraded()
        run.log(f"state_{action}_failed", path=run.state_path)
        return
    # A record missing either of the two facts the whole thing hangs off is
    # not one anything below may read.
    if not observed.valid:
        run.observed = ObservedOutput()
        run.log(
            f"state_{action}_failed",
            reason="invalid_payload",
            path=run.state_path,
        )
        return
    run.observed = observed
    if observed.record_changed:
        run.record_changed = True
    if observed.apple_card_ids:
        run.dongle_card = observed.apple_card_ids[0]
        run.apple_dongle_present = True
    if write:
        # Never fatal: a full /run must not skip the DAC-role policy the
        # caller runs next. --print-env promises no mutations, which is why
        # this is gated on the write.
        try:
            publish_management_transport_marker(
                run, observed.management_transport_available
            )
        except OSError:
            pass
    run.log(
        f"state_{action}",
        path=run.state_path,
        profile_id=observed.profile_id or "unknown",
        status=observed.status or "unknown",
        blockers=_log_token(",".join(observed.blocker_codes) or "none"),
    )


def publish_management_transport_marker(run: Pass, available: bool | None) -> None:
    """Read by jasper-usbgadget's composition with ``test -e``. In /run so
    a reboot clears it before the boot config it describes can change.
    Truncated in place rather than replaced: an unlink would leave a window
    where a true->true republish reads as false."""
    marker = run.management_transport_marker
    if not available:
        marker.unlink(missing_ok=True)
        return
    _ensure_dir(marker.parent, 0o755)
    marker.open("w").close()


def apply_observed_single_policy(run: Pass) -> None:
    """Consume the classifier's verdict for ordinary single devices, so a
    newly registered DAC needs no second hardware rule here."""
    if run.observed.status != "ready":
        return
    if not run.observed.selected_card_id:
        return
    run.output_dac_card = run.observed.selected_card_id
    run.output_dac_id = run.observed.profile_id
    run.output_dac_recognized = True


def apply_observed_composite_policy(run: Pass) -> None:
    if run.observed.kind != "composite":
        return
    # The parked shape, up front: a composite is NAMED whatever its status,
    # so every branch leaves these exactly here except the one that arms.
    run.output_dac_id = run.observed.profile_id
    run.output_dac_card = ""
    run.output_dac_recognized = False
    run.apple_dongle_present = True
    run.apple_dongle_service_card = APPLE_SERVICE_CARD_AUTO
    if run.observed.status != "ready":
        run.log(
            "dual_apple_detected",
            status=run.observed.status or "unknown",
            action="park_until_ready",
        )
        return
    if not run.observed.dual_mapping_ok:
        run.log(
            "dual_apple_detected",
            status="ready",
            action="park_unstable_child_order",
            topology_path=run.output_topology_path,
            reason=run.observed.dual_mapping_reason
            or "unknown",
        )
        return
    run.dual_apple_order_source = run.observed.dual_order_source
    run.dual_apple_dac_a_pcm = run.observed.dual_dac_a_pcm
    run.dual_apple_dac_b_pcm = run.observed.dual_dac_b_pcm
    # The composite sink is rigidly 4-channel in outputd, so the dual
    # branch needs the gate's pass/fail and its ENDPOINT DEVICE but not the
    # returned width. The endpoint field must not be discarded: the marker
    # `active_ring_endpoint_proof` demands is derived from exactly it.
    ok, payload = run.active_graph_status(4)
    if not ok:
        run.dual_apple_dac_a_pcm = ""
        run.dual_apple_dac_b_pcm = ""
        run.dual_apple_active_endpoint_device = ""
        run.log(
            "dual_apple_detected",
            status="ready",
            action="park_until_active_graph",
            reason=payload,
        )
        return
    run.output_dac_card = run.dongle_card
    run.output_dac_recognized = True
    run.dual_apple_active_endpoint_device = payload[1]
    run.log(
        "dual_apple_detected",
        status="ready",
        action="outputd_dual_sink",
        order_source=run.dual_apple_order_source,
        dac_a_pcm=_log_token(run.dual_apple_dac_a_pcm),
        dac_b_pcm=_log_token(run.dual_apple_dac_b_pcm),
        active_endpoint=run.dual_apple_active_endpoint_device or "none",
    )
