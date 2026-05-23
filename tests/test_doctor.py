"""Unit tests for jasper-doctor's env loading, provider-aware key
check, and ALSA mic-card lookup. Hardware-side checks (sounddevice,
systemctl, arecord, etc) are exercised on the Pi via
``jasper-doctor`` itself; this file pins the pure-python helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jasper.cli import doctor
from jasper.config import Config


# ---------------------------------------------------------------- env loading


def test_parse_env_file_basic(tmp_path: Path):
    p = tmp_path / "jasper.env"
    p.write_text(
        "# comment line\n"
        "\n"
        "GEMINI_API_KEY=AIzaSyABC\n"
        "JASPER_VOICE_PROVIDER=openai\n"
        'OPENAI_API_KEY="sk-quoted"\n'
        "EMPTY=\n"
        "  WHITESPACE_KEY  =  trimmed  \n"
    )
    out = doctor._parse_env_file(str(p))
    assert out["GEMINI_API_KEY"] == "AIzaSyABC"
    assert out["JASPER_VOICE_PROVIDER"] == "openai"
    assert out["OPENAI_API_KEY"] == "sk-quoted"
    assert out["EMPTY"] == ""
    assert out["WHITESPACE_KEY"] == "trimmed"


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    out = doctor._parse_env_file(str(tmp_path / "does-not-exist"))
    assert out == {}


def test_load_env_files_wizard_overrides_operator(monkeypatch, tmp_path: Path):
    """`/var/lib/jasper/voice_provider.env` (wizard) must override
    `/etc/jasper/jasper.env` (operator) — same precedence as the
    systemd unit's `EnvironmentFile=` ordering. Verified via the
    explicit-paths form of `load_env_files` so test fixtures don't
    have to monkeypatch a module-level constant."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text(
        "GEMINI_API_KEY=op-key\n"
        "JASPER_VOICE_PROVIDER=gemini\n"
    )
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text(
        "OPENAI_API_KEY=wiz-key\n"
        "JASPER_VOICE_PROVIDER=openai\n"
    )
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "JASPER_VOICE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    load_env_files((str(operator), str(wizard)))

    assert os_environ_get("GEMINI_API_KEY") == "op-key"
    assert os_environ_get("OPENAI_API_KEY") == "wiz-key"
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_load_env_files_shell_wins_over_files(monkeypatch, tmp_path: Path):
    """A var already in the calling shell must NOT be overwritten by
    the env files. Lets an operator probe with `FOO=bar jasper-doctor`."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")

    load_env_files((str(operator),))
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def os_environ_get(name: str) -> str | None:
    import os
    return os.environ.get(name)


# -------------------------------------------------- provider-aware key check


def _fresh_cfg(monkeypatch, **vars_) -> Config:
    """Build a Config with only the requested env vars set.

    Defaults JASPER_VOICE_PROVIDER=gemini so callers that only care
    about a single provider's key can omit it. Pass the var explicitly
    to override (e.g. testing the openai or grok path).
    """
    drop = [
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER", "JASPER_GEMINI_MODEL",
        "SPOTIFY_CLIENT_ID",
    ]
    for v in drop:
        monkeypatch.delenv(v, raising=False)
    defaults = {"JASPER_VOICE_PROVIDER": "gemini"}
    for k, v in {**defaults, **vars_}.items():
        monkeypatch.setenv(k, v)
    return Config.from_env()


def test_provider_key_gemini_ok(monkeypatch):
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaABCDEF12345")
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "GEMINI_API_KEY"


def test_provider_key_openai_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "OPENAI_API_KEY"


def test_provider_key_grok_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="grok",
        XAI_API_KEY="xai-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "XAI_API_KEY"


def test_provider_key_warns_on_wrong_prefix(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "warn"


def test_provider_key_other_providers_keys_unchecked(monkeypatch):
    """Active=openai. GEMINI_API_KEY is intentionally unset; the doctor
    must NOT flag that as a problem — gemini is dormant."""
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"


# ------------------------------------------------ ALSA shorthand mic lookup


def test_extract_card_name_returns_none_for_shorthand():
    assert doctor._extract_card_name("hw:7,1") is None
    assert doctor._extract_card_name("plughw:0,0") is None


def test_extract_card_name_named_card_passthrough():
    assert doctor._extract_card_name("Array") == "Array"
    assert doctor._extract_card_name("plughw:CARD=Loopback") == "Loopback"


def test_check_arecord_l_card_device_match():
    """Mock arecord -l output for a 6-card system that includes the
    LoopbackAEC bridge target (card 7, device 1)."""
    fake_output = (
        "card 0: dongle [USB Audio], device 0: USB Audio [USB Audio]\n"
        "card 1: Array [XVF3800 Voice Capture], device 0: USB Audio\n"
        "card 6: Loopback [Loopback], device 0: Loopback PCM\n"
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is True
        assert doctor._check_arecord_l_card_device(7, 0) is True
        assert doctor._check_arecord_l_card_device(99, 0) is False


def test_check_arecord_l_does_not_match_wrong_card():
    """`device 1:` paired with card 6 must NOT satisfy a query for
    card 7 device 1 — both numbers must come from the same line."""
    fake_output = (
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is False


def test_check_mic_card_routes_shorthand_through_arecord_l(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = (
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "ok"
    assert "card 7 device 1 present" in r.detail


def test_check_mic_capture_falls_back_to_daemon_active(monkeypatch):
    """When PortAudio refuses to open the mic AND jasper-voice is
    running, the check returns ok with a 'daemon holds device' note
    instead of a spurious fail. This is the snd-aloop / AEC bridge
    case where the daemon owns the capture handle exclusively."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    fake_sd = FakeSD()

    def fake_import(*args, **kwargs):
        if args and args[0] == "sounddevice":
            return fake_sd
        return __import__(*args, **kwargs)

    # Use a sd-stub by monkeypatching the import inside the function.
    # Easier: patch a wrapper. Instead, patch _jasper_voice_active and
    # mock sd.rec via injecting into sys.modules.
    import sys
    sys.modules["sounddevice"] = fake_sd
    try:
        with patch.object(doctor, "_jasper_voice_active", return_value=True):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "ok"
        assert "skipped" in r.detail
        assert "jasper-voice holds" in r.detail
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_capture_fails_hard_when_daemon_inactive(monkeypatch):
    """If jasper-voice ISN'T running and the open still fails, the
    fail is real — the device is missing or misconfigured."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    import sys
    sys.modules["sounddevice"] = FakeSD()
    try:
        with patch.object(doctor, "_jasper_voice_active", return_value=False):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "fail"
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_card_shorthand_failure_actionable(monkeypatch):
    """When the shorthand points at a card/device that's missing, the
    failure detail must mention the AEC bridge — that's the most
    common cause (bridge disabled but JASPER_MIC_DEVICE still set)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = "card 0: dongle [USB Audio], device 0: USB Audio\n"
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "fail"
    assert "AEC bridge" in r.detail


# --------------------------------------------- AEC bridge output assessment


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees. Helper for the _assess_aec_bridge_output tests
    below."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )


def test_assess_aec_output_empty_journal_is_ok():
    """No rms lines = bridge probably just restarted in the assessment
    window. Not a failure, just nothing to evaluate."""
    r = doctor._assess_aec_bridge_output("")
    assert r.status == "ok"
    assert "no recent rms windows" in r.detail.lower()


def test_assess_aec_output_idle_returns_ok():
    """Mic and ref both quiet — speaker has been idle, no music has
    played. Doctor must NOT flag this as a degradation."""
    lines = [_rms_log_line(ref=0, mic=200, aec=30, attn_db=-16.5) for _ in range(10)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "no music activity" in r.detail.lower()


def test_assess_aec_output_silent_ref_with_no_healthy_window_fails():
    """The PR #75 dsnoop rate-lock signature: mic shows music acoustically
    throughout, ref delivers silence throughout, ZERO windows prove the
    ref chain ever worked in this period. The check MUST fail — this is
    the regression we exist to catch."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "fail"
    assert "reference path is delivering silence" in r.detail
    assert "Lessons learned" in r.detail  # actionable doc link


def test_assess_aec_output_silent_ref_downgrades_when_loopback_closed():
    """Same mic-loud + ref-silent shape as the rate-lock fail, but the
    music chain isn't active (no renderer writing the loopback). In
    that case ref MUST be silent — snd-aloop produces zeros without a
    producer — and the mic-loud bursts are TTS or voice (both bypass
    the loopback). Downgrade to OK with the diagnosis so a pure-voice
    session doesn't show as a degraded AEC bridge."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output(
        "\n".join(lines), music_chain_active=False,
    )
    assert r.status == "ok"
    assert "loopback playback is closed" in r.detail
    assert "jasper_out bypasses the loopback" in r.detail
    # Counterpart: when music chain IS active, same input still fails —
    # the guard only relaxes the FAIL when we have positive evidence
    # the loopback is idle, not on uncertainty.
    r_active = doctor._assess_aec_bridge_output(
        "\n".join(lines), music_chain_active=True,
    )
    assert r_active.status == "fail"


