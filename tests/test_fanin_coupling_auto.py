# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The unattended pass: the USB combo decision, and the ring convergence.

  - the USB combo arms ONLY on a gadget box that ALSO has canonical USB intent
    On, local sources allowed for the current role, and a ready derived
    lifecycle mirror — B2 capability-gated arming + split-brain fix;
  - off a combo box the fan-in keys are EXPLICIT `disabled`, never unset — F5
    jasper.env-precedence fix;
  - a stale JASPER_FANIN_RING_SLOTS self-heals so it cannot leave the box on a
    Ring-A geometry the ioplug will not attach — F6;
  - idempotence (auto pass twice = one write).

There is no coupling DECISION here since ADR-0100: the ring is the only central
transport, so a box it cannot serve parks under its own name rather than
resolving a second route.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

SHIPPED_RING_CONF_D = (
    Path(__file__).resolve().parents[1] / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)

from jasper.env_file import read_value
from jasper.fanin import coupling_auto as ca
from jasper.fanin import coupling_reconcile as cr
from jasper.fanin import latency_mode as lm
from jasper.fanin.ring_health import read_persisted_coupling
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
)


def test_pure_auto_decision_module_does_not_import_transition_owner():
    """ca is the pure decision surface; importing coupling_reconcile (the
    state-owning module) would reintroduce the coupling ADR-0100 removed."""
    tree = ast.parse(Path(ca.__file__).read_text(encoding="utf-8"), filename=ca.__file__)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    banned = "jasper.fanin.coupling_reconcile"
    assert not any(m == banned or m.startswith(f"{banned}.") for m in imported)


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """Keep effective-env tests independent of the developer host's /etc state."""
    jasper_env = tmp_path / "jasper.env"
    jasper_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(cr, "JASPER_ENV_PATH", str(jasper_env))
    monkeypatch.setattr("jasper.fanin.ring_health.JASPER_ENV_PATH", str(jasper_env))


# --------------------------------------------------------------------------
# The USB combo's authority
# --------------------------------------------------------------------------


def test_usbsink_effective_gate_reads_canonical_source_state_and_role(monkeypatch):
    from jasper import source_intent
    from jasper.local_sources import markers
    from jasper.music_sources import Source

    seen = []
    monkeypatch.setattr(
        source_intent,
        "source_intent_enabled",
        lambda source: seen.append(source) or True,
    )
    monkeypatch.setattr(markers, "local_sources_allowed", lambda: (True, None))
    monkeypatch.setattr(ca, "_usbsink_lifecycle_ready", lambda: True)

    assert ca.usbsink_effectively_enabled() is True
    assert seen == [Source.USBSINK]


def test_usbsink_desired_on_but_follower_parked_disarms_effective_gate(
    monkeypatch,
):
    from jasper import source_intent
    from jasper.local_sources import markers as markers

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(
        markers,
        "local_sources_allowed",
        lambda: (False, "bonded follower"),
    )
    monkeypatch.setattr(
        ca,
        "_usbsink_lifecycle_ready",
        lambda: pytest.fail("parked role must short-circuit readiness probe"),
    )

    assert ca.usbsink_effectively_enabled() is False


def test_usbsink_desired_on_but_derived_lifecycle_not_ready_disarms(
    monkeypatch,
):
    from jasper import source_intent
    from jasper.local_sources import markers as markers

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(markers, "local_sources_allowed", lambda: (True, None))
    monkeypatch.setattr(ca, "_usbsink_lifecycle_ready", lambda: False)

    assert ca.usbsink_effectively_enabled() is False


def test_usbsink_canonical_off_dominates_stale_enabled_mirror(monkeypatch):
    from jasper import source_intent

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: False)
    monkeypatch.setattr(
        ca,
        "_usbsink_lifecycle_ready",
        lambda: pytest.fail("canonical Off must short-circuit readiness probe"),
    )

    assert ca.usbsink_effectively_enabled() is False


