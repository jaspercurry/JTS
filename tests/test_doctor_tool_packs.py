"""jasper-doctor check_tool_packs — tool-pack registration health.

Tool registration is fault-isolated per pack (jasper.tools.packs): a pack
whose build raises contributes no tools but the daemon starts fine. Before
this check that was observable ONLY in the journal
(event=tool_pack.build_failed). check_tool_packs cross-checks the static
registry (TOOL_PACKS) against what jasper-voice actually registered
(/state.voice.tool_packs) so a silently-missing tool family surfaces in
the doctor + /system dashboard.

The assessor is pure (the runtime list is passed in), so the warn/fail/ok
branches are tested without an HTTP round-trip — mirrors the wake-leg
assessor's test shape.
"""
from __future__ import annotations

import pytest

from jasper.cli.doctor import (
    _assess_tool_packs,
    _voice_tool_packs_runtime,
    check_tool_packs,
)
from jasper.cli.doctor import voice as doctor_voice
from jasper.tools.packs import TOOL_PACKS

EXPECTED = [p.name for p in TOOL_PACKS]


def _runtime(names, *, failed=(), skipped=()):
    """Build a /state.voice.tool_packs-shaped runtime list."""
    out = []
    for n in names:
        if n in failed:
            out.append({"name": n, "status": "failed", "tool_count": 0,
                        "error": "ImportError('boom')"})
        elif n in skipped:
            out.append({"name": n, "status": "skipped", "tool_count": 0,
                        "error": None})
        else:
            out.append({"name": n, "status": "registered", "tool_count": 2,
                        "error": None})
    return out


# ----------------------------------------------------------- pure assessor


def test_runtime_none_reports_registry_only_ok():
    """Control unreachable / older daemon → can't see runtime, so report
    the registry alone without alarming."""
    r = _assess_tool_packs(EXPECTED, None)
    assert r.status == "ok"
    assert str(len(EXPECTED)) in r.detail
    assert "unavailable" in r.detail


def test_all_registered_is_ok():
    r = _assess_tool_packs(EXPECTED, _runtime(EXPECTED))
    assert r.status == "ok"
    assert "0 failed" in r.detail


def test_failed_pack_fails_with_name_and_journal_hint():
    r = _assess_tool_packs(EXPECTED, _runtime(EXPECTED, failed={EXPECTED[2]}))
    assert r.status == "fail"
    assert EXPECTED[2] in r.detail
    # Actionable: points at the structured journal line.
    assert "event=tool_pack.build_failed" in r.detail


def test_failed_takes_priority_over_missing():
    """A failed pack is the alarm even if other packs are also absent —
    fail beats warn."""
    rt = _runtime(EXPECTED, failed={EXPECTED[0]})
    rt = rt[:-1]  # also drop one (would otherwise be "missing")
    assert _assess_tool_packs(EXPECTED, rt).status == "fail"


def test_missing_pack_warns():
    """A registry pack absent from the runtime report (daemon predates it)
    is a warn, not a fail."""
    rt = _runtime(EXPECTED)[:-1]  # drop the last pack
    r = _assess_tool_packs(EXPECTED, rt)
    assert r.status == "warn"
    assert EXPECTED[-1] in r.detail


def test_skipped_packs_reported_in_ok_detail():
    gated = {EXPECTED[-1]}
    r = _assess_tool_packs(EXPECTED, _runtime(EXPECTED, skipped=gated))
    assert r.status == "ok"
    assert "gated off" in r.detail
    assert EXPECTED[-1] in r.detail


# ----------------------------------------------------------- runtime reader


def test_runtime_reader_returns_none_when_control_unreachable(monkeypatch):
    import jasper.control.client as control

    def _raise(*a, **k):
        raise control.ControlError("connection refused")

    monkeypatch.setattr(control, "get_state", _raise)
    assert _voice_tool_packs_runtime() is None


def test_runtime_reader_returns_none_when_field_absent(monkeypatch):
    import jasper.control.client as control
    # voice present but no tool_packs key (older daemon).
    monkeypatch.setattr(control, "get_state", lambda *a, **k: {"voice": {}})
    assert _voice_tool_packs_runtime() is None


def test_runtime_reader_parses_tool_packs(monkeypatch):
    import jasper.control.client as control
    payload = {"voice": {"tool_packs": _runtime(["audio", "timer"])}}
    monkeypatch.setattr(control, "get_state", lambda *a, **k: payload)
    got = _voice_tool_packs_runtime()
    assert [p["name"] for p in got] == ["audio", "timer"]


# ------------------------------------------------------------ wired check


def test_check_tool_packs_uses_static_registry_when_runtime_none(monkeypatch):
    """The decorated check reads the static registry for the expected set
    and is fail-soft when runtime is unavailable."""
    monkeypatch.setattr(
        doctor_voice, "_voice_tool_packs_runtime", lambda: None,
    )
    r = check_tool_packs()
    assert r.status == "ok"
    # Every shipped pack name appears in the registry-only detail.
    for name in EXPECTED:
        assert name in r.detail


def test_check_tool_packs_fails_on_runtime_failure(monkeypatch):
    monkeypatch.setattr(
        doctor_voice, "_voice_tool_packs_runtime",
        lambda: _runtime(EXPECTED, failed={EXPECTED[1]}),
    )
    assert check_tool_packs().status == "fail"


@pytest.mark.parametrize("order", [44.5])
def test_check_is_registered_at_reserved_order(order):
    from jasper.cli.doctor import registered_checks
    by_order = {c.order: c for c in registered_checks()}
    assert order in by_order
    assert by_order[order].func is check_tool_packs
    assert by_order[order].group == "voice"
