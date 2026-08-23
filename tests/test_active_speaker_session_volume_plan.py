# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped fixed measurement volume + fail-closed latch (Wave 2 C).

Pins the SSOT derivation and the durable latch semantics: intent written BEFORE
the first volume mutation, restore-exactly-once, readback-confirm failure ->
unresolved / emergency, wall-clock ceiling force-drain (live and on hydration),
and crash hydration staying fail-closed without relying on a process restart to
flip states.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.excitation_safety_plan import (
    resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    write_seat_level_reference,
)
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_WALL_CLOCK_CEILING_S,
    MAX_WALL_CLOCK_CEILING_S,
    MEASUREMENT_REFERENCE_VOLUME_DB,
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    SessionVolumeRestoreResult,
    loudest_driver_cap_dbfs,
    unsegmented_stimulus_ceiling_db,
    measurement_reference_volume_db,
    session_measurement_volume_db,
)
from tests.active_speaker_fixtures import mono_output_topology


def _profile_and_targets(*, woofer_peak: float = -30.0, tweeter_peak: float = -70.0):
    topology = mono_output_topology()

    def _driver(target_id, role, peak, required):
        return {
            "target_id": target_id,
            "role": role,
            "model": f"model-{role}",
            "hard_excitation_band_hz": [500, 20_000],
            "measurement_band_hz": [500, 10_000],
            # #2603: the tweeter declares its low limit once; its hard floor and
            # protective high-pass derive from it, instead of sharing the
            # woofer's 500 Hz floor under a 5000 Hz protective high-pass.
            **({"recommended_highpass_hz": 1500} if role == "tweeter" else {}),
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 6,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0,
            },
            "required_protection_filters": required,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": 132 if role == "woofer" else 25,
                **({"baffle_width_mm": 210} if role == "woofer" else {}),
            },
        }

    settings = {
        "drivers": [
            _driver(
                "mono:woofer",
                "woofer",
                woofer_peak,
                [{"kind": "lowpass", "cutoff_hz": 3000, "minimum_slope_db_per_octave": 24}],
            ),
            _driver(
                "mono:tweeter",
                "tweeter",
                tweeter_peak,
                [{"kind": "highpass", "cutoff_hz": 5000, "minimum_slope_db_per_octave": 24}],
            ),
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topology)}
    return profile, targets


class FakeVolume:
    """Records set/get ordering; confirms only the listed targets (else drifts)."""

    def __init__(self, *, initial=-6.0, confirm_targets=None, on_set=None):
        self.value = initial
        self.confirm_targets = confirm_targets  # None => confirm everything
        self.order: list = []
        self._on_set = on_set

    async def set(self, target):
        self.order.append(("set", round(float(target), 3)))
        if self._on_set is not None:
            self._on_set()
        if self.confirm_targets is None or round(float(target), 3) in {
            round(float(t), 3) for t in self.confirm_targets
        }:
            self.value = float(target)
        else:
            self.value = 999.0  # drifted: readback will not confirm the target
        return True

    async def get(self):
        self.order.append(("get", round(float(self.value), 3)))
        return self.value


# --- SSOT derivation ---------------------------------------------------------


def test_session_measurement_volume_targets_the_least_sensitive_driver():
    # The B1 rule: V = min(reference -20, max(caps)). The HIGHEST cap (the
    # least-sensitive driver) governs; more-sensitive drivers attenuate DOWN
    # digitally (always satisfiable), never the other way around.
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    # caps: woofer min(0, 0) = 0; tweeter min(-65, -65) = -65; max = 0 -> V = -20.
    assert session_measurement_volume_db(profile, targets.values()) == -20.0

    # When the highest cap binds BELOW the reference, it wins.
    profile2, targets2 = _profile_and_targets(woofer_peak=-30.0, tweeter_peak=-70.0)
    # caps: woofer -30, tweeter -70; max = -30 -> V = min(-20, -30) = -30.
    assert session_measurement_volume_db(profile2, targets2.values()) == -30.0


