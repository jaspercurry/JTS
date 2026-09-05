# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""leader_config — the grouping reconciler's CamillaDSP apply arm
(Increment 5). Pure parts + the fail-closed refusal path (which raises in
prepare, before any websocket I/O): the restore ladder decision, the
prior-config stash, and the bonded-leader bake's graph-carrier refusal
over a roleful/active config. The SUCCESS apply flows do real CamillaDSP
websocket I/O and are validated on hardware (the doctor's `leader pipe`
check + grouping runtime health are their backstops)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.multiroom.leader_config import (
    BONDED_CONFIG_PATH,
    SOLO_RESTORE_PATH,
    _clear_stash,
    _write_stash,
    playback_is_pipe,
    read_stash,
    restore_action,
)


def test_restore_action_none_on_the_common_solo_reconcile():
    """No stash + CamillaDSP already on a solo config ⇒ nothing to do.
    This is every reconcile run on a solo speaker — it MUST be a no-op
    (no CamillaDSP churn)."""
    assert restore_action(
        stash=None, stash_usable=False, bonded_active=False,
    ) == "none"


def test_restore_action_prefers_a_usable_stash():
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_usable=True,
        bonded_active=True,
    ) == "stash"
    # Stash wins even if camilla already flipped off the bonded config
    # (a half-finished prior unwind retries to the user's real config).
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_usable=True,
        bonded_active=False,
    ) == "stash"


def test_restore_action_re_emits_when_stash_is_missing_gone_or_pipe_shaped():
    # Bonded active but no stash at all (stash lost): re-emit solo.
    assert restore_action(
        stash=None, stash_usable=False, bonded_active=True,
    ) == "re_emit"
    # Stash exists but unusable — its file was deleted, OR its content is
    # PIPE-shaped (a /sound save while bonded regenerated sound_current.yml
    # with the pipe sink; restoring it after disband would point camilla at
    # a FIFO whose creator is stopped — the restart-flap wedge): re-emit.
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_usable=False,
        bonded_active=True,
    ) == "re_emit"


def test_is_pipe_config_distinguishes_pipe_from_solo(tmp_path):
    """The content check both stash guards share, against REAL emitted
    configs (emitter/scanner drift fails here)."""
    from jasper.multiroom.leader_config import _is_pipe_config
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    pipe = tmp_path / "pipe.yml"
    pipe.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        )
    )
    solo = tmp_path / "solo.yml"
    solo.write_text(emit_sound_config(SoundProfile(enabled=False)))

    assert _is_pipe_config(str(pipe)) is True
    assert _is_pipe_config(str(solo)) is False
    assert _is_pipe_config(str(tmp_path / "missing.yml")) is False


@pytest.mark.parametrize("playback,expected", [
    # The emitted shape: quoted filename, 2-space block, 4-space fields.
    ('  playback:\n    type: File\n    filename: "%(fifo)s"\n', True),
    # Quoting and the block's indent are the shared parser's business, not
    # this predicate's.
    ("  playback:\n    type: 'File'\n    filename: %(fifo)s\n", True),
    ('    playback:\n      type: File\n      filename: "%(fifo)s"\n', True),
    # A File sink at any OTHER path is not the bond: the filename is matched
    # EXACTLY, so a fifo-prefixed stale path is not pipe-shaped.
    ('  playback:\n    type: File\n    filename: "%(fifo)s.old"\n', False),
    ('  playback:\n    type: File\n    filename: "/dev/null"\n', False),
    ('  playback:\n    type: Alsa\n    device: "jts_ring_playback"\n', False),
])
def test_playback_is_pipe_keys_on_type_and_exact_filename(playback, expected):
    from jasper.multiroom.reconcile import SNAPFIFO

    text = "devices:\n" + (playback % {"fifo": SNAPFIFO})
    assert playback_is_pipe(text, SNAPFIFO) is expected


def test_playback_is_pipe_fails_closed_on_a_duplicated_devices_key():
    """An ambiguous config yields no devices subset at all, so the pipe
    answer is False rather than a guess."""
    from jasper.multiroom.reconcile import SNAPFIFO

    one = f'devices:\n  playback:\n    type: File\n    filename: "{SNAPFIFO}"\n'
    assert playback_is_pipe(one, SNAPFIFO) is True
    assert playback_is_pipe(one + one, SNAPFIFO) is False


def test_stash_round_trip(tmp_path):
    path = str(tmp_path / "prior.txt")
    assert read_stash(path) is None  # missing file → None, no raise
    _write_stash("/var/lib/camilladsp/configs/sound_current.yml", path)
    assert read_stash(path) == "/var/lib/camilladsp/configs/sound_current.yml"
    _clear_stash(path)
    assert read_stash(path) is None
    _clear_stash(path)  # idempotent


