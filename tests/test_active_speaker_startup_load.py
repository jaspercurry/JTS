# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import jasper.active_speaker.commission_load as commission_load_mod
import jasper.active_speaker.startup_load as startup_load_mod
from jasper.active_speaker.startup_hold import startup_hold_marker_path
from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.path_safety import (
    build_startup_load_path_safety_evidence,
    write_path_safety_evidence,
)
from jasper.active_speaker.staging import stage_protected_startup_config
from jasper.active_speaker.startup_load import (
    STARTUP_LOAD_PREFLIGHT_KIND,
    build_startup_load_preflight,
    load_protected_startup_config,
    load_startup_load_state,
    rollback_protected_startup_config,
)
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
)
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    repin_composite_child_serials,
)
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)


class FakeCamilla:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_paths: list[str] = []

    async def get_config_file_path(self) -> str:
        return self.current_path

    async def set_config_file_path(self, path: str) -> bool:
        self.current_path = path
        self.loaded_paths.append(path)
        return True


class SnapshotFailingCamilla(FakeCamilla):
    async def get_config_file_path(self) -> str:
        raise RuntimeError("camilla unavailable")


def _record_reconcile_triggers(monkeypatch, *, ok: bool = True) -> list[dict]:
    calls: list[dict] = []

    def fake_manage_units(*units: str, **kwargs):
        calls.append({"units": units, **kwargs})
        return {"ok": ok, "rc": 0 if ok else 3}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)
    return calls


def _topology(*, identity_verified: bool = True) -> OutputTopology:
    return mono_output_topology(identity_verified=identity_verified)


def _staged(tmp_path: Path) -> dict:
    return stage_protected_startup_config(
        _topology(),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )


def _protected_prior(tmp_path: Path, staged: dict, name: str = "prior_active.yml") -> Path:
    prior = tmp_path / name
    prior.write_text(
        Path(staged["config"]["path"]).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return prior


def _normal_prior(tmp_path: Path, name: str = "prior_stereo.yml") -> Path:
    prior = tmp_path / name
    prior.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "devices:\n"
        "  volume_limit: 0\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: outputd_content_playback\n"
        "    channels: 2\n",
        encoding="utf-8",
    )
    return prior


def _write_path_safety(
    path: Path,
    *,
    topology: OutputTopology | None = None,
    staged: dict,
    current_config_path: str | Path | None = None,
    require_physical_identity: bool = True,
) -> Path:
    evidence = build_startup_load_path_safety_evidence(
        topology or _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=current_config_path or staged["config"]["path"],
        require_physical_identity=require_physical_identity,
    )
    return write_path_safety_evidence(evidence, path=path)


def test_startup_load_preflight_blocks_without_path_safety(
    tmp_path: Path,
) -> None:
    report = build_startup_load_preflight(
        _topology(),
        staged_config=_staged(tmp_path),
        validate=_valid_config,
    )

    assert report["kind"] == STARTUP_LOAD_PREFLIGHT_KIND
    assert report["status"] == "blocked"
    assert report["load_allowed"] is False
    assert "path_safety_evidence_missing" in {
        issue["code"] for issue in report["issues"]
    }
    assert "stop_control_available" not in {
        gate["id"] for gate in report["required_gates"]
    }


def test_startup_and_commission_load_artifacts_own_independent_schema_versions(
    monkeypatch,
    tmp_path: Path,
):
    assert startup_load_mod.STARTUP_LOAD_SCHEMA_VERSION == 1
    assert commission_load_mod.COMMISSION_LOAD_SCHEMA_VERSION == 1
    assert not hasattr(startup_load_mod, "SCHEMA_VERSION")

    monkeypatch.setattr(startup_load_mod, "STARTUP_LOAD_SCHEMA_VERSION", 2)
    monkeypatch.setattr(commission_load_mod, "COMMISSION_LOAD_SCHEMA_VERSION", 3)
    assert startup_load_mod._base_state(tmp_path / "startup.json")[
        "artifact_schema_version"
    ] == 2
    assert commission_load_mod._commission_base_state(tmp_path / "commission.json")[
        "artifact_schema_version"
    ] == 3


