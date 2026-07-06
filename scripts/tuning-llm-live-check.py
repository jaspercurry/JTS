#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Budget-capped LIVE validation of the P6 tuning-LLM request path.

This is the ONE paid harness for P6. It exercises the REAL OpenAI request
path — an interpret packet and one proposer round — against the live
endpoint, asserts the schema + bounds hold on the real model output, and
**saves the raw provider responses as fixtures** under ``tests/fixtures/``
so future tests stay real-shape without spending a cent.

CI / normal test runs NEVER touch this — the fixture-driven pytest suite
covers behaviour with the saved responses. Run this by hand, once, when
you want to confirm the live wire shape or refresh the fixtures.

Cost discipline (AGENTS.md "Voice-eval cost discipline"):
  * hard cap of 2 calls (interpret + propose), one round each — no loops,
    no retries;
  * ``--max-output-tokens`` caps each response;
  * prints a cost ESTIMATE and REFUSES to spend without ``--yes-spend``;
  * never logs the API key.

Usage:
    # dry run — prints the plan + estimate, spends nothing:
    python scripts/tuning-llm-live-check.py

    # actually spend (bounded), refresh fixtures:
    OPENAI_API_KEY=sk-... python scripts/tuning-llm-live-check.py \\
        --yes-spend --save-fixtures
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from jasper.calibration_agent import correction_advisor, key_provisioning
from jasper.calibration_agent import model_client, response as advisor_response

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Rough list-rate estimate for a GPT-5.4-class text model (USD / 1M
# tokens). Only for the pre-spend estimate; the real bill is the
# provider's. Padded generously so the printed number is not an
# under-estimate.
_EST_INPUT_USD_PER_MTOK = 2.50
_EST_OUTPUT_USD_PER_MTOK = 10.0
# Typical packet sizes observed with the fixtures.
_EST_INPUT_TOKENS = 1000
_HARD_CALL_CAP = 2  # interpret + one propose round. Never more.


def _demo_session() -> SimpleNamespace:
    """A deterministic synthetic verified session (one 62 Hz mode, cut,
    accepted) — enough for the model to interpret and propose against.
    Mirrors the shape jasper.correction.session.MeasurementSession exposes
    (the advisor reads it duck-typed)."""
    freqs = np.geomspace(20, 350, 60)
    measured = 8.0 * np.exp(-((np.log2(freqs / 62.0)) ** 2) / (2 * 0.25 ** 2))
    target = np.zeros_like(freqs)
    predicted = measured - 7.0 * np.exp(
        -((np.log2(freqs / 62.0)) ** 2) / (2 * 0.3 ** 2)
    )
    curve = lambda m: SimpleNamespace(
        freqs_hz=freqs.tolist(), magnitude_db=m.tolist()
    )
    return SimpleNamespace(
        state=SimpleNamespace(value="verified"),
        target_choice="flat",
        strategy_choice="balanced",
        current_position=3,
        total_positions=3,
        measured_curve=curve(measured),
        target_curve=curve(target),
        predicted_curve=curve(predicted),
        position1_curve=curve(measured),
        peqs=[SimpleNamespace(freq_hz=62.0, q=3.0, gain_db=-7.0)],
        design_report={
            "dominant_residuals": {
                "peaks": [{"freq_hz": 62.0, "residual_db": 8.1}],
                "nulls": [],
            },
            "band_hz": [20.0, 350.0],
            "predicted": {
                "rms_db": 2.4, "max_abs_db": 7.0,
                "filter_count": 1, "total_positive_boost_db": 0.0,
            },
            "crossover_region": {
                "corner_hz": 80.0,
                "no_boost_band_hz": [63.5, 100.8],
                "excluded_boosts": [],
            },
        },
        confidence_report={"findings": []},
        acceptance={
            "verdict": "accept", "overall_rms_delta_db": 2.4,
            "reasons": ["62 Hz within target"], "bands": [],
        },
        verify_before_after={
            "delta": {"rms_db": 2.1, "max_db": 6.5}, "band_hz": [50.0, 350.0],
        },
    )


def _estimate_usd(max_output_tokens: int) -> float:
    per_call = (
        _EST_INPUT_TOKENS / 1_000_000 * _EST_INPUT_USD_PER_MTOK
        + max_output_tokens / 1_000_000 * _EST_OUTPUT_USD_PER_MTOK
    )
    return per_call * _HARD_CALL_CAP


class _CapturingTransport:
    """Wraps model_client's real HTTP POST, recording the raw provider
    response bytes for each call so they can be saved as fixtures. Never
    stores or logs the Authorization header."""

    def __init__(self) -> None:
        self.responses: list[bytes] = []

    def __call__(self, url, headers, body, timeout):
        status, raw = model_client._post_json(url, headers, body, timeout)
        self.responses.append(raw)
        return status, raw


def _save_fixture(raw: bytes, name: str) -> None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(f"  ! {name}: response was not JSON; not saving")
        return
    payload["_comment"] = (
        "Captured from the live OpenAI endpoint by "
        "scripts/tuning-llm-live-check.py. Real provider wire shape for "
        "the P6 tuning path; tests parse this offline (no paid call)."
    )
    out = FIXTURES_DIR / name
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  saved fixture {out}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes-spend", action="store_true",
        help="actually make the paid calls (default: dry-run, no spend)",
    )
    parser.add_argument(
        "--save-fixtures", action="store_true",
        help="overwrite tests/fixtures/tuning_llm_*_response.json with the "
             "captured live responses",
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=400,
        help="per-call output-token cap (default 400)",
    )
    args = parser.parse_args(argv)

    estimate = _estimate_usd(args.max_output_tokens)
    print("P6 tuning-LLM live check")
    print(f"  calls (hard cap):    {_HARD_CALL_CAP} (interpret + 1 propose)")
    print(f"  max output tokens:   {args.max_output_tokens} per call")
    print(f"  estimated cost:      ~${estimate:.3f} (list-rate estimate, not a bill)")

    key = key_provisioning.read_openai_key()
    model = key_provisioning.resolve_tuning_model()
    print(f"  model:               {model}")
    print(f"  key configured:      {'yes' if key else 'NO'}")

    if not args.yes_spend:
        print("\nDRY RUN — no calls made. Re-run with --yes-spend to actually "
              "exercise the live endpoint.")
        return 0
    if not key:
        print("\nERROR: --yes-spend given but no OpenAI key is configured "
              "(set OPENAI_API_KEY or add one at /voice).", file=sys.stderr)
        return 2

    session = _demo_session()
    capture = _CapturingTransport()

    print("\n[1/2] interpret …")
    interp = correction_advisor.interpret(
        session, transport=capture, max_output_tokens=args.max_output_tokens,
    )
    _assert(interp["validation_accepted"], "interpret output failed validation")
    _assert(
        interp["provenance"]["ok"],
        f"interpret cited unverified numbers: {interp['provenance']['unverified']}",
    )
    print(f"      ok — provenance clean, {interp['usage']} tokens")

    print("[2/2] propose …")
    prop = correction_advisor.propose(
        session, transport=capture, max_output_tokens=args.max_output_tokens,
    )
    _assert(prop["validation_accepted"], "propose output failed validation")
    # Every correction proposal must have gone through the sim gate.
    for p in prop["proposals"]:
        if p.get("kind") == "room_correction":
            _assert("simulation" in p, "a correction proposal skipped the sim gate")
    print(f"      ok — {len(prop['proposals'])} proposal(s), {prop['usage']} tokens")

    # Re-validate the raw model outputs against the deterministic contract
    # one more time as a belt-and-suspenders bounds check on live output.
    _validate_raw_responses(capture.responses)

    if args.save_fixtures and len(capture.responses) >= 2:
        print("\nsaving fixtures:")
        _save_fixture(capture.responses[0], "tuning_llm_interpret_response.json")
        _save_fixture(capture.responses[1], "tuning_llm_propose_response.json")

    print("\nLIVE CHECK PASSED — schema + bounds held on real model output.")
    return 0


def _validate_raw_responses(responses: "list[bytes]") -> None:
    for i, raw in enumerate(responses):
        payload = json.loads(raw.decode("utf-8"))
        text = model_client._extract_response_text(payload)
        advisor = json.loads(text)
        validation = advisor_response.validate_advisor_response(
            advisor,
            advisor_context={"advisor_policy": {"allowed_actions": [
                {"id": "explain", "allowed": True, "reasons": []},
                {"id": "recommend_remeasure", "allowed": True, "reasons": []},
                {"id": "propose_correction_peq_adjustment", "allowed": True, "reasons": []},
                {"id": "propose_target_move", "allowed": True, "reasons": []},
                {"id": "propose_preference_eq_audition", "allowed": True, "reasons": []},
            ]}},
        )
        _assert(
            validation["accepted"],
            f"raw response {i} failed the deterministic contract: "
            f"{validation['issues']}",
        )


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"\nLIVE CHECK FAILED: {msg}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
