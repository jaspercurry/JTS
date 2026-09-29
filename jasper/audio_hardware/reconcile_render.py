# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the reconcile pass renders from its verdict: the asound template
(published through ``jasper-render-asound-conf``), the shm-ring conf.d slot
period, the flat cutover graph, and the boot statefile the graph selector
proves for the saved topology.

Each step takes the pass and states its result on the pass's journal line;
``Pass.execute`` decides the order.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.audio_hardware.reconcile_common import _Abort, _ensure_dir, _log_token

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass


def render_asound_if_needed(run: Pass) -> bool:
    source = Path(run.asound_source_template)
    if not source.is_file():
        run.log(
            "asound_skip", source_template=run.asound_source_template, missing=1
        )
        return False
    destination = Path(run.asound_template)
    _ensure_dir(destination.parent, 0o755)
    handle, tmp = tempfile.mkstemp(
        prefix=destination.name + ".", dir=destination.parent
    )
    os.close(handle)
    run.asound_template_temp = tmp
    # Render and validate BEFORE replacing the live template: a card-less
    # recognized DAC makes the shared renderer fail closed, and an ignored
    # failure would clobber the working source with an empty file.
    rendered = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; jasper_asound_render_template "$2" "$3"',
            "jasper-asound-render",
            run.asound_render_lib,
            str(source),
            tmp,
        ],
        check=False,
        env={
            **os.environ,
            "OUTPUT_DAC_CARD": run.output_dac_card,
            "OUTPUT_DAC_ID": run.output_dac_id,
            "OUTPUT_DAC_RECOGNIZED": "1" if run.output_dac_recognized else "0",
        },
    )  # unbounded: bounded only by the unit's own TimeoutStartSec=50s
    if rendered.returncode != 0 or os.path.getsize(tmp) == 0:
        os.unlink(tmp)
        run.log(
            "asound_render_failed",
            stage="source_template",
            source_template=run.asound_source_template,
            output_dac_id=run.output_dac_id,
            output_dac_card=_log_token(run.output_dac_card),
            preserved_existing=1,
        )
        return False
    os.chmod(tmp, 0o644)
    if destination.is_file() and destination.read_bytes() == Path(tmp).read_bytes():
        os.unlink(tmp)
        return False
    try:
        rc = subprocess.run(
            [run.render_asound_conf],
            check=False,
            env={**os.environ, "JASPER_ASOUND_TEMPLATE": tmp},
        ).returncode  # unbounded: same gap as the render_asound_conf call above
    except OSError:
        # An absent or non-executable renderer is the shell's own 127, and
        # it refuses for the same reason a nonzero one does. Uncaught it
        # escaped main() (which handles only _Abort/SystemExit), skipping
        # the mixer-pin restart and leaking this template.
        rc = 127
    if rc != 0:
        os.unlink(tmp)
        # Fails the unit for the same reason as a rejected candidate:
        # nothing has been stopped or restarted yet, while continuing would
        # restart outputd against an asound.conf naming a different DAC
        # than the outputd.env this pass already committed.
        raise _Abort(
            run.rejected_stage_exit(
                "asound_render_failed",
                stage="asound_conf",
                rc=rc,
                output_dac_id=run.output_dac_id,
                output_dac_card=_log_token(run.output_dac_card),
                preserved_existing=1,
            )
        )
    os.replace(tmp, destination)
    run.log(
        "asound_rendered",
        output_dac_id=run.output_dac_id,
        output_dac_card=run.output_dac_card,
        outputd_active_mode=int(run.outputd_active_mode),
        outputd_active_channels=_log_token(run.outputd_active_channels),
    )
    return True


def render_ring_conf_if_needed(run: Pass) -> None:
    """Render the shm-ring conf.d slot period from the ACTIVE DAC's
    DECLARED latency floor. Narrow on purpose: an unrecognized DAC, a DAC
    with no declared floor, and a floor whose period is not fan-in's
    compile-time RING_SLOT_FRAMES all leave the shipped conf.d untouched.

    Triggers NO restart and feeds no restart flag: ALSA reads the conf.d at
    the next PCM open, and arming is owned by the coupling reconciler.
    """
    from jasper.audio_control.ring_assets import ring_conf_wire_report  # lazy: --print-env skips it (ADR-0226)

    if not run.output_dac_recognized:
        run.log("ring_conf", result="skipped", reason="dac_unrecognized")
        return
    try:
        report = ring_conf_wire_report(
            profile_id=run.output_dac_id,
            conf_d=run.ring_conf_d,
            output_topology=run.output_topology_path,
            topology=run.saved_topology(),
        )
    # noqa reason: the conf.d render is best-effort — a failure leaves the
    # shipped wire in place and must not abort a hardware reconcile.
    except Exception as exc:  # noqa: BLE001
        run.log(
            "ring_conf",
            result="failed",
            output_dac_id=run.output_dac_id,
            detail=_log_token(f"{type(exc).__name__}: {exc}"),
        )
        return
    run.log(
        "ring_conf",
        result=report.get("result") or "unknown",
        output_dac_id=run.output_dac_id,
        period_frames=report.get("period_frames") or "none",
        previous_period_frames=report.get("previous_period_frames") or "none",
        sample_format=report.get("sample_format") or "none",
        ring_a_channels=report.get("ring_a_channels") or "none",
        ring_b_channels=report.get("ring_b_channels") or "none",
        ring_active_channels=report.get("ring_active_channels") or "none",
        topology=report.get("topology") or "none",
        reason=report.get("reason") or "none",
        ring_conf=_log_token(report.get("conf") or ""),
    )


