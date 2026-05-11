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


def test_unit_passes_statefile_to_camilladsp():
    """The whole point of Phase 2.2 — without --statefile, every
    Camilla restart drops back to v1.yml and the user's correction
    is silently lost. This pin makes that regression noisy."""
    body = UNIT_PATH.read_text()
    assert "--statefile" in body
    assert "/var/lib/camilladsp/statefile.yml" in body


def test_unit_falls_back_to_v1_yml_when_statefile_missing():
    """The positional config arg (`/etc/camilladsp/v1.yml`) is the
    fall-back Camilla uses on first boot before any statefile exists.
    Without it, a fresh install would fail to start."""
    body = UNIT_PATH.read_text()
    assert "/etc/camilladsp/v1.yml" in body


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


def test_unit_documents_no_config_recovery_path():
    """The recovery path for "bad correction wedges the speaker" is
    to add --no_config to the ExecStart args. Pin the inline doc so
    it doesn't drift — a stranded operator should be able to read
    the unit and know what to do."""
    body = UNIT_PATH.read_text()
    assert "--no_config" in body
