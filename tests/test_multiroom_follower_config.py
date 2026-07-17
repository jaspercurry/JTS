# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-follower CamillaDSP apply/restore arm (distributed-active Slice 3).

Pins invariant 5 — a follower whose driver-only graph cannot be re-proven
REFUSES to bond (no full-range emit) — plus the happy-path emit/apply/stash and
the unbond restore (which must always restore an ACTIVE graph, never passive).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import jasper.active_speaker.crossover_preview as crossover_preview_mod
import jasper.active_speaker.baseline_profile as baseline_profile_mod
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

# Clock-seam guard imports — the active follower's CamillaDSP is the sole
# rate-tracker of the snapclient round-trip loopback (see the clock-seam tests
# at the end of this file).
from jasper.active_speaker import (
    ActiveSpeakerPreset,
    emit_active_speaker_driver_domain_config,
)
from jasper.camilla_config_contract import DEFAULT_CHUNKSIZE
from jasper.multiroom.reconcile import (
    GROUPING_LOOPBACK_CAPTURE,
    GROUPING_LOOPBACK_CAPTURE_FORMAT,
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_bass_extension_profile import _profile


@pytest.fixture(autouse=True)
def _stable_live_graph_authority(monkeypatch):
    async def prove(*_args, **_kwargs):
        return runtime_contract_mod.GraphSafety(
            classification=runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE,
            allowed=True,
        )

    monkeypatch.setattr(fc, "_prove_live_bass_extension_graph", prove)


def _cfg(channel: str = "left", trim_db: float = 0.0) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel=channel,
        bond_id="bond1",
        leader_addr="jts.local",
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
    sealed = replace(
        _profile(topology=topology),
        bass_owner={"kind": "woofer_way", "roles": ["woofer"], "channels": [0]},
    )
    monkeypatch.setattr(
        baseline_profile_mod,
        "evaluate_bass_extension_profile",
        lambda **_kwargs: SimpleNamespace(status="accepted", profile=sealed),
    )
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
    yaml_text = Path(fc.FOLLOWER_CONFIG_PATH).read_text(encoding="utf-8")
    assert "emit_active_speaker_driver_domain_config" in yaml_text
    assert "# program_channel=left" in yaml_text
    assert 'device: "hw:Loopback,1,6"' in yaml_text  # the round-trip loopback capture (shared pair 6)
    assert "active_baseline_headroom" not in yaml_text  # no leader-baked program domain
    document = yaml.safe_load(yaml_text)
    woofer_chain = next(
        step["names"]
        for step in document["pipeline"]
        if step.get("type") == "Filter" and step.get("channels") == [0]
    )
    assert woofer_chain.index("bass_ext_lt") < woofer_chain.index(
        "bass_ext_subsonic"
    ) < woofer_chain.index("as_woofer_delay")
    # The prior solo-active config was stashed for the unbond restore.
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) == (
        "/var/lib/camilladsp/configs/active_speaker_baseline.yml"
    )


