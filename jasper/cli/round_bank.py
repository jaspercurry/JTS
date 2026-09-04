# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank the round a live session just measured, on the box, into the campaign home.

The operator's door onto :func:`jasper.active_speaker.round_bank.bank_round`.
Session storage is retention-capped and a round's evidence outlives the session
that produced it, so this copies one bundle plus the SSOT documents
:mod:`jasper.active_speaker.crossover_v2.round_inputs` names into ``/var/lib/jasper/active_speaker/campaigns/<round>/``
— the same tree ``scripts/bank-crossover-round.sh`` assembles on a laptop, and
the one ``jasper-round-views`` reads.

**Exit codes are part of the contract** (a script is often the caller), from
``jasper/cli/_refusal.py`` like every other tuning tool. **Nothing here prunes**: the campaign home is
operator-pruned and ``jasper-doctor`` discloses its size.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.round_bank import (
    DEFAULT_CAMPAIGN_ROOT,
    RoundBankError,
    bank_round,
)

from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_WRITE_FAILED

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). It copies
#: evidence into the campaign home and changes nothing the speaker plays.
AUTHORITY_TIER = "mutating (copies evidence; changes nothing played)"


def _cmd_bank(args: argparse.Namespace) -> int:
    payload: dict[str, Any]
    try:
        banked = bank_round(
            Path(args.session_dir), campaign_root=Path(args.campaign_root)
        )
    except RoundBankError as exc:
        payload = {"banked": False, "reason": exc.reason, "detail": str(exc)}
        code = EXIT_REFUSED
        print(f"refused ({exc.reason}): {exc}", file=sys.stderr)
    except OSError as exc:
        payload = {"banked": False, "reason": "write_failed", "detail": str(exc)}
        code = EXIT_WRITE_FAILED
        print(f"could not bank {args.session_dir}: {exc}", file=sys.stderr)
    else:
        provenance = banked.provenance
        payload = {
            "banked": True,
            "round_dir": str(banked.path),
            "provenance": provenance,
        }
        code = EXIT_OK
        if not args.json:
            print(str(banked.path))
            print(
                f"  session={provenance['session_id']} "
                f"banked_at_utc={provenance['banked_at_utc']} "
                f"installed_sha={provenance['installed_sha'] or 'unknown'} "
                f"missing={','.join(provenance['missing'] or ['none'])}",
                file=sys.stderr,
            )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-round-bank",
        description=(
            "Bank one live commissioning session into the on-box campaign "
            "home, where it outlives session retention."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - to pull a round to a laptop -- scripts/bank-crossover-round.sh\n"
            "    assembles the same tree over ssh\n"
            "  - to prune the campaign home -- nothing here evicts; remove a\n"
            "    round directory yourself\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-round-bank /var/lib/jasper/active_speaker/sessions/<id>\n"
            "  jasper-round-bank <session-dir> --json\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- banked; the round directory is on stdout\n"
            "  1  EXIT_REFUSED -- not a session bundle, a session that has\n"
            "     not finished, or that round is already banked (never\n"
            "     overwritten)\n"
            "  3  EXIT_WRITE_FAILED -- the copy could not be written -- a\n"
            "     filesystem problem, not a request problem"
        ),
    )
    parser.add_argument(
        "session_dir",
        help="the live session bundle to bank (the directory holding info.json)",
    )
    parser.add_argument(
        "--campaign-root",
        default=str(DEFAULT_CAMPAIGN_ROOT),
        help=f"where banked rounds live (default: {DEFAULT_CAMPAIGN_ROOT})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the result (or refusal) as JSON"
    )
    parser.set_defaults(func=_cmd_bank)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
