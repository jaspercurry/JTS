# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The cause-naming overrides `compose_audio_health` layers onto the signal
path: a parked transport, a stopped CamillaDSP, and hardware detected but
never declared. The override SEQUENCE and its `_yields_to_a_named_cause`
guard stay in `audio_health.compose_audio_health`; these pin the detectors
`audio_signal_path` now owns and the household copy each produces.
"""

from __future__ import annotations

import pytest

from jasper.control import audio_signal_path
from jasper.control.audio_health import compose_audio_health
from jasper.control.audio_health_sampler import AudioHealthSampler
from jasper.output_hardware import OutputHardwareState
from jasper.output_hardware import write_state as write_output_hardware_state

from .audio_health_fixtures import (
    _CAMILLA_CLEAN_STOP,
    _ROUTE_DISCONNECTED,
    _FakeAirPlay,
    _airplay,
    _compose,
    _compose_camilla,
    _declared_topology,
    _live_parks,
    _mux,
    _output_hardware,
    _outputd,
    _route,
)


# --- parked transport (structurally-mute box) ------------------------------
#
# The jts5 shape: CamillaDSP plays an active graph into an snd-aloop lane that
# nothing drains while outputd captures the passive lane, so the speaker emits
# digital silence with every daemon "healthy". ``transport_coherence_report``
# already detected it for doctor; these pin that /state stops calling it ready.

def test_transport_coherence_error_is_not_disguised_as_audio_is_ready() -> None:
    """A structurally-mute box must not report the idle "Audio is ready" line.

    Doctor already FAILs on this state; before this guard ``/state`` still said
    ``overall: idle, "Audio is ready"`` because the health model read only the
    route profile out of the runtime plan and never its coherence errors.
    """
    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": None,
    })

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["status"] == "issue"
    assert health["overall"]["headline"] == health["signal_path"]["headline"]
    # The contradiction itself is operator evidence and stays off the card
    # (#2472); doctor and the transport evidence keep it.
    assert _ROUTE_DISCONNECTED not in health["signal_path"]["detail"]


def _sample_coherence_park(
    monkeypatch,
    *,
    outputd: dict | None = None,
    transport_park_state: dict | None = None,
) -> dict:
    from jasper.control import transport_eligibility

    monkeypatch.setattr(
        transport_eligibility,
        "snapshot",
        lambda: transport_park_state or {"status": "clear", "parks": []},
    )
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: outputd if outputd is not None else _outputd(),
        mux_probe=lambda: {"sources": {}},
        route_probe=lambda: _route(transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": None,
        }),
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    return sampler.snapshot()


def test_transport_coherence_error_is_a_current_incident(monkeypatch) -> None:
    health = _sample_coherence_park(monkeypatch)

    assert health["current_incident"]["key"] == "path.transport_parked"
    assert health["current_incident"]["title"] == health["signal_path"]["headline"]
    assert health["current_incident"]["likely_area"] == "Shared processing path"


@pytest.mark.parametrize(
    ("outputd", "park_state", "current_key", "generic_present"),
    [
        (
            _outputd(progress_age_ms=30_000),
            None,
            "path.outputd_watchdog_stale",
            True,
        ),
        (
            None,
            {
                "status": "parked",
                "parks": [{"park_class": "mono_full_range"}],
            },
            "path.transport_park.mono_full_range",
            False,
        ),
        (
            _outputd(progress_age_ms=30_000),
            {
                "status": "parked",
                "parks": [{"park_class": "mono_full_range"}],
            },
            "path.outputd_watchdog_stale",
            False,
        ),
    ],
)
def test_specific_incident_outranks_or_replaces_the_coherence_park(
    monkeypatch,
    outputd,
    park_state,
    current_key,
    generic_present,
) -> None:
    health = _sample_coherence_park(
        monkeypatch,
        outputd=outputd,
        transport_park_state=park_state,
    )

    assert health["current_incident"]["key"] == current_key
    keys = {issue["key"] for issue in health["issues"]}
    assert ("path.transport_parked" in keys) is generic_present


def test_parked_status_is_the_value_the_dashboard_alerts_on() -> None:
    """The household's parked surface is keyed to this status string.

    #2381: the System view's Audio card (``outputAlert`` in
    ``deploy/assets/system-status/js/audio-sections.js``) is the household's
    only front-page signal that the speaker cannot play, and it shows itself
    when ``overall.status`` equals one exact literal.

    Renaming that status is not silent on the Python side — measured, two
    neighbours here fail on it as well
    (``test_transport_coherence_error_is_not_disguised_as_audio_is_ready`` and
    ``test_parked_graph_keeps_the_speaker_reported_as_parked``). What none of
    them does is point at the browser constant that has to change with them, so
    the rename can be made green by fixing only the Python literals while the
    card quietly stops appearing. This pin is that missing pointer: it holds
    both halves of the coupling in one assertion and names the file to edit.
    """
    from pathlib import Path

    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": None,
    })
    assert health["overall"]["status"] == "issue"

    module = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "assets" / "system-status" / "js" / "audio-sections.js"
    ).read_text()
    assert 'const OUTPUT_ALERT_STATUS = "issue";' in module, (
        "the dashboard's audio alert no longer keys off the status "
        "compose_audio_health emits for a parked speaker"
    )


@pytest.mark.parametrize(
    "device_label", ["InnoMaker HiFi AMP Pro", "Some Other Active DAC"],
)
def test_parked_detail_names_the_dac_that_cannot_drive_an_active_layout(
    device_label: str,
) -> None:
    """The capability cause is named in household language with the fix path."""
    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": {
            "device_id": "some_device",
            "device_label": device_label,
        },
    })

    assert health["signal_path"]["code"] == "transport_parked"
    assert device_label in health["signal_path"]["detail"]
    assert "/sound/speaker/" in health["signal_path"]["detail"]


def test_live_output_failure_keeps_priority_over_the_parked_reason() -> None:
    """A concrete live failure outranks the standing structural reason.

    Parked is persistent and its remedy is "change the layout"; a stalled
    outputd is happening now and has a different remedy, so it must not be
    overwritten.
    """
    health = _compose(
        outputd=_outputd(progress_age_ms=30_000),
        transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": None,
        },
    )

    assert health["signal_path"]["code"] == "output_stalled"


# --- undeclared hardware: "finish setup" hint -------------------------------
#
# #2812: outputd's control socket never answering must not read as a generic,
# unexplained failure once the reconciler has positively identified ready
# hardware the household never declared.

@pytest.mark.parametrize(
    "outputd",
    [None, _outputd(backend="fake")],
    ids=["outputd_absent", "non_alsa_backend"],
)
def test_setup_hint_fires_for_absent_or_non_alsa_output(outputd: dict | None) -> None:
    """Both halves of "outputd is not delivering" get the same hint: never
    started at all (``outputd is None``), and the dual-Apple
    ``action=park_until_active_graph`` path that keeps its socket alive on a
    ``fake`` backend without ever opening ALSA.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=outputd,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert (
        health["overall"]["headline"]
        == audio_signal_path.UNDECLARED_HARDWARE_HEADLINE
    )
    detail = health["overall"]["detail"]
    assert "Dual Apple USB-C DAC 4-channel pair" in detail
    assert "/sound/speaker/" in detail


