# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The runtime CHOKEPOINT wiring: ``reconcile_current_dsp`` (and, by the same
seam, ``load_profile_config``) must thread the SHARED fan-in→Camilla coupling
kwargs resolved from the environment into ``carrier.reemit``.

These pin the literal call-site wiring — the carrier-level behaviour and the
env resolver are unit-tested elsewhere (``test_sound_graph_carrier.py`` /
``test_fanin_coupling.py``); here we prove the runtime emit path actually passes
the resolved kwargs, so a coupling=shm_ring box re-emits the ring
capture/playback topology on every reconcile (the always-on contract) and a
default (or removed/typo) box passes ``{}`` (byte-identical loopback).

The ``coupling=None`` CLI/install path (``jasper-sound reconcile-current-dsp``)
resolves the coupling token FILE-FRESH from the persisted ``fanin.env`` SSOT —
NOT ``os.environ``. That is the DEFECT-1 fix: on jts.local a polluted
``os.environ`` coupling made the CLI reconcile emit a RING capture/playback
config on a LOOPBACK box (``fanin.env=loopback``), and CamillaDSP crash-looped on
a ring nobody writes. ``test_reconcile_current_dsp_emits_loopback_devices_over_
stale_ring_yaml_on_loopback_box`` reproduces the hardware scenario end-to-end.
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

    ``coupling_env`` names the PERSISTED coupling. The ``coupling=None`` CLI path
    resolves the token FILE-FRESH via ``read_persisted_coupling`` (the SSOT), so
    we drive it by monkeypatching that reader — NOT ``os.environ``. We also clear
    ``JASPER_FANIN_CAMILLA_COUPLING`` from ``os.environ`` to prove the None path
    ignores it (the stale-``os.environ`` bug this suite guards): the persisted
    file wins.
    """
    monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: coupling_env if coupling_env is not None else "loopback",
    )

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

    # Under a non-loopback coupling (shm_ring) the flat-profile noop must be
    # SKIPPED so the apply actually flips the shared topology. Spy
    # load_profile_config (the apply reconcile delegates to) to prove it's
    # reached under a coupling and NOT reached under loopback.
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
    # Sanity: loopback default still short-circuits on the flat-profile noop
    # (byte-identical emit — no apply engine).
    assert result["status"] == "skipped"
    assert result["reason"] == "flat_profile_noop"
    assert seen.get("apply_called") is not True


def test_reconcile_removed_transport_pipe_fails_safe_to_empty(monkeypatch, tmp_path):
    # The REMOVED transport_pipe coupling now resolves loopback at the emit
    # chokepoint, so the reemit gets {} (byte-identical loopback emit) — a
    # migrating box never arms a deleted transport.
    _result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="transport_pipe"
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}


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
    ring_kwargs = fanin_coupling_capture_kwargs("shm_ring")
    assert ring_kwargs["capture_device"] == "jts_ring_capture"
    assert ring_kwargs["playback_device"] == "jts_ring_playback"
    assert ring_kwargs["enable_rate_adjust"] is False
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "shm_ring")
    assert fanin_coupling_capture_kwargs("loopback") == {}
    # None resolves the token FILE-FRESH from the persisted SSOT (NOT os.environ):
    # os.environ says shm_ring above, but the persisted file drives the result.
    # This is the DEFECT-1 fix — the CLI/install reconcile-current-dsp path must
    # not honor a stale os.environ coupling (which on jts.local emitted a RING
    # config on a loopback box). A shm_ring persisted file => ring kwargs; a
    # loopback persisted file (or read fail-safe) => {}.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    assert "capture_device" in fanin_coupling_capture_kwargs(None)
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    # os.environ still says shm_ring — the file-fresh loopback wins.
    assert fanin_coupling_capture_kwargs(None) == {}


def test_reconcile_explicit_shm_ring_override_arms_regardless_of_env(monkeypatch, tmp_path):
    # coupling="shm_ring" passed to reconcile_current_dsp emits the ring
    # topology even when the env says loopback (the reconciler's
    # stale-env-proof path), and the override threads to the durable apply.
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="loopback", coupling_override="shm_ring",
    )
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_device"] == "jts_ring_capture"
    assert kwargs["playback_device"] == "jts_ring_playback"
    assert seen["apply_called"] is True
    assert seen["apply_coupling"] == "shm_ring"
    assert result["status"] == "reconciled"


def test_reconcile_explicit_loopback_override_beats_shm_ring_env(monkeypatch, tmp_path):
    # coupling="loopback" override emits the ALSA capture even when
    # env=shm_ring;
    # the flat profile then correctly short-circuits (loopback is byte-identical).
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env="shm_ring", coupling_override="loopback",
    )
    assert seen["fanin_coupling_capture_kwargs"] == {}
    assert result["reason"] == "flat_profile_noop"
    assert seen.get("apply_called") is not True


def test_reconcile_current_dsp_emits_loopback_devices_over_stale_ring_yaml_on_loopback_box(
    monkeypatch, tmp_path,
):
    """DEFECT 1 (hardware-reproduced twice on jts.local): the CLI/install
    ``jasper-sound reconcile-current-dsp`` path (``coupling=None``) must resolve
    the coupling token FILE-FRESH from the persisted ``fanin.env`` SSOT, not from
    a stale ``os.environ``.

    jts.local's exact state:
      * ``fanin.env`` (the ONLY coupling line anywhere) = ``loopback``;
      * a prior ``sound_current.yml`` on disk carrying RING devices
        (``jts_ring_capture`` / ``jts_ring_playback``);
      * ``os.environ[JASPER_FANIN_CAMILLA_COUPLING]`` polluted to ``shm_ring``.

    Pre-fix, ``fanin_coupling_capture_kwargs(None)`` synthesized
    ``dict(os.environ)`` and read the STALE ``shm_ring``, re-emitting a RING
    capture/playback config on a LOOPBACK box — CamillaDSP then crash-looped
    (SIGKILLed via the LimitRTTIME busy-loop) on the missing/writer-dead ring.
    This drives the REAL emit path (``carrier.reemit`` -> ``emit_sound_config``)
    and asserts LOOPBACK devices land in the emitted YAML.
    """
    from jasper.audio_runtime_plan import fanin_coupling_capture_kwargs
    from jasper.sound.camilla_yaml import emit_flat_ring_config
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import SoundProfile

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    # The prior on-disk config has RING devices (the jts.local sound_current.yml).
    current = config_dir / "sound_current.yml"
    emit_flat_ring_config(out_path=current)
    assert "jts_ring_capture" in current.read_text()

    # The pollution: os.environ carries a STALE ring coupling...
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "shm_ring")
    # ...but the persisted SSOT (fanin.env) is loopback.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )

    # The CLI resolves coupling=None -> file-fresh loopback -> {} (no ring kwargs).
    kwargs = fanin_coupling_capture_kwargs(None)
    assert kwargs == {}, f"stale os.environ leaked ring kwargs: {kwargs}"

    # End-to-end: the real emit path writes LOOPBACK devices, not the prior ring.
    carrier = carrier_for_loaded_config(current, config_dir=config_dir)
    out_path = config_dir / "sound_out.yml"
    carrier.reemit(
        SoundProfile(enabled=False),
        out_path=out_path,
        fanin_coupling_capture_kwargs=kwargs,
    )
    emitted = out_path.read_text()
    assert "jts_ring_capture" not in emitted
    assert "jts_ring_playback" not in emitted
    assert 'device: "plug:jasper_capture"' in emitted
    assert 'device: "outputd_content_playback"' in emitted
