#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Derive the committed crossover-v2 incident fixture from a banked bundle.

The 2026-08-10 jts3 crossover incident (issue #2291) is banked, raw and
SHA-verified, under ``captures/jts3-incident-20260810-issue2291/`` — 93 MB of
session bundles, WAVs and DSP state that ``captures/`` keeps out of git. This
script reduces that bank to the small NON-AUDIO JSON set under
``tests/fixtures/crossover_v2_incident_20260810/`` that
``tests/test_crossover_v2_incident_replay.py`` replays, so the fixture is
reproducible rather than hand-copied.

Usage (laptop-side, offline — it reads files and nothing else)::

    python3 scripts/derive-crossover-incident-fixture.py            # write
    python3 scripts/derive-crossover-incident-fixture.py --check    # verify
    python3 scripts/derive-crossover-incident-fixture.py --bundle DIR --out DIR

``--check`` re-derives in memory and diffs against the committed files; it is an
operator tool, not a CI gate, because the bundle it needs is gitignored and
lives only on the machine that pulled it. It exits 2 when the bundle is absent
(so "I could not check" never reads as "the check passed"), and 1 on a
mismatch OR on a hand-banked field (:data:`HAND_BANKED_KEYS`) having gone
missing from the committed fixture. That second case is the same principle
read the other way: a guarded value this script cannot regenerate disappearing
must never read as "the check passed" either, and the diff loop structurally
cannot see it — absent from both sides compares equal.

What this script does NOT emit, and why (issue #2291 Phase 0):

* **The per-driver measured responses the fit consumed.** Never retained as
  arrays. Re-deriving them offline from ``measure_program.wav`` plus the
  UMIK-2 calibration is possible, but both inputs are gitignored capture data
  and the analysis grid is far too large to commit (~5e5 bins per driver; the
  same session's VERIFY frame graded 37,080 bins across 1.7 kHz). The replay
  test supplies synthetic branches instead and says so.
* **The post-apply cloud curves.** They are retained, but decimated for display
  (512 bins at 46.875 Hz, and an 89-bin per-position grid), while the banked
  flatness verdict was computed over 10752 bins of the full FFT grid. They
  cannot reproduce their own verdict, so the verdict SCALARS travel in
  ``expected_outcome.json`` and the curves stay in the bank.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = REPO_ROOT / "captures" / "jts3-incident-20260810-issue2291"
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "crossover_v2_incident_20260810"

# The banked identifiers this script refuses to run without. They are the
# incident's identity: a bundle that does not carry them is a different
# session, and silently deriving a fixture from it would put another
# speaker's numbers behind this incident's name.
STAGE1_CAPTURE_ID = "cap_OqlWdywQv9ZlWO7RqkZpSQ"
STAGE2_CAPTURE_ID = "cap_VlE2sNj8v5ego--UqbNDUQ"
CANDIDATE_FINGERPRINT = (
    "3df7a4da7f33f5dfaa55866334cfaf7ebdb32bfa76dd0405f41fcc8a79d0941d"
)
BUILD_SHA = "9cc41b987"

# Derivation identity stamped into every emitted file. Bump the date only when
# the emitted content changes — it dates the DERIVATION, not the edit.
DERIVED_ON = "2026-08-10"
SCRIPT_NAME = "scripts/derive-crossover-incident-fixture.py"

ROLES = ("woofer", "tweeter")


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(sources: dict[str, Path], bundle: Path, note: str) -> dict[str, Any]:
    """The header every emitted file carries.

    Records the source session ids, the candidate fingerprint, the build the
    speaker was running, this script, and the SHA-256 of each source file read
    — so a reader can tie any fixture number back to a specific banked byte
    range without trusting this script's summary of it.
    """
    return {
        "note": note,
        "incident": "2026-08-10 jts3 crossover prescription incident (issue #2291)",
        # The bank's directory NAME, never its path: the bank is gitignored and
        # sits at a different absolute path in every checkout and worktree, so a
        # path here would make the emitted fixture differ per machine and turn
        # ``--check`` into a machine-identity test.
        "bundle_dir_name": bundle.name,
        "stage1_capture_session_id": STAGE1_CAPTURE_ID,
        "stage2_capture_session_id": STAGE2_CAPTURE_ID,
        "candidate_fingerprint": CANDIDATE_FINGERPRINT,
        "speaker_build_sha": BUILD_SHA,
        "derived_by": SCRIPT_NAME,
        "derived_on": DERIVED_ON,
        "source_sha256": {
            name: _sha256(path) for name, path in sorted(sources.items())
        },
    }


def _measure_sidecar(bundle: Path, analysis: dict[str, Any]) -> tuple[Path, dict]:
    """The MEASURE capture sidecar belonging to THIS candidate.

    Six sidecars were retained — one per evaluated Fc candidate, in
    ``fc_selection.candidate_order`` — and they differ only in their
    re-analysis diagnostics. Selection is by exact match on the three
    diagnostics the candidate artifact also carries, and it is an error for
    the match not to be unique: picking the wrong sidecar would attach one
    candidate's capture context to another candidate's trims, which is a
    milder version of the very confusion this fixture exists to pin.
    """
    wanted = (
        round(float(analysis["delay_us"]), 3),
        round(float(analysis["alignment_confidence"]), 4),
        round(float(analysis["predicted_ripple_db"]), 4),
    )
    hits = []
    for path in sorted((bundle / "dsp_state" / "capture_dump_20260810").glob("*_measure_*.json")):
        diag = _read_json(path).get("diagnostic", {})
        got = (
            round(float(diag.get("delay_us", float("nan"))), 3),
            round(float(diag.get("alignment_confidence", float("nan"))), 4),
            round(float(diag.get("predicted_ripple_db", float("nan"))), 4),
        )
        if got == wanted:
            hits.append((path, diag))
    if len(hits) != 1:
        raise SystemExit(
            f"expected exactly one MEASURE sidecar matching {wanted}, found {len(hits)}"
        )
    return hits[0]


def _sweep_band_hz(check: dict[str, Any], role: str) -> list[float]:
    return [float(v) for v in check["role_solves"][role]["band_hz"]]


def derive(bundle: Path) -> dict[str, dict[str, Any]]:
    """Read the banked bundle; return ``{filename: payload}``."""
    stage1 = bundle / "session_OqlWdywQv9"
    artifacts = (
        stage1 / "evidence" / "v1" / "artifacts" / "crossover_v2" / STAGE1_CAPTURE_ID
    )
    paths = {
        "candidate.json": artifacts / "candidate.json",
        "check.json": artifacts / "check.json",
        "info.json": stage1 / "info.json",
        "crossover_v2_state.json": (
            bundle / "dsp_state" / "active_speaker_crossover_v2_state.json"
        ),
        "baseline_profile.json": (
            bundle / "dsp_state" / "active_speaker_baseline_profile.json"
        ),
        "build.txt": bundle / "provenance" / "build.txt",
    }
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise SystemExit("missing banked source file(s):\n  " + "\n  ".join(missing))

    candidate = _read_json(paths["candidate.json"])
    check = _read_json(paths["check.json"])
    info = _read_json(paths["info.json"])
    state = _read_json(paths["crossover_v2_state.json"])
    profile = _read_json(paths["baseline_profile.json"])

    if candidate["fingerprint"] != CANDIDATE_FINGERPRINT:
        raise SystemExit(
            f"candidate fingerprint {candidate['fingerprint']} is not this incident's"
        )
    if state["session_id"] != STAGE2_CAPTURE_ID:
        raise SystemExit(f"flow state names capture {state['session_id']}, not this one")

    analysis = candidate["analysis"]
    sidecar_path, sidecar = _measure_sidecar(bundle, analysis)
    sources = dict(paths)
    sources["measure_sidecar.json"] = sidecar_path

    fc_selection = state["fc_selection"]
    region = candidate["source_preset"]["crossover_regions"][0]

    session_context = {
        "_provenance": _provenance(
            sources,
            bundle,
            "Session-level inputs the stage-1 Fc comparison ran under. "
            "configured_fc_hz is the crossover the SESSION was configured at "
            "and selected_fc_hz is the corner the comparison recommended; the "
            "gap between them is what the replay test characterizes.",
        ),
        "bundle_session_id": info["session_id"],
        "configured_fc_hz": float(fc_selection["configured_hz"]),
        "selected_fc_hz": float(fc_selection["recommended_hz"]),
        "fc_selection": {
            "candidate_order": [float(v) for v in fc_selection["candidate_order"]],
            "limits": fc_selection["limits"],
            "margin_db": fc_selection["margin_db"],
            "scores": fc_selection["scores"],
            "verdict": fc_selection["verdict"],
        },
        "gain_plan_db": check["gain_plan_db"],
        "mic_calibration_id": state["evidence"]["calibration"]["verify"][
            "calibration_id"
        ],
        "mic_tier": candidate["linearization"]["woofer"]["mic_tier"],
        "sweep_band_hz": {role: _sweep_band_hz(check, role) for role in ROLES},
        "tier": state["tier"],
        "capture_context": {
            "alignment_confidence": analysis["alignment_confidence"],
            "epsilon_ppm": sidecar["epsilon_ppm"],
            "gate_floor_source": sidecar["woofer_gate_floor_source"],
            "gate_window_ms": sidecar["woofer_gate_window_ms"],
            "snr_db": {
                "tweeter": sidecar["tweeter_snr_db"],
                "woofer": sidecar["woofer_snr_db"],
            },
            "validity_floor_hz": sidecar["woofer_validity_floor_hz"],
        },
    }

    candidate_fit = {
        "_provenance": _provenance(
            sources,
            bundle,
            "The published measured-crossover candidate, reduced to the fields "
            "the replay needs. Every per-role linearization block is the "
            "serialized LinearizationFit the fit engine produced, so the test "
            "can rebuild the dataclass rather than invent one.",
        ),
        "alignment": candidate["alignment"],
        "analysis": {
            "flatness_improvement_db": analysis["flatness_improvement_db"],
            "polarity": analysis["polarity"],
            "predicted_ripple_db": analysis["predicted_ripple_db"],
            "trim_band_average_db": analysis["trim_band_average_db"],
            "trim_db": analysis["trim_db"],
        },
        "crossover_region": region,
        "drivers": candidate["source_preset"]["drivers"],
        "fingerprint": candidate["fingerprint"],
        "linearization": candidate["linearization"],
        "linearization_outcome": candidate["linearization_outcome"],
        "program_id": candidate["program_id"],
        "role_attenuations_db": candidate["role_attenuations_db"],
        "source_preset": candidate["source_preset"],
    }

    # ``anchor_replay`` IS NOT DERIVED HERE ANY MORE, and it cannot be.
    #
    # This script used to recompute it as ``trim_band_average_db +
    # correction_giveback_db``, normalized. That formula stopped describing
    # production on 2026-08-19: the anchor's give-back is now measured over
    # ``branch_level_bands_hz`` by ``solve_branch_trims``, which needs the
    # per-driver COMPLEX RESPONSES — and this bundle never retained them (see
    # the replay test's own module docstring: they were dropped for size, not
    # by accident). There is no arithmetic over the banked scalars that can
    # reach the new number, so a derivation kept here would report drift on a
    # correct fixture forever.
    #
    # A checker that is known to fail is worse than no checker: it trains a
    # reader to ignore ``--check``. So the field is now HAND-BANKED in
    # ``expected_outcome.json`` carrying its own provenance note, and this
    # script does not derive it or validate its VALUE — only its presence,
    # which ``--check`` requires and ``main`` preserves by carrying the
    # committed block through verbatim, so neither a re-run nor a check can
    # quietly lose a value this script can no longer regenerate.
    #
    # What still guards the number: the replay test asserts production's own
    # anchor equals the banked value, so a drift in ``anchor_trims`` fails
    # there. What is lost is only this script's independent second opinion on
    # it, and that opinion was arithmetic this bundle can no longer support.
    committed = candidate["role_attenuations_db"]
    verify_claims = state["verify"]["claims"]
    flatness = state["cloud"]["cloud_verify"]["pipeline"]["flatness"]

    expected_outcome = {
        "_provenance": _provenance(
            sources,
            bundle,
            "What the incident actually emitted, and what the speaker then "
            "measured. Every number here is banked verbatim. anchor_replay is "
            "NOT derived by this script — see the comment above it — and is "
            "carried through from the committed fixture unchanged.",
        ),
        "applied": {
            "applied_at": profile["applied_at"],
            "config_basename": profile["config"]["basename"],
            "config_sha256": profile["config"]["sha256"],
            "corrections": profile["corrections"],
            "measured_candidate_fingerprint": profile["source"][
                "measured_candidate_fingerprint"
            ],
        },
        "committed_attenuations_db": committed,
        "fingerprint": candidate["fingerprint"],
        "linearization_outcome": candidate["linearization_outcome"],
        "post_apply": {
            "cloud_flatness": {
                "max_band_hz": flatness["max_band_hz"],
                "max_db": flatness["max_db"],
                "max_hz": flatness["max_hz"],
                "passed": flatness["passed"],
                "rms_db": flatness["rms_db"],
                "tolerance_db": flatness["tolerance_db"],
            },
            "verify_claims": {
                name: verify_claims[name] for name in ("absolute", "integration")
            },
        },
    }

    return {
        "session_context.json": session_context,
        "candidate_fit.json": candidate_fit,
        "expected_outcome.json": expected_outcome,
    }


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _without_bundle_name(text: str) -> Any:
    """One rendered fixture, parsed, with the bundle's directory name blanked.

    Lets ``--check`` tell "this is a different session" from "this is the same
    session, read out of a directory with another name" — a real case, since
    the bank is copied by hand between machines. Unparseable text compares as
    itself, so a hand-corrupted file never masquerades as a rename.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    provenance = payload.get("_provenance")
    if isinstance(provenance, dict):
        provenance["bundle_dir_name"] = ""
    return payload


#: The one key this script no longer owns. See the comment at its former
#: derivation site in :func:`derive`.
HAND_BANKED_KEYS = ("anchor_replay",)


def _carry_hand_banked(
    out: Path, derived: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Splice hand-banked keys from the committed fixture into ``derived``.

    Returns what was carried, or ``None`` when there is no committed fixture to
    carry from (a first-ever derivation). Mutates ``derived`` in place. Key
    order is not load-bearing — :func:`_render` sorts — but the merge is built
    sorted anyway so an in-memory payload reads the way the file does.
    """
    name = "expected_outcome.json"
    target = out / name
    if name not in derived or not target.exists():
        return None
    committed = _read_json(target)
    carried = {
        key: committed[key] for key in HAND_BANKED_KEYS if key in committed
    }
    if not carried:
        return None
    merged = {**derived[name], **carried}
    derived[name] = {key: merged[key] for key in sorted(merged)}
    return carried


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check", action="store_true",
        help="re-derive and diff against the committed fixture instead of writing",
    )
    args = parser.parse_args(argv)

    if not args.bundle.is_dir():
        print(
            f"banked bundle not available at {args.bundle} — this derivation reads "
            "the gitignored capture bank, so it can only run on the machine that "
            "pulled it.",
            file=sys.stderr,
        )
        return 2

    derived = derive(args.bundle)

    # Carry the hand-banked ``anchor_replay`` through from the committed
    # fixture. `derive` cannot regenerate it (its comment says why), so without
    # this a plain re-run would WRITE a fixture with the field missing and
    # delete a value nothing can rebuild. Splicing it here rather than in
    # `derive` keeps that function a pure bundle->facts mapping, and makes the
    # field match by construction under ``--check`` instead of being silently
    # skipped by a special case in the diff loop.
    _carry_hand_banked(args.out, derived)
    # Ask the PAYLOAD what it ended up holding, not the carry what it found.
    # The carry returns ``None`` for three different reasons — no committed
    # fixture, a committed fixture with the key deleted, no derived entry to
    # splice into — and they all land in the same place: the payload this run
    # is about to write or compare carries no value for a field nothing can
    # rebuild. Asking per key also keeps a partial carry from reading as a
    # complete one if ``HAND_BANKED_KEYS`` ever grows.
    missing_banked = [
        key
        for key in HAND_BANKED_KEYS
        if key not in derived.get("expected_outcome.json", {})
    ]
    banked_list = ", ".join(repr(key) for key in missing_banked)

    if not args.check:
        if missing_banked:
            # Loud, but not fatal. A fixture written without the field is
            # recoverable by restoring the field, and refusing to write would
            # also block the first-ever derivation, which legitimately has
            # nothing to carry from.
            print(
                f"warning: nothing to carry {banked_list} from, so the fixture "
                f"written to {args.out} will not have it. This script cannot "
                "regenerate it — restore it by hand, with its provenance note, "
                "before committing.",
                file=sys.stderr,
            )
        args.out.mkdir(parents=True, exist_ok=True)
        for name, payload in derived.items():
            (args.out / name).write_text(_render(payload), encoding="utf-8")
            print(f"wrote {args.out / name}")
        return 0

    drifted = False
    # A bundle copied to a differently-named directory re-derives identical
    # content under a different ``bundle_dir_name``. That is still a mismatch —
    # the committed fixture names a bundle this run did not read — but calling
    # it corruption would send the reader hunting for a data difference that is
    # not there, so the two cases get different sentences. Both exit 1.
    rename_only = True
    for name, payload in derived.items():
        target = args.out / name
        want = _render(payload)
        have = target.read_text(encoding="utf-8") if target.exists() else ""
        if have == want:
            continue
        drifted = True
        if _without_bundle_name(have) != _without_bundle_name(want):
            rename_only = False
        print(f"--- {target} (committed)\n+++ {target} (re-derived)", file=sys.stderr)
        sys.stderr.writelines(
            difflib.unified_diff(
                have.splitlines(keepends=True), want.splitlines(keepends=True), n=1,
            )
        )
    if drifted:
        print(
            (
                "fixture content matches, but it records a bundle directory name "
                f"other than {args.bundle.name!r} — re-run without --check to restamp"
            )
            if rename_only
            else "fixture does not match the bundle it claims to come from",
            file=sys.stderr,
        )
    if missing_banked:
        # The one failure a re-run does not repair, so the message must not
        # suggest one: this script gave up deriving the field precisely
        # because the bundle cannot reach it.
        print(
            f"hand-banked {banked_list} missing from "
            f"{args.out / 'expected_outcome.json'} — this script cannot "
            "regenerate it (the bundle never retained the per-driver complex "
            "responses its give-back band needs), so re-deriving will not "
            "bring it back. Restore it by hand from git history, with its "
            "provenance note.",
            file=sys.stderr,
        )
    if drifted or missing_banked:
        return 1
    print(f"fixture matches the banked bundle ({len(derived)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
