"""Move this box's ACTIVE endpoint onto the ring, once, by itself. (#2285 P7)

WHAT THIS CLOSES. The ACTIVE endpoint was the one arming axis a machine could
not move. ``resolve_output_layout`` picks the emit device from a marker; the
hardware reconciler derives that marker from the LOADED GRAPH; and the graph
only names the ring once something re-emits it there. Marker <- graph <- marker:
a fixed point whose only lever was a human typing ``jasper-active-speaker
baseline-reemit --endpoint ring``. Post-#2534 a roleful box waiting on that
human is not a working speaker, it is the #2261 park. This is the lever, pulled
by the unattended pass at the three events that already exist: boot, deploy, and
a DAC hotplug.

IT IS ONE STEP, NOT A SEQUENCER. The already-landed gates decide whether this
box may be on the ring — ``ring_roleful_unattended_ready`` (P6) is the whole
admission argument and none of it is restated here. This function only asks the
questions those gates cannot: is the graph already there, did a human pin this
box, and is the applied record still the truth. Then it moves the graph and lets
the pass carry on.

NO RESTORE POINT, AND THE REASON IS IDEMPOTENCE, not optimism. Every crash point
leaves a state the next pass handles:

* before the move — nothing is written;
* mid-move — every write is atomic and durable, so no file is torn. The APPLIED
  branch rewrites the artifact in place, so the next pass reads the moved graph
  and reports it converged; the ANCHOR branch's statefile write is last, so a
  next pass still reads the old graph and simply re-emits, which is idempotent;
* after the move, before the kick — the hardware reconciler is
  ``WantedBy=multi-user.target`` and re-derives the marker pair from the loaded
  graph at the next boot anyway;
* during the arm — that is the coupling reconciler's own ordered rollback,
  unchanged.

The one interval this leaves is a graph at the ring under a ``loopback``
coupling. It converges on the next pass, it does not start silent (the re-emit
performs no CamillaDSP reload, so the box keeps playing the graph it already
had until the next camilla start), and while it lasts the doctor names it
outright — ``check_active_ring_split_transport``, with the runnable remedy. A
one-owner box gets a loud, actionable state and self-heals; that is the trade
this ships instead of a durable restore point.

WHAT IT WRITES: the graph artifact, the canonical copy, and the statefile
pointer — all three already ``baseline-reemit``'s job, reached by calling that
CLI rather than by a second implementation of it. NO env: the hardware
reconciler stays the single writer of ``outputd.env``'s active-lane marker pair
and is only asked to re-derive from the moved graph.

WHAT IT NEVER DOES: decide the coupling. A refusal here is not an abort — the
pass continues into its gate set, which resolves loopback on its own terms.
"""
from __future__ import annotations

