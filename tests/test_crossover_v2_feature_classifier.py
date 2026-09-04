# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The classification instrument, against known answers it must get right.

Every fixture here is SYNTHETIC and its answer is known before the instrument
runs: a minimum-phase resonance built from an RBJ peaking filter is a
minimum-phase peak by construction, its inverted twin is a minimum-phase dip,
and an all-pass changes nothing a magnitude measurement can see. That is the
only way to test a classifier — a real capture has no ground truth to check it
against, which is exactly why the instrument carries controls at all.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import wave
from pathlib import Path
from typing import Callable, get_args

import numpy as np
import pytest

from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.audio_measurement.quality_model import TrustLevel

from jasper.active_speaker.crossover_v2 import feature_classifier as fx
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CLASSIFICATION_ARTIFACT,
    round_artifact_dir,
    round_program_dir,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    CLASSIFICATIONS,
    DEFECT_BOOSTABLE,
    DEFECT_CUTTABLE,
    EGD_AMBIGUOUS,
    EGD_MIN_PHASE,
    EGD_NON_MIN_PHASE,
    GATE_STABLE,
    INTERFERENCE_BARRED,
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    ROOM,
    UNRESOLVED,
    defect_boostable_at,
    read_feature_verdicts,
)
from jasper.active_speaker.crossover_v2.gate_sweep import (
    DEFAULT_RUNGS_MS,
    NULL_INSUFFICIENT_VALID_RUNGS,
    ROUTE_CENTRE_SHIFT,
    ROUTE_DEPTH_DELTA,
    ROUTE_SIGMA_GROWTH,
    WINDOW_MOVED,
    WINDOW_STABLE,
    WINDOW_UNRESOLVED,
    frame_descriptor,
)
from jasper.active_speaker.crossover_v2.round_captures import (
    REFUSE_RADIATED_BAND_MISSING,
)
from jasper.active_speaker.crossover_v2.gate_sweep import analysis_grid as sweep_grid
from jasper.cli import round_views as cli

SR = 48000
SESSION_ID = "bundle5essi0n"
RESONANCE_HZ = 3000.0


# --------------------------------------------------------------------------- #
# synthetic speakers, whose answers are known before the instrument runs
# --------------------------------------------------------------------------- #


def _sweep(seconds: float = 1.0, f0: float = 20.0, f1: float = 20000.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    k = math.log(f1 / f0)
    x = np.sin(2 * np.pi * f0 * seconds / k * (np.exp(t * k / seconds) - 1.0))
    fade = int(0.01 * SR)
    x[:fade] *= np.hanning(fade * 2)[:fade]
    x[-fade:] *= np.hanning(fade * 2)[fade:]
    return x * 0.5


def _flat_ir() -> np.ndarray:
    ir = np.zeros(4096)
    ir[64] = 1.0
    return ir


def _resonant_ir(gain_db: float, q: float = 6.0, f0: float = RESONANCE_HZ) -> np.ndarray:
    from scipy.signal import lfilter

    b, a = fx.biquad_peaking(f0, gain_db, q, SR)
    return np.asarray(lfilter(b, a, _flat_ir()), dtype=np.float64)


def _write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


#: Leading silence per phase, so this fixture's phases carry DIFFERENT program
#: bytes the way production's do — a position group's sweep is the verify one
#: minus the courtesy prelude (``crossover_v2.programs``). A capture binds to
#: the program whose bytes it heard (#3504), and phases sharing one waveform
#: could not tell that binding from a phase-label one.
_PROGRAM_LEAD_SAMPLES = {"verify": 240, "cloud_verify": 480}


def _program_for(phase: str) -> np.ndarray:
    lead = np.zeros(_PROGRAM_LEAD_SAMPLES.get(phase, 0))
    return np.concatenate([lead, _sweep()])


def _bundle(
    root: Path,
    ir: np.ndarray,
    *,
    phases: tuple[str, ...] = ("verify", "cloud_verify", "cloud_verify"),
    session_id: str = SESSION_ID,
    seed: int = 11,
    band_hz: tuple[float, float] | None = (150.0, 20000.0),
    bank_shape: bool = False,
    stimulus_sha: Callable[[str, dict[str, str]], str | None] | None = None,
) -> tuple[Path, Path]:
    """A commissioning bundle plus a capture ring, of one synthetic speaker.

    ``bank_shape=True`` banks the program WAVs the way
    ``scripts/bank-crossover-round.sh`` pulls a live Pi session bundle: in a
    SIBLING ``crossover_v2/<capture>/`` directory next to — not inside —
    ``evidence/``, rather than beside the JSON receipts. ``round_dir`` itself
    still has to exist either way, empty or not, for
    :func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_artifact_dir`
    to find it at all.

    Every sidecar banks ``provenance.stimulus.wav_sha256`` over the bytes of
    the program its capture was made against, as the play seam does.
    ``stimulus_sha`` overrides that: it is handed the sidecar's phase and the
    phase→digest map, and a ``None`` return omits the field entirely.
    """
    rng = np.random.default_rng(seed)
    bundle = root / "bundle"
    round_dir = bundle / "evidence/v1/artifacts/crossover_v2/wired-TEST"
    programs_dir = (bundle / "crossover_v2/wired-TEST") if bank_shape else round_dir
    dumps = root / "dumps"
    round_dir.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    for phase in set(phases) | {"verify", "cloud_verify"}:
        path = programs_dir / f"{phase}_program.wav"
        _write_wav(path, _program_for(phase))
        shas[phase] = hashlib.sha256(path.read_bytes()).hexdigest()
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": session_id}))

    for index, phase in enumerate(phases):
        program = _program_for(phase)
        captured = np.convolve(program, ir)[: program.size + 2048]
        captured = captured + rng.normal(0, 3e-4, captured.size)
        captured = captured / np.max(np.abs(captured)) * 0.8
        name = f"{1787000000000000 + index * 50_000_000}_{phase}_mic"
        _write_wav(dumps / "wav" / f"{name}.wav", captured)
        (dumps / "sidecar").mkdir(parents=True, exist_ok=True)
        doc: dict = {
            "phase": phase,
            "jts_session_identity": {"session_id": session_id},
            # The band the DUT radiates, exactly as a real summed sidecar
            # banks it. The window ladder normalises on a reference band
            # intersected with this one and refuses without it (E5, #1969).
            "curves": [
                {"role": "summed", "band_hz": list(band_hz or ())}
            ],
        }
        if band_hz is None:
            doc.pop("curves")
        banked = shas[phase] if stimulus_sha is None else stimulus_sha(phase, shas)
        if banked is not None:
            doc["provenance"] = {"stimulus": {"wav_sha256": banked}}
        (dumps / "sidecar" / f"{name}.json").write_text(json.dumps(doc))
    return bundle, dumps


def _classify(root: Path, ir: np.ndarray, **kwargs) -> dict:
    bundle, dumps = _bundle(root, ir, **kwargs)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    return fx.classify_round(captures)


@pytest.fixture(scope="module")
def peak_artifact(tmp_path_factory) -> dict:
    """One classified round whose speaker has a known minimum-phase PEAK."""
    return _classify(tmp_path_factory.mktemp("peak"), _resonant_ir(+3.0))


@pytest.fixture(scope="module")
def dip_artifact(tmp_path_factory) -> dict:
    """The same speaker with the resonance inverted into a known DIP."""
    return _classify(tmp_path_factory.mktemp("dip"), _resonant_ir(-3.0))


# --------------------------------------------------------------------------- #
# the known answers
# --------------------------------------------------------------------------- #


def test_a_minimum_phase_peak_is_classified_cuttable(peak_artifact):
    """The whole point: a resonance the instrument may aim a cut at."""
    rows = peak_artifact["rows"]
    assert len(rows) == 1, [row["hz"] for row in rows]
    row = rows[0]
    assert abs(math.log2(row["hz"] / RESONANCE_HZ)) < fx.FEATURE_HALF_OCT
    assert row["classification"] == DEFECT_CUTTABLE
    assert row["egd_verdict"] == EGD_MIN_PHASE
    assert row["gate_verdict"] == GATE_STABLE
    assert row["is_dip"] is False
    assert row["depth_db"] > 0.5


def test_a_minimum_phase_dip_is_classified_boostable_and_carries_its_depth(
    dip_artifact,
):
    """The mirror, and the field the boost bar refuses without."""
    rows = dip_artifact["rows"]
    assert len(rows) == 1, [row["hz"] for row in rows]
    row = rows[0]
    assert abs(math.log2(row["hz"] / RESONANCE_HZ)) < fx.FEATURE_HALF_OCT
    assert row["classification"] == DEFECT_BOOSTABLE
    assert row["is_dip"] is True
    assert row["depth_db"] == pytest.approx(abs(row["pooled_db"]))
    assert row["depth_db"] > 0.5


#: How far either side of a resonance this test looks for manufactured
#: shoulders. A literal, and wide enough to contain the ones the buggy first
#: draft actually produced — 2338 Hz and 3908 Hz against a 3000 Hz resonance,
#: which is 0.360 octaves below and 0.382 above. An earlier version of this
#: test bounded the search at `NEIGHBOURHOOD_OCT` (0.333) and both of them
#: escaped through the gap, so it passed against the very bug it names.
_SHOULDER_SEARCH_OCT = 1.0


def test_the_detrend_shoulders_of_a_resonance_are_not_features(peak_artifact):
    """A one-octave baseline manufactures a trough on each flank of a peak.

    The 2026-08-20 first draft of the detector reported both of them as
    minimum-phase DIPS, either side of a resonance that has none — and a dip's
    own shoulders are peaks, which could vouch for a cut at a frequency the
    speaker is flat at. The two-sided prominence read off the PRE-detrend curve
    is what rejects them, so this asserts that within an octave of the
    resonance the instrument found the resonance and nothing else.
    """
    near = [
        row["hz"]
        for row in peak_artifact["rows"]
        if abs(math.log2(row["hz"] / RESONANCE_HZ)) <= _SHOULDER_SEARCH_OCT
    ]
    assert len(near) == 1, near
    assert abs(math.log2(near[0] / RESONANCE_HZ)) < fx.FEATURE_HALF_OCT


#: A reflection arriving here is INSIDE the 7 ms window and OUTSIDE the 3 ms
#: one, which is the whole span the gate ladder exists to walk. Its comb has a
#: peak at exactly 3000 Hz (12 rungs of 1/tau), so the same frequency the
#: minimum-phase fixture puts a driver resonance at is here a room arrival —
#: two speakers that look alike to a magnitude reading and must not classify
#: alike.
_ROOM_ARRIVAL_MS = 4.0
_ROOM_ARRIVAL_GAIN = 0.5


def test_a_reflection_inside_the_window_is_classified_as_the_room(tmp_path):
    """The gate test's own known answer, and the one a cut must never be aimed at.

    A quiet delayed copy is MINIMUM phase (``|g| < 1``), so the excess-GD test
    alone would call this a driver defect. The gate ladder is what separates
    them: shorten the window past the arrival and the feature collapses far
    beyond anything the matched-Q null model loses, which is
    :data:`~jasper.active_speaker.crossover_v2.feature_classifier.GATE_MOVED`
    and therefore ``room``.

    These captures are repeat takes at ONE pose, so their across-pose sigma is
    capture noise and the growth ratio is not read — this is the corrected-
    delta route deciding alone, which is the route a round with no real pose
    cloud still has.
    """
    ir = fx.add_delayed_copy(_flat_ir(), _ROOM_ARRIVAL_GAIN, _ROOM_ARRIVAL_MS, SR)
    bundle, dumps = _bundle(tmp_path, ir)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    # Pinned rather than detected: the comb has dozens of rungs across the
    # band and this test is about ONE of them, not about the detector.
    artifact = fx.classify_round(captures, at=[RESONANCE_HZ])
    row = artifact["rows"][0]
    assert row["egd_verdict"] == EGD_MIN_PHASE
    assert row["gate_verdict"] == fx.GATE_MOVED
    assert row["classification"] == ROOM
    # One entry, keyed by the rung the corrected delta was read at against the
    # shortest resolution-valid one. The delta is the WINDOW's doing with the
    # window's own bias already subtracted, so exceeding the slack is the
    # finding.
    (rung_key, loss), = row["excess_loss_vs_null"].items()
    assert abs(loss) > row["gate_slack"][rung_key]
    sensitivity = row["gate_sensitivity"]["sensitivity"]
    assert f"{sensitivity['longest_valid_rung_ms']:g}" == rung_key
    # The reflection is at 4 ms: the 3 ms rung excludes it and every longer one
    # admits it, so the feature reads ~1.0 dB bigger at the top of the ladder
    # once the window's own bias is subtracted. A literal, not a multiple of
    # the bar it clears -- a case written as a multiple of the constant it
    # tests moves with the constant and pins nothing.
    assert sensitivity["corrected_delta_db"] > 0.9
    # ...and the growth route did NOT decide it, because it was not readable:
    # repeat takes at one pose have no across-pose disagreement to grow.
    assert sensitivity["sigma_growth_readable"] is False
    reasons = row["gate_sensitivity"]["window_verdict_reasons"]
    assert ROUTE_SIGMA_GROWTH not in reasons
    assert ROUTE_DEPTH_DELTA in reasons


#: A custom ladder that deliberately reaches past SEARCH_T_MAX_MS (7 ms):
#: ticket 6.1 requires those rungs to be legal, not clamped or refused, since
#: they re-admit reflections and make convergence vs fan-out readable.
_WIDE_LADDER_MS = (3.0, 5.0, 7.0, 9.0, 11.0)


def test_a_commanded_gate_ladder_reports_per_rung_facts(tmp_path):
    """6.1: every commanded rung, including ones past SEARCH_T_MAX_MS, is a fact.

    A caller who commands a wider ladder gets every rung's own reading back,
    additively, with nothing clamped at the shipped ceiling. The ladder is the
    gate sweep's and the PRIMARY window is this instrument's, and the two are
    independent: a commanded ladder replaces the engine's rungs and leaves the
    primary — the window the phase test, the detector and the trusted band are
    read through — exactly where it was.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    artifact = fx.classify_round(captures, at=[RESONANCE_HZ], gates_ms=_WIDE_LADDER_MS)
    assert artifact["measurement"]["gate_ladder_ms"] == list(_WIDE_LADDER_MS)
    assert artifact["measurement"]["gate_ms_primary"] == fx.DEFAULT_GATE_MS
    row = artifact["rows"][0]
    rungs = row["gate_rungs"]
    assert set(rungs) == {f"{g:g}" for g in _WIDE_LADDER_MS}
    for entry in rungs.values():
        assert isinstance(entry["pooled_db"], float)
        assert isinstance(entry["sigma_db"], float)
        assert isinstance(entry["cycles"], float)
        assert isinstance(entry["resolved"], bool)
    # The 11 ms rung past the primary is read like any other, and the ladder's
    # own longest resolution-valid rung -- not the primary -- is what the
    # corrected delta is keyed by.
    assert "11" in rungs
    assert set(row["excess_loss_vs_null"]) == {"11"}


def test_a_round_banking_no_radiated_band_refuses_the_ladder_not_the_round(
    tmp_path,
):
    """The LADDER is refused by name; every other fact still reports.

    The ladder normalises each capture on a reference band intersected with
    the band its own DUT radiates, and no declared band substitutes for one
    the capture did not bank (E5, #1969). That costs the window verdict, and
    it costs nothing else: the phase class, the decay reads and the per-pose
    facts are all still measured, and the refusal is named in the artifact and
    in every row rather than showing up as a suspiciously stable window.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0), band_hz=None)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    assert all(capture.radiated_band_hz is None for capture in captures)

    artifact = fx.classify_round(captures, at=[RESONANCE_HZ])
    refusal = artifact["measurement"]["gate_ladder_refused"]
    assert refusal["reason"] == REFUSE_RADIATED_BAND_MISSING
    assert refusal["captures"] == [capture.wav.name for capture in captures]
    row = artifact["rows"][0]
    assert row["gate_verdict"] == UNRESOLVED
    assert row["classification"] == UNRESOLVED
    assert row["gate_rungs"] == {}
    assert row["gate_sensitivity"]["ladder_refused"] == refusal
    # The phase test never needed the ladder and still answers.
    assert row["egd_verdict"] == EGD_MIN_PHASE
    assert row["depth_db"] > 0.5


