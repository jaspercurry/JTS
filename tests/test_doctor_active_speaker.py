# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor active-speaker domain.

Every assertion pins ``status`` and ``reason`` — never ``detail`` prose
(ADR-0233 rule 3). ``active_speaker.REASON_*`` is the closed vocabulary.
"""

import json
from pathlib import Path

import pytest

import jasper.active_speaker._common as _common
import jasper.active_speaker.setup_status as setup_status_mod
from jasper.cli.doctor import active_speaker
from jasper.cli.doctor._evidence import evidence

from .test_doctor_audio import _point_at_config


# ------------------------------------------------- active speaker runtime graph


def _save_topology(monkeypatch, tmp_path, topology):
    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    return topology_path


def _point_at_topology_and_config(monkeypatch, tmp_path, topology, config_text, name):
    _save_topology(monkeypatch, tmp_path, topology)
    return _point_at_config(monkeypatch, tmp_path, config_text, name=name)


def _blocker_bearing_roleful_topology():
    """A roleful topology whose tweeter has no DAC output assigned."""
    from dataclasses import replace

    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    return replace(
        topology,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if channel.role == "tweeter"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )


def _incomplete_passive_topology():
    """A passive stereo layout whose right channels have no DAC output."""
    from dataclasses import replace

    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    complete = _full_range_stereo()
    return replace(
        complete,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if group.kind == "right"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in complete.speaker_groups
        ),
    )


def _stage_parked(monkeypatch, tmp_path, topology):
    """Point the box at the independently PROVED parked graph for ``topology``."""
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph

    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed, "fixture must stage a proved parked graph"
    _point_at_topology_and_config(
        monkeypatch, tmp_path, topology, text, "active_speaker_parked.yml",
    )


def _stage_corrupt_topology(monkeypatch, tmp_path):
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))


def _stage_complete_passive_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    _save_topology(monkeypatch, tmp_path, _full_range_stereo())


def _stage_unreadable_statefile(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _save_topology(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "gone-statefile.yml"))


def _stage_missing_config(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _save_topology(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))


def _stage_unconfigured_parked(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _topology

    _stage_parked(monkeypatch, tmp_path, _topology([]))


def _stage_roleful_parked(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _stage_parked(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))


def _stage_blocker_bearing_parked(monkeypatch, tmp_path):
    _stage_parked(monkeypatch, tmp_path, _blocker_bearing_roleful_topology())


def _stage_incomplete_passive_parked(monkeypatch, tmp_path):
    _stage_parked(monkeypatch, tmp_path, _incomplete_passive_topology())


def _stage_flat_graph_without_a_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _flat_yaml, _topology

    _point_at_topology_and_config(
        monkeypatch, tmp_path, _topology([]), _flat_yaml(), "outputd-cutover.yml",
    )


def _stage_flat_graph_on_a_tweeter_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology, _flat_yaml

    _point_at_topology_and_config(
        monkeypatch,
        tmp_path,
        _active_topology("mono", "active_2_way"),
        _flat_yaml(),
        "outputd-cutover.yml",
    )


def _stage_staged_active_startup(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    config = _point_at_topology_and_config(
        monkeypatch,
        tmp_path,
        topology,
        _active_yaml("mono", 2, frozenset()),
        "active_speaker_staged_startup.yml",
    )
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(json.dumps(_staged_metadata(topology, config)), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))


@pytest.mark.parametrize(
    "stage, status, reason, silent",
    [
        (_stage_corrupt_topology, "fail", active_speaker.REASON_TOPOLOGY_UNREADABLE, False),
        (
            _stage_complete_passive_layout, "ok",
            active_speaker.REASON_GRAPH_PASSIVE_LAYOUT, False,
        ),
        (
            _stage_unreadable_statefile, "fail",
            active_speaker.REASON_CAMILLA_STATEFILE_UNREADABLE, False,
        ),
        (_stage_missing_config, "fail", active_speaker.REASON_CAMILLA_CONFIG_MISSING, False),
        (
            _stage_unconfigured_parked, "warn",
            active_speaker.REASON_GRAPH_PARKED_SILENT, True,
        ),
        (_stage_roleful_parked, "warn", active_speaker.REASON_GRAPH_PARKED_SILENT, True),
        (
            _stage_blocker_bearing_parked, "warn",
            active_speaker.REASON_GRAPH_PARKED_SILENT, True,
        ),
        # Parked over an unfixable layout: a `fail` that is also provably
        # silent, which the summary line supports (#2471).
        (
            _stage_incomplete_passive_parked, "fail",
            active_speaker.REASON_GRAPH_LAYOUT_INCOMPLETE, True,
        ),
        (
            _stage_flat_graph_without_a_layout, "fail",
            active_speaker.REASON_GRAPH_UNSAFE, False,
        ),
        (
            _stage_flat_graph_on_a_tweeter_layout, "fail",
            active_speaker.REASON_GRAPH_UNSAFE, False,
        ),
        (_stage_staged_active_startup, "ok", "", False),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_active_speaker_runtime_graph_branches(
    monkeypatch, tmp_path, stage, status, reason, silent
):
    """One row per outcome of the merged check.

    ``speaker_silent`` rides along because only the parked branch may claim it
    (#2471): an unreadable topology is proof of not knowing, and a legal graph
    is not silence.
    """
    stage(monkeypatch, tmp_path)

    r = active_speaker.check_active_speaker_runtime_graph()

    assert (r.status, r.reason, r.speaker_silent) == (status, reason, silent)


def test_active_speaker_runtime_graph_names_the_blockers_it_is_parked_over(
    monkeypatch, tmp_path
):
    """Parking is gated on the missing startup graph, not on the blockers, so
    the parked row still has to name the layout blockers a household must clear.
    Asserted on the contract's own blocker CODES, not on the sentence."""
    from jasper.active_speaker.runtime_contract import classify_output_contract

    topology = _blocker_bearing_roleful_topology()
    _stage_parked(monkeypatch, tmp_path, topology)
    codes = [str(issue["code"]) for issue in classify_output_contract(topology).issues]
    assert codes, "fixture must carry at least one topology blocker"

    r = active_speaker.check_active_speaker_runtime_graph()

    assert r.reason == active_speaker.REASON_GRAPH_PARKED_SILENT
    assert all(code in r.detail for code in codes)


def test_active_speaker_runtime_graph_exits_are_capability_aware(monkeypatch, tmp_path):
    """The doctor detail must resolve the exits through the OWNED helper.

    The one deliberate `detail` assertion in this file: which of two equal-status
    strings the line carries is the whole finding, and no structured field can
    express it. On an ordinary DAC `parked_muted_exits(topology) ==
    PARKED_MUTED_EXITS`, so only a DAC that declares NO active outputd lane
    separates them — there "finish crossover preview" can never succeed, so the
    helper drops it and the bare constant still offers it.
    """
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        parked_muted_exits,
    )
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
    from tests.active_speaker_fixtures import register_passive_only_dac

    profile = register_passive_only_dac(monkeypatch)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": profile.id,
            "device_label": profile.label,
            "physical_output_count": profile.physical_output_count,
        },
        "speaker_groups": [{
            "id": "mono",
            "label": "Mono",
            "kind": "mono",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    # Unassigned -> the topology-level blocker this row names.
                    "role": "tweeter",
                    "physical_output_index": None,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        }],
        "routing": {"mono_group_id": "mono"},
    })
    # The fixture is only meaningful if the helper and the constant DISAGREE.
    assert parked_muted_exits(topology) != PARKED_MUTED_EXITS
    _stage_parked(monkeypatch, tmp_path, topology)

    r = active_speaker.check_active_speaker_runtime_graph()

    assert r.reason == active_speaker.REASON_GRAPH_PARKED_SILENT
    # Follows the helper, and therefore does NOT offer the impossible action.
    assert parked_muted_exits(topology) in r.detail
    assert PARKED_MUTED_EXITS not in r.detail