def test_absent_seat_level_reference_keeps_the_codified_default(tmp_path):
    """Regression pin: a box that never ran the seat-SPL leveling step behaves
    EXACTLY as it did before the step existed."""
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    assert measurement_reference_volume_db(
        reference_state_path=tmp_path / "absent.json"
    ) == MEASUREMENT_REFERENCE_VOLUME_DB
    assert (
        session_measurement_volume_db(
            profile,
            targets.values(),
            reference_state_path=tmp_path / "absent.json",
        )
        == -20.0
    )


def _bank_reference(path, volume_db):
    write_seat_level_reference(
        reference_volume_db=volume_db,
        measured_db_spl=77.4,
        target=SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07},
        max_main_volume_db=-6.0,
        state_path=path,
    )


def test_a_measured_reference_replaces_the_codified_default(tmp_path):
    """The whole point: a leveling pass that measured 75-80 dB SPL at -17.25 dB
    makes the session hold -17.25 dB, not the -20 dB guess."""
    path = tmp_path / "seat_level_reference.json"
    _bank_reference(path, -17.25)
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    # caps: max = 0 -> the reference is what binds.
    assert (
        session_measurement_volume_db(
            profile, targets.values(), reference_state_path=path
        )
        == -17.25
    )


def test_driver_caps_still_bind_over_a_measured_reference(tmp_path):
    """The caps half is NOT operator-derivable. A banked reference louder than
    every driver's excitation ceiling permits is clamped by ``min``, exactly as
    the codified default is."""
    path = tmp_path / "seat_level_reference.json"
    _bank_reference(path, -8.0)
    profile, targets = _profile_and_targets(woofer_peak=-30.0, tweeter_peak=-70.0)
    # caps: max = -30, well below the -8 dB reference -> the cap wins.
    assert (
        session_measurement_volume_db(
            profile, targets.values(), reference_state_path=path
        )
        == -30.0
    )
    assert loudest_driver_cap_dbfs(profile, targets.values()) == -30.0


def test_an_unsegmented_stimulus_is_bounded_by_digital_headroom_not_a_cap():
    """The 2026-08-23 ruling, pinned as a number.

    The ceiling used to be ``min(caps) - peak``, which made the tightest
    declared ``max_effective_peak_dbfs`` the operative volume ceiling: on this
    fixture, -65 dB for a 0 dBFS stimulus. That figure is protocol discipline,
    not a published damage bound, so it no longer holds the volume down. What
    is left is the room the signal actually has — full scale less its peak.

    Mutation guard: restore ``min(caps) - peak`` and this fails at -65.0,
    65 dB adrift.
    """
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    assert loudest_driver_cap_dbfs(profile, targets.values()) == 0.0
    assert (
        unsegmented_stimulus_ceiling_db(
            profile, targets.values(), stimulus_peak_dbfs=0.0
        )
        == 0.0
    )


def test_the_unsegmented_ceiling_tracks_the_stimulus_peak():
    # A quieter stimulus earns exactly its own headroom back, one for one:
    # full scale - peak, solved for main volume.
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    assert (
        unsegmented_stimulus_ceiling_db(
            profile, targets.values(), stimulus_peak_dbfs=-12.0
        )
        == 12.0
    )


