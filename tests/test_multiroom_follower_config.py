# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-follower CamillaDSP apply/restore arm (distributed-active Slice 3).

Pins invariant 5 — a follower whose driver-only graph cannot be re-proven
REFUSES to bond (no full-range emit) — plus the happy-path emit/apply/stash and
the unbond restore (which must always restore an ACTIVE graph, never passive).
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jasper.log_event import log_event

import jasper.active_speaker.crossover_preview as crossover_preview_mod
import jasper.active_speaker.baseline_profile as baseline_profile_mod
import jasper.active_speaker.design_draft as design_draft_mod
import jasper.active_speaker.measurement as measurement_mod
import jasper.active_speaker.runtime_contract as runtime_contract_mod
import jasper.dsp_apply as dsp_apply_mod
import jasper.output_topology as output_topology_mod
from jasper.multiroom import active_leader_config as alc
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

# Clock-seam guard imports — the grouping ring is the follower's ingress and
# snapclient is its sole rate-tracker (see the clock-seam tests at the end of
# this file).
from jasper.active_speaker import (
    ActiveSpeakerPreset,
    emit_active_speaker_driver_domain_config,
)
from jasper.active_speaker.camilla_yaml import active_emit_devices
from jasper.camilla_config_contract import DEFAULT_CHUNKSIZE
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from jasper.multiroom.grouping_ring import (
    GROUPING_RING_FORMAT,
    GROUPING_RING_PCM,
    GROUPING_RING_PERIOD_FRAMES,
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_bass_extension_profile import _profile

_REAL_PROVE_LIVE_BASS_EXTENSION_GRAPH = fc._prove_live_bass_extension_graph


@pytest.fixture(autouse=True)
def _stable_live_graph_authority(monkeypatch):
    async def prove(
        cam,
        *,
        expected_config_path,
        expected_classification,
        **_kwargs,
    ):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
        assert await cam.get_config_file_path() == str(expected_config_path)
        assert expected_classification in {
            runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE,
            runtime_contract_mod.GRAPH_APPROVED_ACTIVE_RUNTIME,
        }
        return runtime_contract_mod.GraphSafety(
            classification=expected_classification,
            allowed=True,
            config_path=str(expected_config_path),
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
    async def _apply(*, load_config, candidate_path, acquire_lock, **_kw):
        assert acquire_lock is False
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
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
    real_write_stash = fc._write_stash

    def write_stash_while_locked(value, *, path):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
        real_write_stash(value, path=path)

    monkeypatch.setattr(fc, "_write_stash", write_stash_while_locked)

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
    # BOTH capture halves, because the precheck passes both and a wrong FORMAT
    # is not a wrong-sounding graph, it is a negotiation failure at open: the
    # ring PCM is raw (single-valued hw_params, no `plug` in front of it).
    capture = yaml.safe_load(yaml_text)["devices"]["capture"]
    assert capture["device"] == GROUPING_RING_PCM
    assert capture["format"] == GROUPING_RING_FORMAT
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


def test_apply_live_proof_failure_rolls_back_before_unlock(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        fc,
        "FOLLOWER_CONFIG_PATH",
        str(tmp_path / "grouping_follower.yml"),
    )
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    prior = str(tmp_path / "active_speaker_baseline.yml")

    async def refuse(
        cam,
        *,
        expected_config_path,
        expected_classification,
        **_kwargs,
    ):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
        assert expected_config_path == fc.FOLLOWER_CONFIG_PATH
        assert (
            expected_classification
            == runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE
        )
        assert await cam.get_config_file_path() == fc.FOLLOWER_CONFIG_PATH
        raise RuntimeError("candidate proof refused")

    monkeypatch.setattr(fc, "_prove_live_bass_extension_graph", refuse)
    cam = _FakeCamilla(current=prior)

    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(fc.apply_prebuilt_follower_config(camilla_factory=lambda: cam))

    assert exc.value.reason == "graph_unprovable"
    assert cam.loaded == [fc.FOLLOWER_CONFIG_PATH, prior]
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) is None
    assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is None


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


def test_typod_ring_wire_refusal_surfaces_as_follower_error(
    monkeypatch, tmp_path
) -> None:
    """The SAME ``except``, one seam earlier: the candidate's device derivation.

    An ARMED box resolves its sink to the active ring, so the follower's build
    reaches ``active_emit_devices`` — and a typo'd
    ``JASPER_FANIN_RING_WIRE_FORMAT`` refuses there, ABOVE the L0 emit gate the
    test above provokes. #2338 gives that refusal the ``ActiveSpeakerConfigError``
    type precisely so it lands in this same clause: the wire parser raises a
    BARE ``ValueError``, which this ``except`` (a subclass) does not catch, so
    an armed + bonded box with one typo'd env line would abort the grouping
    reconcile oneshot rather than fall back to solo. CamillaDSP is never loaded.
    """
    from jasper.fanin_coupling import (
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        RING_WIRE_FORMAT_ENV_VAR,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    _patch_evidence(monkeypatch, tmp_path, topology, draft, preview, measurements)
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())

    # ARM the box — the endpoint marker is the whole difference between this
    # test and the solo boxes every other case here drives — then typo its wire.
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}=1\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s32le\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )

    cam = _FakeCamilla(current="/var/lib/camilladsp/configs/active_speaker_baseline.yml")
    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(
            fc.apply_active_follower_config(
                _cfg("left"), camilla_factory=lambda: cam, validate=_valid_config,
            )
        )
    assert exc.value.reason == "driver_domain_emit_refused"
    assert isinstance(exc.value, RuntimeError)  # the type the reconciler catches
    # NON-DEGENERATE: this is the WIRE refusal, not some other emit-gate refusal
    # the armed box might have hit on the way.
    assert RING_WIRE_FORMAT_ENV_VAR in str(exc.value)
    assert "s32le" in str(exc.value)
    assert cam.loaded == []


