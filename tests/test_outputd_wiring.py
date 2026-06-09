"""Static checks for the outputd topology."""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _non_comment(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _pcm_block(text: str, name: str) -> str:
    start = text.index(f"pcm.{name}")
    tail = text[start:]
    next_def = re.search(r"^(?:pcm|ctl)\.", tail[len(f"pcm.{name}"):], re.MULTILINE)
    if next_def:
        return tail[:len(f"pcm.{name}") + next_def.start()]
    return tail


def test_asoundrc_declares_outputd_post_dsp_lane_without_dsnoop():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    playback = _pcm_block(rc, "outputd_content_playback")
    capture = _pcm_block(rc, "outputd_content_capture")
    assert "type plug" in playback
    assert 'pcm "hw:Loopback,0,6"' in playback
    assert "type plug" in capture
    assert 'pcm "hw:Loopback,1,6"' in capture
    assert "type dsnoop" not in capture


def test_asoundrc_declares_outputd_active_four_channel_lane():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    playback = _pcm_block(rc, "outputd_active_content_playback")
    capture = _pcm_block(rc, "outputd_active_content_capture")
    assert "type plug" in playback
    assert 'pcm "hw:Loopback,0,5"' in playback
    assert "channels 4" in playback
    assert "type plug" in capture
    assert 'pcm "hw:Loopback,1,5"' in capture
    assert "channels 4" in capture
    assert "type dsnoop" not in capture


def test_asoundrc_declares_outputd_rendered_dac_alias_placeholder():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    assert "__OUTPUTD_DAC_PCM_BLOCK__" in rc
    assert "ctl.outputd_dac" in rc
    assert "card __OUTPUT_DAC_CARD__" in rc


def test_install_prefers_dac8x_for_outputd_without_reusing_dongle_mixer_card():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert "select_audio_hardware_roles()" in install_sh
    assert "jasper-audio-hardware-reconcile\" --print-env" in install_sh
    assert "DAC8X_OUTPUT_CARD=" in reconcile
    assert "DAC8X_STUDIO_OUTPUT_CARD=" in reconcile
    assert 'OUTPUT_DAC_ID="hifiberry_dac8x"' in reconcile
    assert 'OUTPUT_DAC_ID="hifiberry_dac8x_studio"' in reconcile
    assert 'OUTPUT_DAC_ID="apple_usb_c_dongle"' in reconcile
    assert "snd_rpi_hifiberry_dac8x" in reconcile
    assert "hifiberry_dac8x" in reconcile
    assert 'echo "  Output DAC: CARD=${OUTPUT_DAC_CARD}"' in install_sh
    assert 'echo "  Output DAC id: ${OUTPUT_DAC_ID}"' in install_sh
    assert "jasper_asound_render_template" in install_sh
    assert "asoundrc.jasper.source" in install_sh
    assert "JASPER_AUDIO_DAC_ID" in install_sh
    assert "JASPER_AUDIO_DAC_CARD" in reconcile
    assert "JASPER_OUTPUT_DAC_ROUTE" in reconcile
    assert "OUTPUT_DAC_ROUTE" in install_sh
    assert "APPLE_DONGLE_PRESENT=1" in reconcile
    assert "APPLE_DONGLE_PRESENT=0" in reconcile
    assert 'APPLE_DONGLE_SERVICE_CARD="auto"' in reconcile


def test_output_dac_route_policy_is_narrow_and_dac8x_family_only():
    route_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert 'OUTPUT_DAC_ID" != "hifiberry_dac8x"' in route_lib
    assert 'OUTPUT_DAC_ID" != "hifiberry_dac8x_studio"' in route_lib
    assert "mono:([1-8])" in route_lib
    assert "stereo:([1-8]),([1-8])" in route_lib
    assert "channels 8" in route_lib
    assert "0.${mono_idx} 0.5" in route_lib
    assert "1.${mono_idx} 0.5" in route_lib
    assert "duplicate_stereo_channel" in route_lib
    assert 'OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch"' in route_lib
    assert "type null" in route_lib
    assert "jasper_asound_route_ignored()" in reconcile
    assert "event=audio_hardware_reconcile.${name}" in reconcile


def test_apple_dongle_mixer_services_are_enabled_only_for_apple_output_role():
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    gated = reconcile.split(
        'if [[ ( "$OUTPUT_DAC_ID" == "apple_usb_c_dongle" || "$OUTPUT_DAC_ID" == "dual_apple_usb_c_dac_4ch" ) && "$APPLE_DONGLE_PRESENT" == "1" ]]; then',
        1,
    )[1].split("restart_audio_if_needed()", 1)[0]
    assert '"$SYSTEMCTL" enable jasper-dac-init.service jasper-headphone-monitor.service' in gated
    assert '"$SYSTEMCTL" start jasper-dac-init.service' in gated
    assert '"$SYSTEMCTL" restart jasper-headphone-monitor.service' in gated
    assert '"$SYSTEMCTL" disable --now jasper-dac-init.service jasper-headphone-monitor.service' in gated
    assert '"$SYSTEMCTL" reset-failed jasper-dac-init.service jasper-headphone-monitor.service' in gated
    assert "output_dac_id=${OUTPUT_DAC_ID}" in gated


def test_apple_dongle_helpers_use_runtime_safe_card_template():
    init_unit = (REPO / "deploy" / "systemd" / "jasper-dac-init.service").read_text()
    unit = (REPO / "deploy" / "systemd" / "jasper-headphone-monitor.service").read_text()
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    init_script = (REPO / "deploy" / "bin" / "jasper-dac-init").read_text()
    monitor_script = (REPO / "deploy" / "bin" / "jasper-headphone-monitor").read_text()
    assert "ExecStart=/usr/local/bin/jasper-dac-init __APPLE_DONGLE_CARD__ Headphone" in init_unit
    assert "ExecStart=/usr/local/bin/jasper-headphone-monitor __APPLE_DONGLE_CARD__ Headphone" in unit
    assert 's/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g' in install_sh
    assert 'CONFIGURED_CARD="${1:-auto}"' in init_script
    assert 'CONFIGURED_CARD="${1:-auto}"' in monitor_script
    assert "event=apple_dongle.dac_init.skip" in init_script
    assert "event=apple_dongle.headphone_monitor.absent" in monitor_script


def test_apple_dongle_udev_rule_escapes_literal_headphone_percent():
    rule = (REPO / "deploy" / "udev" / "99-jasper-apple-dongle.rules").read_text()
    run_line = next(
        line
        for line in rule.splitlines()
        if "RUN+=" in line and not line.lstrip().startswith("#")
    )

    assert "100%% unmute" in run_line
    assert "100% unmute" not in run_line


def test_audio_hardware_reconciler_is_installed_and_udev_triggered():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    unit = (REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service").read_text()
    rule = (REPO / "deploy" / "udev" / "99-jasper-audio-hardware-reconcile.rules").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    startup_load = (REPO / "jasper" / "active_speaker" / "startup_load.py").read_text()
    assert "deploy/systemd/jasper-audio-hardware-reconcile.service" in install_sh
    assert "deploy/bin/jasper-audio-hardware-reconcile" in install_sh
    assert "deploy/lib/jasper-asound-render.sh" in install_sh
    assert "/usr/local/lib/jasper/jasper-asound-render.sh" in install_sh
    assert "99-jasper-audio-hardware-reconcile.rules" in install_sh
    assert "ExecStart=/usr/local/sbin/jasper-audio-hardware-reconcile --reason systemd" in unit
    assert "Before=jasper-outputd.service" in unit
    before_line = next(
        line for line in unit.splitlines() if line.startswith("Before=")
    )
    assert "jasper-dac-init.service" not in before_line
    assert "jasper-headphone-monitor.service" not in before_line
    assert 'ACTION=="add|remove|change", SUBSYSTEM=="sound", KERNEL=="controlC*"' in rule
    assert 'ENV{SYSTEMD_WANTS}+="jasper-audio-hardware-reconcile.service"' in rule
    assert "/usr/local/sbin/jasper-audio-hardware-reconcile --reason install" in install_sh
    assert "dual_apple_active_graph_status()" in reconcile
    assert "action=park_until_active_graph" in reconcile
    assert 'JASPER_OUTPUTD_BACKEND" "fake"' in reconcile
    assert "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE" in reconcile
    assert "JASPER_CAMILLA_STATEFILE" in reconcile
    assert "outputd_active_content_playback" in reconcile
    assert "AUDIO_HARDWARE_RECONCILE_UNIT" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_load\")" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_rollback\")" in startup_load


def test_voice_tts_socket_is_canonical_fanin_path():
    unit = (REPO / "deploy" / "systemd" / "jasper-voice.service").read_text()
    assert 'Environment="JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock"' in unit
    assert 'Environment="JASPER_DUCK_TRANSPORT=fanin"' in unit
    assert "EnvironmentFile=-/var/lib/jasper/tts.env" not in unit


def test_fanin_exposes_outputd_compatible_tts_socket():
    main_rs = (REPO / "rust" / "jasper-fanin" / "src" / "main.rs").read_text()
    config_rs = (REPO / "rust" / "jasper-fanin" / "src" / "config.rs").read_text()
    tts_rs = (REPO / "rust" / "jasper-fanin" / "src" / "tts.rs").read_text()
    mixer_rs = (REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text()
    outputd_main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    outputd_config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    outputd_lib_rs = (REPO / "rust" / "jasper-outputd" / "src" / "lib.rs").read_text()
    assert '"/run/jasper-fanin/tts.sock"' in config_rs
    assert "spawn_tts_server(" in main_rs
    assert "spawn_tts_server(" not in outputd_main_rs
    assert "handle_tts_client(" not in outputd_main_rs
    assert "JASPER_OUTPUTD_TTS_SOCKET" not in outputd_config_rs
    assert "pub mod protocol;" not in outputd_lib_rs
    assert "TtsCommand::FlushSync" in tts_rs
    assert "TtsCommand::ProgramDuckOn" in tts_rs
    assert '"PROGRAM_DUCK_ON"' in tts_rs
    assert "prepare_period()" in mixer_rs
    assert "mix_into_with_gain" in mixer_rs
    assert "program_gain" in mixer_rs
    assert "AUDIO byte length must contain whole stereo frames" in tts_rs
    assert "tts.mix_period(&mut self.sum_buf)" in mixer_rs


def test_voice_uses_fanin_tts_and_duck_for_all_output_profiles():
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    voice_unit = (REPO / "deploy" / "systemd" / "jasper-voice.service").read_text()
    voice_daemon = (REPO / "jasper" / "voice_daemon.py").read_text()
    config_py = (REPO / "jasper" / "config.py").read_text()
    assert "TTS_ENV_FILE" not in reconcile
    assert "JASPER_TTS_OUTPUTD_SOCKET" not in reconcile
    assert "JASPER_DUCK_TRANSPORT" not in reconcile
    assert 'JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock' in voice_unit
    assert 'JASPER_DUCK_TRANSPORT=fanin' in voice_unit
    assert 'duck_transport=_env("JASPER_DUCK_TRANSPORT", "fanin")' in config_py
    assert 'cfg.duck_transport == "fanin"' in voice_daemon
    assert "FanInDucker" in voice_daemon


def test_outputd_dual_apple_sink_is_fail_closed_and_final_sink_only():
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    assert "SinkMode::DualApple" in config_rs
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM" in config_rs
    assert "outputd_active_content_capture" in config_rs
    assert "dual_apple_requires_pre_dsp_tts" not in main_rs
    assert "run_alsa_dual_apple" in main_rs
    assert "DualAppleBackend::new(config)" in main_rs
    assert "deinterleave_4ch_to_dual_stereo" in alsa_rs
    assert "aborted on xrun/suspend" in alsa_rs
    assert "delay divergence" in alsa_rs


def test_outputd_dual_apple_zero_frame_active_read_silences_period():
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    read_dual = alsa_rs.split(
        "pub fn read_content_period(&mut self, out: &mut [i16]) -> Result<usize> {",
        2,
    )[2].split("pub fn write_dual_period", 1)[0]
    zero_frame_branch = read_dual.split("if frames == 0 {", 1)[1].split(
        "} else if frames < requested_frames",
        1,
    )[0]

    assert "content_empty_period_count += 1" in zero_frame_branch
    assert "out.fill(0);" in zero_frame_branch


def test_camilla_outputd_config_is_not_legacy_v1():
    production = (REPO / "deploy" / "camilladsp" / "v1.yml").read_text()
    cutover = (REPO / "deploy" / "camilladsp" / "outputd-cutover.yml").read_text()
    assert 'device: "jasper_out"' in production
    assert 'device: "outputd_content_playback"' in cutover
    assert 'volume_limit: 0.0' in cutover


def test_install_uses_separate_outputd_statefile():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    assert "outputd-cutover.yml" in install_sh
    assert "config_path: /etc/camilladsp/outputd-cutover.yml" in install_sh
    assert "config_path: /etc/camilladsp/v1.yml" in install_sh
    assert "Preserved outputd Camilla statefile" in install_sh
    assert "legacy playback path" in install_sh
    assert "unsafe volume_limit" in install_sh
    assert "camilla_config_has_safe_volume_limit" in install_sh
    assert "--statefile /var/lib/camilladsp/outputd-statefile.yml" in camilla_unit


def test_outputd_alsa_loop_publishes_reference_only_after_dac_write():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    content_read = run_alsa.index("backend.read_content_period(&mut content_buf)?;")
    dac_write = run_alsa.index("backend.write_dac_period(&content_buf)?;")
    publish = run_alsa.index("ref_outputs.publish(&content_buf);")
    state = run_alsa.index("state.mark_period(")

    assert "prepare_period_with_content" not in run_alsa
    assert "commit_prepared_period" not in run_alsa
    assert content_read < dac_write < publish < state


def test_outputd_ready_is_after_alsa_output_is_primed_and_started():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    backend_rs = (
        REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs"
    ).read_text()
    main_fn = main_rs.split("fn main() -> Result<()> {", 1)[1].split(
        "fn run_fake(",
        1,
    )[0]
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    backend_open = run_alsa.index("let mut backend = AlsaBackend::new(config)?;")
    primed = run_alsa.index(
        ".context(\"priming outputd DAC with silence\")?;"
    )
    started = run_alsa.index("backend.start_dac()?;")
    ready = run_alsa.index("notify_ready(config)?;")

    assert 'notify_systemd("READY=1")' not in main_fn
    assert "notify_ready(config)?" not in main_fn
    assert backend_open < primed < started < ready
    assert "swp.set_start_threshold(negotiated.buffer_frames as i64)" in backend_rs
    assert "fn prime_periods(buffer_frames: u32, period_frames: u32) -> u32" in main_rs
    assert "event=outputd.alsa.primed" in run_alsa


def test_outputd_dual_apple_ready_is_after_multi_period_prime_and_start():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_dual = main_rs.split("fn run_alsa_dual_apple(", 1)[1].split(
        "fn downmix_dual_active_reference(",
        1,
    )[0]
    backend_open = run_dual.index("let mut backend = DualAppleBackend::new(config)?;")
    prime_count = run_dual.index("let prime_periods = prime_periods(")
    prime_loop = run_dual.index("for _ in 0..prime_periods")
    primed = run_dual.index(".context(\"priming dual Apple DACs with silence\")?;")
    started = run_dual.index("backend.start_dacs()?;")
    ready = run_dual.index("notify_ready(config)?;")

    assert backend_open < prime_count < prime_loop < primed < started < ready
    assert "event=outputd.dual_apple.primed" in run_dual


def test_outputd_state_socket_is_bound_before_thread_spawn():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    spawn_state = main_rs.split("fn spawn_state_server(", 1)[1].split(
        "fn lock_memory(",
        1,
    )[0]
    bind = spawn_state.index("StateServer::bind(path, state)")
    spawn = spawn_state.index(".spawn(move ||")

    assert "StateServer::new" not in main_rs
    assert bind < spawn


def test_outputd_no_longer_owns_tts_ipc_runtime():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()

    for stale in [
        "spawn_tts_server(",
        "spawn_tts_client(",
        "handle_tts_client(",
        "outputd.tts_socket",
        "outputd.tts_flush",
        "TtsQueueMetrics",
        "mark_tts_command_dropped",
    ]:
        assert stale not in main_rs
        assert stale not in state_rs
