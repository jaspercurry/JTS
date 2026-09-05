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
    DSP_PROOF_ANCHOR_MISSING,
    DSP_PROOF_CANDIDATE_CHANGED,
    DSP_PROOF_CANDIDATE_UNREADABLE,
    DSP_PROOF_INACTIVE_RESULTS,
    DspApplyError,
    DspApplyState,
    DspWriterLockTimeout,
    ValidationStatus,
    apply_dsp_config,
    config_file_sha256,
    same_config_file,
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

from ._async_wait import wait_signalled, wait_writer_lock_waiting


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
    await wait_signalled(holder_entered, "holder acquired the lock", producer=holder)

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


async def test_dsp_writer_lock_reports_a_refused_acquire_as_its_own_timeout(
    tmp_path: Path,
    monkeypatch,
):
    """A helper ``TimeoutError`` reaches the caller as the typed DSP one."""

    class Refused:
        async def __aenter__(self):
            raise TimeoutError("timed out waiting for lock")

        async def __aexit__(self, exc_type, exc, traceback):
            raise AssertionError("a lock that never acquired cannot release")

    monkeypatch.setattr(
        dsp_apply_module,
        "advisory_file_lock_async",
        lambda *args, **kwargs: Refused(),
    )

    with pytest.raises(DspWriterLockTimeout):
        async with dsp_writer_lock(
            tmp_path,
            timeout_s=0.01,
            source="late_waiter",
        ):
            pytest.fail("a refused acquire was admitted")


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
    await wait_signalled(holder_entered, "holder acquired the lock", producer=holder)
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
    # The owner only signals if it wins the lock inside its 0.5 s budget.
    # A loaded box can miss that, killing the task with
    # DspWriterLockTimeout; bounded so that surfaces instead of hanging.
    await wait_signalled(
        owner_entered,
        "contended owner acquired the lock",
        producer=owner,
    )
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
    await wait_signalled(holder_entered, "holder acquired the lock", producer=holder)
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


async def test_apply_dsp_config_refuses_pending_bass_intent_before_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    intent = tmp_path / "bass-intent.json"
    intent.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.bass_extension.BASS_EXTENSION_APPLY_INTENT_PATH",
        intent,
    )
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("---\ndevices:\n  volume_limit: 0.0\n", encoding="utf-8")
    loaded: list[str] = []

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    with pytest.raises(BassExtensionApplyPending):
        await apply_dsp_config(
            source="ordinary_apply",
            candidate_path=candidate,
            load_config=load,
            validate=lambda path: CamillaConfigValidationResult(
                status=ValidationStatus.VALID,
                path=str(path),
            ),
            state_path=tmp_path / "state.json",
        )

    assert loaded == []


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
    caplog,
) -> None:
    caplog.set_level("INFO")
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
    await wait_signalled(ordinary_entered, "ordinary writer entered", producer=first)
    publisher = asyncio.create_task(publish_intent())
    await wait_writer_lock_waiting(caplog, "bass_extension.apply")
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
            validate=lambda path: CamillaConfigValidationResult(
                status=ValidationStatus.VALID,
                path=str(path),
            ),
            state_path=tmp_path / "state.json",
        )

    assert result.result == "success"