def test_setup_hint_fires_when_no_topology_was_ever_saved(monkeypatch, tmp_path) -> None:
    """#2812 B2: the feature's primary case -- a box that has NEVER saved a
    topology at all -- must still get the hint.

    This is the false negative the round-2 gate proved live: adoption being
    allowed (a ready record) guarantees ``new_topology_draft``'s missing-file
    auto-seed matches the observed record exactly, so
    ``declared_hardware_mismatch`` alone can never see "genuinely
    undeclared" once hardware is ready -- the two conjuncts were mutually
    exclusive with no topology file. Only
    ``load_output_topology_snapshot``'s ``revision == "missing"`` can.

    Proven through the REAL default output-hardware AND output-topology
    probes (neither injected) against a genuinely absent
    ``JASPER_OUTPUT_TOPOLOGY_PATH`` and a REAL, populated
    ``JASPER_OUTPUT_HARDWARE_STATE_PATH``. Both must be real: an injected
    ``output_hardware_probe`` would desync from ``new_topology_draft``'s OWN
    internal ``load_output_hardware_state()`` read (which sees only the real
    file, not this sampler's injected value) and mask the exact defect this
    test exists to catch -- confirmed by mutation: replacing the revision
    check with an unconditional branch still passed against an
    injected-probe version of this test, because the auto-seed contamination
    never actually happened without a matching real file underneath it.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "never-saved.json")
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", str(tmp_path / "output_hardware.json")
    )
    write_output_hardware_state(
        OutputHardwareState(
            profile_id="dual_apple_usb_c_dac_4ch",
            profile_label="Dual Apple USB-C DAC 4-channel pair",
            status="ready",
            physical_output_count=4,
            apple_dac_count=2,
        ),
        path=tmp_path / "output_hardware.json",
    )
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        # output_hardware_probe AND output_topology_probe are deliberately
        # left at their real defaults -- see the docstring above.
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health is not None
    assert (
        health["overall"]["headline"]
        == audio_signal_path.UNDECLARED_HARDWARE_HEADLINE
    )
    assert "Dual Apple USB-C DAC 4-channel pair" in health["overall"]["detail"]


@pytest.mark.parametrize(
    ("output_hardware", "output_topology_snapshot"),
    [
        # #2812 B1: already declared and armed exactly the hardware attached.
        (
            OutputHardwareState(
                profile_id="apple_usb_c_dongle",
                profile_label="Apple USB-C audio adapter",
                status="ready",
                physical_output_count=2,
                apple_dac_count=1,
            ),
            _declared_topology(
                device_id="apple_usb_c_dongle",
                device_label="Apple USB-C audio adapter",
                physical_output_count=2,
            ),
        ),
        # No record at all (reconciler never ran, or unreadable).
        (None, _declared_topology()),
        # No topology read yet (before the sampler's first slow-cadence tick).
        (_output_hardware(), None),
        # Detection blocked from adoption (degraded/ambiguous).
        (
            _output_hardware(
                status="partial",
                issues=(
                    {
                        "severity": "blocker",
                        "code": "dual_apple_usb_topology_mismatch",
                        "message": (
                            "two Apple DACs are present but not on the same "
                            "USB controller/bus"
                        ),
                    },
                ),
            ),
            _declared_topology(),
        ),
    ],
)
def test_setup_hint_does_not_fire_without_a_genuine_undeclared_match(
    output_hardware, output_topology_snapshot,
) -> None:
    """False-positive guards: adoption being allowed is not, by itself,
    enough to fire the hint — each case here fails one of #2812 B1's two
    conjuncts, or leaves an input unread, and must fall back to the
    pre-existing generic ``output_absent`` wording rather than guess.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
        output_hardware=output_hardware,
        output_topology_snapshot=output_topology_snapshot,
    )

    assert health["signal_path"]["code"] == "output_absent"
    assert (
        health["overall"]["headline"]
        != audio_signal_path.UNDECLARED_HARDWARE_HEADLINE
    )


