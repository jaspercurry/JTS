# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The four named parks of the one-audio-transport rule.

ADR-0100 makes ``shm_ring`` the only central transport and says a topology the
ring cannot serve **parks loudly** — doctor FAIL, ``/state``, web banner —
naming its tracked issue. [ADR-0178](../../docs/adr/0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name.md)
names the four shapes and why each is a class; this module is the one place
that answers *which* park a box is in, so the three operator/household
surfaces cannot disagree about it (the shape ``camilla_recover_state``
already uses for its own out-of-band record).

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

**One signal that is not a park.** :func:`snapshot`'s ``unproven_endpoint``
names the coverage seam ADR-0184 records — a box whose wide-ring width
resolves with no armed endpoint and no class to name it. It carries neither
an issue nor a remedy, which is exactly why ADR-0178 refuses it a class, and
it stops at the operator surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from ..env_load import merged_env_files

if TYPE_CHECKING:
    from ..output_topology import OutputTopology

#: Park class tokens. Structured, matched by tests and by the web surface;
#: the human prose beside them is presentation. See ADR-0178 for what each
#: shape is and why it is a class of its own.
PARK_PASSIVE_STEREO_COMPOSITE = "passive_stereo_composite"
PARK_MONO_FULL_RANGE = "mono_full_range"
PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED = "roleful_active_endpoint_unconverged"
PARK_GROUPED_DAC_CONTENT_LANE = "grouped_dac_content_lane"

#: The tracked rebuild issue each shape waits on, in the tree's ``#NNNN``
#: spelling. ``roleful_active_endpoint_unconverged`` has none on purpose: it
#: needs no rebuild, only the ladder below.
ISSUE_COMPOSITE_ON_RING = "#2982"
ISSUE_MONO_ON_RING = "#3117"
ISSUE_GROUPED_ON_RING = "#3118"

#: The recorded remedy for an unconverged ACTIVE endpoint. Owner ruling
#: 2026-08-26: a recorded command, not a new reconciler rung.
#:
#: BOTH steps, because the first one alone does not clear this park.
#: ``baseline-reemit --endpoint ring`` moves the GRAPH; the endpoint marker
#: this park reads has exactly one writer, ``jasper-audio-hardware-reconcile``,
#: which derives it from that graph. Recording only step 1 would send an
#: operator to re-run the doctor and find the identical park still there —
#: the dead-remedy defect this campaign is removing, one level in.
#:
#: The doctor's fan-in coupling check prescribes the same ladder plus its own
#: third step and composes it FROM this constant, so the two surfaces cannot
#: drift apart while both live.
ACTIVE_ENDPOINT_REMEDY = (
    "sudo /opt/jasper/.venv/bin/jasper-active-speaker baseline-reemit "
    "--endpoint ring && sudo systemctl start jasper-audio-hardware-reconcile"
)


