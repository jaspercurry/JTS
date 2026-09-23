# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Source intent client and completion receipt tests."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from jasper import source_intent
from jasper.control.restart_broker import START_ONLY_UNITS
from jasper.music_sources import Source
from jasper.local_sources import reconcile as source_reconcile
from tests._log_events import event_fields


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "source_intent.env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_target_status(
    status_path: str,
    env_path: str,
    source: Source,
    desired: str,
    *,
    result: str = "ok",
    effective: str = "off",
    reason: str = "",
    completed_monotonic_ns: int | None = None,
    fingerprint: str | None = None,
    siblings: dict[str, dict[str, str]] | None = None,
) -> None:
    text = Path(env_path).read_text(encoding="utf-8")
    source_reconcile._default_write_status(
        status_path,
        {
            "completed_monotonic_ns": (
                time.monotonic_ns()
                if completed_monotonic_ns is None
                else completed_monotonic_ns
            ),
            "intent_fingerprint": (
                source_intent.intent_fingerprint(text)
                if fingerprint is None
                else fingerprint
            ),
            "sources": {
                source.value: {
                    "desired": desired,
                    "effective": effective,
                    "result": result,
                    "reason": reason,
                },
                **(siblings or {}),
            },
        },
    )


def _key(source: Source) -> str:
    return source_intent.intent_env_key(source)


def _problem_events(env_path: str) -> list[tuple[str, Source | None]]:
    _, problems = source_intent.parse_source_intents(
        Path(env_path).read_text(encoding="utf-8"),
    )
    return [(problem.event, problem.source) for problem in problems]


def test_allowlist_and_legacy_keys_are_registry_derived():
    assert set(source_intent.source_intent_sources()) == {
        Source.AIRPLAY,
        Source.SPOTIFY,
        Source.BLUETOOTH,
        Source.USBSINK,
    }
    assert _key(Source.AIRPLAY) == "JASPER_SOURCE_INTENT_SHAIRPORT_SYNC_SERVICE"
    assert _key(Source.SPOTIFY) == "JASPER_SOURCE_INTENT_LIBRESPOT_SERVICE"
    assert _key(Source.USBSINK) == "JASPER_SOURCE_INTENT_JASPER_USBSINK_SERVICE"
    assert _key(Source.BLUETOOTH) == "JASPER_BLUETOOTH_SOURCE_INTENT"
    # Existing unit-string callers remain byte-for-byte compatible.
    assert source_intent.intent_env_key("shairport-sync.service") == _key(
        Source.AIRPLAY
    )


def test_read_source_intents_fills_defaults_and_applies_overrides(tmp_path):
    missing = tmp_path / "missing.env"
    assert source_intent.read_source_intents(str(missing)) == {
        Source.AIRPLAY: True,
        Source.SPOTIFY: True,
        Source.BLUETOOTH: True,
        Source.USBSINK: False,
    }
    env = _write(
        tmp_path,
        f"{_key(Source.AIRPLAY)}=disabled\n{_key(Source.BLUETOOTH)}=disabled\n",
    )
    intents = source_intent.read_source_intents(env)
    assert intents[Source.AIRPLAY] is False
    assert intents[Source.BLUETOOTH] is False
    assert intents[Source.SPOTIFY] is True
    assert source_intent.source_intent_enabled(Source.USBSINK, env) is False


def test_request_source_intent_writes_fixed_key_then_kicks(tmp_path):
    calls = []
    status_path = str(tmp_path / "status.json")

    def writer(path, updates):
        calls.append(("write", path, dict(updates)))
        source_intent._default_write_intent(path, updates)

    def kicker():
        calls.append(("kick",))
        _write_target_status(
            status_path,
            path,
            Source.BLUETOOTH,
            "disabled",
        )
        return {"ok": True}

    path = str(tmp_path / "intent.env")
    source_intent.request_source_intent(
        Source.BLUETOOTH,
        False,
        env_path=path,
        status_path=status_path,
        writer=writer,
        kicker=kicker,
    )
    assert calls == [
        ("write", path, {"JASPER_BLUETOOTH_SOURCE_INTENT": "disabled"}),
        ("kick",),
    ]


