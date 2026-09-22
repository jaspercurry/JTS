# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Review and apply the commissioning profile through the wizard API.

The composer retains the banked tune or starts from the declared crossover.
Apply runs under the DSP writer lock.
See ADR-0312.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from jasper.active_speaker.wizard_client import WizardClient
from jasper.active_speaker.baseline_profile import (
    baseline_profile_state_path,
    load_applied_baseline_profile_state,
)

from ._refusal import EXIT_OK as EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, answered, failed

#: The basic-profile door at its EXTERNAL path. nginx's ``location
#: /sound/speaker/`` proxies to jasper-web on ``127.0.0.1:8784/`` with the
#: prefix stripped (deploy/nginx-jasper.conf), which is why the backend's own
#: ``/active-speaker/...`` routes are reached with this prefix and not without
#: it. The ``/sound/speaker/crossover/`` pages next door are a LONGER nginx
#: prefix and a DIFFERENT daemon (jasper-correction-web, :8770).
REVIEW_PATH = "/sound/speaker/active-speaker/baseline-profile"
SAVE_AND_APPLY_PATH = REVIEW_PATH + "/save-and-apply"

#: Mint the double-submit pair from a page this door's OWN daemon serves. The
#: token is stateless (jasper/web/_common.py compares the request header to the
#: ``Path=/`` cookie, with no per-process secret), so the correction daemon's
#: page happens to validate here too -- but only while both daemons keep one
#: scheme, which nothing enforces.
CSRF_PAGE_PATH = "/sound/speaker/"

#: The door refused and named neither a blocker nor a status of its own.
DOOR_REFUSED = "door_refused"

#: No answer to read. The same slug ``jasper-round`` publishes for the same
#: condition, so one round trip lost is one word whichever tool made it.
ANSWER_LOST = "answer_lost"

#: Said whenever an apply's answer is lost, because "it failed" is a claim this
#: tool cannot make there and the applied record is what can settle it.
LOST_ANSWER_ADVICE = (
    "the apply may or may not have taken effect -- run "
    "`jasper-basic-profile review` and read the applied state before "
    "deciding what the speaker is playing"
)

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "mutating-with-gates"


