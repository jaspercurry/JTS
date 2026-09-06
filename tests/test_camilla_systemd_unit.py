# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the jasper-camilla.service systemd unit invariants.

The unit owns the load-bearing CamillaDSP launch — anything that drifts
here can silently break audio (Restart=always, the StartLimit recovery
policy) or silently wipe a user's room correction on reboot (--statefile).

These tests are a defensive moat around regressions like:
  - "we tweaked ExecStart and accidentally dropped --statefile" →
    every reboot loses the loaded config, correction lost
  - "we removed Restart=always to make systemd less aggressive" →
    a clean exit leaves audio dead until manual intervention
    (real 2026-05-07 incident in the unit header comment)
  - "we changed the statefile path but forgot to update install.sh's
    mkdir" → unit fails to start because /var/lib/camilladsp/ is
    missing on a fresh install
"""
from __future__ import annotations

from pathlib import Path

from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)

UNIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "systemd" / "jasper-camilla.service"
)
RECOVER_UNIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "systemd" / "jasper-camilla-recover.service"
)
RECOVER_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "bin" / "jasper-camilla-recover"
)
INSTALL_SH = (
    Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
)


def test_unit_starts_after_outputd_and_fanin_for_pipe_rendezvous():
    body = UNIT_PATH.read_text()

    after = _values_for(body, "After")
    wants = _values_for(body, "Wants")
    assert "jasper-outputd.service" in after
    assert "jasper-fanin.service" in after
    assert "jasper-outputd.service" in wants
    assert "jasper-fanin.service" in wants


def test_unit_elects_rt_below_the_sinks_and_bounds_rttime():
    """Audio-latency foundation G1+G4. CamillaDSP must run SCHED_FIFO so it
    isn't preempted under load (the source of the fan-in short-read/xrun
    storms), but BELOW the two sinks that have to win the CPU when the system
    is starved: jasper-fanin (30) and jasper-outputd (35). Camilla feeds them,
    so the ordering 25 < 30 < 35 must hold — verify it against the sibling
    units, not just a literal. LimitRTPRIO=99 gives the binary's
    audio_thread_priority promotion headroom; LimitRTTIME=200000 (200 ms) bounds
    a runaway RT thread to a SIGXCPU instead of a watchdog reboot (mandatory
    with G1)."""
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    camilla_prio = int(_value_for(unit, "CPUSchedulingPriority"))
    assert camilla_prio == 25
    assert _value_for(unit, "LimitRTPRIO") == "99"
    assert _value_for(unit, "LimitRTTIME") == "200000"

    systemd_dir = UNIT_PATH.parent
    fanin_prio = int(
        _value_for(
            (systemd_dir / "jasper-fanin.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    outputd_prio = int(
        _value_for(
            (systemd_dir / "jasper-outputd.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    assert camilla_prio < fanin_prio < outputd_prio, (
        f"Camilla ({camilla_prio}) must stay below fan-in ({fanin_prio}) and "
        f"outputd ({outputd_prio}) so the sinks win the CPU when starved."
    )


def test_unit_passes_cutover_statefile_to_camilladsp():
    """The outputd topology uses a separate Camilla statefile so
    rollback to pre-outputd code can keep the user's normal correction statefile
    intact."""
    body = UNIT_PATH.read_text()
    assert "--statefile" in body
    assert "/var/lib/camilladsp/outputd-statefile.yml" in body


def test_unit_has_no_positional_configfile():
    """CamillaDSP behavior we hit on first cutover: when both a
    positional CONFIGFILE and --statefile are given, the positional
    WINS on startup AND clobbers the statefile with the positional
    path on every start. So having a config path as a positional arg
    here defeats the entire persistence feature. Fresh installs are
    handled by install.sh seeding the statefile.
    Pin the absence so this doesn't quietly come back."""
    body = UNIT_PATH.read_text()
    # The positional arg would appear on its own line after the
    # other ExecStart args. Verify the ExecStart's last non-comment
    # non-blank line is the --statefile arg, not a CONFIGFILE path.
    in_exec = False
    last_line = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("ExecStart="):
            in_exec = True
            last_line = stripped
            continue
        if in_exec:
            if stripped.endswith("\\"):
                last_line = stripped
                continue
            # First non-continuation line ends the ExecStart.
            last_line = stripped
            break
    assert last_line is not None
    assert "v1.yml" not in last_line, (
        f"ExecStart ends with a positional config — clobbers statefile. "
        f"Last line: {last_line!r}"
    )
    assert "--statefile" in last_line


def test_unit_restarts_always_not_on_failure():
    """Restart=on-failure ignores clean exits (status=0). A real
    2026-05-07 incident left the speaker silently dead overnight
    because Camilla exited cleanly. Restart=always covers both
    crash and clean-exit paths."""
    body = UNIT_PATH.read_text()
    # Read the actual directive line, not occurrences in comments.
    directive_lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip().startswith("Restart=")
    ]
    assert directive_lines == ["Restart=always"], directive_lines


