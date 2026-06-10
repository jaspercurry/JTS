"""inv-5 (docs/HANDOFF-multiroom.md §2) — a grouped member's local CamillaDSP
runs rate_adjust OFF, because snapclient's sample-stuffing is the single
rate-tracker for the synced chain (two rate-adjusters oscillate). Covers the
shared predicate, the generator param on the live generators, and the
jasper-doctor backstop that reads the ACTIVE config (so it catches any
generator + a config generated before the bond formed)."""
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


def test_member_camilla_kwargs_active_member():
    """An active member's config needs rate_adjust off + a channel-split."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="follower", channel="right",
                                    bond_id="b", leader_addr="jts.local"))
    assert kw["enable_rate_adjust"] is False
    assert kw["channel_split"] is not None
    assert kw["channel_split"].channel == "right"


def test_member_camilla_kwargs_solo_is_unchanged_defaults():
    """Solo / off → the solo-speaker defaults (config byte-for-byte unchanged)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg())  # grouping off
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None


def test_member_camilla_kwargs_stereo_member_no_split():
    """An active member with channel=stereo gets a passthrough split (no weave)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="leader", channel="stereo", bond_id="b"))
    assert kw["enable_rate_adjust"] is False
    assert kw["channel_split"].is_passthrough  # stereo = no-op weave


def test_member_camilla_kwargs_invalid_member_unchanged():
    """Enabled-but-invalid is NOT active → solo defaults (fail-safe)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="", channel="left",
                                    bond_id="", error="bond_id empty"))
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None


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


def test_doctor_check_skips_when_not_active_member(monkeypatch):
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: _cfg())  # solo
    from jasper.cli.doctor.grouping import check_grouping_rate_adjust
    result = check_grouping_rate_adjust()
    assert result.status == "ok"
    assert "not an active bond member" in result.detail


def test_doctor_check_warns_active_member_with_rate_adjust_on(monkeypatch, tmp_path):
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


def test_doctor_check_ok_active_member_rate_adjust_off(monkeypatch, tmp_path):
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="follower", channel="right",
                             bond_id="b", leader_addr="jts.local"),
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


# ---------- jasper-doctor: channel-split observability backstop ----------
#
# A missing channel-split is SILENT (the speaker plays full stereo) — unlike
# rate_adjust, which oscillates audibly. This check is the only way a
# wrong-channel member is visible.

_BASE_MIXERS = "mixers:\n  master_gain:\n    channels: { in: 2, out: 2 }\n"


def _channel_split_check(monkeypatch, tmp_path, *, cfg, config_text):
    import jasper.cli.doctor.correction as corrmod
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)
    config_file = tmp_path / "active.yml"
    config_file.write_text(config_text)
    monkeypatch.setattr(
        corrmod, "_active_camilla_config_path",
        lambda: ("statefile", str(config_file)),
    )
    from jasper.cli.doctor.grouping import check_grouping_channel_split
    return check_grouping_channel_split()


def test_channel_split_check_skips_solo_and_stereo(monkeypatch, tmp_path):
    # solo
    r = _channel_split_check(monkeypatch, tmp_path, cfg=_cfg(), config_text=_BASE_MIXERS)
    assert r.status == "ok"
    # active member but channel=stereo (passthrough — no channel_select expected)
    r = _channel_split_check(
        monkeypatch, tmp_path,
        cfg=_cfg(enabled=True, role="leader", channel="stereo", bond_id="b"),
        config_text=_BASE_MIXERS,
    )
    assert r.status == "ok"


def test_channel_split_check_warns_when_missing_for_nonstereo_member(monkeypatch, tmp_path):
    r = _channel_split_check(
        monkeypatch, tmp_path,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        config_text=_BASE_MIXERS,  # no channel_select
    )
    assert r.status == "warn"
    assert "channel=right" in r.detail
    assert "wrong channel" in r.detail


def test_channel_split_check_ok_when_present(monkeypatch, tmp_path):
    text = _BASE_MIXERS + "  channel_select:\n    channels: { in: 2, out: 2 }\n"
    r = _channel_split_check(
        monkeypatch, tmp_path,
        cfg=_cfg(enabled=True, role="follower", channel="left",
                 bond_id="b", leader_addr="jts.local"),
        config_text=text,
    )
    assert r.status == "ok"


def test_config_has_channel_select_is_block_scoped():
    """channel_select must be a top-level mixer, not a stray match elsewhere."""
    from jasper.cli.doctor.grouping import _config_has_channel_select
    assert _config_has_channel_select(_BASE_MIXERS + "  channel_select:\n") is True
    assert _config_has_channel_select(_BASE_MIXERS) is False
    # A `channel_select:` outside the mixers block must NOT match.
    assert _config_has_channel_select("filters:\n  channel_select:\n") is False


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


# ---------- jasper-doctor: TTS-separation blocker (inv-3) ----------
#
# The snapfifo producer is unwired (dead code) and would leak the leader's TTS
# to followers if re-wired without a fanin music-only stream. This check is the
# honest operator signal that a leader isn't actually streaming.


def test_tts_separation_check_warns_for_active_leader(monkeypatch):
    import jasper.multiroom.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    from jasper.cli.doctor.grouping import check_grouping_tts_separation
    result = check_grouping_tts_separation()
    assert result.status == "warn"
    assert "TTS separation" in result.detail
    assert "leak" in result.detail


def test_tts_separation_check_skips_follower_solo_and_off(monkeypatch):
    import jasper.multiroom.config as cfgmod
    from jasper.cli.doctor.grouping import check_grouping_tts_separation
    # solo
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: _cfg())
    assert check_grouping_tts_separation().status == "ok"
    # follower — the leak path is the LEADER's tap, not this speaker
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: _cfg(enabled=True, role="follower", channel="right",
                             bond_id="b", leader_addr="jts.local"),
    )
    assert check_grouping_tts_separation().status == "ok"