class _DoorUnreachable(Exception):
    """The door's answer was lost. ``path`` is which round trip lost it."""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def _door(
    wizard: WizardClient, path: str, body: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """One round trip. A ``body`` means POST -- and a POST here means a WRITE.

    The review route is read-only only as a GET. Its POST arm compiles with
    ``write=True``, which mkdirs and rewrites both the baseline CamillaDSP YAML
    and the candidate state JSON (jasper/active_speaker/baseline_profile.py).
    A "review" doing that would replace the file CamillaDSP's own statefile
    still points at, so the next daemon restart would play a graph nobody
    applied -- which is why nothing here POSTs except the apply itself.
    """
    status, payload = (
        wizard.post_json(path, body) if body is not None else wizard.get_json(path)
    )
    if status != 200 or not isinstance(payload, dict):
        raise _DoorUnreachable(
            path,
            f"{f'HTTP {status}' if status else 'no response'}: "
            f"{str(payload).strip()[:200]}",
        )
    return payload


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trims(corrections: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(corrections, Mapping):
        return {}
    out = {
        str(role): {
            "gain_db": _float(entry.get("gain_db")),
            "delay_ms": _float(entry.get("delay_ms")),
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
    profile = _door(wizard, REVIEW_PATH)
    summary = _summary(profile)
    issues = _issues(profile)
    apply_line = "jasper-basic-profile apply"
    _say("basic profile candidate")
    _print_facts(summary)
    for issue in issues:
        _say(_issue_line(issue))
    _say(
        "\nNothing was applied. To apply the saved setup:\n"
        f"  {apply_line}"
    )
    return answered({**summary, "issues": issues, "next": apply_line})


def _door_refusal_reason(payload: Mapping[str, Any]) -> str:
    for issue in _issues(payload):
        if issue["severity"] == "blocker" and issue["code"]:
            return issue["code"]
    return str(payload.get("status") or "") or DOOR_REFUSED


def _proof() -> dict[str, Any] | None:
    """What the speaker's own applied record says, read back after the apply."""
    applied = load_applied_baseline_profile_state()
    if applied is None:
        return None
    summary = _summary(applied)
    return {
        "candidate_fingerprint": summary["candidate_fingerprint"],
        "applied_at": str(applied.get("applied_at") or ""),
        "tuning_owner": summary["tuning_owner"],
        "linearization_roles": summary["linearization_roles"],
        "blend_correction_count": summary["blend_correction_count"],
        "structure_and_trim_only": summary["structure_and_trim_only"],
    }


def _cmd_apply(wizard: WizardClient, args: argparse.Namespace) -> int:
    applied = _door(wizard, SAVE_AND_APPLY_PATH, {})
    if str(applied.get("status") or "") != "applied":
        return failed(EXIT_REFUSED, _door_refusal_reason(applied), applied)

    proof = _proof()
    fingerprint = proof["candidate_fingerprint"] if proof is not None else ""
    state_path = baseline_profile_state_path()
    # The trim this compiled is measured where a measurement backs it and
    # derived from the sensitivity gap where none does; the door says which as
    # an issue, and the runbook tells the operator to read that before deciding
    # this is what they wanted.
    issues = _issues(applied)
    _say("applied.")
    for issue in issues:
        _say(_issue_line(issue))
    _say(f"  {'fingerprint':<22}{fingerprint or '(unknown)'}")
    if proof is None:
        _say(f"  the applied record at {state_path} could not be read")
    else:
        _say(f"  proof, from {state_path}")
        for key in (
            "candidate_fingerprint",
            "applied_at",
            "tuning_owner",
            "structure_and_trim_only",
        ):
            _say(f"    {key:<24}{proof[key]}")
        roles = proof["linearization_roles"]
        _say(
            f"    {'linearization':<24}"
            + ("none" if not roles else f"{len(roles)}: {', '.join(roles)}")
        )
        blend = proof["blend_correction_count"]
        _say(f"    {'blend correction':<24}" + ("none" if not blend else str(blend)))
    return answered(
        {
            "result": "applied",
            "candidate_fingerprint": fingerprint,
            "proof": proof,
            "issues": issues,
        }
    )


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
            "Review and reapply the current candidate, including its tuning layers. "
            "Without an applied candidate, use the saved profile or commissioning "
            "candidate. No evidence is deleted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLE\n"
            "  ssh pi@jts3.local\n"
            "  sudo /opt/jasper/.venv/bin/jasper-basic-profile review\n"
            "  sudo /opt/jasper/.venv/bin/jasper-basic-profile apply\n"
            "\n"
            "  `apply` prints a proof read back from the speaker's own\n"
            "  applied record. It reports the layers actually applied;\n"
            "  linearization and blend correction can remain in the tune.\n"
            "  `sudo` is for that read: the record is group-readable only,\n"
            "  and without it the apply still succeeds but prints no proof.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - you want the banked evidence cleared -- this replaces the\n"
            "    GRAPH and touches no round, candidate or journey state;\n"
            "    starting the measurement journey over is\n"
            "    `POST /crossover/reset` on the correction wizard\n"
            "  - you want a different banked candidate applied -- use\n"
            "    `jasper-round apply <fp>`\n"
            "\n"
            "EXIT CODES\n"
            "  0  the door answered; `review` printed the candidate, or\n"
            "     `apply` put it on the speaker\n"
            "  1  EXIT_REFUSED -- {status, reason, detail} on stdout: the\n"
            "     reason is the door's own blocker code\n"
            "     and the detail carries the payload. Nothing was applied\n"
            "  2  EXIT_UNREADABLE -- reason `answer_lost`: there was no\n"
            "     answer to read (wrong --hostname, the daemon is down, a\n"
            "     dropped connection), and the detail names which round\n"
            "     trip. `review` only reads, so nothing changed there -- but\n"
            "     a lost answer to the apply POST does NOT mean the apply\n"
            "     failed. Run `review` and read the applied state"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser(
        "review",
        help="what the basic candidate carries; a pure read, writes nothing",
    )
    _add_connection_args(review)
    review.set_defaults(func=_cmd_review)

    apply_ = sub.add_parser(
        "apply",
        help="make the saved setup the speaker's live graph",
    )
    _add_connection_args(apply_)
    apply_.set_defaults(func=_cmd_apply)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    """``opener`` is :class:`WizardClient`'s own transport seam, for tests."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    wizard = WizardClient(
        host_header=args.hostname,
        base_url=args.base_url,
        csrf_page_path=CSRF_PAGE_PATH,
        opener=opener,
    )
    try:
        return int(args.func(wizard, args))
    except _DoorUnreachable as exc:
        # UNREADABLE and not a refusal: there was no answer to read. On the
        # apply POST the outcome is genuinely unknown -- the route has no
        # try/except around its own answer, so a connection dropped after the
        # graph was loaded looks exactly like one dropped before it, which is
        # the one case that has to carry the advice.
        detail: dict[str, Any] = {"path": exc.path, "detail": exc.detail}
        if exc.path == SAVE_AND_APPLY_PATH:
            detail["advice"] = LOST_ANSWER_ADVICE
        return failed(EXIT_UNREADABLE, ANSWER_LOST, detail)


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