def test_the_new_horn_incident_target_is_reachable():
    """The incident this ruling came from, as an invariant.

    ``jasper-seat-level --target-db-spl 75`` refused ``spl_target_unreachable``
    on the new-horn box at 68.3 dB SPL, ceiling -18.8 dB. Its chain: the woofer
    declared -20 (the figure the research ask itself told the assistant to
    send), the tweeter left on the -65 class seed, declared sensitivities
    10.8 dB apart -> the tweeter's derived cap -30.8 -> ``min(caps)`` -> the
    -18.8 ceiling, with the digital signal peaking at -30.8 dBFS and ~30 dB of
    headroom unused. Reaching 75 dB SPL needed about -12.1 dB.

    Both per-driver caps still resolve to exactly the numbers that bound then;
    what changed is that they no longer bound the volume. The ceiling now
    clears the -12.1 the target needs by a wide margin.

    Mutation guard: restore the cap term and the ceiling is -18.8, BELOW the
    volume the target needs, which is the refusal.
    """
    profile, targets = _profile_and_targets(woofer_peak=-20.0, tweeter_peak=-65.0)
    sensitivities = {"woofer": 90.0, "tweeter": 100.8}
    caps = {
        role: resolve_driver_excitation_ceilings(
            profile,
            fingerprint,
            program_admission=True,
            declared_sensitivities=sensitivities,
        )[1]
        for role, fingerprint in targets.items()
    }
    assert caps["woofer"] == pytest.approx(-20.0)
    assert caps["tweeter"] == pytest.approx(-30.8)
    # What the retired rule produced, recomputed here from those same caps.
    assert min(caps.values()) - (-12.0) == pytest.approx(-18.8)

    ceiling = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=sensitivities,
    )
    assert ceiling == pytest.approx(12.0)
    # The volume that measured 75 dB SPL is now inside the ceiling, so the ramp
    # can command it. The kernel separately clamps to 0 dB full scale, which is
    # still 12.1 dB above what the target needs.
    assert ceiling > -12.1
    assert min(ceiling, 0.0) > -12.1


def test_the_declared_caps_are_disclosed_beside_the_ceiling(caplog):
    """Disclose instead of refuse: the receipt names what was driven past.

    The doctrine's shape for a bound that no longer refuses. An operator has to
    be able to see that this volume puts the tweeter past its declared figure,
    by how much, and how many decibels the declared figures were leaving unused
    — otherwise the ruling reads as "the cap silently stopped mattering".
    """
    profile, targets = _profile_and_targets(woofer_peak=-20.0, tweeter_peak=-65.0)
    sensitivities = {"woofer": 90.0, "tweeter": 100.8}
    with caplog.at_level("INFO", logger="jasper.active_speaker.session_volume_plan"):
        unsegmented_stimulus_ceiling_db(
            profile,
            targets.values(),
            stimulus_peak_dbfs=-12.0,
            declared_sensitivities=sensitivities,
        )
    line = caplog.text
    assert "event=active_speaker.unsegmented_ceiling_bound" in line
    assert "ceiling_db=12.00" in line
    # The ceiling the declared caps alone would have produced, and the gap.
    assert "declared_cap_ceiling_db=-18.80" in line
    assert "headroom_over_declared_caps_db=+30.80" in line
    # Per driver: its cap, what it receives at this ceiling, and the overshoot.
    assert "cap=-30.80" in line and "at_ceiling=0.00" in line
    assert "past_cap=+30.80" in line and "past_cap=+20.00" in line


def test_the_unsegmented_ceiling_refuses_a_non_finite_stimulus_peak():
    profile, targets = _profile_and_targets()
    with pytest.raises(SessionVolumePlanError):
        unsegmented_stimulus_ceiling_db(
            profile, targets.values(), stimulus_peak_dbfs=float("nan")
        )


# --- the per-branch bound (the second conservatism, retired) ----------------
#
# JTS3's declared numbers throughout: woofer admitted at -8.0 dBFS, declared
# sensitivities 108.5 (tweeter) / 83.3 (woofer), and a -14.4 dB L-pad on the
# tweeter recorded in the same declaration.

_JTS3_NAKED_SENS = {"woofer": 83.3, "tweeter": 108.5}
_JTS3_PADDED_SENS = {"woofer": 83.3, "tweeter": 108.5 - 14.4}


