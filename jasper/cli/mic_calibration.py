# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-mic-calibration`` — register the household's measurement mic.

One household microphone record serves every product (ADR-0255 §3), and this
is the door that establishes it: fetch a vendor calibration by serial, or
store a file the household already has, then remember that mic in
``jasper.audio_measurement.household_mic``. Every measurement resolves its
calibration context from that record, so a box with no record measures
uncalibrated.

``fetch`` is the one verb that reaches the network. Both writing verbs file
their result under ``configured_calibration_root()``, installed root-owned
and group ``jasper`` (``install -d -m 2770 -g jasper``, deploy/install.sh);
the login account is in neither, so they run under ``sudo``.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

# The model registry is the numpy-free leaf's, so `models` and the label
# default cost nothing to import; everything else here reaches `calibration`
# lazily because that module pulls numpy.
from jasper.audio_measurement.mic_identity import (
    DEFAULT_SIGN_CONVENTION,
    SUPPORTED_MODELS,
)
from jasper.audio_measurement.household_mic import (
    household_mic_path,
    read_household_mic,
    resolved_household_mic,
    save_household_mic,
)

from ._refusal import (
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    answered,
    failed,
)

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). This CLI
#: persists the household's microphone identity — it plays no audio, arms no
#: renderer, and touches no DSP config.
AUTHORITY_TIER = "advisory (`fetch`/`upload` write; `models`/`show` do not)"

#: The model/serial pair names no lookup the vendor fetchers can make: an
#: unregistered model key, or a serial with nothing in it.
REFUSE_LOOKUP_INVALID = "mic_calibration_lookup_invalid"
#: The vendor answered, and holds no calibration for that serial.
REFUSE_VENDOR_NOT_FOUND = "mic_calibration_vendor_not_found"
#: The vendor lookup could not be completed at all.
REFUSE_VENDOR_UNREACHABLE = "mic_calibration_vendor_unreachable"
#: No household microphone has been registered yet.
REFUSE_NONE_REGISTERED = "mic_calibration_none_registered"
#: A record exists, and names a calibration that is no longer on disk.
REFUSE_UNRESOLVABLE = "mic_calibration_unresolvable"
#: The named file could not be read, or holds no parsable calibration curve.
REASON_FILE_UNREADABLE = "mic_calibration_file_unreadable"
#: The calibration parsed and could not be filed under the calibration root.
REASON_STORE_UNWRITABLE = "mic_calibration_store_unwritable"

#: A calibration curve is a few thousand text rows; anything past 1 MiB is
#: refused unread, because this runs on a 1 GB Pi.
MAX_UPLOAD_BYTES = 1024 * 1024


def _calibration_root() -> Path:
    from jasper.audio_measurement.calibration import (  # lazy: numpy
        configured_calibration_root,
    )

    return configured_calibration_root()


def _store_detail(exc: OSError) -> str:
    # The calibration root is root-owned, group `jasper`; the login account is
    # in neither, so writing it needs sudo.
    hint = " — run with sudo" if isinstance(exc, PermissionError) else ""
    return f"{_calibration_root()}: {exc}{hint}"


def _label_for(model_key: str) -> str:
    """How an unlabelled upload names its mic: the registry's label when the
    model is one JTS knows, else the generic one."""
    spec = SUPPORTED_MODELS.get(model_key) or {}
    return str(spec.get("label") or "Other calibrated mic")


def _established(record: Any, *, serial: str | None) -> int:
    """Remember ``record`` as the household mic and answer with what it is.

    The record is read back rather than echoed: :func:`save_household_mic` is
    documented fail-soft, so what the household HAS is what the file says,
    not what this asked for.
    """
    save_household_mic(record, serial=serial)
    stored = read_household_mic(path=household_mic_path())
    return answered(
        {
            "calibration": record.public_metadata(),
            "household_mic": stored.to_dict() if stored is not None else None,
            "record_path": str(household_mic_path()),
        },
        f"stored {record.label} calibration {record.calibration_id}",
    )


def _cmd_models(_args: argparse.Namespace) -> int:
    return answered({
        "models": [{"key": key, **spec} for key, spec in SUPPORTED_MODELS.items()],
    })