def test_apply_threads_pair_trim_into_driver_domain(monkeypatch, tmp_path) -> None:
    """Active followers clear outputd's dac_content trim lane, so grouping trim
    must be emitted into the relocated driver-domain graph."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    asyncio.run(
        fc.apply_active_follower_config(
            _cfg("right", trim_db=-2.5),
            camilla_factory=lambda: cam,
            validate=_valid_config,
        )
    )

    yaml = Path(fc.FOLLOWER_CONFIG_PATH).read_text(encoding="utf-8")
    assert "# pair_trim_db=2.500" in yaml
    assert "pair_balance_trim:" in yaml
    assert "parameters: { gain: -2.5000" in yaml


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
    import jasper.active_speaker.camilla_yaml as camilla_yaml

    original = camilla_yaml._driver_baseline_filter_chain

    def omit_woofer_crossover(preset, role, *args, **kwargs):
        names = original(preset, role, *args, **kwargs)
        if role == "woofer":
            return [name for name in names if not name.endswith("_lp")]
        return names

    monkeypatch.setattr(
        camilla_yaml, "_driver_baseline_filter_chain", omit_woofer_crossover
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


def test_apply_emit_gate_refusal_surfaces_as_follower_error(
    monkeypatch, tmp_path
) -> None:
    """L0 emit-gate seam: if the driver-domain emit REFUSES an unprotected tweeter,
    the gate's ActiveSpeakerConfigError (a ValueError) is converted to
    ActiveFollowerError (a RuntimeError) so the reconciler's `except RuntimeError`
    fail-safe-to-solo path catches it (test_main_active_follower_precheck_failure_
    falls_back_to_solo) instead of the oneshot crashing. CamillaDSP is never
    loaded."""
    import jasper.active_speaker.camilla_yaml as camilla_yaml

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    # Provoke the L0 gate: strip the tweeter high-pass from the baseline chain the
    # driver-domain emitter uses, so the emitted graph is an unprotected tweeter.
    original = camilla_yaml._driver_baseline_filter_chain

    def _hp_stripped(preset, role):
        names = original(preset, role)
        return [n for n in names if not n.endswith("_hp")] if role == "tweeter" else names

    monkeypatch.setattr(camilla_yaml, "_driver_baseline_filter_chain", _hp_stripped)

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(
            fc.apply_active_follower_config(
                _cfg("left"), camilla_factory=lambda: cam, validate=_valid_config,
            )
        )
    assert exc.value.reason == "driver_domain_emit_refused"
    assert isinstance(exc.value, RuntimeError)  # the type the reconciler catches
    assert cam.loaded == []  # no unprotected-tweeter emit reached CamillaDSP


def _patch_restore_reproof(monkeypatch, *, allowed: bool):
    """Stub the topology load + the graph re-proof for restore tests."""
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology_strict", lambda *a, **k: object()
    )
    def decide(_topology, *, current_config_path=None, **_kwargs):
        graph = SimpleNamespace(
            allowed=allowed,
            classification="x" if allowed else "unsafe",
            issues=[],
        )
        return SimpleNamespace(
            current_graph=graph,
            selected_config_path=(str(current_config_path) if allowed else None),
        )

    monkeypatch.setattr(
        runtime_contract_mod, "safe_graph_for_current_topology", decide
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


def test_restore_refuses_candidate_when_reproof_has_no_graph(
    monkeypatch, tmp_path, caplog,
) -> None:
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology_strict", lambda *a, **k: object()
    )
    from jasper.active_speaker import baseline_profile as bp_mod
    monkeypatch.setattr(
        bp_mod, "baseline_config_path", lambda *a, **k: tmp_path / "no_baseline.yml"
    )
    monkeypatch.setattr(
        runtime_contract_mod,
        "safe_graph_for_current_topology",
        lambda *_a, **_k: SimpleNamespace(
            current_graph=None,
            selected_config_path=None,
            issues=({"code": "candidate_unavailable"},),
        ),
    )
    candidate = tmp_path / "active_speaker_baseline.yml"
    candidate.write_text("# candidate with unavailable proof\n", encoding="utf-8")
    fc._write_stash(str(candidate), path=fc.FOLLOWER_PRIOR_STASH)

    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []
    assert "classification=unavailable" in caplog.text
    assert "candidate_unavailable" in caplog.text


def test_precheck_fails_closed_on_unreadable_topology(monkeypatch, tmp_path) -> None:
    """Critical (adversarial review): the re-proof must use the STRICT topology
    loader. A corrupt/unreadable topology.json (the filesystem-loss class) must
    make precheck REFUSE to bond — not fall through to an empty draft where a
    flat full-range graph would re-prove allowed and reach the tweeter."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)

    def _boom(*a, **k):
        raise output_topology_mod.OutputTopologyError("topology.json corrupt")

    monkeypatch.setattr(output_topology_mod, "load_output_topology_strict", _boom)

    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(fc.precheck_active_follower(_cfg("left"), validate=_valid_config))
    assert exc.value.reason == "topology_unreadable"


def test_restore_fails_closed_on_unreadable_topology_never_loads(
    monkeypatch, tmp_path,
) -> None:
    """Critical (adversarial review): on an unreadable topology, restore must NOT
    re-prove against a fail-soft empty draft (where a flat stash would pass) —
    it loads NOTHING and leaves CamillaDSP on its current safe graph."""
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    def _boom(*a, **k):
        raise output_topology_mod.OutputTopologyError("topology.json corrupt")

    monkeypatch.setattr(output_topology_mod, "load_output_topology_strict", _boom)
    # A flat config sitting in the stash (what the filesystem-loss class leaves).
    flat = tmp_path / "active_speaker_baseline.yml"
    flat.write_text("# flat full-range config\n", encoding="utf-8")
    fc._write_stash(str(flat), path=fc.FOLLOWER_PRIOR_STASH)

    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored is None
    assert cam.loaded == []  # never loaded a config under a blind topology


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


# --- follower clock-seam guard -----------------------------------------------
# docs/HANDOFF-distributed-active.md "Clock domain + fail-closed" calls the
# active follower's loopback clock seam safety-critical, but the 2026-06-21
# over-engineering pressure-test found it unpinned by any test. The active
# follower's CamillaDSP is the SOLE rate-tracker of the snapclient round-trip
# loopback it captures, so the seam must hold: chunksize >= 1024 (512 -> EPIPE
# underruns on a Pi), NO resampler (the rate_adjust+AsyncSinc oscillation trap,
# CamillaDSP #207), enable_rate_adjust true, and a RAW hw: loopback capture (a
# plug: device would silently insert a resampler). The follower inherits the
# SHARED DEFAULT_CHUNKSIZE (it passes no chunksize override), so a solo-side
# retune to 512 would silently regress it — these guards fail first.


def _follower_driver_domain_devices() -> dict:
    """The follower's driver-domain ``devices`` block, emitted exactly as the
    reconciler feeds it (raw round-trip loopback capture, shared chunksize
    default — `build_baseline_profile_candidate` passes no chunksize override)."""
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    text = emit_active_speaker_driver_domain_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        program_channel="left",
        capture_device=GROUPING_LOOPBACK_CAPTURE,
        capture_format=GROUPING_LOOPBACK_CAPTURE_FORMAT,
    )
    return yaml.safe_load(text)["devices"]


def test_follower_clock_seam_chunksize_at_least_1024() -> None:
    # The shared default the follower inherits; 512 -> EPIPE underruns.
    assert DEFAULT_CHUNKSIZE >= 1024
    assert _follower_driver_domain_devices()["chunksize"] >= 1024


def test_follower_clock_seam_no_resampler_rate_adjust_on() -> None:
    devices = _follower_driver_domain_devices()
    # The follower is the sole rate-tracker of the loopback it captures.
    assert devices["enable_rate_adjust"] is True
    # No resampler when capture rate == playback rate (the oscillation trap).
    assert "resampler" not in devices
    assert "resampler_type" not in devices


def test_follower_clock_seam_raw_hw_loopback_capture() -> None:
    # A plug: capture would insert a resampler and break bit-perfect tracking;
    # the round-trip loopback must be raw hw + bit-exact format.
    assert GROUPING_LOOPBACK_CAPTURE.startswith("hw:")
    assert "plug" not in GROUPING_LOOPBACK_CAPTURE
    assert GROUPING_LOOPBACK_CAPTURE_FORMAT == "S16_LE"
    devices = _follower_driver_domain_devices()
    assert devices["capture"]["type"] == "Alsa"
    assert devices["capture"]["device"] == GROUPING_LOOPBACK_CAPTURE