def test_combo_is_armed_requires_both_signals():
    assert ca.combo_is_armed(gadget_present=True, usb_intent_enabled=True) is True
    assert ca.combo_is_armed(gadget_present=True, usb_intent_enabled=False) is False
    assert ca.combo_is_armed(gadget_present=False, usb_intent_enabled=True) is False
    assert ca.combo_is_armed(gadget_present=False, usb_intent_enabled=False) is False


@pytest.mark.parametrize(
    "mode,decay,floor",
    [
        ("low", "enabled", "576"),
        ("medium", "enabled", "1024"),
        ("high", "disabled", "2560"),
    ],
)
def test_usb_combo_actions_map_fixed_latency_presets(mode, decay, floor):
    acts = ca.usb_combo_actions(armed=True, latency_mode=mode)
    values = {action.key: action.value for action in acts}
    assert all(action.action == "set" for action in acts)
    assert values[ca.USB_DIRECT_ENV_VAR] == "enabled"
    assert values[ca.HOST_CLOCK_ENV_VAR] == "enabled"
    assert values[ca.CUSHION_DECAY_ENV_VAR] == decay
    assert values[ca.CUSHION_DECAY_FLOOR_ENV_VAR] == floor


def test_usb_combo_actions_explicit_disabled_when_not_armed():
    # F5: explicit `disabled` (NOT unset) so a stale jasper.env `enabled` can't win.
    acts = ca.usb_combo_actions(armed=False)
    assert all(a.action == "set" for a in acts)
    assert [a.value for a in acts[:3]] == ["disabled"] * 3
    assert acts[3].value == "576"
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_usb_latency_preference_reports_selected_applied_and_recovery(tmp_path):
    state_path = tmp_path / "usb_latency.env"
    lm.write_requested_mode("medium", state_path)
    airplay = {
        "current": {
            "fanin": {
                "inputs": {
                    "usbsink": {
                        "resampler": {
                            "locked": True,
                            "held_target_frames": 2048,
                            "decay": {"enabled": True, "floor_frames": 1024},
                        },
                    },
                },
            },
        },
    }

    state = lm.read_state(airplay, state_path=state_path)

    assert state["selected_mode"] == "medium"
    assert state["applied_mode"] == "medium"
    assert state["effective_mode"] is None
    assert state["state"] == "recovery"
    assert state["live_buffer_ms"] == 42.7
    assert lm.read_requested_mode(state_path) == "medium"


def test_usb_latency_reports_apply_transition_instead_of_stale_mismatch(tmp_path):
    state_path = tmp_path / "usb_latency.env"
    lm.write_requested_mode("low", state_path)
    old_high = {
        "current": {
            "fanin": {
                "inputs": {
                    "usbsink": {
                        "resampler": {
                            "locked": True,
                            "held_target_frames": 2560,
                            "decay": {"enabled": False, "floor_frames": 2560},
                        },
                    },
                },
            },
        },
    }

    state = lm.read_state(
        old_high,
        state_path=state_path,
        applying_mode="low",
    )

    assert state["state"] == "applying"
    assert state["error"] is None
    assert "High remains active" in state["detail"]


def test_usb_latency_reports_terminal_host_clock_fallback(tmp_path):
    state_path = tmp_path / "usb_latency.env"
    lm.write_requested_mode("low", state_path)
    fallback = {
        "current": {
            "fanin": {
                "host_clock": {
                    "ladder": "l2_fallback",
                    "fallback_reason": "probe_noncompliant",
                },
                "inputs": {
                    "usbsink": {
                        "resampler": {
                            "locked": True,
                            "held_target_frames": 2560,
                            "decay": {"enabled": True, "floor_frames": 576},
                        },
                    },
                },
            },
        },
    }

    state = lm.read_state(fallback, state_path=state_path)

    assert state["state"] == "fallback"
    assert state["effective_mode"] == "high"
    assert "host timing check failed" in state["detail"]
    assert "53.3 ms" in state["detail"]
    assert "next USB session" in state["detail"]


