# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The runtime CHOKEPOINT wiring: ``reconcile_current_dsp`` (and, by the same
seam, ``load_profile_config``) must thread the SHARED fan-in→Camilla coupling
kwargs into ``carrier.reemit``.

These pin the literal call-site wiring — the carrier-level behaviour and the
resolver are unit-tested elsewhere (``test_sound_graph_carrier.py`` /
``test_fanin_coupling.py``); here we prove the runtime emit path actually passes
them, so every reconcile re-emits the ring capture/playback topology.

Since ADR-0100 the ring is the only central transport, so the kwargs are
UNCONDITIONAL: no env value, no persisted token and no explicit override can
make this seam answer ``{}``. That matters at exactly this chokepoint — a
``{}`` here re-emits a graph whose capture names a lane nothing writes.
"""
from __future__ import annotations

import asyncio

import pytest

from jasper.sound import runtime
from jasper.sound.graph_carrier import ReemitResult


@pytest.fixture(autouse=True)
def _saved_passive_layout(tmp_path, monkeypatch):
    """Runtime coupling tests exercise a flat DAC graph intentionally."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    save_output_topology(_full_range_stereo(), path)


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

    The fake carrier returns a base_flat result; what this helper needs is the
    dry-run reemit call, not what reconcile decides afterwards.

    ``coupling_env`` names the PERSISTED coupling. It is driven by monkeypatching
    the SSOT reader rather than ``os.environ``, which is also what proves the
    answer no longer depends on either: the persisted
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
            # room_peq_count=0 + a flat profile + 0 trim: nothing to EQ.
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

    # The ring kwargs SKIP the flat-profile noop so the apply actually writes
    # the shared topology. Spy load_profile_config (the apply reconcile
    # delegates to) to prove it is reached.
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


def test_reconcile_with_no_coupling_env_still_passes_the_ring_kwargs(
    monkeypatch, tmp_path
):
    """THE CHOKEPOINT half of ADR-0100's unconditional capture seam.

    A box that declares no coupling anywhere used to reach the carrier with
    ``{}`` and keep its dsnoop defaults. With the ring the only transport that
    would re-emit a graph capturing a lane fan-in does not write, so the seam
    hands the carrier the ring topology — and the flat-profile noop no longer
    short-circuits, because there is now a real topology to write.
    """
    result, seen = _capture_reemit_coupling(
        monkeypatch, tmp_path, coupling_env=None
    )
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_device"] == "jts_ring_capture"
    assert kwargs["playback_device"] == "jts_ring_playback"
    assert result["status"] == "reconciled"
    assert seen["apply_called"] is True


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
    assert "_render_saved_dsp_on_carrier(" in reconcile_src
    materializer_src = inspect.getsource(runtime._render_saved_dsp_on_carrier)
    assert "fanin_coupling_capture_kwargs(coupling)" in materializer_src
    del monkeypatch


def test_the_resolver_helper_answers_the_ring_for_every_input(monkeypatch):
    """No env value, no override and no persisted token can produce ``{}``.

    The DEFECT-1 class this used to guard — a stale ``os.environ`` coupling
    steering the CLI reconcile onto the wrong route — cannot recur, because
    there is no second route to be steered onto and nothing here reads a token
    at all. That is proved rather than reasoned: the persisted reader raises.
    """
    from jasper.audio_runtime_plan import fanin_coupling_capture_kwargs

    def _boom(*a, **k):
        raise AssertionError("the capture kwargs must not depend on a coupling token")

    monkeypatch.setattr("jasper.fanin.ring_health.read_persisted_coupling", _boom)
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "loopback")

    for arg in (None, "shm_ring", "loopback", "transport_pipe", "fif0"):
        kwargs = fanin_coupling_capture_kwargs(arg)
        assert kwargs["capture_device"] == "jts_ring_capture", arg
        assert kwargs["playback_device"] == "jts_ring_playback", arg
        assert kwargs["enable_rate_adjust"] is False, arg


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