import logging
import os

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


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
    it was found. Every decision it makes is guarded, but the caller guards the
    call as well — a corrupt ``fanin.env`` raises ``UnicodeDecodeError`` out of
    the very first read (``_read_snapshot`` catches ``OSError`` only), and this
    step runs ahead of everything the pass has always done, so it must never be
    the reason a box fails to reconcile at all.
    """
    from jasper.active_speaker.baseline_profile import (
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.runtime_contract import (
        active_ring_channels_for_topology,
    )
    from jasper.fanin import coupling_reconcile as cr
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    # AN OPERATOR PIN IS A DECISION ABOUT THIS BOX'S TRANSPORT, read first. The
    # pass contractually refuses to CHANGE a pinned coupling, so moving the
    # graph anyway would leave graph@ring under a pinned loopback — and on a
    # roleful box the moved graph is also the boot graph, so it would survive the
    # reboot. It is also a silent override of an explicit human choice, which is
    # the one thing a box for tinkerers must not do.
    if cr.is_operator_choice(cr.read_marker(cr._read_snapshot(cr.FANIN_ENV_PATH).text)):
        return _emit("noop_operator_pinned", reason=reason)

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

    # ALREADY THERE? The ENDPOINT question only. Not
    # ``ring_endpoint_anchor_converged``, which also demands anchor identity and
    # all-muted — both false forever on a box riding an applied baseline, so
    # asking it here would re-emit a commissioned box's graph at every boot,
    # deploy and hotplug.
    graph = cr.read_loaded_camilla_graph()
    if graph.note:
        return _emit(
            "preflight_refused",
            reason=reason,
            detail=f"cannot read the loaded CamillaDSP graph ({graph.note})",
        )
    converged, converged_detail = cr.graph_at_active_ring_endpoint(graph)
    if converged:
        return _emit("already_converged", reason=reason, detail=converged_detail)

    # PROVE BEFORE MOVING, using the gate list the pass itself runs — minus the
    # ONE gate this move exists to satisfy. ``ring_topology``'s roleful arm ends
    # in ``active_ring_endpoint_proof``, which reads the marker that is derived
    # from the graph this has not moved yet; requiring it here is the fixed
    # point. The pass re-runs the whole list immediately after, so that gate is
    # proved, just after the write rather than before it.
    for name, gate in cr.default_ring_gates():
        if name == "ring_topology":
            continue
        try:
            ok, detail = gate()
        except Exception as exc:  # noqa: BLE001 - a raising gate is a refusal
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            return _emit(
                "preflight_refused", reason=reason, detail=f"{name}: {detail}"
            )

    # THE APPLIED-RECORD REFUSAL (#2558's closing contract). Asked ONLY when a
    # record exists: ``applied_profile_displacement`` answers
    # ``applied_profile_path_unknown`` for a missing one, so asking it
    # unconditionally would refuse every anchor-riding box — every fresh install
    # and every mid-commission speaker, the class that converges first. "" means
    # the record is what CamillaDSP is playing and this converges freely; any
    # non-empty verdict means it is not, or that we could not check, and an
    # unattended re-emit would publish a graph the speaker is not playing.
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
                detail=f"{verdict} (#2558); leaving the coupling where it is",
                level=logging.WARNING,
            )

    moved, detail = _reemit_graph_at_ring()
    if not moved:
        return _emit(
            "reemit_refused", reason=reason, detail=detail, level=logging.WARNING
        )
    kick_ok, kick_detail = cr._start_audio_hardware_reconcile(reason=reason)
    return _emit(
        "graph_reemitted",
        reason=reason,
        detail=kick_detail if not kick_ok else "",
        level=logging.INFO if kick_ok else logging.WARNING,
    )


def _reemit_graph_at_ring() -> tuple[bool, str]:
    """Run ``baseline-reemit --endpoint ring``. (ok, detail).

    Through the CLI's own ``main``, not a second implementation of it, so what
    this publishes is byte-identical to what an operator's ``baseline-reemit``
    publishes and every default comes from the one parser that owns them.

    ``--force`` IS NEVER PASSED, and the absence is load-bearing rather than
    tidy: ``_reemit_staged_startup_anchor`` refuses while a per-driver
    commissioning load is active, because moving the anchor mid-load re-points
    the operator's own stop control. This caller's safety is that refusal, so
    the flag is not threaded through and cannot be.

    ``--statefile`` IS passed, from the resolver ``applied_profile_displacement``
    reads. The CLI's argparse default is a literal that ignores
    ``JASPER_CAMILLA_STATEFILE``; taking it would let the divergence check read
    one statefile while the re-emit re-pointed another.
    """
    from jasper.active_speaker.environment import DEFAULT_CAMILLA_STATEFILE
    from jasper.cli import active_speaker as cli

    statefile = os.environ.get(
        "JASPER_CAMILLA_STATEFILE", str(DEFAULT_CAMILLA_STATEFILE)
    )
    argv = ["baseline-reemit", "--endpoint", "ring", "--statefile", statefile]
    try:
        rc = int(cli.main(argv))
    except SystemExit as exc:  # the CLI's own parser.exit on a config error
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - report, never raise out of the pass
        return False, f"baseline-reemit raised: {type(exc).__name__}: {exc}"
    if rc == 0:
        return True, ""
    return False, f"baseline-reemit refused (rc={rc}); nothing was written"
