# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    assert "channel_delays_ms" not in kw


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


def test_member_camilla_kwargs_invalid_member_unchanged():
    """Enabled-but-invalid is NOT active → solo defaults (fail-safe)."""
    from jasper.multiroom.member_config import member_camilla_kwargs
    kw = member_camilla_kwargs(_cfg(enabled=True, role="", channel="left",
                                    bond_id="", error="bond_id empty"))
    assert kw["enable_rate_adjust"] is True
    assert kw["channel_split"] is None
    assert kw["playback_pipe_path"] is None


# ---------- generated sound configs emit the param ----------


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


def _channel_pick_check(
    monkeypatch, *, cfg, env_text=None, env_path=None, active_box=False,
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
    monkeypatch.setattr(recmod, "is_active_speaker_box", lambda: active_box)
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


def test_channel_pick_check_active_endpoint_uses_loopback(monkeypatch, tmp_path):
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    derived = outputd_grouping_env(cfg, active_endpoint=True)
    assert derived[OUTPUTD_DAC_CONTENT_FIFO_ENV] == ""
    assert derived[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] == ""
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text="".join(f"{k}={v}\n" for k, v in derived.items()),
        env_path=env,
        active_box=True,
    )
    assert r.status == "ok"
    assert "loopback" in r.detail


def test_channel_pick_check_active_endpoint_warns_on_stale_dumb_lane(
    monkeypatch, tmp_path,
):
    from jasper.multiroom.reconcile import (
        MEMBER_CONTENT_FIFO,
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
    )
    cfg = _cfg(enabled=True, role="leader", channel="right", bond_id="b")
    env = tmp_path / "grouping-outputd.env"
    r = _channel_pick_check(
        monkeypatch, cfg=cfg,
        env_text=(
            f"{OUTPUTD_DAC_CONTENT_FIFO_ENV}={MEMBER_CONTENT_FIFO}\n"
            f"{OUTPUTD_DAC_CONTENT_CHANNEL_ENV}=right\n"
        ),
        env_path=env,
        active_box=True,
    )
    assert r.status == "warn"
    assert "active endpoint" in r.detail


def _sub_corner_check(monkeypatch, *, cfg, env_text=None, env_path=None):
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
    return groupmod.check_grouping_sub_corner()


def test_sub_corner_check_na_for_non_sub(monkeypatch):
    r = _sub_corner_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
    )
    assert r.status == "ok"
    assert "not an active sub member" in r.detail


def test_sub_corner_check_warns_when_env_missing(monkeypatch):
    r = _sub_corner_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="sub",
                 bond_id="b", leader_addr="jts.local"),
    )
    assert r.status == "warn"
    assert "not wired with the low-pass corner" in r.detail


def test_sub_corner_check_na_for_active_speaker_box(monkeypatch):
    """N2: an active-speaker box bonds via CamillaDSP (active endpoint), which
    clears the outputd dumb lane — the SUB_HZ env is correctly absent there.
    The check must be n/a, NOT a false 'corner missing' warn."""
    import jasper.multiroom.reconcile as recmod
    monkeypatch.setattr(recmod, "is_active_speaker_box", lambda: True)
    r = _sub_corner_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="sub",
                 bond_id="b", leader_addr="jts.local"),
    )
    assert r.status == "ok"
    assert "active-speaker box" in r.detail


def test_sub_corner_check_warns_when_corner_absent(monkeypatch, tmp_path):
    from jasper.multiroom.reconcile import (
        MEMBER_CONTENT_FIFO,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
    )
    env = tmp_path / "grouping-outputd.env"
    r = _sub_corner_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="sub",
                 bond_id="b", leader_addr="jts.local", crossover_hz=120.0),
        # FIFO present but the SUB_HZ key absent (a stale pre-feature file).
        env_text=f"{OUTPUTD_DAC_CONTENT_FIFO_ENV}={MEMBER_CONTENT_FIFO}\n",
        env_path=env,
    )
    assert r.status == "warn"
    assert "missing while channel=sub" in r.detail