def test_pad_folding_moves_the_DISCLOSED_cap_by_exactly_the_pad():
    """The #1665 guard, at the owner it still has.

    The derived HF cap is the woofer's own cap less the two drivers' declared
    sensitivity difference, so folding a -14.4 dB L-pad into the tweeter's
    figure moves that cap by exactly the pad and nothing else. It no longer
    moves the seat-level ceiling — the caps are disclosed there, not binding —
    but it still sets the tweeter's composed level, so reading the naked figure
    instead of the pad-folded one is still the bug #1665 named.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)

    def _tweeter_cap(sensitivities):
        return resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"],
            program_admission=True,
            declared_sensitivities=sensitivities,
        )[1]

    assert _tweeter_cap(_JTS3_NAKED_SENS) == pytest.approx(-33.2)
    assert _tweeter_cap(_JTS3_PADDED_SENS) == pytest.approx(-18.8)
    assert _tweeter_cap(_JTS3_PADDED_SENS) - _tweeter_cap(
        _JTS3_NAKED_SENS
    ) == pytest.approx(14.4)

    # And the ceiling is indifferent to both, because it is headroom now.
    assert unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_NAKED_SENS,
    ) == unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
    )


def test_the_per_branch_bound_is_the_LOUDEST_branch_the_signal_reaches():
    """Each branch against the room it has, not against the full-band peak.

    The full-band bound assumes every branch sees the whole stimulus peak. Told
    what each branch actually gets, the one that clips first is the loudest —
    here the woofer at -18.03, so the ceiling is full scale less that, and the
    quieter tweeter branch does not hold the volume down.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    branch = {targets["woofer"]: -18.03, targets["tweeter"]: -22.66}
    ceiling = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
        branch_peaks_dbfs=branch,
    )
    assert ceiling == pytest.approx(18.03)
    # The move is exactly the full-band peak less the BINDING branch's peak.
    full_band = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
    )
    assert ceiling - full_band == pytest.approx(-12.0 - (-18.03))


def test_a_branch_that_BOOSTS_binds_tighter_than_the_full_band_bound():
    """This is not a one-way loosening, and the direction is not decorative.

    A branch whose chain has net positive gain — a peaking EQ above unity —
    hands back a peak ABOVE the full-band figure, and the same arithmetic then
    derives a QUIETER ceiling than the bound it replaces. A ceiling that only
    ever rose would be a hedge removal; this is a measurement, and it is the
    remaining reason branch peaks are rendered at all.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    full_band = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
    )
    boosted = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
        # The tweeter branch drives 6 dB ABOVE the program's own peak.
        branch_peaks_dbfs={targets["woofer"]: -18.03, targets["tweeter"]: -6.0},
    )
    assert boosted == pytest.approx(6.0)
    assert boosted < full_band


@pytest.mark.parametrize(
    "branch, why",
    [
        ({}, "an empty mapping"),
        ({"woofer-only": -18.0}, "a mapping for a driver that is not active"),
        ({"BOTH": -18.0}, "only one of the two active drivers"),
        ({"BOTH": float("nan"), "OTHER": -22.0}, "a non-finite branch peak"),
        ({"BOTH": None, "OTHER": -22.0}, "a null branch peak"),
        ({"BOTH": True, "OTHER": -22.0}, "a bool where a peak belongs"),
    ],
)
def test_incomplete_branch_facts_fall_back_to_the_conservative_bound(branch, why):
    """Fail-conservative, never fail-open.

    A partial render would bound the speaker by only the branches that happened
    to resolve — which is the fail-OPEN direction, since the loudest branch is
    the binding one and a missing fact would simply stop constraining the
    ceiling. Every incomplete shape must land back on the full-band bound.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    resolved = dict(branch)
    if "BOTH" in resolved:
        resolved[targets["woofer"]] = resolved.pop("BOTH")
    if "OTHER" in resolved:
        resolved[targets["tweeter"]] = resolved.pop("OTHER")
    ceiling = unsegmented_stimulus_ceiling_db(
        profile,
        targets.values(),
        stimulus_peak_dbfs=-12.0,
        declared_sensitivities=_JTS3_PADDED_SENS,
        branch_peaks_dbfs=resolved,
    )
    assert ceiling == pytest.approx(12.0), why


