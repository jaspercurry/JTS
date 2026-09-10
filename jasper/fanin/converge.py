"""Move this box's ACTIVE endpoint onto the ring, once, per unattended pass.

Pulled at the three events that already exist: boot, deploy, and a DAC hotplug.
Admission is decided entirely by the ring readiness gates (see ``_ring_gates``);
this step only asks the two questions those gates cannot — is the graph already
at the ring, and is the applied baseline record still what CamillaDSP plays —
and then re-emits the graph through ``baseline-reemit``.

There is no restore point, by idempotence: every write is atomic and durable, so
each crash point leaves a state the next pass either reports converged or simply
re-emits. While the moved graph and the endpoint marker disagree,
``check_content_transport_coherence`` names it with a runnable remedy.

It writes the graph artifact, the canonical copy and the statefile pointer, all
by calling the CLI rather than reimplementing it. It writes no env: the hardware
reconciler stays the single writer of ``outputd.env``'s active-lane marker pair
and is only asked to re-derive from the moved graph. It never decides the
coupling — there is one (ADR-0100).
"""
from __future__ import annotations

import logging

from jasper.active_speaker.environment import camilla_statefile_path
from jasper.active_speaker.runtime_contract import active_ring_channels_for_topology
from jasper.fanin import ring_readiness as rr
from jasper.fanin.coupling_reconcile import _start_audio_hardware_reconcile
from jasper.log_event import log_event
from jasper.output_topology import OutputTopologyError, load_output_topology_strict

logger = logging.getLogger(__name__)


def _ring_gates() -> tuple[tuple[str, rr.RingGate], ...]:
    """The ring-readiness proofs a graph move must pass, in ONE order.

    Order is a diagnostic decision, not cost: the coarser roleful-admission
    refusal first, then asset presence before the two gates that read those
    assets, then capability before width.

    ``ring_topology_ready`` is deliberately absent: its roleful arm ends in
    ``active_ring_endpoint_proof``, which reads the marker derived from the graph
    this step has not moved yet. The coupling reconcile that follows re-runs it.
    """
    return (
        ("ring_roleful_unattended", rr.ring_roleful_unattended_ready),
        ("ring_assets", rr.ring_assets_ready),
        ("ring_wire_caps", rr.ring_wire_caps_ready),
        ("ring_edge_width", rr.ring_edge_width_ready),
    )


def _emit(result: str, *, reason: str, detail: str = "", level: int = logging.INFO) -> str:
    log_event(
        logger,
        "fanin.converge",
        result=result,
        reason=reason,
        detail=detail or None,
        level=level,
    )
    return result


