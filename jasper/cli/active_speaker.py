# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator tools for active-speaker commissioning artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jasper.active_speaker.profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
)
from jasper.active_speaker.baseline_profile import baseline_profile_state_path
from jasper.active_speaker.camilla_yaml import emit_active_speaker_startup_config
from jasper.active_speaker.environment import read_camilla_statefile_config_path
from jasper.active_speaker.path_safety import (
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
    requirements_payload,
    write_path_safety_evidence,
)
from jasper.active_speaker.calibration_level import load_calibration_level_state
from jasper.active_speaker.environment import probe_active_speaker_environment
from jasper.active_speaker.runtime_contract import (
    DEFAULT_FLAT_OUTPUTD_CONFIG,
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    PARKED_MUTED_STATUS,
    apply_safe_graph_decision_to_statefile,
    parked_muted_exits,
    safe_graph_for_current_topology,
)
from jasper.active_speaker.runtime_convergence import compose_selected_flat_graph
from jasper.active_speaker.staging import load_staged_startup_config
from jasper.active_speaker.startup_load import (
    build_driver_commission_load_preflight,
    load_commission_load_state,
    load_driver_commissioning_config,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.commission_ramp import (
    abort_ramp,
    clear_pending_ramp_step,
    effective_confirmed_roles,
    load_ramp_state,
    ramp_audible_step,
    record_ramp_operator_ack,
)
from jasper.active_speaker.measurement import confirmed_driver_roles
from jasper.active_speaker.commission_wiring import (
    commission_load_config,
    commission_seams,
    read_current_config_path,
    resolve_commission_inputs,
    write_commission_path_safety,
)
from jasper.active_speaker.safe_playback import (
    FLOOR_OPERATOR_OUTCOMES,
    load_safe_playback_state,
    stop_safe_playback_session,
)
from jasper.dsp_apply import validate_camilla_config
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    load_output_topology_strict,
)


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ActiveSpeakerConfigError(f"could not read {label}: {e}") from e
    except json.JSONDecodeError as e:
        raise ActiveSpeakerConfigError(f"{label} is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ActiveSpeakerConfigError(f"{label} JSON must be an object")
    return payload


def _print_template_summary(payload: dict[str, Any]) -> None:
    print(f"Preset: {payload['preset_id']} ({payload['name']})")
    print(f"Topology: {payload['way_count']}-way {payload['layout']}")
    print(f"Output channels: {payload['output_count']}")
    print(f"Template: {payload['output']}")
    validation = payload.get("validation") or {}
    status = validation.get("status", "skipped")
    print(f"Validation: {status}")
    if status == "missing":
        print("  camilladsp binary not found; syntax preflight skipped")
    elif validation.get("stderr_tail"):
        print(f"  stderr: {validation['stderr_tail']}")


def _print_requirements(payload: dict[str, Any]) -> None:
    print("Active speaker path-safety requirements:")
    for requirement in payload["requirements"]:
        print(f"- {requirement['id']}: {requirement['label']}")
        print(f"  checks: {', '.join(requirement['checks'])}")
        print(f"  why: {requirement['why']}")


def _print_path_audit_summary(payload: dict[str, Any]) -> None:
    print(f"Path safety: {payload['status']}")
    print(f"Evidence source: {payload['evidence_source']}")
    print(
        f"Hardware probe backed: {'yes' if payload['hardware_probe_backed'] else 'no'}"
    )
    print(f"Load gate: {payload['load_gate']}")
    print(
        f"OK to load active config: {'yes' if payload['ok_to_load_active_config'] else 'no'}"
    )
    print(f"Blockers: {payload['blocker_count']}")
    for path in payload["paths"]:
        print(f"- {path['id']}: {path['status']}")
    if payload["issues"]:
        print("Issues:")
        for issue in payload["issues"]:
            print(f"  [{issue['severity']}] {issue['path_id']}: {issue['message']}")


def _print_environment_summary(payload: dict[str, Any]) -> None:
    config = payload["camilla_config"]
    alsa = payload["alsa"]
    path_safety = payload["path_safety"]
    validation = payload["camilla_validation"]
    print(f"Active speaker environment: {payload['status']}")
    print(f"Load gate: {payload['load_gate']}")
    print(
        f"OK to load active config: {'yes' if payload['ok_to_load_active_config'] else 'no'}"
    )
    print(
        f"Camilla config: {config['classification']} ({config.get('path') or 'none'})"
    )
    print(f"  {config['label']}")
    print(
        "  playback: "
        f"{config.get('playback_device') or 'unknown'} "
        f"channels={config.get('playback_channels') or 'unknown'} "
        f"volume_limit={config.get('volume_limit_db')!r}"
    )
    print(f"Camilla validation: {validation.get('status', 'unknown')}")
    print(
        "ALSA playback devices: "
        f"{len(alsa.get('devices', []))} "
        f"({'available' if alsa.get('available') else 'unavailable'})"
    )
    print(
        "Path safety: "
        f"{path_safety.get('status', 'unknown')} "
        f"gate={path_safety.get('load_gate', 'unknown')}"
    )
    if payload["issues"]:
        print("Issues:")
        for issue in payload["issues"]:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")


def _cmd_startup_template(args: argparse.Namespace) -> int:
    preset = ActiveSpeakerPreset.from_mapping(
        _load_json_object(Path(args.preset), label="preset")
    )
    output = Path(args.output)
    emit_active_speaker_startup_config(
        preset,
        playback_device=args.playback_device,
        out_path=output,
        baseline_id=args.baseline_id,
    )

    validation = None
    if args.check:
        validation = validate_camilla_config(output).to_dict()

    payload: dict[str, Any] = {
        "preset_id": preset.preset_id,
        "name": preset.name,
        "way_count": preset.way_count,
        "layout": preset.channel_map.layout,
        "output_count": len(preset.channel_map.outputs),
        "output": str(output),
        "validation": validation or {"status": "skipped"},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_template_summary(payload)

    status = payload["validation"].get("status")
    return 1 if status in {"invalid_config", "runner_error", "timeout"} else 0


def _cmd_path_audit(args: argparse.Namespace) -> int:
    if args.requirements:
        payload = requirements_payload()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_requirements(payload)
        return 0
    if not args.evidence:
        raise ActiveSpeakerConfigError(
            "path-audit requires evidence JSON or --requirements"
        )

    payload = evaluate_path_safety_evidence(
        _load_json_object(Path(args.evidence), label="path-safety evidence")
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_path_audit_summary(payload)
    return 0 if payload["requirements_met"] else 1


def _cmd_path_probe(args: argparse.Namespace) -> int:
    evidence = build_startup_load_path_safety_evidence(
        load_output_topology_strict(args.topology),
        staged_config=load_staged_startup_config(),
        calibration_level=load_calibration_level_state(),
        current_config_path=args.current_config,
    )
    evidence_path = write_path_safety_evidence(evidence, path=args.output)
    report = evaluate_path_safety_evidence(evidence)
    payload = {
        "evidence_path": str(evidence_path),
        "report": report,
        "evidence": evidence,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Wrote path-safety evidence: {evidence_path}")
        _print_path_audit_summary(report)
        print("No audio was emitted and CamillaDSP was not reloaded.")
    return 0 if report["ok_to_load_active_config"] else 1


def _cmd_environment_probe(args: argparse.Namespace) -> int:
    payload = probe_active_speaker_environment(
        config_path=args.config,
        statefile_path=args.statefile,
        path_safety_evidence_path=args.path_safety_evidence,
        run_config_check=args.check_config,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_environment_summary(payload)
    return 0 if payload["ok_to_load_active_config"] else 1


def _print_runtime_safe_graph_summary(
    payload: dict[str, Any],
    *,
    wrote_statefile: bool,
    topology: OutputTopology | None = None,
) -> None:
    contract = payload["topology_contract"]
    current = payload.get("current_graph") or {}
    preferred = payload.get("preferred_graph") or {}
    fallback = payload.get("fallback_graph") or {}
    print(f"Runtime graph decision: {payload['status']}")
    print(f"  reason: {payload['reason']}")
    print(
        "  topology: "
        f"{contract['classification']} "
        f"requires_roleful_graph={contract['requires_roleful_graph']}"
    )
    if current:
        print(
            "  current: "
            f"{current.get('classification')} "
            f"allowed={current.get('allowed')} "
            f"path={current.get('config_path')}"
        )
    if preferred:
        print(
            "  preferred: "
            f"{preferred.get('classification')} "
            f"allowed={preferred.get('allowed')} "
            f"path={preferred.get('config_path')}"
        )
    if fallback:
        print(
            "  fallback: "
            f"{fallback.get('classification')} "
            f"allowed={fallback.get('allowed')} "
            f"path={fallback.get('config_path')}"
        )
    if payload.get("selected_config_path"):
        print(f"  selected: {payload['selected_config_path']}")
    # A deliberately-silenced PHYSICAL output deserves a trail. The graph the
    # box is about to boot may hard-mute a DAC output the saved topology does
    # not claim (a mono speaker on a stereo DAC); install's transcript is the
    # only place an operator would ever see that, so name the channels rather
    # than let a silent output look like a fault later.
    for label, graph in (("current", current), ("fallback", fallback)):
        muted = (graph.get("details") or {}).get("hard_muted_outputs")
        if muted:
            print(
                f"  {label} hard-muted outputs: "
                f"{', '.join(str(index) for index in muted)} "
                "(not assigned by the saved topology)"
            )
    print(f"  statefile written: {'yes' if wrote_statefile else 'no'}")
    if payload["status"] == PARKED_MUTED_STATUS:
        # The parked state is an ACTION for the household, not a stack of
        # blockers for an operator to decode. Name the exits and stop —
        # the blocker wall stays for a genuinely unsafe graph.
        #
        # Through the capability-aware helper, not the bare constant: on a DAC
        # with no active outputd lane "finish crossover preview" can never
        # succeed, and offering an impossible action is worse than offering
        # none. The doctor and `/state` already resolve it this way, so this is
        # the third of the three surfaces that name the same exits agreeing on
        # one owner rather than two of them agreeing and one drifting.
        print(f"  next: {parked_muted_exits(topology)}")
    for issue in payload.get("issues") or []:
        print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")


def _cmd_runtime_safe_graph(args: argparse.Namespace) -> int:
    # The persisted fan-in coupling decides the flat fallback: a ring-armed box
    # (shm_ring) re-seeds the ring flat config, not the loopback one (finding 5).
    # --coupling lets install.sh pass the live value explicitly; when omitted we
    # read the persisted intent from fanin.env (fail-safe to loopback), so a bare
    # operator run still seeds the right graph.
    coupling = args.coupling
    if coupling is None:
        from jasper.fanin.ring_health import read_persisted_coupling

        coupling = read_persisted_coupling()
    topology = load_output_topology_strict(args.topology)
    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=args.statefile,
        current_config_path=args.current_config,
        flat_config_path=args.flat_config,
        coupling=coupling,
        applied_baseline_path=baseline_profile_state_path(
            args.applied_baseline_state
        ),
        staged_metadata_path=args.staged_metadata,
        consider_applied_baseline=not args.no_applied_baseline,
    )
    wrote = False
    if args.write_statefile and decision.ok:
        try:
            decision = compose_selected_flat_graph(
                decision,
                topology=topology,
                coupling=coupling,
            )
            wrote = apply_safe_graph_decision_to_statefile(
                decision,
                statefile_path=args.statefile,
                # Same topology object the decision was made from, so the
                # write-time all-muted re-proof cannot be answered by a
                # second, differently-read topology.
                topology=topology,
            )
        except (
            ActiveSpeakerConfigError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            # Only the parked branch generates bytes, and it refuses to write
            # anything it cannot re-prove all-muted. Fail the run: a statefile
            # pointing at a config we would not write is worse than a red deploy.
            print(f"Runtime graph decision: {decision.status}")
            print(f"  ERROR: {exc}")
            return 1
    payload = decision.to_dict()
    payload["statefile_written"] = wrote
    payload["statefile_path"] = args.statefile
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_runtime_safe_graph_summary(
            payload,
            wrote_statefile=wrote,
            # The same topology the decision was made from, so the exits named
            # here cannot come from a second, differently-read topology.
            topology=topology,
        )
    return 0 if decision.ok else 1


def _baseline_reemit_endpoint(
    topology: Any, endpoint: str | None
) -> tuple[str | None, str]:
    """Which playback endpoint this re-emit targets, and where that came from.

    ``--endpoint ring`` is the explicit RE-EMIT-NOW verb: it moves the GRAPH
    onto the ACTIVE ring, after which the marker derives from the graph and the
    coupling follows. It is no longer a choice BETWEEN transports — the ring is
    the only legal endpoint (``OUTPUTD_LEGAL_ENDPOINT_DEVICES``) — so there is
    no ``aloop`` rollback arm to name. That direction was retired with the
    snd-aloop ACTIVE lane's PCM definitions (#2534): naming it moved the anchor
    onto a transport that does not exist, so the only thing it could still
    produce was a park. Recovery is forward, by re-arming the ring.

    Omitting it keeps the auto answer (``resolve_output_layout``, the single
    chooser). Since that chooser now returns the ring unconditionally, omitting
    and passing ``ring`` agree on the device and differ only in provenance —
    which is why this still reports WHERE the answer came from.
    """
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    if endpoint == "ring":
        return RING_ACTIVE_PLAYBACK_DEVICE, "explicit_endpoint_ring"
    if endpoint:
        # An explicit endpoint this function does not recognise must NOT fall
        # through to the auto-resolver. Falling through would answer the ring
        # anyway and report the provenance as auto — so a caller asking for a
        # retired endpoint (`aloop`) would be told it got what it asked for.
        # argparse's `choices` is the only entry point today, but proving that
        # stays true is more expensive than refusing here.
        raise ValueError(
            f"unrecognised playback endpoint {endpoint!r}: "
            f"{RING_ACTIVE_PLAYBACK_DEVICE} is the one legal ACTIVE endpoint"
        )
    return resolve_active_playback_device(topology)


def _startup_anchor_from_decision(decision: Any) -> Any | None:
    """The decision's operative graph, when it IS the all-muted startup anchor.

    Two safe-graph statuses can put a box on that anchor, and they differ only in
    where the graph came from: ``preserve_current`` (the loaded graph already is
    it) and ``select_active_startup`` (the persisted staged candidate is it). The
    classification is checked rather than the status alone, because
    ``preserve_current`` is also how an APPROVED runtime graph and a
    driver-domain baseline are preserved — keying on the status would let this
    command re-stage over a box that is on neither.
    """
    graph = {
        "preserve_current": decision.current_graph,
        "select_active_startup": decision.fallback_graph,
    }.get(decision.status)
    if (
        graph is not None
        and graph.allowed
        and graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return graph
    return None


def _describe_safe_graph_for_refusal(decision: Any) -> str:
    """What this box was actually found on, in one line an operator can act on."""
    seen = [
        f"{label}={graph.classification}"
        for label, graph in (
            ("current", decision.current_graph),
            ("preferred", decision.preferred_graph),
            ("fallback", decision.fallback_graph),
        )
        if graph is not None
    ]
    detail = f"safe-graph status={decision.status}"
    if seen:
        detail += " (" + ", ".join(seen) + ")"
    return detail


def _relocate_validation_evidence(
    validation: Any, proof_path: Path, target: Path
) -> Any:
    """Re-point a validation result's own path fields from the proof dir to the target.

    ``validate_camilla_config`` records the file it was handed — ``path`` on
    every return branch, and the same string inside ``argv`` when the camilladsp
    binary exists. The anchor is proved in a temporary directory that is deleted
    before the metadata lands, so publishing the result verbatim would record a
    path that cannot exist. The VERDICT is about bytes that are now at ``target``,
    so the verdict travels and its locations are corrected with it.

    Substring replacement over ``argv`` rather than a rebuilt command line: the
    argv belongs to the validator, and guessing its shape here would be a second
    place that knows how it invokes camilladsp.
    """
    if not isinstance(validation, Mapping):
        return validation
    proof, dest = str(proof_path), str(target)
    relocated = dict(validation)
    if relocated.get("path") == proof:
        relocated["path"] = dest
    argv = relocated.get("argv")
    if isinstance(argv, list):
        relocated["argv"] = [
            dest if arg == proof else arg for arg in argv
        ]
    return relocated


def _reemit_staged_startup_anchor(
    args: argparse.Namespace, topology: Any, device: str, source: str
) -> int:
    """Re-stage the all-muted startup ANCHOR against ``device``. Step 1, no baseline.

    The fleet-typical composite box is mid-commission by design: it has no
    APPLIED baseline, and its boot graph is the all-muted staged startup graph.
    Its ring arm needs the same first step every roleful box needs — the GRAPH
    moves first, so ``jasper-audio-hardware-reconcile`` has a loaded graph to
    derive the endpoint marker FROM. Refusing here left that box with no step 1
    at all, and therefore no way onto (or off) the ring.

    DERIVED FROM PERSISTED STATE ONLY. The re-stage reads the box's own saved
    design draft and crossover preview — the same two files
    ``jasper.active_speaker.web_commissioning._stage_startup_config`` reads when
    it is handed neither a preset nor a preview. The operator supplies exactly
    one thing, ``--endpoint``, which is the act that breaks the marker<->graph
    fixed point; nothing else about the graph is operator-supplied.

    NOTHING LIVE IS TOUCHED UNTIL THE GRAPH PROVES. The staged artifact sits at a
    FIXED path, so writing it IS moving the boot graph — there is no separate
    pointer to gate on. So the re-stage runs against a temporary directory
    first, with the real CamillaDSP validation, and only the exact bytes that
    proved are published over the live artifact. A refusal leaves the box's
    existing anchor untouched, which mirrors the applied path's "a refusal
    writes nothing at all".

    SINGLE-FLIGHT AGAINST A LIVE COMMISSION LOAD. The path this publishes over is
    not only the boot graph — it is the universal RE-MUTE anchor of the audible
    commissioning flow. ``load_driver_commissioning_config`` records it as
    ``previous_config_path``, and four controls reload exactly it: the
    commission-load rollback, ``commission-rollback``, ``commission-ramp abort``,
    and the operator's own by-ear ``commission-ramp ack --outcome too_loud``.
    Moving it to a different endpoint while a driver is armed at level would
    re-point the operator's stop button at a graph whose device this box may not
    be arming yet. So this refuses while a commission load is active, mirroring
    the refusal ``_cmd_commission_load`` already makes for the same shared
    artifact; ``--force`` is the same escape hatch there. Roll back first.

    A STALE ``loaded`` RECORD REFUSES TOO, and that is expected rather than a
    bug to route around. The state file is durable
    (``/var/lib/jasper/active_speaker_commission_load.json``) while per-driver
    commissioning is deliberately transient, so a reboot or a CamillaDSP restart
    mid-commission leaves ``status="loaded"`` on disk with nothing loaded. This
    reads that record RAW — no live-graph consult — because the whole point of
    the command is to work with CamillaDSP down, and
    ``commission_load_runtime_status`` (what ``commission-rollback`` and the web
    wizard overlay to report ``stale``) needs the running graph to answer. The
    way past a stale record is ``--force``, or a ``commission-rollback`` /
    wizard visit, which reconcile the record against the live graph.
    """
    import tempfile

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.runtime_contract import write_camilla_statefile
    from jasper.active_speaker.staging import (
        StagedAnchorLockContended,
        stage_protected_startup_config,
        staged_anchor_lock,
        staged_config_path,
        staged_metadata_path,
    )
    from jasper.atomic_io import atomic_write_json, atomic_write_text

    # Single-flight (see SINGLE-FLIGHT above). Checked BEFORE the stage, so a
    # refused run does no work and touches nothing at all.
    existing = load_commission_load_state()
    if existing.get("status") == "loaded" and not args.force:
        refusal = {
            "status": "refused",
            "reason": "commission_load_active",
            "active_target": existing.get("target"),
            "candidate_config_path": existing.get("candidate_config_path"),
            "next_step": (
                "A per-driver commissioning config is loaded, and this command "
                "republishes the all-muted anchor that commission-rollback / "
                "commission-ramp abort / `ack --outcome too_loud` reload. Run "
                "`commission-rollback` first, or pass --force."
            ),
        }
        if args.json:
            print(json.dumps(refusal, indent=2, sort_keys=True))
        else:
            print("Re-emit refused: a per-driver commissioning load is active.")
            print(f"  active target: {existing.get('target')}")
            print(f"  {refusal['next_step']}")
        return 1

    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)

    with tempfile.TemporaryDirectory(prefix="jts-reemit-anchor-") as tmp:
        proof_dir = Path(tmp)
        payload = stage_protected_startup_config(
            topology,
            crossover_preview=crossover_preview,
            playback_device=device,
            config_dir=proof_dir,
            metadata_path=proof_dir / "staged_metadata.json",
        )
        blockers = [
            issue
            for issue in payload.get("issues") or []
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        if payload.get("status") != "staged" or blockers:
            print(
                "ERROR: could not re-stage the all-muted startup anchor against "
                f"{device}; NOTHING was written"
            )
            for issue in blockers:
                print(
                    f"  [{issue.get('severity')}] {issue.get('code')}: "
                    f"{issue.get('message') or issue.get('detail')}"
                )
            return 1

        proof_path = Path(payload["config"]["path"])
        yaml = proof_path.read_text(encoding="utf-8")

        # RE-PROOF before any byte lands, exactly as the applied path re-proves —
        # and through the SAME host that will select this graph at the next
        # deploy or CamillaDSP restart. Asking the selector rather than the
        # classifier directly is what makes the answer mean "this box will boot
        # from it", not merely "these bytes classify". It also keeps
        # `persisted_candidate` classification owned by one function
        # (`tests/test_active_speaker_cli.py` pins that), instead of teaching a
        # second caller the evidence-pairing rules.
        #
        # The proof graph goes in as `current_config_path` so the selector judges
        # THESE bytes. Left to its default it would read the statefile — the box's
        # existing anchor — and happily preserve the very graph being replaced.
        # The staged metadata is the proof run's own, so the graph is judged
        # against the evidence that describes it rather than the outgoing set.
        proof_decision = safe_graph_for_current_topology(
            topology,
            current_config_path=proof_path,
            applied_baseline_path=baseline_profile_state_path(
                args.applied_baseline_state
            ),
            staged_metadata_path=Path(payload["metadata_path"]),
            # There is no applied baseline on this path by definition; saying so
            # keeps a missing-baseline read out of the decision entirely.
            consider_applied_baseline=False,
        )
        graph = _startup_anchor_from_decision(proof_decision)
        selected = proof_decision.selected_config_path
        if graph is None or selected != str(proof_path):
            print(
                "ERROR: the re-staged startup anchor did not re-prove as "
                f"{GRAPH_ALL_MUTED_ACTIVE_STARTUP}; NOTHING was written"
            )
            print(f"  found:  {_describe_safe_graph_for_refusal(proof_decision)}")
            for issue in proof_decision.issues:
                print(
                    f"  [{issue.get('severity')}] {issue.get('code')}: "
                    f"{issue.get('message')}"
                )
            return 1

        if args.out:
            preview_path = Path(args.out)
            if not preview_path.parent.exists():
                print(f"ERROR: parent directory does not exist: {preview_path.parent}")
                return 1
            atomic_write_text(preview_path, yaml, mode=0o640)
            return _report_startup_anchor_reemit(
                args,
                device=device,
                source=source,
                classification=graph.classification,
                yaml=yaml,
                written_path=preview_path,
                preview=True,
                statefile_written=False,
            )

        # Publish the PROVEN bytes. YAML first, then the metadata that locates
        # it. Both orders are recoverable — the anchor's path is FIXED, so
        # metadata written first would locate the previous, still-valid bytes
        # rather than nothing — but this order keeps the interleaved window
        # pointing at a graph whose evidence is at worst one revision stale,
        # instead of at bytes its evidence has never described.
        #
        # Second writer of this pair; the first is the /sound wizard's own
        # staging (`stage_protected_startup_config`). Both halves go out under
        # the pair's shared `staged_anchor_lock`, so an interleaved run can no
        # longer leave one graph's metadata over another's bytes (#2518). The
        # proof run above deliberately stays outside the hold: it writes only
        # into its temp directory, and holding the live lock across a full
        # re-stage plus CamillaDSP validation would make a /sound/ save wait on
        # work that publishes nothing. A /sound/ stage that lands between the
        # proof and this publish is therefore overwritten wholesale — last pair
        # wins, and each pair stays internally consistent, which is the property
        # #2518 asks for.
        target = staged_config_path()
        meta_target = staged_metadata_path()
        try:
            with staged_anchor_lock(target, source="baseline-reemit"):
                try:
                    target_mode = stat.S_IMODE(target.stat().st_mode)
                except OSError:
                    target_mode = 0o640
                atomic_write_text(
                    target,
                    yaml,
                    mode=target_mode,
                    group_from_parent=True,
                    durable=True,
                )
                # Every field that names a LOCATION is rewritten — the three
                # top-level ones and the validation result's own `path`/`argv`,
                # which `validate_camilla_config` builds from whatever file it
                # was handed and which would otherwise publish the deleted proof
                # directory. Nothing reads those two today
                # (`_staged_candidate_ready` takes only `status`), so this is
                # evidence honesty rather than behaviour — but published
                # evidence naming a path that cannot exist is exactly the kind
                # of thing a later reader trusts. Every remaining field
                # describes the graph itself and is location-independent, so the
                # published evidence stays the evidence that proved.
                published = dict(payload)
                published["metadata_path"] = str(meta_target)
                published["config"] = {
                    **payload["config"],
                    "path": str(target),
                    "basename": target.name,
                    "validation": _relocate_validation_evidence(
                        payload["config"].get("validation"), proof_path, target
                    ),
                }
                # durable=True to match the graph write above: this file is what
                # LOCATES the graph, so losing it to a power cut while the
                # durable half survives leaves the box off its anchor until
                # someone re-stages. It parks silent and still takes deploys,
                # but the two halves should survive together.
                atomic_write_json(
                    meta_target,
                    published,
                    mode=0o640,
                    group_from_parent=True,
                    durable=True,
                )
        except StagedAnchorLockContended as exc:
            print(
                "ERROR: another writer is publishing the startup anchor "
                f"({exc}); NOTHING was written"
            )
            return 1

        statefile = Path(args.statefile)
        statefile_written = False
        if read_camilla_statefile_config_path(statefile) != str(target):
            write_camilla_statefile(statefile, target)
            statefile_written = True

        return _report_startup_anchor_reemit(
            args,
            device=device,
            source=source,
            classification=graph.classification,
            yaml=yaml,
            written_path=target,
            preview=False,
            statefile_written=statefile_written,
        )


def _report_startup_anchor_reemit(
    args: argparse.Namespace,
    *,
    device: str,
    source: str,
    classification: str,
    yaml: str,
    written_path: Path,
    preview: bool,
    statefile_written: bool,
) -> int:
    payload = {
        "playback_device": device,
        "playback_device_source": source,
        "classification": classification,
        "preview": preview,
        "written_path": str(written_path),
        "statefile_path": str(args.statefile),
        "statefile_written": statefile_written,
        "bytes": len(yaml),
        "reemitted": "staged_startup_anchor",
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Re-staged all-muted startup anchor against playback_device={device}")
    print(f"  source:         {source}")
    print(f"  classification: {classification}")
    print(f"  bytes:          {len(yaml)}")
    if preview:
        print(f"  PREVIEW only:   {written_path}")
        print("  (live artifact, staged metadata and statefile untouched)")
    else:
        print(f"  wrote:          {written_path}")
        print(
            "  statefile:      "
            + (
                f"repointed -> {written_path}"
                if statefile_written
                else "already correct"
            )
        )
    return 0


def _cmd_baseline_reemit(args: argparse.Namespace) -> int:
    """Re-emit the APPLIED active baseline against a chosen playback endpoint.

    WHY THIS EXISTS. The applied baseline is a roleful box's BOOT graph — the
    statefile points at it, and ``safe_graph_for_current_topology`` preserves or
    re-selects it on every deploy and every CamillaDSP restart. Its on-disk
    artifact names whichever playback endpoint was resolved when it was emitted.
    So after the active endpoint MOVES — the ring-v2 arm being the case that
    creates this — the artifact still names the old lane, and the next Camilla
    restart quietly de-arms the box: CamillaDSP writes snd-aloop while outputd
    reads the ring, and the speaker goes silent with every daemon reporting
    healthy.

    AND IT IS THE BOOTSTRAP. The endpoint marker derives from the loaded graph;
    the graph's device derives from the marker. That was a fixed point: at
    marker-absent the pair could only reproduce itself, so a box could neither
    arm nor release. ``resolve_output_layout`` no longer reads the marker, which
    removes the circle; ``--endpoint ring`` remains the explicit re-emit-now verb
    that moves the GRAPH first, which is why the arm ladder is
    ``baseline-reemit --endpoint ring`` -> ``jasper-audio-hardware-reconcile``
    (the marker derives 1) -> ``jasper-fanin-coupling-reconcile shm_ring``. It
    has no mirror: the release direction was retired with the aloop endpoint.

    ON THE APPLIED PATH it is a pure re-emit from the IMMUTABLE applied snapshot
    — the same seam ``/sound`` and the commissioning host use — so Layer A is
    rebuilt from the evidence that was applied, not from any current draft, and
    the only thing that moves is the endpoint the graph is emitted against. The
    ANCHOR path below has no immutable snapshot to rebuild from: it re-stages
    from the box's CURRENT design draft and crossover preview, so a draft edited
    since the anchor was last staged does land in the re-staged graph. That is
    the same derivation the ``/sound`` wizard's own staging runs, through the
    same bind/protection/all-muted gates, and the result is all-muted either way
    — but it is a real difference between the two paths, not a shared "only the
    endpoint moves" guarantee.

    TWO GRAPH CLASSES ARE ACCEPTED, because a roleful box has two legal boot
    graphs and both have to be able to take step 1:

    * ``approved_active_runtime`` — a commissioned box's APPLIED baseline. The
      path above.
    * ``all_muted_active_startup`` — the all-muted startup ANCHOR a
      mid-commission box boots from, which is the fleet-typical composite state
      (no applied baseline yet). Re-staged from the box's own persisted design
      draft and crossover preview by
      :func:`_reemit_staged_startup_anchor`.

    Precedence is applied-baseline-first: when a baseline exists it is re-emitted
    and the anchor is left alone, so a commissioned box behaves exactly as it did
    before the anchor path existed. Anything else — a parked graph, an
    unrecognised class, a topology with no roleful outputs — is refused by name
    rather than guessed at.

    WHAT IT WRITES. By default, the artifact the statefile and the classifier
    actually read: the applied profile's own ``config.path``. The bytes are
    published atomically at the target's existing mode, the canonical
    ``active_speaker_baseline.yml`` copy is refreshed, and the statefile is
    pointed at the artifact (a no-op when it already is). Nothing is written
    until the recomposed graph RE-PROVES as ``GRAPH_APPROVED_ACTIVE_RUNTIME``;
    a refusal writes nothing at all and exits non-zero.

    ``--out`` is a PREVIEW: it writes the emitted YAML to that path and touches
    nothing else — no live artifact, no canonical copy, no statefile. The
    re-proof still gates it, so a preview file is never a graph the contract
    rejected.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        promote_applied_baseline_candidate,
        recompose_applied_baseline_yaml,
    )
    from jasper.active_speaker.runtime_contract import (
        classify_bass_extension_graph,
        write_camilla_statefile,
    )
    from jasper.atomic_io import atomic_write_text
    from jasper.bass_extension.profile import evaluate_bass_extension_profile

    topology = load_output_topology_strict(args.topology)
    applied = load_applied_baseline_profile_state(args.applied_baseline_state)
    device, source = _baseline_reemit_endpoint(topology, args.endpoint)
    if not device:
        print(
            "ERROR: this topology resolves no active playback endpoint, so there "
            "is no device to re-emit against"
        )
        return 1

    if not applied:
        # No applied baseline is the fleet-typical MID-COMMISSION state, not a
        # broken one — so ask what this box is actually booting from before
        # refusing. An all-muted startup anchor is a legal roleful boot graph and
        # gets step 1; anything else is named and refused.
        decision = safe_graph_for_current_topology(
            topology,
            statefile_path=args.statefile,
            applied_baseline_path=baseline_profile_state_path(
                args.applied_baseline_state
            ),
        )
        if _startup_anchor_from_decision(decision) is not None:
            return _reemit_staged_startup_anchor(args, topology, device, source)
        print(
            "ERROR: no APPLIED active-speaker baseline profile is saved, and this "
            "box is not on the all-muted active startup graph either, so there is "
            "nothing to re-emit"
        )
        print(f"  found:  {_describe_safe_graph_for_refusal(decision)}")
        print(
            f"  accepts: an applied baseline ({GRAPH_APPROVED_ACTIVE_RUNTIME}) or "
            f"the all-muted startup anchor ({GRAPH_ALL_MUTED_ACTIVE_STARTUP})"
        )
        print(
            "  next:   commission the speaker at http://jts.local/sound/setup/ "
            "(stage a protected startup config), then re-run this command"
        )
        return 1

    # Bass evidence is split exactly as the /sound recompose splits it: only an
    # ACCEPTED profile is emitted, while the proof is asked against whatever was
    # evaluated, so a rejected profile cannot be silently emitted OR silently
    # excused.
    bass_evaluation = evaluate_bass_extension_profile(
        topology=topology, applied_baseline_state=applied
    )
    bass_emission_profile = (
        bass_evaluation.profile if bass_evaluation.status == "accepted" else None
    )
    yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=device,
        out_path=None,
        bass_extension_profile=bass_emission_profile,
    )
    if yaml is None or issues:
        print("ERROR: could not re-emit the applied baseline:")
        for issue in issues or []:
            print(
                f"  [{issue.get('severity')}] {issue.get('code')}: "
                f"{issue.get('message') or issue.get('detail')}"
            )
        return 1

    # RE-PROOF before any byte lands. This graph is about to become the box's
    # boot graph, so it is held to the same contract the runtime holds a loaded
    # graph to — and it is re-derived here rather than trusted from the emitter,
    # because the emitter is the thing being checked.
    graph = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=yaml,
        applied_baseline_state=applied,
        desired_profile=bass_evaluation.profile,
    )
    if not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
        print(
            "ERROR: the re-emitted baseline did not re-prove as "
            f"{GRAPH_APPROVED_ACTIVE_RUNTIME} (got {graph.classification}); "
            "NOTHING was written"
        )
        for issue in graph.issues:
            print(
                f"  [{issue.get('severity')}] {issue.get('code')}: "
                f"{issue.get('message')}"
            )
        return 1

    preview_path = Path(args.out) if args.out else None
    written_path: Path | None = None
    statefile_written = False
    if preview_path is not None:
        if not preview_path.parent.exists():
            print(
                f"ERROR: parent directory does not exist: {preview_path.parent}"
            )
            return 1
        atomic_write_text(preview_path, yaml, mode=0o640)
        written_path = preview_path
    else:
        applied_config = applied.get("config")
        raw_target = (
            applied_config.get("path") if isinstance(applied_config, Mapping) else None
        )
        if not isinstance(raw_target, str) or not raw_target.strip():
            print(
                "ERROR: the applied baseline profile records no config path, so "
                "there is no artifact to re-emit over; NOTHING was written"
            )
            return 1
        target = Path(raw_target)
        # Preserve the target's own mode when it exists (this rewrites a file
        # someone else created); fall back to the module's 0640 convention when
        # it does not.
        try:
            target_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            target_mode = 0o640
        atomic_write_text(
            target,
            yaml,
            mode=target_mode,
            group_from_parent=True,
            durable=True,
        )
        written_path = target
        # Keep the canonical readable copy in step (fail-soft by its own
        # contract), then make sure the boot pointer names the artifact we just
        # rewrote — idempotent when it already does.
        promote_applied_baseline_candidate(applied)
        statefile = Path(args.statefile)
        if read_camilla_statefile_config_path(statefile) != str(target):
            write_camilla_statefile(statefile, target)
            statefile_written = True

    payload = {
        "playback_device": device,
        "playback_device_source": source,
        "classification": graph.classification,
        "preview": preview_path is not None,
        "written_path": str(written_path) if written_path else None,
        "statefile_path": str(args.statefile),
        "statefile_written": statefile_written,
        "bytes": len(yaml),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Re-emitted applied baseline against playback_device={device}")
        print(f"  source:         {source}")
        print(f"  classification: {graph.classification}")
        print(f"  bytes:          {len(yaml)}")
        if preview_path is not None:
            print(f"  PREVIEW only:   {preview_path}")
            print("  (live artifact, canonical copy and statefile untouched)")
        else:
            print(f"  wrote:          {written_path}")
            print(
                "  statefile:      "
                + (
                    f"repointed -> {written_path}"
                    if statefile_written
                    else "already correct"
                )
            )
    return 0


def _camilla_controller() -> Any:
    """Return a CamillaController bound to the live CamillaDSP websocket.

    An operator running the commission-load CLI reaches the same running graph
    as the daemons and web wizards.
    """
    from jasper.camilla import primary_controller

    return primary_controller()


def _resolve_commission_inputs(
    args: argparse.Namespace,
) -> tuple[ActiveSpeakerPreset | None, dict[str, Any] | None]:
    """Resolve (preset, crossover_preview) for a commission command.

    Loads the optional ``--preset`` file (CLI-specific), then delegates to the
    shared :func:`resolve_commission_inputs` so the preview/fallback choice
    matches what protected staging and the web card do.
    """
    preset = (
        ActiveSpeakerPreset.from_mapping(
            _load_json_object(Path(args.preset), label="preset")
        )
        if args.preset
        else None
    )
    return resolve_commission_inputs(preset)


def _print_commission_load_summary(payload: dict[str, Any], *, dry_run: bool) -> None:
    load = payload.get("load") or {}
    preflight = payload.get("preflight") or {}
    target = (load.get("target") or preflight.get("target") or {})
    if dry_run:
        print(f"Commission-load preflight: {preflight.get('status')}")
        print(
            f"  load_allowed: {'yes' if preflight.get('load_allowed') else 'no'}"
        )
    else:
        print(f"Commission load: {load.get('status')}")
    print(
        f"  target: group={target.get('speaker_group_id')} "
        f"role={target.get('role')} outputs={target.get('audible_outputs')}"
    )
    candidate = load.get("candidate_config_path") or preflight.get(
        "candidate_config_path"
    )
    print(f"  candidate config: {candidate}")
    if not dry_run:
        print(f"  rollback anchor (staged boot config): {load.get('previous_config_path')}")
        print(
            "  durable statefile intact (crash-recovery-MUTED): "
            f"{load.get('durable_statefile_intact')}"
        )
        live = load.get("live_evidence") or {}
        print(
            "  live read-back gate: "
            f"{'passed' if live.get('passed') else 'failed/none'}"
        )
    gates = preflight.get("required_gates") or []
    failed_gates = [g for g in gates if not g.get("passed")]
    if failed_gates:
        print("  failed gates:")
        for gate in failed_gates:
            print(f"    - {gate['id']}: {gate.get('message')}")
    issues = load.get("issues") or preflight.get("issues") or []
    if issues:
        print("  issues:")
        for issue in issues:
            print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")
    if not dry_run and load.get("status") == "loaded":
        print(
            "Armed at the protected floor (gain -120 dB, mute off) — SILENT. "
            "The audible level is the Stage-5 ramp; no audio was emitted by this load."
        )


def _commission_load_exit_code(payload: dict[str, Any], *, dry_run: bool) -> int:
    if dry_run:
        return 0 if (payload.get("preflight") or {}).get("load_allowed") else 1
    return 0 if (payload.get("load") or {}).get("status") == "loaded" else 1


def _cmd_commission_load(args: argparse.Namespace) -> int:
    # Single-flight: an armed per-driver commissioning load is exclusive. The
    # commissioning config path is shared, so refuse a second concurrent arm
    # rather than silently overwrite a live load — roll back first. (Stage-5
    # gain-ramp re-loads of the SAME armed target go through their own command,
    # not this one.)
    existing = load_commission_load_state()
    if existing.get("status") == "loaded" and not args.force:
        refusal = {
            "status": "refused",
            "reason": "commission_load_already_active",
            "active_target": existing.get("target"),
            "candidate_config_path": existing.get("candidate_config_path"),
            "next_step": (
                "A per-driver commissioning config is already loaded. Run "
                "`commission-rollback` to return to the all-muted staged config, "
                "or pass --force to re-arm."
            ),
        }
        if args.json:
            print(json.dumps(refusal, indent=2, sort_keys=True))
        else:
            print("Commission load refused: a load is already active.")
            print(f"  active target: {existing.get('target')}")
            print(f"  {refusal['next_step']}")
        return 1

    topology = load_output_topology_strict(args.topology)
    staged = load_staged_startup_config()
    preset, crossover_preview = _resolve_commission_inputs(args)
    cam = _camilla_controller()

    async def _run() -> dict[str, Any]:
        current_config_path, current_config_error = await read_current_config_path(cam)
        evidence_path = write_commission_path_safety(
            topology, staged, current_config_path, current_config_error
        )
        if args.dry_run:
            return {
                "preflight": build_driver_commission_load_preflight(
                    topology,
                    speaker_group_id=args.group,
                    role=args.role,
                    staged_config=staged,
                    preset=preset,
                    crossover_preview=crossover_preview,
                    path_safety_evidence_path=evidence_path,
                    current_config_path=current_config_path,
                ),
                "load": {},
            }
        load_config, read_running_config, get_current_config_path = commission_seams(cam)
        return await load_driver_commissioning_config(
            topology,
            speaker_group_id=args.group,
            role=args.role,
            load_config=load_config,
            read_running_config=read_running_config,
            get_current_config_path=get_current_config_path,
            preset=preset,
            crossover_preview=crossover_preview,
            staged_config=staged,
            path_safety_evidence_path=evidence_path,
        )

    payload = asyncio.run(_run())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_commission_load_summary(payload, dry_run=args.dry_run)
    return _commission_load_exit_code(payload, dry_run=args.dry_run)


def _cmd_commission_rollback(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    payload = asyncio.run(
        rollback_driver_commissioning_config(
            load_config=commission_load_config(cam),
        )
    )
    rollback = payload.get("rollback") or {}
    if rollback.get("status") == "rolled_back":
        # The graph is proven back on the all-muted anchor, so the step the ramp
        # was waiting on is gone with it. Only a proven rollback clears it: a
        # blocked / failed one may still be audible.
        payload["ramp"] = clear_pending_ramp_step()
    # Unconditional, exactly as the web twin and `abort_ramp` do it: a rollback
    # ATTEMPT ends the operator's playback authority whatever the graph did, and
    # revoking it only ever makes the ramp gate stricter. Without this the CLI
    # would be the one re-mute path that clears the step but leaves the floor
    # tri-state armed, so `commission-ramp status` would print "pending step:
    # None" beside "floor_pending_operator" until the session's TTL expired.
    payload["safe_playback"] = stop_safe_playback_session(
        reason="commission_rollback"
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Commission rollback: {rollback.get('status')}")
        print(f"  reloaded staged boot config: {rollback.get('active_config_path')}")
        for issue in rollback.get("issues") or []:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    return 0 if rollback.get("status") in {"rolled_back", "blocked"} else 1


def _print_ramp_step_summary(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    print(f"Stage-5 ramp step: {status}")
    print(
        f"  target: group={payload.get('speaker_group_id')} role={payload.get('role')}"
    )
    gate = payload.get("gate") or {}
    if gate:
        print(
            "  gain: "
            f"{gate.get('current_gain_db')} -> {gate.get('next_gain_db')} dB"
        )
        failed = sorted(k for k, ok in (gate.get("checks") or {}).items() if not ok)
        if failed:
            print(f"  gate failed: {', '.join(failed)}")
    safe = payload.get("safe_playback") or {}
    if safe:
        print(
            "  per-driver floor: "
            f"{safe.get('floor_status')} (awaiting operator ACK)"
        )
    if status == "stepped":
        print(
            "  The driver is now AUDIBLE at this level. Confirm by ear, then run "
            "`commission-ramp ack --outcome heard_correct_driver` (or too_loud / "
            "silent / heard_wrong_driver). `commission-ramp abort` re-mutes."
        )
    for issue in payload.get("issues") or []:
        print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")


def _cmd_commission_ramp_step(args: argparse.Namespace) -> int:
    topology = load_output_topology_strict(args.topology)
    staged = load_staged_startup_config()
    preset, crossover_preview = _resolve_commission_inputs(args)
    cam = _camilla_controller()

    async def _run() -> dict[str, Any]:
        current_config_path, current_config_error = await read_current_config_path(cam)
        evidence_path = write_commission_path_safety(
            topology, staged, current_config_path, current_config_error
        )
        load_config, read_running_config, get_current_config_path = commission_seams(cam)
        return await ramp_audible_step(
            topology,
            speaker_group_id=args.group,
            role=args.role,
            load_config=load_config,
            read_running_config=read_running_config,
            get_current_config_path=get_current_config_path,
            preset=preset,
            crossover_preview=crossover_preview,
            path_safety_evidence_path=evidence_path,
            staged_config=staged,
            confirmed_roles=confirmed_driver_roles(
                topology,
                speaker_group_id=args.group,
            ),
        )

    payload = asyncio.run(_run())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_ramp_step_summary(payload)
    return 0 if payload.get("status") == "stepped" else 1


def _cmd_commission_ramp_ack(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    # load_config lets terminal by-ear outcomes re-mute the transient graph.
    payload = asyncio.run(
        record_ramp_operator_ack(
            outcome=args.outcome,
            load_config=commission_load_config(cam),
        )
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Stage-5 ramp ack ({args.outcome}): {payload.get('status')}")
        safe = payload.get("safe_playback") or {}
        if safe:
            print(f"  per-driver floor: {safe.get('floor_status')}")
        rollback = payload.get("rollback")
        if rollback:
            print(f"  re-muted via rollback: {rollback.get('status')}")
        for issue in payload.get("issues") or []:
            print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    return 0 if payload.get("status") in {"confirmed", "retry", "aborted"} else 1


def _cmd_commission_ramp_status(args: argparse.Namespace) -> int:
    commission = load_commission_load_state()
    ramp = load_ramp_state()
    target = commission.get("target") or {}
    group = str(
        target.get("speaker_group_id") or ramp.get("speaker_group_id") or ""
    ).strip()
    durable_confirmed: list[str] = []
    if group:
        try:
            topology = load_output_topology_strict(args.topology)
        except OutputTopologyError:
            durable_confirmed = []
        else:
            durable_confirmed = confirmed_driver_roles(topology, speaker_group_id=group)
    payload = {
        "commission_load": commission,
        "ramp": {
            **ramp,
            "confirmed_roles": effective_confirmed_roles(
                ramp,
                speaker_group_id=group,
                confirmed_roles=durable_confirmed,
            ),
        },
        "safe_playback": load_safe_playback_state(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        commission = payload["commission_load"]
        ramp = payload["ramp"]
        quiet = (payload["safe_playback"].get("quiet_start") or {})
        target = commission.get("target") or {}
        print(f"Commission load: {commission.get('status')}")
        print(
            f"  armed target: group={target.get('speaker_group_id')} "
            f"role={target.get('role')} gain={target.get('audible_gain_db')} dB"
        )
        print(f"Ramp: confirmed_roles={ramp.get('confirmed_roles')}")
        print(f"  pending step: {ramp.get('pending')}")
        print(f"Per-driver floor tri-state: {quiet.get('status')}")
    return 0


def _cmd_commission_ramp_abort(args: argparse.Namespace) -> int:
    cam = _camilla_controller()
    payload = asyncio.run(abort_ramp(load_config=commission_load_config(cam)))
    rollback = payload.get("rollback") or {}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Stage-5 ramp abort: {payload.get('status')}")
        print(f"  re-muted via rollback: {rollback.get('status')}")
    return 0 if rollback.get("status") in {"rolled_back", "blocked"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-active-speaker",
        description="Generate and inspect active-speaker commissioning artifacts",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    template = sub.add_parser(
        "startup-template",
        help="write a muted/protected active-speaker CamillaDSP startup template",
    )
    template.add_argument("preset", help="path to an active-speaker preset JSON file")
    template.add_argument(
        "--playback-device",
        required=True,
        help="explicit active-hardware playback device, e.g. hw:MultiChannelDAC",
    )
    template.add_argument(
        "--output",
        "-o",
        required=True,
        help="path to write the generated CamillaDSP YAML",
    )
    template.add_argument(
        "--baseline-id",
        help="optional baseline id embedded in the generated template comment",
    )
    template.add_argument(
        "--check",
        dest="check",
        action="store_true",
        default=True,
        help="run camilladsp --check when the binary is available (default)",
    )
    template.add_argument(
        "--no-check",
        dest="check",
        action="store_false",
        help="write the template without CamillaDSP syntax preflight",
    )
    template.add_argument("--json", action="store_true")
    template.set_defaults(func=_cmd_startup_template)

    path_audit = sub.add_parser(
        "path-audit",
        help="evaluate or list active-speaker audible-path safety gates",
    )
    path_audit.add_argument(
        "evidence",
        nargs="?",
        help="path to path-safety evidence JSON",
    )
    path_audit.add_argument(
        "--requirements",
        action="store_true",
        help="print the required audible-path evidence checklist",
    )
    path_audit.add_argument("--json", action="store_true")
    path_audit.set_defaults(func=_cmd_path_audit)

    path_probe = sub.add_parser(
        "path-probe",
        help="generate no-audio startup-load path-safety evidence",
    )
    path_probe.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    path_probe.add_argument(
        "--current-config",
        help=(
            "current CamillaDSP config path to treat as the rollback target; "
            "omitting it writes blocked evidence"
        ),
    )
    path_probe.add_argument(
        "--output",
        "-o",
        help=(
            "where to write path-safety evidence "
            "(default: JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE or /var/lib/jasper)"
        ),
    )
    path_probe.add_argument("--json", action="store_true")
    path_probe.set_defaults(func=_cmd_path_probe)

    environment = sub.add_parser(
        "environment-probe",
        help="read active-speaker environment evidence without playback or reloads",
    )
    environment.add_argument(
        "--config",
        help=(
            "CamillaDSP config to inspect; when omitted, read config_path from "
            "the CamillaDSP statefile"
        ),
    )
    environment.add_argument(
        "--statefile",
        help=(
            "CamillaDSP statefile to read when --config is omitted "
            "(default: JASPER_CAMILLA_STATEFILE or outputd-statefile.yml)"
        ),
    )
    environment.add_argument(
        "--path-safety-evidence",
        help="optional active-speaker path-safety evidence JSON",
    )
    environment.add_argument(
        "--check-config",
        dest="check_config",
        action="store_true",
        default=True,
        help="run camilladsp --check on the inspected config when available (default)",
    )
    environment.add_argument(
        "--no-check-config",
        dest="check_config",
        action="store_false",
        help="skip CamillaDSP config validation; load gate will remain blocked",
    )
    environment.add_argument("--json", action="store_true")
    environment.set_defaults(func=_cmd_environment_probe)

    runtime = sub.add_parser(
        "runtime-safe-graph",
        help=(
            "select the safe persisted CamillaDSP graph for the saved output "
            "topology; optionally repair the outputd statefile"
        ),
    )
    runtime.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    runtime.add_argument(
        "--statefile",
        default="/var/lib/camilladsp/outputd-statefile.yml",
        help="outputd CamillaDSP statefile to inspect/write",
    )
    runtime.add_argument(
        "--current-config",
        help="current CamillaDSP config path; when omitted, read --statefile",
    )
    runtime.add_argument(
        "--flat-config",
        default=str(DEFAULT_FLAT_OUTPUTD_CONFIG),
        help="normal full-range outputd config path",
    )
    runtime.add_argument(
        "--coupling",
        default=None,
        help=(
            "persisted fan-in coupling; when omitted, read from fanin.env"
        ),
    )
    runtime.add_argument(
        "--applied-baseline-state",
        help=(
            "saved active-speaker baseline profile state to prefer when it "
            "has status=applied (default: active_speaker_baseline_profile.json)"
        ),
    )
    runtime.add_argument(
        "--no-applied-baseline",
        action="store_true",
        help="ignore any saved applied active-speaker baseline profile",
    )
    runtime.add_argument(
        "--staged-metadata",
        help=(
            "active-speaker staged metadata path "
            "(default: JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH or /var/lib/jasper)"
        ),
    )
    runtime.add_argument(
        "--write-statefile",
        action="store_true",
        help="write --statefile to the selected safe config path",
    )
    runtime.add_argument("--json", action="store_true")
    runtime.set_defaults(func=_cmd_runtime_safe_graph)

    reemit = sub.add_parser(
        "baseline-reemit",
        help=(
            "re-emit this box's roleful boot graph against a playback endpoint, "
            "publishing it over the live artifact and repointing the statefile. "
            "This is the FIRST step of the active-ring arm (--endpoint ring), "
            "which has no rollback: the reconciler derives its endpoint marker "
            "from the loaded graph, so the graph must move first. "
            "Accepts either roleful boot graph — an APPLIED baseline "
            "(approved_active_runtime) on a commissioned box, or the all-muted "
            "startup anchor (all_muted_active_startup) on a mid-commission one, "
            "which is re-staged from the box's own saved design draft and "
            "crossover preview. A baseline wins when both are present; any other "
            "graph class is refused by name"
        ),
        # `help=` shows in the PARENT listing; `description=` is what an operator
        # running `baseline-reemit --help` actually reads. The accepted graph
        # classes are spelled from the runtime contract's own constants so this
        # text cannot drift away from what the command really accepts.
        description=(
            "Re-emit this box's roleful boot graph against a playback endpoint. "
            "Two graph classes are accepted: "
            f"'{GRAPH_APPROVED_ACTIVE_RUNTIME}' (a commissioned box's APPLIED "
            "baseline, re-emitted from its immutable snapshot) and "
            f"'{GRAPH_ALL_MUTED_ACTIVE_STARTUP}' (a mid-commission box's "
            "all-muted startup anchor, re-staged from its own saved design draft "
            "and crossover preview). An applied baseline takes precedence when "
            "both are present. Any other graph class — a parked graph, an "
            "unrecognised one, or a topology with no roleful outputs — is refused "
            "by name rather than guessed at. This is the FIRST step of the "
            "active-ring arm (--endpoint ring), which has no rollback: the "
            "reconciler derives its endpoint marker from the loaded graph, so "
            "the graph must move first."
        ),
    )
    reemit.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    reemit.add_argument(
        "--applied-baseline-state",
        help=(
            "saved active-speaker baseline profile state "
            "(default: active_speaker_baseline_profile.json)"
        ),
    )
    reemit.add_argument(
        "--endpoint",
        choices=("ring",),
        help=(
            "playback endpoint to emit against. 'ring' (the ACTIVE ring, "
            "jts_ring_active_playback) is the only legal endpoint and so the "
            "only choice: pass it to re-emit the graph onto the ring now. Omit "
            "to keep the endpoint the box already resolves"
        ),
    )
    reemit.add_argument(
        "--statefile",
        default="/var/lib/camilladsp/outputd-statefile.yml",
        help="CamillaDSP statefile to point at the re-emitted artifact",
    )
    reemit.add_argument(
        "--out",
        help=(
            "PREVIEW: write the re-emitted YAML here and touch nothing else — "
            "no live artifact, no canonical copy, no statefile"
        ),
    )
    reemit.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-stage the startup anchor even while a per-driver commissioning "
            "load is active. Refused by default: this command republishes the "
            "all-muted anchor that commission-rollback and the Stage-5 ramp's "
            "abort / `ack --outcome too_loud` reload, so moving it mid-load "
            "re-points the operator's own stop control"
        ),
    )
    reemit.add_argument("--json", action="store_true")
    reemit.set_defaults(func=_cmd_baseline_reemit)

    commission_load = sub.add_parser(
        "commission-load",
        help=(
            "load a per-driver commissioning config into the RUNNING CamillaDSP "
            "graph (armed at the protected floor — SILENT)"
        ),
    )
    commission_load.add_argument(
        "--group",
        required=True,
        help="speaker group id to commission (must be the single active group)",
    )
    commission_load.add_argument(
        "--role",
        required=True,
        help="driver role to arm audible (e.g. woofer, tweeter)",
    )
    commission_load.add_argument(
        "--preset",
        help=(
            "optional preset JSON override (preset-fallback mode); default loads "
            "the saved crossover preview to match protected staging"
        ),
    )
    commission_load.add_argument(
        "--topology",
        help="optional output-topology JSON path (default: JTS output topology state)",
    )
    commission_load.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run the guarded preflight only (writes the candidate config; loads "
            "nothing, emits no audio)"
        ),
    )
    commission_load.add_argument(
        "--force",
        action="store_true",
        help="re-arm even if a commissioning load is already active (single-flight override)",
    )
    commission_load.add_argument("--json", action="store_true")
    commission_load.set_defaults(func=_cmd_commission_load)

    commission_rollback = sub.add_parser(
        "commission-rollback",
        help=(
            "reload the all-muted staged config, ending a per-driver "
            "commissioning load (returns the speaker to everything-muted)"
        ),
    )
    commission_rollback.add_argument("--json", action="store_true")
    commission_rollback.set_defaults(func=_cmd_commission_rollback)

    ramp = sub.add_parser(
        "commission-ramp",
        help=(
            "Stage-5: raise an armed driver from the silent floor to a low audible "
            "level, one gated step at a time (operator-confirmed, woofer first)"
        ),
    )
    ramp_sub = ramp.add_subparsers(dest="ramp_action", required=True)

    ramp_step = ramp_sub.add_parser(
        "step", help="take one gated audible gain step on the armed driver"
    )
    ramp_step.add_argument("--group", required=True, help="armed speaker group id")
    ramp_step.add_argument("--role", required=True, help="armed driver role")
    ramp_step.add_argument(
        "--preset",
        help="optional preset JSON override (must match the armed load)",
    )
    ramp_step.add_argument("--topology", help="optional output-topology JSON path")
    ramp_step.add_argument("--json", action="store_true")
    ramp_step.set_defaults(func=_cmd_commission_ramp_step)

    ramp_ack = ramp_sub.add_parser(
        "ack", help="record the operator's verdict for the pending audible step"
    )
    ramp_ack.add_argument(
        "--outcome",
        required=True,
        choices=sorted(FLOOR_OPERATOR_OUTCOMES),
        help=(
            "heard_correct_driver confirms; too_loud / heard_wrong_driver re-mute; "
            "silent allows a louder retry"
        ),
    )
    ramp_ack.add_argument("--json", action="store_true")
    ramp_ack.set_defaults(func=_cmd_commission_ramp_ack)

    ramp_status = ramp_sub.add_parser(
        "status", help="show the commission-load, ramp, and per-driver floor state"
    )
    # The handler reads args.topology to merge durable confirmed-role evidence
    # for the armed group; without this flag the read raises AttributeError on
    # every box that has ever armed a driver (the armed target outlives a
    # rollback), which is the whole life of the verb after the first arm.
    ramp_status.add_argument("--topology", help="optional output-topology JSON path")
    ramp_status.add_argument("--json", action="store_true")
    ramp_status.set_defaults(func=_cmd_commission_ramp_status)

    ramp_abort = ramp_sub.add_parser(
        "abort", help="re-mute: roll back to the all-muted staged config and reset"
    )
    ramp_abort.add_argument("--json", action="store_true")
    ramp_abort.set_defaults(func=_cmd_commission_ramp_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Commissioning applies the candidate graph inline (commission_load_config
    # -> set_active_config_raw), so its swap duck needs a canonical target.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ActiveSpeakerConfigError, OutputTopologyError, OSError) as e:
        parser.exit(2, f"{parser.prog}: error: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