def test_usb_latency_does_not_treat_idle_ceiling_as_effective_high(tmp_path):
    state_path = tmp_path / "usb_latency.env"
    lm.write_requested_mode("low", state_path)
    idle = {
        "current": {
            "fanin": {
                "host_clock": {
                    "ladder": "probing",
                    "probe": {"waiting_for_lock": False},
                },
                "inputs": {
                    "usbsink": {
                        "resampler": {
                            "locked": False,
                            "held_target_frames": 2560,
                            "decay": {"enabled": True, "floor_frames": 576},
                        },
                    },
                },
            },
        },
    }

    state = lm.read_state(idle, state_path=state_path)

    assert state["state"] == "idle"
    assert state["effective_mode"] is None
    assert "when USB audio starts" in state["detail"]


def test_usb_latency_local_fallback_can_recover_in_same_session(tmp_path):
    state_path = tmp_path / "usb_latency.env"
    lm.write_requested_mode("low", state_path)
    fallback = {
        "current": {
            "fanin": {
                "host_clock": {
                    "ladder": "l2_fallback",
                    "fallback_reason": "actuator_unavailable",
                },
                "inputs": {
                    "usbsink": {
                        "resampler": {
                            "locked": True,
                            "held_target_frames": 2560,
                            "decay": {"enabled": True, "floor_frames": 576},
                        },
                    },
                },
            },
        },
    }

    state = lm.read_state(fallback, state_path=state_path)

    assert state["state"] == "fallback"
    assert state["effective_mode"] == "high"
    assert "temporarily unavailable" in state["detail"]
    assert "retry automatically" in state["detail"]
    assert "next USB session" not in state["detail"]


def test_usb_latency_apply_keeps_requested_mode_visible_on_reconcile_failure(
    tmp_path,
):
    state_path = tmp_path / "usb_latency.env"

    with pytest.raises(lm.LatencyApplyError, match="restart failed"):
        lm.apply_requested_mode(
            "high",
            state_path=state_path,
            reconcile=lambda **_kwargs: SimpleNamespace(
                ok=False, detail="restart failed"
            ),
        )

    assert lm.read_requested_mode(state_path) == "high"


def test_live_gadget_probe_reads_shared_resolved_capability(monkeypatch):
    monkeypatch.setattr(
        ca,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=True),
    )
    assert ca.read_usb_gadget_available() is True

    monkeypatch.setattr(
        ca,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=False),
    )
    assert ca.read_usb_gadget_available() is False


# --------------------------------------------------------------------------
# reconcile_auto orchestration — env writes, combo, idempotence
# --------------------------------------------------------------------------


def _stub_ring_geometry_heals(monkeypatch):
    """Keep the ring convergence's own file reads out of these combo tests.

    The two geometry heals run on every pass and read the conf.d and /dev/shm;
    stub both so these tests exercise the combo half only. Their own behavior is
    covered separately (the F6 tests below run the REAL slot heal).
    """
    monkeypatch.setattr(
        cr, "_migrate_stale_fanin_ring_slots", lambda snap, reason: (snap, False)
    )
    monkeypatch.setattr(
        cr, "_delete_stale_ring_files", lambda reason, fanin_text="": False
    )