def test_assess_aec_output_silent_ref_with_healthy_window_is_ok():
    """The 2026-05-16 false-positive: TTS / wake cues / loud ambient
    push silent_ref over threshold, but at least one window in the
    assessment period has ref signal (proving the chain works). The
    check must NOT fail — silent-ref windows have benign explanations
    when the ref path is demonstrably alive."""
    lines = [
        # 5 mic-loud + ref-silent windows (TTS bypasses the loopback)
        _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2400, aec=2300, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2600, aec=2500, attn_db=-0.3),
        _rms_log_line(ref=0, mic=2100, aec=2050, attn_db=-0.2),
        _rms_log_line(ref=0, mic=2300, aec=2250, attn_db=-0.2),
        # 2 windows where music played and ref captured it correctly
        _rms_log_line(ref=800, mic=2400, aec=200, attn_db=-21.6),
        _rms_log_line(ref=1100, mic=2800, aec=180, attn_db=-23.8),
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "likely TTS or ambient" in r.detail
    assert "ref path proven healthy" in r.detail


def test_assess_aec_output_healthy_aec_work_is_ok():
    """Music playing through the loopback, ref strong, attenuation
    meaningful — the bridge is doing its job. ok with a summary."""
    lines = [_rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "real AEC work" in r.detail


def test_assess_aec_output_drift_warnings_warn():
    """High count of `drained N stale ref frames (drift)` warnings
    indicates ref/mic clock skew or rate mismatch. Warn, don't fail."""
    drift_line = (
        "2026-05-16 17:00:00,000 aec-bridge WARNING "
        "drained 7 stale ref frames (drift)"
    )
    # Threshold is 30 in 5 min; 40 is comfortably over.
    journal = "\n".join([drift_line] * 40)
    r = doctor._assess_aec_bridge_output(journal)
    assert r.status == "warn"
    assert "ref-drift warnings" in r.detail


def test_assess_aec_output_single_healthy_window_suffices():
    """Boundary: exactly one healthy_ref window flips the silent-ref
    pattern from fail to ok. Documents the design choice — if the ref
    chain proved itself once in the window, we trust it."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(7)]
    lines.append(_rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0))
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"


def test_assess_aec_output_silent_ref_below_alarm_surfaces_in_summary():
    """When silent_ref_count is 1-4 (non-zero but below the fail
    threshold of 5), the OK summary appends a `silent-ref=N` note so
    intermittent ref glitches are visible before they tip into a real
    outage. Per PR #124 upstream — preserved in the refactor."""
    lines = [_rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(6)]
    # 3 mic-loud + ref-silent windows: above 0 but below the 5-count alarm.
    lines += [_rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4) for _ in range(3)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "silent-ref=3" in r.detail
    assert "below alarm" in r.detail


def test_loopback_playback_active_reads_proc_status(tmp_path):
    """Helper must report True for any non-closed subdev and False when
    every subdev is closed. Verifies the first-line strip-and-compare
    against the actual /proc/asound status file format (single word
    `closed` vs `state: RUNNING\\n…`)."""
    fake_root = tmp_path / "asound" / "Loopback" / "pcm0p"
    fake_root.mkdir(parents=True)
    sub_paths = []
    for sub in range(4):
        d = fake_root / f"sub{sub}"
        d.mkdir()
        status = d / "status"
        status.write_text("closed\n")
        sub_paths.append(str(status))

    with patch("glob.glob", return_value=sub_paths):
        # All closed → inactive.
        assert doctor._loopback_playback_active() is False
        # Flip sub2 to RUNNING → active.
        (fake_root / "sub2" / "status").write_text(
            "state: RUNNING\nowner_pid   : 12345\n"
        )
        assert doctor._loopback_playback_active() is True

    # No status files at all (e.g., snd-aloop not loaded) → inactive,
    # never raises.
    with patch("glob.glob", return_value=[]):
        assert doctor._loopback_playback_active() is False


# ---------------------------------------------------- peering doctor checks


def test_check_peering_mode_no_file_returns_ok_default(monkeypatch, tmp_path):
    """When /var/lib/jasper/peering.env doesn't exist, peering is off
    by design — the default. Doctor should return ok with a hint."""
    fake = tmp_path / "peering.env"  # does not exist
    with patch("jasper.cli.doctor.Path", side_effect=lambda p: fake if "peering.env" in p else Path(p)):
        r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_off_explicit(tmp_path, monkeypatch):
    """Explicit JASPER_PEERING=off — same ok status, slightly different
    message (operator made the choice deliberately)."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=off\n")
    monkeypatch.setattr("jasper.cli.doctor.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_on(tmp_path, monkeypatch):
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    monkeypatch.setattr("jasper.cli.doctor.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "on" in r.detail.lower()


def test_check_peering_mode_garbage_warns(tmp_path, monkeypatch):
    """A malformed value warns the user — silent failure here would let
    a typo (JASPER_PEERING=onn) leave the user thinking peering is on
    when it actually resolved to off."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=banana\n")
    monkeypatch.setattr("jasper.cli.doctor.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "warn"
    assert "banana" in r.detail


def test_check_peering_discovery_no_peers(monkeypatch):
    """avahi-browse returns no peers — single-device mode (ok)."""
    fake_output = "+ eth0 IPv4 SomeOtherService _foo._tcp local\n"
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "0 sibling" in r.detail


def test_check_peering_discovery_sees_siblings(monkeypatch, tmp_path):
    """avahi-browse returns two siblings — count them, exclude self."""
    fake_output = (
        '+ eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n'
        '= eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n'
        '  hostname = [alice.local]\n'
        '  txt = ["peer_id=alice-uuid" "room=kitchen" "primary=1" "proto=1"]\n'
        '+ eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n'
        '= eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n'
        '  hostname = [bob.local]\n'
        '  txt = ["peer_id=bob-uuid" "room=bedroom" "primary=0" "proto=1"]\n'
    )
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    # Pretend we're alice — filter ourselves out.
    monkeypatch.setattr("jasper.cli.doctor._local_peer_id", lambda: "alice-uuid")
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "1 sibling" in r.detail
    assert "bob-uuid" in r.detail


def test_check_peering_discovery_no_avahi_browse_warns(monkeypatch):
    """Without avahi-browse we can't verify discovery — warn but
    don't fail (it's an optional dep)."""
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: None)
    r = doctor.check_peering_discovery()
    assert r.status == "warn"