def test_startup_load_preflight_requires_level_floor(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    report = build_startup_load_preflight(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(requested_level_dbfs=-70),
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
        ),
        validate=_valid_config,
    )

    assert report["status"] == "blocked"
    assert report["calibration_level"]["at_floor"] is False
    assert "calibration_level_not_at_floor" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_preflight_blocks_stale_staged_topology(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][1]["protection_status"] = "present"
    topology = OutputTopology.from_mapping(raw)

    report = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
        ),
        validate=_valid_config,
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["staged_topology"]["matched"] is False
    assert gates["staged_topology_matches_current"] is False
    assert "staged_targets_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_preflight_allows_identity_audition_mode(
    tmp_path: Path,
) -> None:
    topology = _topology(identity_verified=False)
    staged = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )

    strict = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "strict_path_safety.json",
            topology=topology,
            staged=staged,
        ),
        validate=_valid_config,
    )
    audition = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "audition_path_safety.json",
            topology=topology,
            staged=staged,
            require_physical_identity=False,
        ),
        require_physical_identity=False,
        validate=_valid_config,
    )
    strict_gates = {gate["id"]: gate["passed"] for gate in strict["required_gates"]}
    audition_gates = {
        gate["id"]: gate["passed"] for gate in audition["required_gates"]
    }

    assert strict["status"] == "blocked"
    assert strict_gates["physical_identity_verified"] is False
    assert audition["status"] == "ready"
    assert audition["load_allowed"] is True
    assert audition["identity"]["physical_identity_required"] is False
    assert audition_gates["physical_identity_verified"] is True
    assert audition["path_safety"]["binding"]["checks"][
        "target_assignment_signature"
    ] is True


def test_startup_load_preflight_blocks_stale_path_safety_rollback_binding(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior_a = _protected_prior(tmp_path, staged, "prior_a.yml")
    prior_b = _protected_prior(tmp_path, staged, "prior_b.yml")

    report = build_startup_load_preflight(
        _topology(),
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
            current_config_path=prior_a,
        ),
        current_config_path=prior_b,
        validate=_valid_config,
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["path_safety"]["load_gate"] == "evidence_stale"
    assert gates["path_safety_matches_current_startup_load"] is False
    assert "path_safety_evidence_stale" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_blocks_when_rollback_anchor_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    missing_prior = tmp_path / "missing-prior.yml"
    fake = FakeCamilla(str(missing_prior))
    state_path = tmp_path / "startup_load.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=missing_prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert result["preflight"]["load_allowed"] is False
    assert result["load"]["status"] == "blocked"
    assert fake.loaded_paths == []
    assert "rollback_target_available_not_verified" in {
        issue["code"] for issue in result["preflight"]["issues"]
    }
    assert state["status"] == "blocked"
    assert state["rollback_available"] is False


def test_startup_load_records_normal_rollback_state(monkeypatch, tmp_path: Path) -> None:
    stage = _staged(tmp_path)
    prior = _normal_prior(tmp_path)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=stage,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "loaded"
    assert fake.loaded_paths == [stage["config"]["path"]]
    assert state["rollback_available"] is True
    assert state["previous_config_path"] == str(prior)
    assert state["candidate_config_path"] == stage["config"]["path"]
    assert reconcile_calls == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "active_speaker_startup_load",
        "no_block": False,
        "timeout": 15.0,
    }]


