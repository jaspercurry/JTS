"""leader_config — the grouping reconciler's CamillaDSP apply arm
(Increment 5). Pure parts + the fail-closed refusal path (which raises in
prepare, before any websocket I/O): the restore ladder decision, the
prior-config stash, and the bonded-leader bake's graph-carrier refusal
over a roleful/active config. The SUCCESS apply flows do real CamillaDSP
websocket I/O and are validated on hardware (the doctor's `leader pipe`
check + grouping runtime health are their backstops)."""
from __future__ import annotations

import pytest

from jasper.multiroom.leader_config import (
    BONDED_CONFIG_PATH,
    SOLO_RESTORE_PATH,
    _clear_stash,
    _write_stash,
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
    from jasper.multiroom.leader_config import CONFIG_DIR
    from jasper.sound.camilla_yaml import is_jts_generated_config

    assert is_jts_generated_config(BONDED_CONFIG_PATH, config_dir=CONFIG_DIR)
    assert is_jts_generated_config(SOLO_RESTORE_PATH, config_dir=CONFIG_DIR)


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
    monkeypatch.setattr(leader_config, "CONFIG_DIR", str(tmp_path / "configs"))
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
