# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A kept correction survives the deploy reconcile (#2572).

THE DEFECT. ``jasper-sound reconcile-current-dsp`` runs on every deploy. On a
speaker running a kept active-crossover candidate
(``active_speaker_baseline_candidate_<hash>.yml``) the active carrier recomposes
the graph from the immutable applied-profile record, so the re-emitted CONTENT
is the candidate's own bytes — but the reconcile wrote them to
``sound_current.yml`` anyway, because its unchanged-check was gated on the
running config being the file it was about to write, which a candidate never is.
Nothing about the sound changed; the NAME did. CamillaDSP's statefile then
stopped naming the candidate, and every consumer that asks "is the applied
record still what the speaker is playing?" by comparing PATHS
(:func:`~jasper.active_speaker.baseline_profile.applied_profile_displacement`)
answered "displaced" about a speaker that had not stopped playing it. One layer
up, ``_active_graph_fingerprint`` then returns ``""`` and a measurement round
loses the identity of its own entry graph — mid-series adoption stops chaining.

Observed on jts3 2026-08-15: after a deploy, ``sound_current.yml`` and the kept
candidate had the SAME sha256. An identity move, not a content move.

THE FIX, pinned here: the unchanged-check compares the intent against the config
the speaker is RUNNING, whatever it is named
(``jasper.sound.runtime._running_config_is_intent``). Identical bytes are a
no-op — no write, no load, no displacement. Genuinely changed intent still
re-emits and applies exactly as before.

THE PREMISE these rest on — that the applied record recomposes to the candidate's
bytes at all — is pinned separately and by its owner, next to its already-shipped
sibling for the mutable-evidence recompose:
``tests/test_active_speaker_baseline_profile.py::
test_recompose_applied_baseline_yaml_matches_the_durable_candidate_it_records``.
That is deliberately not restated here: if byte-identity ever stops holding, the
comparison in ``_running_config_is_intent`` simply fails and the reconcile falls
back to today's write-and-apply, so this file's fix is SAFE without it and only
EFFECTIVE with it.

These walk the real reconcile over real files — a real topology, a real applied
snapshot, a real candidate on disk, a real statefile — because the defect was a
predicate about two paths and a mock of either side would pass straight through
it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jasper.active_speaker.baseline_profile import (
    STATE_PATH_ENV,
    applied_profile_displacement,
    build_baseline_profile_candidate,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile
from jasper.sound.runtime import reconcile_current_dsp
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)
from tests.test_transport_endpoint_preservation import _FakeCamilla

pytestmark = pytest.mark.asyncio


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

    return candidate, config_dir, _FakeCamilla(str(candidate))


async def test_a_kept_candidate_survives_the_deploy_reconcile(
    tmp_path: Path, monkeypatch, caplog,
):
    """THE DoD PIN (#2572): mid-series adoption survives a deploy.

    Saved intent is unchanged, so the recompose reproduces the candidate the
    speaker is already playing. Pre-fix this wrote those same bytes to
    ``sound_current.yml`` and re-pointed CamillaDSP at it; the applied record
    was then displaced from the statefile and the round lost its entry graph.
    """
    candidate, config_dir, camilla = _reigning_candidate_box(tmp_path, monkeypatch)
    before = candidate.read_bytes()

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)
    caplog.set_level(logging.INFO, logger="jasper.sound.runtime")

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "unchanged", payload
    assert payload["carrier_kind"] == "active"
    assert payload["current_config_path"] == str(candidate)

    # Nothing was written and nothing was loaded.
    assert camilla.loaded_path is None
    assert not (config_dir / "sound_current.yml").exists()
    assert candidate.read_bytes() == before
    assert not (tmp_path / "dsp.json").exists()

    # THE CONSEQUENCE THE ISSUE IS ABOUT: the record still names what the
    # speaker plays, so the round can still name its own entry graph.
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )

    # The STRICT form: `""` is "the record is authoritative", which is what the
    # fixture asserted before the reconcile ran. Asserting merely "not
    # displaced" would also accept the two could-not-check codes, and a
    # survival claim that tolerates "we lost the ability to look" is not the
    # claim this test is making.
    assert applied_profile_displacement(load_applied_baseline_profile_state()) == ""

    # Observable: ONE line, naming the running config it left in place and the
    # file it declined to write.
    assert "event=sound.reconcile_current_dsp" in caplog.text
    assert "result=unchanged" in caplog.text
    assert "reason=running_config_matches_intent" in caplog.text
    assert f"current={candidate}" in caplog.text
    assert f"candidate={config_dir / 'sound_current.yml'}" in caplog.text
    assert caplog.text.count("event=sound.reconcile_current_dsp") == 1


async def test_changed_intent_still_re_emits_over_a_kept_candidate(
    tmp_path: Path, monkeypatch,
):
    """The other direction: the no-op is about CONTENT, not about the carrier.

    Same box, same kept candidate — but the household has since saved a +6 dB
    bass preference, so the recomposed graph is genuinely different from what
    the speaker is playing. The reconcile must write and load it exactly as
    before; a fix that made the active carrier unconditionally quiet would
    strand saved intent that was never applied.

    The record IS displaced afterwards, and that verdict is HONEST rather than a
    regression: the speaker really did move to a graph the record does not name,
    which is the state ``applied_profile_displacement`` exists to report. What
    #2572 removed is the displacement that reported a move that never happened.
    """
    candidate, config_dir, camilla = _reigning_candidate_box(tmp_path, monkeypatch)

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=6.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    assert payload["carrier_kind"] == "active"
    emitted = config_dir / "sound_current.yml"
    assert camilla.loaded_path == str(emitted)
    assert emitted.exists()
    assert emitted.read_text(encoding="utf-8") != candidate.read_text(encoding="utf-8")
    # The saved preference actually reached the graph (the re-emit is not just
    # a different-looking copy of the same DSP).
    assert "sound_simple_bass" in emitted.read_text(encoding="utf-8")
