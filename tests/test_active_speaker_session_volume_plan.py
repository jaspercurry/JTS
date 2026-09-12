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

pytestmark = pytest.mark.usefixtures("banked_session_level")

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
    FaderVolumeDoor,
    RestoreOutcome,
    MAX_WALL_CLOCK_CEILING_S,
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    SessionVolumeRestoreResult,
    loudest_driver_cap_dbfs,
    session_measurement_volume_db,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests._log_events import event_fields


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
                # ``None`` omits the key: since 2026-08-23 that is the ordinary
                # shape, and it means "this maker publishes no level limit".
                **({} if peak is None else {"max_effective_peak_dbfs": peak}),
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


@pytest.mark.parametrize(
    ("woofer_peak", "expected_woofer_cap", "expected_tweeter_cap", "anchor"),
    [
        (-20.0, -20.0, -30.8, "declared"),
        (None, 0.0, -10.8, "undeclared"),
    ],
    ids=["woofer-declares-a-limit", "woofer-declares-none"],
)
def test_the_hf_ceiling_moves_with_its_ANCHOR_contract_shape(
    caplog, woofer_peak, expected_woofer_cap, expected_tweeter_cap, anchor
):
    """The derived tweeter cap is a delta from the WOOFER's own cap.

    So it moves with the woofer's contract shape, and by 20 dB on this
    fixture. A woofer that declares -20 anchors the derivation there; one that
    declares nothing anchors it at the woofer class default, which for a
    low-frequency role IS full scale. Both are correct answers to different
    declarations under the 2026-08-23 ruling -- there is no refusal here, and
    the mic-measured commissioning stop still bounds physical output -- but a
    20 dB shift on a compression driver must be a NAMED fact rather than an
    emergent one, so the supersede line carries the anchor it used.

    Mutation guard: stop passing the anchor through ``_derived_hf_ceiling_dbfs``
    and the ``anchor=``/``anchor_cap_dbfs=`` fields vanish from the receipt.
    """
    profile, targets = _profile_and_targets(
        woofer_peak=woofer_peak, tweeter_peak=-65.0
    )
    sensitivities = {"woofer": 90.0, "tweeter": 100.8}

    def _cap(role):
        return resolve_driver_excitation_ceilings(
            profile,
            targets[role],
            program_admission=True,
            declared_sensitivities=sensitivities,
        )[1]

    with caplog.at_level(
        "INFO", logger="jasper.active_speaker.excitation_safety_plan"
    ):
        assert _cap("woofer") == pytest.approx(expected_woofer_cap)
        assert _cap("tweeter") == pytest.approx(expected_tweeter_cap)
    fields = event_fields(caplog, "active_speaker.excitation_ceiling_superseded")
    assert fields["anchor"] == anchor
    assert fields["anchor_cap_dbfs"] == f"{expected_woofer_cap:.1f}"
    # The shift is exactly the anchor's own shift, and nothing else.
    assert expected_tweeter_cap - expected_woofer_cap == pytest.approx(-10.8)


# --- the per-branch bound (the second conservatism, retired) ----------------
#
# JTS3's declared numbers throughout: woofer admitted at -8.0 dBFS, declared
# sensitivities 108.5 (tweeter) / 83.3 (woofer), and a -14.4 dB L-pad on the
# tweeter recorded in the same declaration.

_JTS3_NAKED_SENS = {"woofer": 83.3, "tweeter": 108.5}
_JTS3_PADDED_SENS = {"woofer": 83.3, "tweeter": 108.5 - 14.4}


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

    result = asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
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
    assert asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get))) is SessionVolumeOpenResult.OPENED
    assert vol.value == -12.0
    first = asyncio.run(plan.close(FaderVolumeDoor(vol.set, vol.get)))
    assert first is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0  # original restored
    set_calls = sum(1 for e in vol.order if e[0] == "set")
    again = asyncio.run(plan.close(FaderVolumeDoor(vol.set, vol.get)))
    assert again is SessionVolumeRestoreResult.ALREADY_RESOLVED
    # Idempotent: a second close performs no further volume mutation.
    assert sum(1 for e in vol.order if e[0] == "set") == set_calls
    assert plan.unresolved_volume_safety is None


# --- latch: readback-confirm failure ----------------------------------------


