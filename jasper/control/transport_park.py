# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The four named parks of the one-audio-transport rule.

ADR-0100 makes ``shm_ring`` the only central transport and says a topology the
ring cannot serve **parks loudly**, naming its tracked issue.
[ADR-0178](../../docs/adr/0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name.md)
names the four shapes and why each is a class; this module is the one place
that answers *which* park a box is in, so the operator and household
surfaces cannot disagree about it (the shape ``camilla_recover_state``
already uses for its own out-of-band record).

**Where a park shows.** jasper-doctor FAILs, ``/state`` carries the verdict,
and the ``/system`` page renders one row per park —
[ADR-0187](../../docs/adr/0187-park-presentation-is-the-system-screen-only.md):
no banner, a browser learns about a park on the system screen and nowhere
else. That ADR supersedes ADR-0178's presentation clause only; its classes
and bars stand. The household audio card still speaks for a live park,
because that box is silent and the household must be told.

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

**Three signals that are not parks.** :func:`snapshot`'s ``unproven_endpoint``
names the coverage seam ADR-0184 records — a box whose wide-ring width
resolves with no armed endpoint and no class to name it. ``converge_refused``
names the shape past it: the marker IS armed and the program still never
reached the endpoint. ``endpoint_armed_without_active_modes`` names the seam's
mirror ([ADR-0189](../../docs/adr/0189-an-armed-endpoint-under-no-active-modes-discloses-on-non-composite-sinks.md)):
the marker is armed under no active modes, on a sink that is not composite.
None carries an issue or a remedy, which is exactly why ADR-0178 refuses them
a class, and all stop at the operator surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from ..env_load import outputd_reconciled_env

if TYPE_CHECKING:
    from ..output_topology import OutputTopology

#: Park class tokens. Structured, matched by tests and by the web surface;
#: the human prose beside them is presentation. See ADR-0178 for what each
#: shape is and why it is a class of its own.
PARK_PASSIVE_STEREO_COMPOSITE = "passive_stereo_composite"
PARK_MONO_FULL_RANGE = "mono_full_range"
PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED = "roleful_active_endpoint_unconverged"
PARK_GROUPED_DAC_CONTENT_LANE = "grouped_dac_content_lane"
PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE = "dac_content_marker_beside_bridge"

#: The tracked rebuild issue each shape waits on, in the tree's ``#NNNN``
#: spelling. ``roleful_active_endpoint_unconverged`` has none on purpose: it
#: needs no rebuild, only the ladder below.
ISSUE_COMPOSITE_ON_RING = "#2982"
ISSUE_MONO_ON_RING = "#3117"
ISSUE_GROUPED_ON_RING = "#3118"

#: The recorded remedy for a marker/bridge contradiction: the grouping writer
#: clears the bridge on its own next pass, so the operator runs one command.
BRIDGE_BESIDE_MARKER_REMEDY = (
    "sudo systemctl start jasper-grouping-reconcile.service"
)

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

#: Recorded text when the reconciler's last-observed output hardware names no
#: registry DAC: neither remedy step has anything to drive, so naming the
#: normal command would send the household to run one that cannot work (#2575).
NO_RECOGNIZED_DAC_REMEDY = "no recognized DAC; the remedy cannot converge"

#: Display name of the jasper-doctor check that catches the underlying cause
#: for an I2S box: its dtoverlay line dropped from config.txt ahead of the
#: reboot that would lose the DAC. Owned here, not in the check's own module,
#: so every reader of a park record (doctor, /state, the web card) names the
#: same check without the control layer importing the cli layer.
I2S_DAC_OVERLAY_CHECK_NAME = "I2S DAC overlay persists"


def _active_endpoint_remedy(topology: "OutputTopology") -> str:
    """The remedy for :data:`PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED`.

    Reads the reconciler's last-observed output hardware ONCE, for whether the
    DAC is *recognized* (``observed_profile_id`` — what the reconciler SAW),
    never whether it is *driven* (``active_profile_id`` needs ``ready`` and a
    selected card, which is irrelevant here: a recognized DAC that is merely
    not ready yet keeps the normal remedy). Consuming that already-written
    fact is not re-deriving ring eligibility — see the module docstring.

    No record at all (state unavailable) is inconclusive, not a claim of
    "nothing recognized" — it keeps the normal remedy too; only a record that
    POSITIVELY names no DAC swaps the text.
    """
    from ..output_hardware import load_state as _load_output_hardware_state

    state = _load_output_hardware_state()
    if state is None or state.observed_profile_id is not None:
        return ACTIVE_ENDPOINT_REMEDY

    from ..audio_hardware.dac import by_id as _dac_by_id

    profile = _dac_by_id(topology.hardware.device_id)
    if profile is not None and profile.connection == "i2s":
        return (
            f"{NO_RECOGNIZED_DAC_REMEDY} — see jasper-doctor's "
            f'"{I2S_DAC_OVERLAY_CHECK_NAME}" check'
        )
    return NO_RECOGNIZED_DAC_REMEDY


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
    #: Why the loaded graph is not at the endpoint its marker claims, or
    #: ``None``. The second not-a-park signal, and the complement of BOTH the
    #: ones above: same width, same marker, marker ARMED. See
    #: :func:`_endpoint_graph_refusal`.
    converge_refused: str | None
    #: A width resolves, the endpoint marker IS armed, and the layout declares
    #: no active-crossover mode — the fourth combination of the same three
    #: facts, and the one ADR-0184 did not model.
    #: [ADR-0189](../../docs/adr/0189-an-armed-endpoint-under-no-active-modes-discloses-on-non-composite-sinks.md)
    #: gives it its meaning and scopes it to NON-composite sinks. Operator-only:
    #: NOT a park, NOT a household claim.
    endpoint_armed_without_active_modes: bool