def test_default_writer_publishes_env_and_inner_lock_for_both_web_owners(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        source_intent,
        "locked_update_env_file",
        lambda path, updates, **kwargs: calls.append((path, updates, kwargs)),
    )

    source_intent._default_write_intent("/var/lib/jasper/source_intent.env", {"K": "V"})

    assert calls == [
        (
            "/var/lib/jasper/source_intent.env",
            {"K": "V"},
            {
                "mode": 0o660,
                "max_bytes": source_intent._MAX_INTENT_BYTES,
                "lock_timeout_sec": source_intent._REQUEST_LOCK_TIMEOUT_SEC,
                "owner": source_intent._INTENT_ENV_OWNER,
            },
        )
    ]


def test_web_broker_wait_outlasts_complete_source_reconcile(monkeypatch):
    calls = []
    monkeypatch.setattr(
        source_intent,
        "manage_units",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    assert source_intent.kick_source_reconcile() == {"ok": True}
    assert calls == [
        (
            (source_intent.RECONCILE_UNIT,),
            {
                "verb": "start",
                "reason": "source enable/disable",
                "no_block": False,
                "timeout": source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS,
            },
        )
    ]


def test_request_source_intent_keeps_written_intent_when_kick_fails(tmp_path, caplog):
    written = []
    env_path = str(tmp_path / "intent.env")

    def writer(path, updates):
        written.append(dict(updates))
        source_intent._default_write_intent(path, updates)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=str(tmp_path / "missing-status.json"),
            writer=writer,
            kicker=lambda: {"ok": False, "error": "coordinator failed"},
        )
    assert written == [{"JASPER_BLUETOOTH_SOURCE_INTENT": "disabled"}]
    fields = event_fields(caplog, "source.intent_apply_failed")
    assert (fields["source"], fields["desired"]) == ("bluetooth", "disabled")


def test_request_succeeds_when_target_did_but_sibling_failed(tmp_path, caplog):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
        )
        return {"ok": False, "error": "spotify failed"}

    with caplog.at_level("WARNING"):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 1
    fields = event_fields(caplog, "source.intent_sibling_failure")
    assert (
        fields["source"],
        fields["desired"],
        fields["failed_siblings"],
        fields["aggregate_error"],
    ) == ("bluetooth", "disabled", "null", "spotify failed")


def test_sibling_failure_names_the_sibling_that_actually_failed(tmp_path, caplog):
    """#2175 — the warning must name the FAILING source, not just the pressed one.

    A Bluetooth toggle on a box whose USB gadget cannot compose reconciled
    ``result=ok`` for Bluetooth while the pass exited non-zero for USB. The old
    line carried only ``source=bluetooth`` and an opaque ``aggregate_error=rc=1``,
    and was read as "the Bluetooth toggle failed" — the misreport this pins shut.
    """

    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    usb_reason = "USB On transition failed: gadget restart timed out"

    def kicker():
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "enabled",
            effective="on",
            siblings={
                "airplay": {
                    "desired": "enabled",
                    "effective": "on",
                    "result": "ok",
                    "reason": "",
                },
                "usbsink": {
                    "desired": "enabled",
                    "effective": "degraded",
                    "result": "failed",
                    "reason": usb_reason,
                },
            },
        )
        return {"ok": False, "rc": 1}

    with caplog.at_level("WARNING"):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            True,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    # The source that converged is never listed as a failure, and the requested
    # source is never listed as its own sibling.
    fields = event_fields(caplog, "source.intent_sibling_failure")
    assert fields["failed_siblings"] == f"usbsink: {usb_reason}"