def test_the_fdw_rungs_carry_pooled_db_and_centre_hz_for_both_cycle_counts(
    peak_artifact,
):
    """6.10: every row publishes both FDW variants, shaped like a gate rung."""
    row = peak_artifact["rows"][0]
    rungs = row["fdw_rungs"]
    assert set(rungs) == {f"{c:.0f}" for c in fx.FDW_CYCLES}
    for entry in rungs.values():
        assert isinstance(entry["pooled_db"], float)
        assert isinstance(entry["centre_hz"], float)
    assert peak_artifact["measurement"]["fdw_cycles"] == list(fx.FDW_CYCLES)
    assert peak_artifact["measurement"]["fdw_taper"] == fx.FDW_TAPER


#: A reflection at 2 ms sits INSIDE the 7 ms fixed gate -- the ticket's own
#: known-answer control: a synthetic IR carrying a delayed reflection.
#: 1750 Hz is an EXACT null of this delay's two-path comb -- (3 + 0.5) / 2 ms
#: -- chosen over a higher one so the departure survives the fixed gate's own
#: 1/12-octave smoothing (a comb this dense washes out fast: the SAME fixed
#: gate reads -2.96 dB at 1750 Hz and only -0.04 dB at 7750 Hz, measured).
#: FDW-5's window there is 1.429 ms each side of the peak, short of the 2 ms
#: delay, so a passing FDW-5 read is a near-zero one and a passing fixed-gate
#: read is a large departure.
_FDW_REFLECTION_MS = 2.0
_FDW_REFLECTION_GAIN = 0.5
_FDW_HF_NULL_HZ = 1750.0


def test_fdw_5_excludes_a_reflection_the_fixed_gate_retains(tmp_path):
    """6.10: the asymmetry ADR-0201 funds FDW to surface, pinned as facts.

    Never a verdict here -- the reading rule ("disagreement is reflection
    evidence") is guide content, not code (ADR-0201); this only pins that
    the two windows disagree on this synthetic reflection the way the
    physics says they must.
    """
    ir = fx.add_delayed_copy(
        _flat_ir(), _FDW_REFLECTION_GAIN, _FDW_REFLECTION_MS, SR
    )
    bundle, dumps = _bundle(tmp_path, ir)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    artifact = fx.classify_round(captures, at=[_FDW_HF_NULL_HZ])
    row = artifact["rows"][0]

    fixed_gate_db = row["gate_rungs"][f"{fx.DEFAULT_GATE_MS:.0f}"]["pooled_db"]
    fdw5_db = row["fdw_rungs"]["5"]["pooled_db"]
    assert abs(fixed_gate_db) > 1.0, fixed_gate_db
    assert abs(fdw5_db) < abs(fixed_gate_db) / 4.0, (fdw5_db, fixed_gate_db)


def test_cycles_in_primary_gate_is_frequency_times_the_primary_window(
    peak_artifact,
):
    """6.11a: the grey-zone read research 03 names, per feature."""
    row = peak_artifact["rows"][0]
    assert row["cycles_in_primary_gate"] == pytest.approx(
        row["hz"] * fx.DEFAULT_GATE_MS * 1e-3
    )


def test_the_egd_window_source_names_the_gate_and_lead_it_reads(peak_artifact):
    """6.11b: the receipt. Investigated and judged deliberate and defensible
    -- reflection-freeness (``DEFAULT_GATE_MS`` is already the longest window
    the product ever calls reflection-free) with the C1/C3 controls
    calibrated to this exact window on this round's own IR -- so nothing
    about the window itself changed here, only the disclosure.
    """
    source = peak_artifact["measurement"]["egd_window_source"]
    assert source == {
        "kind": fx.EGD_WINDOW_KIND,
        "gate_ms": fx.DEFAULT_GATE_MS,
        "lead_ms": fx.PHASE_GATE_LEAD_MS,
    }