async def test_apply_dsp_config_acquires_lock_when_ownership_is_absent(
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


# ---------------------------------------------------------------------------
# #2519 — the proof phase's three distinct refusals.
#
# All three used to be one result carrying one message: "DSP candidate changed
# after validation and before load". A jts3 Undo refused deterministically
# under that sentence over a config file nothing had touched in four days, and
# two attempts nine minutes apart produced it again — a race's diagnosis for a
# condition that cannot be a race. These pin that each condition now names
# itself, and that the true race keeps the sentence it earned.
# ---------------------------------------------------------------------------


def _always_valid(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(status=ValidationStatus.VALID, path=str(path))


async def _proof_refusal(tmp_path: Path, cfg: Path, expected_sha: str):
    loaded: list[str] = []

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    with pytest.raises(DspApplyError) as excinfo:
        await apply_dsp_config(
            source="active_speaker_baseline_restore",
            candidate_path=cfg,
            load_config=load,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            expected_candidate_sha256=expected_sha,
            validate=_always_valid,
        )
    # Every proof refusal happens BEFORE the load, which is what makes all
    # three members of DSP_PROOF_INACTIVE_RESULTS honest.
    assert loaded == []
    return excinfo.value


async def test_an_empty_restore_anchor_is_named_missing_not_a_race(tmp_path: Path):
    """An empty-but-not-None expectation is a guaranteed refusal. Reporting it
    as a candidate that "changed after validation" tells the operator to look
    for a writer that does not exist."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")

    exc = await _proof_refusal(tmp_path, cfg, "")

    assert exc.state.result == DSP_PROOF_ANCHOR_MISSING
    assert "no recorded digest" in str(exc)
    assert "changed after validation" not in str(exc)


async def test_an_unreadable_candidate_is_named_unreadable_not_a_race(
    tmp_path: Path, monkeypatch,
):
    """``_sha256`` answers ``None`` for a file it cannot read — a permissions
    or I/O fault on a file whose bytes may be perfectly intact. Comparing that
    ``None`` against a real digest produced the race's message too."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    monkeypatch.setattr(dsp_apply_module, "_sha256", lambda _path: None)

    exc = await _proof_refusal(tmp_path, cfg, "a" * 64)

    assert exc.state.result == DSP_PROOF_CANDIDATE_UNREADABLE
    assert "could not be read" in str(exc)
    assert "changed after validation" not in str(exc)


async def test_a_digest_that_disagrees_keeps_the_race_message(tmp_path: Path):
    """The one condition the sentence was written for: the bytes were read,
    and they are not the bytes that were proven."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")

    exc = await _proof_refusal(tmp_path, cfg, "b" * 64)

    assert exc.state.result == DSP_PROOF_CANDIDATE_CHANGED
    assert str(exc) == "DSP candidate changed after validation and before load"


async def test_a_matching_digest_still_loads(tmp_path: Path):
    """The proof's pass path, so the three refusals above are proven to be
    refusals rather than the only outcome this guard can produce."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    loaded: list[str] = []

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    state = await apply_dsp_config(
        source="active_speaker_baseline_restore",
        candidate_path=cfg,
        load_config=load,
        state_path=tmp_path / "dsp_apply_state.json",
        lock_path=tmp_path / "dsp_apply.lock",
        expected_candidate_sha256=config_file_sha256(cfg),
        validate=_always_valid,
    )

    assert state.result == "success"
    assert loaded == [str(cfg)]


def test_config_file_sha256_is_the_hasher_the_proof_uses(tmp_path: Path):
    """The public helper exists so a caller verifying the same bytes cannot
    reach a different answer than the proof does."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices: {}\n")

    assert config_file_sha256(cfg) == dsp_apply_module._sha256(cfg)
    assert config_file_sha256(tmp_path / "absent.yml") is None


def test_every_proof_result_is_declared_inactive():
    """The set exists so a classifier reads it instead of transcribing it. A
    fourth proof result added without a decision about activity fails here."""
    assert DSP_PROOF_INACTIVE_RESULTS == {
        DSP_PROOF_ANCHOR_MISSING,
        DSP_PROOF_CANDIDATE_UNREADABLE,
        DSP_PROOF_CANDIDATE_CHANGED,
    }


def test_same_config_file_never_raises_on_a_path_it_cannot_resolve(tmp_path: Path):
    """The claim its docstring makes, and the reason it is load-bearing (#2537).

    Both callers are on READ paths that must not raise: one answers whether an
    applied-profile record is still authoritative, the other whether a restore's
    target is still the graph the round applied. A propagated ``ValueError``
    from a malformed path would turn a provenance question into a 500 on a
    household's Undo.

    A NUL byte is the reachable shape — ``Path.resolve`` raises ``ValueError``
    on it — and the fallback is the plain string comparison, so the answer is
    still honest rather than merely non-raising.
    """
    assert same_config_file("/tmp/a\x00b.yml", "/tmp/a\x00b.yml") is True
    assert same_config_file("/tmp/a\x00b.yml", "/tmp/other.yml") is False


def test_same_config_file_resolves_rather_than_string_compares(tmp_path: Path):
    """One file, two spellings — the whole reason this is not ``==``.

    A statefile carries whatever CamillaDSP was handed and a record carries what
    the apply wrote, so a symlinked or non-normalised spelling of one file must
    not read as two.
    """
    real = tmp_path / "config.yml"
    real.write_text("devices: {}\n", encoding="utf-8")
    link = tmp_path / "link.yml"
    link.symlink_to(real)

    assert same_config_file(link, real) is True
    assert same_config_file(tmp_path / "sub" / ".." / "config.yml", real) is True
    assert same_config_file(real, tmp_path / "other.yml") is False


# ---------------------------------------------------------------------------
# In-place applies: the candidate IS the graph already loaded. The reconcile's
# re-anchor writes a refreshed graph back over the kept active-crossover
# candidate the box is running (#2572), which takes the ordinary rollback away
# — re-loading ``prior_config_path`` re-loads the file that was just
# overwritten. These pin the byte-restore that replaces it, and the honesty of
# the verdict it records: ``cli/doctor/audio`` grades a rollback that reports
# itself failed as a FAIL row, so a box repaired by a working restore must not
# be left claiming otherwise.
# ---------------------------------------------------------------------------


async def test_an_in_place_rollback_restores_the_bytes_not_the_path(tmp_path: Path):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nrunning: true\n")
    pristine = cfg.read_text()

    def prepare() -> None:
        cfg.write_text("---\nrejected: true\n")

    async def load(path: str) -> bool:
        if cfg.read_text() != pristine:
            raise RuntimeError("CamillaDSP rejected the rewritten graph")
        return True

    with pytest.raises(DspApplyError) as excinfo:
        await apply_dsp_config(
            source="sound_reconcile",
            candidate_path=cfg,
            prior_config_path=cfg,
            load_config=load,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    state = excinfo.value.state
    assert cfg.read_text() == pristine
    assert state.rollback_succeeded is True
    assert state.result == "load_failed_rolled_back"


async def test_a_failed_prepare_leaves_the_candidate_as_it_was_found(tmp_path: Path):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nrunning: true\n")
    pristine = cfg.read_text()
    loaded: list[str] = []

    def prepare() -> None:
        cfg.write_text("---\nhalf-w")
        raise RuntimeError("emit failed part way")

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    with pytest.raises(DspApplyError) as excinfo:
        await apply_dsp_config(
            source="sound_reconcile",
            candidate_path=cfg,
            prior_config_path=cfg,
            load_config=load,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    state = excinfo.value.state
    assert state.result == "prepare_failed"
    assert cfg.read_text() == pristine
    assert loaded == []
    # No graph was loaded, so no rollback was attempted — the field the doctor
    # turns red must stay clear for a failure that never reached the box.
    assert state.rollback_attempted is False


def _never_valid(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.INVALID_CONFIG, path=str(path)
    )


@pytest.mark.parametrize(
    ("validate", "expected_sha", "result"),
    [
        (_never_valid, None, "invalid_config"),
        (_always_valid, "0" * 64, DSP_PROOF_CANDIDATE_CHANGED),
    ],
)
async def test_a_candidate_refused_before_load_is_put_back_in_place(
    tmp_path: Path, validate, expected_sha, result
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nrunning: true\n")
    pristine = cfg.read_text()
    loaded: list[str] = []

    def prepare() -> None:
        cfg.write_text("---\nrejected: true\n")

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    with pytest.raises(DspApplyError) as excinfo:
        await apply_dsp_config(
            source="sound_reconcile",
            candidate_path=cfg,
            prior_config_path=cfg,
            load_config=load,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=validate,
            expected_candidate_sha256=expected_sha,
        )

    state = excinfo.value.state
    assert state.result == result
    assert cfg.read_text() == pristine
    assert loaded == []
    assert state.rollback_attempted is False


async def test_a_rejected_apply_keeps_the_graph_it_emitted_when_not_in_place(
    tmp_path: Path,
):
    """A rejected candidate that is not the running graph stays on disk.

    It is the evidence — the graph CamillaDSP refused. Reverting it would also
    leave ``config_sha256`` describing bytes nobody can read any more.
    """

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nstale: true\n")
    running = tmp_path / "running.yml"
    running.write_text("---\nrunning: true\n")
    emitted = "---\nrejected: true\n"

    def prepare() -> None:
        cfg.write_text(emitted)

    async def load(path: str) -> bool:
        if path == str(cfg):
            raise RuntimeError("CamillaDSP rejected the candidate")
        return True

    async def current() -> str:
        return str(running)

    with pytest.raises(DspApplyError) as excinfo:
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            get_current_config_path=current,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    assert cfg.read_text() == emitted
    assert excinfo.value.state.config_sha256 == config_file_sha256(cfg)


async def test_a_cancelled_in_place_apply_puts_the_candidate_back(tmp_path: Path):
    """The FILE goes back; the box is deliberately not reloaded onto it.

    Reloading would re-enter the DSP writer lock, and the runner that survives a
    cancellation does it from a spawned task — which the lock's re-entrancy gate
    (keyed on the current task) refuses. So the durable half is repaired and the
    recorded verdict declines to claim the box came back with it.
    """

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nrunning: true\n")
    pristine = cfg.read_text()
    loaded: list[str] = []

    def prepare() -> None:
        cfg.write_text("---\nunproven: true\n")

    async def load(path: str) -> bool:
        loaded.append(path)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await apply_dsp_config(
            source="sound_reconcile",
            candidate_path=cfg,
            prior_config_path=cfg,
            load_config=load,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    assert cfg.read_text() == pristine
    assert loaded == [str(cfg)]
    state = last_dsp_apply_state(state_path=tmp_path / "dsp_apply_state.json")
    assert state["rollback_attempted"] is True
    assert state["rollback_succeeded"] is None


async def test_a_cancelled_persist_keeps_the_graph_that_already_proved_itself(
    tmp_path: Path,
):
    """Cancellation after confirm must not revert a live, proven graph."""

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nrunning: true\n")
    applied = "---\nproven: true\n"

    def prepare() -> None:
        cfg.write_text(applied)

    async def load(path: str) -> bool:
        return True

    async def current() -> str:
        return str(cfg)

    async def persist() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await apply_dsp_config(
            source="sound_reconcile",
            candidate_path=cfg,
            prior_config_path=cfg,
            load_config=load,
            get_current_config_path=current,
            prepare=prepare,
            persist=persist,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    assert cfg.read_text() == applied


async def test_a_cancelled_apply_never_loads_a_candidate_it_was_not_running(
    tmp_path: Path,
):
    """The recovery reload is in-place only.

    A candidate that is NOT the loaded graph is a scratch name being abandoned.
    Loading it during recovery would move the box ONTO the apply that just
    failed — the opposite of a rollback.
    """

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\nstale: true\n")
    loaded: list[str] = []

    def prepare() -> None:
        cfg.write_text("---\nunproven: true\n")

    async def load(path: str) -> bool:
        loaded.append(path)
        raise asyncio.CancelledError

    running = tmp_path / "running.yml"
    running.write_text("---\nrunning: true\n")

    async def current() -> str:
        return str(running)

    with pytest.raises(asyncio.CancelledError):
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            get_current_config_path=current,
            prepare=prepare,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=_always_valid,
        )

    assert loaded == [str(cfg)]


async def test_confirm_accepts_another_spelling_of_the_loaded_file(tmp_path: Path):
    real = tmp_path / "real.yml"
    real.write_text("---\n")
    link = tmp_path / "link.yml"
    link.symlink_to(real)

    async def load(path: str) -> bool:
        return True

    async def current() -> str:
        return str(real)

    state = await apply_dsp_config(
        source="sound",
        candidate_path=link,
        prior_config_path="/etc/camilladsp/v1.yml",
        load_config=load,
        get_current_config_path=current,
        state_path=tmp_path / "dsp_apply_state.json",
        lock_path=tmp_path / "dsp_apply.lock",
        validate=_always_valid,
    )

    assert state.result == "success"
    assert state.rollback_attempted is False
