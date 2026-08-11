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

from jasper.active_speaker import ActiveSpeakerConfigError, ActiveSpeakerPreset
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
    DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    PARKED_MUTED_EXITS,
    PARKED_MUTED_STATUS,
    apply_safe_graph_decision_to_statefile,
    safe_graph_for_current_topology,
)
from jasper.active_speaker.staging import load_staged_startup_config
from jasper.active_speaker.startup_load import (
    build_driver_commission_load_preflight,
    load_commission_load_state,
    load_driver_commissioning_config,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.commission_ramp import (
    abort_ramp,
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
)
from jasper.dsp_apply import validate_camilla_config
from jasper.output_topology import OutputTopologyError, load_output_topology_strict


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
        # blockers for an operator to decode. Name the two exits and stop —
        # the blocker wall stays for a genuinely unsafe graph.
        print(f"  next: {PARKED_MUTED_EXITS}")
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
        from jasper.fanin.coupling_reconcile import read_persisted_coupling

        coupling = read_persisted_coupling()
    topology = load_output_topology_strict(args.topology)
    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=args.statefile,
        current_config_path=args.current_config,
        flat_config_path=args.flat_config,
        ring_flat_config_path=args.ring_flat_config,
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
            wrote = apply_safe_graph_decision_to_statefile(
                decision,
                statefile_path=args.statefile,
                # Same topology object the decision was made from, so the
                # write-time all-muted re-proof cannot be answered by a
                # second, differently-read topology.
                topology=topology,
            )
        except ActiveSpeakerConfigError as exc:
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
        _print_runtime_safe_graph_summary(payload, wrote_statefile=wrote)
    return 0 if decision.ok else 1


def _baseline_reemit_endpoint(
    topology: Any, endpoint: str | None
) -> tuple[str | None, str]:
    """Which playback endpoint this re-emit targets, and where that came from.

    ``--endpoint`` is what makes this command the ARM/ROLLBACK entry point
    rather than a tidy-up. The reconciler derives its ring marker FROM the
    loaded graph, and the graph's device derives from that marker, so an
    auto-resolving re-emit can only ever reproduce the state the box is already
    in — it cannot bootstrap either direction. Naming the endpoint explicitly is
    the operator act that breaks that circle: the GRAPH moves first, the marker
    then derives from it, and the coupling follows.

    Omitting it keeps the auto answer (``resolve_output_layout``, the single
    chooser, reading the marker) — correct for a plain refresh, and never able
    to change which lane the box is on.
    """
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    if endpoint == "ring":
        return RING_ACTIVE_PLAYBACK_DEVICE, "explicit_endpoint_ring"
    if endpoint == "aloop":
        return OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "explicit_endpoint_aloop"
    return resolve_active_playback_device(topology)


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
    the graph's device derives from the marker. That is a fixed point: at
    marker-absent the pair can only reproduce itself, in BOTH directions — a box
    can neither arm nor release. ``--endpoint`` is the explicit operator act
    that breaks it by moving the GRAPH first, which is why the arm ladder is
    ``baseline-reemit --endpoint ring`` -> ``jasper-audio-hardware-reconcile``
    (the marker derives 1) -> ``jasper-fanin-coupling-reconcile shm_ring``, and
    the rollback is its mirror through ``--endpoint aloop``.

    It is a pure re-emit from the IMMUTABLE applied snapshot — the same seam
    ``/sound`` and the commissioning host use — so Layer A is rebuilt from the
    evidence that was applied, not from any current draft. The only thing that
    moves is the endpoint the graph is emitted against.

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

    The full ladder, its ordering rationale, and why every intermediate state is
    silence rather than wrong audio live in
    ``docs/HANDOFF-audio-graph-consolidation.md`` ("The ACTIVE-ring arm/rollback
    lifecycle") — one home, pointed at from here rather than restated.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        promote_applied_baseline_candidate,
        recompose_applied_baseline_yaml,
    )
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_bass_extension_graph,
        write_camilla_statefile,
    )
    from jasper.atomic_io import atomic_write_text
    from jasper.bass_extension.profile import evaluate_bass_extension_profile

    topology = load_output_topology_strict(args.topology)
    applied = load_applied_baseline_profile_state(args.applied_baseline_state)
    if not applied:
        print(
            "ERROR: no APPLIED active-speaker baseline profile is saved; there is "
            "nothing to re-emit (commission the speaker first)"
        )
        return 1
    device, source = _baseline_reemit_endpoint(topology, args.endpoint)
    if not device:
        print(
            "ERROR: this topology resolves no active playback endpoint, so there "
            "is no device to re-emit against"
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
        "--ring-flat-config",
        default=str(DEFAULT_RING_FLAT_OUTPUTD_CONFIG),
        help=(
            "ring (shm_ring) full-range outputd config path; selected instead of "
            "--flat-config when the box is ring-armed (finding 5 re-seed)"
        ),
    )
    runtime.add_argument(
        "--coupling",
        default=None,
        help=(
            "persisted fan-in coupling (loopback|shm_ring); when "
            "omitted, read from fanin.env. Ring-armed selects --ring-flat-config."
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
            "re-emit the APPLIED active baseline against a playback endpoint, "
            "publishing it over the live artifact and repointing the statefile. "
            "This is the FIRST step of the active-ring arm (--endpoint ring) and "
            "of its rollback (--endpoint aloop): the reconciler derives its "
            "endpoint marker from the loaded graph, so the graph must move first"
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
        choices=("ring", "aloop"),
        help=(
            "playback endpoint to emit against: 'ring' = the ACTIVE ring "
            "(jts_ring_active_playback, the arm), 'aloop' = the ALSA active lane "
            "(outputd_active_content_playback, the rollback). Omit to keep the "
            "endpoint the box already resolves, which can refresh a graph but "
            "never move a box between lanes"
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
    ramp_status.add_argument("--json", action="store_true")
    ramp_status.set_defaults(func=_cmd_commission_ramp_status)

    ramp_abort = ramp_sub.add_parser(
        "abort", help="re-mute: roll back to the all-muted staged config and reset"
    )
    ramp_abort.add_argument("--json", action="store_true")
    ramp_abort.set_defaults(func=_cmd_commission_ramp_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ActiveSpeakerConfigError, OutputTopologyError, OSError) as e:
        parser.exit(2, f"{parser.prog}: error: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