def test_the_cli_gates_ms_flag_reaches_the_banked_artifact(tmp_path, capsys):
    """6.1: ``--gates-ms`` is not merely parsed -- it reaches classify_round.

    It REPLACES the engine's ladder rather than adding to it, and the primary
    window is not folded in: the two are independent choices now that the
    ladder compares its own shortest and longest valid rungs instead of
    everything against the primary.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    code = cli.main([
        "classify-features", str(bundle), "--dumps", str(dumps),
        "--at", str(RESONANCE_HZ),
        "--gates-ms", "3", "--gates-ms", "9", "--gates-ms", "11",
    ])
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())
    assert banked["measurement"]["gate_ladder_ms"] == [3.0, 9.0, 11.0]
    assert set(banked["rows"][0]["gate_rungs"]) == {"3", "9", "11"}


def test_a_single_rung_ladder_refuses_the_ladder_by_name_and_still_classifies(
    tmp_path, capsys
):
    """One rung compares with nothing, and that costs the window verdict only.

    The engine raises on a ladder it cannot walk. Letting that out would turn
    a mistyped flag into an unreadable round -- so it is folded into the same
    named ladder refusal a round with no radiated band gets, and every other
    fact still reports.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    code = cli.main([
        "classify-features", str(bundle), "--dumps", str(dumps),
        "--at", str(RESONANCE_HZ), "--gates-ms", "7",
    ])
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())

    refusal = banked["measurement"]["gate_ladder_refused"]
    assert refusal["reason"] == fx.GATE_LADDER_NEEDS_TWO_RUNGS
    assert refusal["rungs_ms"] == [7.0]
    for row in banked["rows"]:
        assert row["gate_verdict"] == UNRESOLVED
        assert row["gate_sensitivity"]["ladder_refused"] == refusal
    # ...and the phase test, which never needed the ladder, still answers.
    assert banked["rows"][0]["egd_verdict"] == EGD_MIN_PHASE


def test_the_pose_each_ladder_row_belongs_to_is_banked_once_for_the_round(
    peak_artifact,
):
    """Who a pose IS does not vary with the bin, so it is not banked per bin.

    The engine keys every feature's pose rows on ``pose_key`` alone; the
    triple that key is built from, and the capture behind it, are the ROUND's
    facts and sit beside the frame they were read in.
    """
    banked = peak_artifact["measurement"]["gate_ladder_poses"]
    assert banked, peak_artifact["measurement"]["gate_ladder_refused"]
    assert [pose["capture_wav"] for pose in banked] == (
        peak_artifact["measurement"]["captures"]
    )

    rows = peak_artifact["rows"][0]["gate_sensitivity"]["poses"]
    assert {pose["pose_key"] for pose in rows} == {
        pose["pose_key"] for pose in banked
    }
    for pose in rows:
        assert set(pose) == {
            "pose_key", "value_db_by_rung", "detrended_db_by_rung",
        }


#: Across-pose sigma the engine would have read the growth ratio at. A number
#: this composer never decides on -- it is here so the shape the engine
#: publishes is the shape these cases hand back.
_READABLE_SIGMA_DB = 0.5


def _swept(*, verdict=WINDOW_STABLE, reasons=(), corrected_delta_db=0.0):
    """One feature as :mod:`.gate_sweep` publishes it, window-stable by default.

    The ROUTES are the engine's and are pinned in its own tests; what the
    composer decides is what each of its three words costs a row.
    """
    priced = verdict != WINDOW_UNRESOLVED
    return {
        "bin_hz": RESONANCE_HZ,
        "cycles_by_rung": {"3": 9.0, "20": 60.0},
        "resolution_by_rung": {"3": "ok", "20": "ok"},
        "sigma_db_by_rung": {"3": _READABLE_SIGMA_DB, "20": _READABLE_SIGMA_DB},
        "n_valid_rungs": 2,
        "valid_rungs_ms": [3.0, 20.0],
        "poses": [
            {
                "pose_key": "az_na_el_na_d_na",
                "detrended_db_by_rung": {"3": 1.0, "20": 1.0},
                "value_db_by_rung": {"3": 1.0, "20": 1.0},
            }
        ],
        "window_verdict": verdict,
        "window_verdict_reasons": list(reasons),
        "sensitivity": {
            "shortest_valid_rung_ms": 3.0,
            "longest_valid_rung_ms": 20.0,
            "sigma_growth_ratio": 1.0,
            "sigma_growth_readable": True,
            "corrected_delta_db": corrected_delta_db,
        }
        if priced
        else None,
        "sensitivity_null_reason": (
            None if priced else NULL_INSUFFICIENT_VALID_RUNGS
        ),
    }


def _composed(**overrides):
    """One row through the composer, off a sweep that is stable by default.

    Lets a case move exactly one input and read the verdict, which is how the
    thresholds below are bracketed without either case being written as a
    multiple of the constant it is testing.
    """
    egd = {
        "pooled_excursion_us": overrides.pop("excursion_us", 0.0),
        "sd_us": 0.0,
        "nbhd_sd_us": overrides.pop("nbhd_sd_us", 100.0),
        "p2p_us": 1.0,
        "lead_sensitivity_us": 0.0,
        "clean": True,
    }
    refusal = overrides.pop("ladder_refusal", None)
    sweep = None if refusal else _swept(**overrides.pop("sweep_kwargs", {}))
    gate = fx._gate_call(sweep, refusal)
    row = fx._compose(
        RESONANCE_HZ,
        egd,
        gate,
        {"nmp_delta_us": overrides.pop("nmp_scale_us", 500.0)},
        pooled_db=overrides.pop("pooled_db", 1.0),
        measured_q=6.0,
        controls_ok=True,
        timing_available=True,
    )
    # The two blocks classify_round hangs off the row beside the composed
    # verdict, so a case can read the working the verdict was made on.
    row["gate_rungs"] = gate["gate_rungs"]
    row["gate_sensitivity"] = gate["gate_sensitivity"]
    return row


@pytest.mark.parametrize(
    ("excursion_us", "expected_egd", "expected_class"),
    # 400 / 175 / 50 microseconds against a 500 us non-minimum-phase control
    # scale are fractions of 0.80 / 0.35 / 0.10, which BRACKET both shipped
    # thresholds (0.50 and 0.25) from every side. Literals on purpose: a case
    # written as a multiple of the constant it tests moves with the constant
    # and pins nothing — the same defect this file already had once.
    [
        (400.0, EGD_NON_MIN_PHASE, INTERFERENCE_BARRED),
        (175.0, EGD_AMBIGUOUS, UNRESOLVED),
        (50.0, EGD_MIN_PHASE, DEFECT_CUTTABLE),
    ],
)
def test_the_excess_gd_fraction_decides_the_phase_class(
    excursion_us, expected_egd, expected_class
):
    """`interference-barred` is the instrument's headline safety verdict.

    It is the answer that says a filter is STRUCTURALLY the wrong tool — a cut
    aimed at a cancellation lowers the direct sound and its delayed copy
    together — so the branch that produces it needs its own known input, not
    coverage borrowed from a fixture that never reaches it.
    """
    composed = _composed(excursion_us=excursion_us)
    assert composed["egd_verdict"] == expected_egd
    assert composed["classification"] == expected_class


def test_a_barred_feature_stays_barred_when_the_gate_also_moved():
    """Precedence: a cancellation is not reclassified as the room.

    Both are refusals, so the ORDER looks harmless — but the two send a reader
    somewhere different, and `interference-barred` is the one that says no
    filter of either sign belongs here.
    """
    composed = _composed(
        excursion_us=400.0,
        sweep_kwargs={"verdict": WINDOW_MOVED, "reasons": [ROUTE_DEPTH_DELTA]},
    )
    assert composed["gate_verdict"] == fx.GATE_MOVED
    assert composed["classification"] == INTERFERENCE_BARRED


@pytest.mark.parametrize(
    ("sweep_kwargs", "expected_gate", "expected_class"),
    # Every route the engine can fire, and its two other words. WHICH route
    # fired must not change what the row costs -- a centre that walked is the
    # room exactly as much as a depth that moved.
    [
        pytest.param(
            {"verdict": WINDOW_MOVED, "reasons": [ROUTE_SIGMA_GROWTH]},
            fx.GATE_MOVED, ROOM, id="sigma-grew",
        ),
        pytest.param(
            {"verdict": WINDOW_MOVED, "reasons": [ROUTE_DEPTH_DELTA]},
            fx.GATE_MOVED, ROOM, id="depth-moved",
        ),
        pytest.param(
            {"verdict": WINDOW_MOVED, "reasons": [ROUTE_CENTRE_SHIFT]},
            fx.GATE_MOVED, ROOM, id="centre-walked",
        ),
        pytest.param({}, GATE_STABLE, DEFECT_CUTTABLE, id="stable"),
        pytest.param(
            {"verdict": WINDOW_UNRESOLVED}, UNRESOLVED, UNRESOLVED, id="unpriced",
        ),
    ],
)
def test_the_engines_window_verdict_is_what_the_row_classifies_on(
    sweep_kwargs, expected_gate, expected_class
):
    """A mapping, not a second rule.

    :mod:`.gate_sweep` decides what counts as the window having moved a
    feature and its own tests bracket every threshold; what is pinned here is
    that each of its three words reaches the register intact -- and in
    particular that ``unresolved`` costs the phase class rather than being
    read as the ``STABLE`` half of a ``defect-*`` verdict.
    """
    composed = _composed(sweep_kwargs=sweep_kwargs)
    assert composed["gate_verdict"] == expected_gate
    assert composed["classification"] == expected_class


def test_a_ladder_that_did_not_run_never_vouches_for_a_filter():
    """An unrun window test is ``ambiguous``, and never ``STABLE``.

    ``STABLE`` is a finding -- the feature survived the ladder -- and it is
    half of what a ``defect-*`` verdict vouches a filter with. A round whose
    captures bank no radiated band, or that has one pose, gets no ladder at
    all, and reading that silence as a pass would vouch for a filter aimed at
    a feature nothing checked for the room.
    """
    composed = _composed(ladder_refusal={"reason": REFUSE_RADIATED_BAND_MISSING})
    assert composed["gate_verdict"] == UNRESOLVED
    assert composed["classification"] == UNRESOLVED
    assert composed["egd_verdict"] == EGD_MIN_PHASE
    assert (
        composed["gate_sensitivity"]["ladder_refused"]["reason"]
        == REFUSE_RADIATED_BAND_MISSING
    )