def _outputd_env() -> dict[str, str]:
    """outputd's persistent env, read fresh through its own layering.

    The two paths and the lane keys are taken from the modules that own them
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


@dataclass(frozen=True)
class _Assessment:
    """One pass over the topology + env: the parks, and the honest silence."""

    parks: tuple[TransportPark, ...]
    #: Configured, yet the eligibility SSOT resolved NO ring geometry of
    #: either kind. When no class then names it, the box is neither servable
    #: nor named, and saying "the ring can serve this box" would be false.
    ring_unresolved: bool
    #: The wide ring resolves a width for this box, nothing has armed the
    #: endpoint that would carry it, and no park class covers the gap — the
    #: coverage seam [ADR-0184](../../docs/adr/0184-a-resolvable-width-with-no-armed-endpoint-signals-rather-than-parks.md)
    #: records. Operator-only: NOT a park, NOT a household claim.
    unproven_endpoint: bool


def ring_only_transport() -> bool:
    """Is the ring the only central transport on this tree yet?

    DERIVED from the coupling vocabulary rather than carried as a flag, so the
    transport-deletion slice flips this by deleting the loopback coupling —
    there is no second edit to forget and no dead knob left behind. While
    ``loopback`` is still a legal coupling, a box in one of the four classes
    still has a working route and must not be reported as silent.

    A fan-in COUPLING vocabulary correctly gates a CONTENT-BRIDGE park
    (``grouped_dac_content_lane``) because
    :func:`jasper.audio_runtime_plan.coupling_supported_for_route` already
    joins the two: it blocks the ``shm_ring`` coupling exactly where the
    dac_content lane is armed, so "loopback is gone" and "the armed lane has
    nowhere left to run" are one fact seen from two ends.
    """
    from ..fanin_coupling import COUPLING_SHM_RING, VALID_COUPLINGS

    return set(VALID_COUPLINGS) == {COUPLING_SHM_RING}


def _assess(
    topology: "OutputTopology | None",
    env: Mapping[str, str] | None,
) -> _Assessment:
    from ..active_speaker.runtime_contract import (
        CONTRACT_NORMAL_MONO_FULL_RANGE,
        active_ring_channels_for_topology,
        classify_output_contract,
        ring_channels_for_topology,
        topology_sink_is_composite,
    )
    from ..fanin_coupling import OUTPUTD_ENV_BOOL_TRUE, ring_active_endpoint_armed
    from ..multiroom.dac_content_ring import DAC_CONTENT_LANE_ENV
    from ..multiroom.reconcile import OUTPUTD_DAC_CONTENT_FIFO_ENV
    from ..output_topology import load_output_topology_strict

    if topology is None:
        # STRICT, not the fail-soft loader: that one degrades a corrupt or
        # unreadable topology to an empty draft, which classifies as
        # not-configured and would report a box with a rotted topology file as
        # a healthy speaker on all three surfaces. Raising here is what makes
        # this module's documented "unavailable" posture reachable. Missing is
        # still an empty draft — a fresh box must not park on never-configured.
        topology = load_output_topology_strict()
    if env is None:
        env = _outputd_env()

    contract = classify_output_contract(topology)
    stereo_ring = ring_channels_for_topology(topology)
    active_ring = active_ring_channels_for_topology(topology)
    ring_unresolved = bool(
        contract.topology_configured and stereo_ring is None and active_ring is None
    )

    parks: list[TransportPark] = []

    if ring_unresolved:
        # The eligibility SSOT resolved NO ring geometry of either kind for
        # this saved layout. Name which shape it is; a shape with no name here
        # leaves `ring_unresolved` standing and is disclosed as unclassified
        # rather than reported servable.
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

    endpoint_armed = ring_active_endpoint_armed(env)

    # ACTIVE-CROSSOVER boxes only, not every roleful one. `requires_roleful_graph`
    # is also True for a passive stereo box that merely adds a subwoofer or a
    # protected output, and those have no active-speaker baseline to re-emit —
    # parking them would hand the household a remedy that cannot run. The
    # narrower `active_modes` is the fact the remedy actually needs.
    if active_ring is not None and contract.active_modes and not endpoint_armed:
        parks.append(
            TransportPark(
                park_class=PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED,
                issue=None,
                remedy=ACTIVE_ENDPOINT_REMEDY,
                detail=(
                    f"this active-crossover box resolves a {active_ring}-channel "
                    "ACTIVE ring, but its endpoint marker has not converged onto "
                    "it, so nothing carries the post-crossover program"
                ),
            )
        )

    # EITHER spelling arms the ONE round-trip lane, so both are this class —
    # but no longer for one reason (``rust/jasper-outputd/src/config.rs``). The
    # FIFO half needs ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct``, which its own
    # grouping writer no longer emits. The MARKER half now parses instead: it
    # SELECTS the return ring as the sole content source, and is armed ahead of
    # the reconciler that writes its producer. Reading the FIFO key alone would
    # leave a marker-armed box with no class naming it here.
    #
    # Each key is read with the semantics outputd reads it with: the FIFO is a
    # PATH, non-empty rather than present, because the grouping reconciler
    # writes it as an EMPTY string off-bond and a cleared bond leaves the key
    # behind; the marker is a BARE flag, so ``=0`` is not armed.
    #
    # EXPIRY: the FIFO half of this test dies with the FIFO arm itself, in the
    # deletion PR that follows a bonded pair playing through the ring on metal.
    fifo_armed = bool((env.get(OUTPUTD_DAC_CONTENT_FIFO_ENV) or "").strip())
    ring_armed = (
        env.get(DAC_CONTENT_LANE_ENV) or ""
    ).strip().lower() in OUTPUTD_ENV_BOOL_TRUE
    if fifo_armed or ring_armed:
        parks.append(
            TransportPark(
                park_class=PARK_GROUPED_DAC_CONTENT_LANE,
                issue=ISSUE_GROUPED_ON_RING,
                remedy=None,
                detail=(
                    "this box is a bonded grouping member whose round-trip "
                    "dac_content lane has no producer: the FIFO half needs a "
                    "content bridge its writer no longer emits, and the marker "
                    "half is armed ahead of the reconciler that serves its ring"
                ),
            )
        )

    # The coverage seam ADR-0184 records, and the ONLY new fact this module
    # reports beyond the four classes. It is deliberately the complement of the
    # class-(c) condition above — same width, same marker, `active_modes`
    # inverted — so the two can never both describe one box and ADR-0178's
    # double-report objection does not apply.
    unproven_endpoint = bool(
        active_ring is not None and not contract.active_modes and not endpoint_armed
    )

    return _Assessment(
        parks=tuple(parks),
        ring_unresolved=ring_unresolved,
        unproven_endpoint=unproven_endpoint,
    )


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
    (#2135) and is not this module's to re-report. It is NOT by itself proof
    the ring can serve the box; :func:`snapshot` carries that distinction as
    ``unclassified``.

    Raises ``OutputTopologyError`` on a corrupt or unreadable topology when
    ``topology`` is not supplied; :func:`snapshot` is the fail-soft caller.
    """
    return _assess(topology, env).parks