def _persist_ring_eligible_topology(tmp_path: Path, monkeypatch) -> Path:
    """Save the explicit passive stereo intent required by Ring B."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    path = tmp_path / "output_topology.json"
    save_output_topology(_full_range_stereo(), path=path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    return path


def _armed_outputd_env() -> str:
    """The ``outputd.env`` an ALREADY-ARMED ring box actually carries.

    An EMPTY file is not that box: an absent ``JASPER_OUTPUTD_CONTENT_FORMAT``
    declares the outputd daemon's own compiled-in ``S16_LE`` default, which
    ``ring_edge_width_ready`` correctly refuses against a wide resolved wire —
    so a fixture that writes nothing here is testing a genuinely sheared box
    while claiming to test a healthy one. ``jasper-audio-hardware-reconcile`` is
    that key's single writer and derives it from the coupling, so the fixture
    derives it the same way rather than naming a literal that could drift.
    """
    from jasper.fanin_coupling import content_lane_format_for_coupling

    return (
        "JASPER_OUTPUTD_CONTENT_FORMAT="
        f"{content_lane_format_for_coupling()}\n"
    )


def _auto(
    fanin,
    outputd,
    *,
    gadget,
    restarts,
    usb_intent=None,
    camilla_ok=True,
    fanin_ok=True,
    camilla_stop_ok=True,
    camilla_start_ok=True,
    kick_ok=True,
):
    """Run reconcile_auto with recorded daemon ops.

    ``usb_intent`` defaults to ``gadget`` so a test that says "gadget on" gets the
    combo armed unless it opts out — matching the common jts.local case (gadget
    present AND USB audio on). ``camilla_stop``/``camilla_start`` are the coordinated
    combo-restart pause/resume ops (record ``camilla_stop``/``camilla_start`` in
    ``restarts``) so the RTTIME-SIGKILL coordination can be exercised hardware-free.
    ``kick_hardware_reconcile`` records ``hardware_reconcile`` — the spine's
    content-format converge — so its ORDER against ``outputd`` is assertable, and
    ``kick_ok=False`` exercises the fail-closed refusal."""
    if usb_intent is None:
        usb_intent = gadget

    def rf():
        restarts.append("fanin")
        return (fanin_ok, "" if fanin_ok else "fanin restart failed")

    def ro():
        restarts.append("outputd")
        return (True, "")

    def rsc():
        restarts.append("camilla_stop")
        return (camilla_stop_ok, "" if camilla_stop_ok else "camilla stop failed")

    def rstc():
        restarts.append("camilla_start")
        return (camilla_start_ok, "" if camilla_start_ok else "camilla start failed")

    def rc():
        restarts.append(f"camilla:{COUPLING_SHM_RING}")
        return (camilla_ok, "reconciled" if camilla_ok else "bad")

    def kh():
        restarts.append("hardware_reconcile")
        return (kick_ok, "" if kick_ok else "hardware reconcile kick failed")

    return cr.reconcile_auto(
        reason="t",
        env_path=fanin,
        outputd_env_path=outputd,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        restart_fanin=rf,
        restart_outputd=ro,
        stop_camilla=rsc,
        start_camilla=rstc,
        reconcile_camilla=rc,
        kick_hardware_reconcile=kh,
    )

def test_auto_gadget_box_with_intent_arms_ring_and_combo(
    tmp_path, monkeypatch
):
    """jts.local shape: gadget present and USB audio ON — the pass writes
    shm_ring and enables the fan-in direct-capture combo."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _persist_ring_eligible_topology(tmp_path, monkeypatch)
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.combo_armed is True
    assert r.usb_combo_changed is True
    assert r.ok is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "enabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "enabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "enabled"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_SHM_RING
    assert r.restarted_fanin_for_combo is False


