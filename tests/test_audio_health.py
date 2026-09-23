# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re

import pytest

from jasper.control import audio_signal_path, audio_state_issues
from jasper.control.audio_health import compose_audio_health
from jasper.control.audio_health_sampler import AudioHealthSampler

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


def test_a_deaf_output_never_displaces_the_cause_that_produced_it() -> None:
    """Every upstream stall empties the ring, so `output_deaf` co-occurs.

    It latches at 2 s while FANIN_STALE_MS trips at 5, so placing it ahead of
    the fan-in watchdog would make `path_stalled` — and its
    `path.fanin_watchdog_stale` row — unreachable for the whole outage.
    Warmup is the same rule in time: outputd reads an empty ring before
    CamillaDSP is producing, so a deploy's restart is not a deaf speaker.
    """
    stalled_airplay = _airplay()
    stalled_airplay["current"]["fanin"]["watchdog"]["last_progress_age_ms"] = 60_000
    stalled = compose_audio_health(
        airplay=stalled_airplay,
        outputd=_outputd(content_deaf=True),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
    )
    assert stalled["signal_path"]["code"] == "path_stalled"

    warming = compose_audio_health(
        airplay=_airplay(warmup=True),
        outputd=_outputd(content_deaf=True),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux(),
    )
    assert warming["signal_path"]["code"] != "output_deaf"


# --------------------------------------------------------------------------- #
# Household register (#2472).
#
# Every sentence this module writes reaches the /system/ Audio card verbatim —
# that is the one-writer discipline #2466 chose — so all of it is household
# copy: what is wrong with the household's sound, and what they can do about
# it. The operator half of each state keeps its own home: `jasper-doctor` for
# unit names, systemd states and `journalctl` lines, and
# `/state.audio_health.technical` for the raw counters this card is built from.
# --------------------------------------------------------------------------- #

# Vocabulary that means nothing to a household: JTS's own component and unit
# names, the seams between them, and operator commands. `DSP`, `DAC` and `USB`
# are deliberately NOT here — those are printed on the hardware a speaker owner
# buys, unlike `fan-in` or `outputd`, which exist only inside this repo.
_OPERATOR_VOCABULARY = re.compile(
    r"(?i)(?:^|[^A-Za-z])("
    r"fan-?in|outputd|camilladsp|dsp engine|mux|journalctl|systemctl|systemd|"
    r"install\.sh|reconciler|renderer|watchdog|work-loop|backend|alsa|"
    r"route plan|host-clock|signal path|shared audio path|"
    r"jasper-[a-z-]+|[a-z][a-z0-9-]*\.service"
    r")(?:[^A-Za-z]|$)"
)

# The payload keys whose values a household reads. `label` is in here because
# the evidence breakdown rows ({"label", "value"}) render on this same card, so
# a daemon name in a row label is as household-facing as one in a sentence.
# `technical` is skipped wholesale: it is the raw evidence block, and naming
# daemons is its whole job.
_MESSAGE_FIELDS = frozenset({
    "headline", "detail", "title", "summary", "observed", "likely_area", "label",
})


