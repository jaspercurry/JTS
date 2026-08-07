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
    assert "input unavailable" in r.detail
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


# --- push-to-talk-only box (issue #2205) -------------------------------------

def _push_to_talk_only() -> MicPresence:
    """Gate open (a remote is paired), but no local mic to probe."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


def test_push_to_talk_box_reads_as_advisory_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this PR must not ship: opening the gate for an accessory
    makes the local-device checks actually probe, and on a box with no local mic
    they would find nothing and go RED. A box whose only microphone is a paired
    remote must not look broken."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    monkeypatch.setattr(
        audio, "check_alsa_card",
        lambda *a, **k: audio.CheckResult("mic ALSA card (Array)", "fail", "absent"),
    )
    card = audio.check_mic_card_matches_config(_CFG)
    assert card.status == "warn"
    assert "wiim_remote_2" in card.detail
    # GATE state, not runtime state. Opening the gate is necessary but not
    # sufficient — the daemon still exits 66 with no local mic (issue #2205) —
    # so this must not claim voice is running on the accessory.
    assert "the voice-input gate is open for it" in card.detail
    assert "#2205" in card.detail
    assert "runs push-to-talk" not in card.detail
    # The local finding is preserved, not swallowed.
    assert "absent" in card.detail


def test_push_to_talk_box_is_distinguishable_from_a_working_local_mic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator must be able to tell the two apart. The headline names the
    accessory and never claims "present"; the local probe says which half is
    actually there."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    headline = audio.check_microphone()
    assert headline.status == "ok"
    assert "push-to-talk accessory paired: wiim_remote_2" in headline.detail
    assert not headline.detail.startswith("present")
    # Never a present-tense claim about a daemon this check does not look at.
    assert "runs" not in headline.detail

    monkeypatch.setattr(audio, "read_mic_presence", _present)
    assert "push-to-talk" not in audio.check_microphone().detail


def test_soften_never_upgrades_a_passing_or_warning_result() -> None:
    presence = _push_to_talk_only()
    for status in ("ok", "warn"):
        original = audio.CheckResult("mic capture", status, "detail")
        assert audio._soften_for_push_to_talk(original, presence) is original


def test_recorded_silence_stays_a_failure_even_with_an_accessory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope guard on the softening: a mic that OPENS but records silence is a
    present-and-broken local mic, not an absent one. Calling that
    "no local microphone" would be a lie, and hiding it behind an accessory
    would let a muted array go unnoticed."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    rec = types.SimpleNamespace()

    class _FakeSd:
        @staticmethod
        def rec(*_a: object, **_k: object) -> object:
            return rec

    class _FakeNp:
        @staticmethod
        def abs(_x: object) -> object:
            return types.SimpleNamespace(max=lambda: 0)

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", _FakeSd)
    monkeypatch.setitem(__import__("sys").modules, "numpy", _FakeNp)
    result = audio.check_mic_capture(_CFG)
    assert result.status == "fail"
    assert "recorded silence" in result.detail
    assert "push-to-talk" not in result.detail


def test_soften_leaves_failures_alone_without_an_accessory() -> None:
    """No accessory means a missing local mic really is the whole story — the
    existing red failure must survive untouched."""
    original = audio.CheckResult("mic capture", "fail", "Array: no such device")
    assert audio._soften_for_push_to_talk(original, _present_non_xvf()) is original


def _local_mic_and_accessory() -> MicPresence:
    """A healthy non-XVF local mic on a box that ALSO has a remote paired."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


def test_headline_stays_ok_when_a_working_local_mic_also_has_a_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why ``check_microphone`` must NOT warn just because an accessory is
    paired.

    ``MicPresence`` has no local probe, so this record — present, no XVF
    enrichment, one accessory source — is byte-identical for two boxes: the
    push-to-talk-only one above, and a perfectly healthy speaker whose custom
    ``JASPER_MIC_DEVICE`` (a UMIK-2 corpus rig, a plain USB mic) resolves fine
    and happens to have a remote paired too. Warning on the record alone would
    put a permanent false yellow on the second box while its own ``mic ALSA
    card`` check reports green — the contradicting-checks scatter
    ``jasper.mic_presence`` exists to prevent.

    The distinguishing evidence lives in the checks that probe the device, and
    this test pins that they behave differently on the two boxes."""
    assert _local_mic_and_accessory() == _push_to_talk_only()

    monkeypatch.setattr(audio, "read_mic_presence", _local_mic_and_accessory)
    monkeypatch.setattr(
        audio, "check_alsa_card",
        lambda *a, **k: audio.CheckResult("mic ALSA card (Array)", "ok", "found"),
    )
    assert audio.check_microphone().status == "ok"
    # The local half is present, so the probing check says so — green, and NOT
    # softened into a push-to-talk advisory.
    card = audio.check_mic_card_matches_config(_CFG)
    assert card.status == "ok"
    assert "push-to-talk" not in card.detail

    # Same record, absent local device: only the probing check changes.
    monkeypatch.setattr(
        audio, "check_alsa_card",
        lambda *a, **k: audio.CheckResult("mic ALSA card (Array)", "fail", "absent"),
    )
    assert audio.check_microphone().status == "ok"
    assert audio.check_mic_card_matches_config(_CFG).status == "warn"