def test_open_confirm_failure_falls_back_to_emergency():
    # Neither the measurement volume nor the original confirms; emergency does.
    vol = FakeVolume(initial=-6.0, confirm_targets={-60.0})
    plan = SessionVolumePlan()
    result = asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
    assert result is SessionVolumeOpenResult.EMERGENCY_ATTENUATED
    assert vol.value == -60.0  # emergency floor
    # Emergency confirmed => resolved (no lingering unresolved risk).
    assert plan.unresolved_volume_safety is None


def test_open_confirm_failure_no_fallback_latches_unresolved(tmp_path):
    # Nothing confirms -> measurement, exact, AND emergency all fail.
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan = SessionVolumePlan(state_path=p)
    result = asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
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
    asyncio.run(opener.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
    assert vol.value == -12.0

    # A fresh process hydrates the durable state well past the ceiling.
    later = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=10.0, clock=lambda: 5000.0)
    assert later.stale_active() is True
    with pytest.raises(SessionVolumePlanError):
        later.assert_ready()
    drained = asyncio.run(later.enforce_ceiling(FaderVolumeDoor(vol.set, vol.get)))
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
    asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
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
    asyncio.run(opener.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))
    fresh = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1001.0)
    assert fresh.stale_active() is False
    assert asyncio.run(fresh.enforce_ceiling(FaderVolumeDoor(vol.set, vol.get))) is None
    assert vol.value == -12.0  # untouched


# --- crash hydration is fail-closed -----------------------------------------


def test_crash_hydrated_active_is_not_ready_until_recovered(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))

    # Simulate a restart: new instance hydrates the SAME durable active state,
    # still within the ceiling. The status is NOT flipped to unresolved...
    reborn = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0)
    assert reborn.unresolved_volume_safety is None
    assert reborn.stale_active() is False
    # ...but the volume is not owned by this process, so it is not usable.
    with pytest.raises(SessionVolumePlanError):
        reborn.assert_ready()
    # recover_unresolved drains it (unlike the lease, this does not refuse active).
    recovered = asyncio.run(reborn.recover_unresolved(FaderVolumeDoor(vol.set, vol.get)))
    assert recovered is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0


def test_needs_recovery_true_for_unresolved_and_foreign_active(tmp_path):
    # Branch 1: latched unresolved -> needs_recovery (and surfaced payload).
    p1 = tmp_path / "sv1.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan1 = SessionVolumePlan(state_path=p1)
    asyncio.run(plan1.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))  # nothing confirms
    assert plan1.unresolved_volume_safety is not None
    assert plan1.needs_recovery is True

    # Branch 2: crash-hydrated active within the ceiling -> needs_recovery is
    # the ONLY surfaced signal (unresolved_volume_safety stays None).
    p2 = tmp_path / "sv2.json"
    vol2 = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0
    )
    asyncio.run(opener.open(-12.0, FaderVolumeDoor(vol2.set, vol2.get)))
    assert opener.needs_recovery is False  # owned by this process
    reborn = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0
    )
    assert reborn.unresolved_volume_safety is None
    assert reborn.needs_recovery is True

    # Draining resolves both signals.
    asyncio.run(reborn.recover_unresolved(FaderVolumeDoor(vol2.set, vol2.get)))
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
        asyncio.run(plan.open(-12.0, FaderVolumeDoor(vol.set, vol.get)))


# --- the injected door -------------------------------------------------------
#
# What W5-c0 adds is the SHAPE the plan reaches the fader through, so what is
# pinned here is that shape's one contract: every verb answers about the FADER,
# not about a write having been attempted.