def _endpoint_graph_refusal() -> str | None:
    """Why the loaded graph is not at the endpoint its marker claims, or ``None``.

    The shape ADR-0178's four classes and ADR-0184's seam all miss: a box the
    ring CAN serve, whose endpoint marker IS armed, whose program was never
    moved onto that endpoint. :mod:`jasper.fanin.converge` refuses such a box
    on every unattended pass and — by its own contract — leaves it exactly as
    it found it, but the refusal is a journald line and nothing else: no
    statefile, no env key, no ``/state`` field. So the box reads ``parked:
    false`` on every surface while its program goes nowhere.

    NOT a park class, for the same reason ADR-0178 refuses one to the ADR-0184
    seam and one more of its own: a refusal leaves whatever graph was already
    loaded running, so "this box emits NOTHING" — the claim ``parked`` makes on
    the household card — would be false here. This is an operator signal that
    changes no status and adds no park.

    Reads the graph rather than predicting the converge pass, and answers only
    when it positively read one: an unreadable graph is unknown, not a refusal,
    and the surfaces that own that shape (``active_speaker_parked``,
    ``camilla_recover``) are already loud about it.
    """
    from ..fanin.ring_health import (
        graph_at_active_ring_endpoint,
        read_loaded_camilla_graph,
    )

    try:
        graph = read_loaded_camilla_graph()
        if graph.note:
            return None
        converged, detail = graph_at_active_ring_endpoint(graph)
    except Exception:  # noqa: BLE001 - an optional signal must not mask the parks
        return None
    if converged:
        return None
    return (
        "outputd's active-ring endpoint marker is armed, but the loaded "
        f"CamillaDSP graph is not at that endpoint ({detail}), so the converge "
        "pass refuses every time and leaves the box as it found it"
    )


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
    from ..fanin_coupling import (
        dac_content_marker_contradicted,
        ring_active_endpoint_armed,
    )
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
        env = outputd_reconciled_env()

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
                        "this box declares a 1-channel full-range layout, "
                        "and its contract carries issues that keep it from "
                        "resolving the 2-channel ring a clean mono box rides"
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
                remedy=_active_endpoint_remedy(topology),
                detail=(
                    f"this active-crossover box resolves a {active_ring}-channel "
                    "ACTIVE ring, but its endpoint marker has not converged onto "
                    "it, so nothing carries the post-crossover program"
                ),
            )
        )

    # OUTPUTD'S OWN REFUSAL, MIRRORED. The marker beside a DECLARED bridge is
    # the pair `Config::from_env` bails EX_CONFIG on, which the unit's
    # `RestartPreventExitStatus=78` turns into a parked daemon — silent, while
    # every writer reports the bond formed. Reachable because
    # `jasper-fanin-coupling-auto` writes the bridge into the FIRST env layer on
    # every pass, so a member whose grouping layer failed to clear it lands
    # here (ADR-0220).
    if dac_content_marker_contradicted(env):
        parks.append(
            TransportPark(
                park_class=PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE,
                issue=ISSUE_GROUPED_ON_RING,
                remedy=BRIDGE_BESIDE_MARKER_REMEDY,
                detail=(
                    "this bonded member carries both the dac-content lane marker "
                    "and a declared content bridge; outputd refuses that pair at "
                    "startup, so the speaker is silent with every unit green"
                ),
            )
        )

    # THE LEGACY FIFO SPELLING ONLY: it needs
    # ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct``, which no writer emits, so nothing
    # produces its audio. The ring MARKER is SERVED and does not park (ADR-0220
    # supersedes that row of ADR-0178). Read as a PATH, non-empty rather than
    # present, because the grouping reconciler writes this key as an EMPTY
    # string on every branch.
    #
    # EXPIRY: dies with outputd's own FIFO reader, after a bonded pair plays
    # through the ring on metal (ADR-0220, #3118).
    fifo_armed = bool((env.get(OUTPUTD_DAC_CONTENT_FIFO_ENV) or "").strip())
    if fifo_armed:
        parks.append(
            TransportPark(
                park_class=PARK_GROUPED_DAC_CONTENT_LANE,
                issue=ISSUE_GROUPED_ON_RING,
                remedy=None,
                detail=(
                    "this box is a bonded grouping member pinned to the legacy "
                    "raw-PCM FIFO round-trip lane, which needs a content bridge "
                    "its writer no longer emits, so nothing produces its audio"
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

    # A third arm off the same three facts, reached by a combination neither
    # of the two above claims: class (c) needs the marker NOT armed, ADR-0184's
    # seam needs no active modes AND no marker, and this needs the marker armed
    # ON an active-crossover box — so nothing else asks whether the program
    # actually moved. Only then is the graph read at all, and a box with no
    # active ring pays nothing for this.
    #
    converge_refused = (
        _endpoint_graph_refusal()
        if active_ring is not None and contract.active_modes and endpoint_armed
        else None
    )

    # The fourth combination of the same three facts, and the last one with no
    # answer: width resolved, marker ARMED, no active modes (ADR-0189).
    #
    # SCOPED TO NON-COMPOSITE SINKS, which is the whole of the decision. The
    # marker's writers arm it for an accepted active-speaker graph, so on a
    # roleful box that declares no active mode the pair means one of two things
    # — reconfiguration lag, since the hardware reconciler disarms on its NEXT
    # pass rather than instantly, or a genuine mismatch. Neither deserves the
    # greenest verdict. A composite sink is the exception and must stay silent:
    # it is served with the marker armed and no active modes of its own, so a
    # class-blind read here would warn on every healthy composite.
    endpoint_armed_without_active_modes = bool(
        active_ring is not None
        and not contract.active_modes
        and endpoint_armed
        and not topology_sink_is_composite(topology)
    )

    return _Assessment(
        parks=tuple(parks),
        ring_unresolved=ring_unresolved,
        unproven_endpoint=unproven_endpoint,
        converge_refused=converge_refused,
        endpoint_armed_without_active_modes=endpoint_armed_without_active_modes,
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
) -> dict[str, Any]:
    """Fail-soft park verdict for jasper-doctor, ``/state`` and the web card.

    Shapes, discriminated by ``status``:

    ``{"status": "ok", "parked": False, "parks": []}``
        The ring resolves a geometry for this box, or it is not configured yet.

    ``{"status": "parked", "parked": True, "parks": [...]}``
        No ring serves this box: it emits NOTHING. Loud on every surface.

    ``{"status": "unclassified", "parked": False, "parks": []}``
        Configured, no ring geometry of either kind, and none of the four
        classes names it — a mid-commissioning layout with an unassigned lane,
        say. NOT a fifth class and not a household incident; it is the refusal
        to claim "the ring can serve this box" about a box the ring demonstrably
        cannot serve, which is the false quiet ADR-0100 exists to prevent.

    ``{"status": "unavailable", "parked": False, "parks": [], "error": ...}``
        Topology or env could not be read. Reported distinctly rather than as
        a healthy box, the same posture the other park readers hold.

    Three signals ride alongside ``status``, never inside it. All are OPERATOR
    facts: none changes a status, adds a park, or reaches the household card,
    and all are always present.

    ``unproven_endpoint``
        The ADR-0184 coverage seam — a box whose wide-ring width resolves with
        no armed endpoint and no class to name it. ``False`` when it cannot be
        assessed.

    ``converge_refused``
        Why the loaded graph is not at the endpoint its ARMED marker claims,
        or ``None`` — see :func:`_endpoint_graph_refusal`. A box in this shape
        is ring-eligible and reads ``parked: false`` everywhere while its
        program goes nowhere, which is why it needs a name of its own.

    ``endpoint_armed_without_active_modes``
        A width resolves, the marker IS armed, the layout declares no active
        mode, and the sink is not composite — ADR-0189. ``False`` when it
        cannot be assessed.

    Never raises.
    """
    try:
        assessment = _assess(topology, env)
    except Exception as exc:  # noqa: BLE001 - a park reader must not raise here
        return {
            "status": "unavailable",
            "parked": False,
            "parks": [],
            "unproven_endpoint": False,
            "converge_refused": None,
            "endpoint_armed_without_active_modes": False,
            "error": str(exc),
        }

    parks = assessment.parks
    if parks:
        status = "parked"
    elif assessment.ring_unresolved:
        status = "unclassified"
    else:
        status = "ok"

    return {
        "status": status,
        "parked": status == "parked",
        "parks": [park.to_dict() for park in parks],
        "unproven_endpoint": assessment.unproven_endpoint,
        "converge_refused": assessment.converge_refused,
        "endpoint_armed_without_active_modes": (
            assessment.endpoint_armed_without_active_modes
        ),
    }
