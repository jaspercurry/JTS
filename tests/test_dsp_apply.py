# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import stat
from pathlib import Path

import pytest

import jasper.dsp_apply as dsp_apply_module

from jasper.dsp_apply import (
    BassExtensionApplyPending,
    CANONICAL_DSP_WRITER_LOCK_PATH,
    CamillaConfigValidationResult,
    DspApplyError,
    DspApplyState,
    DspWriterLockTimeout,
    ValidationStatus,
    apply_dsp_config,
    camilla_graph_mutation,
    _DSP_LOCK_OWNERSHIP,
    _dsp_apply_lock,
    _default_apply_lock_path,
    dsp_apply_lock_path,
    dsp_write_epoch,
    dsp_write_epoch_from_state,
    dsp_writer_lock,
    last_dsp_apply_state,
    record_dsp_apply_state,
    validate_camilla_config,
)


def _fake_camilladsp(tmp_path: Path, *, exit_code: int = 0) -> Path:
    script = tmp_path / "camilladsp"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$JASPER_ARGV_CAPTURE\"\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


def test_validate_camilla_config_uses_check_flag_with_positional_config(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    argv_capture = tmp_path / "argv.txt"
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))
    monkeypatch.setenv("JASPER_ARGV_CAPTURE", str(argv_capture))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.VALID
    assert argv_capture.read_text().splitlines() == ["--check", str(cfg)]


def test_dsp_write_epoch_tracks_latest_apply_state(tmp_path: Path):
    state_path = tmp_path / "dsp_apply_state.json"

    assert dsp_write_epoch_from_state(None) == "none"
    assert dsp_write_epoch(state_path=state_path) == "none"

    record_dsp_apply_state(
        DspApplyState(
            schema_version=1,
            op_id="op-123",
            source="test",
            phase="done",
            result="success",
            started_at="2026-05-28T00:00:00Z",
            finished_at="2026-05-28T00:00:01Z",
            prior_config_path=None,
            candidate_config_path="/tmp/test.yml",
        ),
        state_path=state_path,
    )

    assert dsp_write_epoch(state_path=state_path) == "op-123"
    assert dsp_apply_lock_path(tmp_path) == tmp_path / ".dsp_apply.lock"


async def test_dsp_writer_lock_file_is_group_writable_under_restrictive_umask(
    tmp_path: Path,
):
    old_umask = os.umask(0o077)
    try:
        async with dsp_writer_lock(tmp_path, source="test_lock_mode"):
            pass
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE((tmp_path / ".dsp_apply.lock").stat().st_mode)
    assert mode == 0o660


async def test_dsp_writer_lock_times_out_without_stealing_ownership(
    tmp_path: Path,
    caplog,
):
    caplog.set_level("INFO")
    async with dsp_writer_lock(tmp_path, source="holder"):
        async def contend():
            async with dsp_writer_lock(
                tmp_path,
                timeout_s=0.05,
                source="contender",
            ):
                pytest.fail("contended writer lock was admitted")
        with pytest.raises(DspWriterLockTimeout) as caught:
            await asyncio.create_task(contend())

    assert caught.value.source == "contender"
    assert caught.value.timeout_s == pytest.approx(0.05)
    assert caught.value.waited_s >= 0.04
    assert any(
        "event=dsp.writer_lock" in record.message
        and "result=timeout" in record.message
        and "source=contender" in record.message
        for record in caplog.records
    )


async def test_cancelled_dsp_writer_waiter_cannot_acquire_late(tmp_path: Path):
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold() -> None:
        async with dsp_writer_lock(tmp_path, source="holder"):
            holder_entered.set()
            await release_holder.wait()

    holder = asyncio.create_task(hold())
    await holder_entered.wait()

    async def wait_then_mark() -> None:
        async with dsp_writer_lock(
            tmp_path,
            timeout_s=1.0,
            source="cancelled_waiter",
        ):
            pytest.fail("cancelled waiter acquired the writer lock")

    waiter = asyncio.create_task(wait_then_mark())
    await asyncio.sleep(0.03)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_holder.set()
    await holder
    async with dsp_writer_lock(
        tmp_path,
        timeout_s=0.1,
        source="successor",
    ):
        pass