def test_auto_gadget_present_but_usb_audio_off_does_not_arm_combo(tmp_path, monkeypatch):
    """B2: a gadget-capable box with USB audio Off must not arm the combo; it
    writes explicit-off values rather than enabled ones."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, usb_intent=False, restarts=restarts
    )
    assert r.combo_armed is False
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"


def test_auto_malformed_usb_intent_disarms_stale_combo_then_fails(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Invalid canonical USB intent must not abort before the safe disarm.

    Unreadable authorization cannot preserve a stale USB DIRECT lane, and it
    cannot fail the ring convergence either — the two halves are independent.
    """

    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
        f"{ca.USB_DIRECT_ENV_VAR}=enabled\n"
        f"{ca.HOST_CLOCK_ENV_VAR}=enabled\n"
        f"{ca.CUSHION_DECAY_ENV_VAR}=enabled\n"
        "JASPER_UNRELATED_SOURCE_SENTINEL=enabled\n"
    )
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_geometry_heals(monkeypatch)

    def invalid_usb_intent():
        raise RuntimeError("bad USB intent value")

    monkeypatch.setattr(
        cr,
        "usbsink_effectively_enabled",
        invalid_usb_intent,
    )
    restarts: list[str] = []
    caplog.set_level("ERROR", logger=cr.__name__)

    result = cr.reconcile_auto(
        reason="malformed_usb_test",
        env_path=fanin,
        outputd_env_path=outputd,
        gadget_present=True,
        restart_fanin=lambda: (restarts.append("fanin"), (True, ""))[1],
        restart_outputd=lambda: (restarts.append("outputd"), (True, ""))[1],
        stop_camilla=lambda: (restarts.append("camilla_stop"), (True, ""))[1],
        start_camilla=lambda: (restarts.append("camilla_start"), (True, ""))[1],
        reconcile_camilla=lambda: (True, "reconciled"),
        kick_hardware_reconcile=lambda: (True, ""),
    )

    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "disabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "disabled"
    assert read_value(text, "JASPER_UNRELATED_SOURCE_SENTINEL") == "enabled"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_SHM_RING
    assert restarts == ["camilla_stop", "fanin", "camilla_start"]
    assert result.combo_armed is False
    assert result.usb_combo_changed is True
    assert result.restarted_fanin_for_combo is True
    assert result.usb_intent_enabled is False
    assert result.ok is False
    assert "bad USB intent value" in result.detail
    assert "result=auto_usb_intent_fail_closed" in caplog.text


def test_auto_gadget_lost_clears_stale_combo_keys(tmp_path, monkeypatch):
    """Single-writer discipline: a box that previously had the combo armed but LOST
    the gadget has the fan-in combo keys driven to their explicit OFF value
    (`disabled`, F5: not unset, so a stale jasper.env `enabled` can't win).
    fan-in restarts to release the gadget. USB audio is left unavailable."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_USB_DIRECT=enabled\n"
        "JASPER_FANIN_HOST_CLOCK=enabled\n"
        "JASPER_FANIN_RESAMPLER_CUSHION_DECAY=enabled\n"
    )
    outputd.write_text("")
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert r.usb_combo_changed is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "disabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "disabled"
    # fan-in restarts to release the gadget; no second audio owner is involved.
    assert "fanin" in restarts


def test_auto_is_idempotent_second_pass_writes_nothing(tmp_path, monkeypatch):
    """Idempotence: two identical auto passes converge with ONE write. The second
    pass reports no combo change and leaves fanin.env byte-identical."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _persist_ring_eligible_topology(tmp_path, monkeypatch)
    _stub_ring_geometry_heals(monkeypatch)

    r1 = _auto(fanin, outputd, gadget=True, restarts=[])
    assert r1.usb_combo_changed is True
    after_first_fanin = fanin.read_text()

    restarts2: list[str] = []
    r2 = _auto(fanin, outputd, gadget=True, restarts=restarts2)
    assert r2.usb_combo_changed is False
    assert fanin.read_text() == after_first_fanin
    # The second pass writes nothing and bounces NO data-plane daemon. It DOES
    # re-run the lightweight camilla confirm (the shm_ring CONFIRM-path self-heal),
    # which is by design — that never glitches audio. Assert only that no fan-in /
    # outputd restart fired.
    assert "fanin" not in restarts2
    assert "outputd" not in restarts2


