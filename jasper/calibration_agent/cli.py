"""Calibration-agent intake, prompt, and response-validation CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jasper.sound.profile import PROFILE_LIBRARY_PATH, PROFILE_PATH

from . import actions, model_client, prompt, response, tools


def _fmt_issue(issue: dict[str, Any]) -> str:
    parts = [str(issue.get("severity") or "info")]
    code = issue.get("code")
    if code:
        parts.append(str(code))
    where = issue.get("artifact_path")
    if where:
        parts.append(str(where))
    return f"[{' / '.join(parts)}] {issue.get('message') or '(no message)'}"


def render_markdown(intake: dict[str, Any]) -> str:
    summary = intake["summary"]
    evidence = intake.get("evidence") or {}
    readiness = evidence.get("agent_readiness") or {}
    acoustic = (evidence.get("acoustic_quality") or {}).get("summary") or {}
    position = evidence.get("position_analysis") or {}
    repeatability = evidence.get("repeatability") or {}
    runtime = (evidence.get("runtime_integrity") or {}).get("summary") or {}
    permissions = (
        (evidence.get("capability_permissions") or {}).get("permissions")
        or {}
    )
    bundle_issues = summary.get("bundle_issues") or []
    quality_issues = summary.get("quality_issues") or []
    feature_flags = position.get("feature_flags") or []
    missing_evidence = list(evidence.get("missing_evidence") or [])
    if not intake.get("schroeder", {}).get("available"):
        missing_evidence.append({
            "code": "schroeder_estimate_missing",
            "severity": "info",
            "message": "room volume / RT60 for Schroeder estimate is unavailable",
        })

    trustworthy: list[str] = []
    if summary.get("mic_calibrated"):
        trustworthy.append("A microphone calibration record is attached.")
    if acoustic.get("snr_level") in {"high", "medium"}:
        trustworthy.append(
            f"SNR evidence is `{acoustic.get('snr_level')}`."
        )
    if runtime.get("level") in {"ok", "pass"}:
        trustworthy.append("Runtime integrity did not report blocking issues.")
    if repeatability.get("level") in {"high", "medium"}:
        trustworthy.append(
            f"Same-position repeatability is `{repeatability.get('level')}`."
        )
    if not bundle_issues:
        trustworthy.append("Bundle contract validation found no issues.")

    suspicious: list[str] = []
    suspicious.extend(str(reason) for reason in readiness.get("reasons") or [])
    suspicious.extend(
        issue.get("message") or issue.get("code") or "quality issue"
        for issue in quality_issues[:5]
    )
    suspicious.extend(
        issue.get("message") or issue.get("code") or "bundle issue"
        for issue in bundle_issues[:5]
    )

    refused: list[str] = []
    for flag in feature_flags[:5]:
        if not isinstance(flag, dict):
            continue
        decision = flag.get("decision") or "avoid_aggressive_correction"
        reason = flag.get("reason") or flag.get("kind")
        refused.append(f"`{decision}` — {reason}")
    for null in (intake.get("peaks_nulls") or {}).get("nulls") or []:
        refused.append(
            "Deep null at "
            f"{float(null['freq_hz']):.1f} Hz was not something to boost blindly."
        )

    lines = [
        f"# Calibration Agent Intake: {summary.get('session_id')}",
        "",
        "> Read-only deterministic intake. No filters were changed and no LLM was called.",
        "",
        "## What Happened",
        f"- State: `{summary.get('state')}`",
        f"- Target: `{summary.get('target_choice')}`",
        f"- Positions: {summary.get('current_position')} / {summary.get('total_positions')}",
        f"- Result bundle: {'yes' if summary.get('has_result') else 'no'}",
        f"- Verify measurement: {'yes' if summary.get('has_verify') else 'no'}",
        f"- Calibrated mic: {'yes' if summary.get('mic_calibrated') else 'no'}",
        f"- Designed PEQs: {summary.get('peq_count')}",
        "",
        "## What Looks Trustworthy",
    ]
    lines.extend(
        f"- {item}" for item in (
            trustworthy or ["No high-trust evidence stood out yet."]
        )
    )
    lines.extend([
        "",
        "## What Looks Suspicious",
    ])
    lines.extend(
        f"- {item}" for item in (
            suspicious or ["No suspicious evidence was surfaced."]
        )
    )
    lines.extend([
        "",
        "## What JTS Refused To Correct",
    ])
    lines.extend(
        f"- {item}" for item in (
            refused or ["No refused/caution correction regions were recorded."]
        )
    )
    lines.extend([
        "",
        "## What I Would Do Next",
        f"- {readiness.get('recommended_action') or 'Review the bundle by hand.'}",
    ])
    if not summary.get("mic_calibrated"):
        lines.append("- Re-run with a calibrated measurement microphone if possible.")
    if acoustic.get("snr_level") in {"low", "unavailable"}:
        lines.append("- Re-run after improving signal-to-noise or room quiet.")
    lines.extend([
        "",
        "## What Evidence Is Missing",
    ])
    lines.extend(
        f"- {_fmt_issue(item)}"
        for item in (missing_evidence or [{
            "severity": "info",
            "message": "Nothing obvious.",
        }])
    )
    lines.extend([
        "",
        "## Quality",
    ])
    if quality_issues:
        lines.extend(f"- {_fmt_issue(issue)}" for issue in quality_issues)
    else:
        lines.append("- No capture-quality issues recorded.")

    lines.extend([
        "",
        "## Evidence Readiness",
        f"- Review state: `{readiness.get('level') or 'unknown'}`",
        f"- Recommended action: {readiness.get('recommended_action') or 'unknown'}",
        (
            "- Acoustic quality: "
            f"`{acoustic.get('level') or 'unknown'}`; "
            f"SNR `{acoustic.get('snr_level') or 'unknown'}`"
        ),
        (
            "- Runtime integrity: "
            f"`{runtime.get('level') or runtime.get('status') or 'unknown'}`"
        ),
        (
            "- Same-position repeatability: "
            f"`{repeatability.get('level') or 'unknown'}`"
        ),
    ])
    if permissions:
        lines.extend(["", "## Capability Permissions"])
        for label, key in (
            ("Safe PEQ", "safe_peq"),
            ("Balanced PEQ", "balanced_peq"),
            ("Assertive PEQ", "assertive_peq"),
            ("Future FIR", "future_fir"),
        ):
            payload = permissions.get(key) or {}
            lines.append(
                f"- {label}: {'allowed' if payload.get('allowed') else 'blocked'}"
            )
            for reason in payload.get("reasons") or []:
                lines.append(f"  - {reason}")
    if acoustic.get("min_estimated_snr_db") is not None:
        lines.append(
            "- Minimum estimated SNR: "
            f"{float(acoustic['min_estimated_snr_db']):.1f} dB"
        )
    for reason in readiness.get("reasons") or []:
        lines.append(f"- Caution: {reason}")

    if bundle_issues:
        lines.extend(["", "## Bundle Contract"])
        lines.extend(f"- {_fmt_issue(issue)}" for issue in bundle_issues)

    peaks_nulls = intake["peaks_nulls"]
    lines.extend(["", "## Bass Residual"])
    if not peaks_nulls.get("available"):
        lines.append(f"- Not available: {peaks_nulls.get('reason')}")
    else:
        peaks = peaks_nulls.get("peaks") or []
        nulls = peaks_nulls.get("nulls") or []
        if peaks:
            lines.append("- Peaks above target:")
            lines.extend(
                f"  - {p['freq_hz']:.1f} Hz: +{p['residual_db']:.1f} dB"
                for p in peaks
            )
        else:
            lines.append("- No local bass peaks above threshold.")
        if nulls:
            lines.append("- Nulls below target:")
            lines.extend(
                f"  - {n['freq_hz']:.1f} Hz: {n['residual_db']:.1f} dB"
                for n in nulls
            )
        else:
            lines.append("- No local bass nulls below threshold.")

    schroeder = intake["schroeder"]
    lines.extend(["", "## Schroeder Estimate"])
    if schroeder.get("available"):
        lines.append(f"- Estimated frequency: {schroeder['freq_hz']:.1f} Hz")
    else:
        lines.append(f"- Not available: {schroeder.get('reason')}")

    corpus_hits = intake.get("corpus_hits") or []
    lines.extend(["", "## Corpus Pointers"])
    if corpus_hits:
        for hit in corpus_hits:
            lines.append(f"- {hit['title']} ({hit['path']})")
            lines.append(f"  {hit['excerpt']}")
    else:
        lines.append("- No calibration-agent corpus found.")

    lines.extend([
        "",
        "## Next Safe Move",
    ])
    if any(i.get("severity") == "fail" for i in quality_issues):
        lines.append("- Re-measure before interpreting or applying correction.")
    elif not summary.get("mic_calibrated"):
        lines.append("- Prefer a calibrated measurement mic before trusting full-range advice.")
    elif bundle_issues:
        lines.append("- Fix bundle/doctor warnings before feeding this to an LLM.")
    else:
        lines.append("- Bundle is ready for human review and future bounded LLM critique.")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic calibration-agent intake, prompt, and action substrate "
            "for a correction session bundle."
        ),
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default="latest",
        help="session id under --sessions-dir, or 'latest' (default)",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=tools.DEFAULT_SESSIONS_DIR,
        help="correction sessions directory",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        help="load this bundle directory directly instead of resolving a session id",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        help="docs/calibration-agent directory to search for guidance snippets",
    )
    parser.add_argument(
        "--repeat-bundle-dir",
        type=Path,
        help=(
            "optional same-position repeat bundle for repeatability "
            "evidence"
        ),
    )
    parser.add_argument(
        "--sound-profile-path",
        type=Path,
        help=(
            "optional sound_profile.json path to summarize in the advisor "
            "context"
        ),
    )
    parser.add_argument(
        "--sound-library-path",
        type=Path,
        help="optional sound_profiles.json path for --audition-sound",
    )
    parser.add_argument(
        "--sound-config-dir",
        type=Path,
        help="optional CamillaDSP config directory for --audition-sound",
    )
    parser.add_argument(
        "--user-message",
        help="optional user-facing prompt text for advisor prompt/model modes",
    )
    parser.add_argument(
        "--advisor-provider",
        default=None,
        help="advisor provider for --call-advisor (currently: openai)",
    )
    parser.add_argument(
        "--advisor-model",
        default=None,
        help=(
            "model id for --call-advisor; otherwise "
            "JASPER_CALIBRATION_ADVISOR_MODEL"
        ),
    )
    parser.add_argument(
        "--advisor-timeout-sec",
        type=float,
        default=None,
        help="HTTP timeout for --call-advisor",
    )
    parser.add_argument(
        "--audition-sound",
        action="store_true",
        help=(
            "wire validated preference-EQ audition actions into the existing "
            "/sound/ audition backend; does not persist profiles"
        ),
    )
    parser.add_argument(
        "--user-confirmed",
        action="store_true",
        help=(
            "mark validated persistent advisor actions as user-confirmed; "
            "only meaningful with --validate-advisor-response or "
            "--run-advisor-actions"
        ),
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of markdown",
    )
    output.add_argument(
        "--advisor-context-json",
        action="store_true",
        help="emit only the redacted LLM-ready advisor context JSON",
    )
    output.add_argument(
        "--advisor-prompt-json",
        action="store_true",
        help="emit a provider-neutral prompt package for a future advisor call",
    )
    output.add_argument(
        "--validate-advisor-response",
        type=Path,
        metavar="PATH",
        help=(
            "validate a proposed advisor response JSON against the current "
            "advisor context"
        ),
    )
    output.add_argument(
        "--run-advisor-actions",
        type=Path,
        metavar="PATH",
        help=(
            "validate a proposed advisor response JSON and run the "
            "action runner; DSP executors are side-effect-free unless "
            "--audition-sound is passed"
        ),
    )
    output.add_argument(
        "--call-advisor",
        action="store_true",
        help=(
            "call the configured advisor model, validate the response, and "
            "run the action runner; requires OPENAI_API_KEY for provider=openai"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bundle = tools.load_measurement_bundle(
            session_id=args.session_id,
            bundle_dir=args.bundle_dir,
            sessions_dir=args.sessions_dir,
        )
        intake = tools.build_intake(
            bundle,
            corpus_dir=args.corpus_dir,
            repeat_bundle_dir=args.repeat_bundle_dir,
            sound_profile_path=args.sound_profile_path,
        )
    except tools.AgentToolError as e:
        print(f"jasper-calibration-agent: {e}", file=sys.stderr)
        return 2

    if args.advisor_context_json:
        print(json.dumps(intake["advisor_context"], indent=2, sort_keys=True))
    elif args.advisor_prompt_json:
        package = prompt.build_advisor_prompt_package(
            intake["advisor_context"],
            user_message=args.user_message,
        )
        print(json.dumps(package, indent=2, sort_keys=True))
    elif args.validate_advisor_response:
        try:
            raw = json.loads(args.validate_advisor_response.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"jasper-calibration-agent: invalid advisor response: {e}", file=sys.stderr)
            return 2
        validation = response.validate_advisor_response(
            raw,
            advisor_context=intake["advisor_context"],
            user_confirmed=args.user_confirmed,
        )
        print(json.dumps(validation, indent=2, sort_keys=True))
        if not validation["accepted"]:
            return 1
    elif args.run_advisor_actions:
        try:
            raw = json.loads(args.run_advisor_actions.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"jasper-calibration-agent: invalid advisor response: {e}", file=sys.stderr)
            return 2
        validation = response.validate_advisor_response(
            raw,
            advisor_context=intake["advisor_context"],
            user_confirmed=args.user_confirmed,
        )
        run = actions.run_validated_action_plan(
            validation,
            audition_executor=_sound_audition_executor(args),
        )
        print(json.dumps(run, indent=2, sort_keys=True))
        if not validation["accepted"] or not run["accepted"]:
            return 1
    elif args.call_advisor:
        package = prompt.build_advisor_prompt_package(
            intake["advisor_context"],
            user_message=args.user_message,
        )
        try:
            model_call = model_client.call_advisor(
                package,
                provider=args.advisor_provider,
                model=args.advisor_model,
                timeout_sec=args.advisor_timeout_sec,
            )
        except model_client.AdvisorModelError as e:
            print(f"jasper-calibration-agent: advisor model call failed: {e}", file=sys.stderr)
            return 2
        validation = response.validate_advisor_response(
            model_call["advisor_response"],
            advisor_context=intake["advisor_context"],
            user_confirmed=args.user_confirmed,
        )
        run = actions.run_validated_action_plan(
            validation,
            audition_executor=_sound_audition_executor(args),
        )
        envelope = {
            "artifact_schema_version": 1,
            "kind": "jts_advisor_model_review",
            "model_call": model_call,
            "validation": validation,
            "action_run": run,
            "side_effects": [
                *model_call.get("side_effects", []),
                *run.get("side_effects", []),
            ],
        }
        print(json.dumps(envelope, indent=2, sort_keys=True))
        if not validation["accepted"] or not run["accepted"]:
            return 1
    elif args.json:
        print(json.dumps(intake, indent=2, sort_keys=True))
    else:
        print(render_markdown(intake), end="")
    return 0


def _sound_audition_executor(args: argparse.Namespace) -> actions.ActionExecutor | None:
    if not args.audition_sound:
        return None
    from .sound_actions import build_sound_audition_executor

    return build_sound_audition_executor(
        profile_path=args.sound_profile_path or PROFILE_PATH,
        library_path=args.sound_library_path or PROFILE_LIBRARY_PATH,
        config_dir=args.sound_config_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