async def test_dsp_writer_lock_does_not_retry_after_late_wakeup(
    tmp_path: Path,
    monkeypatch,
):
    attempts = 0
    real_sleep = asyncio.sleep

    def pretend_contended_then_available(_lock) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    async def oversleep(_delay: float) -> None:
        await real_sleep(0.03)

    monkeypatch.setattr(
        "jasper.dsp_apply._FileLock.try_acquire",
        pretend_contended_then_available,
    )
    monkeypatch.setattr("jasper.dsp_apply.asyncio.sleep", oversleep)

    with pytest.raises(DspWriterLockTimeout):
        async with dsp_writer_lock(
            tmp_path,
            timeout_s=0.01,
            source="late_waiter",
        ):
            pytest.fail("waiter was admitted after its deadline")

    assert attempts == 1


async def test_cancelling_contended_owner_is_not_logged_as_wait_cancellation(
    tmp_path: Path,
    caplog,
):
    caplog.set_level("INFO")
    release_holder = asyncio.Event()
    holder_entered = asyncio.Event()

    async def hold() -> None:
        async with dsp_writer_lock(tmp_path, source="holder"):
            holder_entered.set()
            await release_holder.wait()

    holder = asyncio.create_task(hold())
    await holder_entered.wait()
    owner_entered = asyncio.Event()

    async def own_then_wait() -> None:
        async with dsp_writer_lock(
            tmp_path,
            timeout_s=0.5,
            source="contended_owner",
        ):
            owner_entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(own_then_wait())
    await asyncio.sleep(0.03)
    release_holder.set()
    await holder
    await owner_entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert not any(
        "event=dsp.writer_lock" in record.message
        and "result=cancelled" in record.message
        and "source=contended_owner" in record.message
        for record in caplog.records
    )


async def test_dsp_writer_lock_acquires_after_contention_before_deadline(
    tmp_path: Path,
):
    release_holder = asyncio.Event()
    holder_entered = asyncio.Event()

    async def hold() -> None:
        async with dsp_writer_lock(tmp_path, source="holder"):
            holder_entered.set()
            await release_holder.wait()

    holder = asyncio.create_task(hold())
    await holder_entered.wait()
    acquired = asyncio.Event()

    async def contend() -> None:
        async with dsp_writer_lock(
            tmp_path,
            timeout_s=0.5,
            source="contender",
        ):
            acquired.set()

    contender = asyncio.create_task(contend())
    await asyncio.sleep(0.03)
    release_holder.set()
    await holder
    await contender
    assert acquired.is_set()