def test_failed_siblings_never_names_the_requested_source():
    """The field means "somebody ELSE failed". A status document that somehow
    reports the requested source as failed is the caller's own outcome (already
    raised by the target check) and is not repeated here as a sibling."""

    named = source_intent._failed_siblings(
        {
            "bluetooth": {
                "desired": "enabled",
                "effective": "degraded",
                "result": "failed",
                "reason": "rfkill failed",
            },
            "spotify": {
                "desired": "enabled",
                "effective": "degraded",
                "result": "failed",
                "reason": "librespot down",
            },
        },
        Source.BLUETOOTH,
    )

    assert named == "spotify: librespot down"


def test_failed_siblings_caps_each_reason_so_no_sibling_name_is_crowded_out():
    """One verbose sibling must not truncate the NEXT sibling's name away.

    The field's whole job is naming WHO failed, so the per-entry reason cap is
    what keeps the last sibling's name inside the overall bound — capping only
    the joined string would spend the budget on one reason."""

    named = source_intent._failed_siblings(
        {
            "airplay": {"result": "failed", "reason": "x" * 500},
            "spotify": {"result": "failed", "reason": "y" * 500},
            "usbsink": {"result": "failed", "reason": "z" * 500},
        },
        Source.BLUETOOTH,
    )

    for name in ("airplay", "spotify", "usbsink"):
        assert f"{name}: " in named
    assert "x" * source_intent._MAX_SIBLING_REASON_CHARS in named
    assert "x" * (source_intent._MAX_SIBLING_REASON_CHARS + 1) not in named
    assert len(named) <= 300


def test_sibling_failure_reports_null_when_no_source_can_be_named(tmp_path, caplog):
    """An aggregate failure with no failed source (an unpublishable status, a
    rejected intent key, a broker-level error) reports ``failed_siblings=null``
    rather than inventing one. Untrusted keys in the status document are never
    named — only declared sources are."""

    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    undeclared_source = "; rm -rf /"

    def kicker():
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "enabled",
            effective="on",
            siblings={
                undeclared_source: {
                    "desired": "enabled",
                    "effective": "degraded",
                    "result": "failed",
                    "reason": "not a declared source",
                },
            },
        )
        return {"ok": False, "error": "restart broker unavailable"}

    with caplog.at_level("WARNING"):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            True,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    fields = event_fields(caplog, "source.intent_sibling_failure")
    assert fields["failed_siblings"] == "null"
    assert not any(undeclared_source in value for value in fields.values())


def test_request_fails_when_fresh_target_outcome_failed(tmp_path):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
            result="failed",
            effective="degraded",
            reason="rfkill failed",
        )
        return {"ok": False, "error": "aggregate failed"}

    # Prose pin: the raise is a bare RuntimeError, and the property under test
    # is that both halves of the detail (aggregate + target outcome) reach the
    # caller, which nothing structured carries.
    with pytest.raises(
        RuntimeError,
        match="aggregate=aggregate failed.*target effective=degraded failed: rfkill failed",
    ):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 1


def test_request_retries_once_after_stale_join_then_accepts_fresh_pass(tmp_path):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
            completed_monotonic_ns=0 if kicks == 1 else None,
        )
        return {"ok": True}

    source_intent.request_source_intent(
        Source.BLUETOOTH,
        False,
        env_path=env_path,
        status_path=status_path,
        kicker=kicker,
    )

    assert kicks == 2


@pytest.mark.parametrize(
    ("status_shape", "detail"),
    [
        ("stale", "completion status is stale"),
        ("malformed", "completion status is unreadable"),
        ("wrong_fingerprint", "completion status intent does not match"),
    ],
)
def test_request_refuses_untrusted_completion_status(tmp_path, status_shape, detail):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        if status_shape == "malformed":
            Path(status_path).write_text("not-json", encoding="utf-8")
        else:
            _write_target_status(
                status_path,
                env_path,
                Source.BLUETOOTH,
                "disabled",
                completed_monotonic_ns=(0 if status_shape == "stale" else None),
                fingerprint=("0" * 64 if status_shape == "wrong_fingerprint" else None),
            )
        return {"ok": True}

    # Prose pin: all three refusals raise the same bare RuntimeError, so the
    # detail sentence is the only thing that tells them apart.
    with pytest.raises(RuntimeError, match=detail):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 2