def _patch_restore_reproof(monkeypatch, *, allowed: bool):
    """Stub the topology load + the graph re-proof for restore tests."""
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology_strict", lambda *a, **k: object()
    )

    def decide(_topology, *, current_config_path=None, **_kwargs):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
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


@pytest.mark.parametrize(
    ("actual_path", "actual_classification"),
    [
        ("/tmp/wrong.yml", runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE),
        (None, runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE),
        (
            "/tmp/expected.yml",
            runtime_contract_mod.GRAPH_APPROVED_ACTIVE_RUNTIME,
        ),
    ],
    ids=["wrong-path", "missing-path", "wrong-classification"],
)
def test_live_proof_requires_exact_candidate_path_and_classification(
    monkeypatch,
    actual_path,
    actual_classification,
) -> None:
    monkeypatch.setattr(
        output_topology_mod,
        "load_output_topology_strict",
        lambda: object(),
    )

    async def classify(*_args, **_kwargs):
        return runtime_contract_mod.GraphSafety(
            classification=actual_classification,
            allowed=True,
            config_path=actual_path,
        )

    monkeypatch.setattr(
        runtime_contract_mod,
        "classify_active_bass_extension_graph",
        classify,
    )

    with pytest.raises(RuntimeError, match="live bass-extension graph proof failed"):
        asyncio.run(
            _REAL_PROVE_LIVE_BASS_EXTENSION_GRAPH(
                _FakeCamilla(current="/tmp/expected.yml"),
                expected_config_path="/tmp/expected.yml",
                expected_classification=(
                    runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE
                ),
            )
        )


