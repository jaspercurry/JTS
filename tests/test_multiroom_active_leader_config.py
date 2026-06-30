# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-LEADER CamillaDSP apply/restore arm (distributed-active Stage B / Slice
5). Pins the fail-closed GATE (build + RE-PROVE BOTH camilla#1's program bake AND
camilla#2's driver-domain graph — either failing refuses the bond), the bake
apply + stash, the crossover statefile re-seed (the never-flat guarantee for an
armed camilla#2), and the unbond restore (always an ACTIVE graph, never passive,
re-using the shared follower_config ladder)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import jasper.active_speaker.crossover_preview as crossover_preview_mod
import jasper.active_speaker.design_draft as design_draft_mod
import jasper.active_speaker.measurement as measurement_mod
import jasper.active_speaker.runtime_contract as runtime_contract_mod
import jasper.dsp_apply as dsp_apply_mod
import jasper.output_topology as output_topology_mod
import jasper.sound.profile as sound_profile_mod
import jasper.sound.settings as sound_settings_mod
from jasper.multiroom import active_leader_config as alc
from jasper.multiroom import follower_config as fc
from jasper.multiroom.config import GroupingConfig
from jasper.sound.profile import SoundProfile

# Reuse the commissioning-evidence fixtures from the baseline-profile tests so
# the leader's camilla#2 arm is exercised against the SAME evidence shape the
# solo apply + the follower arm use (the leader is its own receiver — the
# driver-domain build is identical, only the config/state paths differ).
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview


def _cfg(channel: str = "left", trim_db: float = 0.0) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel=channel,
        bond_id="bond1",
        leader_addr="",
        buffer_ms=400,
        codec="flac",
        trim_db=trim_db,
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
    # The re-proof uses the STRICT loader (fail-closed); patch that.
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology_strict", lambda *a, **k: topology
    )
    monkeypatch.setattr(design_draft_mod, "load_design_draft", lambda *a, **k: draft)
    monkeypatch.setattr(
        crossover_preview_mod, "load_crossover_preview", lambda *a, **k: preview
    )
    monkeypatch.setattr(
        measurement_mod, "load_measurement_state", lambda *a, **k: measurements
    )
    # Leader-specific config/state/stash paths so nothing clobbers the solo
    # baseline OR the active-follower arm's files.
    monkeypatch.setattr(
        alc, "CROSSOVER_CONFIG_PATH", str(tmp_path / "grouping_active_leader_crossover.yml")
    )
    monkeypatch.setattr(alc, "CROSSOVER_STATE_PATH", str(tmp_path / "crossover_state.json"))
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    # The camilla#1 program bake reads the saved sound profile + trim. Stub them
    # hermetically (a flat profile → File/pipe bake the verifier exempts).
    monkeypatch.setattr(
        sound_profile_mod, "load_profile", lambda *a, **k: SoundProfile(enabled=False)
    )
    monkeypatch.setattr(
        sound_settings_mod, "load_sound_settings", lambda *a, **k: object()
    )
    monkeypatch.setattr(sound_settings_mod, "output_trim_db", lambda *a, **k: 0.0)
    # Snapcast precondition: pretend the binaries are installed (a dev machine has
    # no snapserver/snapclient, which would otherwise fail-close the precheck).
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    # The active-leader program bake currently requires the legacy fan-in
    # loopback capture; isolate tests from the host's persisted coupling file.
    monkeypatch.setattr(alc, "read_persisted_coupling", lambda *a, **k: "loopback")


def _fake_apply_dsp_config():
    async def _apply(*, load_config, candidate_path, **_kw):
        await load_config(str(candidate_path))
        return SimpleNamespace(to_dict=lambda: {"result": "applied"})

    return _apply


# --- the fail-closed GATE: build + RE-PROVE both instances --------------------