def test_the_full_band_bound_is_full_scale_less_the_stimulus_peak():
    """The bound every caller that renders no branch peaks gets."""
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    assert (
        unsegmented_stimulus_ceiling_db(
            profile, targets.values(), stimulus_peak_dbfs=-12.0
        )
        == 12.0
    )
    assert (
        unsegmented_stimulus_ceiling_db(
            profile, targets.values(), stimulus_peak_dbfs=-12.0, branch_peaks_dbfs=None
        )
        == 12.0
    )


def test_which_bound_was_used_is_logged_with_the_per_driver_numbers(caplog):
    """A silent fallback looks identical to a genuinely tight graph.

    Both outcomes name themselves, and both carry each driver's cap and branch
    peak, so an operator triaging a ``spl_target_unreachable`` can see whether
    the ceiling is the rendered bound or the hedge — and if the hedge, which
    branch had no fact.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    branch = {targets["woofer"]: -18.03, targets["tweeter"]: -22.66}

    with caplog.at_level("INFO", logger="jasper.active_speaker.session_volume_plan"):
        unsegmented_stimulus_ceiling_db(
            profile,
            targets.values(),
            stimulus_peak_dbfs=-12.0,
            declared_sensitivities=_JTS3_PADDED_SENS,
            branch_peaks_dbfs=branch,
        )
    used = caplog.text
    assert "event=active_speaker.unsegmented_ceiling_bound" in used
    assert "bound=per_branch" in used
    assert "cap=-18.80" in used and "branch=-22.66" in used

    caplog.clear()
    with caplog.at_level("INFO", logger="jasper.active_speaker.session_volume_plan"):
        unsegmented_stimulus_ceiling_db(
            profile,
            targets.values(),
            stimulus_peak_dbfs=-12.0,
            declared_sensitivities=_JTS3_PADDED_SENS,
            branch_peaks_dbfs={targets["woofer"]: -18.03},
        )
    fell_back = caplog.text
    assert "bound=full_band" in fell_back
    assert "reason=branch_peaks_incomplete" in fell_back
    # An incomplete render is disclosed as incomplete: the branch fact that DID
    # resolve is not quoted as though it had bounded anything.
    assert "branch=none" in fell_back
    assert "branch=-18.03" not in fell_back


def test_the_session_measurement_volume_is_untouched_by_branch_facts():
    """A different contract, deliberately left alone.

    ``session_measurement_volume_db`` derives from ``max(caps)`` because a
    composed v2 program attenuates every other driver down to its own cap with
    per-segment gains. It takes no branch peaks and none of this changes it.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    assert (
        session_measurement_volume_db(
            profile, targets.values(), declared_sensitivities=_JTS3_PADDED_SENS
        )
        == -20.0
    )
    assert loudest_driver_cap_dbfs(
        profile, targets.values(), declared_sensitivities=_JTS3_PADDED_SENS
    ) == pytest.approx(-8.0)


def test_session_measurement_volume_unaffected_by_hf_ceiling_derivation():
    """W6.5 pin: this module exclusively serves the program-admission v2
    conductor, so it always resolves ceilings on the proven-HP path. With
    JTS3's DECLARED sensitivities threaded through and the tweeter at its -65
    seed, the tweeter's OWN resolved cap moves from -65 to -33.2 (derived: the
    woofer's -8 less the 25.2 dB sensitivity delta) -- but ``max(caps)`` is
    still the woofer's -8, so the derived session volume is unchanged. No
    behavior change expected; this pins that.
    """
    profile, targets = _profile_and_targets(woofer_peak=-8.0, tweeter_peak=-65.0)
    assert (
        session_measurement_volume_db(
            profile,
            targets.values(),
            declared_sensitivities={"woofer": 83.3, "tweeter": 108.5},
        )
        == -20.0
    )


def test_session_measurement_volume_refuses_unmeasurable_profile():
    # Every cap at or below the -60 dB emergency floor: no driver can be
    # measured at a safe volume -> typed refusal, never a zero-SNR session.
    # (This invariant would have caught the inverted min(caps) derivation.)
    profile, targets = _profile_and_targets(woofer_peak=-65.0, tweeter_peak=-70.0)
    with pytest.raises(
        SessionVolumePlanError, match="profile_unmeasurable_at_safe_volume"
    ):
        session_measurement_volume_db(profile, targets.values())


