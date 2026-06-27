# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The runtime CHOKEPOINT wiring: ``reconcile_current_dsp`` (and, by the same
seam, ``load_profile_config``) must thread the SHARED fan-in→Camilla coupling
kwargs resolved from the environment into ``carrier.reemit``.

These pin the literal call-site wiring — the carrier-level behaviour and the
env resolver are unit-tested elsewhere (``test_sound_graph_carrier.py`` /
``test_fanin_coupling.py``); here we prove the runtime emit path actually passes
the resolved kwargs, so a coupling=fifo box re-emits a File capture on every
reconcile (the always-on contract) and a default box passes ``{}`` (byte-
identical).
"""
from __future__ import annotations

import asyncio

from jasper.sound import runtime
from jasper.sound.graph_carrier import ReemitResult


class _FakeCamilla:
    """Reports a stable loaded config path; never actually loads anything."""

    def __init__(self, path: str) -> None:
        self._path = path

    async def get_config_file_path(self, *, best_effort: bool = False):
        return self._path


def _capture_reemit_coupling(monkeypatch, tmp_path, *, coupling_env: str | None):
    """Run reconcile_current_dsp far enough to call carrier.reemit once and
    return the ``fanin_coupling_capture_kwargs`` it was given.

    The fake carrier returns a base_flat noop result so reconcile short-circuits
    on ``flat_profile_noop`` BEFORE the apply engine runs — we only need the
    dry-run reemit call.
    """
    if coupling_env is None:
        monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)
    else:
        monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", coupling_env)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text("# loaded\n")

    seen: dict[str, object] = {}

    class _FakeCarrier:
        kind = "base_flat"

        def reemit(self, profile, **kwargs):
            seen["fanin_coupling_capture_kwargs"] = kwargs.get(
                "fanin_coupling_capture_kwargs"
            )
            # room_peq_count=0 + a flat profile + 0 trim => flat_profile_noop
            # short-circuit, so the apply engine is never reached.
            return ReemitResult(yaml="# dry\n", room_peq_count=0)

    # reconcile imports carrier_for_loaded_config lazily from graph_carrier, so
    # patch it at its source module.
    monkeypatch.setattr(
        "jasper.sound.graph_carrier.carrier_for_loaded_config",
        lambda *a, **k: _FakeCarrier(),
    )
    # A flat profile + no settings => sound_filter_count 0, trim 0.0 => noop.
    monkeypatch.setattr(runtime, "load_profile", lambda *a, **k: runtime_flat_profile())
    monkeypatch.setattr(runtime, "build_sound_filters", lambda profile: ())
    monkeypatch.setattr(runtime, "output_trim_db", lambda profile, settings: 0.0)
    monkeypatch.setattr(runtime, "load_sound_settings", lambda *a, **k: object())

    # MB1: under =fifo the flat-profile noop must be SKIPPED so the apply
    # actually flips the shared capture loopback->File. Spy load_profile_config
    # (the apply reconcile delegates to) to prove it's reached under =fifo and
    # NOT reached under loopback — without a full apply-engine fake.
    class _ApplyState:
        active_config_path = "applied.yml"
        room_peq_count = 0

        def to_dict(self):
            return {}

    async def _spy_apply(*a, **k):
        seen["apply_called"] = True
        return _ApplyState(), "applied.yml", None

    monkeypatch.setattr(runtime, "load_profile_config", _spy_apply)

    result = asyncio.run(
        runtime.reconcile_current_dsp(
            config_dir=config_dir,
            camilla_factory=lambda: _FakeCamilla(str(current)),
        )
    )
    return result, seen


def runtime_flat_profile():
    from jasper.sound.profile import SoundProfile

    return SoundProfile(enabled=False)


def test_reconcile_default_passes_empty_coupling(monkeypatch, tmp_path):
    # Default-OFF: no coupling env => the reemit gets {} (byte-identical emit).
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env=None
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}
    # Sanity: loopback default still short-circuits on the flat-profile noop —
    # no apply engine (byte-identical, the MB1 fix only changes the =fifo path).
    assert result["status"] == "skipped"
    assert result["reason"] == "flat_profile_noop"
    assert seen.get("apply_called") is not True


def test_reconcile_fifo_passes_file_capture_coupling(monkeypatch, tmp_path):
    # =fifo: the reemit gets the File-capture kwargs (the always-on arm point).
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="fifo"
    )
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_pipe_path"].endswith("camilla.pipe")
    assert kwargs["resampler_type"] == "AsyncSinc"
    assert kwargs["enable_rate_adjust"] is True
    # MB1 regression guard: under =fifo a flat profile must NOT short-circuit on
    # flat_profile_noop — the shared capture must flip loopback->File, so the
    # apply actually runs. Before the fix the noop fired and left Camilla on the
    # dead loopback while fan-in wrote the pipe -> silent outage.
    assert seen.get("apply_called") is True
    assert result["status"] == "reconciled"


def test_reconcile_unknown_coupling_fails_safe_to_empty(monkeypatch, tmp_path):
    # A typo must not arm the pipe: it fails safe to {} (loopback).
    _result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="fif0"
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}


def test_load_profile_config_resolves_coupling_from_env(monkeypatch):
    # The other chokepoint (the durable apply) resolves the SAME env helper. We
    # assert the call site references coupling_capture_kwargs_from_env so the two
    # paths cannot drift (the dry-run reconcile and the durable apply must agree
    # or the unchanged-detection breaks).
    import inspect

    src = inspect.getsource(runtime.load_profile_config)
    assert "coupling_capture_kwargs_from_env()" in src
    assert "fanin_coupling_capture_kwargs=coupling_capture_kwargs" in src
    # And reconcile's dry-run uses the same resolver.
    reconcile_src = inspect.getsource(runtime.reconcile_current_dsp)
    assert "coupling_capture_kwargs_from_env()" in reconcile_src
    del monkeypatch