def test_a_flat_speaker_refuses_by_name(tmp_path):
    """No features is a finding with a name, never an empty artifact."""
    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        _classify(tmp_path, _flat_ir())
    assert caught.value.reason == fx.NO_FEATURES_DETECTED


# --------------------------------------------------------------------------- #
# the control gate — the instrument is checked before it is believed
# --------------------------------------------------------------------------- #


def test_controls_separate_a_cancellation_from_its_minimum_phase_twin(peak_artifact):
    """C4 against C4b: same comb geometry, opposite phase class.

    If these did not separate, every verdict above would be a coin toss, and
    the fraction each feature is scored against would have no scale.
    """
    verdict = peak_artifact["controls"]["verdict"]
    assert verdict["passes"] is True
    assert verdict["C4_min_separation_us"] > verdict["C4_required_separation_us"]
    for entry in peak_artifact["controls"]["C4_pair"]["at"].values():
        assert abs(entry["nmp_delta_us"]) > abs(entry["mp_delta_us"])


def test_a_minimum_phase_magnitude_change_reads_flat(peak_artifact):
    """C1 and C3 are both NEGATIVE controls, and C3 deliberately so.

    ``h(t) - 0.5*h(t-0.4ms)`` is the research spec's own "interference" case,
    and ``|g| < 1`` puts every zero inside the unit circle — it is MINIMUM
    phase. Flagging it would be a false positive, not a pass.
    """
    controls = peak_artifact["controls"]
    assert max(
        abs(v) for v in controls["C1_min_phase_peaking"]["delta_us"].values()
    ) < fx.CONTROL_MAX_FALSE_POSITIVE_US
    assert max(
        abs(v) for v in controls["C3_min_phase_echo"]["delta_us"].values()
    ) < fx.CONTROL_MAX_ECHO_FALSE_POSITIVE_US


def test_an_allpass_recovers_its_own_group_delay(peak_artifact):
    """C2: pure excess phase, so anything read here is phase and nothing else."""
    lo, hi = fx.CONTROL_ALLPASS_RATIO_BAND
    for entry in peak_artifact["controls"]["C2_allpass"]["at"].values():
        assert lo <= entry["ratio"] <= hi


def _controls(ir: np.ndarray, features: list[float]) -> dict:
    """The control suite on a synthetic host, at the instrument's own defaults.

    Straight at the suite rather than through a bundle: the subject here is its
    own arithmetic, and the frequencies it is read at have to be commanded
    rather than left to whatever the detector finds.
    """
    trusted = (f_trusted_floor_hz(fx.DEFAULT_GATE_MS * 1e-3), fx.TRUSTED_CEILING_HZ)
    return fx._run_controls(
        ir, SR, features, gate_ms=fx.DEFAULT_GATE_MS, trusted_band_hz=trusted
    )


def _band_edge_hz() -> float:
    """The lowest frequency this instrument will admit a feature at."""
    trusted = (f_trusted_floor_hz(fx.DEFAULT_GATE_MS * 1e-3), fx.TRUSTED_CEILING_HZ)
    return fx.classifiable_band_hz(trusted)[0]


def test_the_c3_control_reads_flat_at_the_bottom_of_the_classifiable_band():
    """C3's known answer is flat at EVERY frequency a feature may be admitted at.

    Issue #3493: on a bare synthetic host, with C1/C2/C4 all clean, C3 read
    10.5 us against its own 10.0 us bar at the lowest frequency
    :func:`classifiable_band_hz` allows — so the suite was failing itself, and
    a rig that happened to detect a feature near the band edge could never be
    certified. The bias belongs to the injection, not to any capture.
    """
    verdict = _controls(_resonant_ir(+3.0), [_band_edge_hz(), RESONANCE_HZ])["verdict"]
    assert verdict["C3_max_false_positive_us"] < verdict["C3_limit_us"]
    assert verdict["failed"] == []
    assert verdict["passes"] is True


def test_a_genuinely_non_minimum_phase_host_still_fails_c3():
    """Removing the injection's own artifact is not a rubber stamp.

    ``CONTROL_COMB_NMP_GAIN`` is the module's own ``|g| > 1``: a host the
    chain genuinely cannot read flat. #3493 bars clearing this control by
    widening it, and it equally bars clearing it by over-subtracting.
    """
    host = fx.add_delayed_copy(_flat_ir(), fx.CONTROL_COMB_NMP_GAIN, 0.9, SR)
    verdict = _controls(host, [1800.0, RESONANCE_HZ])["verdict"]
    assert verdict["passes"] is False
    assert "C3_min_phase_echo" in verdict["failed"]
    assert verdict["C3_max_false_positive_us"] > 10 * verdict["C3_limit_us"]


def test_the_c3_verdict_discloses_the_injection_bias_it_removed():
    """#3480: a refusal has to say what moved, and only ``verdict`` rides one.

    The bar itself is untouched — the artifact was removed, not tolerated —
    and the amount removed at the band edge is larger than the whole bar,
    which is the finding rather than a detail.
    """
    controls = _controls(_resonant_ir(+3.0), [_band_edge_hz(), RESONANCE_HZ])
    verdict = controls["verdict"]
    assert verdict["C3_limit_us"] == fx.CONTROL_MAX_ECHO_FALSE_POSITIVE_US == 10.0
    assert verdict["C3_injection_bias_removed_us"] > verdict["C3_limit_us"]

    per_feature = controls["C3_min_phase_echo"]["injection_bias_removed_us"]
    assert set(per_feature) == {f"{_band_edge_hz():.0f}", f"{RESONANCE_HZ:.0f}"}
    # ~1/f^2: negligible where the round's features usually sit, decisive at
    # the edge. Both are properties of the injection alone, not of this host.
    assert abs(per_feature[f"{RESONANCE_HZ:.0f}"]) < 1.0
    assert abs(per_feature[f"{_band_edge_hz():.0f}"]) > 10.0


def test_the_bias_removal_is_defined_only_where_the_injection_is_minimum_phase():
    """``|g| >= 1`` is signal, not artifact, and subtracting it would blind C3.

    The one way this correction could become the rubber stamp #3493 forbids,
    closed at the seam rather than left to a comment.
    """
    trusted = (f_trusted_floor_hz(fx.DEFAULT_GATE_MS * 1e-3), fx.TRUSTED_CEILING_HZ)
    with pytest.raises(ValueError):
        fx.injection_excess_gd(
            fx.CONTROL_COMB_NMP_GAIN, fx.CONTROL_ECHO_MS, SR, trusted_band_hz=trusted
        )


def test_failing_controls_withhold_the_phase_class_and_nothing_else(
    tmp_path, monkeypatch
):
    """The gate is live, and it costs the PHASE test only.

    All four controls calibrate the excess-group-delay scale; none of them
    touches the magnitude ladder, whose verdict has a null model of its own. So
    a failed suite withholds ``egd_verdict`` (and with it every ``defect-*``
    verdict a filter could be vouched by) and publishes the window evidence it
    never invalidated. Mutating the bar rather than corrupting a capture is
    deliberate — it proves the withholding is driven by the CONTROL VERDICT and
    not by some other property of a degraded signal.
    """
    monkeypatch.setattr(fx, "CONTROL_MAX_FALSE_POSITIVE_US", 0.0)
    artifact = _classify(tmp_path, _resonant_ir(+3.0))

    verdict = artifact["controls"]["verdict"]
    assert verdict["passes"] is False
    # WHICH control missed, and against which bar (#3480's addendum: the same
    # suite passed on one verify round of this rig and failed on another, with
    # nothing in the disclosure saying which of the four terms moved).
    assert verdict["failed"] == ["C1_min_phase_peaking"]
    assert verdict["C1_limit_us"] == 0.0
    assert verdict["C1_max_false_positive_us"] > verdict["C1_limit_us"]
    assert artifact["controls_ok"] is False
    assert artifact["controls_disclosure"] == fx.CONTROLS_FAILED_DISCLOSURE

    for row in artifact["rows"]:
        assert row["controls_ok"] is False
        assert row["egd_verdict"] == EGD_AMBIGUOUS
        # The raw reading is kept, so the withholding is auditable rather than
        # a number that was never taken.
        assert row["egd_verdict_raw"] == EGD_MIN_PHASE
        # The gate verdict is REAL — it is the sweep's, and no control of the
        # phase chain speaks to it.
        assert row["gate_verdict"] in {GATE_STABLE, fx.GATE_MOVED}
        # ...and a defect verdict, the one a filter is vouched by, is
        # structurally out of reach: it requires a MIN-PHASE egd verdict.
        assert row["classification"] not in {DEFECT_CUTTABLE, DEFECT_BOOSTABLE}


# --------------------------------------------------------------------------- #
# program shape
# --------------------------------------------------------------------------- #


def test_a_lateral_round_is_refused_by_name(tmp_path):
    """A per-driver capture carries no summed response to classify."""
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    _write_wav(round_dir / "lateral_program.wav", _sweep())
    (dumps / "sidecar" / "1787000000999999_lateral_mic.json").write_text(
        json.dumps(
            {"phase": "lateral", "jts_session_identity": {"session_id": SESSION_ID}}
        )
    )
    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    assert caught.value.reason == fx.LATERAL_CAPTURE_SHAPE
    assert caught.value.detail["lateral_captures"] == 1


