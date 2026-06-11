"""inv-5 (docs/HANDOFF-multiroom.md §2) — the bonded LEADER's local CamillaDSP
runs rate_adjust OFF, because snapclient's sample-stuffing is the single
rate-tracker for the synced chain (two rate-adjusters oscillate). A follower's
local CamillaDSP is out of the bonded path (canonical model, Increment 5) and
keeps solo defaults. Covers the shared predicate, the member-config policy,
the generator param on the live generators, and the jasper-doctor backstops
(active-config rate_adjust + leader pipe + outputd channel-pick env drift)."""
from __future__ import annotations

from jasper.multiroom.config import (
    GroupingConfig,
    is_active_member,
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


def test_member_camilla_kwargs_active_leader_gets_the_pipe_sink():
    """CANONICAL (Increment 5): an active LEADER's local CamillaDSP bakes the
    shared program and writes snapserver's pipe — rate_adjust off (a File
    sink has no output clock; snapclient is the one rate-tracker), no
    channel weave (the pipe carries BOTH channels; members pick channels
    downstream in outputd's ChannelPick)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    from jasper.multiroom.reconcile import SNAPFIFO
    kw = member_camilla_kwargs(
        _cfg(enabled=True, role="leader", channel="left", bond_id="b"))
    assert kw["enable_rate_adjust"] is False
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] == SNAPFIFO


def test_member_camilla_kwargs_active_follower_is_solo_defaults():
    """CANONICAL (Increment 5): an active FOLLOWER's local CamillaDSP is OUT
    of the bonded playback path (the round-trip feeds outputd directly);
    it keeps producing the normal direct lane — the inv-B fallback feed —
    so its config stays byte-for-byte the solo config (rate_adjust=True is
    CORRECT: its sink is the ALSA loopback, which has a clock)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="follower", channel="right",
                                    bond_id="b", leader_addr="jts.local"))
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] is None


def test_member_camilla_kwargs_solo_is_unchanged_defaults():
    """Solo / off → the solo-speaker defaults (config byte-for-byte unchanged)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg())  # grouping off
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] is None


def test_member_camilla_kwargs_stereo_leader_still_bakes_the_pipe():
    """A leader bakes the pipe regardless of its OWN channel assignment —
    the pipe always carries the full shared program."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    from jasper.multiroom.reconcile import SNAPFIFO
    kw = member_camilla_kwargs(_cfg(enabled=True, role="leader", channel="stereo", bond_id="b"))
    assert kw["enable_rate_adjust"] is False
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] == SNAPFIFO


def test_member_camilla_kwargs_invalid_member_unchanged():
    """Enabled-but-invalid is NOT active → solo defaults (fail-safe)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="", channel="left",
                                    bond_id="", error="bond_id empty"))
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] is None


# ---------- the generators emit the param ----------


def test_correction_generator_honors_rate_adjust_param():
    from jasper.correction.camilla_yaml import emit_correction_config
    assert "enable_rate_adjust: true" in emit_correction_config([])  # default
    assert "enable_rate_adjust: false" in emit_correction_config(
        [], enable_rate_adjust=False,
    )


def test_sound_generator_honors_rate_adjust_param():
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SimpleEq, SoundProfile
    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))
    assert "enable_rate_adjust: true" in emit_sound_config(profile)  # default
    assert "enable_rate_adjust: false" in emit_sound_config(
        profile, enable_rate_adjust=False,
    )


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


def test_doctor_check_skips_when_not_active_leader(monkeypatch):
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust
    # solo
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: _cfg())
    result = check_grouping_rate_adjust()
    assert result.status == "ok"
    assert "not an active bond leader" in result.detail
    # active FOLLOWER: out of scope by design — its local CamillaDSP feeds
    # only the inv-B fallback lane, where rate_adjust=true is correct.
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="follower", channel="right",
                             bond_id="b", leader_addr="jts.local"),
    )
    result = check_grouping_rate_adjust()
    assert result.status == "ok"
    assert "not an active bond leader" in result.detail


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
    assert "oscillate" in result.detail


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
    assert "silent" in r.detail

    # The reconciler's bonded emit → ok.
    config_file.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
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
    assert check_grouping_leader_pipe().status == "ok"


def _channel_pick_check(monkeypatch, *, cfg, env_text=None, env_path=None):
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
    return groupmod.check_grouping_channel_pick()


def test_channel_pick_check_warns_when_env_missing(monkeypatch):
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
    )
    assert r.status == "warn"
    assert "not wired" in r.detail


def test_channel_pick_check_warns_on_channel_drift(monkeypatch, tmp_path):
    from jasper.multiroom.reconcile import (
        MEMBER_CONTENT_FIFO,
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
    )
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        env_text=(
            f"{OUTPUTD_DAC_CONTENT_FIFO_ENV}={MEMBER_CONTENT_FIFO}\n"
            f"{OUTPUTD_DAC_CONTENT_CHANNEL_ENV}=left\n"  # drifted
        ),
        env_path=env,
    )
    assert r.status == "warn"
    assert "wrong channel" in r.detail


def test_channel_pick_check_ok_when_wired(monkeypatch, tmp_path):
    from jasper.multiroom.reconcile import (
        MEMBER_CONTENT_FIFO,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    # The reconciler's own pure derive writes the file → the check passes:
    # the two ends of the contract are the same function.
    derived = outputd_grouping_env(cfg)
    assert derived[OUTPUTD_DAC_CONTENT_FIFO_ENV] == MEMBER_CONTENT_FIFO
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text="".join(f"{k}={v}\n" for k, v in derived.items()),
        env_path=env,
    )
    assert r.status == "ok"
    assert "channel=left" in r.detail


def test_outputd_grouping_env_clears_when_not_active():
    """Disable-clears-stale: solo / invalid → BOTH keys present as empty
    strings (outputd reads empty as unset → byte-identical solo loop)."""
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


def test_tts_interim_check_warns_while_bonded(monkeypatch):
    """The PR-1 known gap stays VISIBLE while bonded (no-silent-failure):
    TTS rides the synced stream until PR-2's outputd TTS mixer."""
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_tts_interim
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: _cfg())
    assert check_grouping_tts_interim().status == "ok"
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    r = check_grouping_tts_interim()
    assert r.status == "warn"
    assert "PR-2" in r.detail


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


# NOTE: the former check_grouping_tts_separation tests were REMOVED
# 2026-06-11 with the check itself (the retired outputd-as-producer
# machinery — see HANDOFF-multiroom.md §2 "Stranded by this design"). The
# operator story it carried now lives in check_grouping's runtime detail,
# covered by test_doctor.py::
# test_check_grouping_leader_reads_degraded_until_producer_built.
