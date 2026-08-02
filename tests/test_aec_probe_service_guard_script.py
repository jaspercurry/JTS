# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "_aec_probe_service_guard.sh"


def test_guard_restores_only_services_active_at_entry() -> None:
    text = GUARD.read_text(encoding="utf-8")

    assert "trap on_exit EXIT" in text
    assert '[[ "${bridge_was_active}" == "1" ]]' in text
    assert "sudo systemctl reset-failed jasper-aec-bridge.service" in text
    assert "sudo systemctl start jasper-aec-bridge.service" in text
    assert '[[ "${voice_was_active}" == "1" ]]' in text
    assert "sudo systemctl start jasper-voice.service" in text
    assert '[[ "${nqptp_was_active}" == "1" ]]' in text
    assert "sudo systemctl restart nqptp.service" in text
    assert '[[ "${shairport_was_active}" == "1" ]]' in text
    assert "sudo systemctl restart shairport-sync.service" in text


def test_guard_preserves_probe_failure_over_restore_failure() -> None:
    text = GUARD.read_text(encoding="utf-8")

    assert "local rc=$?" in text
    assert "local restore_rc=$?" in text
    assert 'exit "${restore_rc}"' in text
    assert 'exit "${rc}"' in text


def test_guard_records_service_state_before_stopping() -> None:
    text = GUARD.read_text(encoding="utf-8")

    assert "printf -v \"${state_var}\" '1'" in text
    assert 'sudo systemctl stop "${unit}"' in text
    assert text.index('printf -v "${state_var}"') < text.index(
        'sudo systemctl stop "${unit}"'
    )
