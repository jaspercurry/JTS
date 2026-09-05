# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""inv-5 — no CamillaDSP runs rate_adjust.
snapclient's sample-stuffing is the single rate-tracker for the synced chain
(two rate-adjusters oscillate); and, since ADR-0100, every sink a member can
play into — the leader's pipe, or Ring B for everything else — is one CamillaDSP
cannot actuate rate_adjust on regardless (a File sink has no output clock; a
ring PCM is an ioplug, and alsa-lib reports card -1 for every ioplug), which is
why the emitter resolves the field from the sink. A DUMB follower's local
CamillaDSP is out of the bonded path (canonical model, Increment 5) and keeps
solo defaults. Covers the shared predicate, the member-config policy, the
emitted bonded config, and the jasper-doctor backstops
(active-config rate_adjust + leader pipe + outputd channel-pick env drift)."""
from __future__ import annotations

import pytest

from tests._bonded_member import bonded_grouping_env

from jasper.cli.doctor import grouping as doctor_grouping
from jasper.cli.doctor._evidence import evidence

from jasper.multiroom.dac_content_ring import (
    DAC_CONTENT_LANE_ENV,
    DAC_CONTENT_RING_PERIOD_FRAMES,
)
from jasper.multiroom.config import (
    GroupingConfig,
    is_active_member,
)
from jasper.multiroom.tts_route import VOICE_PARK_ENV, expected_grouping_tts_route
from jasper.tts_routing import (
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET_ENV,
    TTS_MIX_STAGE_ENV,
    TTS_MIX_STAGE_POST_DSP,
    VOICE_TTS_SOCKET_ENV,
)


def _cfg(**kw) -> GroupingConfig:
    base = dict(
        enabled=False, role="", channel="stereo", bond_id="",
        leader_addr="", buffer_ms=400, codec="flac", error=None,
    )
    base.update(kw)
    return GroupingConfig(**base)


# ---------- the shared predicate ----------


def test_active_members_are_active():
    leader = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    follower = _cfg(
        enabled=True, role="follower", channel="right",
        bond_id="b", leader_addr="jts.local",
    )
    assert is_active_member(leader) is True
    assert is_active_member(follower) is True


def test_solo_off_invalid_are_not_active():
    assert is_active_member(_cfg()) is False  # grouping off
    # Enabled-but-INVALID (fail-loud) is NOT an active member: nothing streams,
    # so the local rate_adjust stays as-is (the reconciler won't start a bond).
    invalid = _cfg(
        enabled=True, role="", channel="left", bond_id="",
        error="JASPER_GROUPING_BOND_ID is empty",
    )
    assert is_active_member(invalid) is False


# ---------- member-config policy: the ONE decision, applied path-independently --


@pytest.mark.parametrize(
    ("cfg", "expects_pipe"),
    [
        (_cfg(enabled=True, role="leader", channel="left", bond_id="b"), True),
        (_cfg(enabled=True, role="leader", channel="stereo", bond_id="b"), True),
        (
            _cfg(enabled=True, role="follower", channel="right", bond_id="b",
                 leader_addr="jts.local"),
            False,
        ),
        (_cfg(), False),
        (
            _cfg(enabled=True, role="", channel="left", bond_id="",
                 error="bond_id empty"),
            False,
        ),
    ],
    ids=["leader-left", "leader-stereo", "follower", "solo", "invalid"],
)
def test_member_camilla_kwargs_carries_only_the_leaders_pipe(cfg, expects_pipe):
    """CANONICAL (Increment 5): a LEADER's local CamillaDSP bakes the shared
    program to snapserver's pipe whatever channel it is assigned — the pipe
    carries BOTH channels and members pick theirs downstream in outputd's
    ChannelPick. Every other shape needs no member policy at all: a follower's
    local CamillaDSP is OUT of the bonded playback path (it keeps producing the
    inv-B fallback feed), and solo / off / invalid are the emitter's own
    defaults, byte-for-byte."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    from jasper.multiroom.reconcile import SNAPFIFO

    kw = member_camilla_kwargs(cfg)

    assert kw == ({"playback_pipe_path": SNAPFIFO} if expects_pipe else {})


def test_member_camilla_kwargs_active_leader_preserves_channel_delays():
    from jasper.multiroom.member_config import member_camilla_kwargs

    kw = member_camilla_kwargs(
        _cfg(
            enabled=True,
            role="leader",
            channel="left",
            bond_id="b",
            left_delay_ms=1.25,
            right_delay_ms=0.0,
        )
    )

    assert kw["room_peqs_right"] == []
    assert kw["channel_delays_ms"] == (1.25, 0.0)


