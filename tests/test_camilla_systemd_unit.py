"""Pin the jasper-camilla.service systemd unit invariants.

The unit owns the load-bearing CamillaDSP launch — anything that drifts
here can silently break audio (Restart=always, the StartLimit ceiling)
or silently wipe a user's room correction on reboot (--statefile).

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
INSTALL_SH = (
    Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
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


def test_install_sh_creates_camilladsp_state_dirs():
    """install.sh must create /var/lib/camilladsp/ and configs/ as
    a precondition for both the --statefile and the room-correction
    wizard's emitted YAMLs. Without these dirs, the unit fails to
    write its statefile and the wizard fails on apply."""
    body = INSTALL_SH.read_text()
    assert "install -d -m 0755 /var/lib/camilladsp /var/lib/camilladsp/configs" in body


def test_install_sh_repairs_active_speaker_config_modes_for_web_commissioning():
    """Stale active-speaker YAML may predate the non-root web arm flow.

    The sudo CLI can read root:root 0600 generated configs, but jasper-web must
    read the all-muted startup anchor before arming a driver silently.
    """

    body = INSTALL_SH.read_text()
    assert "-name 'active_speaker_*.yml'" in body
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


def test_install_sh_seeds_outputd_statefile():
    """The outputd statefile is topology-owned, seeded on first deploy,
    and preserved when it already points at an outputd-safe config."""
    body = INSTALL_SH.read_text()
    assert "/var/lib/camilladsp/outputd-statefile.yml" in body
    assert "config_path: /etc/camilladsp/outputd-cutover.yml" in body
    assert "Preserved outputd Camilla statefile" in body
    assert "missing config" in body
    assert "legacy playback path" in body


def test_unit_documents_no_config_recovery_path():
    """The recovery path for "bad correction wedges the speaker" is
    to add --no_config to the ExecStart args. Pin the inline doc so
    it doesn't drift — a stranded operator should be able to read
    the unit and know what to do."""
    body = UNIT_PATH.read_text()
    assert "--no_config" in body
