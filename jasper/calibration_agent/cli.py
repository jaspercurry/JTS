"""Read-only calibration-agent intake CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import tools


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
    lines = [
        f"# Calibration Agent Intake: {summary.get('session_id')}",
        "",
        "> Read-only deterministic intake. No filters were changed and no LLM was called.",
        "",
        "## Measurement Summary",
        f"- State: `{summary.get('state')}`",
        f"- Target: `{summary.get('target_choice')}`",
        f"- Positions: {summary.get('current_position')} / {summary.get('total_positions')}",
        f"- Result bundle: {'yes' if summary.get('has_result') else 'no'}",
        f"- Verify measurement: {'yes' if summary.get('has_verify') else 'no'}",
        f"- Calibrated mic: {'yes' if summary.get('mic_calibrated') else 'no'}",
        f"- Designed PEQs: {summary.get('peq_count')}",
        "",
        "## Quality",
    ]
    quality_issues = summary.get("quality_issues") or []
    if quality_issues:
        lines.extend(f"- {_fmt_issue(issue)}" for issue in quality_issues)
    else:
        lines.append("- No capture-quality issues recorded.")

    bundle_issues = summary.get("bundle_issues") or []
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
        lines.append("- Bundle is ready for human review and future read-only LLM critique.")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only calibration-agent intake for a correction session bundle."
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
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of markdown",
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
        intake = tools.build_intake(bundle, corpus_dir=args.corpus_dir)
    except tools.AgentToolError as e:
        print(f"jasper-calibration-agent: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(intake, indent=2, sort_keys=True))
    else:
        print(render_markdown(intake), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