def test_member_camilla_kwargs_resolves_config_at_call_time(monkeypatch):
    """Regression for #1270. member_config must resolve ``load_config`` /
    ``is_active_leader`` through ``jasper.multiroom.config`` at CALL time, not
    capture them via ``from .config import ...`` at import time. A captured
    binding survived a monkeypatch teardown in an unrelated grouping-doctor
    test (which patches ``config.load_config``) and poisoned later tests —
    pytest-xdist sharding hid it by varying which test first-imported this
    module.

    The spy is the leak-proof part: a stale from-import binding would bypass
    BOTH patches below, so ``is_active_leader`` would never see our sentinel.
    (Asserting on the returned kwargs alone is not enough — the leaked lambda
    happened to also be a bonded leader, so a value check passes by accident.)
    """
    import jasper.multiroom.config as cfgmod
    from jasper.multiroom.member_config import member_camilla_kwargs

    sentinel = object()
    seen = {}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: sentinel)

    def spy_is_active_leader(cfg):
        seen["cfg"] = cfg
        return False

    monkeypatch.setattr(cfgmod, "is_active_leader", spy_is_active_leader)

    kw = member_camilla_kwargs()  # cfg omitted → must call cfgmod.load_config

    # Our patched load_config supplied the cfg, and our patched is_active_leader
    # received THAT exact object — proving call-time module resolution.
    assert seen.get("cfg") is sentinel
    assert kw == {}  # spy returned False → solo defaults


# ---------- generated sound configs emit the param ----------


def test_the_leader_bake_emits_rate_adjust_off_unasked():
    """inv-5 at the emitter, with nobody passing the flag: the leader's
    File/pipe sink is what resolves it, so a bonded leader's baked config cannot
    carry a rate-adjuster to fight snapclient's sample-stuffing."""
    from jasper.camilla_config_contract import parse_camilla_devices_config
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SimpleEq, SoundProfile

    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))
    parsed = parse_camilla_devices_config(
        emit_sound_config(profile, playback_pipe_path="/run/snapfifo")
    )

    assert parsed["enable_rate_adjust"] is False


# ---------- jasper-doctor backstop ----------


def test_doctor_parser_reads_devices_enable_rate_adjust():
    from jasper.cli.doctor.grouping import _devices_rate_adjust_from_text
    assert _devices_rate_adjust_from_text(
        "devices:\n  enable_rate_adjust: true\n") is True
    assert _devices_rate_adjust_from_text(
        "devices:\n  enable_rate_adjust: false\n") is False
    assert _devices_rate_adjust_from_text(
        "devices:\n  samplerate: 48000\n") is None
    # A key OUTSIDE the devices block must NOT match (block-scoped scan).
    assert _devices_rate_adjust_from_text(
        "filters:\n  enable_rate_adjust: true\n") is None


def _stub_active_box(monkeypatch, active: bool):
    """Drive `is_active_speaker_box`'s answer without touching a topology file.

    The check imports it function-locally, so patching the owning module is what
    the call resolves.
    """
    import jasper.multiroom.reconcile as mr

    monkeypatch.setattr(mr, "is_active_speaker_box", lambda: active)


def test_doctor_check_skips_when_no_local_camilla_is_in_the_chain(monkeypatch):
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust

    _stub_active_box(monkeypatch, False)
    # solo
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: _cfg())
    result = check_grouping_rate_adjust()
    assert result.status == "skipped"
    assert result.reason == doctor_grouping.REASON_NOT_APPLICABLE
    # Enabled-but-INVALID: nothing streams, so no bonded chain exists.
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="", channel="left", bond_id="",
                             error="JASPER_GROUPING_BOND_ID is empty"),
    )
    result = check_grouping_rate_adjust()
    assert result.status == "skipped"
    assert result.reason == doctor_grouping.REASON_NOT_APPLICABLE


