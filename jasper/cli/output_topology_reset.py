"""Reset a speaker's saved output topology to a clean passive state.

Recovery primitive. A box's saved output topology
(``/var/lib/jasper/output_topology.json``) can drift from physical reality —
most often a leftover active-speaker (roleful / protected-"tweeter") topology
from an old experiment. The L0 fail-closed runtime gate
(``jasper.active_speaker.runtime_contract``) then correctly refuses to run a
flat full-range graph under that roleful topology, which can BLOCK a deploy at
the install-time "outputd Camilla statefile vs active-speaker runtime contract"
check on a box that is physically a plain passive speaker.

This returns the box to a standard passive single-speaker topology derived from
its CURRENTLY-DETECTED hardware, then kicks ``jasper-audio-hardware-reconcile``
so the running CamillaDSP / outputd graph converges to the flat / passive path.
The end state is consistent — passive topology + flat graph — which the L0 gate
accepts (``requires_roleful_graph`` is false, so it does not even inspect the
graph).

It uses only the supported topology generator / persistence functions
(:func:`jasper.output_topology.new_topology_draft` /
:func:`jasper.output_topology.save_output_topology`) — never hand-edited JSON —
and the same broker-mediated reconcile trigger the active-speaker startup path
uses.

Safe-by-construction: it makes the topology *passive*, so even if the reconcile
kick fails the worst residual state is passive-topology + a still-roleful graph,
which is safe (the active graph's crossover keeps protecting drivers) and
self-heals on the next reconcile / boot. It never produces the dangerous
roleful-topology + flat-graph combination.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    load_output_topology_strict,
    new_topology_draft,
    save_output_topology,
    topology_path,
)

logger = logging.getLogger("jasper.output_topology_reset")

RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"


def _topology_summary(topology: OutputTopology) -> dict[str, Any]:
    return {
        "readable": True,
        "name": topology.name,
        "status": topology.status,
        "topology_id": topology.topology_id,
        "device_label": topology.hardware.device_label,
        "physical_output_count": topology.hardware.physical_output_count,
        "speaker_groups": [
            {"id": group.id, "mode": group.mode}
            for group in topology.speaker_groups
        ],
    }


def _read_before(path: str | Path | None) -> dict[str, Any]:
    """Best-effort summary of the topology being replaced.

    A missing file reads (via the supported strict loader) as an unconfigured
    detected draft. A corrupt / unreadable file is captured as
    ``readable=False`` rather than aborting — recovering from exactly that
    drift is the point of this command.
    """

    try:
        return _topology_summary(load_output_topology_strict(path))
    except OutputTopologyError as exc:
        return {"readable": False, "error": str(exc), "speaker_groups": []}


def _trigger_reconcile() -> dict[str, Any]:
    """Kick the audio-hardware reconcile so outputd / CamillaDSP converge.

    Routes through the same restart broker the active-speaker startup path uses;
    only ``start`` of this single unit is permitted to non-root clients, and a
    root operator falls back to a direct ``systemctl``. Best-effort and never
    raises — a failed reconcile leaves a SAFE state (passive topology + the
    previous graph, which self-heals on the next reconcile / boot), reported so
    an operator can re-run it.
    """

    from jasper.control.restart_broker import manage_units

    result = manage_units(
        RECONCILE_UNIT,
        verb="start",
        reason="output_topology_reset",
        # Wait for the oneshot: the operator wants the running graph converged
        # by the time the command returns, not just requested.
        no_block=False,
        timeout=15.0,
    )
    log_event(
        logger,
        "output_topology.reset_reconcile",
        unit=RECONCILE_UNIT,
        ok=bool(result.get("ok")),
        error=result.get("error"),
        level=logging.INFO if result.get("ok") else logging.WARNING,
    )
    return result


def reset_to_detected_passive(
    *,
    path: str | Path | None = None,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Persist a fresh passive topology from detected hardware; kick reconcile.

    Returns a structured before / after report. Pure except for the single
    topology write and the optional reconcile side-effect. ``reconcile=False``
    skips the systemd kick (used by tests and dry runs).
    """

    target = topology_path(path)
    before = _read_before(path)
    after = new_topology_draft()
    save_output_topology(after, path)
    reconcile_result: dict[str, Any] = (
        _trigger_reconcile() if reconcile else {"ok": None, "skipped": True}
    )
    log_event(
        logger,
        "output_topology.reset",
        path=str(target),
        before_status=before.get("status"),
        before_groups=len(before.get("speaker_groups") or []),
        after_status=after.status,
        after_groups=len(after.speaker_groups),
        device_label=after.hardware.device_label,
        reconcile_ok=reconcile_result.get("ok"),
    )
    return {
        "topology_path": str(target),
        "before": before,
        "after": _topology_summary(after),
        "reconcile": reconcile_result,
    }


def _fmt_groups(groups: list[dict[str, Any]]) -> str:
    if not groups:
        return "[]"
    return (
        "["
        + ", ".join(f"({g['id']!r}, {g['mode']!r})" for g in groups)
        + "]"
    )


def _describe(summary: dict[str, Any]) -> str:
    if not summary.get("readable"):
        return f"<unreadable: {summary.get('error')}>"
    return f"{summary['name']!r}, groups={_fmt_groups(summary['speaker_groups'])}"


def _print_summary(result: dict[str, Any], *, dry_run: bool) -> None:
    after = result["after"]
    outputs = after["physical_output_count"]
    print(f"{'WOULD RESET' if dry_run else 'RESET'} output topology: "
          f"{result['topology_path']}")
    print(f"  BEFORE: {_describe(result['before'])}")
    print(f"  AFTER:  {_describe(after)}")
    print(
        f"  detected hardware: {after['device_label']} "
        f"({outputs} output{'' if outputs == 1 else 's'})"
    )
    if dry_run:
        print("  (dry run — nothing written, reconcile not kicked)")
        return
    reconcile = result["reconcile"]
    if reconcile.get("skipped"):
        print(f"  reconcile: skipped (--no-reconcile); start "
              f"{RECONCILE_UNIT} to converge the running graph")
    elif reconcile.get("ok"):
        print(f"  reconcile: kicked {RECONCILE_UNIT} (running graph converged)")
    else:
        print(
            f"  reconcile: FAILED to kick {RECONCILE_UNIT}: "
            f"{reconcile.get('error') or 'unknown error'}"
        )
        print(
            "  The topology is now passive and SAFE; the running graph will "
            "converge on the next reconcile or reboot. Re-run "
            f"`sudo systemctl start {RECONCILE_UNIT}` to converge now."
        )


def _confirm(target: str, before: dict[str, Any], *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "jasper-output-topology-reset: refusing to reset without "
            "confirmation. Re-run with --yes for non-interactive use.",
            file=sys.stderr,
        )
        return False
    if before.get("readable"):
        groups = before.get("speaker_groups") or []
        descr = f"{before.get('name')!r} with {len(groups)} speaker group(s)"
    else:
        descr = "the current (unreadable) topology"
    print(
        f"This REPLACES {descr} at {target} with a standard passive speaker "
        "topology derived from detected hardware, then reconciles the running "
        "audio graph."
    )
    return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-output-topology-reset",
        description=(
            "Reset the saved output topology to a clean passive (standard "
            "speaker) state derived from detected hardware, removing any "
            "active-speaker setup, then reconcile the running audio graph."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt (required for non-interactive use)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the before/after without writing or reconciling",
    )
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help=(
            "write the passive topology but do not kick "
            "jasper-audio-hardware-reconcile (advanced/offline)"
        ),
    )
    parser.add_argument(
        "--path",
        help=(
            "output-topology JSON path "
            "(default: JASPER_OUTPUT_TOPOLOGY_PATH or /var/lib/jasper)"
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = args.path

    if args.dry_run:
        result: dict[str, Any] = {
            "topology_path": str(topology_path(path)),
            "before": _read_before(path),
            "after": _topology_summary(new_topology_draft()),
            "reconcile": {"ok": None, "skipped": True},
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_summary(result, dry_run=True)
        return 0

    before = _read_before(path)
    if not _confirm(str(topology_path(path)), before, assume_yes=args.yes):
        return 1

    try:
        result = reset_to_detected_passive(
            path=path, reconcile=not args.no_reconcile
        )
    except (OutputTopologyError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result, dry_run=False)

    reconcile = result["reconcile"]
    # Reconcile failure is the only non-zero exit on the write path: the
    # topology reset itself succeeded, but the running graph is not yet
    # converged. --no-reconcile is a deliberate skip, not a failure.
    return 0 if (reconcile.get("skipped") or reconcile.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