def test_sub_corner_check_ok_when_wired(monkeypatch, tmp_path):
    """The reconciler's own pure derive writes the file → the check passes:
    the two ends of the contract are the same function."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_SUB_HZ_ENV,
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="follower", channel="sub", bond_id="b",
               leader_addr="jts.local", crossover_hz=120.0)
    derived = outputd_grouping_env(cfg)
    assert derived[OUTPUTD_DAC_CONTENT_SUB_HZ_ENV] == "120.0"
    env = tmp_path / "grouping-outputd.env"
    r = _sub_corner_check(
        monkeypatch, cfg=cfg,
        env_text="".join(f"{k}={v}\n" for k, v in derived.items()),
        env_path=env,
    )
    assert r.status == "ok"
    assert "120.0 Hz" in r.detail


def test_outputd_grouping_env_clears_when_not_active():
    """Disable-clears-stale: solo / invalid → the lane keys present as
    empty strings (outputd reads empty as unset → byte-identical solo
    loop) and the bridge key fully OMITTED — never present-but-empty
    (outputd's env_str treats a SET-but-empty bridge mode as invalid and
    bails), and never pinned (solo must fall back to the underlying env
    layers so the lab rate_match soak resumes)."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_CONTENT_BRIDGE_ENV,
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
        assert OUTPUTD_CONTENT_BRIDGE_ENV not in env


def test_outputd_grouping_env_writer_validator_parity():
    """THE jts3 2026-06-11 boot-loop pin (writer/validator coherence):
    whenever the writer arms the FIFO it MUST also pin
    CONTENT_BRIDGE=direct — outputd fail-closes on the FIFO + rate_match
    combination, and systemd composes env from layers, so without the
    pin a lab retune in a lower layer crashes outputd into
    StartLimitAction=reboot. And in NO state may the bridge key be
    present-but-empty (outputd bails on an empty bridge mode)."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_CONTENT_BRIDGE_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        outputd_grouping_env,
    )
    configs = [
        _cfg(),
        _cfg(enabled=True, role="leader", channel="left", bond_id="b"),
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"),
        _cfg(enabled=True, role="", channel="left", bond_id="", error="bad"),
    ]
    for cfg in configs:
        env = outputd_grouping_env(cfg)
        if env[OUTPUTD_DAC_CONTENT_FIFO_ENV]:
            assert env.get(OUTPUTD_CONTENT_BRIDGE_ENV) == "direct", (
                "FIFO armed without the direct-bridge pin — the "
                "guard-rejected layering the jts3 incident hit"
            )
        if OUTPUTD_CONTENT_BRIDGE_ENV in env:
            assert env[OUTPUTD_CONTENT_BRIDGE_ENV] == "direct"


def test_outputd_config_exit_code_contract():
    """The Rust EXIT_CONFIG constant and the unit's
    RestartPreventExitStatus must agree — together they make a
    fail-closed config rejection PARK outputd (visible) instead of
    crash-looping into StartLimitAction=reboot (the jts3 incident's
    escalation path)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    main_rs = (root / "rust/jasper-outputd/src/main.rs").read_text()
    unit = (root / "deploy/systemd/jasper-outputd.service").read_text()
    assert "const EXIT_CONFIG: i32 = 78;" in main_rs
    assert "RestartPreventExitStatus=78" in unit


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
            else "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock\n"
        )
    monkeypatch.setattr(
        groupmod,
        "_resolved_jasper_voice_env",
        lambda: (groupmod._parse_systemd_environment(resolved_voice_text), ""),
    )
    monkeypatch.setattr(recmod, "is_active_speaker_box", lambda: active_box)
    return groupmod.check_grouping_tts_lane()


def test_tts_lane_check_solo_clean_is_ok(monkeypatch):
    assert _tts_lane_check(monkeypatch, cfg=_cfg()).status == "ok"


def test_tts_lane_check_solo_with_stale_override_warns(monkeypatch, tmp_path):
    """A solo speaker carrying a leftover outputd pointer has voice
    writing to a socket nobody serves — silent assistant, must warn."""
    from jasper.multiroom.reconcile import OUTPUTD_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
    r = _tts_lane_check(
        monkeypatch, cfg=_cfg(),
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert "un-armed" in r.detail


def test_tts_lane_check_bonded_without_voice_override_warns(monkeypatch):
    """Bonded but voice still on the fanin path → TTS rides the synced
    stream (the retired PR-1 interim shape): degraded, visible."""
    r = _tts_lane_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    )
    assert r.status == "warn"
    assert "rides the synced stream" in r.detail


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
        tmp_path=tmp_path,
        active_box=True,
    )
    assert r.status == "ok"
    assert "upstream of crossover" in r.detail


def test_tts_lane_check_bonded_unarmed_lane_warns_broken(monkeypatch, tmp_path):
    """The worst drift: voice targets outputd's socket but the lane was
    never armed — assistant voice writes into the void."""
    from jasper.multiroom.reconcile import OUTPUTD_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
    r = _tts_lane_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        outputd_text="# lane keys absent\n",
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert "BROKEN" in r.detail