def test_doctor_check_covers_the_active_follower(monkeypatch, tmp_path):
    """The widening: inv-5 is not leader-only. An ACTIVE follower's camilla#1 IS
    in the bonded chain (it captures the grouping ring and runs Layer A), and the
    check's own docstring used to assert the opposite for every follower
    ("rate_adjust: true is REQUIRED there") — already false on a ring-armed box
    and false everywhere once the ingress is on the ring, because a ring PCM is
    an ioplug: CamillaDSP builds no HCtl and the request cannot be actuated.
    """
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust

    _stub_active_box(monkeypatch, True)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="follower", channel="right",
                             bond_id="b", leader_addr="jts.local"),
    )
    config_file = tmp_path / "active.yml"
    config_file.write_text("devices:\n  enable_rate_adjust: true\n")
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )

    result = check_grouping_rate_adjust()
    assert result.status == "warn"
    assert result.reason == doctor_grouping.REASON_RATE_ADJUST_ON

    # Severity stays warn, never fail: on a ring capture the key is INERT, so
    # this is an observability lie rather than a hazard. Escalating would red a
    # fleet over a cosmetic key.
    config_file.write_text("devices:\n  enable_rate_adjust: false\n")
    # A fresh doctor run re-reads the config; the evidence cache from the call
    # above must not serve the stale text.
    evidence.reset()
    assert check_grouping_rate_adjust().status == "ok"


def test_doctor_check_does_not_warn_on_a_dumb_follower(monkeypatch, tmp_path):
    """The scope's OTHER edge, and the reason it is not a bare `is_active_member`.

    A DUMB (passive, single-DAC) follower plays the bond through outputd's
    dac_content lane; its own camilla#1 stays on the solo fallback feed, whose
    sink is Ring B (ADR-0100), an ioplug CamillaDSP cannot actuate rate_adjust
    on. This check still must not warn there: it has no bond apply on that box
    to catch, so it would red every passive follower in the fleet regardless of
    the local config's value — a doctor that cries wolf.
    """
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust

    dumb_follower = _cfg(enabled=True, role="follower", channel="right",
                         bond_id="b", leader_addr="jts.local")

    _stub_active_box(monkeypatch, False)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: dumb_follower)
    config_file = tmp_path / "active.yml"
    config_file.write_text("devices:\n  enable_rate_adjust: true\n")
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )

    result = check_grouping_rate_adjust()
    assert result.status == "skipped"
    assert result.reason == doctor_grouping.REASON_NOT_APPLICABLE


def test_doctor_check_will_not_claim_rate_adjust_off_it_cannot_read(
    monkeypatch, tmp_path,
):
    """The fail-soft asymmetry: `_devices_rate_adjust_from_text` returns None for
    ABSENT *or* unparseable, and the check tested only `is True` — so a config
    with no key reported ok "rate_adjust off", a claim the file does not
    support. Unconfirmed is now warn."""
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="leader", channel="left",
                             bond_id="b"),
    )
    config_file = tmp_path / "active.yml"
    # A devices block with no enable_rate_adjust key at all.
    config_file.write_text("devices:\n  samplerate: 48000\n")
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )

    result = check_grouping_rate_adjust()
    assert result.status == "warn"
    assert result.reason == doctor_grouping.REASON_RATE_ADJUST_UNCONFIRMED


def test_doctor_check_warns_active_leader_with_rate_adjust_on(monkeypatch, tmp_path):
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    config_file = tmp_path / "active.yml"
    config_file.write_text("devices:\n  volume_limit: 0.0\n  enable_rate_adjust: true\n")
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust
    result = check_grouping_rate_adjust()
    assert result.status == "warn"
    # The reason names the INVARIANT (snapclient is the chain's sole tracker),
    # not the leader-only symptom: the same reason now serves a follower,
    # where a stray `true` is inert rather than oscillating.
    assert result.reason == doctor_grouping.REASON_RATE_ADJUST_ON


def test_doctor_check_ok_active_leader_rate_adjust_off(monkeypatch, tmp_path):
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    config_file = tmp_path / "active.yml"
    config_file.write_text("devices:\n  enable_rate_adjust: false\n")
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust
    result = check_grouping_rate_adjust()
    assert result.status == "ok"


# ---------- jasper-doctor: leader-pipe + channel-pick backstops ----------
#
# Both failure classes are SILENT (an un-piped leader streams an empty FIFO
# behind green units; a wrong channel pick plays the full stereo program) —
# unlike rate_adjust, which oscillates audibly. These checks are the only
# way each is visible.