def test_unit_uses_recovery_handler_instead_of_raw_reboot():
    """JTS5's ALSA-busy failure class needs holder forensics and a bounded
    graph restart, not an immediate blind reboot."""
    body = UNIT_PATH.read_text()
    assert _value_for(body, "StartLimitAction") == "none"
    assert _values_for(body, "OnFailure") == ("jasper-camilla-recover.service",)
    assert "StartLimitIntervalSec=60" in body
    assert "StartLimitBurst=5" in body


def test_recovery_unit_points_at_installed_helper():
    body = RECOVER_UNIT_PATH.read_text()
    assert _value_for(body, "Type") == "oneshot"
    assert _assignments_for(body, "ExecStart") == (
        "/usr/local/sbin/jasper-camilla-recover --reason start-limit",
    )
    # Both deadlines are load-bearing: the body must be able to finish its
    # own restore ladder, and the EXIT trap that reruns it on a kill needs
    # the ladder's share as its own budget.
    assert _value_for(body, "TimeoutStartSec") == "600"
    assert _value_for(body, "TimeoutStopSec") == "400"


def test_recovery_helper_is_bounded_and_forensic():
    body = RECOVER_SCRIPT_PATH.read_text()
    assert "event=camilla.recover." in body
    assert "capture_dev_snd_holders" in body
    assert "capture_asound_status" in body
    assert "JASPER_CAMILLA_RECOVER_COOLDOWN_SEC" in body
    assert "systemctl reboot" not in body


def test_install_sh_installs_recovery_unit_and_helper():
    body = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "lib" / "install" / "systemd-units.sh"
    ).read_text()
    assert "deploy/systemd/jasper-camilla-recover.service" in body
    assert "deploy/bin/jasper-camilla-recover" in body
    assert "/usr/local/sbin/jasper-camilla-recover" in body


def test_install_sh_creates_camilladsp_state_dirs():
    """install.sh must create /var/lib/camilladsp/ and configs/ as
    a precondition for both the --statefile and the room-correction
    wizard's emitted YAMLs. Without these dirs, the unit fails to
    write its statefile and the wizard fails on apply.

    configs/ must be created *group-writable* (2775 -g jasper) from its first
    creation, not root-only, so a partial deploy can't leave the non-root
    jasper-web user unable to write staged/correction configs (the jts3
    2026-07-06 PermissionError incident). check_camilla_configs_writable pins
    the runtime posture."""
    body = INSTALL_SH.read_text()
    assert "install -d -m 0755 /var/lib/camilladsp" in body
    assert "install -d -m 2775 -g jasper /var/lib/camilladsp/configs" in body


def test_install_sh_removes_legacy_v1_yml_from_upgraded_boxes():
    """install.sh no longer *seeds* v1.yml (issue #2240), but a box
    upgraded from an older build still has one on disk from a prior
    install unless install_camilladsp actively removes it. A stray
    v1.yml is not inert: camillagui's config picker scans
    /etc/camilladsp/*.yml and can still select it, and the install-time
    statefile guard treats it as a flat-allowed graph — so a leftover
    copy can point the statefile at a config that writes to the
    now-removed pcm.jasper_out dmix."""
    body = INSTALL_SH.read_text()
    assert 'rm -f "${CAMILLA_CONF}/v1.yml"' in body
    # Must live in install_camilladsp, not some other function.
    func_start = body.index("install_camilladsp()")
    func_end = body.index("\n}\n", func_start)
    func_body = body[func_start:func_end]
    assert 'rm -f "${CAMILLA_CONF}/v1.yml"' in func_body


def test_install_sh_repairs_generated_camilla_config_modes_for_non_root_daemons():
    """Stale generated YAML may predate the non-root control/web readers.

    The sudo CLI can read root:root 0600 generated configs, but jasper-control
    and jasper-web read configs/*.yml for /state, /sound, and active-driver
    flows. Repair all generated YAMLs, not just active-speaker baselines.
    """

    body = INSTALL_SH.read_text()
    assert "-name '*.yml'" in body
    assert "-exec chgrp jasper {} +" in body
    assert "-exec chmod 0640 {} +" in body


def test_install_sh_repairs_dsp_apply_lock_for_web_commissioning():
    """A stale root-created lock must not block jasper-web DSP apply paths."""

    body = INSTALL_SH.read_text()
    assert "/var/lib/camilladsp/configs/.dsp_apply.lock" in body
    assert "chgrp jasper /var/lib/camilladsp/configs/.dsp_apply.lock" in body
    assert "chmod 0660 /var/lib/camilladsp/configs/.dsp_apply.lock" in body


