# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Write a bounded audio readiness snapshot."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from jasper.audio_resources import audio_validation_artifacts as artifacts
from jasper.runtime.audio_validation import CHIP_AEC_PROFILE, build_chip_aec_readiness_artifact
from jasper.audio_control.audio_validation_probes import logger
from jasper.log_event import log_event
from jasper.logging_setup import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a bounded audio readiness snapshot artifact.",
    )
    parser.add_argument(
        "--profile",
        default=CHIP_AEC_PROFILE,
        choices=(CHIP_AEC_PROFILE,),
        help="Audio profile to snapshot.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the full artifact JSON to stdout.",
    )
    args = parser.parse_args(argv)

    configure_logging(fmt="%(message)s")
    artifact = build_chip_aec_readiness_artifact(profile=args.profile)
    directory = args.directory or artifacts.artifact_directory()
    try:
        path = artifacts.write_artifact(artifact, directory=directory)
        latest_path = artifacts.write_latest_pointer(artifact, directory=directory)
    except OSError as e:
        log_event(
            logger,
            "audio_validation.write_failed",
            profile=artifact.profile,
            status=artifact.status,
            error=str(e),
            level=logging.ERROR,
        )
        return 1
    log_event(
        logger,
        "audio_validation.snapshot",
        profile=artifact.profile,
        status=artifact.status,
        recommendation=artifact.recommendation,
        path=path,
        latest=latest_path,
    )
    if args.stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if artifact.status != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