def test_leader_pipe_check_warns_on_solo_config_and_passes_on_emitted_pipe(
    monkeypatch, tmp_path,
):
    """Contract coupling on purpose: the ok-case scans a REAL config emitted
    by emit_sound_config with the policy kwargs — if the emitter's playback
    shape ever drifts from the doctor's scanner, this fails."""
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_leader_pipe
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    leader = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: leader)
    config_file = tmp_path / "active.yml"
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )

    # Solo-shaped active config on an active leader → warn (stream silent).
    config_file.write_text(emit_sound_config(SoundProfile(enabled=False)))
    r = check_grouping_leader_pipe()
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_LEADER_PIPE_NOT_WIRED

    # The reconciler's bonded emit → ok. A fresh doctor run re-reads the
    # config; the evidence cache from the call above must not serve the stale
    # text.
    evidence.reset()
    config_file.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            playback_pipe_path=SNAPFIFO,
        )
    )
    r = check_grouping_leader_pipe()
    assert r.status == "ok"


def test_leader_pipe_check_skips_non_leaders(monkeypatch):
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_leader_pipe
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="follower", channel="right",
                             bond_id="b", leader_addr="jts.local"),
    )
    r = check_grouping_leader_pipe()
    assert r.status == "skipped"
    assert r.reason == doctor_grouping.REASON_NOT_APPLICABLE


def _channel_pick_check(
    monkeypatch, *, cfg, env_text=None, env_path=None, active_box=False,
    period=DAC_CONTENT_RING_PERIOD_FRAMES, topology_state=None,
):
    import jasper.cli.doctor.grouping as groupmod
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)
    if env_text is not None:
        env_path.write_text(env_text)
    import jasper.multiroom.reconcile as recmod
    monkeypatch.setattr(
        recmod, "OUTPUTD_GROUPING_ENV_FILE",
        str(env_path) if env_path else "/nonexistent/grouping-outputd.env",
    )
    monkeypatch.setattr(
        recmod,
        "_output_topology_state",
        lambda: topology_state or (active_box, not active_box),
    )
    # This box's outputd runs the return ring's slot, so the fourth arming gate
    # passes and the check's subject is the lane env. The mismatching box is
    # test_channel_pick_check_names_a_period_the_return_ring_cannot_carry.
    monkeypatch.setattr(recmod, "box_outputd_period_frames", lambda: period)
    return groupmod.check_grouping_channel_pick()


def test_channel_pick_check_warns_when_env_missing(monkeypatch):
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_CHANNEL_PICK_LANE_MISSING


def test_channel_pick_check_warns_on_channel_drift(monkeypatch, tmp_path):
    from jasper.multiroom.reconcile import OUTPUTD_DAC_CONTENT_CHANNEL_ENV

    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        env_text=(
            f"{DAC_CONTENT_LANE_ENV}=1\n"
            f"{OUTPUTD_DAC_CONTENT_CHANNEL_ENV}=left\n"  # drifted
        ),
        env_path=env,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_CHANNEL_PICK_DRIFT


def test_channel_pick_check_names_an_unreadable_topology_as_such(
    monkeypatch, tmp_path,
):
    """An unreadable topology is not "the topology forbids a flat graph".

    The reconciler takes its own `active_speaker_topology_unknown` branch there
    and never asks the lane rule at all, so naming the flat-graph rule would
    send the operator to fix a permission the box has not been asked for."""
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        env_text=f"{DAC_CONTENT_LANE_ENV}=\n",
        env_path=env,
        topology_state=(None, False),
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_CHANNEL_PICK_TOPOLOGY_UNKNOWN


def test_channel_pick_check_names_a_period_the_return_ring_cannot_carry(
    monkeypatch, tmp_path,
):
    """A box whose outputd period is not the ring's slot never armed the lane,
    and the reconciler refused its bond on purpose — so the check names that
    rather than prescribing a reconcile that would change nothing."""
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        env_text=f"{DAC_CONTENT_LANE_ENV}=\n",
        env_path=env,
        period=1024,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_CHANNEL_PICK_PERIOD_MISMATCH


def test_channel_pick_check_ok_when_wired(monkeypatch, tmp_path):
    from jasper.fanin_coupling import dac_content_lane_marker_armed

    cfg = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    # The reconciler's own pure derive writes the file → the check passes:
    # the two ends of the contract are the same function.
    derived = bonded_grouping_env(
        cfg,
        flat_output_allowed=True,
    )
    assert dac_content_lane_marker_armed(derived)
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text="".join(f"{k}={v}\n" for k, v in derived.items()),
        env_path=env,
    )
    assert r.status == "ok"


