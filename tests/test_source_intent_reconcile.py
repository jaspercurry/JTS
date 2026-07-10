# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the /sources enable/disable root helper.

``jasper.source_intent`` is the fixed root oneshot (WS1 Phase 3b) that persists
the /sources on/off choice the non-root wizard can't (enable/disable is
manage-unit-files, which the non-root broker deliberately can't run). These pin
the security-critical invariants:

* the unit allowlist is DERIVED from the source-lifecycle registry (can't drift
  from what the wizard toggles) and is the ONLY set of units the helper touches;
* an intent file carrying a non-allowlisted / malformed key is rejected LOUD
  (logged + non-zero exit) and never turned into a ``systemctl enable/disable``;
* the oneshot's exit code is 0 iff every applied unit succeeded, so a failed
  apply propagates a visible error to the wizard instead of a silent success.
"""
from __future__ import annotations

from jasper import source_intent
from jasper.local_sources import local_source_lifecycles


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "source_intent.env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_allowlist_matches_registry_intent_units():
    # The security boundary is derived from the registry — the same source of
    # truth the wizard uses — so it can never drift from the toggled units.
    expected = {
        lc.intent_unit
        for lc in local_source_lifecycles()
        if lc.intent_unit is not None
    }
    assert set(source_intent.source_intent_units()) == expected
    # Bluetooth is runtime-only DBus power (no intent unit) → not in the allowlist.
    assert "bluetooth.service" not in source_intent.source_intent_units()
    assert "bluealsa.service" not in source_intent.source_intent_units()


def test_intent_env_key_is_deterministic_and_env_safe():
    for unit in source_intent.source_intent_units():
        key = source_intent.intent_env_key(unit)
        assert key.startswith("JASPER_SOURCE_INTENT_")
        # Valid env var name: uppercase alnum + underscore only, no `-`/`.`.
        assert all(c.isupper() or c.isdigit() or c == "_" for c in key)
        assert "-" not in key and "." not in key
    assert (
        source_intent.intent_env_key("jasper-usbsink.service")
        == "JASPER_SOURCE_INTENT_JASPER_USBSINK_SERVICE"
    )


def test_reconcile_enables_and_disables_allowlisted_units(tmp_path):
    calls = []
    airplay = "shairport-sync.service"
    spotify = "librespot.service"
    env = _write(
        tmp_path,
        f"{source_intent.intent_env_key(airplay)}=enabled\n"
        f"{source_intent.intent_env_key(spotify)}=disabled\n",
    )
    rc = source_intent.reconcile(
        env_path=env,
        runner=lambda unit, enabled: calls.append((unit, enabled)) or (0, ""),
    )
    assert rc == 0
    assert (airplay, True) in calls
    assert (spotify, False) in calls
    assert len(calls) == 2


def test_reconcile_absent_file_is_noop(tmp_path):
    calls = []
    rc = source_intent.reconcile(
        env_path=str(tmp_path / "missing.env"),
        runner=lambda unit, enabled: calls.append((unit, enabled)) or (0, ""),
    )
    assert rc == 0
    assert calls == []


def test_reconcile_rejects_non_allowlisted_unit_and_fails_loud(tmp_path, caplog):
    # A JASPER_SOURCE_INTENT_* key that maps to a unit NOT in the allowlist
    # (tampering / drift). It must be refused loudly and never enable/disabled.
    calls = []
    env = _write(
        tmp_path,
        "JASPER_SOURCE_INTENT_SSHD_SERVICE=disabled\n"
        f"{source_intent.intent_env_key('shairport-sync.service')}=enabled\n",
    )
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(
            env_path=env,
            runner=lambda unit, enabled: calls.append((unit, enabled)) or (0, ""),
        )
    # Non-zero because a key was rejected — the wizard sees the failure.
    assert rc == 1
    # sshd was NEVER passed to the systemctl runner; only the allowlisted unit ran.
    assert ("sshd.service", False) not in calls
    assert all(unit != "sshd.service" for unit, _ in calls)
    assert ("shairport-sync.service", True) in calls
    assert any(
        "event=source_intent.rejected_unit" in r.getMessage()
        for r in caplog.records
    )


def test_reconcile_ignores_unrelated_env_keys(tmp_path):
    # A plain env line with no JASPER_SOURCE_INTENT_ prefix is not our business
    # and must be silently ignored (no reject, no apply).
    calls = []
    env = _write(
        tmp_path,
        "SOME_OPERATOR_KEY=1\n"
        f"{source_intent.intent_env_key('librespot.service')}=enabled\n",
    )
    rc = source_intent.reconcile(
        env_path=env,
        runner=lambda unit, enabled: calls.append((unit, enabled)) or (0, ""),
    )
    assert rc == 0
    assert calls == [("librespot.service", True)]


def test_reconcile_bad_value_fails_loud(tmp_path, caplog):
    calls = []
    env = _write(
        tmp_path,
        f"{source_intent.intent_env_key('shairport-sync.service')}=maybe\n",
    )
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(
            env_path=env,
            runner=lambda unit, enabled: calls.append((unit, enabled)) or (0, ""),
        )
    assert rc == 1
    assert calls == []  # never applied an ambiguous value
    assert any(
        "event=source_intent.bad_value" in r.getMessage() for r in caplog.records
    )


def test_reconcile_nonzero_exit_when_systemctl_fails(tmp_path, caplog):
    env = _write(
        tmp_path,
        f"{source_intent.intent_env_key('shairport-sync.service')}=disabled\n",
    )
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(
            env_path=env,
            runner=lambda unit, enabled: (1, "Interactive authentication required"),
        )
    assert rc == 1
    assert any(
        "event=source_intent.apply_failed" in r.getMessage()
        for r in caplog.records
    )


def test_reconcile_unit_matches_broker_start_only_grant():
    # The unit name the wizard kicks must equal what the broker + polkit
    # allowlist as start-only (pinned separately); this ties them to the module.
    from jasper.control.restart_broker import START_ONLY_UNITS

    assert source_intent.RECONCILE_UNIT in START_ONLY_UNITS


def test_main_returns_reconcile_exit_code(tmp_path, monkeypatch):
    # The oneshot's exit code IS the success signal the broker relays. main()
    # must return reconcile()'s code so a failed apply fails the unit.
    monkeypatch.setattr(source_intent, "_run_systemctl", lambda unit, enabled: (0, ""))
    env = _write(
        tmp_path,
        f"{source_intent.intent_env_key('librespot.service')}=enabled\n",
    )
    assert source_intent.main(["--env-path", env]) == 0
