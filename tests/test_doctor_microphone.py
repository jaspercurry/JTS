# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The doctor's mic checks are coherent around one source of truth.

A *confirmed-absent* microphone must yield exactly one yellow ``microphone``
headline and zero red failures: the downstream ``mic ALSA card`` and
``mic capture`` checks defer to ``jasper.mic_presence`` instead of
independently re-probing ALSA and contradicting it (the old bug was a red
``✗ mic ALSA card`` firing alongside a green ``✓ mic capture: expected idle``
for the same fact).

Needs Python >= 3.11 (the doctor package imports ``StrEnum``); runs on the Pi
/ CI, not macOS' system 3.9. The reader itself is covered hardware-free in
``tests/test_mic_presence.py``.
"""
from __future__ import annotations

import types

import pytest

from jasper.cli.doctor import audio
from jasper.mic_presence import MicPresence

_CFG = types.SimpleNamespace(
    mic_device="Array", mic_capture_rate=16000, mic_capture_channels=1
)


def _absent() -> MicPresence:
    return MicPresence(present=False, reason="No supported XVF3800 ALSA card detected")


def _present() -> MicPresence:
    return MicPresence(present=True, is_xvf=True, alsa_card="Array", capture_channels=6)


def _present_non_xvf() -> MicPresence:
    # A custom/non-XVF mic is up: present, but no XVF enrichment.
    return MicPresence(present=True)


@pytest.fixture(autouse=True)
def _not_bonded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the bonded-follower short-circuit out of the way — we're exercising
    # the mic-absence path specifically.
    monkeypatch.setattr(audio, "_parked_as_bonded_follower", lambda: False)


def test_headline_absent_is_one_yellow_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    r = audio.check_microphone()
    assert r.name == "microphone"
    assert r.status == "warn"  # the single flag — never a red fail
    assert "not detected" in r.detail
    assert "No supported XVF3800 ALSA card detected" in r.detail


def test_headline_present_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _present)
    r = audio.check_microphone()
    assert r.status == "ok"
    assert "present" in r.detail


def test_headline_non_xvf_present_is_ok_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 guard at the headline: a present non-XVF mic must read as present,
    never "not detected" (it has no XVF enrichment, but it IS a microphone)."""
    monkeypatch.setattr(audio, "read_mic_presence", _present_non_xvf)
    r = audio.check_microphone()
    assert r.status == "ok"
    assert "not detected" not in r.detail


def test_card_and_capture_defer_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    card = audio.check_mic_card_matches_config(_CFG)
    cap = audio.check_mic_capture(_CFG)
    # Both defer to the headline — expected idle, never a red failure.
    assert card.status == "ok"
    assert "microphone" in card.detail
    assert cap.status == "ok"


def test_absent_mic_is_one_flag_zero_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the cleanup: no mic == one warn, never a cascade."""
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    results = [
        audio.check_microphone(),
        audio.check_mic_card_matches_config(_CFG),
        audio.check_mic_capture(_CFG),
    ]
    statuses = [r.status for r in results]
    assert statuses.count("fail") == 0
    assert statuses.count("warn") == 1
