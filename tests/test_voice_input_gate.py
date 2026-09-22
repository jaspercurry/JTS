# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior of the persistent voice-input gate and boot park codes."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.mic_env import DEFAULT_ACCESSORY_MIC_ENV_FILE
from jasper.mic_capture import InputDeviceUnavailable
from jasper.env_load import ENV_FILES
from jasper.mic_presence import (
    MIC_ABSENT_NO_LOCAL_OR_ACCESSORY,
)
from jasper.voice.input_presence import (
    DEFAULT_VOICE_INPUT_ABSENT_MARKER,
    voice_input_absent_marker_path,
    voice_parked_no_mic,
)
from jasper.cues.registry import (
    VOICE_ASSETS_MISSING_CUE_SLUG,
    VOICE_NOT_SET_UP_CUE_SLUG,
)
from jasper.config import VoiceConfigError, VoiceProviderNotConfigured
from jasper.vad import SpeechVADSetupError
from jasper.voice_daemon import (
    VOICE_MIC_UNAVAILABLE_EXIT,
    VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
    VOICE_STARTUP_CONFIG_ERROR_EXIT,
)
from tests._log_events import event_fields
from tests._playout import FakeTts

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "jasper-voice.service"


def _unit_text() -> str:
    return UNIT.read_text()


def _directive_values(text: str, key: str) -> list[str]:
    """Tokens of the last `key=` line in a unit file (systemd: last wins)."""
    vals: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            vals = line[len(key) + 1:].split()
    return vals


def test_voice_service_has_mic_presence_condition() -> None:
    text = _unit_text()
    cond = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("ConditionPathExists=")
    ]
    assert cond, "jasper-voice.service must gate on the mic-presence marker"
    # Negated (fail-open): "run UNLESS the marker exists".
    assert any(c == f"ConditionPathExists=!{DEFAULT_VOICE_INPUT_ABSENT_MARKER}"
               for c in cond), cond


def test_voice_service_starts_udp_mic_producer_softly() -> None:
    """The default/reconciler-managed mic is udp:9876, produced by
    jasper-aec-bridge. If voice starts while the bridge is inactive, the UDP
    capture socket binds but receives no frames, and voice watchdog-restarts.

    The want is still SOFT — bridge restarts must not cascade-stop an
    otherwise healthy voice daemon — but it is no longer static. `Wants=`
    starts a unit even when it is disabled, so a static one re-pulls the whole
    AEC stack onto a box whose mic never touches it: a streambox answering
    through a paired Bluetooth remote reads the accessory path (udp:9892)
    instead. jasper-aec-reconcile already decides whether the bridge runs, so
    it writes and removes the want beside those same calls; that half is
    pinned by test_voice_wants_the_bridge_only_while_the_bridge_carries_the_mic
    in tests/test_aec_reconcile.py.

    After= stays in the unit: pure ordering, free when the bridge is absent.
    """
    text = _unit_text()
    after = _directive_values(text, "After")
    wants = _directive_values(text, "Wants")
    assert "jasper-aec-bridge.service" in after, after
    assert "jasper-aec-bridge.service" not in wants, wants
    assert "Requires=jasper-aec-bridge.service" not in text
    # The soft-dependency guarantee this test is named for still holds for the
    # units that ARE unconditional.
    assert "jasper-fanin.service" in wants, wants


def test_voice_service_parks_on_mic_unavailable_exit() -> None:
    """The mic-unavailable code must park cleanly (never consume the reboot
    budget) — checked against the live Python constant, not a hardcoded
    literal. The exact "66 78" SuccessExitStatus/RestartPreventExitStatus
    set (66 is this constant; 78 is the provider-unset code) is pinned, with
    the rest of the restart ladder, by tests/test_systemd_hardening.py's
    RESTART_POLICY table (R22, #4416)."""
    text = _unit_text()
    success = _directive_values(text, "SuccessExitStatus")
    prevent = _directive_values(text, "RestartPreventExitStatus")
    code = str(VOICE_MIC_UNAVAILABLE_EXIT)
    assert code in success, success
    assert code in prevent, prevent


def test_marker_path_agreement() -> None:
    """The marker path is duplicated in three places (unit literal, bash
    default, Python default). A mismatch silently disables the gate, so
    pin it."""
    unit_text = _unit_text()
    unit_paths = [
        line.strip()[len("ConditionPathExists=!"):]
        for line in unit_text.splitlines()
        if line.strip().startswith("ConditionPathExists=!")
    ]
    assert unit_paths == [DEFAULT_VOICE_INPUT_ABSENT_MARKER], unit_paths



def test_accessory_mic_env_path_agreement() -> None:
    """The accessory half of the gate is one file with one writer. Its path is
    duplicated in the unit's EnvironmentFile= and env_load's list; pin both
    against the owning module, the same treatment the marker path gets."""
    unit_text = _unit_text()
    assert (
        f"EnvironmentFile=-{DEFAULT_ACCESSORY_MIC_ENV_FILE}" in unit_text
    ), DEFAULT_ACCESSORY_MIC_ENV_FILE
    assert DEFAULT_ACCESSORY_MIC_ENV_FILE in ENV_FILES, ENV_FILES



