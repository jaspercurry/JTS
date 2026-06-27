# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the jasper-camilla.service systemd unit invariants.

The unit owns the load-bearing CamillaDSP launch — anything that drifts
here can silently break audio (Restart=always, the StartLimit recovery
policy) or silently wipe a user's room correction on reboot (--statefile).

These tests are a defensive moat around regressions like:
  - "we tweaked ExecStart and accidentally dropped --statefile" →
    every reboot reverts to v1.yml, correction lost
  - "we removed Restart=always to make systemd less aggressive" →
    a clean exit leaves audio dead until manual intervention
    (real 2026-05-07 incident in the unit header comment)
  - "we changed the statefile path but forgot to update install.sh's
    mkdir" → unit fails to start because /var/lib/camilladsp/ is
    missing on a fresh install
"""
from __future__ import annotations

from pathlib import Path

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


def _value_for(unit_text: str, key: str) -> str | None:
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


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
    path on every start. So having `/etc/camilladsp/v1.yml` as a
    positional arg here defeats the entire persistence feature.
    Fresh installs are handled by install.sh seeding the statefile.
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
    assert _value_for(body, "OnFailure") == "jasper-camilla-recover.service"
    assert "StartLimitIntervalSec=60" in body
    assert "StartLimitBurst=5" in body


def test_recovery_unit_points_at_installed_helper():
    body = RECOVER_UNIT_PATH.read_text()
    assert _value_for(body, "Type") == "oneshot"
    assert _value_for(body, "ExecStart") == (
        "/usr/local/sbin/jasper-camilla-recover --reason start-limit"
    )
    assert _value_for(body, "TimeoutStartSec") == "45"


def test_recovery_helper_is_bounded_and_forensic():
    body = RECOVER_SCRIPT_PATH.read_text()
    assert "event=camilla.recover." in body
    assert "capture_dev_snd_holders" in body
    assert "capture_asound_status" in body
    assert "JASPER_CAMILLA_RECOVER_COOLDOWN_SEC" in body
    assert "action \"parked_no_reboot\"" in body
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
    write its statefile and the wizard fails on apply."""
    body = INSTALL_SH.read_text()
    assert "install -d -m 0755 /var/lib/camilladsp /var/lib/camilladsp/configs" in body


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


def test_install_sh_seeds_statefile_when_missing():
    """Because the unit's ExecStart has no positional CONFIGFILE,
    a fresh install with no statefile would leave CamillaDSP with
    nothing to load. install.sh seeds the statefile to point at
    v1.yml on first install — and importantly, never overwrites an
    existing statefile (idempotent), so re-running install.sh on a
    speaker with an applied correction doesn't silently reset it."""
    body = INSTALL_SH.read_text()
    assert "/var/lib/camilladsp/statefile.yml" in body
    # The idempotency guard — must check existence first.
    assert "if [[ ! -f /var/lib/camilladsp/statefile.yml ]]" in body
    # The seed contents point at v1.yml (so first-boot has a config).
    assert "config_path: /etc/camilladsp/v1.yml" in body


def test_install_sh_routes_outputd_statefile_through_runtime_contract():
    """The outputd statefile is topology-owned, so install asks
    active_speaker for the safe graph instead of hard-coding flat stereo."""
    body = INSTALL_SH.read_text()
    assert "/var/lib/camilladsp/outputd-statefile.yml" in body
    assert "runtime-safe-graph" in body
    assert "--write-statefile" in body
    assert "tweeter/protected role" in body
    assert "config_path: /etc/camilladsp/outputd-cutover.yml" not in body


def test_unit_documents_no_config_recovery_path():
    """The recovery path for "bad correction wedges the speaker" is
    to add --no_config to the ExecStart args. Pin the inline doc so
    it doesn't drift — a stranded operator should be able to read
    the unit and know what to do."""
    body = UNIT_PATH.read_text()
    assert "--no_config" in body