def test_install_sh_routes_outputd_statefile_through_runtime_contract():
    """The outputd statefile is topology-owned, so install asks
    active_speaker for the safe graph instead of hard-coding flat stereo."""
    body = INSTALL_SH.read_text()
    assert "/var/lib/camilladsp/outputd-statefile.yml" in body
    assert "runtime-safe-graph" in body
    assert "--write-statefile" in body
    assert "tweeter/protected role" in body
    assert "config_path: /etc/camilladsp/outputd-cutover.yml" not in body


def test_flat_cutover_has_exactly_one_writer():
    """install, the root reconciler, and the reset all go through one entry.

    The flat cutover graph is width-matched to the saved output topology, so
    three different components write it at three different times (deploy, boot /
    udev / topology-save, reset). If any of them emits its own copy, the graph a
    box boots depends on which ran last — and one of them would inevitably ship
    a different file mode or skip the width match. `jasper-sound
    render-flat-cutover` is the single entry; this fails if a second writer
    (an inline heredoc, a direct emitter call) reappears in the shell layer.
    """
    reconcile = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "bin" / "jasper-audio-hardware-reconcile"
    ).read_text()
    install = INSTALL_SH.read_text()

    for name, body in (("install.sh", install), ("reconciler", reconcile)):
        assert "render-flat-cutover" in body, name
        # The emitter is reached through the CLI, never spelled directly in bash.
        assert "emit_flat_outputd_cutover_config" not in body, name

    # The reconciler renders BEFORE it can restart the audio graph, so a restart
    # loads this pass's graph rather than the previous topology's.
    render = reconcile.index("render_flat_cutover_if_needed\n")
    assert render < reconcile.index("restart_audio_if_needed 1")


def test_install_sh_writes_output_hardware_before_flat_statefile_seed():
    """Fresh-flat startup needs output_hardware.json before rendering the base
    graph and before runtime-safe-graph writes outputd-statefile.yml."""
    body = INSTALL_SH.read_text()
    assert "ensure_output_hardware_state" in body
    assert "render_outputd_cutover_config" in body
    # install renders through the product emitter, not a hand-written YAML.
    # It reaches it via the shared CLI rather than an inline heredoc — see
    # test_flat_cutover_has_exactly_one_writer below for why that matters.
    assert "jasper-sound render-flat-cutover" in body

    normal_install = body.index("install_jasper\n")
    state = body.index("ensure_output_hardware_state", normal_install)
    render = body.index("render_outputd_cutover_config", state)
    seed = body.index("ensure_outputd_camilla_statefile", render)
    assert normal_install < state < render < seed

    streambox_install = body.index("install_streambox_jasper\n")
    state = body.index("ensure_output_hardware_state", streambox_install)
    render = body.index("render_outputd_cutover_config", state)
    seed = body.index("ensure_outputd_camilla_statefile", render)
    assert streambox_install < state < render < seed


def test_install_sh_installs_camilladsp_before_the_statefile_seed():
    """#2135: the parked graph's `camilladsp --check` preflight silently no-ops
    if the binary is not on disk yet.

    `_materialise_parked_muted_config` refuses to write a graph CamillaDSP
    rejects — but `validate_camilla_config` treats a MISSING binary as
    `ok_to_apply` (dev hosts and CI have none). So the preflight only protects a
    real box while `install_camilladsp` (which fetches /opt/camilladsp/camilladsp)
    runs BEFORE the two statefile seeds. Reorder them and the guard turns into a
    no-op with no failing test — hence this pin, alongside the output-hardware
    ordering pin above.

    Compares EVERY call-site occurrence, not the first after an anchor: a stray
    earlier seed call is exactly the reorder this guards against.
    """
    body = INSTALL_SH.read_text()

    def call_sites(name: str) -> list[int]:
        return [
            index
            for index, line in enumerate(body.splitlines())
            if line.strip().split("#")[0].strip() == name
        ]

    camilla = call_sites("install_camilladsp")
    outputd_seed = call_sites("ensure_outputd_camilla_statefile")
    crossover_seed = call_sites("ensure_crossover_camilla_statefile")

    # One call per install arm (normal + streambox).
    assert len(camilla) == 2, camilla
    assert len(outputd_seed) == 2, outputd_seed
    assert len(crossover_seed) == 2, crossover_seed
    # Per ARM (the two arms interleave in file order), the binary install comes
    # first, then outputd's seed, then camilla#2's.
    for install, seed, cross in zip(
        camilla, outputd_seed, crossover_seed, strict=True
    ):
        assert install < seed < cross, (install, seed, cross)


def test_unit_documents_no_config_recovery_path():
    """The recovery path for "bad correction wedges the speaker" is
    to add --no_config to the ExecStart args. Pin the inline doc so
    it doesn't drift — a stranded operator should be able to read
    the unit and know what to do."""
    body = UNIT_PATH.read_text()
    assert "--no_config" in body
