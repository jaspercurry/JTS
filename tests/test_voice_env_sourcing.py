"""jasper-voice must source every wizard env file whose keys its Config reads.

The daemon-side mirror of ``test_env_load_mirrors_unit.py`` (which guards the
CLI/doctor side). ``jasper-voice`` is the only daemon that builds the full typed
``Config.from_env()`` from ``os.environ``, so if a wizard writes a key Config
reads but ``jasper-voice.service`` does not source that file, the value is
silently pinned to its default. That was the peering bug: ``JASPER_PEERING``
(``peering.env``) never reached jasper-voice, so ``peering_enabled`` was always
False regardless of what the ``/peers/`` wizard wrote (fixed in #565).

This DERIVES the check instead of hand-writing one assertion per file.
``test_weather_plumbing.py`` / ``test_sound_plumbing.py`` /
``test_peering_plumbing.py`` are three near-identical copies of "assert this
file is sourced by this unit" — a shape that only ever catches the bug already
found. Here: every ``/var/lib/jasper/*.env`` referenced by ANY unit must be
sourced by ``jasper-voice.service`` OR listed in ``VOICE_DOES_NOT_READ`` with
the daemon that consumes it instead. Adding a new wizard env file forces that
decision at test time rather than in production. (The per-file plumbing tests
stay — they each also assert a non-voice contract: weather's web unit + nginx,
peering's control unit, etc.)
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "deploy" / "systemd"
VOICE_UNIT = UNIT_DIR / "jasper-voice.service"

# Wizard env files jasper-voice deliberately does NOT source: its
# ``Config.from_env()`` reads none of their keys (verified by grepping each
# file's JASPER_* key prefixes against jasper/config.py — all 0 hits). Each is
# consumed by another daemon, or resolved into /etc/jasper/jasper.env by a
# reconciler (which voice DOES source). The value names who actually reads the
# file — keep it accurate, and the staleness test below keeps you honest.
VOICE_DOES_NOT_READ: dict[str, str] = {
    "/var/lib/jasper/aec_mode.env":
        "profile/leg intent read by jasper-aec-reconcile, which resolves "
        "JASPER_MIC_DEVICE* into jasper.env (which voice sources)",
    "/var/lib/jasper/fanin.env":
        "JASPER_FANIN_* read by jasper-mux and the Rust jasper-fanin",
    "/var/lib/jasper/grouping.env":
        "JASPER_GROUPING_* read by snapserver / snapclient / grouping-reconcile",
    "/var/lib/jasper/outputd.env":
        "JASPER_OUTPUTD_* read by jasper-outputd and the AEC bridge; voice "
        "gets its TTS socket from an inline Environment= line, not this file",
    "/var/lib/jasper/usbsink.env":
        "JASPER_USBSINK_* read by jasper-usbsink",
    "/var/lib/jasper/wake_corpus_bridge.env":
        "JASPER_AEC_CORPUS_* read by the AEC bridge / aec-init / outputd",
}


def _sourced_state_env_files(unit_text: str) -> set[str]:
    """The ``/var/lib/jasper/*.env`` paths a unit sources via EnvironmentFile=.

    The leading ``-`` (optional-file marker) is stripped. Only wizard state
    files under /var/lib/jasper are in scope — /etc/jasper/jasper.env and
    /run/* runtime files are not wizard-owned config."""
    out: set[str] = set()
    for line in unit_text.splitlines():
        m = re.match(r"^EnvironmentFile=-?(.+)$", line.strip())
        if m and m.group(1).strip().startswith("/var/lib/jasper/"):
            out.add(m.group(1).strip())
    return out


def _all_wizard_env_files() -> set[str]:
    files: set[str] = set()
    for unit in UNIT_DIR.glob("*.service"):
        files |= _sourced_state_env_files(unit.read_text())
    return files


def test_jasper_voice_sources_every_wizard_env_file_it_reads():
    voice = _sourced_state_env_files(VOICE_UNIT.read_text())
    unaccounted = _all_wizard_env_files() - voice - set(VOICE_DOES_NOT_READ)
    assert not unaccounted, (
        "jasper-voice.service does not source these wizard env files, and they "
        f"are not in VOICE_DOES_NOT_READ: {sorted(unaccounted)}. If jasper-"
        "voice's Config.from_env() reads any of their keys, add an "
        "`EnvironmentFile=-<path>` line — this is the peering-bug shape, where "
        "JASPER_PEERING never reached jasper-voice. If voice does NOT read "
        "them, add each to VOICE_DOES_NOT_READ naming the daemon that does."
    )


def test_voice_does_not_read_allowlist_is_not_stale():
    """The allowlist can't silently rot: every entry must still be referenced
    by some unit and must NOT be sourced by jasper-voice (which would make the
    'voice does not read this' claim a lie)."""
    all_wizard = _all_wizard_env_files()
    voice = _sourced_state_env_files(VOICE_UNIT.read_text())
    for path, reason in VOICE_DOES_NOT_READ.items():
        assert path in all_wizard, (
            f"{path} is in VOICE_DOES_NOT_READ but no unit references it — "
            "stale allowlist entry, remove it."
        )
        assert path not in voice, (
            f"{path} is in VOICE_DOES_NOT_READ but jasper-voice.service sources "
            "it — contradiction, remove the allowlist entry."
        )
        assert reason.strip(), f"{path} needs a documented reason (who reads it)."