def test_precheck_emits_reproves_both_configs(monkeypatch, tmp_path) -> None:
    """Happy path: precheck builds camilla#2's driver-domain graph AND camilla#1's
    program bake, RE-PROVES BOTH with the real classifier, and returns both
    paths. The driver-domain config captures the round-trip loopback and carries
    NO leader-baked program domain; the bake is a File sink writing the snapfifo
    (NOT a DAC)."""
    from jasper.multiroom.reconcile import GROUPING_LOOPBACK_CAPTURE, SNAPFIFO

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    bake_path, crossover_path = asyncio.run(
        alc.precheck_active_leader(_cfg("left"), validate=_valid_config)
    )

    assert bake_path == alc.LEADER_BAKE_CONFIG_PATH
    assert crossover_path == alc.CROSSOVER_CONFIG_PATH

    # camilla#2 driver-domain: loopback capture, channel pick, NO program domain.
    crossover_yaml = Path(crossover_path).read_text(encoding="utf-8")
    assert "emit_active_speaker_driver_domain_config" in crossover_yaml
    assert "# program_channel=left" in crossover_yaml
    assert f'device: "{GROUPING_LOOPBACK_CAPTURE}"' in crossover_yaml
    assert "active_baseline_headroom" not in crossover_yaml  # leader bakes B/C

    # camilla#1 program bake: File sink writing the snapfifo, NO Layer A.
    bake_doc = yaml.safe_load(Path(bake_path).read_text(encoding="utf-8"))
    assert bake_doc["devices"]["playback"]["type"] == "File"
    assert bake_doc["devices"]["playback"]["filename"] == SNAPFIFO
    assert bake_doc["devices"]["enable_rate_adjust"] is False
    assert not any(
        n.startswith("split_active_") for n in bake_doc.get("mixers", {})
    )


def test_precheck_refuses_fifo_coupling_before_emit(monkeypatch, tmp_path) -> None:
    """FIFO fan-in coupling and the active-leader program bake are not yet a
    supported pair: the bake captures the ALSA fan-in loopback while fan-in
    would write the pipe. Refuse before writing either generated config."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(alc, "read_persisted_coupling", lambda *a, **k: "fifo")

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("left"), validate=_valid_config))

    assert exc.value.reason == "fanin_fifo_coupling_unsupported"
    assert not Path(alc.LEADER_BAKE_CONFIG_PATH).exists()
    assert not Path(alc.CROSSOVER_CONFIG_PATH).exists()


def test_precheck_threads_pair_trim_into_leader_crossover(
    monkeypatch, tmp_path,
) -> None:
    """The active leader's own speaker path is camilla#2, not outputd's
    dac_content lane, so grouping trim must be in the driver-domain graph."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    asyncio.run(alc.precheck_active_leader(_cfg("left", trim_db=-4.0), validate=_valid_config))

    crossover_yaml = Path(alc.CROSSOVER_CONFIG_PATH).read_text(encoding="utf-8")
    assert "# pair_trim_db=4.000" in crossover_yaml
    assert "pair_balance_trim:" in crossover_yaml
    assert "parameters: { gain: -4.0000" in crossover_yaml


def test_precheck_refuses_uncommissioned_box_no_emit(monkeypatch, tmp_path) -> None:
    """A box with no ready driver-domain baseline cannot lead — precheck raises
    ActiveLeaderError(baseline_not_ready) and never reaches the bake."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, {"summary": {}})

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("left"), validate=_valid_config))
    assert exc.value.reason == "baseline_not_ready"
    # The bake config was never written (the crossover gate failed first).
    assert not Path(alc.LEADER_BAKE_CONFIG_PATH).exists()


def test_precheck_refuses_unprovable_crossover_graph(monkeypatch, tmp_path) -> None:
    """If camilla#2's emitted driver-domain graph cannot be re-proven, refuse to
    bond (no full-range emit) — the bake re-prove is never reached."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(
        runtime_contract_mod, "classify_camilla_graph",
        lambda *a, **k: SimpleNamespace(
            allowed=False, classification="unsafe", issues=[{"code": "forced"}],
        ),
    )

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("right"), validate=_valid_config))
    assert exc.value.reason == "crossover_graph_unprovable"