def test_live_proof_failure_carries_the_boundary_message(monkeypatch) -> None:
    """The raised error must carry the issue MESSAGE, not just its code.

    The boundary's codes are coarse: every way of declining reports
    ``bass_extension_active_snapshot_unstable``. If this caller logs only the
    code, an operator reading the journal is told "unstable" no matter what
    actually happened — which is exactly how the 2026-08-06 bonded-pair outage
    stayed misdiagnosed.
    """

    monkeypatch.setattr(
        output_topology_mod,
        "load_output_topology_strict",
        lambda: object(),
    )

    async def classify(*_args, **_kwargs):
        return runtime_contract_mod.GraphSafety(
            classification=runtime_contract_mod.GRAPH_UNSAFE,
            allowed=False,
            issues=(
                {
                    "severity": "blocker",
                    "code": "bass_extension_active_snapshot_unstable",
                    "message": "the running CamillaDSP graph does not match it",
                },
            ),
        )

    monkeypatch.setattr(
        runtime_contract_mod,
        "classify_active_bass_extension_graph",
        classify,
    )

    with pytest.raises(RuntimeError, match="does not match it"):
        asyncio.run(
            _REAL_PROVE_LIVE_BASS_EXTENSION_GRAPH(
                _FakeCamilla(current="/tmp/expected.yml"),
                expected_config_path="/tmp/expected.yml",
                expected_classification=(
                    runtime_contract_mod.GRAPH_DRIVER_DOMAIN_BASELINE
                ),
            )
        )