@pytest.mark.parametrize(
    ("rewrite", "expected_reason", "expected_capture_reason"),
    [
        pytest.param(
            {"phase": "measure"},
            fx.ROUND_SHAPE_INADMISSIBLE,
            fx.CAPTURE_PHASE_NOT_ADMISSIBLE,
            id="round_shape",
        ),
        pytest.param(
            {"jts_session_identity": {"session_id": "someone-else"}},
            fx.NO_ADMISSIBLE_CAPTURES,
            fx.CAPTURE_OTHER_SESSION,
            id="no_capture_for_this_round",
        ),
    ],
)
def test_the_two_ways_to_reach_no_capture_refuse_under_different_names(
    tmp_path, rewrite, expected_reason, expected_capture_reason
):
    """#3480: one slug covered two situations with two different remedies.

    A ring holding this round's captures in a shape the instrument cannot read
    (``measure`` is per-driver, not the summed post-apply response) is
    plannable-around — point at a verify-shaped round. A ring holding no
    capture of this round at all is about the ring or the bundle it was scoped
    to, and no round shape would satisfy it. The driver on the campaign's
    second night burned its question on the first while reading the second's
    name, so the two must not share one.
    """
    bundle, dumps = _bundle(
        tmp_path, _resonant_ir(+3.0), phases=("verify", "cloud_verify")
    )
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    for sidecar in (dumps / "sidecar").glob("*.json"):
        doc = json.loads(sidecar.read_text())
        doc.update(rewrite)
        sidecar.write_text(json.dumps(doc))

    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)

    assert caught.value.reason == expected_reason
    assert caught.value.reason in fx.CLASSIFICATION_REFUSAL_REASONS
    # The per-capture table: every sidecar the ring listed, and why each one
    # is not classifiable. A refusal that names a count contradicts the ring
    # listing the operator was just handed; this names the captures.
    census = caught.value.detail["captures"]
    assert len(census) == 2
    assert {row["reason"] for row in census} == {expected_capture_reason}
    assert {row["reason"] for row in census} <= fx.CAPTURE_ADMISSIBILITY_REASONS
    assert not any(row["admissible"] for row in census)
    assert all(row["sidecar"] for row in census)


def _lose_every_wav(dumps: Path) -> None:
    """The ring listed the sidecars; the takes they name are not beside them."""
    for wav in (dumps / "wav").glob("*.wav"):
        wav.unlink()


def _strip_every_stamp(dumps: Path) -> None:
    """The bank step wrote dump names without the microsecond stamp."""
    for index, sidecar in enumerate(sorted((dumps / "sidecar").glob("*.json"))):
        stem = f"{sidecar.stem.split('_', 1)[1]}{index}"
        (dumps / "wav" / f"{sidecar.stem}.wav").rename(dumps / "wav" / f"{stem}.wav")
        sidecar.rename(sidecar.with_name(f"{stem}.json"))


@pytest.mark.parametrize(
    ("break_ring", "expected_capture_reason"),
    [
        pytest.param(_lose_every_wav, fx.CAPTURE_WAV_MISSING, id="wav_missing"),
        pytest.param(_strip_every_stamp, fx.CAPTURE_UNSTAMPED_NAME, id="unstamped"),
    ],
)
def test_a_ring_that_lost_this_rounds_takes_blames_the_ring_not_the_round_shape(
    tmp_path, break_ring, expected_capture_reason
):
    """The third way to reach no capture, and the one #3480 was actually about.

    Every sidecar here is this session's and names an admissible phase — the
    round IS verify-shaped, and the remedy ``round_shape_inadmissible``
    documents (point at a verify-shaped round) is the one move that cannot
    help. A slug read off ``phases_seen`` alone cannot tell this from a
    MEASURE-only round; a slug read off the census can.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    break_ring(dumps)

    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)

    assert caught.value.reason == fx.CAPTURES_UNREADABLE
    assert caught.value.reason in fx.CLASSIFICATION_REFUSAL_REASONS
    census = caught.value.detail["captures"]
    assert len(census) == len(list((dumps / "sidecar").glob("*.json")))
    assert {row["reason"] for row in census} == {expected_capture_reason}
    assert not any(row["admissible"] for row in census)
    # The shape the round WAS measured in is still on the refusal, and it is an
    # admissible one -- which is what makes the round shape the wrong remedy.
    assert set(caught.value.detail["phases_seen"]) <= set(fx.ADMISSIBLE_PHASES)
    assert caught.value.detail["note"]


def test_a_capture_whose_program_is_missing_refuses_the_whole_round(tmp_path):
    """Half a round classified is a different answer, silently."""
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    # ``_play`` banks the first summed-sweep phase's bytes a second time under
    # this name, so it shares verify's hash and is invisible in the binding map.
    shutil.copy(round_dir / "verify_program.wav", round_dir / "summed_program.wav")
    (round_dir / "cloud_verify_program.wav").unlink()
    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    detail = caught.value.detail
    assert caught.value.reason == fx.PROGRAM_MISSING
    assert detail["phases"] == ["cloud_verify"]
    assert detail["matched_by"] == "provenance.stimulus.wav_sha256"
    assert detail["programs_present"] == ["summed_program.wav", "verify_program.wav"]


def test_a_capture_binds_to_the_program_whose_bytes_it_heard_not_its_phase_label(
    tmp_path,
):
    """#3504: the phase LABEL on a sidecar does not name the program played.

    ``provenance.stimulus.phase`` reads ``verify`` for every cloud position by
    construction, and the round directory holds two different sweeps under two
    phase names. Only the banked content hash says which of them was emitted,
    so every capture here binds to ``verify_program.wav`` however its own
    sidecar is labelled.
    """
    bundle, dumps = _bundle(
        tmp_path, _flat_ir(), stimulus_sha=lambda phase, shas: shas["verify"]
    )
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    verify = round_dir / "verify_program.wav"
    assert verify.read_bytes() != (round_dir / "cloud_verify_program.wav").read_bytes()

    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)

    assert {cap.phase for cap in captures} == {"verify", "cloud_verify"}
    assert {cap.program for cap in captures} == {verify}


@pytest.mark.parametrize(
    ("stimulus_sha", "expected_reason", "expected_capture_reason", "expected_digest"),
    [
        pytest.param(
            lambda phase, shas: None,
            fx.CAPTURES_UNREADABLE,
            fx.CAPTURE_PROGRAM_UNIDENTIFIED,
            None,
            id="no_hash_banked",
        ),
        pytest.param(
            lambda phase, shas: "0" * 64,
            fx.PROGRAM_MISSING,
            fx.CAPTURE_PROGRAM_MISSING,
            "0" * 12,
            id="hash_no_program_carries",
        ),
    ],
)
def test_a_stimulus_hash_that_binds_to_no_program_refuses_by_name(
    tmp_path, stimulus_sha, expected_reason, expected_capture_reason, expected_digest
):
    """#3504 itself: without proven bytes there is nothing to deconvolve.

    No banked hash and a hash no program carries are one failure — nothing
    says which program played — refused by name, never guessed at by label.
    """
    bundle, dumps = _bundle(tmp_path, _flat_ir(), stimulus_sha=stimulus_sha)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None

    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)

    assert caught.value.reason == expected_reason
    census = caught.value.detail["captures"]
    # The row names the digest it matched against, so a refusal is checkable
    # from its own payload.
    assert {row["stimulus_wav_sha256_12"] for row in census} == {expected_digest}
    assert {row["reason"] for row in census} == {expected_capture_reason}
    assert {row["reason"] for row in census} <= fx.CAPTURE_ADMISSIBILITY_REASONS
    assert not any(row["admissible"] for row in census)


def _bind_a_lateral_capture(round_dir: Path, dumps: Path) -> None:
    (dumps / "sidecar" / "1787000000999999_lateral_mic.json").write_text(
        json.dumps(
            {"phase": "lateral", "jts_session_identity": {"session_id": SESSION_ID}}
        )
    )


def _lose_a_program(round_dir: Path, dumps: Path) -> None:
    (round_dir / "cloud_verify_program.wav").unlink()


@pytest.mark.parametrize(
    ("break_round", "expected_reason", "expected_capture_reason"),
    [
        pytest.param(
            _bind_a_lateral_capture,
            fx.LATERAL_CAPTURE_SHAPE,
            fx.CAPTURE_LATERAL_SHAPE,
            id="lateral",
        ),
        pytest.param(
            _lose_a_program,
            fx.PROGRAM_MISSING,
            fx.CAPTURE_PROGRAM_MISSING,
            id="program_missing",
        ),
    ],
)
def test_every_refusal_this_loader_raises_names_the_captures_the_ring_listed(
    tmp_path, break_round, expected_reason, expected_capture_reason
):
    """The census promise in the loader's docstring is unconditional or noise.

    #3480 is a refusal whose count contradicts the ring listing the operator
    was just handed. Two of this loader's three refusals were raised before the
    census it had already built was attached — so the old shape survived in
    exactly the refusals a per-driver or half-banked round reaches.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    break_round(round_dir, dumps)

    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)

    assert caught.value.reason == expected_reason
    census = caught.value.detail["captures"]
    # One row per sidecar the ring listed, counted off the ring itself rather
    # than restated here: a census shorter than the listing is #3480 again.
    assert len(census) == len(list((dumps / "sidecar").glob("*.json")))
    assert expected_capture_reason in {row["reason"] for row in census}
    assert {row["reason"] for row in census} <= fx.CAPTURE_ADMISSIBILITY_REASONS
    assert all(row["sidecar"] for row in census)


def test_another_round_in_the_same_ring_is_not_pooled_in(tmp_path):
    """A ring holding two rounds is split by the bundle's own session id."""
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    stranger = dumps / "sidecar" / "1787000000888888_verify_mic.json"
    stranger.write_text(
        json.dumps(
            {"phase": "verify", "jts_session_identity": {"session_id": "someone-else"}}
        )
    )
    _write_wav(dumps / "wav" / f"{stranger.stem}.wav", _sweep())
    ours = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    assert len(ours) == 3
    assert all(stranger.stem not in cap.wav.name for cap in ours)


# --------------------------------------------------------------------------- #
# what the artifact promises its readers
# --------------------------------------------------------------------------- #


