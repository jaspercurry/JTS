# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The gating contract's disclosure half — what the gate did, and how it says so.

These tests pin the promises :mod:`jasper.audio_measurement.gate_disclosure`
makes, each of which came from a measurement rather than from taste:

* the evaluation band is the INTERSECTION of the gate's trusted validity
  range with the band the DUT radiates, because the un-intersected reading
  over-reports (E5, ``captures/gating-experiments-20260731`` §5: 4.045 dB
  where the honest number is 1.368 dB);
* a small delta is read WITH ``floor_source``, because "delta ~ 0 means the
  gate sat at a bound" is false;
* a ceiling-capped window never renders as "reflections removed" (#1966).

The real jts3 corpus those numbers come from is an untracked session
artifact, so the fixtures here are synthetic band-limited IRs that reproduce
the same MECHANISM. The exact banked numbers were reproduced bit-for-bit
against the corpus out of band; see the PR body for the transcript.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import firwin, lfilter

from jasper.audio_measurement import gate_disclosure, gating

SR = 48000


def _bandlimited_ir(
    lo_hz: float,
    hi_hz: float,
    *,
    reflection_offset_ms: float | None = None,
    reflection_db: float = -11.6,
    noise_db: float = -50.0,
    seed: int = 1966,
) -> np.ndarray:
    """A physically realizable IR: a band-limited arrival on a noise floor.

    Band-limited on purpose. A bare delta has infinite bandwidth and no
    passband, so it cannot exhibit the "evaluated where the DUT does not
    radiate" failure these tests are about.
    """
    rng = np.random.default_rng(seed)
    n = int(0.030 * SR)
    x = np.zeros(n)
    x[500] = 1.0
    if reflection_offset_ms is not None:
        x[500 + int(round(reflection_offset_ms * 1e-3 * SR))] = 10 ** (
            reflection_db / 20
        )
    taps = firwin(511, [lo_hz / (SR / 2), hi_hz / (SR / 2)], pass_zero=False)
    ir = lfilter(taps, 1.0, x)
    return ir / np.abs(ir).max() + rng.normal(0, 10 ** (noise_db / 20), n)


# ---------- evaluation band: the E5 intersection --------------------------


def test_evaluation_band_intersects_the_trusted_floor_with_the_radiated_band():
    assert gate_disclosure.evaluation_band_hz(357.14, (2500.0, 18000.0)) == (
        2500.0,
        18000.0,
    )
    # Trusted floor above the radiated low edge: the floor binds.
    assert gate_disclosure.evaluation_band_hz(1960.9, (200.0, 2000.0)) == (
        1960.9,
        2000.0,
    )


def test_evaluation_band_is_none_when_the_intersection_is_empty():
    """A gate whose trusted floor sits above everything the DUT radiates can
    be priced over NO band, and no number should be invented for one."""
    assert gate_disclosure.evaluation_band_hz(3870.0, (200.0, 2000.0)) is None


def test_evaluation_band_is_none_without_a_radiated_band_never_a_default():
    """Defaulting the radiated band to the full audio range IS the E5 defect.

    Read literally, the v1 contract evaluated a 2 kHz-high-passed tweeter
    from 357 Hz — where it has no output — and reported 3x the honest RMS.
    An absent band therefore yields no delta at all rather than a
    full-range one.
    """
    assert gate_disclosure.evaluation_band_hz(357.14, None) is None
    assert gate_disclosure.evaluation_band_hz(None, (2500.0, 18000.0)) is None


def test_eval_band_correction_shrinks_a_tweeters_inflated_delta():
    """E5's headline: pricing a high-passed branch from its trusted floor
    measures noise against noise. The intersected reading must be materially
    smaller than the literal one, and BOTH must be recorded so the size of
    the correction stays auditable in the record.

    Banked real-corpus numbers (tweeter, 7 ms gate): literal RMS 4.045 dB
    vs honest 1.368 dB — 3.0x — and max 44.93 vs 10.14 dB. This synthetic
    tweeter has pure noise out of band rather than a real room tail, so its
    inflation is larger still; the assertion is on the direction and the
    order of magnitude, which is what transfers.
    """
    ir = _bandlimited_ir(2500.0, 18000.0)
    gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_SEARCH_BOUND

    delta = gate_disclosure.pre_post_gate_delta(
        ir, gated, SR,
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=(2500.0, 18000.0),
    )
    assert delta is not None
    assert delta["eval_band_hz"] == [2500.0, 18000.0]
    assert delta["literal_band_hz"][0] == pytest.approx(fragment["f_trusted_hz"])
    assert delta["literal_band_hz"][1] == gate_disclosure.DELTA_CEILING_HZ
    # The correction's whole point: the literal reading over-reports.
    assert delta["literal_rms_db"] > 3.0 * delta["rms_db"]
    assert delta["literal_max_db"] > 3.0 * delta["max_db"]
    assert delta["n_bins"] > 0


def test_pre_post_delta_is_mean_removed_so_a_pure_level_offset_reads_zero():
    """The observable asks whether the gate changed the response's SHAPE.

    A gate necessarily removes energy, so a constant offset between the two
    curves is expected and says nothing. Scaling one side by a constant must
    therefore not move the delta.
    """
    ir = _bandlimited_ir(2500.0, 18000.0)
    gated, fragment = gating.gate_impulse_response(ir, SR)
    base = gate_disclosure.pre_post_gate_delta(
        ir, gated, SR,
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=(2500.0, 18000.0),
    )
    scaled = gate_disclosure.pre_post_gate_delta(
        ir, gated * 0.25, SR,
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=(2500.0, 18000.0),
    )
    assert base is not None and scaled is not None
    assert scaled["rms_db"] == pytest.approx(base["rms_db"], abs=1e-9)


def test_pre_post_delta_declines_rather_than_guessing_on_unusable_input():
    ir = _bandlimited_ir(2500.0, 18000.0)
    gated, fragment = gating.gate_impulse_response(ir, SR)
    common = dict(
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=(2500.0, 18000.0),
    )
    assert gate_disclosure.pre_post_gate_delta(ir, gated[:-1], SR, **common) is None
    assert gate_disclosure.pre_post_gate_delta(ir, gated, 0, **common) is None
    assert gate_disclosure.pre_post_gate_delta(
        np.array([]), np.array([]), SR, **common
    ) is None
    assert gate_disclosure.pre_post_gate_delta(
        ir, gated, SR, trusted_floor_hz=fragment["f_trusted_hz"], radiated_band_hz=None
    ) is None


# ---------- the typed record ----------------------------------------------


def test_gated_anything_is_true_only_for_a_measured_reflection():
    """"Reflections were removed" is true on the measured bound and NOWHERE
    else — a ceiling-capped window gated nothing out however long it is."""
    measured = gate_disclosure.build_gate_disclosure(
        {"floor_source": gating.FLOOR_MEASURED, "window_ms": 4.0}
    )
    capped = gate_disclosure.build_gate_disclosure(
        {"floor_source": gating.FLOOR_SEARCH_BOUND, "window_ms": 7.0}
    )
    ungateable = gate_disclosure.build_gate_disclosure({"floor_source": None})
    assert measured.gated_anything is True
    assert capped.gated_anything is False
    assert ungateable.gated_anything is False


def test_internal_reflection_count_excludes_gateable_ledger_entries():
    """A ``gateable`` candidate the detector did not select is NOT a
    loudspeaker-internal feature, and counting it as one would be the same
    overstatement this contract exists to remove."""
    d = gate_disclosure.build_gate_disclosure({
        "floor_source": gating.FLOOR_SEARCH_BOUND,
        "internal_reflection_ledger": [
            {"tau_us": 291.7, "level_db": -11.2,
             "classification": gating.CLASS_DUT_INTERNAL},
            {"tau_us": 645.8, "level_db": -19.8,
             "classification": gating.CLASS_GATEABLE},
        ],
    })
    assert d.internal_reflection_count == 1
    assert d.ledger_count == 2


def test_reflection_delay_is_relative_to_the_direct_arrival():
    """Both stored times are absolute inside the analysed IR, which is an
    artifact of the deconvolution window's origin and meaningless alone."""
    d = gate_disclosure.build_gate_disclosure(
        {"floor_source": gating.FLOOR_MEASURED,
         "direct_peak_ms": 10.4, "first_reflection_ms": 14.4}
    )
    assert d.reflection_delay_ms == pytest.approx(4.0)


def test_build_gate_disclosure_never_raises_and_never_fabricates():
    """It reads records written by older builds and runs on best-effort
    diagnostic paths, so a missing or malformed field is ``None`` —
    "unknown" — never a substituted value."""
    for block in (None, {}, {"window_ms": "seven"}, {"floor_source": 12},
                  {"pre_post_gate_delta": "nope"},
                  {"internal_reflection_ledger": "nope"}):
        d = gate_disclosure.build_gate_disclosure(block)
        assert d.gate_ms is None
        assert d.source_of_bound is None
        assert d.delta_rms_db is None
        assert d.internal_reflection_count == 0


def test_build_gate_disclosure_reads_a_schema_1_block_without_inventing_fields():
    """A pre-contract record has no trusted floor and no ledger. Those come
    back ``None``/0 rather than derived, because a schema-1
    ``first_reflection_ms`` is an ONSET and re-deriving from it would
    silently relabel an onset as an arrival."""
    d = gate_disclosure.build_gate_disclosure({
        "schema_version": 1,
        "direct_peak_ms": 10.4,
        "first_reflection_ms": 14.4,
        "window_ms": 4.0,
        "window": "half_hann_tail",
        "f_valid_floor_hz": 250.0,
        "floor_source": gating.FLOOR_MEASURED,
    })
    assert d.gate_ms == pytest.approx(4.0)
    assert d.f_min_hz == pytest.approx(250.0)
    assert d.f_trusted_hz is None
    assert d.internal_reflection_count == 0
    assert d.ledger_count == 0


# ---------- the copy: #1966's fix at the surface --------------------------


def test_describe_gate_says_reflection_measured_on_the_measured_bound():
    ir = _bandlimited_ir(150.0, 2000.0, reflection_offset_ms=4.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    text = gate_disclosure.describe_gate(fragment)
    assert "reflection measured" in text
    assert "no reflection found" not in text
    assert "nothing was gated out" not in text
    # The window ends at the ONSET, which is earlier than the arrival printed
    # beside it. Unless the copy says which one the gate used, the two numbers
    # read as a contradiction.
    assert "gated to its onset" in text
    assert fragment["reflection_onset_ms"] <= fragment["first_reflection_ms"]


def test_describe_gate_says_nothing_was_gated_out_on_the_ceiling_bound():
    """THE #1966 fix. A 7 ms window at the search ceiling read to every
    downstream consumer as "reflections removed" across the entire
    2026-07-30 corpus, because the only thing printed was the number."""
    ir = _bandlimited_ir(2500.0, 18000.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_SEARCH_BOUND
    text = gate_disclosure.describe_gate(fragment)
    assert "no reflection found" in text
    assert "capped at the 7.00 ms search ceiling" in text
    assert "nothing was gated out" in text
    assert "reflection measured" not in text


def test_describe_gate_distinguishes_ungateable_and_exempt_from_a_clean_gate():
    ungateable = gate_disclosure.describe_gate({"floor_source": None})
    exempt = gate_disclosure.describe_gate(
        gating.exempt_gating_block(np.array([0.0, 1.0, 0.0]), SR)
    )
    assert "could not be gated" in ungateable
    assert "gating not applicable (near_field)" in exempt
    for text in (ungateable, exempt):
        assert "reflection measured" not in text
        assert "no reflection found" not in text


def test_describe_gate_discloses_both_floors_and_never_confuses_them():
    ir = _bandlimited_ir(2500.0, 18000.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    text = gate_disclosure.describe_gate(fragment)
    assert "valid above 143 Hz" in text
    assert "trusted above 357 Hz" in text


def test_a_small_delta_reads_differently_depending_on_the_bound():
    """E5 correction (b): "delta ~ 0 means the gate sat at a bound" is FALSE,
    and the two readings of a small delta are opposite epistemic states.

    Same tiny number, same band: on a measured bound it means the capture is
    genuinely clean; on a ceiling-capped one it means the gate did nothing
    and NOTHING WAS PROVEN. The interpretation must ride with the number.
    """
    delta = {"eval_band_hz": [2500.0, 18000.0], "rms_db": 0.2}
    clean = gate_disclosure.describe_gate(
        {"floor_source": gating.FLOOR_MEASURED, "window_ms": 4.0,
         "pre_post_gate_delta": delta}
    )
    capped = gate_disclosure.describe_gate(
        {"floor_source": gating.FLOOR_SEARCH_BOUND, "window_ms": 7.0,
         "pre_post_gate_delta": delta}
    )
    assert "0.20 dB RMS over 2500-18000 Hz" in clean
    assert "0.20 dB RMS over 2500-18000 Hz" in capped
    assert "genuinely little to remove" in clean
    assert "proves nothing about reflections" in capped
    assert "proves nothing about reflections" not in clean


def test_a_large_delta_gets_no_smallness_gloss_either_way():
    """The gloss explains a SMALL number. A large one speaks for itself, and
    attaching "proves nothing" to it would be its own overstatement."""
    delta = {"eval_band_hz": [2500.0, 18000.0], "rms_db": 4.045}
    text = gate_disclosure.describe_gate(
        {"floor_source": gating.FLOOR_SEARCH_BOUND, "window_ms": 7.0,
         "pre_post_gate_delta": delta}
    )
    assert "4.04 dB RMS" in text
    assert "proves nothing" not in text
    assert "genuinely little to remove" not in text


def test_describe_gate_names_the_internal_ledger_when_the_guard_caught_one():
    """The asymmetric-cost guard is disclosed, not silent: a reader must be
    able to see that a real early feature was found and deliberately NOT
    gated (the jts3 horn's ~291 us feature)."""
    text = gate_disclosure.describe_gate({
        "floor_source": gating.FLOOR_SEARCH_BOUND,
        "window_ms": 7.0,
        "internal_reflection_ledger": [
            {"tau_us": 291.7, "level_db": -11.2,
             "classification": gating.CLASS_DUT_INTERNAL},
        ],
    })
    assert "1 early feature arriving before the search window opens" in text
    assert "deliberately not gated" in text


def test_describe_gate_is_silent_about_a_ledger_of_only_gateable_candidates():
    text = gate_disclosure.describe_gate({
        "floor_source": gating.FLOOR_SEARCH_BOUND,
        "window_ms": 7.0,
        "internal_reflection_ledger": [
            {"tau_us": 645.8, "level_db": -19.8,
             "classification": gating.CLASS_GATEABLE},
        ],
    })
    assert "loudspeaker-internal" not in text