def test_gate_owner_can_actually_report_enabled() -> None:
    """``refresh_voice_input`` starts the gate owner ONLY when systemd reports
    ``UnitFileState=enabled``. That permission is unreachable for a unit with no
    ``[Install]`` section — systemd reports such a unit ``static`` — and it is
    only ever granted because ``install.sh`` enables it.

    Drop either and the accessory half of the gate goes dead on every full
    speaker with **no other test failing**: the reconciler would classify a
    perfectly healthy owner as ``parked``, fall through to ``try-restart``, and
    a freshly-paired remote would never re-derive the marker. Observed
    ``LoadState=loaded UnitFileState=enabled`` on jts3 (2026-08-07)."""
    owner_unit = ROOT / "deploy" / "systemd" / "jasper-aec-reconcile.service"
    assert "[Install]" in owner_unit.read_text(), owner_unit
    installer = (ROOT / "deploy" / "install.sh").read_text()
    enable_lines = [
        line.strip() for line in installer.splitlines()
        if "systemctl enable" in line and "jasper-aec-reconcile.service" in line
        and not line.lstrip().startswith("#")
    ]
    assert enable_lines, "install.sh must enable the voice-input gate owner"


LOCAL_MIC_PRESENT_KEY = "JASPER_LOCAL_MIC_PRESENT"



def _config_with(monkeypatch, **env) -> object:
    from jasper.config import Config

    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


@pytest.mark.parametrize(
    "published,expected",
    [("1", True), ("0", False), ("unknown", None)],
)
def test_config_reads_the_published_local_mic_verdict(
    monkeypatch, published, expected,
) -> None:
    """Tri-state, and the third state is load-bearing: `unknown` must NOT
    collapse into `absent`, or a custom mic the reconciler declines to
    resolve would silently lose its wake leg."""
    cfg = _config_with(monkeypatch, **{LOCAL_MIC_PRESENT_KEY: published})
    assert cfg.local_mic_present is expected


def test_absent_key_reads_as_unresolved(monkeypatch) -> None:
    """No reconcile has ever run (fresh box, first boot). Must read as
    "unknown", never as "no mic" — the daemon's pre-#2205 behaviour."""
    monkeypatch.delenv(LOCAL_MIC_PRESENT_KEY, raising=False)
    cfg = _config_with(monkeypatch)
    assert cfg.local_mic_present is None


def test_published_verdict_reaches_the_leg_planner(monkeypatch) -> None:
    """End to end across the key name: the value the reconciler publishes is
    the value that drops the primary wake leg.

    Renaming the key on either side breaks this even though both sides would
    still be internally consistent — which is the drift this pins.
    """
    from jasper.voice.wake_detect import configured_wake_legs

    cfg = _config_with(
        monkeypatch,
        **{
            LOCAL_MIC_PRESENT_KEY: "0",
            "JASPER_MANUAL_MIC_SOURCES": "wiim_remote_2=udp:9892",
        },
    )
    assert configured_wake_legs(cfg) == []

    # Control: the SAME config with the local half resolved present keeps the
    # primary leg, so the empty plan above is the published verdict's doing
    # and not an artefact of the accessory source alone.
    cfg_with_mic = _config_with(
        monkeypatch,
        **{
            LOCAL_MIC_PRESENT_KEY: "1",
            "JASPER_MANUAL_MIC_SOURCES": "wiim_remote_2=udp:9892",
        },
    )
    assert [s.token for s, _ in configured_wake_legs(cfg_with_mic)] == ["on"]