def test_tts_lane_check_uses_systemd_resolved_voice_socket(monkeypatch, tmp_path):
    """The voice file can look correct while a later unit directive wins.

    The doctor must judge the same resolved env jasper-voice actually
    starts with, not the reconciler-written file in isolation.
    """
    from jasper.multiroom.reconcile import (
        OUTPUTD_TTS_SOCKET,
        VOICE_TTS_SOCKET_ENV,
        outputd_grouping_env,
    )
    cfg = _cfg(enabled=True, role="leader", channel="left", bond_id="b")
    r = _tts_lane_check(
        monkeypatch,
        cfg=cfg,
        voice_text=f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
        resolved_voice_text=f"{VOICE_TTS_SOCKET_ENV}=/run/jasper-fanin/tts.sock\n",
        outputd_text="".join(
            f"{k}={v}\n"
            for k, v in outputd_grouping_env(cfg).items()
        ),
        tmp_path=tmp_path,
    )
    assert r.status == "warn"
    assert "runtime env resolves" in r.detail
    assert "rides the synced stream" in r.detail


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
    assert "member-local TTS wired" in r.detail


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


def test_voice_grouping_env_flips_socket_when_bonded_and_omits_when_solo():
    """PR-2: a passive bonded member's voice plays TTS via outputd
    (post-round-trip); solo and active endpoints OMIT the key entirely so voice
    falls back to fan-in upstream of the crossover."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_TTS_SOCKET,
        VOICE_PARK_ENV,
        VOICE_TTS_SOCKET_ENV,
        voice_grouping_env,
    )
    leader = voice_grouping_env(
        _cfg(enabled=True, role="leader", channel="left", bond_id="b"))
    assert leader == {VOICE_TTS_SOCKET_ENV: OUTPUTD_TTS_SOCKET}
    # A FOLLOWER additionally carries the dumb-follower park flag (the
    # validated signal jasper-aec-reconcile gates voice/AEC parking on);
    # the socket stays armed so a promotion to leader un-parks with the
    # right playout target already set.
    follower = voice_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local"))
    assert follower == {
        VOICE_TTS_SOCKET_ENV: OUTPUTD_TTS_SOCKET,
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
    from jasper.multiroom.reconcile import (
        OUTPUTD_TTS_SOCKET,
        OUTPUTD_TTS_SOCKET_ENV,
        outputd_grouping_env,
    )
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
    assert _pair_channels_check(monkeypatch, cfg=_cfg()).status == "ok"
    assert _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="leader", channel="left", bond_id="b"),
    ).status == "ok"


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
    assert "BOTH speakers" in r.detail
    assert "Swap" in r.detail  # remediation is one tap


def test_pair_channels_check_ok_when_coherent(monkeypatch):
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_payload={"bond_id": "b", "channel": "left"},
    )
    assert r.status == "ok"
    assert "coherent" in r.detail


def test_pair_channels_check_unreachable_leader_defers_to_health(monkeypatch):
    """Connectivity already has an owner (grouping health) — this check
    must not double-report one root cause."""
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_error=OSError("no route"),
    )
    assert r.status == "ok"
    assert "could not compare" in r.detail


def test_pair_channels_check_warns_on_bond_mismatch(monkeypatch):
    r = _pair_channels_check(
        monkeypatch,
        cfg=_cfg(enabled=True, role="follower", channel="right",
                 bond_id="b", leader_addr="jts.local"),
        leader_payload={"bond_id": "OTHER", "channel": "left"},
    )
    assert r.status == "warn"
    assert "re-pair" in r.detail


def test_outputd_grouping_env_carries_the_trim():
    """Bonded: the validated trim derives into the outputd lane env
    (always written, so a cleared trim converges to 0.0); solo clears
    with EMPTY (outputd's env_f32 reads empty as unset -> default 0)."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_TRIM_ENV,
        outputd_grouping_env,
    )
    bonded = outputd_grouping_env(
        _cfg(enabled=True, role="follower", channel="right",
             bond_id="b", leader_addr="jts.local", trim_db=-2.5))
    assert bonded[OUTPUTD_DAC_CONTENT_TRIM_ENV] == "-2.5"
    solo = outputd_grouping_env(_cfg())
    assert solo[OUTPUTD_DAC_CONTENT_TRIM_ENV] == ""