def test_reconcile_logs_the_boundary_reason_not_just_the_code(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    """END TO END: the boundary's reason must reach the journal line.

    The test above pins only the RAISE site. That is not where the detail was
    being lost — it was lost one hop later. ``apply_prebuilt_follower_config``
    re-wraps the RuntimeError as ``ActiveFollowerError(reason, short_message)
    from exc``, and ``ActiveFollowerError.__init__`` passes only
    ``short_message`` to ``super().__init__``. So the detail survived on
    ``__cause__`` alone — and ``reconcile`` logs these with ``error=e`` and NO
    ``exc_info``, which renders ``str(e)`` and never walks the cause. The
    operator-visible line therefore said "failed canonical live re-proof" no
    matter which of the boundary's causes had fired, which is how the
    2026-08-06 bonded-pair outage stayed misdiagnosed.

    So this walks all four hops with production code: boundary issue message ->
    ``_prove_live_bass_extension_graph`` -> ``apply_prebuilt_follower_config``
    -> the rendered ``event=multiroom.reconcile.camilla_failed`` line.
    """

    monkeypatch.setattr(
        fc,
        "FOLLOWER_CONFIG_PATH",
        str(tmp_path / "grouping_follower.yml"),
    )
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    monkeypatch.setattr(
        output_topology_mod,
        "load_output_topology_strict",
        lambda: object(),
    )
    # Hop 1: the real boundary declines with its coarse code and a specific
    # message. Every distinct cause reports this same code.
    reason = "the running CamillaDSP graph does not match the statefile-selected config"

    async def classify(*_args, **_kwargs):
        return runtime_contract_mod.GraphSafety(
            classification=runtime_contract_mod.GRAPH_UNSAFE,
            allowed=False,
            issues=(
                {
                    "severity": "blocker",
                    "code": "bass_extension_active_snapshot_unstable",
                    "message": reason,
                },
            ),
        )

    monkeypatch.setattr(
        runtime_contract_mod,
        "classify_active_bass_extension_graph",
        classify,
    )
    # Hops 2 and 3 are production code: the REAL prover (restored over the
    # autouse stub) and the REAL apply, which is where the re-wrap happens.
    monkeypatch.setattr(
        fc,
        "_prove_live_bass_extension_graph",
        _REAL_PROVE_LIVE_BASS_EXTENSION_GRAPH,
    )

    with pytest.raises(fc.ActiveFollowerError) as caught:
        asyncio.run(
            fc.apply_prebuilt_follower_config(
                camilla_factory=lambda: _FakeCamilla(
                    current=str(tmp_path / "active_speaker_baseline.yml")
                )
            )
        )

    assert caught.value.reason == "graph_unprovable"

    # Hop 4: render it the way reconcile does. `error=<exception>` is rendered
    # by log_event as str(exception) — so if the detail is not IN the message
    # it is not in the journal, full stop.
    logger = logging.getLogger("jasper.multiroom.reconcile")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        log_event(
            logger,
            "multiroom.reconcile.camilla_failed",
            action="active_follower_apply",
            error=caught.value,
            level=logging.ERROR,
        )
    line = caplog.records[-1].getMessage()

    assert "event=multiroom.reconcile.camilla_failed" in line
    assert reason in line, (
        "the operator-visible journal line does not name the cause; it reads: "
        f"{line}"
    )


def test_reconcile_camilla_failed_still_logs_without_exc_info() -> None:
    """Anti-drift anchor for the end-to-end test above.

    That test models reconcile's rendering as ``error=<exception>`` with no
    ``exc_info``. If reconcile ever starts passing ``exc_info``, ``__cause__``
    would render on its own and the interpolation could be revisited — so fail
    here and make someone re-read the pair, rather than letting the model
    silently stop describing production.
    """

    source = (
        Path(fc.__file__).resolve().parent / "reconcile.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "log_event"
        and any(
            isinstance(arg, ast.Constant)
            and arg.value == "multiroom.reconcile.camilla_failed"
            for arg in node.args
        )
    ]

    assert sites, "the camilla_failed log sites should be discoverable"
    for site in sites:
        supplied = {kw.arg for kw in site.keywords if kw.arg is not None}
        assert "error" in supplied
        assert "exc_info" not in supplied


def test_every_chained_grouping_error_interpolates_its_cause() -> None:
    """Every `raise Active*Error(...) from exc` must put `exc` IN the message.

    The adjacency guard. The interpolation fix was applied at THREE raise sites
    and originally only one was pinned — so stripping it from the leader-bake
    site (the 2026-08-06 outage site) or the restore site left the suite fully
    green. Per-site assertions close those two; this closes the *class*, so a
    fourth site cannot be added unguarded.

    Why the rule is safe to state absolutely: `reconcile` logs these as
    `error=<exception>`, which `log_event` renders as `str(e)` and which never
    walks `__cause__` — and it must stay that way, because `exc_info=True`
    would attach a full traceback to all seven `camilla_failed` sites, which is
    the journal spam AGENTS.md warns against. So for a chained grouping error,
    `from exc` alone is decoration: if the cause is not in the message, it does
    not exist as far as an operator is concerned. All 8 such sites across the
    two modules satisfy this today.
    """

    modules = [
        Path(fc.__file__).resolve(),
        Path(alc.__file__).resolve(),
    ]
    checked = 0
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            if not isinstance(node.cause, ast.Name):
                continue
            raised = node.exc
            if not (
                isinstance(raised, ast.Call)
                and getattr(raised.func, "id", "")
                in {"ActiveFollowerError", "ActiveLeaderError"}
            ):
                continue
            cause = node.cause.id
            assert len(raised.args) >= 2, (
                f"{path.name}:{node.lineno} raises without a message argument"
            )
            message = ast.unparse(raised.args[1])
            assert cause in message, (
                f"{path.name}:{node.lineno} chains `from {cause}` but does not "
                f"interpolate it into the message ({message!r}). The cause "
                "would live only on __cause__, which reconcile never renders — "
                "the operator would see a generic line for every distinct "
                "failure, which is what cost the 2026-08-06 debugging session."
            )
            checked += 1

    assert checked >= 8, (
        f"expected at least the 8 known chained grouping raises, found {checked}"
        " — the discovery walk has probably stopped matching"
    )


def test_restore_prefers_stash(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fc, "FOLLOWER_CONFIG_PATH", str(tmp_path / "grouping_follower.yml"))
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    _patch_restore_reproof(monkeypatch, allowed=True)
    solo = tmp_path / "active_speaker_baseline.yml"
    solo.write_text("# solo active baseline\n", encoding="utf-8")
    fc._write_stash(str(solo), path=fc.FOLLOWER_PRIOR_STASH)
    real_clear_stash = fc._clear_stash

    def clear_stash_while_locked(path):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
        real_clear_stash(path)

    monkeypatch.setattr(fc, "_clear_stash", clear_stash_while_locked)

    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)
    restored = asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert restored == str(solo)
    assert cam.loaded == [str(solo)]
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) is None  # cleared on success


