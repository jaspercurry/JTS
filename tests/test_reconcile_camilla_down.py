# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The deploy reconcile converges when CamillaDSP is already down (#2664).

THE DEFECT, observed on jts4 2026-08-17 across the #2601 wide-wire flip.
``install.sh`` runs ``jasper-sound reconcile-current-dsp`` before it restarts
CamillaDSP, precisely so CamillaDSP cannot reopen a stale graph. But the
reconcile asked the RUNNING daemon which config was loaded, and on that deploy
the daemon was already stopped — by install itself.
Install could stop CamillaDSP mid-deploy, so the one deploy that most needed a
re-emit was the one that could not reach the websocket: ``get_config_file_path`` raised
``CamillaUnavailable: Connection refused``, the pass aborted, and install then
started CamillaDSP against a statefile still naming the pre-flip
``sound_current.yml`` — ``snd_pcm_hw_params_set_format`` EINVAL five times, then
``start-limit-hit``. A dead speaker that needed a human.

THE FIX, pinned here: a refused websocket is a TRANSPORT failure, not a reason to
abort. ``jasper.sound.runtime.StatefileCamillaController`` answers the same two
questions off disk — CamillaDSP's statefile names the config it opens next — so
the reconcile converges the graph the box will BOOT. One bounded fallback, no
retry ladder, and ``transport=statefile`` on the result line names which path
ran.

THE SAFETY PROPERTY, pinned by
``test_a_roleful_box_never_falls_back_to_the_flat_cutover``: the fallback moves
the transport, never the graph SELECTION. The carrier is still resolved from the
config the statefile already names, so a roleful box (active crossover, per-driver
amps) re-emits its own roleful graph. The tempting shorter fix — "camilla is down,
just point the statefile at the shipped flat baseline, it always starts" — is what
that test exists to kill: ``outputd-cutover.yml`` maps full-range stereo straight
to the DAC outputs, which on a roleful box is full-range signal into tweeters.
Choosing a graph stays :mod:`jasper.active_speaker.runtime_contract`'s job; this
module must never grow a second opinion about it.

These walk the real reconcile over real files — a real topology, a real applied
snapshot, a real statefile — because the defect was an exception escaping a
transaction, and a mock of the transaction would pass straight through it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jasper.active_speaker.state_paths import (
    BASELINE_PROFILE_STATE_ENV as STATE_PATH_ENV,
)
from jasper.active_speaker.baseline_profile import (
    build_baseline_profile_candidate,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.environment import read_camilla_statefile_config_path
from jasper.camilla import CamillaConfigRejected, CamillaUnavailable
from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
from jasper.sound.profile import SoundProfile, save_profile
from jasper.sound.runtime import reconcile_current_dsp
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)
from tests.transport_camilla_fixtures import FakeCamilla


# The width jts4's statefile was stuck at: the pre-#2601 narrow wire. The
# emitter now writes DEFAULT_PLAYBACK_FORMAT, so "did the reconcile converge?"
# is answerable by reading the format out of the config the statefile names.
STALE_PLAYBACK_FORMAT = "S16_LE"


class _DownCamilla:
    """CamillaDSP with nobody listening: every call refuses the connection.

    Mirrors what ``CamillaController._call`` does after its initial attempt AND
    its reconnect retry both fail against a stopped daemon — it wraps the socket
    error in ``CamillaUnavailable``. ``best_effort=False`` re-raises, which is
    what every call site in the reconcile passes.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        self.calls += 1
        raise CamillaUnavailable("[Errno 111] Connection refused")

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        raise AssertionError(
            "a stopped CamillaDSP was asked to load a config over the websocket"
        )


def _stale_sound_current(config_dir: Path) -> Path:
    """A JTS-generated graph at the PRE-flip wire width.

    Deliberately named ``sound_current.yml``: that is the file jts4's statefile
    pointed at, and it resolves to the ``sound_or_correction`` carrier — a
    stereo host the reconcile can re-emit. The only thing wrong with it is its
    width, which is exactly the failure the box died on.
    """

    path = config_dir / "sound_current.yml"
    path.write_text(
        f"""---
# Auto-generated JTS DSP config (id=stale).
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  target_level: 2048
  volume_limit: 0.0
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: {STALE_PLAYBACK_FORMAT}
  playback:
    type: Alsa
    channels: 2
    device: "hw:Loopback,0,0"
    format: {STALE_PLAYBACK_FORMAT}

filters:
  flat:
    type: Gain
    parameters: {{ gain: 0.0000, inverted: false, mute: false }}

mixers:
  master_gain:
    channels: {{ in: 2, out: 2 }}
    mapping: []

pipeline: []
""",
        encoding="utf-8",
    )
    return path


def _statefile_at(tmp_path: Path, monkeypatch, config_path: Path) -> Path:
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config_path}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return statefile


def _sandbox_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))


def _flat_streambox(tmp_path: Path, monkeypatch):
    """jts4's passive shape: a stale generated graph with CamillaDSP down.

    Returns ``(stale_config, config_dir, statefile)``.
    """

    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    stale = _stale_sound_current(config_dir)
    statefile = _statefile_at(tmp_path, monkeypatch, stale)
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(_full_range_stereo(), topology_path)
    _sandbox_state(tmp_path, monkeypatch)
    return stale, config_dir, statefile


def _roleful_box(tmp_path: Path, monkeypatch):
    """A commissioned active-crossover box, camilla down, statefile stale.

    Same real applied-snapshot fixture the #2572 durability tests use: a real
    topology with roleful/protected outputs, a real candidate on disk, and the
    statefile naming it. Returns ``(candidate, config_dir, statefile)``.
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

    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(topology.to_dict()), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state_path = tmp_path / "active_speaker_baseline_profile.json"
    state_path.write_text(json.dumps(applied), encoding="utf-8")
    monkeypatch.setenv(STATE_PATH_ENV, str(state_path))

    statefile = _statefile_at(tmp_path, monkeypatch, candidate)
    _sandbox_state(tmp_path, monkeypatch)
    return candidate, config_dir, statefile