def test_bonded_and_restore_names_are_jts_generated():
    """The /sound preserve logic must recognise the reconciler's configs
    as JTS-generated — else a profile save while bonded would refuse with
    the custom-config error (or worse, an unlisted name would be treated
    as hand-rolled). Pins the _JTS_GENERATED_RE registration."""
    from jasper.multiroom.leader_config import CANONICAL_CAMILLA_CONFIG_DIR
    from jasper.sound.camilla_yaml import is_jts_generated_config

    assert is_jts_generated_config(
        BONDED_CONFIG_PATH, config_dir=CANONICAL_CAMILLA_CONFIG_DIR,
    )
    assert is_jts_generated_config(
        SOLO_RESTORE_PATH, config_dir=CANONICAL_CAMILLA_CONFIG_DIR,
    )


async def test_apply_bonded_leader_refuses_active_config(tmp_path, monkeypatch):
    """The leader bake must fail CLOSED over a roleful active-crossover config
    — never silently rewrite it into the stereo pipe (which would drop the
    crossover/limiter/HP). PR-3 lets a SOLO active baseline host preference EQ,
    but an active baseline forming a bond is the deferred active×grouping case,
    so the leader bake still refuses — now with the typed bonded-member reason
    (it passes member_kwargs, the bonded-bake signal). The refusal raises in
    prepare, before any websocket swap, so it is hardware-free."""
    from jasper.multiroom import leader_config
    from jasper.multiroom.config import GroupingConfig
    from jasper.sound.graph_carrier import CarrierCannotHostEq
    from tests.test_active_speaker_runtime_contract import _active_baseline_yaml

    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    # Redirect the bonded-config write target off /var/lib (the shared apply
    # engine mkdir's the candidate's parent before prepare runs).
    monkeypatch.setattr(
        leader_config, "CANONICAL_CAMILLA_CONFIG_DIR", tmp_path / "configs",
    )
    monkeypatch.setattr(
        leader_config,
        "BONDED_CONFIG_PATH",
        str(tmp_path / "configs" / "grouping_leader.yml"),
    )
    active = tmp_path / "active_speaker_baseline.yml"
    active.write_text(_active_baseline_yaml("mono", 2))

    class _Cam:
        loaded: str | None = None

        async def get_config_file_path(self, *, best_effort=True):
            return str(active)

        async def set_config_file_path(self, path, *, best_effort=False):
            self.loaded = path

    cam = _Cam()
    cfg = GroupingConfig(
        enabled=True, role="leader", channel="left", bond_id="b",
        leader_addr="", buffer_ms=400, codec="flac", error=None,
    )

    with pytest.raises(RuntimeError) as excinfo:
        await leader_config.apply_bonded_leader_config(cfg, camilla_factory=lambda: cam)

    # Surfaced raw, or wrapped as DspApplyError by the shared apply engine.
    err = excinfo.value
    refusal = err if isinstance(err, CarrierCannotHostEq) else err.__cause__
    assert isinstance(refusal, CarrierCannotHostEq)
    assert refusal.reason_code == "eq_on_active_bonded_member"
    # Fail closed: the leader was never swapped onto the bonded pipe config.
    assert cam.loaded is None


def test_solo_restore_emit_is_lenient_under_protected_tweeter(tmp_path, monkeypatch):
    # Un-bonding must ALWAYS succeed. The solo-restore emit is deliberately NOT
    # routed through the graph carrier (a refusal there would strand the speaker
    # on the bonded pipe config), so it must stay lenient even under a
    # protected-tweeter topology — the program-graph guard lives at the /sound
    # carrier and correction, never on the shared emit_sound_config leaf. This
    # pins that the solo-restore emit is never gated (the regression a leaf-level
    # gate would have introduced).
    import json

    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile
    from tests.test_active_speaker_runtime_contract import _active_topology

    topo = tmp_path / "output_topology.json"
    topo.write_text(json.dumps(_active_topology("stereo", "active_2_way").to_dict()))
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo))

    out = tmp_path / "grouping_solo_restore.yml"
    emit_sound_config(
        SoundProfile(enabled=False),
        out_path=out,
        profile_id="grouping-solo-restore",
    )
    assert out.exists()