def converge_active_endpoint(*, reason: str = "converge") -> str:
    """Converge the ACTIVE endpoint if this box is admitted and not there yet.

    Returns the ``result=`` token it logged. A refusal leaves the box exactly as
    it was found. The caller must still guard the call: a corrupt ``fanin.env``
    raises ``UnicodeDecodeError`` out of the very first read, and this step runs
    ahead of everything else the pass does.
    """
    # Fail-closed on an unreadable topology: a graph move cannot be proved right
    # against a topology this pass cannot read.
    try:
        topology = load_output_topology_strict()
        roleful = active_ring_channels_for_topology(topology) is not None
    except (OutputTopologyError, OSError, ValueError) as exc:
        return _emit(
            "preflight_refused", reason=reason, detail=f"topology unreadable ({exc})"
        )
    if not roleful:
        return _emit("noop_not_roleful", reason=reason)

    # The ENDPOINT question only, not ``ring_endpoint_anchor_converged``: anchor
    # identity and all-muted are false forever on a box riding an applied
    # baseline, so asking those here would re-emit at every boot, deploy and
    # hotplug.
    graph = rr.read_loaded_camilla_graph()
    if graph.note:
        return _emit(
            "preflight_refused",
            reason=reason,
            detail=f"cannot read the loaded CamillaDSP graph ({graph.note})",
        )
    converged, converged_detail = rr.graph_at_active_ring_endpoint(graph)
    if converged:
        return _emit("already_converged", reason=reason, detail=converged_detail)

    for name, gate in _ring_gates():
        try:
            ok, detail = gate()
        except (OSError, ValueError) as exc:
            # The derived set, not a catch-all: a truncated SD card reaches
            # ``read_text`` as ``UnicodeDecodeError`` (a ``ValueError``), plus
            # the ``OSError`` its file reads can throw. Anything else is a bug
            # and belongs at the caller's boundary.
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            return _emit(
                "preflight_refused", reason=reason, detail=f"{name}: {detail}"
            )

    # Asked ONLY when a record exists: ``applied_profile_displacement`` answers
    # ``applied_profile_path_unknown`` for a missing one, which would refuse
    # every anchor-riding box. "" means the record is what CamillaDSP is
    # playing; anything else means an unattended re-emit would publish a graph
    # the speaker is not playing.
    from jasper.active_speaker.baseline_profile import (  # lazy: import cost, pulls scipy via bass_extension (ADR-0226)
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )

    try:
        applied = load_applied_baseline_profile_state()
    except (OSError, ValueError) as exc:
        return _emit(
            "preflight_refused",
            reason=reason,
            detail=f"the applied active-speaker record could not be read ({exc})",
        )
    if applied is not None:
        verdict = applied_profile_displacement(applied)
        if verdict:
            return _emit(
                "applied_record_diverged",
                reason=reason,
                detail=(
                    f"{verdict}; leaving the coupling where it is. "
                    "Re-apply the speaker profile at /sound/speaker/ to put the "
                    "record and the running graph back into agreement — that "
                    "apply is what writes both. Running `baseline-reemit` by "
                    "hand is NOT the fix: it is the exact republish of the "
                    "stale record that this refusal exists to prevent"
                ),
                level=logging.WARNING,
            )

    moved, detail = _reemit_graph_at_ring()
    if not moved:
        return _emit(
            "reemit_refused", reason=reason, detail=detail, level=logging.WARNING
        )
    kick_ok, kick_detail = _start_audio_hardware_reconcile(reason=reason)
    return _emit(
        "graph_reemitted",
        reason=reason,
        detail=kick_detail if not kick_ok else "",
        level=logging.INFO if kick_ok else logging.WARNING,
    )


def _reemit_graph_at_ring() -> tuple[bool, str]:
    """Run ``baseline-reemit --endpoint ring``. (ok, detail).

    Through the CLI's own ``main`` so what this publishes is byte-identical to
    what an operator's ``baseline-reemit`` publishes.

    ``--force`` is never passed, and the absence is load-bearing:
    ``reemit_staged_startup_anchor`` refuses while a per-driver commissioning
    load is active, because moving the anchor mid-load re-points the operator's
    own stop control. That refusal is this caller's safety.

    ``--statefile`` IS passed, from the resolver ``applied_profile_displacement``
    reads. The CLI's argparse default is a literal that ignores
    ``JASPER_CAMILLA_STATEFILE``; taking it would let the divergence check read
    one statefile while the re-emit re-pointed another.
    """
    from jasper.cli import active_speaker as cli  # lazy: import cost, the whole CLI tree (ADR-0226)

    statefile = str(camilla_statefile_path())
    argv = ["baseline-reemit", "--endpoint", "ring", "--statefile", statefile]
    try:
        rc = int(cli.main(argv))
    # SystemExit first: it is not an ``Exception`` subclass, so the order cannot
    # change what is caught, but reading it after a bare ``Exception`` invites a
    # "tidy" of the pair in the direction that would.
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - availability wrap around an ENTIRE CLI
        # Breadth is the point: ``main()``'s converter turns exactly three
        # classes into ``parser.exit``, so everything else the CLI tree can
        # raise arrives here live. Past this frame the box loses its reconcile,
        # not merely its convergence.
        return False, f"baseline-reemit raised: {type(exc).__name__}: {exc}"
    if rc == 0:
        return True, ""
    return False, f"baseline-reemit refused (rc={rc}); nothing was written"