def test_startup_load_rolls_back_to_prior_config(monkeypatch, tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    load = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    rollback = asyncio.run(
        rollback_protected_startup_config(
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert load["load"]["status"] == "loaded"
    assert rollback["rollback"]["status"] == "rolled_back"
    assert fake.loaded_paths[-1] == str(prior)
    assert state["status"] == "rolled_back"
    assert state["rollback_available"] is False
    assert [
        (call["units"], call["verb"], call["reason"], call["no_block"])
        for call in reconcile_calls
    ] == [
        (
            (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
            "start",
            "active_speaker_startup_load",
            False,
        ),
        (
            (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
            "start",
            "active_speaker_startup_rollback",
            False,
        ),
    ]


def test_startup_load_sets_staged_hold_and_rollback_clears_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # The writer wiring for the re-commission deadlock guard: a successful
    # protected startup load holds the staged anchor (so the reconcile it kicks
    # preserves it), and a rollback clears the hold (so the box's baseline can be
    # restored again).
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    marker = startup_hold_marker_path()

    assert not marker.exists()
    load = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    assert load["load"]["status"] == "loaded"
    assert marker.exists()  # anchor is held while the commission is in flight

    rollback = asyncio.run(
        rollback_protected_startup_config(
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            state_path=state_path,
            validate=_valid_config,
        )
    )
    assert rollback["rollback"]["status"] == "rolled_back"
    assert not marker.exists()  # hold released; baseline restore is allowed again


def test_startup_load_refuses_when_the_staged_hold_cannot_be_taken(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # The hold's write is what keeps the reconcile this load kicks from restoring
    # the saved baseline over the anchor. When it cannot be taken — the shape
    # jasper-web hit on hardware before the unit declared
    # RuntimeDirectory=jasper-active-speaker, where /run is read-only under
    # ProtectSystem=strict — the load must refuse instead of answering success
    # for durable work the next reconcile would undo, and must apply nothing.
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    monkeypatch.setattr(startup_load_mod, "hold_staged_startup", lambda: False)

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    # The preflight itself still passes — the refusal is the hold, not a gate.
    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "blocked"
    assert result["load"]["last_action"] == "load_blocked"
    assert "staged_startup_hold_unavailable" in {
        issue["code"] for issue in result["load"]["issues"]
    }
    # Nothing applied, nothing kicked: no DSP load, and no reconcile to undo it.
    assert fake.loaded_paths == []
    assert reconcile_calls == []
    assert state["status"] == "blocked"
    assert state["rollback_available"] is False
    # The blocker names the directory the writing unit has to own.
    message = next(
        issue["message"]
        for issue in result["load"]["issues"]
        if issue["code"] == "staged_startup_hold_unavailable"
    )
    assert "RuntimeDirectory=jasper-active-speaker" in message


def test_startup_load_releases_the_staged_hold_when_the_apply_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # The hold is taken before the apply, so a failed apply — which leaves the
    # anchor off the durable statefile — has to give it back, or the next
    # reconcile would preserve an anchor this session never loaded.
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    marker = startup_hold_marker_path()

    # Spy through the real writer so the final `not marker.exists()` cannot pass
    # vacuously — it has to mean "taken, then given back", not "never taken".
    held: list[bool] = []
    real_hold = startup_load_mod.hold_staged_startup

    def spy_hold() -> bool:
        taken = real_hold()
        held.append(taken and marker.exists())
        return taken

    monkeypatch.setattr(startup_load_mod, "hold_staged_startup", spy_hold)

    async def refuse_load(_path: str) -> bool:
        return False

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=refuse_load,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )

    assert result["load"]["status"] == "failed"
    assert held == [True]  # the hold really was taken before the apply
    assert not marker.exists()  # and given back when the apply failed


def test_startup_load_proceeds_when_a_root_owned_marker_already_holds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # End-to-end shape of the gate's blocker: /sound/room/ (root, UMask=0077)
    # left a root:root 0600 marker that nothing releases, then /sound/ runs the
    # protected load. touch() raises there, but the anchor IS held, so the load
    # must PROCEED rather than refuse with a remedy that cannot fix it.
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    marker = startup_hold_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")  # the sibling root writer's leftover

    real_touch = Path.touch

    def touch_denied_for_the_marker(self, *args, **kwargs):
        if self == marker:
            raise PermissionError(13, "Permission denied")
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", touch_denied_for_the_marker)

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )

    assert result["load"]["status"] == "loaded"
    assert fake.loaded_paths == [staged["config"]["path"]]
    assert reconcile_calls  # the reconcile was kicked, under a real hold
    assert marker.exists()


def test_startup_load_releases_the_staged_hold_on_a_non_dsp_apply_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # apply_dsp_config raises types that do NOT subclass DspApplyError on the
    # writer-lock path the web surfaces contend for. Those escape the handler
    # that renders a payload, so without the catch-all the pre-apply hold would
    # leak and keep preserving a silent anchor this session never loaded.
    from jasper.dsp_apply import DspWriterLockTimeout

    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    marker = startup_hold_marker_path()

    async def raise_lock_timeout(**_kwargs):
        assert marker.exists()  # the hold really was taken before the apply
        raise DspWriterLockTimeout(
            tmp_path / "dsp.lock",
            timeout_s=5.0,
            waited_s=5.0,
            source="active_speaker_startup_load",
        )

    monkeypatch.setattr(startup_load_mod, "apply_dsp_config", raise_lock_timeout)

    with pytest.raises(DspWriterLockTimeout):
        asyncio.run(
            load_protected_startup_config(
                _topology(),
                load_config=fake.set_config_file_path,
                get_current_config_path=fake.get_config_file_path,
                path_safety_evidence_path=_write_path_safety(
                    tmp_path / "path_safety.json",
                    staged=staged,
                    current_config_path=prior,
                ),
                state_path=state_path,
                validate=_valid_config,
            )
        )

    assert not marker.exists()  # released on the way out, and the type re-raised


def test_startup_load_reconcile_trigger_warns_on_failed_broker_start(
    monkeypatch,
    caplog,
) -> None:
    calls = _record_reconcile_triggers(monkeypatch, ok=False)
    caplog.set_level(logging.INFO, logger=startup_load_mod.logger.name)

    startup_load_mod._trigger_audio_hardware_reconcile(source="unit_test")

    assert calls == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "unit_test",
        "no_block": False,
        "timeout": 15.0,
    }]
    assert "event=active_speaker.audio_hardware_reconcile_trigger_failed" in caplog.text
    assert "error=rc=3" in caplog.text
    assert "event=active_speaker.audio_hardware_reconcile_triggered" not in caplog.text


def test_startup_rollback_reports_snapshot_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    failing = SnapshotFailingCamilla(str(prior))

    rollback = asyncio.run(
        rollback_protected_startup_config(
            load_config=failing.set_config_file_path,
            get_current_config_path=failing.get_config_file_path,
            state_path=state_path,
            validate=_valid_config,
        )
    )

    assert rollback["rollback"]["status"] == "rollback_failed"
    assert "startup_rollback_failed" in {
        issue["code"] for issue in rollback["rollback"]["issues"]
    }


def _composite_topology() -> OutputTopology:
    """A commissioned dual-Apple pair: one active 2-way per side, all verified."""

    def group(group_id: str, kind: str, woofer: int, tweeter: int) -> dict:
        return {
            "id": group_id,
            "label": group_id.title(),
            "kind": kind,
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": woofer,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": tweeter,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        }

    def child(child_id: str, serial: str, port: str, indexes: list[int]) -> dict:
        return {
            "child_id": child_id,
            "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
            "device_label": "Apple USB-C audio adapter",
            "serial": serial,
            "usb_path": port,
            "controller": "xhci-hcd.0",
            "physical_output_indexes": indexes,
        }

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "hardware": {
            "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            "device_label": "Dual Apple USB-C DAC 4-channel pair",
            "physical_output_count": 4,
            "child_devices": [
                child("left_dac", "SERIAL-A", "usb1/1-2", [0, 1]),
                child("right_dac", "SERIAL-B", "usb1/1-1", [2, 3]),
            ],
        },
        "speaker_groups": [group("left", "left", 0, 1), group("right", "right", 2, 3)],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def _composite_observation(*, serial_b: str) -> OutputHardwareState:
    def card(card_id: str, serial: str, port: str) -> OutputCardFact:
        return OutputCardFact(
            card_id=card_id,
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial,
            usb_path=port,
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        )

    return classify_output_cards([
        card("A", "SERIAL-A", "usb1/1-2"),
        card("A_1", serial_b, "usb1/1-1"),
    ])


def test_repinned_composite_refuses_startup_load_until_identity_is_reconfirmed(
    tmp_path: Path,
) -> None:
    """A swapped dongle cannot arm on the confirmation the old unit earned.

    ``repin_composite_child_serials`` keeps the design but clears identity for
    the replaced unit's lanes. That one clear must be enough for the gates that
    ALREADY exist to refuse the protected startup load — the re-pin flow adds
    no verification of its own, so if this stopped holding, a household could
    swap a DAC and arm four drivers whose wiring nobody re-checked.

    The assertion is a before/after FLIP, not ``load_allowed`` alone: a
    composite cannot complete ``stage_protected_startup_config`` in a
    hardware-free test, so ``load_allowed`` is already False on both sides and
    proves nothing by itself. What the re-pin must own is that
    ``physical_identity_verified`` was passing on this exact topology and stops
    passing, and that the signature binding staged/path-safety evidence to the
    topology moves — so a restage cannot silently reuse the old evidence.
    """

    from jasper.active_speaker.path_safety import topology_target_signature

    before = _composite_topology()
    staged = stage_protected_startup_config(
        before,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )
    evidence = _write_path_safety(
        tmp_path / "path_safety.json", topology=before, staged=staged
    )

    def gates_for(topology: OutputTopology) -> dict[str, bool]:
        report = build_startup_load_preflight(
            topology,
            staged_config=staged,
            path_safety_evidence_path=evidence,
            validate=_valid_config,
        )
        assert report["load_allowed"] is False
        return {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    after = repin_composite_child_serials(
        before, _composite_observation(serial_b="SERIAL-NEW")
    )

    assert gates_for(before)["physical_identity_verified"] is True
    assert gates_for(after)["physical_identity_verified"] is False
    assert topology_target_signature(after) != topology_target_signature(before)