#: EVERY ``VolumeDoor`` binding this repo ships, as ``(id, factory)``. The pins
#: below take this axis so they are the DOOR's gate rather than
#: ``FaderVolumeDoor``'s — W5-c1 adds its owner-backed door as one entry here
#: and inherits all three as a real gate.
#:
#: **This axis is the whole point, and the first version of these pins did not
#: have it.** They parametrized over the two verbs but hard-coded the one
#: implementation, so an owner-backed door would have run zero of them. The
#: hazard that leaves open is specific and reproducible:
#: ``VolumeOwner.declare_household_level_db`` returns ``True`` for a legitimate
#: DEFERRAL to a higher-ranked claim — the fader is not written and stays where
#: it was. A door that passed that ``True`` through would make ``plan.close``
#: report ``EXACT_RESTORED`` and clear the durable intent over a speaker still
#: sitting at measurement level, which is exactly what ``VolumeDoor``'s
#: docstring forbids and what the walked-away guarantee exists to prevent.
#:
#: So a door added here must answer for the FADER, not for the owner's intent:
#: a deferral is ``False``, because the level is not in effect.
def _owner_door(vol):
    """The wizard's door: one owner, one claim, a PHYSICAL read.

    The claim is real, so ``establish`` writes and confirms through
    ``acquire_level``; ``restore`` declares and then re-reads, so a deferral
    that wrote nothing answers ``False``.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
        OwnerVolumeDoor,
    )
    from jasper.volume_owner import VolumeOwner

    owner = VolumeOwner(set_fader_db=vol.set, get_fader_db=vol.get)
    return OwnerVolumeDoor(
        owner, read_fader=vol.get, claim=MeasurementVolumeClaim(owner),
    )


DOOR_FACTORIES = [
    pytest.param(
        lambda vol: FaderVolumeDoor(vol.set, vol.get), id="fader",
    ),
    pytest.param(_owner_door, id="owner"),
]


@pytest.mark.parametrize("door_factory", DOOR_FACTORIES)
def test_establish_is_true_only_when_the_fader_confirms(door_factory):
    """The open verb answers readback, never "the setter was called".

    A door that answered ``True`` for an unconfirmed write would let a session
    be admitted against a level the speaker is not playing at.
    """
    confirms = FakeVolume(initial=-6.0)
    drifts = FakeVolume(initial=-6.0, confirm_targets=set())
    establish = "establish_measurement_level_db"
    assert asyncio.run(getattr(door_factory(confirms), establish)(-12.0)) is True
    assert asyncio.run(getattr(door_factory(drifts), establish)(-12.0)) is False


@pytest.mark.parametrize("door_factory", DOOR_FACTORIES)
def test_restore_lands_or_fails_but_never_reports_an_unwritten_level(
    door_factory,
):
    """The drain verb answers readback too — and has a THIRD answer.

    ``LANDED`` only when the fader carries the level; ``FAILED`` when the write
    did not confirm. The third, ``DEFERRED``, is not reachable through a door
    holding no competing claim, and is pinned where it lives — against a real
    live claim, in the volume-claim suite. What matters here is that neither of
    these two ever reports a level the fader does not carry: the exact→emergency
    ladder and its durable latch are built on that.
    """
    confirms = FakeVolume(initial=-6.0)
    drifts = FakeVolume(initial=-6.0, confirm_targets=set())
    restore = "restore_household_level_db"
    assert asyncio.run(
        getattr(door_factory(confirms), restore)(-12.0)
    ) is RestoreOutcome.LANDED
    assert asyncio.run(
        getattr(door_factory(drifts), restore)(-12.0)
    ) is RestoreOutcome.FAILED


@pytest.mark.parametrize("door_factory", DOOR_FACTORIES)
def test_the_door_reads_the_household_level_the_fader_actually_carries(
    door_factory,
):
    """The snapshot is a reading, not a declaration.

    ``open`` persists this number as ``original_main_volume_db`` before its
    first mutation, and every drain restores toward it. A door that answered
    with a level some owner DECLARES rather than one the fader carries would
    snapshot the intent instead of the state — and the crash this write exists
    to survive is precisely the one where those two disagree.
    """
    vol = FakeVolume(initial=-6.0)
    assert asyncio.run(door_factory(vol).read_household_level_db()) == -6.0


@pytest.mark.parametrize("door_factory", DOOR_FACTORIES)
def test_a_door_that_cannot_confirm_either_candidate_latches_unresolved(
    tmp_path, door_factory,
):
    """The ladder rides the door: exact, then emergency, then the latch.

    Drives all three rungs through the door rather than through the raw
    callables, which is the coupling W5-c1 changes — a pin taken one level
    below the door could not see a binding that stopped falling through.
    """
    opener = FakeVolume(initial=-6.0, confirm_targets={-12.0})
    plan = SessionVolumePlan(state_path=tmp_path / "sv.json")
    assert asyncio.run(
        plan.open(-12.0, door_factory(opener))
    ) is SessionVolumeOpenResult.OPENED
    stuck = FakeVolume(initial=-12.0, confirm_targets=set())
    assert asyncio.run(
        plan.close(door_factory(stuck))
    ) is SessionVolumeRestoreResult.FAILED
    assert [t for verb, t in stuck.order if verb == "set"] == [-6.0, -60.0]
    assert plan.unresolved_volume_safety is not None
