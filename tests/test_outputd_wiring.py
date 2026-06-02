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


def test_asoundrc_declares_outputd_direct_dac_alias():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    dac = _pcm_block(rc, "outputd_dac")
    assert "type hw" in dac
    assert "card __OUTPUT_DAC_CARD__" in dac
    assert "device 0" in dac


def test_install_prefers_dac8x_for_outputd_without_reusing_dongle_mixer_card():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    assert "find_card()" in install_sh
    assert "OUTPUT_DAC_CARD=$(detect_card aplay" in install_sh
    assert "OUTPUT_DAC_ID=$(audio_dac_id_for_card" in install_sh
    assert "snd_rpi_hifiberry_dac8x" in install_sh
    assert "hifiberry_dac8x" in install_sh
    assert 'echo "  Output DAC: CARD=${OUTPUT_DAC_CARD}"' in install_sh
    assert 'echo "  Output DAC id: ${OUTPUT_DAC_ID}"' in install_sh
    assert 's/__OUTPUT_DAC_CARD__/${OUTPUT_DAC_CARD}/g' in install_sh
    assert "JASPER_AUDIO_DAC_ID" in install_sh
    assert "APPLE_DONGLE_PRESENT=1" in install_sh
    assert "APPLE_DONGLE_PRESENT=0" in install_sh


def test_apple_dongle_mixer_services_are_gated_by_detected_dongle():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    gated = install_sh.split(
        'if [[ "${APPLE_DONGLE_PRESENT:-0}" == "1" ]]; then',
        1,
    )[1].split("systemctl stop jasper-voice.service", 1)[0]
    assert "systemctl enable jasper-dac-init.service jasper-headphone-monitor.service" in gated
    assert "systemctl start jasper-dac-init.service" in gated
    assert "systemctl restart jasper-headphone-monitor.service" in gated
    assert "systemctl disable --now jasper-dac-init.service jasper-headphone-monitor.service" in gated
    assert "systemctl reset-failed jasper-dac-init.service jasper-headphone-monitor.service" in gated


def test_headphone_monitor_uses_detected_dongle_card_template():
    unit = (REPO / "deploy" / "systemd" / "jasper-headphone-monitor.service").read_text()
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    assert "ExecStart=/usr/local/bin/jasper-headphone-monitor __DONGLE_CARD__ Headphone" in unit
    monitor_install = install_sh.split("deploy/systemd/jasper-headphone-monitor.service", 1)[0]
    assert 's/__DONGLE_CARD__/${DONGLE_CARD}/g' in monitor_install


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


def test_outputd_alsa_loop_commits_only_after_dac_write():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    prepare = run_alsa.index("core.prepare_period_with_content(&content_buf);")
    dac_write = run_alsa.index("backend.write_dac_period(core.output_period())?;")
    commit = run_alsa.index("let report = core.commit_prepared_period_with_dac_delay(")
    state = run_alsa.index("state.mark_period(")

    assert prepare < dac_write < commit < state


def test_outputd_ready_is_after_alsa_output_is_primed_and_started():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
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


def test_outputd_state_socket_is_bound_before_thread_spawn():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    spawn_state = main_rs.split("fn spawn_state_server(", 1)[1].split(
        "fn spawn_tts_server(",
        1,
    )[0]
    bind = spawn_state.index("StateServer::bind(path, state)")
    spawn = spawn_state.index(".spawn(move ||")

    assert "StateServer::new" not in main_rs
    assert bind < spawn


def test_outputd_tts_accept_loop_does_not_inline_client_handling():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    spawn_tts = main_rs.split("fn spawn_tts_server(", 1)[1].split(
        "fn spawn_tts_client(",
        1,
    )[0]
    spawn_client = main_rs.split("fn spawn_tts_client(", 1)[1].split(
        "fn handle_tts_client(",
        1,
    )[0]

    assert "spawn_tts_client(" in spawn_tts
    assert "tx.clone()" in spawn_tts
    assert "flush_tx.clone()" in spawn_tts
    assert "Arc::clone(&epoch)" in spawn_tts
    assert "Arc::clone(&state)" in spawn_tts
    assert '.name("outputd-tts-client".to_string())' in spawn_client
    assert (
        ".spawn(move || handle_tts_client(stream, tx, flush_tx, epoch, state))"
        in spawn_client
    )
    assert "Ok(stream) => handle_tts_client(stream" not in spawn_tts