# -------------------------------------------------- check_citibike


def _citibike_cfg(monkeypatch, *, stations: str = "", ebike_only: str = "") -> Config:
    """Fresh Config with only the citibike + voice-provider env vars set.

    Drops every JASPER_CITIBIKE_* from the calling shell so the test
    picks up only the values we pass, then sets a minimal voice
    provider config so `Config.from_env()` doesn't trip the
    JASPER_VOICE_PROVIDER-not-set RuntimeError."""
    for var in (
        "JASPER_CITIBIKE_STATIONS", "JASPER_CITIBIKE_EBIKE_ONLY",
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-stub")
    if stations:
        monkeypatch.setenv("JASPER_CITIBIKE_STATIONS", stations)
    if ebike_only:
        monkeypatch.setenv("JASPER_CITIBIKE_EBIKE_ONLY", ebike_only)
    return Config.from_env()


def test_check_citibike_skips_when_not_configured(monkeypatch):
    cfg = _citibike_cfg(monkeypatch)  # no stations saved
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "not configured" in r.detail


def test_check_citibike_ok_when_all_saved_ids_resolve(monkeypatch):
    """Saved stations all present in GBFS → ok with the count."""
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": [
        {"station_id": "abc"}, {"station_id": "def"},
    ]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av,def|Atlantic",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "2 saved station" in r.detail
    assert "e-bike-only mode" not in r.detail


def test_check_citibike_ok_renders_ebike_only_suffix(monkeypatch):
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": [{"station_id": "abc"}]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av", ebike_only="1",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "e-bike-only mode" in r.detail


def test_check_citibike_warns_when_some_saved_ids_missing(monkeypatch):
    """One saved station retired by Lyft → warn naming the affected
    station, but don't fail (the OK ones still work)."""
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": [{"station_id": "abc"}]}}  # def is gone
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av,def|Gone Station",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "Gone Station" in r.detail
    assert "1/2" in r.detail


def test_check_citibike_fails_when_gbfs_unreachable(monkeypatch):
    import jasper.citibike as citibike_mod

    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")
    r = doctor.check_citibike(cfg)
    assert r.status == "fail"
    assert "GBFS unreachable" in r.detail