@pytest.mark.parametrize(
    ("outputd", "expected_code"),
    [
        # Healthy outputd: the ready/mismatched record alone is not the
        # trigger, only `_signal_path`'s own generic absent/non-ALSA wording
        # is refined.
        (_outputd(), "clean"),
        # A concrete, differently-worded live failure keeps its own
        # diagnosis, the same precedence `_parked_signal` and
        # `_stopped_dsp_signal` hold.
        (_outputd(progress_age_ms=30_000), "output_stalled"),
    ],
)
def test_setup_hint_yields_to_signal_paths_own_verdict(outputd, expected_code) -> None:
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=outputd,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert health["signal_path"]["code"] == expected_code
    assert (
        health["overall"]["headline"]
        != audio_signal_path.UNDECLARED_HARDWARE_HEADLINE
    )


@pytest.mark.parametrize(
    ("camilla_state", "warmup", "expected_code", "expected_status"),
    [
        (_CAMILLA_CLEAN_STOP, False, "camilla_stopped", "issue"),
        (
            {"load_state": "not-found", "active_state": "inactive", "sub_state": "dead"},
            False, "camilla_not_installed", "issue",
        ),
        (
            {
                "load_state": "loaded", "active_state": "active",
                "sub_state": "running", "result": "success",
            },
            False, "clean", "ok",
        ),
        (None, False, "clean", "ok"),  # no systemctl
        ({}, False, "clean", "ok"),  # before the first probe
        # Same boot gate as the issue row, so a deploy's bounce cannot
        # flicker the card.
        (_CAMILLA_CLEAN_STOP, True, "clean", "ok"),
    ],
)
def test_camilla_state_shapes_the_signal_path(
    camilla_state: dict | None,
    warmup: bool,
    expected_code: str,
    expected_status: str,
) -> None:
    """The whole payload has to agree (#2163): before this, `_signal_path`
    read only fan-in and outputd — neither of which notices CamillaDSP
    leaving — so one response reported a clean path and an idle "ready"
    overall while carrying its own stopped-processing incident.
    """
    health = _compose_camilla(camilla_state, warmup=warmup)

    assert health["signal_path"]["code"] == expected_code
    assert health["signal_path"]["status"] == expected_status
    if expected_status == "issue":
        assert health["overall"]["status"] == "issue"
        assert health["overall"]["headline"] == health["signal_path"]["headline"]
    else:
        # No source selected in any of these fixtures, so a clean path reads
        # idle ("Audio is ready"), never a stale "ok" from a prior tick.
        assert health["overall"]["status"] == "idle"

    # A never-installed unit keeps its own remedy: no restart installs a
    # unit that is not there, so it must not share the stopped unit's detail.
    if expected_code == "camilla_not_installed":
        stopped = _compose_camilla(_CAMILLA_CLEAN_STOP)["signal_path"]
        assert health["signal_path"]["detail"] != stopped["detail"]


