# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The four named parks of the one-audio-transport rule (ADR-0178).

ADR-0100 makes ``shm_ring`` the only central transport and says a topology the
ring cannot serve **parks loudly** — doctor FAIL, ``/state``, web banner —
naming its tracked issue. This module is the one place that answers *which*
park a box is in, so the three operator/household surfaces cannot disagree
about it (the shape ``content_lane_state`` and ``camilla_recover_state``
already use for their own out-of-band records).

Four classes, and no fifth without an ADR:

``passive_stereo_composite``
    A multi-child (dual-DAC) sink carrying a passive full-range program. No
    Ring B (the ring carries one coherent stereo sink) and no ACTIVE ring (it
    is not roleful). Rebuilt on the ring under #2982. A **roleful** composite
    is deliberately NOT in this class: its post-crossover program rides the
    ACTIVE ring and jts.local runs exactly that shape today.
``mono_full_range``
    A declared 1-channel full-range layout. The ring layout's accept-set
    starts at two channels, so no ring geometry is representable at all.
    Rebuilt under #3117. Active-crossover **mono** (roleful, 2+ channels) is
    ring-eligible and is not in this class.
``roleful_active_endpoint_unconverged``
    A roleful box whose ACTIVE ring width resolves but whose endpoint marker
    has not converged onto it. The only class with an immediate remedy rather
    than a rebuild issue — :data:`ACTIVE_ENDPOINT_REMEDY`, one command.
``grouped_dac_content_lane``
    A bonded/grouped member whose round-trip ``dac_content`` lane is armed.
    That lane pins ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct`` and outputd
    refuses it against ``shm_ring``, so it is mutually exclusive with the ring
    by construction. Rebuilt under #3118.

**Eligibility is read, never restated.** ``ring_channels_for_topology`` /
``active_ring_channels_for_topology`` in
:mod:`jasper.active_speaker.runtime_contract` own the question "can a ring
carry this topology, and how wide"; this module only NAMES the refusal they
already return. A predicate here that re-derived ring eligibility would be the
second implementation that drifts.

**Freshness.** Topology and env are re-read per call, never sourced from
``os.environ``: jasper-control is not restarted when a bond forms or a
reconciler rewrites ``outputd.env``, so a value captured at import would be
permanently wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from ..env_load import merged_env_files

if TYPE_CHECKING:
    from ..output_topology import OutputTopology

#: Park class tokens. Structured, matched by tests and by the web surface;
#: the human prose beside them is presentation and may be reworded freely.
PARK_PASSIVE_STEREO_COMPOSITE = "passive_stereo_composite"
PARK_MONO_FULL_RANGE = "mono_full_range"
PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED = "roleful_active_endpoint_unconverged"
PARK_GROUPED_DAC_CONTENT_LANE = "grouped_dac_content_lane"

#: The tracked rebuild issue each shape waits on, in the tree's ``#NNNN``
#: spelling. ``roleful_active_endpoint_unconverged`` has none on purpose: it
#: needs no rebuild, only the one command below.
ISSUE_COMPOSITE_ON_RING = "#2982"
ISSUE_MONO_ON_RING = "#3117"
ISSUE_GROUPED_ON_RING = "#3118"

#: The recorded one-command remedy for an unconverged ACTIVE endpoint. Owner
#: ruling 2026-08-26: a recorded command, not a new reconciler rung.
ACTIVE_ENDPOINT_REMEDY = "jasper-active-speaker baseline-reemit --endpoint ring"


def _outputd_env() -> dict[str, str]:
    """outputd's persistent env, read fresh through its own layering.

    The two paths and the FIFO key are taken from the modules that own them
    rather than respelled here: ``OUTPUTD_ENV_PATH`` is the same constant
    :func:`~jasper.fanin_coupling.ring_active_endpoint_armed` reads when it is
    given no mapping, so the endpoint marker this reads and the marker that
    predicate reads cannot come from different files. Layer order is the
    unit's own ``EnvironmentFile=`` order
    (``deploy/systemd/jasper-outputd.service``), so the grouping file's pins
    win here exactly as they win for outputd itself.
    """
    from ..fanin.coupling_reconcile import OUTPUTD_ENV_PATH
    from ..multiroom.reconcile import OUTPUTD_GROUPING_ENV_FILE

    return merged_env_files((OUTPUTD_ENV_PATH, OUTPUTD_GROUPING_ENV_FILE))


@dataclass(frozen=True)
class TransportPark:
    """One named park: the class token, its tracked issue, its remedy."""

    park_class: str
    issue: str | None
    remedy: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "park_class": self.park_class,
            "issue": self.issue,
            "remedy": self.remedy,
            "detail": self.detail,
        }


def ring_only_transport() -> bool:
    """Is the ring the only central transport on this tree yet?

    DERIVED from the coupling vocabulary rather than carried as a flag, so the
    transport-deletion slice flips this by deleting the loopback coupling —
    there is no second edit to forget and no dead knob left behind. While
    ``loopback`` is still a legal coupling, a box in one of the four classes
    below still has a working route and must not be reported as silent.
    """
    from ..fanin_coupling import COUPLING_SHM_RING, VALID_COUPLINGS

    return set(VALID_COUPLINGS) == {COUPLING_SHM_RING}