def test_precheck_refuses_unprovable_bake_graph(monkeypatch, tmp_path) -> None:
    """The crossover re-proves, but camilla#1's program bake does NOT — refuse to
    bond. Selective re-proof keyed on the config path."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    def _selective(*, config_path=None, **k):
        ok = str(config_path) == alc.CROSSOVER_CONFIG_PATH
        return SimpleNamespace(
            allowed=ok,
            classification="driver_domain" if ok else "unsafe",
            issues=[] if ok else [{"code": "forced_bake"}],
        )

    monkeypatch.setattr(runtime_contract_mod, "classify_camilla_graph", _selective)

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("left"), validate=_valid_config))
    assert exc.value.reason == "bake_graph_unprovable"


def test_precheck_fails_closed_on_unreadable_topology(monkeypatch, tmp_path) -> None:
    """A corrupt/unreadable topology.json (the filesystem-loss class) must make
    precheck REFUSE — not fall through to an empty draft where a flat full-range
    graph would re-prove allowed and reach the tweeter."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    def _boom(*a, **k):
        raise output_topology_mod.OutputTopologyError("topology.json corrupt")

    monkeypatch.setattr(output_topology_mod, "load_output_topology_strict", _boom)

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("left"), validate=_valid_config))
    assert exc.value.reason == "topology_unreadable"


def test_precheck_bad_channel_fails_closed_as_leader_error(monkeypatch, tmp_path) -> None:
    """A single active 2-way leader plays ONE inter-speaker channel; stereo/sub
    fail closed. The shared program_channel_for raises a follower-flavoured error
    that the leader arm re-raises as ActiveLeaderError (reason preserved)."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    for bad in ("stereo", "sub"):
        with pytest.raises(alc.ActiveLeaderError) as exc:
            asyncio.run(alc.precheck_active_leader(_cfg(bad), validate=_valid_config))
        assert exc.value.reason == "channel_not_single_box_pick"


def test_precheck_fails_closed_when_snapcast_missing(monkeypatch, tmp_path) -> None:
    """JTS5 incident (2026-06-23): an active leader hosts the wire (snapserver) +
    plays its own channel (snapclient). With EITHER binary absent there is no FIFO
    reader for camilla#1's bake, so the bake can't release the DAC and arming
    camilla#2 onto the DAC would fight camilla#1 and exhaust its recovery
    budget. Refuse the bond UP FRONT (stay solo-active)."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    # snapserver absent (snapclient present) — either-absent must fail closed.
    monkeypatch.setattr(
        shutil, "which",
        lambda name: None if name == "snapserver" else f"/usr/bin/{name}",
    )

    with pytest.raises(alc.ActiveLeaderError) as exc:
        asyncio.run(alc.precheck_active_leader(_cfg("left"), validate=_valid_config))
    assert exc.value.reason == "snapcast_unavailable"


# --- late applies -------------------------------------------------------------


def test_apply_bake_loads_camilla1_and_stashes(monkeypatch, tmp_path) -> None:
    """apply_active_leader_bake swaps camilla#1 to the (pre-built) program bake
    and stashes the prior solo-active config for the unwind."""
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    applied = asyncio.run(alc.apply_active_leader_bake(camilla_factory=lambda: cam))

    assert applied == alc.LEADER_BAKE_CONFIG_PATH
    assert cam.loaded == [alc.LEADER_BAKE_CONFIG_PATH]
    assert fc.read_stash(alc.LEADER_BAKE_PRIOR_STASH) == (
        "/var/lib/camilladsp/configs/active_speaker_baseline.yml"
    )


def test_apply_bake_does_not_stash_itself(monkeypatch, tmp_path) -> None:
    """A re-reconcile where camilla#1 is ALREADY on the bake must not stash the
    bake path (which the unwind would then never escape)."""
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current=str(tmp_path / "grouping_active_leader_bake.yml"))
    asyncio.run(alc.apply_active_leader_bake(camilla_factory=lambda: cam))
    assert fc.read_stash(alc.LEADER_BAKE_PRIOR_STASH) is None