def _household_messages(payload, path: str = "") -> list[tuple[str, str]]:
    """Every household-read sentence in a composed snapshot, with its path."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "technical":
                continue
            where = f"{path}.{key}" if path else str(key)
            if key in _MESSAGE_FIELDS and isinstance(value, str):
                found.append((where, value))
            else:
                found.extend(_household_messages(value, where))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(_household_messages(item, f"{path}[{index}]"))
    return found


def _household_shapes() -> dict[str, dict]:
    """One composed snapshot per household-facing shape this module writes."""
    playing = {"selected": "usbsink", "ladder": "l0_locked"}

    def _mutated(mutate, **kwargs) -> dict:
        airplay = _airplay(**kwargs)
        mutate(airplay)
        return airplay

    def _fanin(airplay) -> dict:
        return airplay["current"]["fanin"]

    def _compose_with(airplay, **kwargs) -> dict:
        kwargs.setdefault("outputd", _outputd())
        kwargs.setdefault("route", _route())
        kwargs.setdefault(
            "mux_status",
            _mux(airplay.get("current", {}).get("fanin", {}).get("selected_input")),
        )
        return compose_audio_health(
            airplay=airplay, issues=[], sampled_at=1000.0, **kwargs
        )

    def _second_tick(airplay_snapshots, outputd_snapshots) -> dict:
        now = [1000.0]
        outputd = list(outputd_snapshots)
        sampler = AudioHealthSampler(
            airplay_sampler=_FakeAirPlay(list(airplay_snapshots)),
            outputd_probe=lambda: outputd.pop(0),
            mux_probe=lambda: _mux(),
            route_probe=_route,
            time_fn=lambda: now[0],
        )
        sampler._tick()
        now[0] += 5.0
        sampler._tick()
        return sampler.snapshot()

    parked = {"coherence_errors": [_ROUTE_DISCONNECTED], "capability_gap": None}
    # A REAL classifier verdict, not a hand-built one: the household sentence
    # must hold for whatever `transport_park` actually returns (#3120).
    live_park = _live_parks()[0]
    failed_unit = {
        "load_state": "loaded", "active_state": "failed", "result": "exit-code",
    }
    skipped_ping = _airplay()
    skipped_ping["current"]["fanin"]["watchdog"]["pings_skipped"] = 1

    return {
        "clean": _compose_with(_airplay(**playing)),
        "starting": _compose_with(
            _mutated(lambda ap: ap["current"].pop("fanin"), warmup=True)
        ),
        # Same shape from the other warmup branch: outputd not up yet.
        "starting_no_output": _compose_with(_airplay(warmup=True), outputd=None),
        "path_unreported": _compose_with(
            _mutated(lambda ap: ap["current"].pop("fanin"), **playing)
        ),
        "output_absent": _compose_with(_airplay(**playing), outputd=None),
        "output_backend_inactive": _compose_with(
            _airplay(**playing), outputd=_outputd(backend="none")
        ),
        "output_stalled": _compose_with(
            _airplay(**playing), outputd=_outputd(progress_age_ms=30_000)
        ),
        "output_deaf": _compose_with(
            _airplay(**playing), outputd=_outputd(content_deaf=True)
        ),
        "path_stalled": _compose_with(_mutated(
            lambda ap: _fanin(ap)["watchdog"].update(last_progress_age_ms=60_000),
            **playing,
        )),
        "input_absent": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(present=False),
            **playing,
        )),
        "input_broken": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(health="broken"),
            **playing,
        )),
        "input_stalled": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(frames_per_sec=0.0),
            **playing,
        )),
        "output_ring_stalled": _compose_with(
            _airplay(ring={"stall_active": True}, **playing)
        ),
        "path_pressured": _compose_with(
            _airplay(ring={"drops_per_sec": 0.4}, **playing)
        ),
        "tts_queue_full": _compose_with(
            _airplay(**playing), outputd=_outputd(tts_pending_frames=96_000)
        ),
        "activity_unknown": _compose_with(_airplay(**playing), mux_status=None),
        "camilla_stopped": _compose_camilla(_CAMILLA_CLEAN_STOP),
        "camilla_not_installed": _compose_camilla({
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
        }),
        "transport_parked": _compose(transport=parked),
        "transport_unservable": _compose_with(
            _airplay(**playing), transport_park=live_park
        ),
        "transport_parked_capability_gap": _compose(transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": {
                "device_id": "innomaker_hifi_amp_pro",
                "device_label": "InnoMaker HiFi AMP Pro",
            },
        }),
        "undeclared_hardware": _compose_with(
            _airplay(),
            outputd=None,
            output_hardware=_output_hardware(),
            output_topology_snapshot=_declared_topology(),
        ),
        "service_failed": _compose(
            service_states={"jasper-usbgadget.service": failed_unit},
        ),
        "off_drift": _compose(
            service_states={"jasper-usbsink.service": {
                "load_state": "loaded",
                "active_state": "active",
                "result": "success",
            }},
            source_intents={"usbsink": False},
        ),
        "source_not_running": _compose(
            service_states={"jasper-usbsink.service": {
                "load_state": "loaded",
                "active_state": "inactive",
                "result": "success",
            }},
        ),
        "usb_latency_fallback": _compose(selected="usbsink", ladder="l2_fallback"),
        "usb_clock_tracking_warn": _compose(selected="usbsink", ladder="l1_warn"),
        "usb_clock_unavailable": _compose(selected="usbsink"),
        "usb_route_unavailable": compose_audio_health(
            airplay=_airplay(selected="usbsink", ladder="l0_locked"),
            outputd=_outputd(),
            route={"status": "unavailable", "low_latency_claim": False},
            issues=[],
            sampled_at=1000.0,
            mux_status=_mux("usbsink"),
        ),
        "outputd_xrun_recovered": _second_tick(
            [_airplay(), _airplay()], [_outputd(), _outputd(dac_xruns=2)]
        ),
        "fanin_watchdog_recovered": _second_tick(
            [_airplay(), skipped_ping], [_outputd(), _outputd()]
        ),
        "clipping": _second_tick(
            [_airplay(), _airplay()], [_outputd(), _outputd(clipped_samples=64)]
        ),
    }


@pytest.mark.parametrize("shape", sorted(_household_shapes()))
def test_every_household_sentence_stays_out_of_operator_register(shape: str) -> None:
    """#2472: no sentence on the household's audio card names JTS's internals.

    If this fails on copy you just wrote, the fix is the sentence — say what is
    wrong with the household's sound and what they can do. The unit name, the
    systemd state and the `journalctl` line belong in doctor, which fails on
    the same facts and already carries all three.
    """
    offenders = {
        where: text
        for where, text in _household_messages(_household_shapes()[shape])
        if _OPERATOR_VOCABULARY.search(text)
    }
    assert not offenders, (
        f"operator vocabulary on the household audio card ({shape}): {offenders}"
    )


def test_the_household_shapes_cover_every_signal_path_code() -> None:
    """The sweep above is only worth its keep if it reaches every shape.

    Equality against `audio_signal_path.SIGNAL_PATH_CODES` — the module's own
    vocabulary, declared beside the branches that emit it — rather than a
    literal kept here. A literal only fails when a shape is RENAMED or
    RETIRED; this fails in both directions, so registering a new shape's code
    (the one edit a new branch cannot skip and still be legible) is what makes
    the sweep demand a fixture for it. What it cannot see is a branch that
    emits a code it never registered — which is exactly why the vocabulary
    lives in the module with the producers and not in this file.
    """
    swept = {
        snapshot["signal_path"]["code"]
        for snapshot in _household_shapes().values()
    }
    assert swept == audio_signal_path.SIGNAL_PATH_CODES, (
        "every signal-path shape must reach the household-register sweep: "
        f"unswept={sorted(audio_signal_path.SIGNAL_PATH_CODES - swept)} "
        f"unregistered={sorted(swept - audio_signal_path.SIGNAL_PATH_CODES)}"
    )


def _every_incident_row() -> list[dict]:
    """Every row `_state_issues` can raise, over all of its branches.

    The rows reach the same card as the sentences swept above but are built by
    a second function, so they are swept from their own writer rather than
    trusted to echo. Each signal-path shape is crossed with the service,
    intent and latency states the other branches key on.
    """
    failed = {
        "load_state": "loaded", "active_state": "failed", "result": "exit-code",
    }
    running = {
        "load_state": "loaded", "active_state": "active", "result": "success",
    }
    service_states = {
        "jasper-camilla.service": _CAMILLA_CLEAN_STOP,
        "jasper-usbgadget.service": failed,
        "jasper-usbsink.service": running,
        "jasper-usbsink-volume.service": running,
    }
    latencies = (
        {"status": "unknown", "runtime": {"raw_mode": "l2_fallback"}},
        {"status": "ok", "runtime": {"raw_mode": "l1_warn"}},
        {"status": "ok", "runtime": {"raw_mode": "disabled"}},
    )
    without_fanin = _airplay(selected="usbsink")
    without_fanin["current"].pop("fanin")
    parks = (None, *_live_parks())
    rows: list[dict] = []
    for snapshot in _household_shapes().values():
        for airplay in (_airplay(selected="usbsink"), without_fanin):
            for latency in latencies:
                for intents in (None, {"usbsink": False}):
                    for park in parks:
                        rows.extend(audio_state_issues._state_issues(
                            airplay,
                            None,
                            snapshot["signal_path"],
                            latency,
                            "usbsink",
                            service_states,
                            intents,
                            activity_unknown=True,
                            coherence_park=(
                                snapshot["signal_path"]
                                if snapshot["signal_path"].get("code")
                                == "transport_parked"
                                else None
                            ),
                            undeclared_hardware=None,
                            transport_park=park,
                        ))
    return rows


def test_every_incident_row_stays_out_of_operator_register() -> None:
    """#2472, for the incident rows: the same bar as the sentences above.

    An audio incident is read by whoever opens the card, not by whoever can
    read a unit file — the failing unit and its state stay in doctor, which
    fails on the same facts.
    """
    rows = _every_incident_row()
    offenders = {
        row["key"]: text
        for row in rows
        for text in (row["title"], row["detail"])
        if _OPERATOR_VOCABULARY.search(text)
    }
    assert not offenders, (
        f"operator vocabulary in audio incident rows: {offenders}"
    )
    # The sweep is only worth its keep if it reached every row. A new row fails
    # here until it is swept, rather than shipping unlinted. The unit names in
    # the last three are KEYS — structured identifiers, never read as a
    # sentence — which is exactly where a unit name is allowed to live.
    assert {row["key"] for row in rows} == {
        "monitor.mux_status_unavailable",
        "path.transport_park.mono_full_range",
        "path.transport_park.passive_stereo_composite",
        "path.transport_park.roleful_active_endpoint_unconverged",
        "path.transport_parked",
        "path.camilla_stopped",
        "path.fanin_unavailable",
        "path.fanin_watchdog_stale",
        "path.outputd_backend_inactive",
        "path.outputd_content_deaf",
        "path.outputd_unavailable",
        "path.outputd_watchdog_stale",
        "path.tts_queue_full",
        "usbsink.clock_tracking_warn",
        "usbsink.host_clock_unavailable",
        "usbsink.input_unavailable",
        "usbsink.latency_fallback",
        "usbsink.latency_state_unavailable",
        "usbsink.service.jasper-usbgadget.service",
        "usbsink.service.jasper-usbsink-volume.service.off_drift",
        "usbsink.service.jasper-usbsink.service.off_drift",
    }