def test_restore_live_proof_failure_rolls_back_and_keeps_stash(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        fc,
        "FOLLOWER_CONFIG_PATH",
        str(tmp_path / "grouping_follower.yml"),
    )
    monkeypatch.setattr(fc, "FOLLOWER_PRIOR_STASH", str(tmp_path / "stash.txt"))
    monkeypatch.setattr(dsp_apply_mod, "apply_dsp_config", _fake_apply_dsp_config())
    _patch_restore_reproof(monkeypatch, allowed=True)
    solo = tmp_path / "active_speaker_baseline.yml"
    solo.write_text("# solo active baseline\n", encoding="utf-8")
    fc._write_stash(str(solo), path=fc.FOLLOWER_PRIOR_STASH)

    async def refuse(
        cam,
        *,
        expected_config_path,
        expected_classification,
        **_kwargs,
    ):
        assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is not None
        assert expected_config_path == str(solo)
        assert (
            expected_classification
            == runtime_contract_mod.GRAPH_APPROVED_ACTIVE_RUNTIME
        )
        assert await cam.get_config_file_path() == str(solo)
        raise RuntimeError("restore proof refused")

    monkeypatch.setattr(fc, "_prove_live_bass_extension_graph", refuse)
    cam = _FakeCamilla(current=fc.FOLLOWER_CONFIG_PATH)

    with pytest.raises(fc.ActiveFollowerError) as exc:
        asyncio.run(fc.restore_active_follower_solo(camilla_factory=lambda: cam))

    assert exc.value.reason == "restore_graph_unprovable"
    # The reason TOKEN alone is not enough: reconcile logs `error=e`, which
    # renders str(e) and never walks __cause__, so a cause that lives only on
    # `from exc` is invisible to an operator. Asserted here as well as at the
    # apply site because the identical edit was made at three raise sites and
    # only one was pinned — this is that adjacency gap closed.
    assert "restore proof refused" in str(exc.value)
    assert cam.loaded == [str(solo), fc.FOLLOWER_CONFIG_PATH]
    assert fc.read_stash(fc.FOLLOWER_PRIOR_STASH) == str(solo)
    assert dsp_apply_mod._DSP_LOCK_OWNERSHIP.get() is None


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
# active follower's ingress clock seam safety-critical, but the 2026-06-21
# over-engineering pressure-test found it unpinned by any test.
#
# SNAPCLIENT IS THE SOLE TRACKER. It steers its own playback rate against the
# server clock, and the grouping ring it writes reports a real
# `snd_pcm_delay` (occupancy slots x period + the staged remainder), so the
# tracking loop it needs is closed on its side of the ring. CamillaDSP's
# `enable_rate_adjust` cannot be a second tracker here whatever it is set to: a
# ring PCM is an ioplug, so `snd_pcm_info.card` is -1, CamillaDSP builds no
# HCtl, finds neither the Loopback nor the UAC2-gadget mixer element, and the
# request is a silent no-op. The emitted value follows the SINK the endpoint
# plays into (`active_emit_devices`), which is what these guards read it from.
#
# THE CHUNK AND THE RING SLOT ARE ONE NUMBER. On the ring branch the chunk is
# RING_CAMILLA_CHUNKSIZE and the grouping ring's slot is
# GROUPING_RING_PERIOD_FRAMES; one chunk is one slot, the relationship every
# other ring in the tree ships. The 1024 floor that used to sit on this path
# was an snd-aloop EPIPE artifact and is deleted — it would now put a chunk
# FOUR TIMES the playback ring's whole 2-slot buffer (128 x 2 = 256 frames;
# eight times one slot) into the graph.
#
# THE RING BRANCH IS THE ONLY PRODUCTION ONE. `resolve_output_layout` returns
# RING_ACTIVE_PLAYBACK_DEVICE unconditionally for any profile with an active
# outputd lane (jasper/output_topology.py — "the ACTIVE ring, unconditionally
# … OUTPUTD_LEGAL_ENDPOINT_DEVICES is one member"), so a bonded active endpoint
# plays into the active ring whatever its coupling says. The DAC branch is
# reached only by the explicit lab/CI override (the `playback_device` argument
# or JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE); its pins below are LAB GUARDS —
# they hold the pre-ring emit byte-identical on a bench box — not a second
# shipped shape.