def test_seed_crossover_statefile_points_at_driver_domain(monkeypatch, tmp_path) -> None:
    """The arm-time re-seed points camilla#2's statefile at the re-proven
    driver-domain config — closing the B1 seam (the install seed is flat; the
    crossover guard repairs only a dead pipe, never a flat statefile)."""
    from jasper.active_speaker import parse_camilla_statefile_config_path

    monkeypatch.setattr(
        alc, "CROSSOVER_CONFIG_PATH", str(tmp_path / "grouping_active_leader_crossover.yml")
    )
    statefile = tmp_path / "crossover-statefile.yml"
    monkeypatch.setenv("JASPER_CAMILLA2_STATEFILE", str(statefile))

    written = alc.seed_crossover_statefile()

    assert written == str(statefile)
    text = statefile.read_text(encoding="utf-8")
    assert parse_camilla_statefile_config_path(text) == alc.CROSSOVER_CONFIG_PATH


# --- unbond restore (re-proven active baseline, never passive) ----------------


def _patch_restore_reproof(monkeypatch, *, allowed: bool):
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology_strict", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        runtime_contract_mod, "classify_camilla_graph",
        lambda *a, **k: SimpleNamespace(
            allowed=allowed, classification="x" if allowed else "unsafe", issues=[],
        ),
    )


def test_restore_prefers_leader_stash(monkeypatch, tmp_path) -> None:
    """Unbond restores camilla#1 from the LEADER stash (the shared ladder, leader
    paths) — re-proven, then loaded; the stash is cleared on success."""
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    _patch_restore_reproof(monkeypatch, allowed=True)
    solo = tmp_path / "active_speaker_baseline.yml"
    solo.write_text("# solo active baseline\n", encoding="utf-8")
    fc._write_stash(str(solo), path=alc.LEADER_BAKE_PRIOR_STASH)

    cam = _FakeCamilla(current=alc.LEADER_BAKE_CONFIG_PATH)
    restored = asyncio.run(alc.restore_active_leader_solo(camilla_factory=lambda: cam))

    assert restored == str(solo)
    assert cam.loaded == [str(solo)]
    assert fc.read_stash(alc.LEADER_BAKE_PRIOR_STASH) is None


def test_restore_refuses_unprovable_candidate_never_loads_passive(
    monkeypatch, tmp_path,
) -> None:
    """The 'never a passive graph' promise: if the stashed candidate cannot be
    re-proven (corrupt / flat — the filesystem-loss class), restore REFUSES to
    load it onto the active sink and leaves camilla#1 on its current safe graph."""
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    from jasper.active_speaker import baseline_profile as bp_mod
    monkeypatch.setattr(
        bp_mod, "baseline_config_path", lambda *a, **k: tmp_path / "no_durable.yml",
    )
    _patch_restore_reproof(monkeypatch, allowed=False)
    corrupt = tmp_path / "active_speaker_baseline.yml"
    corrupt.write_text("# a flat/passive config that slipped onto disk\n", encoding="utf-8")
    fc._write_stash(str(corrupt), path=alc.LEADER_BAKE_PRIOR_STASH)

    cam = _FakeCamilla(current=alc.LEADER_BAKE_CONFIG_PATH)
    restored = asyncio.run(alc.restore_active_leader_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []


def test_restore_noop_when_solo_box(monkeypatch, tmp_path) -> None:
    """A solo-active box that was never an active leader (no leader stash, camilla
    not on the bake config) must not churn camilla#1."""
    monkeypatch.setattr(
        alc, "LEADER_BAKE_CONFIG_PATH", str(tmp_path / "grouping_active_leader_bake.yml")
    )
    monkeypatch.setattr(alc, "LEADER_BAKE_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    restored = asyncio.run(alc.restore_active_leader_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []
