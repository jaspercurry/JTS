# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI for the Bass Extension limiter-evidence bench runner.

Bench-only, operator-supervised. Given an operator-authored manifest of stimulus
requests (a JSON file — the operator's authorized inputs, never defaulted), it
authors the campaign manifest, prints the preflight plan, and — outside
``--dry-run`` — runs the frozen campaign and writes the replayable bundle. It
plays real audio at stress levels and temporarily mutates the live CamillaDSP
graph, so it is run by hand at the bench with the Stop control (Ctrl-C) ready.

``--dry-run`` authors + validates the manifest and prints the plan without
opening any device, socket, or CamillaDSP connection — the safe preflight the
operator runs before the supervised session.

This CLI never wires the pure evidence producer into a runtime path, never
persists a profile, and calls no ``apply_bass_extension`` writer.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Sequence

from jasper.bass_extension.bench.context import (
    BUNDLE_KIND,
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
)
from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    ManifestRefusal,
    author_campaign_manifest,
)
from jasper.bass_extension.bench.runner import Stop
from jasper.bass_extension.targets import MARGINS


def _load_inputs(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("manifest inputs file must be a JSON object")
    return raw


def _target_ids(inputs: dict[str, object]) -> tuple[str, ...]:
    requests = inputs.get("requests")
    if not isinstance(requests, dict) or not requests:
        raise SystemExit(
            "manifest inputs must include a non-empty 'requests' object keyed by "
            "target id (deepest target through natural)"
        )
    return tuple(str(target_id) for target_id in requests)


def _print_plan(inputs: dict[str, object], manifest, target_ids: Sequence[str]) -> None:
    print(f"campaign manifest: margin={manifest.margin_policy_name}")
    print(f"  targets ({len(target_ids)}): {', '.join(target_ids)}")
    print(f"  stimulus roles: {', '.join(STIMULUS_ROLES)}")
    print(
        f"  trusted limiter domain: [{LIMITER_DOMAIN_MIN_DBFS}, "
        f"{LIMITER_DOMAIN_MAX_DBFS}] dBFS"
    )
    print(f"  bundle kind: {BUNDLE_KIND}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="path to the operator-authored manifest-inputs JSON file",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path("bass-extension-bench-bundle"),
        help="directory to write the replayable evidence bundle into",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="author + validate the manifest and print the plan; open no device",
    )
    args = parser.parse_args(argv)

    inputs = _load_inputs(args.manifest)
    if inputs.get("margin_policy_name") not in MARGINS:
        raise SystemExit(
            "margin_policy_name must be one of: " + ", ".join(sorted(MARGINS))
        )
    target_ids = _target_ids(inputs)

    try:
        manifest = author_campaign_manifest(inputs, target_ids=target_ids)
    except ManifestRefusal as refusal:
        print("REFUSED — the manifest is missing operator-authorized inputs:", file=sys.stderr)
        for path in refusal.missing_paths:
            print(f"  - {path}", file=sys.stderr)
        return 2

    _print_plan(inputs, manifest, target_ids)

    if args.dry_run:
        print("dry run: no device opened, no graph mutated, no bundle written")
        return 0

    return _run_live(args, manifest, target_ids)


def _run_live(args: argparse.Namespace, manifest, target_ids: Sequence[str]) -> int:
    """Install the Stop control and run the on-device campaign.

    The campaign composition — the fail-closed activation seam
    (:mod:`jasper.bass_extension.bench.activation`), the pure bundle emitter, the
    analysis kernels, and :func:`jasper.bass_extension.bench.runner.run_campaign`
    — is complete and tested. The live run additionally needs the *on-device*
    collaborators the runner injects: the CamillaDSP controller + the
    ``measurement_window`` (trivial constructors), the ramp/``safe_playback``
    floor adapter, and the play/capture/analyze executor. That executor's
    pre/post-limiter sample taps (content-addressed reads at the CamillaDSP
    limiter input/output) are the one piece with no in-tree helper to compose —
    they are wired and validated at the bench, on the Pi. Until that on-device
    executor is bound here, ``--dry-run`` is the operator preflight and this path
    fails closed rather than pretend to measure.
    """

    stop = Stop()

    def _request_stop(*_: object) -> None:
        print("\nStop requested — fading to floor and restoring.", file=sys.stderr)
        stop.stop()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    raise SystemExit(
        "live bench execution requires the on-device play/capture/tap executor "
        "(run on the Pi, at the bench). The manifest validated and the plan is "
        "shown above; re-run with --dry-run for the full preflight. The campaign "
        "orchestration, activation seam, analysis, and bundle emitter are built "
        "and tested — only the pre/post-limiter tap capture is wired on-device."
    )


if __name__ == "__main__":
    raise SystemExit(main())
