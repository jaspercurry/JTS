# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Saved sound changes preserve candidate identity and failed loads preserve the prior graph."""

from __future__ import annotations

import json
import logging

import pytest
from pathlib import Path


from jasper.active_speaker.state_paths import (
    BASELINE_PROFILE_STATE_ENV as STATE_PATH_ENV,
)
from jasper.active_speaker.baseline_profile import (
    applied_profile_displacement,
    build_baseline_profile_candidate,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile
from jasper.sound.runtime import (
    _config_without_id_header,
    reconcile_current_dsp,
)
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)
from tests._log_events import event_fields, event_records
from tests.sound_camilla_fixtures import FakeCamilla


def _reigning_candidate_box(tmp_path: Path, monkeypatch):
    """jts3's mid-series shape: a kept candidate is the running config.

    A real ``write=True`` candidate lands on its own source-fingerprinted
    sibling (#1666), the applied record names THAT file, and both the durable
    statefile and CamillaDSP report it as the running config. Everything is
    published where production reads it, so the reconcile resolves the carrier,
    the live endpoint, and the applied snapshot the way it does on a Pi.

    Returns ``(candidate_path, config_dir, camilla)``.
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=build_crossover_preview(
            draft, created_at="2026-06-14T12:10:00Z"
        ),
        measurements=_measurements(topology, tmp_path),
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_dir / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    applied["status"] = "applied"
    candidate = Path(applied["config"]["path"])
    # The name is load-bearing, not decoration: it is precisely because a
    # candidate is NOT sound_current.yml that the old path-gated check never
    # fired here.
    assert candidate.name.startswith("active_speaker_baseline_candidate_")
    assert candidate.parent == config_dir

    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(topology.to_dict()), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state_path = tmp_path / "active_speaker_baseline_profile.json"
    state_path.write_text(json.dumps(applied), encoding="utf-8")
    monkeypatch.setenv(STATE_PATH_ENV, str(state_path))

    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {candidate}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))

    # The record is authoritative BEFORE the reconcile. Asserting the starting
    # state is what makes the post-condition a survival claim rather than a
    # coincidence — a fixture that started out displaced would pass the "still
    # not displaced" test only by never having been true.
    assert applied_profile_displacement(applied) == ""

    from tests.active_speaker_fixtures import declare_applied_fixture
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(config_dir / "active_speaker_baseline.yml"))
    declare_applied_fixture(monkeypatch, topology, applied)
    return candidate, config_dir, FakeCamilla(str(candidate))


async def test_a_kept_candidate_survives_the_deploy_reconcile(
    tmp_path: Path, monkeypatch, caplog,
):
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
    from jasper.active_speaker.environment import camilla_statefile_path
    from jasper.active_speaker.runtime_contract import write_camilla_statefile

    candidate, config_dir, camilla = _reigning_candidate_box(tmp_path, monkeypatch)
    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)
    caplog.set_level(logging.INFO, logger="jasper.sound.runtime")
    payload = await reconcile_current_dsp(
        profile_path=profile_path, config_dir=config_dir, camilla_factory=lambda: camilla,
    )
    assert payload["carrier_kind"] == "active"
    assert payload["current_config_path"] == str(candidate)
    assert not (config_dir / "sound_current.yml").exists()
    active_path = Path(await camilla.get_config_file_path())
    assert load_applied_baseline_profile_state()["config"]["path"] == str(active_path)
    camilla.current_path = str(active_path)
    camilla.loaded_path = None
    before = active_path.read_bytes()
    dsp_state_before = (tmp_path / "dsp.json").read_bytes()
    caplog.clear()

    payload = await reconcile_current_dsp(
        profile_path=profile_path, config_dir=config_dir, camilla_factory=lambda: camilla,
    )
    assert payload["status"] == "unchanged", payload
    assert camilla.loaded_path is None
    assert active_path.read_bytes() == before
    assert (tmp_path / "dsp.json").read_bytes() == dsp_state_before
    write_camilla_statefile(camilla_statefile_path(), await camilla.get_config_file_path())
    assert applied_profile_displacement(load_applied_baseline_profile_state()) == ""
    assert len(event_records(caplog, "sound.reconcile_current_dsp")) == 1
    fields = event_fields(caplog, "sound.reconcile_current_dsp")
    assert fields["result"] == "unchanged"
    assert fields["reason"] == "running_config_matches_intent"
    assert fields["current"] == str(active_path)
    assert Path(fields["candidate"]).parent == config_dir


class _RejectingCamilla(FakeCamilla):
    """CamillaDSP that refuses the candidate it is asked to load."""

    def __init__(self, current_path: str) -> None:
        super().__init__(current_path)
        self.rejected: list[str] = []

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        self.rejected.append(path)
        if best_effort:
            # The restore path uses best_effort; let it through so the test
            # observes the restore rather than a second refusal.
            self.loaded_path = path
            return True
        return False


async def test_the_reconcile_writes_only_what_the_shared_recompose_produces(
    tmp_path: Path, monkeypatch,
):
    """ONE DERIVER: the reconcile chooses the destination, never the content."""
    from jasper.sound.runtime import _render_saved_dsp_on_carrier

    candidate, config_dir, camilla = _reigning_candidate_box(tmp_path, monkeypatch)
    profile_path = tmp_path / "sound_profile.json"
    saved = SoundProfile(simple_eq=SimpleEq(bass_db=6.0))
    save_profile(saved, profile_path)

    # What the SHARED recompose says this box's graph should be...
    expected = _render_saved_dsp_on_carrier(
        str(candidate),
        profile_path=profile_path,
        config_dir=config_dir,
        write=False,
    ).yaml

    await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert _config_without_id_header(
        Path(camilla.loaded_path).read_text(encoding="utf-8")
    ) == _config_without_id_header(expected)


async def test_a_rejected_re_anchor_leaves_the_candidate_pristine(
    tmp_path: Path, monkeypatch,
):
    """Apply saved intent through the candidate compiler."""
    candidate, config_dir, _camilla = _reigning_candidate_box(tmp_path, monkeypatch)
    camilla = _RejectingCamilla(str(candidate))
    pristine = candidate.read_text(encoding="utf-8")

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=6.0)), profile_path)

    with pytest.raises(Exception):
        await reconcile_current_dsp(
            profile_path=profile_path,
            config_dir=config_dir,
            camilla_factory=lambda: camilla,
        )

    # The refusal actually happened against the candidate itself...
    assert camilla.rejected
    assert camilla.rejected[0] != str(candidate)
    # ...and the commissioned artifact is byte-for-byte what it was.
    assert candidate.read_text(encoding="utf-8") == pristine


async def test_changed_intent_still_re_emits_over_a_kept_candidate(
    tmp_path: Path, monkeypatch,
):
    """The other direction: changed intent still reaches the speaker."""
    candidate, config_dir, camilla = _reigning_candidate_box(tmp_path, monkeypatch)

    before = candidate.read_text(encoding="utf-8")

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=6.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    assert payload["carrier_kind"] == "active"
    # Written back over the candidate, not under a second name.
    assert camilla.loaded_path != str(candidate)
    assert candidate.read_text(encoding="utf-8") == before
    assert not (config_dir / "sound_current.yml").exists()
    emitted = Path(camilla.loaded_path).read_text(encoding="utf-8")
    assert emitted != before
    # The saved preference actually reached the graph (the re-emit is not just
    # a different-looking copy of the same DSP).
    assert "sound_simple_bass" in emitted
    # And the anchor held, so the round can still name its own entry graph.
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )

    from jasper.active_speaker.environment import camilla_statefile_path
    from jasper.active_speaker.runtime_contract import write_camilla_statefile
    write_camilla_statefile(camilla_statefile_path(), await camilla.get_config_file_path())
    assert applied_profile_displacement(load_applied_baseline_profile_state()) == ""