def test_auto_combo_only_change_forces_fanin_restart(tmp_path, monkeypatch):
    """A combo-only change on a box already coherent on the ring takes the
    no-bounce path, so the new combo would not be live until fan-in restarts.
    The auto pass issues that one restart — CamillaDSP-coordinated, because the
    ring is live and a bare fan-in restart is what SIGKILLs camilla."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.combo_armed is True
    # The no-bounce path did not restart fan-in, so the combo forced one.
    assert r.restarted_fanin_for_combo is True
    assert restarts.count("fanin") == 1
    assert restarts.index("camilla_stop") < restarts.index("fanin")
    assert restarts.index("fanin") < restarts.index("camilla_start")


def _armed_shm_ring_outputd() -> str:
    """The outputd.env an ALREADY-armed shm_ring box carries. With this present a
    subsequent reconcile sees NO outputd move, so the coupling stays put and the
    reconcile takes the lightweight CONFIRM path (not _converge_ring) — the shape that
    makes the combo force a bare fan-in restart the coordination must wrap."""
    return cr._apply_actions("", cr._outputd_actions(""))[0]


def test_auto_combo_change_on_ring_pauses_camilla_around_fanin_restart(
    tmp_path, monkeypatch
):
    """RTTIME-SIGKILL fix — the load-bearing sequence. On a LIVE shm_ring box a
    combo-only change takes the confirm path (no arm bounce) and the combo forces a
    fan-in restart; that restart MUST pause CamillaDSP first and resume it after, so
    the ioplug capture reader can't busy-spin the SCHED_FIFO daemon into a SIGKILL."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    # Already shm_ring (the live-ring coupling) + standby already 1, so the ONLY
    # change is the combo fan-in keys -> confirm path -> combo-forced fan-in restart.
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.restarted_fanin_for_combo is True
    assert r.ok is True
    # The confirm path did NOT reconcile-bounce fan-in; the ONE fan-in restart is the
    # combo's, and it is wrapped: camilla stopped BEFORE, started AFTER.
    assert restarts.count("fanin") == 1
    assert "camilla_stop" in restarts and "camilla_start" in restarts
    assert restarts.index("camilla_stop") < restarts.index("fanin")
    assert restarts.index("fanin") < restarts.index("camilla_start")


def test_auto_ring_combo_camilla_stop_failure_aborts_fanin_restart(tmp_path, monkeypatch):
    """Failure honesty: if camilla can't be paused on a live ring, the combo fan-in
    restart is ABORTED (restarting fan-in with camilla live is what SIGKILLs it),
    surfaced ok=False, and camilla is started back — never left stopped-forever."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, restarts=restarts,
        camilla_stop_ok=False,
    )
    assert r.ok is False
    assert r.restarted_fanin_for_combo is False
    assert "fanin" not in restarts  # fan-in restart was aborted
    assert "camilla_stop" in restarts and "camilla_start" in restarts  # start-back tried
    assert "aborted fan-in restart" in (r.detail or "")


def test_auto_ring_combo_fanin_restart_failure_still_resumes_camilla(tmp_path, monkeypatch):
    """Failure honesty: if the combo fan-in restart fails AFTER camilla was stopped,
    camilla is STILL resumed (start called) — never left stopped-forever — and the
    failure is surfaced ok=False."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_geometry_heals(monkeypatch)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, restarts=restarts,
        fanin_ok=False,
    )
    assert r.ok is False
    assert r.restarted_fanin_for_combo is False
    assert restarts.index("camilla_stop") < restarts.index("fanin")
    assert restarts.index("fanin") < restarts.index("camilla_start")


# --------------------------------------------------------------------------
# F4 — the auto topology gate fails CLOSED on an unreadable topology
# --------------------------------------------------------------------------


def test_ring_topology_strict_fails_closed_on_unreadable(monkeypatch):
    """F4: the strict topology gate (auto path) resolves NOT-eligible when the
    topology cannot be read, where the human-arm gate fails open."""
    from jasper.output_topology import OutputTopologyError

    def boom():
        raise OutputTopologyError("topology file corrupt")

    monkeypatch.setattr(cr, "load_output_topology_strict", boom, raising=False)
    import jasper.output_topology as ot

    monkeypatch.setattr(ot, "load_output_topology_strict", boom)

    open_ok, open_detail = cr.ring_topology_ready()  # human arm: fail-open
    assert open_ok is True
    assert "deferring to outputd" in open_detail

    strict_ok, strict_detail = cr.ring_topology_ready_strict()  # auto: fail-closed
    assert strict_ok is False
    assert "fail-closed" in strict_detail


# --------------------------------------------------------------------------
# F6 — a stale JASPER_FANIN_RING_SLOTS self-heals on every auto pass
# --------------------------------------------------------------------------