def render_flat_cutover_if_needed(run: Pass) -> None:
    """Render the flat cutover startup graph for the saved topology.

    The RUNTIME sibling of install's render, through the same single writer
    so there is no second spelling. It belongs here because the graph is
    width-matched to the saved topology and goes stale whenever that
    changes — and both paths that change it run inside jasper-web's
    sandbox, which has no /etc/camilladsp write path (WS1-deliberate).
    Write-on-change; a failed render leaves the previous bytes.
    """
    # lazy: import cost — the YAML emitters are the pass's second heaviest
    # import and the --print-env path never reaches them (ADR-0226).
    from jasper.output_topology import OutputTopologyError
    from jasper.sound.camilla_yaml import render_flat_cutover_configs

    try:
        result = render_flat_cutover_configs(
            config_dir=run.camilla_conf_dir, topology=run.saved_topology()
        )
    except (OutputTopologyError, OSError, ValueError) as exc:
        run.log(
            "flat_cutover",
            result="failed",
            detail=_log_token(f"{type(exc).__name__}: {exc}"),
        )
        return
    run.log(
        "flat_cutover",
        result="ok",
        changed="yes" if result.changed else "no",
        config_dir=run.camilla_conf_dir,
        topology=run.output_topology_path,
    )


def open_runtime_graph_attempt(run: Pass) -> None:
    """Stamp "this pass is working on THIS topology, unproved" before it acts.

    :func:`converge_runtime_graph` closes the stamp when it writes, so what
    survives a pass names the topology whose boot graph nobody proved. It is
    opened HERE, before the pass mutates anything, rather than inside the
    convergence: EVERY exit from this point on — an i2s apply error, the
    rejected outputd candidate, an OOM kill — is equally a pass that proved
    no graph, and the gate has to see them. (The unreadable-asound abort
    in ``Pass.execute`` is deliberately outside: it precedes every mutation,
    so the box is exactly as the previous pass left it.)
    """
    topology = run.saved_topology()
    if topology is None:
        return
    # lazy: import cost — the stamp writers live beside the topology, and
    # the --print-env path returns before this point (ADR-0226).
    from jasper.output_topology_store import stamp_statefile_convergence  # lazy: --print-env skips topology imports

    stamp_statefile_convergence(run.camilla_statefile, topology, proved=False)


def converge_runtime_graph(run: Pass) -> bool:
    """Seed the proved boot statefile for the saved topology.

    The graph selector is the single owner of topology -> CamillaDSP
    safety. This hardware owner never mutates live CamillaDSP: web
    save/reset owns immediate locked convergence and coupling
    reconciliation owns ordered live route transitions.
    """
    # lazy: the contract behind this is the pass's heaviest import and the
    # --print-env path never reaches it (ADR-0226).
    from jasper.active_speaker.runtime_convergence import converge_boot_statefile

    try:
        result = converge_boot_statefile(
            topology_path=run.output_topology_path,
            topology=run.saved_topology(),
            statefile_path=run.camilla_statefile,
            flat_config_path=os.path.join(
                run.camilla_conf_dir, "outputd-cutover.yml"
            ),
            write_statefile=True,
        )
    # noqa reason: a convergence that cannot decide fails the pass through its
    # own return, which is what blocks CamillaDSP at boot.
    except Exception as exc:  # noqa: BLE001
        run.mark_degraded()
        run.log(
            "runtime_graph",
            result="failed",
            detail=_log_token(f"{type(exc).__name__}: {exc}"),
        )
        return False
    if not result.ok:
        run.log(
            "runtime_graph",
            result="failed",
            detail=_log_token(
                result.error or f"{result.decision.status}:{result.decision.reason}"
            ),
        )
        return False
    run.log(
        "runtime_graph",
        result="ok",
        apply_mode="write-statefile",
        selected=result.decision.selected_config_path,
        detail=_log_token(
            f"{result.decision.status}:"
            f"{'wrote' if result.statefile_written else 'unchanged'}"
        ),
    )
    return True