def snapshot(
    topology: "OutputTopology | None" = None,
    env: Mapping[str, str] | None = None,
    *,
    ring_only: bool | None = None,
) -> dict[str, Any]:
    """Fail-soft park verdict for jasper-doctor, ``/state`` and the web card.

    Shapes, discriminated by ``status``:

    ``{"status": "ok", "parked": False, "parks": []}``
        The ring resolves a geometry for this box, or it is not configured yet.

    ``{"status": "pending", "parked": False, "parks": [...]}``
        This box is in one or more of the four classes, but the loopback route
        still exists and still carries it. Disclosed to the OPERATOR (doctor
        warn, ``/state``) and deliberately NOT to the household: the speaker
        plays, and the household surface must not call a working speaker
        silent. This is the fleet inventory the transport deletion needs.

    ``{"status": "parked", "parked": True, "parks": [...]}``
        Ring-only, and no ring serves this box: it emits NOTHING. Loud on
        every surface.

    ``{"status": "unclassified", "parked": False, "parks": []}``
        Configured, no ring geometry of either kind, and none of the four
        classes names it — a mid-commissioning layout with an unassigned lane,
        say. NOT a fifth class and not a household incident; it is the refusal
        to claim "the ring can serve this box" about a box the ring demonstrably
        cannot serve, which is the false quiet ADR-0100 exists to prevent.

    ``{"status": "unavailable", "parked": False, "parks": [], "error": ...}``
        Topology or env could not be read. Reported distinctly rather than as
        a healthy box, the same posture the other park readers hold.

    ``unproven_endpoint`` rides alongside ``status``, never inside it: it is
    the ADR-0184 coverage seam — a box whose wide-ring width resolves with no
    armed endpoint and no class to name it — and it is an OPERATOR fact only.
    It does not change any status, add a park, or reach the household card.
    Always present, ``False`` when it cannot be assessed.

    Never raises.
    """
    resolved_ring_only = ring_only_transport() if ring_only is None else ring_only
    try:
        assessment = _assess(topology, env)
    except Exception as exc:  # noqa: BLE001 - a park reader must not raise here
        return {
            "status": "unavailable",
            "parked": False,
            "ring_only": resolved_ring_only,
            "parks": [],
            "unproven_endpoint": False,
            "error": str(exc),
        }

    parks = assessment.parks
    if parks:
        status = "parked" if resolved_ring_only else "pending"
    elif assessment.ring_unresolved:
        status = "unclassified"
    else:
        status = "ok"

    return {
        "status": status,
        "parked": status == "parked",
        "ring_only": resolved_ring_only,
        "parks": [park.to_dict() for park in parks],
        "unproven_endpoint": assessment.unproven_endpoint,
    }