def test_voice_parked_no_mic_reads_marker(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "voice-input-absent"
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    assert voice_input_absent_marker_path() == str(marker)
    assert voice_parked_no_mic() is False
    marker.write_text(f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n")
    assert voice_parked_no_mic() is True
    marker.unlink()
    assert voice_parked_no_mic() is False


def test_input_device_unavailable_carries_device() -> None:
    cause = ValueError("No input device matching 'Array'")
    exc = InputDeviceUnavailable("Array", cause)
    assert exc.device == "Array"
    # The original cause is preserved for the forensic log.
    assert type(cause).__name__ in str(exc) and str(cause) in str(exc)


class _ParkCues:
    """Cue-manager stand-in for the boot-park path: records what was asked
    for, then returns a play() verdict or raises the failure under test."""

    def __init__(self, result: bool | BaseException = True) -> None:
        self._result = result
        self.played: list[str] = []

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _parking_daemon(
    exc: Exception,
    monkeypatch,
    *,
    connect_error: BaseException | None = None,
    cue_result: bool | BaseException = True,
):
    """main() whose run() raises `exc` and whose cue path is spied on."""
    from jasper.voice import daemon_main

    async def _boom() -> None:
        raise exc

    from jasper.cues import park as cue_park

    spy = _ParkCues(cue_result)
    monkeypatch.setattr(daemon_main, "run", _boom)
    playout = FakeTts(connect_error=connect_error)
    monkeypatch.setattr(cue_park, "TtsPlayout", lambda **_kw: playout)
    monkeypatch.setattr(cue_park, "build_env_cue_manager", lambda **_kw: spy)
    return daemon_main, spy


@pytest.mark.parametrize(
    ("exc", "code", "slug", "event"),
    [
        (
            InputDeviceUnavailable("Array", ValueError("absent")),
            VOICE_MIC_UNAVAILABLE_EXIT,
            None,
            "voice.mic_unavailable",
        ),
        (
            VoiceProviderNotConfigured("no voice provider configured"),
            VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
            VOICE_NOT_SET_UP_CUE_SLUG,
            "voice.unconfigured",
        ),
        (
            SpeechVADSetupError("silero_vad.onnx is missing"),
            VOICE_STARTUP_CONFIG_ERROR_EXIT,
            VOICE_ASSETS_MISSING_CUE_SLUG,
            "voice.vad_setup_failed",
        ),
        (
            VoiceConfigError("JASPER_IDLE_TIMEOUT_SEC must be a number"),
            VOICE_STARTUP_CONFIG_ERROR_EXIT,
            VOICE_ASSETS_MISSING_CUE_SLUG,
            "voice.config_invalid",
        ),
    ],
    ids=("mic-unavailable", "not-set-up", "vad-setup-failed", "config-invalid"),
)
def test_boot_parks_keep_their_exit_code_and_cue_policy(
    exc: Exception, code: int, slug: str | None, event: str, monkeypatch, caplog,
) -> None:
    daemon_main, spy = _parking_daemon(exc, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == code
    # Recorded at all means recorded before the exit: nothing plays after it.
    assert spy.played == ([slug] if slug else [])
    assert event_fields(caplog, event)


@pytest.mark.parametrize(
    "connect_error",
    [
        OSError("no output path"),
        # TtsPlayout raises TimeoutError on its own 1.0 s connect bound, and
        # `asyncio.TimeoutError is TimeoutError` on 3.11+ — so a connect that
        # timed out must NOT be reported as the 12 s park-cue cap expiring.
        TimeoutError("TTS IPC connect timed out after 1.0s"),
    ],
    ids=("connect-refused", "connect-timed-out"),
)
def test_a_park_cue_that_cannot_play_still_parks_with_the_same_code(
    connect_error: BaseException, monkeypatch, caplog,
) -> None:
    """A dead output path must not take the park with it: the cue never
    changes the exit code, and the failure is named on the wire so a support
    read can tell "nobody heard it" from "nobody was there"."""
    daemon_main, spy = _parking_daemon(
        VoiceProviderNotConfigured("not configured"),
        monkeypatch,
        connect_error=connect_error,
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_PROVIDER_NOT_CONFIGURED_EXIT
    assert spy.played == []
    assert event_fields(caplog, "voice.park_cue")["result"] == "play_error"


def test_a_park_cue_interrupted_still_parks_with_the_same_code(
    monkeypatch, caplog,
) -> None:
    """Ctrl-C on a hand-run daemon lands inside the cue, which runs inside
    main()'s except handler: a BaseException escaping there skips
    `sys.exit(code)` and the process exits 1 — neither systemd's success
    code nor its restart-prevent one."""
    daemon_main, spy = _parking_daemon(
        VoiceProviderNotConfigured("not configured"),
        monkeypatch,
        cue_result=KeyboardInterrupt(),
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_PROVIDER_NOT_CONFIGURED_EXIT
    assert spy.played == [VOICE_NOT_SET_UP_CUE_SLUG]
    assert event_fields(caplog, "voice.park_cue")["result"] == "interrupted"


def test_a_park_cue_with_no_cached_asset_is_named_play_failed(
    monkeypatch, caplog,
) -> None:
    """A play() that returns False has five causes (unknown slug, no
    playout, no cached file, an unreadable one, a write that failed), so the
    event carries the shared `play_failed` vocabulary the wake loop uses —
    not a label claiming the asset specifically was missing."""
    daemon_main, spy = _parking_daemon(
        VoiceProviderNotConfigured("not configured"),
        monkeypatch,
        cue_result=False,
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_PROVIDER_NOT_CONFIGURED_EXIT
    assert spy.played == [VOICE_NOT_SET_UP_CUE_SLUG]
    assert event_fields(caplog, "voice.park_cue")["result"] == "play_failed"


def test_check_mic_capture_reports_expected_idle_when_marked(
    tmp_path, monkeypatch,
) -> None:
    from jasper.cli.doctor import audio

    monkeypatch.setattr(audio, "_parked_follower_result", lambda _label: None)
    marker = tmp_path / "voice-input-absent"
    marker.write_text(f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n")
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))

    # Marker present → early ok return, before any device access, so a
    # bare stand-in cfg is enough.
    result = audio.check_mic_capture(SimpleNamespace())
    assert result.status == "skipped"
    assert result.reason == audio.REASON_MIC_ABSENT_DEFERRED