async def _run_solo_restore(tmp_path, monkeypatch) -> Path:
    """Drive ``restore_solo_config``'s re_emit arm; return the written config.

    The re_emit unwind arm (stash missing/gone) is the one place in this module
    that calls emit_sound_config directly instead of going through
    member_camilla_kwargs — see
    test_solo_restore_emit_is_lenient_under_protected_tweeter for why: un-bonding
    must always succeed.

    The shared apply engine is replaced with a stub that just runs ``prepare``:
    its locking/validation machinery is unrelated to what these tests pin and
    would otherwise need a real writer lock path and a CamillaDSP-valid
    candidate on disk.
    """
    from jasper.multiroom import leader_config

    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    config_dir = tmp_path / "configs"
    solo_restore_path = tmp_path / "grouping_solo_restore.yml"
    monkeypatch.setattr(leader_config, "CANONICAL_CAMILLA_CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        leader_config, "BONDED_CONFIG_PATH", str(config_dir / "grouping_leader.yml")
    )
    monkeypatch.setattr(leader_config, "SOLO_RESTORE_PATH", str(solo_restore_path))
    # No stash — forces the re_emit arm (test_restore_action_re_emits_when_
    # stash_is_missing_gone_or_pipe_shaped pins the "stash=None -> re_emit"
    # decision this relies on).
    monkeypatch.setattr(leader_config, "read_stash", lambda: None)
    monkeypatch.setattr(leader_config, "_clear_stash", lambda: None)

    async def fake_apply_dsp_config(*, prepare=None, **kwargs):
        if prepare is not None:
            prepare()

    monkeypatch.setattr("jasper.dsp_apply.apply_dsp_config", fake_apply_dsp_config)

    class _Cam:
        async def get_config_file_path(self, *, best_effort=True):
            return leader_config.BONDED_CONFIG_PATH  # bonded_active -> re_emit

        async def set_config_file_path(self, path, *, best_effort=False):
            pass

    result = await leader_config.restore_solo_config(camilla_factory=lambda: _Cam())

    assert result == str(solo_restore_path)
    return solo_restore_path


def _save_topology(tmp_path, monkeypatch, topology) -> Path:
    """Point the topology loader at ``topology`` (an OutputTopology, or raw
    text for the corrupt case)."""
    path = tmp_path / "output_topology.json"
    path.write_text(
        topology if isinstance(topology, str) else json.dumps(topology.to_dict())
    )
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    return path


def _unplanned_events(caplog) -> list[dict[str, str]]:
    """The degraded-emit disclosures in ``caplog``, as structured fields."""
    from jasper.multiroom.cascade_timeline import _parse_logfmt_event

    parsed = (_parse_logfmt_event(record.getMessage()) for record in caplog.records)
    return [
        fields
        for event, fields in filter(None, parsed)
        if event == "multiroom.camilla_apply"
        and fields.get("result") == "solo_restore_unplanned"
    ]