def test_channel_pick_check_active_endpoint_names_the_grouping_ring(
    monkeypatch, tmp_path,
):
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    derived = outputd_grouping_env(cfg, active_endpoint=True)
    assert derived[DAC_CONTENT_LANE_ENV] == ""
    assert derived[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] == ""
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text="".join(f"{k}={v}\n" for k, v in derived.items()),
        env_path=env,
        active_box=True,
    )
    assert r.status == "ok"


def test_channel_pick_check_active_endpoint_warns_on_stale_dumb_lane(
    monkeypatch, tmp_path,
):
    from jasper.multiroom.reconcile import OUTPUTD_DAC_CONTENT_CHANNEL_ENV

    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text=(
            f"{DAC_CONTENT_LANE_ENV}=1\n"
            f"{OUTPUTD_DAC_CONTENT_CHANNEL_ENV}=right\n"
        ),
        env_path=env,
        active_box=True,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_CHANNEL_PICK_ACTIVE_ENDPOINT_LANE_ARMED


def test_outputd_grouping_env_clears_when_not_active():
    """Disable-clears-stale: solo / invalid → the lane keys present as
    empty strings (outputd reads empty as unset → byte-identical solo
    loop), and the writer never names a content bridge in any state — the
    round-trip lane has no transport of its own to declare (ADR-0100)."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        outputd_grouping_env,
    )
    for cfg in (
        _cfg(),  # off
        _cfg(enabled=True, role="", channel="left", bond_id="", error="bad"),
    ):
        env = outputd_grouping_env(cfg)
        assert env[OUTPUTD_DAC_CONTENT_FIFO_ENV] == ""
        assert env[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] == ""
        assert "JASPER_OUTPUTD_CONTENT_BRIDGE" not in env


def test_only_the_armed_branch_touches_the_content_bridge_key():
    """UNARMED SHAPES MUST NOT WRITE THE KEY AT ALL, in any spelling.

    This layer loads after ``outputd.env``, so a key written here overrides what
    ``jasper-fanin-coupling-auto`` put there. Without the marker outputd reads
    the bridge with ``env_str``, whose BLANK is a value it parks on, so an
    unarmed box has to inherit layer 1 verbatim — absent, not cleared."""
    from jasper.multiroom.reconcile import outputd_grouping_env
    configs = [
        _cfg(),
        _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"),
        _cfg(enabled=True, role="", channel="left", bond_id="", error="bad"),
    ]
    for cfg in configs:
        # No flat-output permission and no resolved period: every one of these
        # takes an unarmed branch.
        assert "JASPER_OUTPUTD_CONTENT_BRIDGE" not in outputd_grouping_env(cfg)

    armed = bonded_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"),
        flat_output_allowed=True,
    )
    assert armed["JASPER_OUTPUTD_CONTENT_BRIDGE"] == ""


def test_outputd_config_exit_code_contract():
    """The Rust EXIT_CONFIG constant and the unit's
    RestartPreventExitStatus must agree — together they make a
    fail-closed config rejection PARK outputd (visible) instead of
    crash-looping into StartLimitAction=reboot (the jts3 incident's
    escalation path)."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    daemon_rs_path = root / "rust/jasper-daemon/src/lib.rs"
    unit_path = root / "deploy/systemd/jasper-outputd.service"
    rust_exit = re.search(
        r"EXIT_CONFIG:\s*i32\s*=\s*(\d+);", daemon_rs_path.read_text()
    )
    unit_exit = re.search(r"RestartPreventExitStatus=(\d+)", unit_path.read_text())
    assert rust_exit, f"EXIT_CONFIG constant not found in {daemon_rs_path}"
    assert unit_exit, f"RestartPreventExitStatus not declared in {unit_path}"
    assert rust_exit.group(1) == unit_exit.group(1), (
        f"outputd's Rust EXIT_CONFIG ({rust_exit.group(1)}) and the unit's "
        f"RestartPreventExitStatus ({unit_exit.group(1)}) have drifted — a "
        "config-rejection exit would crash-loop into StartLimitAction=reboot "
        "instead of parking"
    )