def test_the_artifact_conforms_to_the_register(peak_artifact):
    """The register's own reader types every row this instrument emits.

    Imported, not restated: a schema this test spelled out itself would be a
    second source of truth for the shape, and the one that matters is whatever
    ``read_feature_verdicts`` accepts.
    """
    verdicts = read_feature_verdicts(peak_artifact)
    assert len(verdicts) == len(peak_artifact["rows"])
    for verdict, row in zip(verdicts, peak_artifact["rows"]):
        # Every field the register types, bound — not a sample of them. A
        # partial check would let the instrument stop emitting one of these and
        # still read as conforming.
        assert verdict.freq_hz == row["hz"]
        assert verdict.classification == row["classification"]
        assert verdict.egd_verdict == row["egd_verdict"]
        assert verdict.gate_verdict == row["gate_verdict"]
        assert verdict.confidence == row["confidence"]
        assert verdict.measured_q == row["measured_q"]
        assert verdict.depth_db == row["depth_db"]
        assert verdict.classification in CLASSIFICATIONS
        # Imported, not restated — same rule as the shape above. This
        # instrument spelled the middle rank `med` until 2026-08-22, alone
        # against every sibling answering the same question; reading the set
        # off the shared vocabulary is what keeps it from drifting back.
        assert verdict.confidence in set(get_args(TrustLevel))
        # The register's own round-trip: what a packet publishes is readable
        # back through the same reader.
        assert read_feature_verdicts([verdict.to_dict()])[0] == verdict


def test_the_register_enumerates_exactly_the_columns_this_instrument_writes(
    peak_artifact, dip_artifact,
):
    """``LAB_ROW_FIELDS`` is the packet's allowlist, so drift silently narrows it.

    The evidence packet copies a banked row field by field through that tuple.
    A column this instrument starts writing that nobody adds there would be
    withheld from every reader — visible only as a name in ``redacted_fields``,
    which is honest but is not what anyone intended. The reverse drift is worse
    in a quieter way: a name left in the tuple after the instrument stops
    emitting it makes the register describe a row that no longer exists.

    Equality, both directions, against a real run rather than a fixture — the
    instrument's own output is the only thing that can settle what it writes.
    Order too: the tuple documents itself as the order the columns are written
    in, and a claim about order that nothing checks is a claim that rots.
    """
    for artifact in (peak_artifact, dip_artifact):
        for row in artifact["rows"]:
            assert tuple(row) == LAB_ROW_FIELDS


def test_every_uncertainty_the_register_labels_is_a_column_that_exists(
    peak_artifact,
):
    """Labels for columns, not for hopes.

    Both maps key on real row columns and never on each other's, so ``random``
    versus ``systematic`` stays a statement about numbers a reader can actually
    see, and no column is both an uncertainty and not one.
    """
    row = peak_artifact["rows"][0]
    labelled = set(LAB_ROW_UNCERTAINTY) | set(LAB_ROW_NOT_AN_UNCERTAINTY)

    assert labelled <= set(row)
    assert not set(LAB_ROW_UNCERTAINTY) & set(LAB_ROW_NOT_AN_UNCERTAINTY)
    for name in LAB_ROW_UNCERTAINTY:
        # A labelled uncertainty is a NUMBER. A label on a verdict string or a
        # per-gate table would be a category error the map's own shape invites.
        assert isinstance(row[name], float), name


def test_the_instrument_stamps_no_vertical_blindness_field(
    peak_artifact, dip_artifact,
):
    """The capture geometry is disclosed ONCE, and not from here.

    Every shape this instrument reads is still horizontal — that fact did not
    change on 2026-08-21 and nothing here claims otherwise. What changed is
    where it is said: the evidence packet's ``not_evaluated`` block states it
    once, as ``vertical_plane_response``, for the whole corpus. A per-row flag
    of that name is what #2783 was: two producers spelling one word two ways
    (this instrument meant the PLANE, the 2026-08-19 lab tool meant "fewer than
    two gates resolved"), with a boost bar honouring whichever it was handed.
    The gate-resolution fact keeps its own honest name here.
    """
    for artifact in (peak_artifact, dip_artifact):
        assert "vertical_blind" not in artifact["measurement"]
        assert "vertical_blind_note" not in artifact["measurement"]
        for row in artifact["rows"]:
            assert "vertical_blind" not in row
            assert "resolved_gates" in row
    dip = read_feature_verdicts(dip_artifact)
    vouching, _ = defect_boostable_at(dip, dip[0].freq_hz)
    assert vouching is not None


def test_the_artifact_states_every_threshold_it_used(peak_artifact):
    """A reader that disagrees with a threshold can re-derive rather than argue."""
    thresholds = peak_artifact["thresholds"]
    assert thresholds["frac_nmp_min_phase"] == fx.FRAC_NMP_MIN_PHASE
    assert thresholds["frac_nmp_non_min_phase"] == fx.FRAC_NMP_NON_MIN_PHASE
    assert thresholds["z_local_flat"] == fx.Z_LOCAL_FLAT
    assert thresholds["sigma_growth_room_ratio"] == fx.SIGMA_GROWTH_ROOM_RATIO
    assert thresholds["sigma_growth_min_sigma_db"] == fx.SIGMA_GROWTH_MIN_SIGMA_DB
    assert thresholds["gate_delta_slack_db"] == fx.GATE_DELTA_SLACK_DB
    measurement = peak_artifact["measurement"]
    # The ladder is the engine's, and the frame every rung was read in rides
    # beside it: the same capture and feature read materially different depths
    # under each defensible frame (P1 sec 6), so a banked number without one is
    # the frame's number rather than the speaker's.
    assert measurement["gate_ladder_ms"] == list(DEFAULT_RUNGS_MS)
    assert measurement["gate_ladder_frame"] == frame_descriptor(
        DEFAULT_RUNGS_MS, sweep_grid()
    )
    assert measurement["gate_ladder_refused"] is None
    # The trusted floor is the gating module's, derived from the gate length —
    # never a number this instrument spells.
    assert measurement["trusted_band_hz"][0] == pytest.approx(
        f_trusted_floor_hz(fx.DEFAULT_GATE_MS * 1e-3)
    )


def test_the_operator_summary_is_one_line_per_row_under_any_disclosure(
    peak_artifact,
):
    """What the view prints, owned beside the artifact whose columns it reads.

    A round whose controls passed has no disclosure line, so the lines ARE the
    rows; a failed suite prepends exactly one line, because an exit-0 round
    that gave up its phase class must not read as a clean one.
    """
    clean = fx.summary_lines(peak_artifact)
    assert len(clean) == len(peak_artifact["rows"])
    assert all(f"{row['hz']:.0f} Hz" in line for row, line in zip(peak_artifact["rows"], clean))
    assert all(row["classification"] in line for row, line in zip(peak_artifact["rows"], clean))

    disclosed = fx.summary_lines(
        {**peak_artifact, "controls_disclosure": fx.CONTROLS_FAILED_DISCLOSURE}
    )
    assert disclosed[1:] == clean
    assert fx.CONTROLS_FAILED_DISCLOSURE in disclosed[0]


def test_timing_scatter_reports_that_it_did_not_run(peak_artifact):
    """No repeated angle means no pair, and an unmeasured dimension says so."""
    timing = peak_artifact["timing_scatter"]
    assert timing["available"] is False
    assert timing["n_pairs"] == 0
    assert "subsample_residual_us" not in timing
    assert all(row["timing_corroborated"] is False for row in peak_artifact["rows"])
    assert all(row["confidence"] != "high" for row in peak_artifact["rows"])


# --------------------------------------------------------------------------- #
# 6.2: off-axis persistence, read from ALREADY-BANKED lateral pose curves
# --------------------------------------------------------------------------- #