def test_auto_stale_ring_slots_self_heals_and_keeps_ring(tmp_path, monkeypatch):
    """A box carrying a stale JASPER_FANIN_RING_SLOTS=8 line self-heals: the
    pass writes the conf.d's coherent value so fan-in creates Ring A with the
    geometry the ioplug attaches against, instead of leaving the box sheared.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
        "JASPER_FANIN_RING_SLOTS=8\n"
    )
    outputd.write_text(_armed_outputd_env())
    _persist_ring_eligible_topology(tmp_path, monkeypatch)

    # Uses the REAL slot heal so the wiring is exercised end to end; what is
    # stubbed is the /dev/shm sweep beside it and the conf.d the heal reads.
    monkeypatch.setattr(
        cr, "_delete_stale_ring_files", lambda reason, fanin_text="": False
    )
    import jasper.ring_assets as ra

    # The heal reads the conf.d's declared wire before it writes the slot count;
    # point it at the SHIPPED file rather than the dev host's /etc.
    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    # conf.d Ring-A n_slots = 2 (the pinned default); the on-disk `=8` disagrees.
    monkeypatch.setattr(ra, "ring_conf_n_slots", lambda pcm, conf_d=None: 2)

    restarts: list[str] = []
    _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert read_value(fanin.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert read_persisted_coupling(fanin) == COUPLING_SHM_RING


def test_auto_stale_base_ring_slots_self_heals_and_keeps_ring(tmp_path, monkeypatch):
    """F6 through the real systemd env chain.

    A stale ``JASPER_FANIN_RING_SLOTS=8`` in /etc/jasper/jasper.env is still the
    effective fan-in value when fanin.env has no later override. The auto pass
    must write the coherent fanin.env override.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    jasper_env = tmp_path / "jasper.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n", encoding="utf-8")
    outputd.write_text(_armed_outputd_env(), encoding="utf-8")
    jasper_env.write_text("JASPER_FANIN_RING_SLOTS=8\n", encoding="utf-8")
    monkeypatch.setattr(cr, "JASPER_ENV_PATH", str(jasper_env))
    _persist_ring_eligible_topology(tmp_path, monkeypatch)

    monkeypatch.setattr(
        cr, "_delete_stale_ring_files", lambda reason, fanin_text="": False
    )
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(ra, "ring_conf_n_slots", lambda pcm, conf_d=None: 2)

    restarts: list[str] = []
    _auto(fanin, outputd, gadget=False, restarts=restarts)

    assert read_value(fanin.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert read_persisted_coupling(fanin) == COUPLING_SHM_RING


# --------------------------------------------------------------------------
# Fresh-install low-latency reproduction — pins the measurement doc's claim
# --------------------------------------------------------------------------
#
# The measurement doc §2 ("this is what a fresh install
# ships") asserts that EVERY low-latency USB value is either a shipped code
# default or armed automatically by the coupling auto-pass on an eligible gadget
# box. That is a load-bearing promise (the ~55.5 ms measured number only holds if
# a fresh flash actually reproduces the measured config with no operator action).
# These tests PIN that promise, so a silent drift in any of the named values
# reddens here and the doc's claim is caught rather than becoming stale prose.
#
# Two halves, matching the doc's two §2 tables:
#   1. the host-clock combo the auto-pass ARMS on an eligible gadget box, and
#   2. the ring-geometry CODE DEFAULTS the doc's table names.
#
# ``ring_slots default == 2`` is pinned to config.rs's ``env_u32(…, 2)`` source
# text in tests/test_fanin_coupling_rust_contract.py
# (test_shm_ring_env_var_names_and_defaults_agree); here we only reference the
# Python constant that pin ties the Rust default to, so we do not duplicate the
# source-text read.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"


def test_fresh_install_auto_arms_exactly_the_documented_combo_block(
    tmp_path, monkeypatch
):
    """§2 combo table: on a gadget box with usbsink intent enabled the auto-pass
    writes EXACTLY this block into fanin.env — the three combo flags ``enabled``
    AND coupling ``shm_ring``. If the auto-pass ever stopped arming one of these,
    a fresh install would silently ship a slower config than the doc claims.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _persist_ring_eligible_topology(tmp_path, monkeypatch)
    _stub_ring_geometry_heals(monkeypatch)

    r = _auto(fanin, outputd, gadget=True, restarts=[])

    assert r.combo_armed is True
    text = fanin.read_text()
    # EXACTLY the documented combo env block (measurement doc §2 host-clock table).
    documented_combo = {
        ca.USB_DIRECT_ENV_VAR: "enabled",
        ca.HOST_CLOCK_ENV_VAR: "enabled",
        ca.CUSHION_DECAY_ENV_VAR: "enabled",
        COUPLING_ENV_VAR: COUPLING_SHM_RING,
    }
    for key, value in documented_combo.items():
        assert read_value(text, key) == value, (
            f"fresh-install auto-pass must write {key}={value} on an eligible "
            "gadget box (measurement doc §2); it did not"
        )


def test_fresh_install_ring_geometry_defaults_match_the_doc_table():
    """§2 ring-geometry table: the Camilla ring-emit geometry the doc names —
    chunksize 128 / target_level 128 / queuelimit 1 / rate_adjust off — and the
    2-slot Ring A default. These are shipped CODE defaults (no auto-pass needed);
    a fresh install reproduces them because they are the constant values. Pinning
    the literals here catches a silent drift the doc could not.
    """
    # The doc's table values are these constants; assert the literals so a drift
    # in the constant itself (not just its usage) reddens.
    assert RING_CAMILLA_CHUNKSIZE == 128
    assert RING_CAMILLA_TARGET_LEVEL == 128
    assert RING_CAMILLA_QUEUELIMIT == 1
    assert RING_CAMILLA_ENABLE_RATE_ADJUST is False
    # ring_slots default == 2 (config.rs env_u32(…, 2) is pinned to this constant
    # in test_fanin_coupling_rust_contract.py; referenced, not re-read here).
    assert DEFAULT_FANIN_RING_SLOTS == 2


def test_fresh_install_ring_geometry_emits_the_doc_table_values():
    """The same §2 ring-geometry values as they actually land in the emitted
    CamillaDSP flat config (``emit_flat_outputd_cutover_config`` — the config the
    statefile seeder re-seeds). Pins the values end-to-end through the
    emitter, not just the constants, so a wiring change that dropped one can't slip
    past. Hardware-free: a pure YAML-text emit, no CamillaDSP process.
    """
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    text = emit_flat_outputd_cutover_config()
    assert "chunksize: 128" in text
    assert "target_level: 128" in text
    assert "queuelimit: 1" in text
    assert "enable_rate_adjust: false" in text
    # Both ring ends are the SHM-ring devices (the end-to-end shm_ring topology).
    assert 'device: "jts_ring_capture"' in text
    assert 'device: "jts_ring_playback"' in text


def test_fresh_install_cushion_decay_floor_default_is_576():
    """§2 host-clock table: JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES ships
    at the hardware-validated 576 floor (config.rs DEFAULT_CUSHION_DECAY_FLOOR_FRAMES).
    The Rust behavioural test (cushion_decay_floor_defaults_to_validated_floor)
    asserts the config equals the CONSTANT but is tautological on the constant's
    value; this source-text pin catches the constant itself silently drifting off
    the 576 the doc's table names. Hardware-free (the crate does not build on
    macOS; the CI Linux rust job builds it).
    """
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    text = _FANIN_CONFIG_RS.read_text(encoding="utf-8")
    assert "pub const DEFAULT_CUSHION_DECAY_FLOOR_FRAMES: u32 = 576;" in text, (
        "config.rs DEFAULT_CUSHION_DECAY_FLOOR_FRAMES must stay 576 — the "
        "hardware-validated floor the measurement doc §2 table ships"
    )

