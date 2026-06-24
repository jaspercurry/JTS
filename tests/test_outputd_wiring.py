# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static checks for the outputd topology."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from jasper.audio_hardware import dac
from jasper.tts_routing import (
    DUCK_TRANSPORT_ENV,
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET_ENV,
    VOICE_TTS_SOCKET_ENV,
)
from tests.install_surface import installer_shell_paths, installer_text

from ._voice_runtime_text import voice_runtime_text


REPO = Path(__file__).resolve().parents[1]


def _non_comment(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _env_file_text_to_map(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve_systemd_unit_env(
    unit_text: str,
    env_files: dict[str, str],
) -> dict[str, str]:
    """Resolve the unit's Environment* directives in declaration order."""
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if line.startswith("EnvironmentFile="):
            path = line.partition("=")[2].strip().strip('"').strip("'")
            if path.startswith("-"):
                path = path[1:]
            if path in env_files:
                env.update(_env_file_text_to_map(env_files[path]))
            continue
        if line.startswith("Environment="):
            payload = line.partition("=")[2].strip()
            for assignment in shlex.split(payload):
                if "=" not in assignment:
                    continue
                key, _, value = assignment.partition("=")
                env[key] = value
    return env


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


def test_asoundrc_active_content_lane_is_raw_hw_no_plug():
    """The active-crossover content lane (snd-aloop substream 5) uses raw
    `type hw` on both sides of the pair — card/device/subdevice only, exactly
    like the outputd_dac block (the ALSA `hw` plugin rejects channels/rate/
    format as unknown fields). The width is NOT pinned in the conf; it is set
    by the openers (CamillaDSP playback: channels: N; outputd's
    JASPER_OUTPUTD_ACTIVE_CHANNELS) and locked by snd-aloop, so a mismatch
    fails closed at open rather than silently remixing onto live drivers.
    `type plug` is banned (it is the auto-converting plugin)."""
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    playback = _pcm_block(rc, "outputd_active_content_playback")
    capture = _pcm_block(rc, "outputd_active_content_capture")
    assert "type hw" in playback
    assert "card Loopback" in playback
    assert "device 0" in playback
    assert "subdevice 5" in playback
    assert "type hw" in capture
    assert "card Loopback" in capture
    assert "device 1" in capture
    assert "subdevice 5" in capture
    # No conversion plugin, and no channels/rate/format keys (the hw plugin
    # would reject them, and the width is not pinned here by design).
    for block in (playback, capture):
        assert "type plug" not in block
        assert "type dsnoop" not in block
        assert "channels" not in block
        assert "rate" not in block
        assert "format" not in block


def test_active_path_pcms_never_use_plug_or_plughw():
    """Contract: NO `type plug` / `plughw:` anywhere on the active-crossover
    path. `plug` is the auto-converting channel/rate/format plugin; on a live-
    driver path it could remix 8->4 onto a tweeter (the single most dangerous
    fail-open in active mode). Covers the asoundrc active content lanes and
    every outputd_dac block the render lib emits (direct hw / composite null /
    DAC8x direct — all conversion-free)."""
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    for name in ("outputd_active_content_playback", "outputd_active_content_capture"):
        block = _pcm_block(rc, name)
        assert "type plug" not in block, name
        assert "plughw" not in block, name
    render_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    assert "type plug" not in render_lib
    assert "plughw" not in render_lib


def test_asoundrc_declares_outputd_rendered_dac_alias_placeholder():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    render_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    assert "__OUTPUTD_DAC_PCM_BLOCK__" in rc
    assert "__OUTPUTD_DAC_CTL_BLOCK__" in rc
    assert "__OUTPUT_DAC_CARD__" not in rc
    assert "line//__OUTPUT_DAC_CARD__" not in render_lib
    assert "OUTPUT_DAC_RECOGNIZED:-1" in render_lib


def test_install_prefers_dac8x_for_outputd_without_reusing_dongle_mixer_card():
    install_sh = installer_text()
    install_without_env_migrations = "\n".join(
        path.read_text(encoding="utf-8")
        for path in installer_shell_paths()
        if path.name != "env-migrations.sh"
    )
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
    assert "Skipped /etc/asound.conf render" not in install_sh
    assert "asoundrc.jasper.source" in install_sh
    assert "JASPER_AUDIO_DAC_ID" in install_sh
    assert "JASPER_AUDIO_DAC_CARD" in reconcile
    assert "JASPER_OUTPUT_DAC_ROUTE" not in reconcile
    assert "OUTPUT_DAC_ROUTE" not in install_without_env_migrations
    assert "APPLE_DONGLE_PRESENT=1" in reconcile
    assert "APPLE_DONGLE_PRESENT=0" in reconcile
    assert 'APPLE_DONGLE_SERVICE_CARD="auto"' in reconcile


def test_bash_output_detection_literals_track_registered_dac_profiles():
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    install_sh = installer_text()

    for profile_id in (
        dac.APPLE_USB_C_DONGLE_ID,
        dac.HIFIBERRY_DAC8X_ID,
        dac.HIFIBERRY_DAC8X_STUDIO_ID,
        dac.DUAL_APPLE_USB_C_DAC_4CH_ID,
    ):
        assert profile_id in reconcile

    assert "jasper-audio-hardware-reconcile\" --print-env" in install_sh
    assert "find_card 'usb-c to 3.5mm'" in reconcile
    assert "find_card 'hifiberry.*dac8x.*studio|dac8x.*studio'" in reconcile
    assert "find_card 'snd_rpi_hifiberry_dac8x|hifiberry.*dac8x|dac8x'" in reconcile
    assert reconcile.index(
        "DAC8X_STUDIO_OUTPUT_CARD=\"$(find_card"
    ) < reconcile.index("DAC8X_OUTPUT_CARD=\"$(find_card")


def test_output_dac_route_policy_is_removed_from_renderer_and_reconciler():
    route_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert "JASPER_OUTPUT_DAC_ROUTE" not in route_lib
    assert "OUTPUT_DAC_ROUTE" not in route_lib
    assert "mono:([1-8])" not in route_lib
    assert "stereo:([1-8]),([1-8])" not in route_lib
    assert "type route" not in route_lib
    assert 'OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch"' in route_lib
    assert "type null" in route_lib
    assert "jasper_asound_route_ignored()" not in reconcile
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
    install_sh = installer_text()
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
    install_sh = installer_text()
    unit = (REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service").read_text()
    rule = (REPO / "deploy" / "udev" / "99-jasper-audio-hardware-reconcile.rules").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    startup_load = (REPO / "jasper" / "active_speaker" / "startup_load.py").read_text()
    assert "deploy/systemd/jasper-audio-hardware-reconcile.service" in install_sh
    assert "deploy/bin/jasper-audio-hardware-reconcile" in install_sh
    assert "deploy/bin/jasper-output-hardware-hotplug" in install_sh
    assert "deploy/bin/jasper-outputd-failure-reconcile" in install_sh
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
    assert 'ACTION=="remove", SUBSYSTEM=="usb", ENV{PRODUCT}=="5ac/110a/*"' in rule
    assert 'ACTION=="remove", SUBSYSTEM=="usb", ENV{PRODUCT}=="05ac/110a/*"' in rule
    assert 'RUN+="/usr/local/sbin/jasper-output-hardware-hotplug"' in rule
    hotplug = (REPO / "deploy" / "bin" / "jasper-output-hardware-hotplug").read_text()
    assert "--no-block start jasper-audio-hardware-reconcile.service" in hotplug
    assert "event=audio_hardware_hotplug.reconcile_requested" in hotplug
    assert "/usr/local/sbin/jasper-audio-hardware-reconcile --reason install" in install_sh
    # The cutover gate is width-aware and shared by the composite + single
    # active paths (renamed from dual_apple_active_graph_status). It now trusts
    # the durable runtime contract, not transient startup-load state: a saved
    # active baseline must stay playable after setup completes.
    assert "active_graph_status()" in reconcile
    assert "active_graph_width_out_of_range" in reconcile
    assert "action=park_until_active_graph" in reconcile
    assert 'JASPER_OUTPUTD_BACKEND" "fake"' in reconcile
    assert "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE" not in reconcile
    assert "JASPER_CAMILLA_STATEFILE" in reconcile
    assert "JASPER_OUTPUT_TOPOLOGY_PATH" in reconcile
    assert "classify_camilla_graph" in reconcile
    assert "outputd_active_content_playback" in reconcile
    assert "AUDIO_HARDWARE_RECONCILE_UNIT" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_load\")" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_rollback\")" in startup_load


def test_install_alsa_refreshes_asound_renderer_before_rendering():
    install_sh = installer_text()
    start = install_sh.index("install_alsa() {")
    end = install_sh.index("\nwrite_build_manifest() {", start)
    install_alsa = install_sh[start:end]

    render_lib_install = install_alsa.index(
        "/usr/local/lib/jasper/jasper-asound-render.sh"
    )
    source_template_install = install_alsa.index("asoundrc.jasper.source")
    render_call = install_alsa.index("jasper_asound_render_template")

    assert "deploy/lib/jasper-asound-render.sh" in install_alsa
    assert render_lib_install < source_template_install
    assert render_lib_install < render_call


def test_voice_tts_socket_resolves_fanin_solo_and_outputd_when_bonded():
    """systemd resolves env directives in order; the bonded override must win."""
    unit = (REPO / "deploy" / "systemd" / "jasper-voice.service").read_text()
    assert f'Environment="{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}"' in unit
    assert f'Environment="{DUCK_TRANSPORT_ENV}=fanin"' in unit
    assert "EnvironmentFile=-/var/lib/jasper/tts.env" not in unit
    assert "EnvironmentFile=-/var/lib/jasper/grouping-voice.env" in unit
    env_directives = [
        line.strip() for line in unit.splitlines()
        if line.strip().startswith(("Environment=", "EnvironmentFile="))
    ]
    assert env_directives[-1] == "EnvironmentFile=-/var/lib/jasper/grouping-voice.env"

    solo = _resolve_systemd_unit_env(unit, {})
    assert solo["JASPER_TTS_TRANSPORT"] == "outputd"
    assert solo[VOICE_TTS_SOCKET_ENV] == FANIN_TTS_SOCKET
    assert solo[DUCK_TRANSPORT_ENV] == "fanin"

    bonded = _resolve_systemd_unit_env(
        unit,
        {
            "/var/lib/jasper/grouping-voice.env": (
                f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
                "JASPER_GROUPING_VOICE_PARK=1\n"
            ),
        },
    )
    assert bonded[VOICE_TTS_SOCKET_ENV] == OUTPUTD_TTS_SOCKET
    assert bonded["JASPER_GROUPING_VOICE_PARK"] == "1"


def test_fanin_exposes_outputd_compatible_tts_socket():
    """fanin's TTS server is the SOLO production ingress. Since
    Increment 5 PR-2 outputd serves the SAME wire protocol for bonded
    members (`rust/jasper-outputd/src/tts.rs`) — gated on the
    reconciler-set socket env, default OFF, so solo outputd stays
    TTS-free. Both ends of the twin are pinned here."""
    main_rs = (REPO / "rust" / "jasper-fanin" / "src" / "main.rs").read_text()
    config_rs = (REPO / "rust" / "jasper-fanin" / "src" / "config.rs").read_text()
    tts_rs = (REPO / "rust" / "jasper-fanin" / "src" / "tts.rs").read_text()
    mixer_rs = (REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text()
    outputd_main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    outputd_config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    outputd_lib_rs = (REPO / "rust" / "jasper-outputd" / "src" / "lib.rs").read_text()
    assert f'"{FANIN_TTS_SOCKET}"' in config_rs
    assert "spawn_tts_server(" in main_rs
    # outputd's twin: present, but constructed ONLY when the grouping
    # reconciler set the socket env (no baked-in default path).
    assert "spawn_tts_server(" in outputd_main_rs
    assert "if let Some(path) = &config.tts_socket_path" in outputd_main_rs
    assert (
        f'env_optional("{OUTPUTD_TTS_SOCKET_ENV}")' in outputd_config_rs
    )  # Option, no baked-in default — unset env means solo, TTS off
    assert "pub mod protocol;" not in outputd_lib_rs
    assert "TtsCommand::FlushSync" in tts_rs
    assert "TtsCommand::ProgramDuckOn" in tts_rs
    assert "prepare_period()" in mixer_rs
    assert "program_gain" in mixer_rs
    assert "apply_gain_to_sum(&mut self.sum_buf, program_gain)" in mixer_rs
    assert "tts.mix_period(&mut self.sum_buf)" in mixer_rs
    assert mixer_rs.index(
        "apply_gain_to_sum(&mut self.sum_buf, program_gain)"
    ) < mixer_rs.index("tts.mix_period(&mut self.sum_buf)")
    # The wire layer itself (command vocabulary + parser) lives ONCE in
    # the shared crate; both daemons consume it as a path dependency —
    # the structural guarantee the old byte-twin asserts approximated.
    proto_rs = (
        REPO / "rust" / "jasper-tts-protocol" / "src" / "lib.rs"
    ).read_text()
    assert '"PROGRAM_DUCK_ON"' in proto_rs
    assert "AUDIO byte length must contain whole stereo frames" in proto_rs
    assert "pub fn read_command" in proto_rs
    for crate in ("jasper-fanin", "jasper-outputd"):
        manifest = (REPO / "rust" / crate / "Cargo.toml").read_text()
        assert 'jasper-tts-protocol = { path = "../jasper-tts-protocol" }' in manifest
    assert '"PROGRAM_DUCK_ON"' not in tts_rs  # no drifting local copy


def test_voice_uses_fanin_tts_and_duck_for_all_output_profiles():
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    voice_unit = (REPO / "deploy" / "systemd" / "jasper-voice.service").read_text()
    voice_runtime = voice_runtime_text()
    config_py = (REPO / "jasper" / "config.py").read_text()
    assert "TTS_ENV_FILE" not in reconcile
    assert VOICE_TTS_SOCKET_ENV not in reconcile
    assert DUCK_TRANSPORT_ENV not in reconcile
    assert f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}" in voice_unit
    assert f"{DUCK_TRANSPORT_ENV}=fanin" in voice_unit
    assert 'duck_transport=_env("JASPER_DUCK_TRANSPORT", "fanin")' in config_py
    assert 'cfg.duck_transport == "fanin"' in voice_runtime
    assert "FanInDucker" in voice_runtime


def test_outputd_dual_apple_sink_is_fail_closed_and_final_sink_only():
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    # The composite sink shape was renamed `DualApple` -> `Composite` (the
    # transport dispatches on shape, not the DAC's name), with `dual_apple`
    # kept as a parse alias and the stable `/state` wire value.
    assert "SinkMode::Composite" in config_rs
    assert '"composite" | "dual_apple"' in config_rs
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM" in config_rs
    assert "outputd_active_content_capture" in config_rs
    assert "dual_apple_requires_pre_dsp_tts" not in main_rs
    assert "run_alsa_dual_apple" not in main_rs
    assert "downmix_dual_active_reference" not in main_rs
    assert "enum RuntimeAlsaSink" in main_rs
    assert "Composite(PairedCompositeSink)" in main_rs
    assert "PairedCompositeSink::new(config)" in main_rs
    assert "deinterleave_4ch_to_dual_stereo" in alsa_rs
    assert "aborted on xrun/suspend" in alsa_rs
    assert "delay divergence" in alsa_rs


def test_outputd_single_sink_is_width_parametric_with_mono_reference_fold():
    """The coherent single sink carries width as DATA (a DAC8x rides the same
    path as a 2ch Apple), publishes a stereo reference via a clip-proof 1/N mono
    fold for wide sinks, and counts real clipping instead of a hardwired 0."""
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()

    # Width is reconciler-supplied data, validated, with the composite shape
    # pinned at 4 and the wide single path kept a pure passthrough.
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS" in config_rs
    assert "fixed at 4 (two stereo children)" in config_rs

    # The single backend reads + writes the runtime width, not a 2ch literal.
    assert "channels: u16," in alsa_rs
    assert "self.channels as usize" in alsa_rs

    # Mono reference fold (1/N, clip-proof) + honest clip accounting.
    assert "fn fold_reference(" in main_rs
    assert "fn fold_reference_pairwise_composite(" in main_rs
    assert "fn count_full_scale_samples(" in main_rs
    # The wide path folds; the 2ch path stays byte-identical (publishes content).
    assert "fold_reference(&content_buf, content_channels, &mut reference_buf);" in main_rs
    assert "fold_reference_pairwise_composite(&content_buf, &mut reference_buf);" in main_rs
    assert "ref_outputs.publish(&content_buf, next_reference_sequence);" in main_rs


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
    install_sh = installer_text()
    systemd_units = (
        REPO / "deploy" / "lib" / "install" / "systemd-units.sh"
    ).read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    assert "outputd-cutover.yml" in install_sh
    assert "config_path: /etc/camilladsp/v1.yml" in install_sh
    assert "runtime-safe-graph" in install_sh
    assert "ensure_outputd_camilla_statefile" in install_sh
    assert "--write-statefile" in install_sh
    assert "tweeter/protected role" in install_sh
    assert "JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR=1" in systemd_units
    assert "Restarting jasper-camilla.service after statefile repair" in install_sh
    assert "Reset outputd Camilla statefile" not in install_sh
    assert "--statefile /var/lib/camilladsp/outputd-statefile.yml" in camilla_unit


def test_outputd_parks_on_missing_configured_output_dac_without_reboot_loop():
    outputd_unit = (REPO / "deploy" / "systemd" / "jasper-outputd.service").read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    cutover = (REPO / "deploy" / "camilladsp" / "outputd-cutover.yml").read_text()
    recover_rule = (
        REPO / "deploy" / "udev" / "99-jasper-audio-hardware-reconcile.rules"
    ).read_text()
    recover_unit = (
        REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service"
    ).read_text()
    recover_script = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    failure_reconcile = (
        REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
    ).read_text()

    assert "StartLimitAction=reboot" in outputd_unit
    assert "Restart=on-failure" in outputd_unit
    assert "RestartPreventExitStatus=78" in outputd_unit
    assert "ExecCondition=/bin/sh -c" in outputd_unit
    assert 'backend="$${JASPER_OUTPUTD_BACKEND:-alsa}"' in outputd_unit
    assert '[ "$$backend" = "fake" ]' in outputd_unit
    assert 'card="$${JASPER_AUDIO_DAC_CARD:-}"' in outputd_unit
    assert '[ -e "/proc/asound/$$card" ]' in outputd_unit
    assert "event=outputd.output_device_gate.park reason=missing_dac" in outputd_unit
    assert "outputd_backend=$$backend" in outputd_unit
    assert "exit 1" in outputd_unit
    assert "ExecStartPre=/bin/sh -c" not in outputd_unit
    assert "ExecStopPost=-/usr/local/sbin/jasper-outputd-failure-reconcile" in outputd_unit
    assert "--reason outputd-failure --no-restart" in failure_reconcile
    assert 'RESULT="${SERVICE_RESULT:-unknown}"' in failure_reconcile
    assert 'STATUS="${EXIT_STATUS:-}"' in failure_reconcile
    assert '"$RESULT" == "success"' in failure_reconcile
    assert '"$RESULT" == "condition"' in failure_reconcile
    assert '"$STATUS" == "78"' in failure_reconcile

    assert "JASPER_AUDIO_DAC_CARD" not in camilla_unit
    assert 'device: "outputd_content_playback"' in cutover
    assert 'ENV{SYSTEMD_WANTS}+="jasper-audio-hardware-reconcile.service"' in recover_rule
    assert "Before=jasper-outputd.service" in recover_unit
    assert "--no-block start jasper-outputd.service" in recover_script
    assert "--no-block restart jasper-outputd.service" in recover_script
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in recover_script


def test_outputd_alsa_loop_publishes_reference_only_after_dac_write():
    """inv-A ordering, both branches: the reference tap publishes what
    the DAC was JUST given, never earlier. Solo publishes the raw
    content period; the bonded TTS branch (Increment 5 PR-2) publishes
    the post-mix engine period, with the duck applied to the CONTENT
    before the mix so the reference carries the ducked program too."""
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    # Solo branch — unchanged pre-PR-2 ordering. The mark_period search
    # string is the solo call's exact one-line form so it can't bind to
    # the TTS branch's multi-line call.
    content_read = run_alsa.index("sink.read_content_period(&mut content_buf)?;")
    dac_write = run_alsa.index("sink.write_period(&content_buf)?;")
    # Width-2 (byte-identical) branch publishes the content directly; the wide
    # sink folds to a stereo reference first. Either way the publish follows the
    # DAC write (inv-A) and precedes the period mark.
    publish = run_alsa.index(
        "ref_outputs.publish(&content_buf, next_reference_sequence);"
    )
    composite_fold = run_alsa.index(
        "fold_reference_pairwise_composite(&content_buf, &mut reference_buf);"
    )
    # Solo branch now reports REAL clip accounting (a full-scale-sample count)
    # rather than the old hardwired 0, so the no-clip commissioning gate is not
    # vacuously green.
    clipped = run_alsa.index("let clipped = count_full_scale_samples(&content_buf);")
    state = run_alsa.index(
        "state.mark_period(sink.counters(), reference_sequence, clipped);"
    )
    assert content_read < dac_write < publish < state
    assert dac_write < clipped < composite_fold < state
    assert "state.mark_period(sink.counters(), reference_sequence, 0)" not in run_alsa
    assert "clipped_samples=0" not in run_alsa

    # Bonded TTS branch — duck → prepare(mix) → DAC write → DAC-true
    # commit → post-mix reference publish → ledger-true state mark.
    duck = run_alsa.index("bridge.content_duck_gain()")
    prepare = run_alsa.index("core.prepare_period_with_content(&content_buf);")
    tts_write = run_alsa.index("sink.write_period(core.output_period())?;")
    commit = run_alsa.index("core.commit_prepared_period_with_dac_delay(")
    tts_publish = run_alsa.index(
        "ref_outputs.publish(core.output_period(), reference_sequence);"
    )
    tts_state = run_alsa.index(
        "state.mark_period(sink.counters(), reference_sequence, report.clipped_samples);"
    )
    assert content_read < duck < prepare < tts_write < commit < tts_publish
    assert tts_publish < tts_state


def test_outputd_chip_ref_tee_is_diagnostic_only_and_env_gated():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    writer = main_rs.split("fn run_chip_ref_writer(", 1)[1].split(
        "fn write_playback_period(",
        1,
    )[0]

    assert "JASPER_OUTPUTD_CHIP_REF_TEE_PATH" in config_rs
    assert "chip_ref_tee_path: env_optional(" in config_rs
    assert "write_chip_ref_tee(&mut tee, &packet.samples, state);" in writer
    assert "write_chip_ref_tee" not in run_alsa
    assert "diagnostic_tee_path" in state_rs
    assert "diagnostic_tee_active" in state_rs
    assert "diagnostic_tee_open_error_count" in state_rs
    assert "mark_chip_ref_tee_open_error" in main_rs


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
    sink_open = run_alsa.index("let mut sink = RuntimeAlsaSink::open(config)?;")
    primed = run_alsa.index(
        ".context(sink.prime_context())?;"
    )
    started = run_alsa.index("sink.start()?;")
    ready = run_alsa.index("notify_ready(config)?;")

    assert 'notify_systemd("READY=1")' not in main_fn
    assert "notify_ready(config)?" not in main_fn
    assert sink_open < primed < started < ready
    assert "swp.set_start_threshold(negotiated.buffer_frames as i64)" in backend_rs
    assert "fn prime_periods(buffer_frames: u32, period_frames: u32) -> u32" in main_rs
    assert '"outputd.alsa.primed"' in main_rs


def test_outputd_dual_apple_ready_is_after_multi_period_prime_and_start():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    sink_impl = main_rs.split("impl RuntimeAlsaSink", 1)[1].split(
        "fn run_alsa(",
        1,
    )[0]
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    composite_open = sink_impl.index("SinkMode::Composite")
    paired_open = sink_impl.index("PairedCompositeSink::new(config)?")
    sink_open = run_alsa.index("let mut sink = RuntimeAlsaSink::open(config)?;")
    prime_count = run_alsa.index("let prime_periods = prime_periods(")
    prime_loop = run_alsa.index("for _ in 0..prime_periods")
    primed = run_alsa.index(".context(sink.prime_context())?;")
    started = run_alsa.index("sink.start()?;")
    ready = run_alsa.index("notify_ready(config)?;")

    assert composite_open < paired_open
    assert sink_open < prime_count < prime_loop < primed < started < ready
    assert '"outputd.dual_apple.primed"' in main_rs


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


def test_outputd_tts_runtime_is_bonded_scoped():
    """Successor of the 9102e13 tombstone (`test_outputd_no_longer_owns_
    tts_ipc_runtime`): Increment 5 PR-2 deliberately re-introduced an
    outputd TTS server — but scoped to bonded members. What stays
    pinned: (a) the RETIRED pre-9102e13 API names never come back, and
    (b) the new runtime is construction-gated on the reconciler-set
    socket env, so a solo speaker's outputd never binds a TTS socket."""
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()

    for retired in [
        "spawn_tts_client(",
        "TtsQueueMetrics",
        "mark_tts_command_dropped",
    ]:
        assert retired not in main_rs
        assert retired not in state_rs

    gate = main_rs.index("if let Some(path) = &config.tts_socket_path")
    spawn = main_rs.index("spawn_tts_server(")
    assert gate < spawn
    # The STATUS block tells the truth on a solo speaker: the tts
    # section's emitter writes enabled:false when no socket is set.
    tts_block = state_rs.index('"tts":{')
    disabled = state_rs.index(
        'push_kv_bool(&mut buf, "enabled", false);', tts_block
    )
    assert disabled > tts_block
