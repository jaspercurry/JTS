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
UNCONDITIONAL: neither an env value nor a persisted token can make this seam
answer ``{}``. That matters at exactly this chokepoint — a ``{}`` here re-emits
a graph whose capture names a lane nothing writes.
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


def _capture_reemit_coupling(monkeypatch, tmp_path):
    """Run reconcile_current_dsp far enough to call carrier.reemit once and
    return the ``fanin_coupling_capture_kwargs`` it was given.

    The fake carrier returns a base_flat result; what this helper needs is the
    dry-run reemit call, not what reconcile decides afterwards.

    BOTH token sources are pinned AWAY from the ring — the env var unset and a
    real ``fanin.env`` declaring the retired ``loopback`` — so ring kwargs
    coming out the far end prove the seam consults neither. The persisted half
    is a FILE the SSOT readers open, never a function stub, which is only as
    good as the caller still calling the stubbed name (the vacuous-stub class
    #3644 fixed); the assertion below proves the door really says off-ring.
    """
    monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n", encoding="utf-8")
    # Both constants, so nothing falls back to the runner's real fanin.env.
    monkeypatch.setattr("jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env))
    monkeypatch.setattr("jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env))
    from jasper.fanin.ring_health import persisted_coupling_feeds_ring

    assert persisted_coupling_feeds_ring(str(fanin_env)) is False

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
    result, seen = _capture_reemit_coupling(monkeypatch, tmp_path)
    kwargs = seen["fanin_coupling_capture_kwargs"]
    assert kwargs["capture_device"] == "jts_ring_capture"
    assert kwargs["playback_device"] == "jts_ring_playback"
    assert result["status"] == "reconciled"
    assert seen["apply_called"] is True


def test_the_resolver_helper_ignores_persisted_and_env_coupling(monkeypatch):
    """No persisted token and no ``os.environ`` value can produce ``{}``.

    The DEFECT-1 class this used to guard — a stale ``os.environ`` coupling
    steering the CLI reconcile onto the wrong route — cannot recur, because
    there is no second route to be steered onto and the resolver takes no
    coupling argument at all (ADR-0100: it consults neither). That is proved
    rather than reasoned: the persisted reader raises.
    """
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    def _boom(*a, **k):
        raise AssertionError("the capture kwargs must not depend on a coupling token")

    monkeypatch.setattr("jasper.fanin.ring_health.read_persisted_coupling", _boom)
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "loopback")

    kwargs = coupling_capture_kwargs_from_env()
    assert kwargs["capture_device"] == "jts_ring_capture"
    assert kwargs["playback_device"] == "jts_ring_playback"