_PLAYBACK_BRANCHES = ("hw:CARD=DAC8x,DEV=0", RING_ACTIVE_PLAYBACK_DEVICE)


def _follower_driver_domain_devices(playback_device: str) -> dict:
    """The follower's driver-domain ``devices`` block, emitted exactly as the
    reconciler feeds it: the grouping ring on the capture side (the precheck's
    two kwargs) and the sink's own geometry everywhere else, DERIVED from
    ``active_emit_devices`` the way ``build_baseline_profile_candidate`` derives
    it rather than restated here."""
    devices = active_emit_devices(playback_device)
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    text = emit_active_speaker_driver_domain_config(
        preset,
        playback_device=playback_device,
        program_channel="left",
        capture_device=GROUPING_RING_PCM,
        capture_format=GROUPING_RING_FORMAT,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
    )
    return yaml.safe_load(text)["devices"]


def test_follower_clock_seam_chunk_is_the_ring_slot() -> None:
    # Ring branch: one chunk is one grouping-ring slot, and nothing floors it.
    assert (
        _follower_driver_domain_devices(RING_ACTIVE_PLAYBACK_DEVICE)["chunksize"]
        == GROUPING_RING_PERIOD_FRAMES
    )
    # DAC branch: the sink has no opinion, so the shared default stands.
    assert (
        _follower_driver_domain_devices("hw:CARD=DAC8x,DEV=0")["chunksize"]
        == DEFAULT_CHUNKSIZE
    )


@pytest.mark.parametrize("playback_device", _PLAYBACK_BRANCHES)
def test_follower_clock_seam_no_resampler(playback_device: str) -> None:
    # Capture rate == playback rate on both branches, so there is nothing to
    # resample — and an async resampler is the documented half of the
    # rate-adjust oscillation trap (CamillaDSP #207).
    devices = _follower_driver_domain_devices(playback_device)
    assert "resampler" not in devices
    assert "resampler_type" not in devices


def test_follower_clock_seam_rate_adjust_follows_the_sink() -> None:
    # Ring sink: OFF. A blocking slot handshake gives the rate controller
    # nothing to adjust to, and on a ring capture the request cannot be
    # actuated at all — snapclient is the tracker.
    assert (
        _follower_driver_domain_devices(RING_ACTIVE_PLAYBACK_DEVICE)[
            "enable_rate_adjust"
        ]
        is False
    )
    # DAC sink: unchanged from the pre-ring emit.
    assert (
        _follower_driver_domain_devices("hw:CARD=DAC8x,DEV=0")["enable_rate_adjust"]
        is True
    )


@pytest.mark.parametrize("playback_device", _PLAYBACK_BRANCHES)
def test_follower_capture_is_the_raw_grouping_ring(playback_device: str) -> None:
    # The ring PCM is opened directly: a `plug` wrapper would insert a
    # conversion in front of a single-valued hw_params, and the format has to
    # be the one the conf.d block declares or the open fails at negotiation.
    assert "plug" not in GROUPING_RING_PCM
    assert GROUPING_RING_FORMAT == "S16_LE"
    devices = _follower_driver_domain_devices(playback_device)
    assert devices["capture"]["type"] == "Alsa"
    assert devices["capture"]["device"] == GROUPING_RING_PCM
    assert devices["capture"]["format"] == GROUPING_RING_FORMAT