# ------------------------------------------------------------- sound profile


def test_check_sound_profile_reports_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))

    r = active_speaker.check_sound_profile()

    assert r.status == "ok"
    assert r.reason == active_speaker.REASON_SOUND_PROFILE_DEFAULT


_SAVED_SOUND_PROFILE = {
    "enabled": True,
    "curve_id": "harman",
    "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
}


def test_check_sound_profile_warns_when_saved_profile_not_active(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps(_SAVED_SOUND_PROFILE))
    _point_at_config(monkeypatch, tmp_path, "# base\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = active_speaker.check_sound_profile()

    assert r.status == "warn"
    assert r.reason == active_speaker.REASON_SOUND_PROFILE_NOT_ACTIVE


@pytest.mark.parametrize(
    "active_name",
    [
        # The four shapes the check's old literal set did not know about.
        "sound_reset_s1_1717000000.yml",
        "sound_snapshot_s1_1717000000.yml",
        "sound_lean_current.yml",
        "grouping_leader.yml",
    ],
)
def test_check_sound_profile_accepts_every_jts_generated_active_name(
    monkeypatch, tmp_path, active_name,
):
    """One owner decides what "JTS-generated" means, and the doctor asks it.

    `check_sound_profile` held a second, narrower copy of that predicate as a
    literal set and had fallen four shapes behind
    :func:`jasper.sound.camilla_yaml.is_jts_generated_config`, so it told a
    household its saved profile was not reflected in a graph that
    demonstrably carries it. Permanent since #2572: the reconcile now leaves a
    content-identical graph running under whatever it is named.

    The negative half is
    `test_check_sound_profile_warns_when_saved_profile_not_active` above.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile, build_sound_filters

    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps(_SAVED_SOUND_PROFILE))

    # The active graph genuinely carries the saved preference EQ.
    saved = SoundProfile.from_mapping(_SAVED_SOUND_PROFILE)
    assert build_sound_filters(saved)
    _point_at_config(
        monkeypatch,
        tmp_path,
        emit_sound_config(saved, profile_id="t"),
        name=active_name,
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = active_speaker.check_sound_profile()

    assert r.status == "ok"
    assert r.reason == ""


def test_check_sound_profile_fails_on_corrupt_json(monkeypatch, tmp_path):
    profile = tmp_path / "sound_profile.json"
    profile.write_text("{not json")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = active_speaker.check_sound_profile()

    assert r.status == "fail"
    assert r.reason == active_speaker.REASON_SOUND_PROFILE_UNREADABLE


# ----------------------------------------------------------- DSP apply state


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (None, "ok", active_speaker.REASON_DSP_APPLY_NONE),
        (
            {
                "op_id": "abcdef123456",
                "source": "sound",
                "phase": "done",
                "result": "success",
                "candidate_config_path":
                    "/var/lib/camilladsp/configs/sound_current.yml",
            },
            "ok", "",
        ),
        (
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed_rollback_failed",
                "rollback_attempted": True,
                "rollback_succeeded": False,
            },
            "fail", active_speaker.REASON_DSP_APPLY_ROLLBACK_FAILED,
        ),
        (
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed",
                "rollback_attempted": True,
                "rollback_succeeded": True,
            },
            "warn", active_speaker.REASON_DSP_APPLY_UNSUCCESSFUL,
        ),
    ],
    ids=["none-recorded", "success", "rollback-failed", "rolled-back"],
)
def test_check_dsp_apply_state_verdicts(monkeypatch, tmp_path, state, status, reason):
    path = tmp_path / "dsp_apply_state.json"
    if state is not None:
        path.write_text(json.dumps(state))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(path))

    r = active_speaker.check_dsp_apply_state()

    assert r.status == status
    assert r.reason == reason


# --- #1666: canonical active-speaker baseline durability -------------------


def _point_at_baseline(monkeypatch, *, live, canonical, statefile):
    if live is not None:
        statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
        monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    else:
        monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))


def test_active_speaker_baseline_canonical_ok_when_live_is_canonical(
    monkeypatch,
    tmp_path,
):
    canonical = tmp_path / "active_speaker_baseline.yml"
    canonical.write_text("# whatever is currently loaded\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=canonical,
        canonical=canonical,
        statefile=tmp_path / "statefile.yml",
    )

    r = active_speaker.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == ""


@pytest.mark.parametrize("live_name", [None, "outputd-cutover.yml"])
def test_active_speaker_baseline_canonical_ok_when_not_applicable(
    monkeypatch,
    tmp_path,
    live_name,
):
    """A live config that is not a baseline candidate at all (a plain
    stereo/flat topology's own generated config), and a statefile that cannot
    be read, are both outside this check's scope — never a false warning on the
    majority of the fleet that has no active baseline commissioned. A missing
    statefile is already a real failure at
    `check_active_speaker_runtime_graph`, which owns it."""
    live = None
    if live_name is not None:
        live = tmp_path / live_name
        live.write_text("devices:\n  samplerate: 48000\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=live,
        canonical=tmp_path / "active_speaker_baseline.yml",
        statefile=tmp_path / "statefile.yml",
    )

    r = active_speaker.check_active_speaker_baseline_canonical()

    assert r.status == "skipped"
    assert r.reason == active_speaker.REASON_BASELINE_CANONICAL_NOT_APPLICABLE


async def test_active_speaker_baseline_canonical_ok_when_content_matches_live(
    monkeypatch,
    tmp_path,
):
    """The common case: every successful apply's post-success promote keeps
    canonical's content equal to whatever candidate is actually live."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        _prior_payload,
        run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    _point_at_baseline(
        monkeypatch,
        live=Path(run8_payload["profile"]["config"]["path"]),
        canonical=config_path,
        statefile=tmp_path / "statefile.yml",
    )

    r = active_speaker.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == ""


async def test_active_speaker_baseline_canonical_discloses_divergence(
    monkeypatch,
    tmp_path,
):
    """Canonical holds run-8's promoted bytes; simulating CamillaDSP still
    running the PRIOR candidate is a genuine content mismatch — disclosed, not
    warned: the live graph is the audible truth, only the copy is stale."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        prior_payload,
        _run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    _point_at_baseline(
        monkeypatch,
        live=Path(prior_payload["profile"]["config"]["path"]),
        canonical=config_path,
        statefile=tmp_path / "statefile.yml",
    )

    r = active_speaker.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == active_speaker.REASON_BASELINE_CANONICAL_STALE


def test_active_speaker_baseline_canonical_discloses_missing_canonical(
    monkeypatch,
    tmp_path,
):
    """A live applied-candidate sibling with no canonical file yet (a box whose
    last promote never ran) is disclosed, not warned — the next apply or
    restore re-promotes it."""
    live = tmp_path / "active_speaker_baseline_candidate_abc123def456.yml"
    live.write_text("# a real applied candidate, never promoted\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=live,
        canonical=tmp_path / "active_speaker_baseline.yml",
        statefile=tmp_path / "statefile.yml",
    )

    r = active_speaker.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == active_speaker.REASON_BASELINE_CANONICAL_MISSING


# ------------------------------------------------- active speaker startup hold


def test_active_speaker_startup_hold_ok_when_no_hold_is_in_flight():
    # The conftest fixture points the marker at a per-test path that starts
    # absent, which is the no-hold baseline.
    r = active_speaker.check_active_speaker_startup_hold()

    assert r.status == "ok"
    assert r.reason == active_speaker.REASON_STARTUP_HOLD_NONE


@pytest.mark.parametrize(
    "load_status, live_is_anchor, status, reason, silent",
    [
        ("loaded", True, "ok", active_speaker.REASON_STARTUP_HOLD_IN_FLIGHT, False),
        # A hold with no load behind it keeps a box that is STILL on the anchor
        # silent across every reconcile.
        ("rolled_back", True, "fail", active_speaker.REASON_STARTUP_HOLD_STALE, True),
        # Off the anchor the box plays: the selector rung the marker feeds also
        # requires the anchor graph, and /run empties before the next boot.
        ("rolled_back", False, "warn", active_speaker.REASON_STARTUP_HOLD_STALE, False),
    ],
    ids=["in-flight", "stale-on-anchor", "stale-but-playing"],
)
def test_active_speaker_startup_hold_verdicts(
    monkeypatch, tmp_path, load_status, live_is_anchor, status, reason, silent
):
    from jasper.active_speaker.startup_hold import hold_staged_startup

    assert hold_staged_startup() is True
    anchor = tmp_path / "active_speaker_staged_startup.yml"
    state = tmp_path / "startup_load.json"
    state.write_text(
        json.dumps(
            {"status": load_status, "candidate_config_path": str(anchor)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE", str(state))
    live = anchor if live_is_anchor else tmp_path / "active_speaker_baseline.yml"
    evidence.seed("camilla_config", (str(tmp_path / "statefile.yml"), str(live)))

    r = active_speaker.check_active_speaker_startup_hold()

    assert r.status == status
    assert r.reason == reason
    assert r.speaker_silent is silent


# ---------------------------------------------------- room correction authority


@pytest.mark.parametrize(
    "acoustic, status, reason",
    [
        pytest.param(
            {"required": False}, "skipped",
            active_speaker.REASON_ROOM_AUTHORITY_NOT_REQUIRED, id="no_authority_needed",
        ),
        pytest.param(
            {"required": True, "allowed": True, "authority": "manual_applied_profile"},
            "ok", "", id="banked",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": "active_commissioning_receipt_stale",
                "detail": "re-mint it when convenient",
            },
            "ok", active_speaker.REASON_ROOM_AUTHORITY_UNPROVEN,
            id="unproven_runs_anyway",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "detail": "finish commissioning when convenient",
            },
            "ok", active_speaker.REASON_ROOM_AUTHORITY_UNBANKED,
            id="never_minted_is_the_state_most_speakers_are_in",
        ),
        # ABSENT is also the module's catch-all default, so a store code under a
        # verified lifecycle (a receipt that VANISHED) arrives here too; it stays
        # ok rather than turning the fleet's normal state into a nag (ADR-0196).
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "cause": "missing",
                "detail": "finish commissioning when convenient",
            },
            "ok", active_speaker.REASON_ROOM_AUTHORITY_UNBANKED, id="vanished_receipt",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "cause": "PermissionError:EACCES:/var/lib/jasper/receipt.json",
                "detail": "a machine-level fault",
            },
            "warn", active_speaker.REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE,
            id="machine_fault_still_warns",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": "active_applied_profile_graph_mismatch",
                "detail": "apply that crossover again",
            },
            "warn", active_speaker.REASON_ROOM_AUTHORITY_BLOCKED,
            id="non_receipt_denial_blocks_the_run",
        ),
        pytest.param(
            None, "warn", active_speaker.REASON_ROOM_AUTHORITY_NO_DECISION,
            id="no_room_decision_published",
        ),
    ],
)
def test_room_correction_authority_discloses_but_never_fails(
    monkeypatch, acoustic, status, reason,
):
    """The doctor line is the only place an unproven room run is visible.

    Ruling S10 stopped the RECEIPT from refusing the run, so nothing else tells
    a household that the result it just measured is not banked as verified: an
    unproven receipt is `ok` with its reason. A machine fault reading the
    record, and every denial that is not a receipt at all, warn.
    """
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"acoustic_commissioning": acoustic},
    )

    r = active_speaker.check_room_correction_authority()

    assert r.status == status
    assert r.reason == reason


def test_room_correction_authority_warns_when_setup_cannot_be_read(monkeypatch):
    def _unreadable(**_kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(
        setup_status_mod, "read_active_speaker_setup_status", _unreadable
    )

    r = active_speaker.check_room_correction_authority()

    assert r.status == "warn"
    assert r.reason == active_speaker.REASON_SPEAKER_SETUP_UNREADABLE


# ------------------------------------------------- active speaker setup notices


@pytest.mark.parametrize(
    "issues, status, reason",
    [
        pytest.param([], "ok", active_speaker.REASON_SETUP_NOTICES_NONE, id="nothing_standing"),
        pytest.param(
            [{"severity": "blocker", "code": "x", "message": "m"}],
            "ok", active_speaker.REASON_SETUP_NOTICES_NONE,
            id="blockers_have_their_own_surfaces",
        ),
        pytest.param(
            [{
                "severity": "warning",
                "code": "active_baseline_topology_changed",
                "message": "topology changed since the applied baseline",
            }],
            "ok", active_speaker.REASON_SETUP_NOTICES_STANDING, id="topology_notice",
        ),
    ],
)
def test_active_speaker_setup_notices_renders_only_non_blockers(
    monkeypatch, issues, status, reason,
):
    """Nothing else in the tree renders a non-blocker setup issue, so demoting
    the topology block without this line would have been a silent deletion."""
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"issues": issues},
    )

    r = active_speaker.check_active_speaker_setup_notices()

    assert r.status == status
    assert r.reason == reason


# -------------------------------------------------- active speaker applied graph


@pytest.mark.parametrize(
    "payload, status, reason",
    [
        pytest.param(
            {"protected_profile": None}, "skipped",
            active_speaker.REASON_APPLIED_GRAPH_NO_PROFILE, id="nothing_applied_to_bind",
        ),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "current", "matches": True, "loaded_fingerprint": "a",
            }}},
            "ok", "", id="durable_graph_is_the_profiles",
        ),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "distributed_active_unsupported", "matches": False,
            }}},
            "skipped", active_speaker.REASON_APPLIED_GRAPH_NOT_EVALUATED,
            id="grouped_active_is_not_drift",
        ),
        pytest.param(
            {
                "issues": [{
                    "severity": "blocker",
                    "code": setup_status_mod.IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
                    "message": "commissioning graph is loaded",
                }],
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                }},
            },
            "skipped", active_speaker.REASON_APPLIED_GRAPH_STAGED_ANCHOR,
            id="staged_anchor_mismatch_is_by_design",
        ),
        pytest.param(
            {
                "active_config_path": "/var/lib/camilladsp/configs/candidate.yml",
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                    "expected_fingerprint": "a", "loaded_fingerprint": "b",
                    "differences": [{
                        "field": "as_woofer_delay.delay",
                        "expected": "0.0",
                        "loaded": "0.1286",
                    }],
                }},
            },
            "warn", active_speaker.REASON_APPLIED_GRAPH_MISMATCH,
            id="f6_rejected_delay_left_on_the_durable_anchor",
        ),
    ],
)
def test_active_speaker_applied_graph_discloses_never_gates(
    monkeypatch, payload, status, reason,
):
    """Blind-run finding F-6 had no surface that named the disagreement.

    WARN, never FAIL, and the two states that legitimately differ — a grouped
    active runtime, a staged/commissioning anchor — must not read as drift.
    """
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: payload,
    )

    r = active_speaker.check_active_speaker_applied_graph()

    assert r.status == status
    assert r.reason == reason


def test_active_speaker_applied_graph_warns_when_setup_cannot_be_read(monkeypatch):
    def _unreadable(**_kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(
        setup_status_mod, "read_active_speaker_setup_status", _unreadable
    )

    r = active_speaker.check_active_speaker_applied_graph()

    assert r.status == "warn"
    assert r.reason == active_speaker.REASON_SPEAKER_SETUP_UNREADABLE