def test_check_citibike_caps_missing_list_at_three_with_suffix(monkeypatch):
    """When > 3 stations are missing, the detail names the first 3 and
    appends a '+N more' suffix so the line stays scannable."""
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": []}}  # everything retired
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="a|A,b|B,c|C,d|D,e|E",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "+2 more" in r.detail


# ---- shairport-sync.conf output_device check ---------------------------

def _patch_shairport_conf(monkeypatch, conf_text: str, tmp_path: Path):
    """Have the doctor read a synthetic shairport-sync.conf instead of
    /etc/shairport-sync.conf. The function takes no args and hardcodes
    the path, so we substitute the `Path` constructor at the module
    level via a thin shim."""
    target = tmp_path / "shairport-sync.conf"
    target.write_text(conf_text)
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return target
        return real_path_cls(arg)

    monkeypatch.setattr(doctor, "Path", fake_path)


def test_shairport_check_jasper_renderer_in_is_ok(monkeypatch, tmp_path):
    """Canonical post-PR-#214 wiring: output_device targets the dmix
    front-end. Doctor should report `ok`."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "jasper_renderer_in";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"
    assert "jasper_renderer_in" in r.detail


def test_shairport_check_legacy_plughw_warns_with_redeploy_hint(
    monkeypatch, tmp_path,
):
    """Pre-PR-#214 wiring: output_device still points at the bare
    loopback. Doctor warns and tells the user to redeploy. This is
    the legacy-but-functional path, not a hard failure."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "plughw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert "plughw:Loopback" in r.detail
    assert "redeploy" in r.detail.lower() or "deploy-to-pi" in r.detail


def test_shairport_check_raw_hw_loopback_fails(monkeypatch, tmp_path):
    """Raw `hw:Loopback,0,0` bypasses plug entirely. shairport requests
    44.1 kHz and snd-aloop is locked at 48 kHz → silent rejection.
    This is the hard-fail case."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "hw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "fail"


def test_shairport_check_missing_output_device_warns(monkeypatch, tmp_path):
    """A conf without an output_device line at all means shairport is
    using its own default — almost certainly wrong on this host."""
    _patch_shairport_conf(
        monkeypatch, 'alsa = {\n    output_rate = 44100;\n};\n', tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert "no `output_device`" in r.detail


def test_shairport_check_comments_ignored(monkeypatch, tmp_path):
    """// comments referencing plughw:Loopback (e.g. PR-history notes
    in the template) must not bait the check into reporting `ok` when
    the active line says something else."""
    conf = (
        "alsa = {\n"
        '    // Pre-2026-05-22 this was plughw:Loopback,0,0 directly\n'
        '    output_device = "jasper_renderer_in";\n'
        "};\n"
    )
    _patch_shairport_conf(monkeypatch, conf, tmp_path)
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"


# ---- renderer ALSA device resolvable (PR #223 — the bug-class catch) ---

# These tests mock the parse helpers + the systemd-user lookup + the
# probe subprocess. They don't actually shell out — we're testing the
# orchestration, not aplay. The integration angle (does aplay actually
# open the device?) only meaningfully runs on the Pi via `jasper-doctor`.

def test_renderer_resolvable_all_ok(monkeypatch):
    """Happy path: every renderer has a discoverable device and the
    probe succeeds for each."""
    monkeypatch.setattr(doctor, "_renderer_device_shairport",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_renderer_device_librespot",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_renderer_device_bluealsa",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_systemd_user_for",
                        lambda unit: {
                            "shairport-sync.service": "shairport-sync",
                            "librespot.service": "pi",
                            "bluealsa-aplay.service": None,  # root
                        }[unit])
    monkeypatch.setattr(doctor, "_probe_open_as_user",
                        lambda dev, user: (True, ""))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert "shairport-sync(shairport-sync)→jasper_renderer_in" in r.detail
    assert "librespot(pi)→jasper_renderer_in" in r.detail
    assert "bluealsa-aplay(root)→jasper_renderer_in" in r.detail


def test_renderer_resolvable_catches_pr214_regression(monkeypatch):
    """The exact bug PR #223 fixes: configs look right, services look
    active, but shairport-sync's runtime user can't open the device.
    Pre-#223 the doctor missed this entirely. This test pins that the
    new check would have caught it."""
    monkeypatch.setattr(doctor, "_renderer_device_shairport",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_renderer_device_librespot",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_renderer_device_bluealsa",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_systemd_user_for",
                        lambda unit: {
                            "shairport-sync.service": "shairport-sync",
                            "librespot.service": "pi",
                            "bluealsa-aplay.service": None,
                        }[unit])

    # Simulate the bug: as shairport-sync user, the open fails with
    # the canonical "Unknown PCM" pattern. Root + pi (somehow) succeed
    # — only shairport-sync fails. Doctor must still fail-the-check.
    def fake_probe(dev, user):
        if user == "shairport-sync":
            return (False, 'ALSA lib pcm.c:2722: Unknown PCM jasper_renderer_in')
        return (True, "")
    monkeypatch.setattr(doctor, "_probe_open_as_user", fake_probe)

    r = doctor.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert "shairport-sync" in r.detail
    assert "Unknown PCM" in r.detail
    # The actionable hint should mention the fix path.
    assert "/etc/asound.conf" in r.detail


def test_renderer_resolvable_fail_includes_user_in_detail(monkeypatch):
    """Failure details must name the failing user — that's the key
    diagnostic for any "device works as root, fails as non-root" bug
    of which the PR #214 regression is the canonical example."""
    monkeypatch.setattr(doctor, "_renderer_device_shairport",
                        lambda: "weird-device")
    monkeypatch.setattr(doctor, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor, "_probe_open_as_user",
                        lambda d, u: (False, "open failed"))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert "(shairport-sync)" in r.detail