def _pose_curve(
    ir: np.ndarray,
    *,
    pose_id: str,
    position_deg: int | None,
    role: str = "woofer",
    band_hz: tuple[float, float] = (200.0, 8000.0),
) -> fx.RoundPoseCurve:
    """A synthetic banked pose curve, built the same way the module's own
    ``_resonant_ir`` fixtures are read -- :func:`fx.magnitude_response`, the
    seam :func:`~jasper.active_speaker.crossover_v2.feature_classifier.smoothed_curve`
    itself uses, never a hand-rolled transform.
    """
    freqs, db = fx.magnitude_response(ir.astype(np.float32), SR)
    keep = np.isfinite(db) & (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    return fx.RoundPoseCurve(
        pose_id=pose_id,
        position_deg=position_deg,
        role=role,
        freqs_hz=freqs[keep],
        magnitude_db=db[keep],
        band_hz=band_hz,
    )


def test_off_axis_persistence_reads_present_and_not_resolved(tmp_path):
    """6.2: a feature present at every pose reads a depth/centre there; a
    pose whose curve never swept the feature's band reads not-resolved.

    ``absent is not zero`` (the ticket's own words): the not-resolved pose's
    numbers are ``None``, never a fabricated 0 dB.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    present = [
        _pose_curve(_resonant_ir(+3.0), pose_id="lateral_00_a01", position_deg=-15),
        _pose_curve(_resonant_ir(+3.0), pose_id="lateral_01_a01", position_deg=15),
    ]
    # This pose's own driven band never reached the feature at all -- the
    # honest, unambiguous way to construct "vanished" without guessing
    # whether a sign-flipped read means the feature moved or the noise did.
    vanished = _pose_curve(
        _flat_ir(), pose_id="lateral_02_a01", position_deg=30, band_hz=(20.0, 500.0)
    )
    artifact = fx.classify_round(
        captures, at=[RESONANCE_HZ], pose_curves=[*present, vanished]
    )
    assert artifact["pose_bank"] == {"available": True, "n_poses": 3}
    persistence = artifact["rows"][0]["pose_persistence"]
    assert len(persistence) == 3
    by_pose = {entry["pose_id"]: entry for entry in persistence}
    for pose_id, degrees in (("lateral_00_a01", -15), ("lateral_01_a01", 15)):
        entry = by_pose[pose_id]
        assert entry["resolved"] is True
        assert entry["position_deg"] == degrees
        assert isinstance(entry["pooled_db"], float)
        assert isinstance(entry["centre_hz"], float)
        assert abs(math.log2(entry["centre_hz"] / RESONANCE_HZ)) < fx.NEIGHBOURHOOD_OCT
    vanished_entry = by_pose["lateral_02_a01"]
    assert vanished_entry["resolved"] is False
    assert vanished_entry["pooled_db"] is None
    assert vanished_entry["centre_hz"] is None


def test_no_lateral_poses_reads_as_not_run(peak_artifact):
    """6.2(d): a round with no banked pose curves gets the classifier's own
    NOT-RUN shape (mirroring ``_timing_scatter``'s), and every row's
    persistence table is empty rather than absent.
    """
    assert peak_artifact["pose_bank"]["available"] is False
    assert peak_artifact["pose_bank"]["n_poses"] == 0
    assert all(row["pose_persistence"] == [] for row in peak_artifact["rows"])


def _bank_lateral_pose(
    bundle: Path, *, take_id: str, position_deg: int, curves: list[dict],
    vertical_deg: int = 0, capture: str = "wired-TEST",
) -> None:
    """Directly write a banked ``positions/<take_id>.json`` -- the exact
    fields :func:`~jasper.active_speaker.crossover_v2.record_index.bundle_measurements`
    and :func:`~jasper.active_speaker.crossover_v2.position_cycle.read_take_curves`
    read, real-shaped without going through the retention engine.
    """
    from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND

    positions_dir = (
        bundle / "evidence/v1/artifacts/crossover_v2" / capture / "positions"
    )
    positions_dir.mkdir(parents=True, exist_ok=True)
    (positions_dir / f"{take_id}.json").write_text(
        json.dumps({
            "kind": POSITION_EVIDENCE_KIND,
            "phase": fx.PHASE_LATERAL,
            "position_deg": position_deg,
            "vertical_deg": vertical_deg,
            "curves": curves,
        })
    )


def test_the_cli_reads_banked_lateral_poses_into_persistence(tmp_path, capsys):
    """6.2: the reuse this ticket requires, end to end -- ``load_round_pose_curves``
    reaches a REAL banked take file through the same reader
    ``jasper-round-views delay-landscape`` uses, never a second tree-walker.

    The stop is banked TWICE -- a superseded first attempt whose curve never
    swept the feature, then the retake. Latest attempt wins: exactly one
    persistence entry, the retake's, resolved. An include-all regression
    would read two poses; an oldest-wins regression would read unresolved.

    The stop is a RAISED seat, so the entry's pose key carries both halves
    of the pose -- bearing and elevation -- off the banked file.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    superseded = _pose_curve(
        _flat_ir(), pose_id="ignored", position_deg=-20, band_hz=(20.0, 500.0)
    )
    curve = _pose_curve(_resonant_ir(+3.0), pose_id="ignored", position_deg=-20)
    for take_id, banked_curve in (
        ("lateral_00_a01", superseded), ("lateral_00_a02", curve),
    ):
        _bank_lateral_pose(
            bundle,
            take_id=take_id,
            position_deg=-20,
            vertical_deg=10,
            curves=[{
                "role": "woofer",
                "band_hz": list(banked_curve.band_hz),
                "freqs_hz": [float(v) for v in banked_curve.freqs_hz],
                "magnitude_db": [float(v) for v in banked_curve.magnitude_db],
                "phase_deg": [0.0] * banked_curve.freqs_hz.size,
            }],
        )
    code = cli.main(
        ["classify-features", str(bundle), "--dumps", str(dumps), "--at", str(RESONANCE_HZ)]
    )
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())
    assert banked["pose_bank"] == {"available": True, "n_poses": 1}
    persistence = banked["rows"][0]["pose_persistence"]
    assert len(persistence) == 1
    assert persistence[0]["pose_id"] == "lateral_00_a02"
    assert persistence[0]["position_deg"] == -20
    assert persistence[0]["vertical_deg"] == 10
    assert persistence[0]["resolved"] is True


# --------------------------------------------------------------------------- #
# 6.3: narrow-band decay, from the IRs this instrument already deconvolves
# --------------------------------------------------------------------------- #

#: Tolerance the injected-ring control's recovered time must land within,
#: relative to the ring's own analytically known time-to-neg20dB
#: (``tau * ln(10)``). Loose enough to absorb the FFT band mask's own
#: passband ripple; tight enough that a broken envelope read could not pass
#: by luck -- a clean impulse (no injected ring at all) reads roughly 20x
#: faster than this control's expected time, at the same centre band.
_DECAY_CONTROL_REL_TOL = 0.25
_DECAY_RESONANCE_TAU_S = 0.02


def _decaying_sinusoid_ir(
    fc: float, tau_s: float, *, peak: int = 200, seconds: float = 0.6
) -> np.ndarray:
    """An impulse plus a decaying sinusoid of analytically KNOWN ring time.

    Time-to-``DECAY_TARGET_DROP_DB`` of ``exp(-t/tau)`` is exactly
    ``tau * ln(10^(DECAY_TARGET_DROP_DB/20))`` -- ``tau * ln(10)`` at the
    shipped 20 dB target -- which is the known answer the control below
    grades :func:`fx._decay_read` against.
    """
    n = int(seconds * SR)
    ir = np.zeros(n)
    ir[peak] = 1.0
    t = np.arange(n - peak) / SR
    ir[peak:] += 0.5 * np.exp(-t / tau_s) * np.sin(2 * np.pi * fc * t)
    return ir


def test_decay_recovers_an_injected_rings_known_time():
    """The campaign's own known-answer control, mirroring the EGD controls'
    style: inject a ring of a KNOWN time constant and read it back within
    tolerance.
    """
    ir = _decaying_sinusoid_ir(RESONANCE_HZ, _DECAY_RESONANCE_TAU_S)
    band = fx._decay_bands_hz(RESONANCE_HZ)["center"]
    result = fx._decay_read(fx._DecayHost.of(ir, SR), band)
    expected_ms = _DECAY_RESONANCE_TAU_S * math.log(10) * 1000.0
    assert result["below_floor"] is False
    assert result["time_to_neg20_db_ms"] == pytest.approx(
        expected_ms, rel=_DECAY_CONTROL_REL_TOL
    )


def test_decay_reads_fast_on_a_clean_impulse():
    """Negative control: no injected ring, so the band-limited impulse's own
    bandwidth-bound ring-down must read far faster than a real resonance —
    the "6-10 ms just outside" half of the campaign's own contrast.
    """
    ir = np.zeros(int(0.6 * SR))
    ir[200] = 1.0
    band = fx._decay_bands_hz(RESONANCE_HZ)["center"]
    result = fx._decay_read(fx._DecayHost.of(ir, SR), band)
    assert result["below_floor"] is False
    assert result["time_to_neg20_db_ms"] is not None
    assert result["time_to_neg20_db_ms"] < 10.0


def test_decay_reports_below_floor_for_a_steady_tone():
    """An undamped sinusoid never decays, so the -20 dB point is
    unreachable above its own tail — reported as ``below_floor``, never a
    fabricated time.
    """
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    ir = 0.5 * np.sin(2 * np.pi * RESONANCE_HZ * t)
    band = fx._decay_bands_hz(RESONANCE_HZ)["center"]
    result = fx._decay_read(fx._DecayHost.of(ir, SR), band)
    assert result["below_floor"] is True
    assert result["time_to_neg20_db_ms"] is None


def test_the_artifact_carries_the_decay_field_with_units(peak_artifact):
    """Facts only, and the field names carry their own units."""
    assert (
        peak_artifact["thresholds"]["decay_target_drop_db"] == fx.DECAY_TARGET_DROP_DB
    )
    decay = peak_artifact["rows"][0]["decay"]
    assert set(decay) == {"center", "flank_lo", "flank_hi"}
    for band in decay.values():
        assert set(band) == {
            "band_hz", "noise_floor_db", "below_floor", "time_to_neg20_db_ms",
        }
        assert isinstance(band["noise_floor_db"], float)
        assert isinstance(band["below_floor"], bool)
        assert band["time_to_neg20_db_ms"] is None or isinstance(
            band["time_to_neg20_db_ms"], float
        )


# --------------------------------------------------------------------------- #
# the pieces the verdicts rest on
# --------------------------------------------------------------------------- #


def test_the_vectorised_slope_matches_a_least_squares_fit():
    """The group-delay derivative is a prefix-sum OLS; pin it against polyfit.

    A per-bin ``polyfit`` over ~21 000 in-band bins is what this replaced, and
    a silently-wrong vectorisation would move every excursion at once — which
    is the one error the controls could NOT catch, because it would move the
    controls too.
    """
    rng = np.random.default_rng(4)
    y = np.cumsum(rng.normal(0, 0.01, 2048))
    lo = np.arange(2048) - 40
    hi = np.arange(2048) + 41
    lo = np.clip(lo, 0, 2048)
    hi = np.clip(hi, 0, 2048)
    fast = fx._window_slopes(y, lo, hi)
    for index in (100, 512, 1024, 2000):
        window = np.arange(lo[index], hi[index], dtype=float)
        expected = np.polyfit(window, y[lo[index] : hi[index]], 1)[0]
        assert fast[index] == pytest.approx(expected, rel=1e-6, abs=1e-12)


def test_a_short_window_yields_no_slope():
    """Fewer than four samples is two points and a rounding error."""
    y = np.arange(10, dtype=float)
    lo = np.zeros(10, dtype=int)
    hi = np.full(10, 3)
    assert np.all(np.isnan(fx._window_slopes(y, lo, hi)))


def test_the_classifiable_band_keeps_a_feature_off_the_edge():
    """A verdict needs its whole neighbourhood inside the trusted band."""
    lo, hi = fx.classifiable_band_hz((357.0, 16000.0))
    assert lo == pytest.approx(357.0 * 2 ** fx.NEIGHBOURHOOD_OCT)
    assert hi == pytest.approx(16000.0 * 2**-fx.NEIGHBOURHOOD_OCT)
    assert lo > 357.0 and hi < 16000.0


def test_a_requested_frequency_outside_that_band_is_not_classified(tmp_path):
    """``--at`` is a request, not an override of what the gate can resolve."""
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    captures = fx.load_round_captures(round_dir, dumps, session_id=SESSION_ID)
    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.classify_round(captures, at=[15500.0])
    assert caught.value.reason == fx.NO_FEATURES_DETECTED
    assert caught.value.detail["requested"] == [15500.0]


def test_a_quiet_delayed_copy_stays_minimum_phase():
    """``|g| < 1`` keeps every zero inside the unit circle. This is physics.

    The whole classification turns on it: a delayed copy only goes
    non-minimum-phase once it is LOUDER than the direct sound, which is why the
    spec's ``-0.5`` case is a NEGATIVE control and the ``1.2`` case is the
    positive one. Asserted against the instrument's own perturbation, so the
    claim in its docstring is checked rather than restated.
    """
    from scipy.signal import tf2zpk

    impulse = np.zeros(64)
    impulse[0] = 1.0
    for gain, expect_inside in (
        (fx.CONTROL_COMB_MP_GAIN, True),
        (fx.CONTROL_COMB_NMP_GAIN, False),
    ):
        taps = fx.add_delayed_copy(impulse, gain, 4 / SR * 1e3, SR)
        zeros, _, _ = tf2zpk(np.trim_zeros(taps, "b"), np.array([1.0]))
        assert all(abs(z) < 1.0 for z in zeros) is expect_inside


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def test_the_cli_files_the_verdict_where_the_packet_reads_it(tmp_path, capsys):
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    code = cli.main(["classify-features", str(bundle), "--dumps", str(dumps)])
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())
    assert read_feature_verdicts(banked)[0].classification == DEFECT_CUTTABLE