async def test_the_jts4_deploy_converges_instead_of_aborting(
    tmp_path: Path, monkeypatch, caplog,
):
    """THE REGRESSION PIN (#2664): camilla down + stale-width statefile.

    Pre-fix this raised ``CamillaUnavailable`` out of the writer lock, install
    logged a WARN and moved on, and the next ``systemctl start jasper-camilla``
    opened the narrow graph and hit EINVAL. Post-fix the pass completes over the
    statefile and the graph the box boots carries the current width.
    """

    stale, config_dir, statefile = _flat_streambox(tmp_path, monkeypatch)
    assert STALE_PLAYBACK_FORMAT in stale.read_text(encoding="utf-8")
    assert DEFAULT_PLAYBACK_FORMAT != STALE_PLAYBACK_FORMAT

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)
    caplog.set_level(logging.INFO, logger="jasper.sound.runtime")

    camilla = _DownCamilla()
    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    assert payload["transport"] == "statefile"
    assert camilla.calls == 1, "the websocket was retried instead of falling back"

    # THE HEAL: the config the box will boot now carries the current wire width.
    booted = read_camilla_statefile_config_path(statefile)
    assert booted is not None
    booted_text = Path(booted).read_text(encoding="utf-8")
    assert f"format: {DEFAULT_PLAYBACK_FORMAT}" in booted_text
    assert f"format: {STALE_PLAYBACK_FORMAT}" not in booted_text

    # Observable: one line, naming the transport that carried the pass.
    assert "event=sound.reconcile_current_dsp" in caplog.text
    assert "result=reconciled" in caplog.text
    assert "transport=statefile" in caplog.text
    assert caplog.text.count("event=sound.reconcile_current_dsp") == 1


async def test_a_roleful_box_never_falls_back_to_the_flat_cutover(
    tmp_path: Path, monkeypatch,
):
    """THE HEARING-SAFETY PIN: the fallback moves transport, never selection.

    ``outputd-cutover.yml`` maps full-range stereo straight to the DAC outputs.
    On a box whose saved topology assigns an output to a tweeter/protected role
    that is full-range signal into a driver that cannot take it, which is why
    the shipped seeding contract refuses flat on such a box and seeds an
    all-muted graph instead.

    So the camilla-down branch must not reach for a shipped baseline "because it
    always starts". It re-emits from the carrier the statefile already names,
    and this test dies if that is ever replaced with a flat seed.
    """

    candidate, config_dir, statefile = _roleful_box(tmp_path, monkeypatch)

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: _DownCamilla(),
    )
    assert payload["transport"] == "statefile", payload

    booted = read_camilla_statefile_config_path(statefile)
    assert booted is not None
    # NOT the flat cutover. There is one now — the ring sibling collapsed into
    # it (ADR-0100) — and install removes the orphan, so naming the retired file
    # here would pin a shape no box can reach.
    assert Path(booted).name != "outputd-cutover.yml"
    # And what it DOES name is the roleful graph: the per-driver split mixer is
    # the structural signal the safety classifier itself keys on, so asserting
    # it here is asserting "still band-limited per driver", not "still a file".
    booted_text = Path(booted).read_text(encoding="utf-8")
    assert "split" in booted_text, booted_text[:400]
    assert payload["carrier_kind"] == "active", payload


async def test_a_live_camilla_that_rejects_a_config_is_not_treated_as_down(
    tmp_path: Path, monkeypatch,
):
    """``CamillaConfigRejected`` subclasses ``CamillaUnavailable`` — on purpose.

    A daemon that ANSWERED and refused the content is not an absent daemon.
    Catching only the parent would divert a real refusal down the disk path and
    "resolve" it by writing a statefile, hiding a config error behind a
    successful-looking reconcile. The narrower catch re-raises it instead.
    """

    stale, config_dir, statefile = _flat_streambox(tmp_path, monkeypatch)
    before = statefile.read_bytes()

    class _RejectingCamilla:
        async def get_config_file_path(self, *, best_effort: bool = False) -> str:
            raise CamillaConfigRejected("invalid config: filter 'x' is unknown")

        async def set_config_file_path(
            self, path: str, *, best_effort: bool = False
        ) -> bool:  # pragma: no cover - never reached
            raise AssertionError("unreachable")

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)

    with pytest.raises(CamillaConfigRejected):
        await reconcile_current_dsp(
            profile_path=profile_path,
            config_dir=config_dir,
            camilla_factory=lambda: _RejectingCamilla(),
        )

    # Nothing was converged behind the refusal.
    assert statefile.read_bytes() == before


async def test_a_live_camilla_still_reconciles_over_the_websocket(
    tmp_path: Path, monkeypatch,
):
    """The control: an ordinary deploy is unchanged by any of this.

    Without it the two tests above would also pass on an implementation that
    used the statefile for EVERY reconcile — which would silently stop asking
    the running daemon what it is actually playing.
    """

    stale, config_dir, statefile = _flat_streambox(tmp_path, monkeypatch)

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)

    camilla = FakeCamilla(str(stale))
    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    assert payload["transport"] == "websocket"
    # The LIVE daemon was told to load it; the statefile is CamillaDSP's to
    # write in this direction, so the reconcile leaves it alone.
    assert camilla.loaded_path == str(config_dir / "sound_current.yml")
    assert read_camilla_statefile_config_path(statefile) == str(stale)