def test_request_source_intent_serializes_write_and_apply_across_callers(tmp_path):
    """A second writer cannot join an already-running apply with newer state."""
    first_apply_entered = threading.Event()
    release_first_apply = threading.Event()
    second_write_seen = threading.Event()
    calls: list[tuple[str, str]] = []
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")

    def request(name: str, source: Source) -> None:
        def writer(path, updates):
            calls.append((name, "write"))
            source_intent._default_write_intent(path, updates)
            if name == "second":
                second_write_seen.set()

        def kicker():
            calls.append((name, "apply"))
            if name == "first":
                first_apply_entered.set()
                assert release_first_apply.wait(timeout=2)
            _write_target_status(
                status_path,
                env_path,
                source,
                "disabled",
            )
            return {"ok": True}

        source_intent.request_source_intent(
            source,
            False,
            env_path=env_path,
            status_path=status_path,
            writer=writer,
            kicker=kicker,
        )

    first = threading.Thread(target=request, args=("first", Source.AIRPLAY))
    second = threading.Thread(target=request, args=("second", Source.SPOTIFY))
    first.start()
    assert first_apply_entered.wait(timeout=2)
    second.start()
    assert not second_write_seen.wait(timeout=0.1)
    release_first_apply.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [
        ("first", "write"),
        ("first", "apply"),
        ("second", "write"),
        ("second", "apply"),
    ]


def test_production_status_reader_rejects_writable_parent(tmp_path, monkeypatch):
    env_path = _write(tmp_path, f"{_key(Source.BLUETOOTH)}=disabled\n")
    status_path = str(tmp_path / "status.json")
    _write_target_status(
        status_path,
        env_path,
        Source.BLUETOOTH,
        "disabled",
    )
    monkeypatch.setattr(source_intent, "SOURCE_STATUS_PATH", status_path)
    real_lstat = os.lstat

    def unsafe_parent(path):
        observed = real_lstat(path)
        mode = observed.st_mode
        if os.fspath(path) == os.fspath(tmp_path):
            mode |= 0o022
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(source_intent.os, "lstat", unsafe_parent)
    result = source_intent._read_target_status(
        path=status_path,
        source=Source.BLUETOOTH,
        desired="disabled",
        intent_fingerprint=source_intent.intent_fingerprint(
            Path(env_path).read_text(encoding="utf-8")
        ),
        not_before_monotonic_ns=0,
    )

    assert result.exact is False
    assert result.detail == "completion status ownership is unsafe"


def test_public_reader_fails_strictly_on_bad_or_unknown_keys(tmp_path):
    bad_value = _write(tmp_path, f"{_key(Source.AIRPLAY)}=maybe\n")
    with pytest.raises(RuntimeError):
        source_intent.read_source_intents(bad_value)
    assert _problem_events(bad_value) == [
        ("source_intent.bad_value", Source.AIRPLAY),
    ]
    unknown = _write(tmp_path, "JASPER_SOURCE_INTENT_SSHD_SERVICE=enabled\n")
    with pytest.raises(RuntimeError):
        source_intent.read_source_intents(unknown)
    assert _problem_events(unknown) == [("source_intent.rejected_unit", None)]


def test_per_source_reader_fails_only_the_affected_source(tmp_path):
    env = _write(
        tmp_path,
        f"{_key(Source.AIRPLAY)}=maybe\n"
        "JASPER_SOURCE_INTENT_SSHD_SERVICE=enabled\n"
        f"{_key(Source.SPOTIFY)}=enabled\n",
    )

    with pytest.raises(RuntimeError):
        source_intent.source_intent_enabled(Source.AIRPLAY, env)
    assert _problem_events(env) == [
        ("source_intent.bad_value", Source.AIRPLAY),
        ("source_intent.rejected_unit", None),
    ]
    assert source_intent.source_intent_enabled(Source.SPOTIFY, env) is True


def test_reconcile_unit_matches_broker_start_only_grant():
    assert source_intent.RECONCILE_UNIT in START_ONLY_UNITS
