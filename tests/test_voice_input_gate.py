# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice-input gate for jasper-voice.

Pins the pieces that keep a no-input box from crash-looping into
StartLimitAction=reboot:

1. jasper-voice.service gates ExecStart on the reconciler-written marker
   (ConditionPathExists), and parks (not crash-loops) on the
   mic-unavailable exit code.
2. The marker path agrees across the unit, the bash reconciler default,
   and the Python reader — a drift here silently breaks the gate.
3. The daemon exits VOICE_MIC_UNAVAILABLE_EXIT on a primary mic-open
   failure, and the doctor reports the parked state as expected-idle.
4. The gate is an OR over a local mic and a paired accessory mic
   (issue #2205): the accessory env path agrees across its owner, the unit,
   and env_load, and the bash reconciler carries no copy of it because it
   asks jasper.accessories.mic_env instead.
5. The gate owner PUBLISHES which half it resolved
   (JASPER_LOCAL_MIC_PRESENT), and the daemon's leg planner reads that
   published fact rather than re-deriving mic presence from its own config.
6. Closing the gate is AUDIBLE: the daemon plays the mic-loss cue once
   during its own shutdown when the marker is there, and the wait for it
   cannot outrun the unit's TimeoutStopSec (ADR-0238).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.mic_env import DEFAULT_ACCESSORY_MIC_ENV_FILE
from jasper.audio_io import InputDeviceUnavailable
from jasper.env_load import ENV_FILES
from jasper.voice.input_presence import (
    DEFAULT_VOICE_INPUT_ABSENT_MARKER,
    voice_input_absent_marker_path,
    voice_parked_no_mic,
)
from jasper.voice_daemon import (
    NO_ROOM_MIC_CUE_SLUG,
    VOICE_MIC_UNAVAILABLE_EXIT,
    WakeLoop,
)
from tests._log_events import event_fields, event_records

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "jasper-voice.service"
RECONCILE = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"


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
    text = _unit_text()
    success = _directive_values(text, "SuccessExitStatus")
    prevent = _directive_values(text, "RestartPreventExitStatus")
    code = str(VOICE_MIC_UNAVAILABLE_EXIT)
    # Both the mic-unavailable code (66) and the provider-unset code (78)
    # must park cleanly — neither may consume the reboot budget.
    assert code in success, success
    assert code in prevent, prevent
    assert "78" in success and "78" in prevent, (success, prevent)


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

    m = re.search(
        r'VOICE_INPUT_ABSENT_MARKER="\$\{JASPER_VOICE_INPUT_ABSENT_MARKER:-([^}]+)\}"',
        RECONCILE.read_text(),
    )
    assert m, "reconciler must define VOICE_INPUT_ABSENT_MARKER with a default"
    assert m.group(1) == DEFAULT_VOICE_INPUT_ABSENT_MARKER, m.group(1)


def test_accessory_mic_env_path_agreement() -> None:
    """The accessory half of the gate is one file with one writer. Its path is
    duplicated in the unit's EnvironmentFile= and env_load's list; pin both
    against the owning module, the same treatment the marker path gets."""
    unit_text = _unit_text()
    assert (
        f"EnvironmentFile=-{DEFAULT_ACCESSORY_MIC_ENV_FILE}" in unit_text
    ), DEFAULT_ACCESSORY_MIC_ENV_FILE
    assert DEFAULT_ACCESSORY_MIC_ENV_FILE in ENV_FILES, ENV_FILES


def test_reconciler_asks_python_for_the_accessory_half() -> None:
    """The bash reconciler must NOT carry a copy of the accessory env path or
    re-derive the entry format in shell — a second parser is how the gate and
    the daemon come to disagree about whether a source is published. It shells
    to the owning module instead (same posture as the mic-profile resolver)."""
    script = RECONCILE.read_text()
    assert "jasper.accessories.mic_env" in script
    # Prose may name the path and the key; executable shell may not.
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert DEFAULT_ACCESSORY_MIC_ENV_FILE not in code
    assert "JASPER_MANUAL_MIC_SOURCES" not in code


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


def test_reconciler_publishes_the_local_half_of_the_gate() -> None:
    """The gate owner must publish WHICH half it resolved.

    The marker is the AND of both absences, so it structurally cannot say
    "there is no local mic but a remote is paired" — and that is exactly the
    case the daemon has to serve. Without this published key the daemon would
    have to guess local-mic presence from its own config, which cannot work:
    `Config.mic_device` defaults to the literal "Array" and the reconciler
    writes a real candidate name on its no-mic paths.
    """
    code = "\n".join(
        line for line in RECONCILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert f"set_env_var \"$ENV_FILE\" {LOCAL_MIC_PRESENT_KEY}" in code


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
    from jasper.voice_daemon import _configured_wake_legs

    cfg = _config_with(
        monkeypatch,
        **{
            LOCAL_MIC_PRESENT_KEY: "0",
            "JASPER_MANUAL_MIC_SOURCES": "wiim_remote_2=udp:9892",
        },
    )
    assert _configured_wake_legs(cfg) == []

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
    assert [s.token for s, _ in _configured_wake_legs(cfg_with_mic)] == ["on"]


def test_voice_parked_no_mic_reads_marker(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "voice-input-absent"
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    assert voice_input_absent_marker_path() == str(marker)
    assert voice_parked_no_mic() is False
    marker.write_text("reason=test\n")
    assert voice_parked_no_mic() is True
    marker.unlink()
    assert voice_parked_no_mic() is False


def test_input_device_unavailable_carries_device() -> None:
    cause = ValueError("No input device matching 'Array'")
    exc = InputDeviceUnavailable("Array", cause)
    assert exc.device == "Array"
    assert "Array" in str(exc)
    # The original cause is preserved for the forensic log.
    assert "No input device matching" in str(exc)


def test_main_exits_mic_unavailable_code(monkeypatch) -> None:
    """main() must translate a primary mic-open failure into a clean
    VOICE_MIC_UNAVAILABLE_EXIT, not let it crash with a traceback (exit 1
    → systemd Restart=on-failure → crash-loop)."""
    from jasper.voice import daemon_main

    async def _boom() -> None:
        raise InputDeviceUnavailable("Array", ValueError("absent"))

    monkeypatch.setattr(daemon_main, "run", _boom)
    with pytest.raises(SystemExit) as exc:
        daemon_main.main()
    assert exc.value.code == VOICE_MIC_UNAVAILABLE_EXIT


def test_check_mic_capture_reports_expected_idle_when_marked(
    tmp_path, monkeypatch,
) -> None:
    from jasper.cli.doctor import audio

    monkeypatch.setattr(audio, "_parked_follower_result", lambda _label: None)
    marker = tmp_path / "voice-input-absent"
    marker.write_text("reason=test\n")
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))

    # Marker present → early ok return, before any device access, so a
    # bare stand-in cfg is enough.
    result = audio.check_mic_capture(SimpleNamespace())
    assert result.status == "skipped"
    assert result.reason == audio.REASON_MIC_ABSENT_DEFERRED


def _shutting_down_daemon(
    marked: bool, tmp_path, monkeypatch, *, transient: bool = False
):
    """A WakeLoop whose cue path runs for real, and a marker to match."""
    from tests.test_voice_daemon_manual_start_guard import _SpyCues

    marker = tmp_path / "voice-input-absent"
    if marked:
        body = "reason=no candidate microphone present\n"
        if transient:
            body += "transient=1\n"
        marker.write_text(body)
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    wake_loop = WakeLoop.for_tests()
    wake_loop._cues = _SpyCues()
    return wake_loop


@pytest.mark.parametrize(
    ("marked", "transient", "played", "result"),
    [
        (True, False, True, "ok"),
        (False, False, False, "not_parked"),
        (True, True, False, "transient_park"),
    ],
    ids=("no-mic", "plain-restart", "transient-park"),
)
async def test_the_mic_loss_cue_follows_the_marker_at_shutdown(
    marked: bool, transient: bool, played: bool, result: str,
    tmp_path, monkeypatch, caplog,
) -> None:
    """The daemon's own stop is the transition into deafness, because the
    reconciler writes the marker and only then stops jasper-voice, and
    ConditionPathExists=! refuses every start while it stands (ADR-0238).
    So the marker at shutdown decides, and a plain restart stays silent —
    including in the journal, so `voice.mic_loss_cue` means a real loss.
    transient-park is the chip-AEC validation bounce's own park
    (`transient=1`): marked, so it still logs, but never a real absence, so
    the cue is skipped."""
    from jasper.voice import daemon_main

    wake_loop = _shutting_down_daemon(
        marked, tmp_path, monkeypatch, transient=transient
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        actual_result = await daemon_main._announce_mic_loss_at_shutdown(wake_loop)

    assert wake_loop._cues.played == ([NO_ROOM_MIC_CUE_SLUG] if played else [])
    assert actual_result == result
    records = event_records(caplog, "voice.mic_loss_cue")
    assert [record.levelno for record in records] == (
        [logging.INFO] if result in ("ok", "transient_park") else []
    )


@pytest.mark.parametrize("outcome", ["play_error", "timeout"])
async def test_a_cue_that_cannot_play_warns_and_the_stop_still_finishes(
    outcome: str, tmp_path, monkeypatch, caplog,
) -> None:
    """A dead output path (outputd down, cue never baked) must not hold the
    daemon past jasper-voice.service's TimeoutStopSec — SIGKILL mid-teardown
    is a worse failure than a missed cue. Both shapes end the same way: the
    code is named on the wire, at WARNING, and the caller gets it back."""
    from jasper.voice import daemon_main

    wake_loop = _shutting_down_daemon(True, tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_main, "MIC_LOSS_CUE_WAIT_SEC", 0.01)

    async def _cannot_play(_slug: str) -> str:
        if outcome == "timeout":
            await asyncio.sleep(60)
        raise RuntimeError("no output path")

    wake_loop.play_cue = _cannot_play

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await daemon_main._announce_mic_loss_at_shutdown(wake_loop)

    assert result == outcome
    assert event_fields(caplog, "voice.mic_loss_cue")["result"] == outcome
    (record,) = event_records(caplog, "voice.mic_loss_cue")
    assert record.levelno == logging.WARNING
