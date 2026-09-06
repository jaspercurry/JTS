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
   failure — and VOICE_PROVIDER_NOT_CONFIGURED_EXIT with no provider —
   announcing each park with a cue first; the doctor reports the parked
   state as expected-idle.
4. The gate is an OR over a local mic and a paired accessory mic
   (issue #2205): the accessory env path agrees across its owner, the unit,
   and env_load, and the bash reconciler carries no copy of it because it
   asks jasper.accessories.mic_env instead.
5. The gate owner PUBLISHES which half it resolved
   (JASPER_LOCAL_MIC_PRESENT), and the daemon's leg planner reads that
   published fact rather than re-deriving mic presence from its own config.
6. Closing the gate is AUDIBLE: the daemon plays the mic-loss cue once
   during its own shutdown when the marker is there, and the unit's
   TimeoutStopSec clears MIC_LOSS_CUE_STOP_FLOOR_SEC (ADR-0239).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.mic_env import DEFAULT_ACCESSORY_MIC_ENV_FILE
from jasper.audio_io import InputDeviceUnavailable
from jasper.config import VoiceProviderNotConfigured
from jasper.env_load import ENV_FILES
from jasper.mic_presence import (
    MIC_ABSENT_CHIP_AEC_VALIDATING,
    MIC_ABSENT_NO_LOCAL_OR_ACCESSORY,
)
from jasper.voice.input_presence import (
    DEFAULT_VOICE_INPUT_ABSENT_MARKER,
    voice_input_absent_marker_path,
    voice_parked_no_mic,
)
from jasper.voice_daemon import (
    NO_ROOM_MIC_CUE_SLUG,
    VOICE_MIC_UNAVAILABLE_EXIT,
    VOICE_NOT_SET_UP_CUE_SLUG,
    VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
    WakeLoop,
)
from tests._log_events import event_fields, event_records
from tests.systemd_unit_helpers import value_for

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
    marker.write_text(f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n")
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


class _ParkPlayout:
    """Stands in for TtsPlayout in the boot-park cue: an async context
    manager that either opens, or fails its connect the way a dead fan-in
    socket does. Callable, so it also stands in for the class itself."""

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self.connect_error = connect_error

    def __call__(self, **_kwargs) -> "_ParkPlayout":
        return self

    async def __aenter__(self) -> "_ParkPlayout":
        if self.connect_error is not None:
            raise self.connect_error
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


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

    spy = _ParkCues(cue_result)
    monkeypatch.setattr(daemon_main, "run", _boom)
    monkeypatch.setattr(
        daemon_main, "TtsPlayout", _ParkPlayout(connect_error=connect_error),
    )
    monkeypatch.setattr(daemon_main, "build_env_cue_manager", lambda **_kw: spy)
    return daemon_main, spy


@pytest.mark.parametrize(
    ("exc", "code", "slug"),
    [
        (
            InputDeviceUnavailable("Array", ValueError("absent")),
            VOICE_MIC_UNAVAILABLE_EXIT,
            NO_ROOM_MIC_CUE_SLUG,
        ),
        (
            VoiceProviderNotConfigured("no voice provider configured"),
            VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
            VOICE_NOT_SET_UP_CUE_SLUG,
        ),
    ],
    ids=("mic-unavailable", "not-set-up"),
)
def test_a_boot_park_is_announced_before_main_exits(
    exc: Exception, code: int, slug: str, monkeypatch,
) -> None:
    """Both park codes must reach systemd unchanged — a park that crashed
    with a traceback would be exit 1 → Restart=on-failure → crash-loop — and
    both must have said so out loud first. Every check that raises these runs
    before the daemon's own cue manager exists, so this is the largest window
    in which the speaker goes deaf with nothing spoken (non-negotiable 6).
    The mic path reuses the cue ADR-0239 speaks for the same fact at
    shutdown; only the unconfigured path needs its own."""
    daemon_main, spy = _parking_daemon(exc, monkeypatch)

    with pytest.raises(SystemExit) as raised:
        daemon_main.main()

    assert raised.value.code == code
    # Recorded at all means recorded before the exit: nothing plays after it.
    assert spy.played == [slug]


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
        InputDeviceUnavailable("Array", ValueError("absent")),
        monkeypatch,
        connect_error=connect_error,
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_MIC_UNAVAILABLE_EXIT
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
        InputDeviceUnavailable("Array", ValueError("absent")),
        monkeypatch,
        cue_result=KeyboardInterrupt(),
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_MIC_UNAVAILABLE_EXIT
    assert spy.played == [NO_ROOM_MIC_CUE_SLUG]
    assert event_fields(caplog, "voice.park_cue")["result"] == "interrupted"


def test_a_park_cue_with_no_cached_asset_is_named_play_failed(
    monkeypatch, caplog,
) -> None:
    """A play() that returns False has five causes (unknown slug, no
    playout, no cached file, an unreadable one, a write that failed), so the
    event carries the shared `play_failed` vocabulary the wake loop uses —
    not a label claiming the asset specifically was missing."""
    daemon_main, spy = _parking_daemon(
        InputDeviceUnavailable("Array", ValueError("absent")),
        monkeypatch,
        cue_result=False,
    )

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        with pytest.raises(SystemExit) as raised:
            daemon_main.main()

    assert raised.value.code == VOICE_MIC_UNAVAILABLE_EXIT
    assert spy.played == [NO_ROOM_MIC_CUE_SLUG]
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


def _shutting_down_daemon(
    marked: bool, tmp_path, monkeypatch, *, transient: bool = False
):
    """A WakeLoop whose cue path runs for real, and a marker to match."""
    from tests.test_voice_daemon_manual_start_guard import _SpyCues

    marker = tmp_path / "voice-input-absent"
    if marked:
        code = (
            MIC_ABSENT_CHIP_AEC_VALIDATING if transient
            else MIC_ABSENT_NO_LOCAL_OR_ACCESSORY
        )
        marker.write_text(f"reason={code}\n")
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
    ConditionPathExists=! refuses every start while it stands (ADR-0239).
    So the marker at shutdown decides, and a plain restart stays silent —
    including in the journal, so `voice.mic_loss_cue` means a real loss.
    transient-park is the chip-AEC validation bounce's own park
    (`reason=chip_aec_validating`, a transient code): marked, so it still
    logs, but never a real absence, so the cue is skipped."""
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


async def test_a_cue_that_cannot_play_warns_and_the_stop_still_finishes(
    tmp_path, monkeypatch, caplog,
) -> None:
    """A dead output path (outputd down, cue never baked) must not take the
    shutdown down with it: the code is named on the wire, at WARNING, and the
    caller gets it back."""
    from jasper.voice import daemon_main

    wake_loop = _shutting_down_daemon(True, tmp_path, monkeypatch)

    async def _cannot_play(_slug: str) -> str:
        raise RuntimeError("no output path")

    wake_loop.play_cue = _cannot_play

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await daemon_main._announce_mic_loss_at_shutdown(wake_loop)

    assert result == "play_error"
    assert event_fields(caplog, "voice.mic_loss_cue")["result"] == "play_error"
    (record,) = event_records(caplog, "voice.mic_loss_cue")
    assert record.levelno == logging.WARNING


def test_stop_budget_clears_the_mic_loss_cue_floor() -> None:
    """The cue runs its natural length through the daemon's owned ducked
    output — nothing bounds it — so the unit's stop budget has to cover it
    (ADR-0239). Derived from the unit file, not restated."""
    from jasper.voice.daemon_main import MIC_LOSS_CUE_STOP_FLOOR_SEC

    raw = value_for(_unit_text(), "TimeoutStopSec")
    assert raw is not None and raw.endswith("s"), raw
    assert float(raw[:-1]) >= MIC_LOSS_CUE_STOP_FLOOR_SEC, raw