def test_session_measurement_volume_requires_targets():
    profile, _ = _profile_and_targets()
    with pytest.raises(SessionVolumePlanError):
        session_measurement_volume_db(profile, [])


# --- latch: intent before mutation ------------------------------------------


def test_open_writes_active_intent_before_first_mutation(tmp_path):
    p = tmp_path / "sv.json"
    statuses_seen: list[str] = []

    def _record_status():
        statuses_seen.append(json.loads(p.read_text())["status"])

    vol = FakeVolume(initial=-6.0, on_set=_record_status)
    plan = SessionVolumePlan(state_path=p)

    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.OPENED
    # The durable state was already 'active' at the moment of the first set.
    assert statuses_seen and statuses_seen[0] == "active"
    on_disk = json.loads(p.read_text())
    assert on_disk["status"] == "active"
    assert "opened_at" in on_disk
    assert plan.measurement_volume_db == -12.0
    plan.assert_ready()


# --- latch: restore-once idempotence ----------------------------------------


def test_restore_is_exact_and_once():
    vol = FakeVolume(initial=-6.0)
    plan = SessionVolumePlan()
    assert asyncio.run(plan.open(-12.0, vol.set, vol.get)) is SessionVolumeOpenResult.OPENED
    assert vol.value == -12.0
    first = asyncio.run(plan.close(vol.set, vol.get))
    assert first is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0  # original restored
    set_calls = sum(1 for e in vol.order if e[0] == "set")
    again = asyncio.run(plan.close(vol.set, vol.get))
    assert again is SessionVolumeRestoreResult.ALREADY_RESOLVED
    # Idempotent: a second close performs no further volume mutation.
    assert sum(1 for e in vol.order if e[0] == "set") == set_calls
    assert plan.unresolved_volume_safety is None


# --- latch: readback-confirm failure ----------------------------------------


def test_open_confirm_failure_falls_back_to_emergency():
    # Neither the measurement volume nor the original confirms; emergency does.
    vol = FakeVolume(initial=-6.0, confirm_targets={-60.0})
    plan = SessionVolumePlan()
    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.EMERGENCY_ATTENUATED
    assert vol.value == -60.0  # emergency floor
    # Emergency confirmed => resolved (no lingering unresolved risk).
    assert plan.unresolved_volume_safety is None


def test_open_confirm_failure_no_fallback_latches_unresolved(tmp_path):
    # Nothing confirms -> measurement, exact, AND emergency all fail.
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan = SessionVolumePlan(state_path=p)
    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.FAILED
    unresolved = plan.unresolved_volume_safety
    assert unresolved is not None
    assert unresolved["emergency_volume_db"] == -60.0
    assert json.loads(p.read_text())["status"] == "unresolved"


# --- ceiling force-drain (live + hydration) ---------------------------------


def test_wall_clock_ceiling_force_drains_stale_active(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=10.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))
    assert vol.value == -12.0

    # A fresh process hydrates the durable state well past the ceiling.
    later = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=10.0, clock=lambda: 5000.0)
    assert later.stale_active() is True
    with pytest.raises(SessionVolumePlanError):
        later.assert_ready()
    drained = asyncio.run(later.enforce_ceiling(vol.set, vol.get))
    assert drained is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0
    assert json.loads(p.read_text())["status"] == "resolved"