def _tts_lane_check(
    monkeypatch, *, cfg, voice_text=None, outputd_text=None,
    resolved_voice_text=None, tmp_path=None, active_box=False,
):
    import jasper.cli.doctor.grouping as groupmod
    import jasper.multiroom.config as cfgmod
    import jasper.multiroom.reconcile as recmod
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)
    voice_path = "/nonexistent/grouping-voice.env"
    outputd_path = "/nonexistent/grouping-outputd.env"
    if voice_text is not None:
        f = tmp_path / "grouping-voice.env"
        f.write_text(voice_text)
        voice_path = str(f)
    if outputd_text is not None:
        f = tmp_path / "grouping-outputd.env"
        f.write_text(outputd_text)
        outputd_path = str(f)
    monkeypatch.setattr(recmod, "VOICE_GROUPING_ENV_FILE", voice_path)
    monkeypatch.setattr(recmod, "OUTPUTD_GROUPING_ENV_FILE", outputd_path)
    if resolved_voice_text is None:
        resolved_voice_text = (
            voice_text
            if voice_text is not None
            else f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n"
        )
    monkeypatch.setattr(
        groupmod,
        "_resolved_jasper_voice_env",
        lambda: (groupmod._parse_systemd_environment(resolved_voice_text), ""),
    )
    monkeypatch.setattr(
        recmod, "_output_topology_state", lambda: (active_box, not active_box)
    )
    return groupmod.check_grouping_tts_lane()


def test_tts_lane_check_solo_clean_is_ok(monkeypatch):
    assert _tts_lane_check(monkeypatch, cfg=_cfg()).status == "ok"