def test_the_cli_classifies_a_bank_shape_round(tmp_path, capsys):
    """The exact composition docs/testing-tooling.md shows: a banked round.

    ``bank-crossover-round.sh`` tars a live Pi session bundle verbatim, so its
    program WAVs land in a sibling ``crossover_v2/<capture>/`` directory,
    never inside the JSON receipts one. Before this fix, pointing the CLI at
    exactly this shape refused ``classification_program_missing`` with
    ``programs_present: []`` on a real banked round; this reproduces that
    failure synthetically and asserts it is now classified instead.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0), bank_shape=True)
    code = cli.main(["classify-features", str(bundle), "--dumps", str(dumps)])
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    # Filed where the receipts shape always filed it -- the ONE location the
    # evidence packet reads -- even though the programs it was computed from
    # live in the sibling crossover_v2/ directory instead.
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())
    assert read_feature_verdicts(banked)[0].classification == DEFECT_CUTTABLE


def test_bank_and_receipts_shapes_resolve_to_the_same_captures(tmp_path):
    """Same synthetic speaker, banked two ways -- the classifier reads it alike.

    Mutation check: break the sibling fallback in
    ``evidence_packet.round_program_dir`` (for example, make it always
    return ``round_dir``) and this test's OWN
    ``bank_programs_dir != bank_round_dir`` guard fails first -- proving the
    resolver stopped resolving to the sibling at all. That guard, not
    ``PROGRAM_MISSING``, is the failure mode here:
    ``test_the_cli_classifies_a_bank_shape_round`` is the one that then
    fails downstream with ``PROGRAM_MISSING``, end to end through the CLI.
    """
    ir = _resonant_ir(+3.0)
    receipts_bundle, receipts_dumps = _bundle(tmp_path / "receipts", ir)
    bank_bundle, bank_dumps = _bundle(tmp_path / "bank", ir, bank_shape=True)

    receipts_round_dir, _ = round_artifact_dir(receipts_bundle)
    bank_round_dir, _ = round_artifact_dir(bank_bundle)
    assert receipts_round_dir is not None
    assert bank_round_dir is not None

    receipts_captures = fx.load_round_captures(
        receipts_round_dir, receipts_dumps, session_id=SESSION_ID
    )
    bank_programs_dir = round_program_dir(
        bank_bundle, bank_round_dir, fx.ADMISSIBLE_PHASES
    )
    assert bank_programs_dir != bank_round_dir, "must resolve to the sibling dir"
    bank_captures = fx.load_round_captures(
        bank_programs_dir, bank_dumps, session_id=SESSION_ID
    )

    assert [c.phase for c in bank_captures] == [c.phase for c in receipts_captures]
    assert [c.program.name for c in bank_captures] == [
        c.program.name for c in receipts_captures
    ]
    # Same synthetic speaker, same seed, on both sides: the verdicts must
    # match exactly, not just the phase/program bookkeeping around them.
    assert fx.classify_round(bank_captures) == fx.classify_round(receipts_captures)


def test_a_partially_banked_receipts_round_is_not_rescued_by_the_sibling(tmp_path):
    """round_dir holds only verify's program -- a genuinely incomplete round.

    Mutation pin for the any-vs-all precedence in
    ``evidence_packet.round_program_dir`` (SF3, #2796 gate): its ``any(...)``
    checks, mutated to ``all(...)``, let all 34 tests in this file pass
    anyway -- none of them had a round_dir carrying SOME but not ALL of the
    admissible phases. This one does: round_dir carries verify_program.wav
    only, the sibling carries both, and the ring holds cloud_verify captures
    too. A rescue from the sibling would silently paper over that gap, so
    the resolver must still pick round_dir (it has "any") and
    load_round_captures must still refuse PROGRAM_MISSING for the phase
    round_dir is actually missing.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0), bank_shape=True)
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    sibling = bundle / "crossover_v2/wired-TEST"
    shutil.copy(sibling / "verify_program.wav", round_dir / "verify_program.wav")

    programs_dir = round_program_dir(bundle, round_dir, fx.ADMISSIBLE_PHASES)
    assert programs_dir == round_dir, "any(...) must win over the sibling"

    with pytest.raises(fx.FeatureClassificationRefused) as caught:
        fx.load_round_captures(programs_dir, dumps, session_id=SESSION_ID)
    assert caught.value.reason == fx.PROGRAM_MISSING
    assert caught.value.detail["phases"] == ["cloud_verify"]


def test_a_program_missing_refusal_names_the_directory_it_actually_read(
    tmp_path, capsys
):
    """SF2, #2796 gate: a doctored real bundle showed
    ``programs_present: ['verify']`` about a directory holding zero WAVs --
    because the message named ``round_dir`` while the count came from
    whichever directory the resolver actually read (here, the sibling, not
    round_dir). The published refusal must name that directory, or this
    instrument's own refusal starts the very wrong-directory hunt it exists
    to end.
    """
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0), bank_shape=True)
    (bundle / "crossover_v2/wired-TEST/cloud_verify_program.wav").unlink()
    code = cli.main(["classify-features", str(bundle), "--dumps", str(dumps)])
    assert code == cli.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "crossover_v2/wired-TEST" in captured.err
    payload = json.loads(captured.out)
    assert payload["reason"] == fx.PROGRAM_MISSING
    # The exact shape the gate demonstrated on a real bundle: programs_present
    # names only what the SIBLING carried, and the record must now also say
    # that the sibling -- not evidence/'s round_dir -- is what was read.
    detail = json.loads(payload["detail"])
    assert detail["programs_present"] == ["verify_program.wav"]
    assert detail["programs_dir"].endswith("crossover_v2/wired-TEST")


def test_a_refusal_exits_two_and_banks_nothing(tmp_path, capsys):
    """A refusal must not leave a file a later reader would act on."""
    bundle, dumps = _bundle(tmp_path, _flat_ir())
    code = cli.main(["classify-features", str(bundle), "--dumps", str(dumps)])
    assert code == cli.EXIT_REFUSED
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    assert not (round_dir / CLASSIFICATION_ARTIFACT).exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == fx.NO_FEATURES_DETECTED
    assert payload["reason"] in fx.CLASSIFICATION_REFUSAL_REASONS


def test_failed_controls_exit_zero_and_bank_their_own_disclosure(
    tmp_path, monkeypatch, capsys
):
    """The round is not the casualty of its own known-answer check.

    A failed suite is a fact about the PHASE test; withholding the artifact
    withheld the window evidence too. The verdict is filed, the summary says
    what was lost, and a scripted caller sees exit 0 rather than a refusal it
    cannot distinguish from a broken round.
    """
    monkeypatch.setattr(fx, "CONTROL_MAX_FALSE_POSITIVE_US", 0.0)
    bundle, dumps = _bundle(tmp_path, _resonant_ir(+3.0))
    code = cli.main(["classify-features", str(bundle), "--dumps", str(dumps)])
    assert code == cli.EXIT_OK
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    banked = json.loads((round_dir / CLASSIFICATION_ARTIFACT).read_text())
    assert banked["controls_ok"] is False
    assert banked["controls_disclosure"] == fx.CONTROLS_FAILED_DISCLOSURE
    assert all(row["egd_verdict"] == EGD_AMBIGUOUS for row in banked["rows"])
    # Not silent: an exit-0 round whose controls failed relays the module's
    # own disclosure on stderr rather than reading as a clean run.
    assert fx.CONTROLS_FAILED_DISCLOSURE in capsys.readouterr().err


def test_a_bundle_with_two_rounds_is_refused_rather_than_guessed_at(tmp_path, capsys):
    bundle, dumps = _bundle(tmp_path, _flat_ir())
    (bundle / "evidence/v1/artifacts/crossover_v2/wired-OTHER").mkdir(parents=True)
    assert cli.main(
        ["classify-features", str(bundle), "--dumps", str(dumps)]
    ) == cli.EXIT_UNREADABLE
    err = capsys.readouterr().err
    # Nit from the #2796 gate: the both-shapes guidance belongs on the
    # "structure missing entirely" refusal, not here -- this bundle's
    # structure is fine, the problem is which of two rounds to read, and
    # "check for a second accepted shape" would be misleading advice.
    assert "bank-crossover-round.sh" not in err


def test_a_dir_matching_neither_shape_names_both_in_its_refusal(tmp_path, capsys):
    """Point the CLI at the WAV leaf itself -- the second real failure tonight.

    Neither shape's ``evidence/v1/artifacts/crossover_v2/<capture>/`` exists
    anywhere under this path, so the pre-existing "round could not be read"
    refusal fires -- but its message must name BOTH accepted shapes, not
    only the receipts one, so an operator who just banked a round with
    bank-crossover-round.sh is pointed at the bundle directory instead of
    guessing through a symlink shim, as tonight's real debugging session did.
    """
    bundle, dumps = _bundle(tmp_path, _flat_ir(), bank_shape=True)
    programs_leaf = bundle / "crossover_v2/wired-TEST"
    assert programs_leaf.is_dir()
    code = cli.main(["classify-features", str(programs_leaf), "--dumps", str(dumps)])
    assert code == cli.EXIT_UNREADABLE
    err = capsys.readouterr().err
    assert "evidence/v1/artifacts/crossover_v2" in err
    assert "bank-crossover-round.sh" in err