def classify(
    topology: "OutputTopology | None" = None,
    env: Mapping[str, str] | None = None,
) -> tuple[TransportPark, ...]:
    """Every park this box is in, in the order ADR-0178 lists them.

    A TUPLE rather than one winning verdict: the classes answer different
    questions (three topology shapes, one runtime pin) and a box can genuinely
    be in two — a bonded mono speaker is waiting on both #3117 and #3118. A
    single verdict would force an invented precedence and hide the other
    tracked issue from the operator who has to clear both.

    ``()`` — no park — is the answer for every ring-eligible box, and for an
    UNCONFIGURED topology, which holds silence through the speaker-setup park
    (#2135) and is not this module's to re-report.
    """
    from ..active_speaker.runtime_contract import (
        CONTRACT_NORMAL_MONO_FULL_RANGE,
        active_ring_channels_for_topology,
        classify_output_contract,
        ring_channels_for_topology,
        topology_sink_is_composite,
    )
    from ..fanin_coupling import ring_active_endpoint_armed
    from ..multiroom.reconcile import OUTPUTD_DAC_CONTENT_FIFO_ENV
    from ..output_topology import load_output_topology

    if topology is None:
        topology = load_output_topology()
    if env is None:
        env = _outputd_env()

    contract = classify_output_contract(topology)
    stereo_ring = ring_channels_for_topology(topology)
    active_ring = active_ring_channels_for_topology(topology)

    parks: list[TransportPark] = []

    if contract.topology_configured and stereo_ring is None and active_ring is None:
        # The eligibility SSOT resolved NO ring geometry of either kind for
        # this saved layout. Name which shape it is; a shape with no name here
        # is out of ADR-0178's four classes and gets no park from this module.
        if topology_sink_is_composite(topology) and not contract.requires_roleful_graph:
            parks.append(
                TransportPark(
                    park_class=PARK_PASSIVE_STEREO_COMPOSITE,
                    issue=ISSUE_COMPOSITE_ON_RING,
                    remedy=None,
                    detail=(
                        "this box's sink spans two child DACs carrying a "
                        "passive full-range program, which is neither the "
                        "single coherent stereo sink Ring B drives nor a "
                        "roleful graph the ACTIVE ring carries"
                    ),
                )
            )
        elif contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE:
            parks.append(
                TransportPark(
                    park_class=PARK_MONO_FULL_RANGE,
                    issue=ISSUE_MONO_ON_RING,
                    remedy=None,
                    detail=(
                        "this box declares a 1-channel full-range layout and "
                        "the ring layout's accept-set starts at 2 channels, "
                        "so no ring geometry exists for it"
                    ),
                )
            )

    if active_ring is not None and not ring_active_endpoint_armed(env):
        parks.append(
            TransportPark(
                park_class=PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED,
                issue=None,
                remedy=ACTIVE_ENDPOINT_REMEDY,
                detail=(
                    f"this roleful box resolves a {active_ring}-channel ACTIVE "
                    "ring, but its endpoint marker has not converged onto it, "
                    "so nothing carries the post-crossover program"
                ),
            )
        )

    # Non-empty is the arming test, not presence: the grouping reconciler
    # writes this key as an EMPTY string when the speaker is not an active
    # member, so a cleared bond leaves the key behind.
    if (env.get(OUTPUTD_DAC_CONTENT_FIFO_ENV) or "").strip():
        parks.append(
            TransportPark(
                park_class=PARK_GROUPED_DAC_CONTENT_LANE,
                issue=ISSUE_GROUPED_ON_RING,
                remedy=None,
                detail=(
                    "this box is a bonded grouping member whose round-trip "
                    "dac_content lane pins the direct content bridge, which "
                    "outputd refuses against shm_ring"
                ),
            )
        )

    return tuple(parks)


def snapshot(
    topology: "OutputTopology | None" = None,
    env: Mapping[str, str] | None = None,
    *,
    ring_only: bool | None = None,
) -> dict[str, Any]:
    """Fail-soft park verdict for jasper-doctor, ``/state`` and the web card.

    Three shapes, discriminated by ``status``:

    ``{"status": "ok", "parked": False, "parks": []}``
        The ring can serve this box.

    ``{"status": "pending", "parked": False, "parks": [...]}``
        This box is in one or more of the four classes, but the loopback route
        still exists and still carries it. Disclosed to the OPERATOR (doctor
        warn, ``/state``) and deliberately NOT to the household: the speaker
        plays, and the household surface must not call a working speaker
        silent. This is the fleet inventory the transport deletion needs.

    ``{"status": "parked", "parked": True, "parks": [...]}``
        Ring-only, and no ring serves this box: it emits NOTHING. Loud on
        every surface.

    ``{"status": "unavailable", "parked": False, "parks": [], "error": ...}``
        Topology or env could not be read. Reported distinctly rather than as
        a healthy box, the same posture the other park readers hold.

    Never raises.
    """
    resolved_ring_only = ring_only_transport() if ring_only is None else ring_only
    try:
        parks = classify(topology, env)
    except Exception as exc:  # noqa: BLE001 - a park reader must not raise here
        return {
            "status": "unavailable",
            "parked": False,
            "ring_only": resolved_ring_only,
            "parks": [],
            "error": str(exc),
        }

    if not parks:
        status = "ok"
    elif resolved_ring_only:
        status = "parked"
    else:
        status = "pending"

    return {
        "status": status,
        "parked": status == "parked",
        "ring_only": resolved_ring_only,
        "parks": [park.to_dict() for park in parks],
    }