def test_set_wall_clock_ceiling_stamps_the_next_open_and_stays_bounded(tmp_path):
    """The ceiling is a property of the measurement about to run — a full
    crossover-cloud commission legitimately outlasts the 3-entry flow's
    default — so the caller that built the plan sets it, and the OPEN
    session's own recorded value is what ``stale_active`` reads.

    Fail-closed both ways: a nonsense value is refused rather than silently
    disabling the walked-away guarantee, and no caller can stretch the ceiling
    past ``MAX_WALL_CLOCK_CEILING_S``.
    """
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    plan = SessionVolumePlan(state_path=p, clock=lambda: 1000.0)
    assert plan.wall_clock_ceiling_s == DEFAULT_WALL_CLOCK_CEILING_S

    plan.set_wall_clock_ceiling_s(3360.0)
    asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert json.loads(p.read_text())["wall_clock_ceiling_s"] == 3360.0
    # A session that would have been stale under the 1800 s default is not
    # stale under the ceiling this plan actually opened with...
    fresh = SessionVolumePlan(state_path=p, clock=lambda: 1000.0 + 3000.0)
    assert fresh.stale_active() is False
    # ...and still retires once its own ceiling passes.
    late = SessionVolumePlan(state_path=p, clock=lambda: 1000.0 + 3400.0)
    assert late.stale_active() is True

    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(SessionVolumePlanError):
            plan.set_wall_clock_ceiling_s(bad)
    plan.set_wall_clock_ceiling_s(10 * MAX_WALL_CLOCK_CEILING_S)
    assert plan.wall_clock_ceiling_s == MAX_WALL_CLOCK_CEILING_S


def test_enforce_ceiling_noop_when_fresh(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))
    fresh = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1001.0)
    assert fresh.stale_active() is False
    assert asyncio.run(fresh.enforce_ceiling(vol.set, vol.get)) is None
    assert vol.value == -12.0  # untouched


# --- crash hydration is fail-closed -----------------------------------------


def test_crash_hydrated_active_is_not_ready_until_recovered(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))

    # Simulate a restart: new instance hydrates the SAME durable active state,
    # still within the ceiling. The status is NOT flipped to unresolved...
    reborn = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0)
    assert reborn.unresolved_volume_safety is None
    assert reborn.stale_active() is False
    # ...but the volume is not owned by this process, so it is not usable.
    with pytest.raises(SessionVolumePlanError):
        reborn.assert_ready()
    # recover_unresolved drains it (unlike the lease, this does not refuse active).
    recovered = asyncio.run(reborn.recover_unresolved(vol.set, vol.get))
    assert recovered is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0


def test_needs_recovery_true_for_unresolved_and_foreign_active(tmp_path):
    # Branch 1: latched unresolved -> needs_recovery (and surfaced payload).
    p1 = tmp_path / "sv1.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan1 = SessionVolumePlan(state_path=p1)
    asyncio.run(plan1.open(-12.0, vol.set, vol.get))  # nothing confirms
    assert plan1.unresolved_volume_safety is not None
    assert plan1.needs_recovery is True

    # Branch 2: crash-hydrated active within the ceiling -> needs_recovery is
    # the ONLY surfaced signal (unresolved_volume_safety stays None).
    p2 = tmp_path / "sv2.json"
    vol2 = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0
    )
    asyncio.run(opener.open(-12.0, vol2.set, vol2.get))
    assert opener.needs_recovery is False  # owned by this process
    reborn = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0
    )
    assert reborn.unresolved_volume_safety is None
    assert reborn.needs_recovery is True

    # Draining resolves both signals.
    asyncio.run(reborn.recover_unresolved(vol2.set, vol2.get))
    assert reborn.needs_recovery is False

    # No state at all -> nothing to recover.
    assert SessionVolumePlan().needs_recovery is False


def test_hydrated_malformed_state_is_unresolved(tmp_path):
    p = tmp_path / "sv.json"
    p.write_text("{ not valid json")
    plan = SessionVolumePlan(state_path=p)
    assert plan.unresolved_volume_safety is not None
    with pytest.raises(SessionVolumePlanError):
        plan.assert_ready()


def test_open_refuses_over_unresolved_state(tmp_path):
    p = tmp_path / "sv.json"
    p.write_text("garbage")
    vol = FakeVolume()
    plan = SessionVolumePlan(state_path=p)
    with pytest.raises(SessionVolumePlanError, match="recover it"):
        asyncio.run(plan.open(-12.0, vol.set, vol.get))