def _cmd_fetch(args: argparse.Namespace) -> int:
    from jasper.audio_measurement.calibration import (  # lazy: numpy
        CalibrationNotFoundError,
        CalibrationUpstreamError,
        fetch_vendor_calibration,
    )

    # The lookup arguments are judged HERE so the ValueError left below is the
    # vendor file failing to parse (store_calibration's, raised inside the
    # fetch) and not an argument this could have named itself.
    if args.model not in SUPPORTED_MODELS:
        return failed(
            EXIT_REFUSED, REFUSE_LOOKUP_INVALID,
            f"unsupported calibration model: {args.model}",
        )
    if not args.serial.strip():
        return failed(
            EXIT_REFUSED, REFUSE_LOOKUP_INVALID, "serial number is required",
        )
    try:
        record = fetch_vendor_calibration(
            model_key=args.model,
            serial=args.serial,
            orientation=args.orientation,
            root=_calibration_root(),
        )
    except CalibrationNotFoundError as exc:
        return failed(EXIT_REFUSED, REFUSE_VENDOR_NOT_FOUND, str(exc))
    except CalibrationUpstreamError as exc:
        return failed(EXIT_REFUSED, REFUSE_VENDOR_UNREACHABLE, str(exc))
    except ValueError as exc:
        return failed(
            EXIT_UNREADABLE, REASON_FILE_UNREADABLE,
            f"the vendor file holds no calibration curve: {exc}",
        )
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, REASON_STORE_UNWRITABLE, _store_detail(exc))
    return _established(record, serial=args.serial)


def _cmd_upload(args: argparse.Namespace) -> int:
    from jasper.audio_measurement.calibration import store_calibration  # lazy: numpy

    try:
        size = args.path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            return failed(
                EXIT_UNREADABLE, REASON_FILE_UNREADABLE,
                f"{args.path}: {size} bytes is past the {MAX_UPLOAD_BYTES}-byte cap",
            )
        text = args.path.read_text()
    except OSError as exc:
        return failed(EXIT_UNREADABLE, REASON_FILE_UNREADABLE, f"{args.path}: {exc}")
    try:
        record = store_calibration(
            text=text,
            provider="manual_upload",
            model=args.model,
            label=args.label or _label_for(args.model),
            source=f"uploaded:{args.path.name}",
            serial=args.serial,
            orientation=args.orientation,
            sign_convention=args.sign_convention,
            root=_calibration_root(),
        )
    except ValueError as exc:
        return failed(EXIT_UNREADABLE, REASON_FILE_UNREADABLE, f"{args.path}: {exc}")
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, REASON_STORE_UNWRITABLE, _store_detail(exc))
    return _established(record, serial=args.serial)


def _cmd_show(_args: argparse.Namespace) -> int:
    found = resolved_household_mic()
    if found is None:
        path = household_mic_path()
        stored = read_household_mic(path=path)
        if stored is None:
            return failed(
                EXIT_REFUSED, REFUSE_NONE_REGISTERED,
                f"no household microphone registered at {path}",
            )
        return failed(EXIT_REFUSED, REFUSE_UNRESOLVABLE, {
            "calibration_id": stored.calibration_id,
            "model_key": stored.model_key,
            "record_path": str(path),
        })
    household, calibration = found
    return answered({
        "household_mic": household.to_dict(),
        "calibration": calibration.public_metadata(),
        "record_path": str(household_mic_path()),
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-mic-calibration",
        description=(
            "Register the household's measurement microphone: fetch its vendor "
            "calibration by serial or store a file you already have, and "
            "remember that mic so every measurement resolves its calibration "
            "from one record. A box with no record measures uncalibrated."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    models = sub.add_parser(
        "models", help="list the mic models the vendor fetchers know",
    )
    models.set_defaults(func=_cmd_models)

    fetch = sub.add_parser(
        "fetch", help="fetch a vendor calibration by serial and remember the mic",
    )
    fetch.add_argument("--model", required=True, help="a model key from `models`")
    fetch.add_argument("--serial", required=True, help="the serial printed on the mic")
    fetch.add_argument(
        "--orientation", default="unknown",
        help="0deg or 90deg; miniDSP serves one file per orientation",
    )
    fetch.set_defaults(func=_cmd_fetch)

    upload = sub.add_parser(
        "upload", help="store a local calibration file and remember the mic",
    )
    upload.add_argument("path", type=Path, help="the calibration file to store")
    upload.add_argument(
        "--model", default="other",
        help=(
            "a model key from `models`, or `other` for a mic that registry does "
            "not name (default: %(default)s)"
        ),
    )
    upload.add_argument("--serial", default=None, help="the serial printed on the mic")
    upload.add_argument("--label", default=None, help="how this mic is named in reports")
    upload.add_argument("--orientation", default="unknown", help="0deg, 90deg or unknown")
    # A measurement-mic calibration file states the mic's RESPONSE, which is
    # what every model in SUPPORTED_MODELS declares; the correction is its
    # negation. A caller that omits the flag gets that answer, not the
    # opposite one.
    upload.add_argument(
        "--sign-convention", default=DEFAULT_SIGN_CONVENTION,
        choices=("response", "correction"),
        help="what the file states (default: %(default)s)",
    )
    upload.set_defaults(func=_cmd_upload)

    show = sub.add_parser(
        "show", help="print the remembered mic and its resolved calibration",
    )
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
