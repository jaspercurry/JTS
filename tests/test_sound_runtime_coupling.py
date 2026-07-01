# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The runtime CHOKEPOINT wiring: ``reconcile_current_dsp`` (and, by the same
seam, ``load_profile_config``) must thread the SHARED fan-in→Camilla coupling
kwargs resolved from the environment into ``carrier.reemit``.

These pin the literal call-site wiring — the carrier-level behaviour and the
env resolver are unit-tested elsewhere (``test_sound_graph_carrier.py`` /
``test_fanin_coupling.py``); here we prove the runtime emit path actually passes
the resolved kwargs, so a coupling=transport_pipe box re-emits the dual-pipe
RawFile/File topology on every reconcile (the always-on contract) and a default
box passes ``{}`` (byte-
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


def _capture_reemit_coupling(
    monkeypatch, tmp_path, *, coupling_env: str | None,
    coupling_override: str | None = None,
):
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

    # Under transport_pipe the flat-profile noop must be SKIPPED so the apply
    # actually flips the shared topology loopback->dual-pipe. Spy
    # load_profile_config (the apply reconcile delegates to) to prove it's
    # reached under transport_pipe and NOT reached under loopback.
    class _ApplyState:
        active_config_path = "applied.yml"
        room_peq_count = 0

        def to_dict(self):
            return {}

    async def _spy_apply(*a, **k):
        seen["apply_called"] = True
        seen["apply_coupling"] = k.get("coupling")
        return _ApplyState(), "applied.yml", None

    monkeypatch.setattr(runtime, "load_profile_config", _spy_apply)

    result = asyncio.run(
        runtime.reconcile_current_dsp(
            config_dir=config_dir,
            camilla_factory=lambda: _FakeCamilla(str(current)),
            coupling=coupling_override,
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
    # no apply engine (byte-identical, the topology fix only changes the
    # transport_pipe path).
    assert result["status"] == "skipped"
    assert result["reason"] == "flat_profile_noop"
    assert seen.get("apply_called") is not True


def test_reconcile_transport_pipe_passes_dual_pipe_coupling(monkeypatch, tmp_path):
    # transport_pipe: the reemit gets the dual-pipe kwargs.
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="transport_pipe"
    )
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_pipe_path"].endswith("camilla.pipe")
    assert kwargs["playback_pipe_path"].endswith("content.pipe")
    assert kwargs["resampler_type"] is None
    assert kwargs["enable_rate_adjust"] is False
    assert kwargs["transport_paced_pipe"] is True
    # Regression guard: under transport_pipe a flat profile must NOT
    # short-circuit on flat_profile_noop — the graph transport must flip
    # loopback->dual-pipe, so the apply actually runs.
    assert seen.get("apply_called") is True
    assert result["status"] == "reconciled"


def test_reconcile_unknown_coupling_fails_safe_to_empty(monkeypatch, tmp_path):
    # A typo must not arm the pipe: it fails safe to {} (loopback).
    _result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="fif0"
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}


def test_both_chokepoints_resolve_coupling_through_one_helper(monkeypatch):
    # Both chokepoints (the durable apply + the dry-run reconcile) resolve the
    # coupling through the SAME plan helper (fanin_coupling_capture_kwargs),
    # so the dry-run YAML and the durable apply can never disagree (which would
    # break unchanged-detection) — and an explicit override threads to both.
    import inspect

    src = inspect.getsource(runtime.load_profile_config)
    assert "fanin_coupling_capture_kwargs(coupling)" in src
    assert "fanin_coupling_capture_kwargs=coupling_capture_kwargs" in src
    reconcile_src = inspect.getsource(runtime.reconcile_current_dsp)
    assert "fanin_coupling_capture_kwargs(coupling)" in reconcile_src
    del monkeypatch


def test_resolver_helper_override_beats_env(monkeypatch):
    # The explicit coupling override is what the coupling reconciler passes
    # (its os.environ may be stale after it just rewrote fanin.env).
    from jasper.audio_runtime_plan import fanin_coupling_capture_kwargs

    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "loopback")
    pipe_kwargs = fanin_coupling_capture_kwargs("transport_pipe")
    assert "capture_pipe_path" in pipe_kwargs
    assert "playback_pipe_path" in pipe_kwargs
    assert pipe_kwargs["enable_rate_adjust"] is False
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "transport_pipe")
    assert fanin_coupling_capture_kwargs("loopback") == {}
    # None falls through to the env (every existing caller's behavior).
    assert "capture_pipe_path" in fanin_coupling_capture_kwargs(None)


def test_reconcile_explicit_transport_pipe_override_arms_regardless_of_env(monkeypatch, tmp_path):
    # coupling="transport_pipe" passed to reconcile_current_dsp emits the dual
    # pipe topology even when the env says loopback (the reconciler's
    # stale-env-proof path), and the override threads to the durable apply.
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="loopback", coupling_override="transport_pipe",
    )
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_pipe_path"].endswith("camilla.pipe")
    assert kwargs["playback_pipe_path"].endswith("content.pipe")
    assert seen["apply_called"] is True
    assert seen["apply_coupling"] == "transport_pipe"
    assert result["status"] == "reconciled"


def test_reconcile_explicit_loopback_override_beats_transport_pipe_env(monkeypatch, tmp_path):
    # coupling="loopback" override emits the ALSA capture even when
    # env=transport_pipe;
    # the flat profile then correctly short-circuits (loopback is byte-identical).
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="transport_pipe", coupling_override="loopback",
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}
    assert result["reason"] == "flat_profile_noop"
    assert seen.get("apply_called") is not True