def test_tts_lane_check_solo_with_stale_override_warns(monkeypatch, tmp_path):
    """A solo speaker carrying a leftover outputd pointer has voice
    writing to a socket nobody serves — silent assistant, must warn."""
    r = _tts_lane_check(
        monkeypatch, cfg=_cfg(),
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_SOCKET_DRIFT


def test_tts_lane_check_solo_with_stale_park_flag_warns(monkeypatch, tmp_path):
    """A solo speaker must not stay voice-parked after unbond."""
    r = _tts_lane_check(
        monkeypatch,
        cfg=_cfg(),
        resolved_voice_text=(
            f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n"
            f"{VOICE_PARK_ENV}=1\n"
        ),
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_PARK_FLAG_DRIFT


def test_tts_lane_check_bonded_without_voice_override_warns(monkeypatch):
    """Bonded but voice still on the fanin path → TTS rides the synced
    stream (the retired PR-1 interim shape): degraded, visible."""
    r = _tts_lane_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_SOCKET_DRIFT


def test_tts_lane_check_active_endpoint_fanin_is_ok(monkeypatch, tmp_path):
    """Active endpoints must use fan-in upstream of the crossover; outputd's
    post-crossover TTS socket is intentionally unarmed."""
    from jasper.multiroom.reconcile import outputd_grouping_env, voice_grouping_env
    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    r = _tts_lane_check(
        monkeypatch,
        cfg=cfg,
        voice_text="".join(
            f"{k}={v}\n"
            for k, v in voice_grouping_env(cfg, active_endpoint=True).items()
        ),
        outputd_text="".join(
            f"{k}={v}\n"
            for k, v in outputd_grouping_env(cfg, active_endpoint=True).items()
        ),
        resolved_voice_text=f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n",
        tmp_path=tmp_path,
        active_box=True,
    )
    assert r.status == "ok"


def test_tts_lane_check_active_endpoint_stale_outputd_socket_warns(
    monkeypatch, tmp_path,
):
    """Active endpoints must not carry a stale post-crossover outputd TTS lane."""
    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    r = _tts_lane_check(
        monkeypatch,
        cfg=cfg,
        resolved_voice_text=f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n",
        outputd_text=f"{OUTPUTD_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        tmp_path=tmp_path,
        active_box=True,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_OUTPUTD_LANE_ARMED


def test_tts_lane_check_bonded_unarmed_lane_warns_broken(monkeypatch, tmp_path):
    """The worst drift: voice targets outputd's socket but the lane was
    never armed — assistant voice writes into the void."""
    r = _tts_lane_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        outputd_text="# lane keys absent\n",
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_OUTPUTD_LANE_UNARMED


def test_tts_lane_check_uses_systemd_resolved_voice_socket(monkeypatch, tmp_path):
    """The voice file can look correct while a later unit directive wins.

    The doctor must judge the same resolved env jasper-voice actually
    starts with, not the reconciler-written file in isolation.
    """
    from jasper.multiroom.reconcile import (
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    r = _tts_lane_check(
        monkeypatch,
        cfg=cfg,
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        resolved_voice_text=f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n",
        outputd_text="".join(
            f"{k}={v}\n"
            for k, v in outputd_grouping_env(cfg).items()
        ),
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_TTS_SOCKET_DRIFT


def test_tts_lane_check_ok_when_reconciler_wired_both_ends(monkeypatch, tmp_path):
    """The reconciler's own pure derives write both files → the check
    passes: the two ends of the contract are the same functions."""
    from jasper.multiroom.reconcile import outputd_grouping_env, voice_grouping_env
    cfg = _cfg(enabled=True, role="follower", channel="right",
               bond_id="b", leader_addr="jts.local")
    r = _tts_lane_check(
        monkeypatch, cfg=cfg,
        voice_text="".join(
            f"{k}={v}\n" for k, v in voice_grouping_env(cfg).items()),
        outputd_text="".join(
            f"{k}={v}\n" for k, v in outputd_grouping_env(cfg).items()),
        tmp_path=tmp_path,
    )
    assert r.status == "ok"


def test_grouping_tts_route_matrix_matches_reconciler_writers():
    """One matrix feeds both env writers, including special endpoints."""
    from jasper.multiroom.reconcile import outputd_grouping_env, voice_grouping_env

    cases = (
        (_cfg(), False),
        (_cfg(enabled=True, role="leader", channel="left", bond_id="b"), False),
        (
            _cfg(
                enabled=True,
                role="follower",
                channel="right",
                bond_id="b",
                leader_addr="jts.local",
            ),
            False,
        ),
        (_cfg(enabled=True, role="leader", channel="right", bond_id="b"), True),
        (
            _cfg(
                enabled=True,
                role="follower",
                channel="right",
                bond_id="b",
                leader_addr="jts.local",
            ),
            True,
        ),
    )

    for cfg, active_endpoint in cases:
        route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)
        voice_env = voice_grouping_env(cfg, active_endpoint=active_endpoint)
        outputd_env = outputd_grouping_env(cfg, active_endpoint=active_endpoint)

        if route.voice_env_socket is None:
            assert VOICE_TTS_SOCKET_ENV not in voice_env
        else:
            assert voice_env[VOICE_TTS_SOCKET_ENV] == route.voice_env_socket
        assert (voice_env.get(VOICE_PARK_ENV) == "1") is route.voice_parked
        assert outputd_env[OUTPUTD_TTS_SOCKET_ENV] == route.outputd_tts_socket


def test_camilla_block_field_shared_scanner():
    """The ONE config-field scanner the doctor's three checks share. Returns the
    raw value (scalar), "" for a nested-block key (presence), None when the
    block or key is absent, and is block-scoped (no cross-block match)."""
    from jasper.cli.doctor._shared import _camilla_block_field
    cfg = (
        "devices:\n  volume_limit: 0.0\n  enable_rate_adjust: true\n"
        "mixers:\n  channel_select:\n    channels: { in: 2, out: 2 }\n"
    )
    assert _camilla_block_field(cfg, "devices", "volume_limit") == "0.0"
    assert _camilla_block_field(cfg, "devices", "enable_rate_adjust") == "true"
    # A nested-block key (a mixer name) scans as "" → present, not None.
    assert _camilla_block_field(cfg, "mixers", "channel_select") == ""
    # Absent key / absent block → None.
    assert _camilla_block_field(cfg, "devices", "nope") is None
    assert _camilla_block_field(cfg, "pipeline", "anything") is None
    # Block-scoped: the devices key must not match from inside mixers.
    assert _camilla_block_field("mixers:\n  volume_limit: 9\n", "devices", "volume_limit") is None
    # Comments + quotes are stripped.
    assert _camilla_block_field("devices:\n  codec: 'flac'  # x\n", "devices", "codec") == "flac"


def test_voice_grouping_env_flips_socket_when_bonded_and_omits_when_solo():
    """PR-2: a passive bonded member's voice plays TTS via outputd
    (post-round-trip); solo and active endpoints OMIT the key entirely so voice
    falls back to fan-in upstream of the crossover."""
    from jasper.multiroom.reconcile import voice_grouping_env
    leader = voice_grouping_env(
        _cfg(enabled=True, role="leader", channel="left", bond_id="b"))
    assert leader == {
        VOICE_TTS_SOCKET_ENV: OUTPUTD_TTS_SOCKET,
        TTS_MIX_STAGE_ENV: TTS_MIX_STAGE_POST_DSP,
    }
    # A FOLLOWER additionally carries the dumb-follower park flag (the
    # validated signal jasper-aec-reconcile gates voice/AEC parking on) and the
    # outputd socket route.
    follower = voice_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"))
    assert follower == {
        VOICE_TTS_SOCKET_ENV: OUTPUTD_TTS_SOCKET,
        TTS_MIX_STAGE_ENV: TTS_MIX_STAGE_POST_DSP,
        VOICE_PARK_ENV: "1",
    }
    active_leader = voice_grouping_env(
        _cfg(enabled=True, role="leader", channel="right", bond_id="b"),
        active_endpoint=True,
    )
    assert active_leader == {}
    active_follower = voice_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"),
        active_endpoint=True,
    )
    assert active_follower == {VOICE_PARK_ENV: "1"}
    for cfg in (
        _cfg(),  # off
        _cfg(enabled=True, role="", channel="left", bond_id="", error="bad"),
    ):
        assert voice_grouping_env(cfg) == {}


def test_outputd_grouping_env_arms_tts_socket_with_the_lane():
    """The TTS socket arms/clears in lockstep with the round-trip lane —
    one file, one writer, no half-armed member."""
    from jasper.multiroom.reconcile import outputd_grouping_env
    bonded = outputd_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"))
    assert bonded[OUTPUTD_TTS_SOCKET_ENV] == OUTPUTD_TTS_SOCKET
    solo = outputd_grouping_env(_cfg())
    assert solo[OUTPUTD_TTS_SOCKET_ENV] == ""  # empty = unset to outputd


def _pair_channels_check(monkeypatch, *, cfg, leader_payload=None,
                         leader_error=None):
    import jasper.cli.doctor.grouping as groupmod  # noqa: F401 — import side
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_pair_channels
    from jasper.control import client as control_client

    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)

    class _Resp:
        def json(self):
            return {"grouping": leader_payload}

    def fake_get(path, *, base_url, timeout):
        if leader_error is not None:
            raise leader_error
        assert path == "/grouping"
        assert base_url == f"http://{cfg.leader_addr}:8780"
        return _Resp()

    monkeypatch.setattr(control_client, "get", fake_get)
    return check_grouping_pair_channels()


def test_pair_channels_check_skips_solo_and_leader(monkeypatch):
    r1 = _pair_channels_check(monkeypatch, cfg=_cfg())
    assert r1.status == "skipped"
    assert r1.reason == doctor_grouping.REASON_NOT_APPLICABLE
    r2 = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    assert r2.status == "skipped"
    assert r2.reason == doctor_grouping.REASON_NOT_APPLICABLE


def test_pair_channels_check_warns_on_same_channel_pair(monkeypatch):
    """The cross-member drift no member-local check can see: both speakers
    on one channel after an interrupted swap whose rollback failed."""
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="left",
                 bond_id="b", leader_addr="jts.local"),
        leader_payload={"bond_id": "b", "channel": "left"},
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_PAIR_CHANNELS_SAME_CHANNEL


def test_pair_channels_check_ok_when_coherent(monkeypatch):
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_payload={"bond_id": "b", "channel": "left"},
    )
    assert r.status == "ok"


