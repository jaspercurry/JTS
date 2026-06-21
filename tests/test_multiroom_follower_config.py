"""Active-follower CamillaDSP apply/restore arm (distributed-active Slice 3).

Pins invariant 5 — a follower whose driver-only graph cannot be re-proven
REFUSES to bond (no full-range emit) — plus the happy-path emit/apply/stash and
the unbond restore (which must always restore an ACTIVE graph, never passive).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import jasper.active_speaker.crossover_preview as crossover_preview_mod
import jasper.active_speaker.design_draft as design_draft_mod
import jasper.active_speaker.measurement as measurement_mod
import jasper.active_speaker.runtime_contract as runtime_contract_mod
import jasper.dsp_apply as dsp_apply_mod
import jasper.output_topology as output_topology_mod
from jasper.multiroom import follower_config as fc
from jasper.multiroom.config import GroupingConfig

# Reuse the commissioning-evidence fixtures from the baseline-profile tests so
# the follower arm is exercised against the SAME evidence shape the solo apply
# uses (the only difference is driver_domain + the loopback capture).
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview


def _cfg(channel: str = "left") -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel=channel,
        bond_id="bond1",
        leader_addr="jts.local",
        buffer_ms=400,
        codec="flac",
        error=None,
    )


class _FakeCamilla:
    def __init__(self, current: str | None) -> None:
        self._current = current
        self.loaded: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = True):
        return self._current

    async def set_config_file_path(self, path, *, best_effort: bool = False):
        self.loaded.append(str(path))
        self._current = str(path)
        return True


def _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements):
    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: topology)
    monkeypatch.setattr(design_draft_mod, "load_design_draft", lambda *a, **k: draft)
    monkeypatch.setattr(
        crossover_preview_mod, "load_crossover_preview", lambda *a, **k: preview
    )
    monkeypatch.setattr(
        measurement_mod, "load_measurement_state", lambda *a, **k: measurements
    )
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_STATE_PATH", str(tmp_path / "follower_state.json"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))


def _fake_apply_dsp_config():
    async def _apply(*, load_config, candidate_path, **_kw):
        await load_config(str(candidate_path))
        return SimpleNamespace(to_dict=lambda: {"result": "applied"})

    return _apply


def test_program_channel_for_fail_closed() -> None:
    assert fc.program_channel_for("left") == "left"
    assert fc.program_channel_for("right") == "right"
    assert fc.program_channel_for("mono") == "mono"
    for bad in ("stereo", "sub", "", "bogus"):
        with pytest.raises(fc.ActiveFollowerError) as exc:
            fc.program_channel_for(bad)
        assert exc.value.reason == "channel_not_single_box_pick"


def test_apply_emits_reproves_applies_and_stashes(monkeypatch, tmp_path) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    applied = asyncio.run(
        fc.apply_active_follower_config(
            _cfg("left"), camilla_factory=lambda: cam, validate=_valid_config,
        )
    )

    # The driver-domain config was emitted, re-proven, and loaded into CamillaDSP.
    assert applied == fc.FOLLOWER_CONFIG_PATH
    assert cam.loaded == [fc.FOLLOWER_CONFIG_PATH]
    yaml = Path(fc.FOLLOWER_CONFIG_PATH).read_text(encoding="utf-8")
    assert "emit_active_speaker_driver_domain_config" in yaml
    assert "# program_channel=left" in yaml
    assert 'device: "hw:Loopback,1,5"' in yaml  # the round-trip loopback capture
    assert "active_baseline_headroom" not in yaml  # no leader-baked program domain
    # The prior solo-active config was stashed for the unbond restore.
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) == (
        "/var/lib/camilladsp/configs/active_speaker_baseline.yml"
    )


def test_apply_refuses_uncommissioned_box_no_emit(monkeypatch, tmp_path) -> None:
    """Invariant 5 (not-ready path): a box with no ready baseline cannot be
    relocated — apply raises and NEVER loads a config into CamillaDSP."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, {"summary": {}})
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(
            fc.apply_active_follower_config(
                _cfg("left"), camilla_factory=lambda: cam, validate=_valid_config,
            )
        )
    assert exc.value.reason == "baseline_not_ready"
    assert cam.loaded == []  # no full-range (or any) emit reached CamillaDSP


def test_apply_refuses_unprovable_graph_no_emit(monkeypatch, tmp_path) -> None:
    """Invariant 5 (re-proof path): if the EMITTED driver-only graph cannot be
    re-proven, refuse to bond — CamillaDSP is never loaded."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    # Force the re-proof to reject (e.g. a hypothetical emitter regression).
    monkeypatch.setattr(
        runtime_contract_mod,
        "classify_camilla_graph",
        lambda *a, **k: SimpleNamespace(
            allowed=False, classification="unsafe", issues=[{"code": "forced"}],
        ),
    )

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(
            fc.apply_active_follower_config(
                _cfg("right"), camilla_factory=lambda: cam, validate=_valid_config,
            )
        )
    assert exc.value.reason == "graph_unprovable"
    assert cam.loaded == []


def _patch_restore_reproof(monkeypatch, *, allowed: bool):
    """Stub the topology load + the graph re-proof for restore tests."""
    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: object())
    monkeypatch.setattr(
        runtime_contract_mod, "classify_camilla_graph",
        lambda *a, **k: SimpleNamespace(
            allowed=allowed, classification="x" if allowed else "unsafe", issues=[],
        ),
    )


def test_restore_prefers_stash(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    _patch_restore_reproof(monkeypatch, allowed=True)
    solo = tmp_path / "active_speaker_baseline.yml"
    solo.write_text("# solo active baseline\n", encoding="utf-8")
    fc._write_stash(str(solo), path=fc.FOLLOWER_PRIOR_STASH)

    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored == str(solo)
    assert cam.loaded == [str(solo)]
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) is None  # cleared on success


def test_restore_refuses_unprovable_candidate_never_loads_passive(
    monkeypatch, tmp_path,
) -> None:
    """The 'never a passive graph' promise enforced AT LOAD: if the stashed /
    durable candidate cannot be re-proven (corrupt / replaced with a flat
    config — the filesystem-loss class), restore REFUSES to load it onto the
    active sink and leaves CamillaDSP on its current safe graph."""
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    # No durable baseline on disk → only the (unprovable) stash candidate.
    from jasper.active_speaker import baseline_profile as bp_mod
    monkeypatch.setattr(
        bp_mod, "baseline_config_path",
        lambda *a, **k: tmp_path / "no_durable_baseline.yml",
    )
    _patch_restore_reproof(monkeypatch, allowed=False)  # candidate fails re-proof
    corrupt = tmp_path / "active_speaker_baseline.yml"
    corrupt.write_text("# a flat/passive config that slipped onto disk\n", encoding="utf-8")
    fc._write_stash(str(corrupt), path=fc.FOLLOWER_PRIOR_STASH)

    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []  # NEVER loaded the unprovable config onto the active sink


def test_restore_noop_when_solo_box(monkeypatch, tmp_path) -> None:
    """A solo-active box that was never an active follower must not churn
    CamillaDSP (no stash, not on the follower config)."""
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []
