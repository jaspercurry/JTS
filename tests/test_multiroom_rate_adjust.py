"""inv-5 (docs/HANDOFF-multiroom.md §2) — a grouped member's local CamillaDSP
runs rate_adjust OFF, because snapclient's sample-stuffing is the single
rate-tracker for the synced chain (two rate-adjusters oscillate). Covers the
shared predicate, the generator param on the live generators, and the
jasper-doctor backstop that reads the ACTIVE config (so it catches any
generator + a config generated before the bond formed)."""
from __future__ import annotations

from jasper.multiroom.config import (
    GroupingConfig,
    disables_local_rate_adjust,
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


def test_active_members_disable_rate_adjust():
    leader = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    follower = _cfg(
        enabled=True, role="follower", channel="right",
        bond_id="b", leader_addr="jts.local",
    )
    assert disables_local_rate_adjust(leader) is True
    assert disables_local_rate_adjust(follower) is True
    assert is_active_member(leader) is True


def test_solo_off_invalid_keep_rate_adjust():
    assert disables_local_rate_adjust(_cfg()) is False  # grouping off
    # Enabled-but-INVALID (fail-loud) is NOT an active member: nothing streams,
    # so the local rate_adjust stays as-is (the reconciler won't start a bond).
    invalid = _cfg(
        enabled=True, role="", channel="left", bond_id="",
        error="JASPER_GROUPING_BOND_ID is empty",
    )
    assert disables_local_rate_adjust(invalid) is False
    assert is_active_member(invalid) is False


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