def test_stopped_camilla_outranks_the_deafness_it_causes() -> None:
    """The stopped DSP is the ring writer, so `output_deaf` is its symptom.

    Naming the cause is strictly more useful than naming the silence, and
    `output_deaf` is the one issue-status shape a stopped DSP displaces.
    """
    health = _compose_camilla(
        _CAMILLA_CLEAN_STOP, outputd=_outputd(content_deaf=True)
    )

    assert health["signal_path"]["code"] == "camilla_stopped"


def test_a_park_outranks_the_deafness_it_causes() -> None:
    """A parked lane IS a lane with no producer, so it reads deaf by design.

    `dac_content_marker_beside_bridge` parks a box outputd refuses to start,
    so it zero-fills forever. Letting `output_deaf` stand would replace a
    structural verdict carrying its own rebuild issue with "Try Restart
    audio", which cannot clear it.
    """
    for park in _live_parks():
        health = compose_audio_health(
            airplay=_airplay(),
            outputd=_outputd(content_deaf=True),
            route=_route(),
            issues=[],
            sampled_at=1000.0,
            mux_status=_mux(),
            transport_park=park,
        )
        assert health["signal_path"]["code"] == "transport_unservable"

    coherence = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(content_deaf=True),
        route=_route(
            transport={"coherence_errors": [_ROUTE_DISCONNECTED], "capability_gap": None}
        ),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
    )
    assert coherence["signal_path"]["code"] == "transport_parked"


def test_stopped_camilla_outranks_a_source_that_looks_like_it_is_playing() -> None:
    """mux still calls AirPlay "playing"; nothing reaches the drivers."""
    health = _compose_camilla(_CAMILLA_CLEAN_STOP, selected="airplay")

    assert health["overall"]["status"] == "issue"
    assert health["signal_path"]["code"] == "camilla_stopped"
    # The active source card carries the same reason, not a green "ok".
    airplay_card = next(c for c in health["sources"] if c["id"] == "airplay")
    assert airplay_card["status"] == "issue"
    assert airplay_card["headline"] == health["signal_path"]["headline"]


def test_a_live_path_failure_still_outranks_a_stopped_camilla() -> None:
    """Deference, not precedence: the override only claims a clean path.

    Same `!= "issue"` guard `_parked_signal` uses — a concrete failure that is
    happening in fan-in or outputd keeps its own, more specific remedy.
    """
    health = _compose_camilla(
        _CAMILLA_CLEAN_STOP,
        outputd=_outputd(progress_age_ms=audio_signal_path.OUTPUTD_STALE_MS + 1),
    )

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "output_stalled"