def _solo_restore_yaml(muted=frozenset(), fold=None) -> str:
    """The bytes the un-bond re-emit owes for a given channel plan.

    Reconstructed from the same loaders `prepare` uses, so the comparison is
    against a real emitter call rather than against another arm of the code
    under test. The default arguments are the pre-plan call verbatim — the
    emitter's solo-impact contract makes them byte-identical to passing
    neither, which is what "byte-identical to today" means here.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    profile = load_profile()
    settings = load_sound_settings()
    return emit_sound_config(
        profile,
        room_peqs=[],
        profile_id="grouping-solo-restore",
        output_trim_db=output_trim_db(profile, settings),
        enable_rate_adjust=False,
        muted_outputs=muted,
        mono_fold_output=fold,
    )


async def test_solo_restore_re_emit_requests_no_rate_adjust(tmp_path, monkeypatch):
    """It must say enable_rate_adjust=False — this box's local playback sink is
    Ring B (ADR-0100), an ioplug CamillaDSP cannot actuate rate_adjust on
    regardless of what the config asks for; see member_config's module
    docstring."""
    written = await _run_solo_restore(tmp_path, monkeypatch)

    assert "enable_rate_adjust: false" in written.read_text()


# #2179's channel plan reaches the THIRD flat writer. Un-bonding hands the box
# back its own DAC, so the same mutes and the same mono fold every other flat
# writer emits belong here — while the path's binding constraint (un-bonding
# can never refuse) survives every way the plan can fail.


async def test_un_bonding_a_mono_box_emits_the_folded_muted_graph(
    tmp_path, monkeypatch
):
    from jasper.active_speaker.runtime_contract import classify_camilla_graph
    from tests.test_active_speaker_runtime_contract import _full_range_mono_on

    topology = _full_range_mono_on(0)
    _save_topology(tmp_path, monkeypatch, topology)

    text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    # BOTH halves of the plan, against an independently-stated expectation:
    # output 1 declined, and the program folded onto the one declared output.
    assert text == _solo_restore_yaml(muted=frozenset({1}), fold=0)
    # And judged by the verifier that owns the question rather than by reading
    # the bytes: the emitted graph is one this topology accepts.
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, graph.issues
    assert graph.details["hard_muted_outputs"] == [1]


async def test_un_bonding_a_stereo_box_is_byte_identical_to_today(
    tmp_path, monkeypatch
):
    """A stereo topology declares both outputs: nothing to mute, nothing to
    fold. The plan must change nothing at all here."""
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    _save_topology(tmp_path, monkeypatch, _full_range_stereo())

    text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    assert text == _solo_restore_yaml()


@pytest.mark.parametrize("topology", ["{ not json", json.dumps({"kind": "bogus"})])
async def test_un_bonding_never_refuses_on_a_corrupt_topology(
    tmp_path, monkeypatch, caplog, topology
):
    """The binding constraint. A topology that cannot be read must not strand
    the speaker on the bonded pipe config — it degrades to exactly the call
    this path made before the plan existed, and says so."""
    from jasper.multiroom import leader_config

    _save_topology(tmp_path, monkeypatch, topology)

    with caplog.at_level("INFO", logger=leader_config.logger.name):
        text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    assert text == _solo_restore_yaml()
    # Degraded, and DISCLOSED — an unmuted graph on a box that may be mono is
    # not something this path may emit silently.
    assert [fields["error"] for fields in _unplanned_events(caplog)] == [
        "OutputTopologyError"
    ]


async def test_un_bonding_never_refuses_on_a_valid_topology_with_a_bad_field(
    tmp_path, monkeypatch, caplog
):
    """The case the corrupt-topology pins above cannot reach.

    Those two die at an earlier gate — one is not JSON, the other has the wrong
    `kind` — so neither exercises a topology that is valid all the way to a
    single bad FIELD. This one is a well-formed mono layout whose
    `hardware.device_id` is a JSON number: nothing refuses it until the schema
    validates that field, which is exactly where an escaping error would reach
    `apply_dsp_config`, be wrapped as `DspApplyError`, and re-raise — refusing
    the un-bond this path forbids refusing.
    """
    import json as _json

    from jasper.multiroom import leader_config
    from tests.test_active_speaker_runtime_contract import _full_range_mono_on

    payload = _full_range_mono_on(0).to_dict()
    payload["hardware"]["device_id"] = 5
    _save_topology(tmp_path, monkeypatch, _json.dumps(payload))

    with caplog.at_level("INFO", logger=leader_config.logger.name):
        text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    assert text == _solo_restore_yaml()
    assert [fields["error"] for fields in _unplanned_events(caplog)] == [
        "OutputTopologyError"
    ]


async def test_un_bonding_with_no_saved_topology_stays_quiet(
    tmp_path, monkeypatch, caplog
):
    """A MISSING topology is not a failure — nothing is declared, so nothing is
    undeclared. It takes the empty plan without claiming degradation."""
    from jasper.multiroom import leader_config

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "absent_topology.json")
    )

    with caplog.at_level("INFO", logger=leader_config.logger.name):
        text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    assert text == _solo_restore_yaml()
    assert not _unplanned_events(caplog)


async def test_un_bonding_never_refuses_when_the_emit_rejects_the_plan(
    tmp_path, monkeypatch, caplog
):
    """The other way the plan can fail: it is derived, but the emitter refuses
    it at its own API boundary. Un-bonding still succeeds, on today's bytes."""
    import jasper.sound.camilla_yaml as sound_yaml
    from jasper.multiroom import leader_config
    from jasper.sound.camilla_yaml import FlatChannelPlan
    from tests.test_active_speaker_runtime_contract import _full_range_mono_on

    _save_topology(tmp_path, monkeypatch, _full_range_mono_on(0))
    # A plan the emitter MUST reject at its API boundary: a fold whose
    # complement is left unmuted would put raw program on an output the
    # topology never assigned.
    monkeypatch.setattr(
        sound_yaml,
        "flat_graph_channel_plan",
        lambda *a, **kw: FlatChannelPlan(mono_fold_output=0),
    )

    with caplog.at_level("INFO", logger=leader_config.logger.name):
        text = (await _run_solo_restore(tmp_path, monkeypatch)).read_text()

    assert text == _solo_restore_yaml()
    assert [fields["error"] for fields in _unplanned_events(caplog)] == ["ValueError"]