def test_pair_channels_check_unreachable_leader_defers_to_health(monkeypatch):
    """Connectivity already has an owner (grouping health) — this check
    must not double-report one root cause."""
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_error=OSError("no route"),
    )
    assert r.status == "skipped"
    assert r.reason == doctor_grouping.REASON_PAIR_CHANNELS_LEADER_UNREACHABLE


def test_pair_channels_check_warns_on_bond_mismatch(monkeypatch):
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_payload={"bond_id": "OTHER", "channel": "left"},
    )
    assert r.status == "warn"
    assert r.reason == doctor_grouping.REASON_PAIR_CHANNELS_BOND_MISMATCH


def test_outputd_grouping_env_carries_the_trim():
    """Bonded: the validated trim derives into the outputd lane env
    (always written, so a cleared trim converges to 0.0); solo clears
    with EMPTY (outputd's env_f32 reads empty as unset -> default 0)."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_TRIM_ENV,
        outputd_grouping_env,
    )
    bonded = bonded_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local", trim_db=-2.5),
        flat_output_allowed=True,
    )
    assert bonded[OUTPUTD_DAC_CONTENT_TRIM_ENV] == "-2.5"
    solo = outputd_grouping_env(_cfg())
    assert solo[OUTPUTD_DAC_CONTENT_TRIM_ENV] == ""