async def test_private_admission_refuses_pending_bass_intent_for_any_source(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "bass-intent.json"
    intent.write_text("{}\n", encoding="utf-8")

    with pytest.raises(BassExtensionApplyPending):
        async with _dsp_apply_lock(
            tmp_path / ".dsp_apply.lock",
            source="bass_extension.recovery",
            bass_extension_intent_path=intent,
        ):
            pytest.fail("a source label granted recovery permission")


async def test_task_local_reentry_inherits_only_outer_recovery_permission(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "bass-intent.json"
    intent.write_text("{}\n", encoding="utf-8")
    lock_path = dsp_apply_lock_path(tmp_path)

    async with dsp_writer_lock(
        tmp_path,
        source="bass_extension.recovery",
        allow_pending_bass_extension_recovery=True,
        bass_extension_intent_path=intent,
    ):
        async with camilla_graph_mutation(
            source="camilla.reload",
            lock_path=lock_path,
            bass_extension_intent_path=intent,
        ):
            pass


async def test_pending_intent_race_orders_ordinary_writer_before_recovery(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "bass-intent.json"
    ordinary_entered = asyncio.Event()
    release_ordinary = asyncio.Event()

    async def ordinary() -> None:
        async with _dsp_apply_lock(
            dsp_apply_lock_path(tmp_path),
            source="ordinary",
            bass_extension_intent_path=intent,
        ):
            ordinary_entered.set()
            await release_ordinary.wait()

    async def publish_intent() -> None:
        async with dsp_writer_lock(
            tmp_path,
            source="bass_extension.apply",
            allow_pending_bass_extension_recovery=True,
            bass_extension_intent_path=intent,
        ):
            intent.write_text("{}\n", encoding="utf-8")

    first = asyncio.create_task(ordinary())
    await ordinary_entered.wait()
    publisher = asyncio.create_task(publish_intent())
    await asyncio.sleep(0.03)
    assert not intent.exists()
    release_ordinary.set()
    await first
    await publisher

    with pytest.raises(BassExtensionApplyPending):
        async with _dsp_apply_lock(
            dsp_apply_lock_path(tmp_path),
            source="later-ordinary",
            bass_extension_intent_path=intent,
        ):
            pytest.fail("writer entered after intent publication")


def test_recovery_permission_literal_is_owned_only_by_bass_transaction() -> None:
    repo = Path(__file__).resolve().parents[1]
    owners = {
        path.relative_to(repo).as_posix()
        for path in (repo / "jasper").rglob("*.py")
        if "allow_pending_bass_extension_recovery=True" in path.read_text(
            encoding="utf-8"
        )
    }

    assert owners == {"jasper/bass_extension/__init__.py"}


def test_apply_lock_is_fixed_in_production_with_explicit_pytest_temp_injection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.yml"
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert _default_apply_lock_path(candidate) == CANONICAL_DSP_WRITER_LOCK_PATH

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test seam")
    assert _default_apply_lock_path(candidate) == dsp_apply_lock_path(tmp_path)


async def test_public_writer_lock_uses_same_fixed_production_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths: list[Path] = []

    @contextlib.asynccontextmanager
    async def capture(path: Path, **_kwargs):
        paths.append(path)
        yield

    monkeypatch.setattr(dsp_apply_module, "_dsp_apply_lock", capture)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    async with dsp_writer_lock(tmp_path, source="production-path-proof"):
        pass

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test seam")
    async with dsp_writer_lock(tmp_path, source="pytest-path-proof"):
        pass

    assert paths == [
        CANONICAL_DSP_WRITER_LOCK_PATH,
        dsp_apply_lock_path(tmp_path),
    ]


async def test_apply_dsp_config_skips_lock_when_caller_already_owns_it(
    tmp_path: Path,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")

    async with dsp_writer_lock(tmp_path, source="outer"):
        result = await apply_dsp_config(
            source="nested_apply",
            candidate_path=cfg,
            load_config=lambda _path: asyncio.sleep(0, result=True),
            acquire_lock=False,
            validate=lambda path: CamillaConfigValidationResult(
                status=ValidationStatus.VALID,
                path=str(path),
            ),
            state_path=tmp_path / "state.json",
        )

    assert result.result == "success"


async def test_apply_dsp_config_false_hint_acquires_when_ownership_is_absent(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    loaded: list[str] = []

    async def load(path: str) -> bool:
        owned = _DSP_LOCK_OWNERSHIP.get()
        assert owned is not None
        assert owned.task is asyncio.current_task()
        assert owned.path == dsp_apply_lock_path(tmp_path)
        loaded.append(path)
        return True

    result = await apply_dsp_config(
        source="legacy_nested_apply",
        candidate_path=cfg,
        load_config=load,
        acquire_lock=False,
        validate=lambda path: CamillaConfigValidationResult(
            status=ValidationStatus.VALID,
            path=str(path),
        ),
        state_path=tmp_path / "state.json",
    )

    assert result.result == "success"
    assert loaded == [str(cfg)]


def test_validate_camilla_config_classifies_invalid_config(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    binary = _fake_camilladsp(tmp_path, exit_code=101)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))
    monkeypatch.setenv("JASPER_ARGV_CAPTURE", str(tmp_path / "argv.txt"))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply


def test_validate_camilla_config_classifies_usage_error_as_runner_error(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    script = tmp_path / "camilladsp"
    script.write_text(
        "#!/bin/sh\n"
        "printf 'Usage: camilladsp [OPTIONS] [CONFIGFILE]\\n' >&2\n"
        "exit 2\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(script))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.RUNNER_ERROR
    assert not result.ok_to_apply


def test_validate_camilla_config_timeout_output_is_json_safe(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", "/tmp/camilladsp")

    def fake_run(*args, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(
            cmd=["camilladsp"],
            timeout=10,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr("jasper.dsp_apply.subprocess.run", fake_run)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.TIMEOUT
    assert result.stdout_tail == "partial stdout"
    assert result.stderr_tail == "partial stderr"
    assert isinstance(result.to_dict()["stdout_tail"], str)


async def test_apply_dsp_config_validation_failure_does_not_load_or_persist(
    tmp_path: Path,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    loaded: list[str] = []
    persisted = False

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    def persist() -> None:
        nonlocal persisted
        persisted = True

    def validate(path: str | Path) -> CamillaConfigValidationResult:
        return CamillaConfigValidationResult(
            status=ValidationStatus.INVALID_CONFIG,
            path=str(path),
            returncode=101,
        )

    try:
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            prior_config_path="/etc/camilladsp/v1.yml",
            persist=persist,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=validate,
        )
    except DspApplyError as e:
        assert e.state.result == "invalid_config"
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected validation failure")

    assert loaded == []
    assert not persisted
    assert last_dsp_apply_state(
        state_path=tmp_path / "dsp_apply_state.json",
    )["result"] == "invalid_config"


async def test_apply_dsp_config_rolls_back_when_reload_fails(tmp_path: Path):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    calls: list[str] = []

    async def load(path: str) -> bool:
        calls.append(path)
        if path == str(cfg):
            raise RuntimeError("reload failed")
        return True

    try:
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            prior_config_path="/etc/camilladsp/v1.yml",
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=lambda path: CamillaConfigValidationResult(
                status=ValidationStatus.VALID,
                path=str(path),
            ),
        )
    except DspApplyError as e:
        assert e.state.result == "load_failed_rolled_back"
        assert e.state.rollback_succeeded is True
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected reload failure")

    assert calls == [str(cfg), "/etc/camilladsp/v1.yml"]


# ---------------------------------------------------------------------------
# Audit C6 — devices.volume_limit safety ceiling at the validate gate.
# CamillaDSP's own --check accepts a positive limit and defaults the main
# fader's maximum to +50 dB when the key is omitted; the JTS apply path
# must reject both shapes before anything touches live audio.
# ---------------------------------------------------------------------------


def test_validate_rejects_positive_volume_limit(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 3.0\n")
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply
    assert "0 dB" in (result.error or "")


def test_validate_rejects_missing_volume_limit(tmp_path: Path, monkeypatch):
    """Omitted key means CamillaDSP defaults the fader ceiling to +50 dB —
    a loud-output hazard, rejected like a positive limit."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  samplerate: 48000\n")
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply
    assert "volume_limit" in (result.error or "")


@pytest.mark.parametrize(
    "text",
    [
        "---\ndevices:\n  playback:\n    volume_limit: 0.0\n",
        "---\ndevices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n",
        "---\ndevices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n",
    ],
)
def test_validate_rejects_ambiguous_volume_limit_without_binary(
    tmp_path: Path,
    monkeypatch,
    text: str,
):
    import jasper.dsp_apply as dsp_apply

    cfg = tmp_path / "candidate.yml"
    cfg.write_text(text)
    monkeypatch.setattr(dsp_apply, "_camilladsp_binary", lambda: None)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply
    assert "volume_limit" in (result.error or "")


def test_validate_limit_check_applies_without_camilladsp_binary(
    tmp_path: Path, monkeypatch,
):
    """Dev machines without CamillaDSP skip the CLI preflight (MISSING is
    ok_to_apply) but must still get the pure-Python safety rejection."""
    import jasper.dsp_apply as dsp_apply

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 6.0\n")
    monkeypatch.setattr(dsp_apply, "_camilladsp_binary", lambda: None)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply


def test_validate_accepts_zero_volume_limit_without_binary(
    tmp_path: Path, monkeypatch,
):
    import jasper.dsp_apply as dsp_apply

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    monkeypatch.setattr(dsp_apply, "_camilladsp_binary", lambda: None)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.MISSING
    assert result.ok_to_apply
