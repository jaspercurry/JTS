"""Emit the resolved XVF3800 mic runtime profile.

This is the bridge between the Python single source of truth in
``jasper.mics.xvf3800`` and shell-only policy layers such as
``jasper-aec-reconcile``. It performs no streaming I/O and never writes
chip settings; it only reads local ALSA procfs facts and optionally
publishes the resolved profile as a small JSON state artifact.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.mics import xvf3800


DEFAULT_STATE_PATH = Path("/run/jasper-mic-profile/xvf3800.json")


def _env_lines(payload: dict[str, object]) -> str:
    values = {
        "JASPER_XVF_PRESENT": "1" if payload["present"] else "0",
        "JASPER_XVF_VARIANT": str(payload["variant_id"] or ""),
        "JASPER_XVF_DISPLAY_NAME": str(payload["display_name"] or ""),
        "JASPER_XVF_GEOMETRY": str(payload["geometry"] or ""),
        "JASPER_XVF_ALSA_CARD": str(payload["alsa_card_name"] or ""),
        "JASPER_XVF_CAPTURE_CHANNELS": (
            "" if payload["capture_channels"] is None
            else str(payload["capture_channels"])
        ),
        "JASPER_XVF_CHIP_BEAM_PLAN": (
            str((payload.get("chip_beam_plan") or {}).get("id", ""))
            if isinstance(payload.get("chip_beam_plan"), dict) else ""
        ),
        "JASPER_XVF_CHIP_AEC_SUPPORTED": (
            "1" if payload["chip_aec_supported"] else "0"
        ),
        "JASPER_XVF_RECOMMENDED_PROFILE": str(payload["recommended_profile"] or ""),
        "JASPER_XVF_REASON": str(payload["reason"] or ""),
    }
    return "".join(
        f"{key}={shlex.quote(value)}\n"
        for key, value in values.items()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asound-root",
        default="/proc/asound",
        help="procfs ALSA root to inspect (test hook; default: /proc/asound)",
    )
    parser.add_argument(
        "--bld-msg",
        default="",
        help="optional XVF BLD_MSG value if the caller already read it",
    )
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help=f"JSON state artifact path (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--write-state",
        action="store_true",
        help="atomically write the resolved profile JSON state artifact",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print shell-safe environment assignments instead of JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = xvf3800.detect_runtime_profile(
        asound_root=Path(args.asound_root),
        bld_msg=args.bld_msg or None,
    )
    payload = profile.as_dict()
    text = json.dumps(payload, sort_keys=True) + "\n"
    if args.write_state:
        try:
            atomic_write_text(args.state_path, text, mode=0o644)
        except OSError as e:
            print(
                f"jasper-xvf-profile: warning: state write failed: {e}",
                file=sys.stderr,
            )
    if args.env:
        print(_env_lines(payload), end="")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
