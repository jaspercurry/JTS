# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Review, through the wizard API, what the /sound/ page's Save to speaker applies.

The composer retains the banked tune or starts from the declared crossover.
See ADR-0312.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from jasper.active_speaker.wizard_client import WizardClient
from jasper.platform.json_fields import as_float

from ._refusal import EXIT_OK as EXIT_OK, EXIT_UNREADABLE, answered, failed

#: The basic-profile door at its EXTERNAL path. nginx's ``location
#: /sound/speaker/`` proxies to jasper-web on ``127.0.0.1:8784/`` with the
#: prefix stripped (deploy/nginx-jasper.conf), which is why the backend's own
#: ``/active-speaker/...`` routes are reached with this prefix and not without
#: it. The ``/sound/speaker/crossover/`` pages next door are a LONGER nginx
#: prefix and a DIFFERENT daemon (jasper-correction-web, :8770).
REVIEW_PATH = "/sound/speaker/active-speaker/baseline-profile"

#: No answer to read. The same slug ``jasper-round`` publishes for the same
#: condition, so one round trip lost is one word whichever tool made it.
ANSWER_LOST = "answer_lost"

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (`review` reads)"


def _trims(corrections: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(corrections, Mapping):
        return {}
    out = {
        str(role): {
            "gain_db": as_float(entry.get("gain_db")),
            "delay_ms": as_float(entry.get("delay_ms")),
            "inverted": bool(entry.get("inverted")),
        }
        for role, entry in corrections.items()
        if isinstance(entry, Mapping)
    }
    return dict(sorted(out.items()))


def _summary(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The three facts that make a profile basic, plus the trims it carries.

    Read off the payload rather than asserted: a door that ever started
    emitting linearization here should print that, not the word this tool
    expected.
    """
    linearization = profile.get("linearization")
    blend = profile.get("blend_correction")
    roles = sorted(linearization) if isinstance(linearization, Mapping) else []
    blend_count = len(blend) if isinstance(blend, list) else 0
    owner = str(profile.get("tuning_owner") or "")
    return {
        "candidate_fingerprint": str(profile.get("candidate_fingerprint") or ""),
        "status": str(profile.get("status") or ""),
        "tuning_owner": owner,
        "linearization_roles": roles,
        "blend_correction_count": blend_count,
        "structure_and_trim_only": not roles and not blend_count and owner == "manual",
        "trims": _trims(profile.get("corrections")),
    }


def _issues(profile: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = profile.get("issues")
    return [
        {
            "severity": str(issue.get("severity") or ""),
            "code": str(issue.get("code") or ""),
            "message": str(issue.get("message") or ""),
            # Absent unless the door sent one: this record is also the CLI's
            # machine-readable answer, and an empty key would be noise in it.
            **({"detail": str(issue["detail"])} if issue.get("detail") else {}),
        }
        for issue in (raw if isinstance(raw, list) else [])
        if isinstance(issue, Mapping)
    ]


def _issue_line(issue: Mapping[str, str]) -> str:
    """One printed issue. ``detail`` is a code, not prose: it names WHICH
    condition of ``code`` the door hit, so it is printed as it was sent."""
    detail = issue.get("detail") or ""
    return (f"  issue  {issue['severity']}  {issue['code']}: {issue['message']}"
            + (f" [{detail}]" if detail else ""))


def _say(line: str = "") -> None:
    """The human rendering, on stderr: stdout carries the answer."""
    print(line, file=sys.stderr)


def _render_number(value: float | None, suffix: str) -> str:
    return "?" if value is None else f"{value:.2f} {suffix}"


def _print_facts(summary: Mapping[str, Any]) -> None:
    roles = summary["linearization_roles"]
    blend = summary["blend_correction_count"]
    _say(f"  {'fingerprint':<22}{summary['candidate_fingerprint'] or '(none)'}")
    _say(f"  {'status':<22}{summary['status'] or '(none)'}")
    _say(
        f"  {'linearization':<22}"
        + ("none" if not roles else f"{len(roles)}: {', '.join(roles)}")
    )
    _say(f"  {'blend correction':<22}" + ("none" if not blend else str(blend)))
    _say(f"  {'tuning owner':<22}{summary['tuning_owner'] or '(none)'}")
    trims = summary["trims"]
    _say("  trims" if trims else f"  {'trims':<22}(none)")
    for role, trim in trims.items():
        _say(
            f"    {role:<16}{_render_number(trim['gain_db'], 'dB'):>12}"
            f"{_render_number(trim['delay_ms'], 'ms'):>12}"
            f"   {'inverted' if trim['inverted'] else 'normal'}"
        )


def _cmd_review(wizard: WizardClient, args: argparse.Namespace) -> int:
    # One GET: the route's POST arm compiles and applies, so nothing here sends one.
    status, profile = wizard.get_json(REVIEW_PATH)
    if status != 200 or not isinstance(profile, dict):
        lost = f"{f'HTTP {status}' if status else 'no response'}: {str(profile).strip()[:200]}"
        return failed(EXIT_UNREADABLE, ANSWER_LOST, {"path": REVIEW_PATH, "detail": lost})
    summary = _summary(profile)
    issues = _issues(profile)
    _say("basic profile candidate")
    _print_facts(summary)
    for issue in issues:
        _say(_issue_line(issue))
    _say("\nNothing was applied; Save to speaker on the /sound/ page applies this setup.")
    return answered({**summary, "issues": issues})


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hostname",
        default=None,
        help="Host header override (default: derived from --base-url)",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1",
        help="where the wizard is reached (default: %(default)s)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-basic-profile",
        description=(
            "Review what Save to speaker applies: the current candidate with its tuning layers, "
            "or without an applied candidate the saved profile or the declared crossover. "
            "Nothing is applied."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLE\n"
            "  ssh pi@jts3.local\n"
            "  /opt/jasper/.venv/bin/jasper-basic-profile review\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - you want an applied speaker back on its saved tune -- use\n"
            "    `jasper-round reset`\n"
            "  - you want a banked candidate applied -- use\n"
            "    `jasper-round apply <fp>`\n"
            "\n"
            "EXIT CODES\n"
            "  0  the door answered and `review` printed the candidate\n"
            "  2  EXIT_UNREADABLE -- reason `answer_lost`: there was no\n"
            "     answer to read (wrong --hostname, the daemon is down, a\n"
            "     dropped connection), and the detail names the path.\n"
            "     `review` only reads, so nothing changed"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser(
        "review",
        help="what the basic candidate carries; a pure read, writes nothing",
    )
    _add_connection_args(review)
    review.set_defaults(func=_cmd_review)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    """``opener`` is :class:`WizardClient`'s own transport seam, for tests."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    wizard = WizardClient(host_header=args.hostname, base_url=args.base_url, opener=opener)
    return int(args.func(wizard, args))


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