def test_renderer_resolvable_skips_missing_renderers(monkeypatch):
    """A stripped image without all renderers installed should
    `ok` for what works, `warn` only if nothing was probeable."""
    monkeypatch.setattr(doctor, "_renderer_device_shairport",
                        lambda: "jasper_renderer_in")
    monkeypatch.setattr(doctor, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor, "_probe_open_as_user",
                        lambda d, u: (True, ""))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert "shairport-sync" in r.detail
    # Skipped renderers should be mentioned (informational).
    assert "skipped" in r.detail.lower()


def test_renderer_resolvable_no_renderers_at_all_is_warn(monkeypatch):
    """If literally nothing is configured, no audio path exists —
    surface as warn, not fail (could be a doctor-only image)."""
    monkeypatch.setattr(doctor, "_renderer_device_shairport", lambda: None)
    monkeypatch.setattr(doctor, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor, "_renderer_device_bluealsa", lambda: None)
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "warn"


# ---- renderer device parsers ----------------------------------------

def test_parse_shairport_device_from_conf(tmp_path, monkeypatch):
    """shairport-sync.conf uses libconfig syntax. Parser must handle
    double quotes, leading whitespace, and ignore // comments."""
    conf = tmp_path / "shairport-sync.conf"
    conf.write_text(
        "alsa = {\n"
        '    // Pre-2026-05-23 this was plughw:Loopback,0,0\n'
        '    output_device = "jasper_renderer_in";\n'
        "};\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return conf
        return real_path_cls(arg)

    monkeypatch.setattr(doctor, "Path", fake_path)
    assert doctor._renderer_device_shairport() == "jasper_renderer_in"


def test_parse_librespot_device_from_systemd_unit(tmp_path, monkeypatch):
    """librespot.service has a multi-line ExecStart= with backslash
    continuations. Parser must handle line joining and grab --device."""
    unit = tmp_path / "librespot.service"
    unit.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/librespot \\\n"
        "    --name JTS \\\n"
        "    --backend alsa \\\n"
        "    --device jasper_renderer_in \\\n"
        "    --format S24_3\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/librespot.service":
            return unit
        return real_path_cls(arg)

    monkeypatch.setattr(doctor, "Path", fake_path)
    assert doctor._renderer_device_librespot() == "jasper_renderer_in"


def test_parse_bluealsa_device_from_dropin(tmp_path, monkeypatch):
    """bluealsa-aplay's device is configured via a drop-in's --pcm= flag."""
    dropin_dir = tmp_path / "bluealsa-aplay.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "jts-output.conf"
    dropin.write_text(
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=/usr/bin/bluealsa-aplay -S --pcm=jasper_renderer_in\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf":
            return dropin
        # The other candidate (override.conf) should not exist for this test.
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/override.conf":
            return tmp_path / "does-not-exist"
        return real_path_cls(arg)

    monkeypatch.setattr(doctor, "Path", fake_path)
    assert doctor._renderer_device_bluealsa() == "jasper_renderer_in"
