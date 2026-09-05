# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the privileged restart broker.

Pins the closed verb vocabulary, the unit allowlist, SO_PEERCRED peer-uid
auth, the request/response wire contract, and the root fallback — the
properties the user-drop PR relies on. No real systemctl runs: the broker's
``subprocess.run`` and the uid allowlist are patched so the suite is
hardware-free and side-effect-free.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from jasper.control import restart_broker

# The broker's peer-cred auth uses SO_PEERCRED (Linux-only — the broker runs on
# the Pi). On a macOS dev box the constant is absent, so the server round-trip
# tests are skipped there; CI on Linux is the source of truth. The pure-helper
# and client-fallback tests run everywhere.
requires_peercred = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"),
    reason="SO_PEERCRED is Linux-only (the restart broker runs on the Pi)",
)

_REPO = Path(__file__).resolve().parent.parent
_CONTROL_UNIT = _REPO / "deploy" / "systemd" / "jasper-control.service"


def test_control_unit_declares_broker_runtime_dir():
    """The broker binds /run/jasper-control/restart.sock; systemd must create
    that dir (RuntimeDirectory) owned by the unit's user, or the broker can't
    bind after the user drop."""
    text = _CONTROL_UNIT.read_text()
    assert "RuntimeDirectory=jasper-control" in text
    # The default socket path lives under that runtime dir.
    assert restart_broker.DEFAULT_SOCKET_PATH.startswith("/run/jasper-control/")


# --------------------------------------------------------------------------
# Pure helpers: verb vocabulary, unit normalization, allowlist.
# --------------------------------------------------------------------------


def test_verb_vocabulary_is_closed_and_complete():
    # The exact set the clients use. A new verb must be added deliberately.
    assert restart_broker.ALLOWED_VERBS == frozenset({
        "restart", "try-restart", "start", "stop",
        "enable", "enable-now", "disable-now", "reset-failed",
    })


@pytest.mark.parametrize("verb,no_block,expected", [
    ("restart", True, ["systemctl", "restart", "--no-block", "jasper-voice.service"]),
    ("restart", False, ["systemctl", "restart", "jasper-voice.service"]),
    ("try-restart", True, ["systemctl", "try-restart", "--no-block", "jasper-voice.service"]),
    ("start", True, ["systemctl", "start", "--no-block", "jasper-voice.service"]),
    ("stop", True, ["systemctl", "stop", "--no-block", "jasper-voice.service"]),
    # enable / reset-failed never take --no-block even when asked.
    ("enable", True, ["systemctl", "enable", "jasper-voice.service"]),
    ("enable-now", True, ["systemctl", "enable", "--now", "--no-block", "jasper-voice.service"]),
    ("disable-now", False, ["systemctl", "disable", "--now", "jasper-voice.service"]),
    ("reset-failed", True, ["systemctl", "reset-failed", "jasper-voice.service"]),
])
def test_build_argv(verb, no_block, expected):
    assert restart_broker._build_argv(
        verb, ["jasper-voice.service"], no_block=no_block,
    ) == expected


@pytest.mark.parametrize("raw,normalized", [
    ("jasper-voice", "jasper-voice.service"),
    ("jasper-voice.service", "jasper-voice.service"),
    ("shairport-sync", "shairport-sync.service"),
    ("jasper-web.socket", "jasper-web.socket"),
    (" librespot ", "librespot.service"),
])
def test_normalize_unit(raw, normalized):
    assert restart_broker._normalize_unit(raw) == normalized


def test_managed_units_excludes_tier_b_reconcilers():
    """Tier-B reconcilers stay root in Phase 3 and must NOT be brokerable — a
    non-root daemon must never be able to ask the broker to restart the
    self-healing units that recover Wi-Fi / output hardware / the DAC /
    the dongle."""
    tier_b = {
        "jasper-wifi-guardian.service",
        "jasper-wifi-recover.service",
        "jasper-wifi-scan-repair.service",
        "jasper-audio-hardware-reconcile.service",
        "jasper-dac-init.service",
        "jasper-headphone-monitor.service",
        "jasper-dongle-recover.service",
    }
    assert tier_b.isdisjoint(restart_broker.MANAGED_UNITS), (
        f"Tier-B units in MANAGED_UNITS: {tier_b & restart_broker.MANAGED_UNITS}"
    )


def test_start_only_units_are_not_general_managed_units():
    """Graph transitions may kick the fixed hardware reconcile helper, but only
    with `start`; it must not become generally restartable/stoppable through the
    broker."""
    assert restart_broker.START_ONLY_UNITS == frozenset({
        "jasper-audio-hardware-reconcile.service",
        "jasper-fanin-coupling-auto.service",
        "jasper-source-intent-reconcile.service",
        "jasper-wifi-scan-repair.service",
        "jasper-wiim-remote-ce.service",
        "jasper-xvf-firmware-update.service",
        "jasper-enhanced-aec-install.service",
        "jasper-aec-commission.service",
    })
    assert restart_broker.START_ONLY_UNITS.isdisjoint(restart_broker.MANAGED_UNITS)


def test_managed_units_cover_every_routed_client_unit():
    # Units the wizard / mux / correction / wake-corpus client sites send.
    must_contain = {
        "jasper-voice.service", "jasper-control.service", "jasper-web.service",
        "jasper-mux.service", "jasper-input.service",
        "shairport-sync.service", "nqptp.service", "librespot.service",
        "jasper-usbsink.service", "jasper-usbgadget.service",
        "jasper-usbmic-apply.service", "jasper-usbmic.service",
        "bluetooth.service", "bluealsa.service", "bluealsa-aplay.service",
        "bt-agent.service",
        "jasper-aec-bridge.service", "jasper-aec-init.service",
        "jasper-aec-reconcile.service", "jasper-grouping-reconcile.service",
        "jasper-grouping-reconcile-trailing.service",
        "jasper-camilla.service", "jasper-outputd.service",
    }
    assert must_contain <= restart_broker.MANAGED_UNITS


# --------------------------------------------------------------------------
# Server: a real UDS broker on a tmp socket, with subprocess + uid patched.
# --------------------------------------------------------------------------


@dataclass
class _FakeProc:
    returncode: int = 0
    stderr: str = ""


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """Start a real broker on a tmp socket. Yields (socket_path, calls)
    where `calls` records every systemctl argv the broker would run. The
    peer-uid allowlist is widened to include the test runner's uid so the
    in-process client is authorized; `subprocess.run` is faked so nothing
    real restarts."""
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("SO_PEERCRED is Linux-only (the restart broker runs on the Pi)")
    calls: list[list[str]] = []
    rc_holder = {"rc": 0, "stderr": ""}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _FakeProc(returncode=rc_holder["rc"], stderr=rc_holder["stderr"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {os.getuid(), 0})

    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    assert server is not None
    # serve_forever runs on a daemon thread started by start_broker.
    yield sock_path, calls, rc_holder
    server.shutdown()
    server.server_close()


# --- Connection resilience under load ---------------------------------------
#
# Every test in this file that starts a real _BrokerServer
# (ThreadingUnixStreamServer) and drives a real client socket round trip
# against it shares one exposure. Under FD/thread pressure from a loaded
# parallel run (the same resource-exhaustion class documented in
# test_wifi_guardian_script.py's module docstring — #1909's third flake),
# CPython's socketserver can fail to spin up the per-connection handler thread
# (`threading.Thread.start()` inside `ThreadingMixIn.process_request`); its
# caller (`_handle_request_noblock`) then unconditionally closes the
# already-accepted socket in a `finally` without ever reading the request.
#
# That reaches the client as one of TWO shapes, decided by whether the
# client's `sendall()` won the race against the close:
#
#   1. sendall lost   -> `BrokenPipeError` / `ConnectionResetError`, raw in the
#                        hand-rolled round trip below, or chained onto
#                        `BrokerUnavailable` by `request_restart`.
#   2. sendall won    -> the bytes sat in the socket buffer, the peer closed,
#                        and the following `recv()` returned b"" — surfacing
#                        as a CAUSELESS `BrokerUnavailable("empty broker
#                        response")`.
#
# Both are retried. Shape 2 is easy to mistake for a protocol-level answer
# because it carries no chained exception, and covering only shape 1 leaves
# the flake alive under a different error string. It is rare but real:
# inducing the race 480x under 8-wide concurrent load on a Pi 5 produced
# shape 2 three times, the rest split between the two shape-1 exceptions.
#
# Observed once in CI (#1909) in test_unauthorized_peer_uid_rejected: the
# run's only failure among 16,421 items, alongside 14 contemporaneous
# `_TransientSpawnRetryWarning`s from test_wifi_guardian_script.py in the same
# worker pool — the same class, just manifesting as a stdlib thread-spawn
# failure instead of a subprocess fork() failure.
#
# Every round trip here retries, not only the one observed failing. Nothing
# about that test is more exposed than its siblings: they reach the identical
# stdlib code path through the identical client function, so the observed
# failure is a sample from the group rather than a property of one member.
# This mirrors test_wifi_guardian_script.py, where the sibling `_run_guardian`
# helper wraps 20 of that file's 22 tests — every one that spawns a
# subprocess; the two that don't only read files — and whose comment likewise
# records the whole group failing together under load.
#
# This is a test-harness resource hiccup, not a broker bug: the broker's
# accept/dispatch/auth code never touches the socket until a handler thread
# exists, so retrying the CLIENT's connection attempt changes nothing about
# what gets asserted afterward. In particular the handler never ran, so no
# systemctl call was recorded and a retried round trip leaves the exact
# `calls == [...]` assertions intact. Only the two shapes above are retried; a
# genuinely absent socket, a real broker answer (including a real
# "unauthorized" rejection), a response the broker actually wrote but that
# won't parse, or a hang all still fail immediately — mirrors `_run_guardian`'s
# discipline (bounded attempts, warn per retry, never mask a real failure).
_BROKER_CONNECT_RETRIES = 5

# request_restart's exact message for shape 2 — the peer accepted, then closed
# without writing a byte. Matched by string because there is no exception to
# chain; pinned against the product by
# test_peer_close_without_answer_uses_the_sentinel_the_retry_matches, so a
# reword can't silently narrow the retry back to shape 1.
_BROKER_CLOSED_WITHOUT_ANSWER = "empty broker response"

# Shape 2 is NOT unique to the race. A handler that runs and then raises
# before replying (_BrokerServer.handle_error exists precisely because
# _BrokerHandler.handle can) also closes with nothing written, reaching the
# client as the same causeless "empty broker response" — same signature,
# opposite meaning: a real broker bug we must never retry away.
#
# The two are told apart by watching the SERVER, since the client-visible
# signatures are identical. Two counters, and it takes both:
#
#   entered  — handle() began. The race fails inside
#              ThreadingMixIn.process_request, so a raced connection never
#              enters the handler at all.
#   crashed  — handle_error() fired. socketserver calls it for BOTH a failed
#              thread spawn and a handler that raised, so on its own it cannot
#              separate them either.
#
# entered AND crashed  -> the handler ran and blew up: a real broker bug, and
#                         the one case that must never be retried.
# entered, not crashed -> the handler ran to completion and the client still
#                         saw a transient error. Retry is correct.
# neither              -> the accept-then-close race. Retry is correct.
#
# Requiring both is load-bearing, not belt-and-braces: gating on `entered`
# alone made test_unauthorized_peer_uid_rejected fail 1-in-64 under 8-wide
# concurrent load, because the deny path answers from peer credentials alone
# and so legitimately runs before the client's sendall completes. (That client
# now recovers the reply rather than reporting the send error — see
# request_restart — so the deny path no longer *produces* a failure; the
# middle row is pinned synthetically by
# test_a_handler_that_ran_without_crashing_does_not_block_the_retry.)
#
# `_retry_transient_broker_io` snapshots this once per attempt, so it assumes
# one round trip per attempt: a `call()` making two, where the first succeeds
# and the second crashes a handler, would be attributed to the pair. Every
# call shape in this file is single-round-trip.
#
# Written from the handler thread, read from the test thread. Safe because
# there is only ever one client in flight at a time, NOT because of the GIL —
# `d[k] += 1` is load/add/store and is not atomic.
_HANDLER_ACTIVITY = {"entered": 0, "crashed": 0}


@pytest.fixture(autouse=True)
def _watch_broker_handler_activity(monkeypatch):
    """Count handler entries and handler crashes (see above)."""
    _HANDLER_ACTIVITY.update(entered=0, crashed=0)
    real_handle = restart_broker._BrokerHandler.handle
    real_handle_error = restart_broker._BrokerServer.handle_error

    def counting_handle(self):
        _HANDLER_ACTIVITY["entered"] += 1
        return real_handle(self)

    def counting_handle_error(self, request, client_address):
        _HANDLER_ACTIVITY["crashed"] += 1
        return real_handle_error(self, request, client_address)

    monkeypatch.setattr(restart_broker._BrokerHandler, "handle", counting_handle)
    monkeypatch.setattr(
        restart_broker._BrokerServer, "handle_error", counting_handle_error,
    )


class _TransientBrokerConnectRetryWarning(UserWarning):
    """Emitted once per transient-connection retry (see comment above).

    A momentary blip warns 1-2x and the test still passes; a persistently
    broken connection warns on every attempt and then fails loudly via the
    bounded re-raise, so the retry can never silently hide a real problem.
    """


def _transient_broker_cause(exc: BaseException) -> BaseException | None:
    """The "peer already closed on us" failure inside `exc`, or None when
    `exc` is not one.

    Accepts every shape the race produces: the raw exception (the hand-rolled
    socket round trip), the `BrokerUnavailable` that `request_restart` chains
    it onto, and the causeless `BrokerUnavailable` of shape 2, for which `exc`
    itself is the whole story. A `BrokerUnavailable` reporting a response the
    broker actually wrote ("invalid broker response: …", "broker response was
    not an object") is a real protocol-level answer and never matches — the
    handler cannot have written those without having run.
    """
    if isinstance(exc, restart_broker.BrokerUnavailable):
        if str(exc) == _BROKER_CLOSED_WITHOUT_ANSWER:
            return exc
        cause = exc.__cause__
    else:
        cause = exc
    if isinstance(cause, (BrokenPipeError, ConnectionResetError)):
        return cause
    return None


def _retry_transient_broker_io(call):
    """Run `call`, retrying only a transient connection failure.

    The single retry policy for every real round trip in this file — bound,
    backoff, and warning are defined once here so the three call shapes
    (`request_restart`, the raw socket, `manage_units`) cannot drift apart.
    """
    last_exc: BaseException | None = None
    for attempt in range(_BROKER_CONNECT_RETRIES + 1):
        before = dict(_HANDLER_ACTIVITY)
        try:
            return call()
        except (OSError, restart_broker.BrokerUnavailable) as exc:
            cause = _transient_broker_cause(exc)
            if cause is None:
                raise
            if (
                _HANDLER_ACTIVITY["entered"] > before["entered"]
                and _HANDLER_ACTIVITY["crashed"] > before["crashed"]
            ):
                # The handler ran AND blew up: a real broker bug wearing the
                # race's signature. Surface it instead of retrying it away.
                raise
            last_exc = exc
            if attempt == _BROKER_CONNECT_RETRIES:
                break  # out of attempts: re-raise below without a false promise
            warnings.warn(
                f"test_restart_broker: retrying broker connect (attempt "
                f"{attempt + 1}/{_BROKER_CONNECT_RETRIES}) after "
                f"transient failure: {cause!r}",
                _TransientBrokerConnectRetryWarning,
                stacklevel=2,
            )
            time.sleep(0.05 * (attempt + 1))
    assert last_exc is not None  # only reached via the except-continue path
    raise last_exc


def _request_restart_retrying_transient_failures(*args, **kwargs):
    """`restart_broker.request_restart`, retried only on a transient
    connection failure (see the module comment above)."""
    return _retry_transient_broker_io(
        lambda: restart_broker.request_restart(*args, **kwargs),
    )


def _forbid_silent_direct_fallback(monkeypatch):
    """Make `manage_units`' root fallback impossible to take silently.

    `manage_units` swallows `BrokerUnavailable` and, when root, re-runs the
    request through `_direct_systemctl` — which builds its argv with the same
    `_build_argv` the broker uses, so the two paths record IDENTICAL systemctl
    calls. (Their result dicts do differ: only the broker's carries
    `status`/`confirmed`/`self_deferred`, which is why the caller also asserts
    on `status` as positive proof of provenance.) A transient connect failure
    would therefore have left a `calls == [...]` assertion passing while the
    broker was never used at all.

    Re-raising the failure that drove us here does two jobs: it feeds a
    transient cause back to the retry so a blip does not flake the test, and
    it turns a genuine preference regression into an immediate, named failure
    instead of a silent pass.
    """

    def direct_is_forbidden(verb, units, **kwargs):
        pending = sys.exc_info()[1]
        if isinstance(pending, restart_broker.BrokerUnavailable):
            raise pending
        raise AssertionError(
            "manage_units reached the direct systemctl fallback with no "
            "BrokerUnavailable in flight "
            f"(verb={verb!r}, units={units!r}). Either it stopped preferring "
            "the broker, or the fallback moved out of the `except` block that "
            "sys.exc_info() above depends on — check which before assuming a "
            "preference regression."
        )

    monkeypatch.setattr(restart_broker, "_direct_systemctl", direct_is_forbidden)


def test_happy_path_restart(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-voice", verb="restart", reason="test", socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert resp["action"] == "restart"
    assert resp["units"] == ["jasper-voice.service"]
    assert calls == [["systemctl", "restart", "--no-block", "jasper-voice.service"]]


def test_self_restart_is_queued_after_other_units(broker, monkeypatch):
    """A wizard restart list may include jasper-control itself. The broker
    must queue voice/mux first so killing control cannot cancel them."""
    sock_path, calls, _ = broker
    popen_calls: list[list[str]] = []

    class _FakePopen:
        pid = 12345

    def fake_popen(argv, **kwargs):
        popen_calls.append(list(argv))
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        return _FakePopen()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    resp = _request_restart_retrying_transient_failures(
        "jasper-voice",
        "jasper-control",
        "jasper-mux",
        verb="restart",
        reason="spotify setup",
        socket_path=sock_path,
    )

    assert resp["ok"] is True
    assert resp["self_deferred"] is True
    assert resp["confirmed"] is False
    assert resp["status"] == "queued_unconfirmed"
    assert resp["rc"] is None
    assert resp["units"] == [
        "jasper-voice.service",
        "jasper-control.service",
        "jasper-mux.service",
    ]
    assert calls == [[
        "systemctl",
        "restart",
        "--no-block",
        "jasper-voice.service",
        "jasper-mux.service",
    ]]
    assert popen_calls == [[
        "systemctl",
        "restart",
        "--no-block",
        "jasper-control.service",
    ]]


def test_enable_now_maps_through_broker(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "shairport-sync.service", verb="enable-now", no_block=False,
        socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert calls == [["systemctl", "enable", "--now", "shairport-sync.service"]]


def test_unknown_verb_rejected_without_running_anything(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-voice.service", verb="exec", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert "unknown verb" in resp["error"]
    assert calls == []


def test_unit_not_in_allowlist_rejected(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "sshd.service", verb="stop", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert "allowlist" in resp["error"]
    assert calls == []


def test_start_only_unit_allows_start(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-audio-hardware-reconcile.service",
        verb="start",
        no_block=False,
        socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert calls == [
        ["systemctl", "start", "jasper-audio-hardware-reconcile.service"],
    ]


def test_wifi_scan_repair_helper_allows_start_only(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-wifi-scan-repair.service",
        verb="start",
        no_block=False,
        socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert calls == [
        ["systemctl", "start", "jasper-wifi-scan-repair.service"],
    ]


def test_start_only_unit_rejects_restart(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-audio-hardware-reconcile.service",
        verb="restart",
        socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert "allowlist" in resp["error"]
    assert calls == []


def test_one_bad_unit_blocks_the_whole_request(broker):
    sock_path, calls, _ = broker
    resp = _request_restart_retrying_transient_failures(
        "jasper-voice.service", "sshd.service", verb="restart",
        socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert calls == []


def test_nonzero_systemctl_surfaces_rc_and_stderr(broker):
    sock_path, calls, rc_holder = broker
    rc_holder["rc"] = 5
    rc_holder["stderr"] = "Unit not loaded"
    resp = _request_restart_retrying_transient_failures(
        "jasper-voice.service", verb="restart", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert resp["rc"] == 5
    assert "Unit not loaded" in resp["stderr"]
    assert calls  # it DID attempt the restart


# The wrapper's own retry/no-retry decision is pinned directly against a
# monkeypatched restart_broker.request_restart -- no real socket, no
# SO_PEERCRED, so these run on every platform and every CI leg (the
# integration test below is Linux-only). Mirrors
# test_wifi_guardian_script.py's own harness-resilience tests
# (test_run_guardian_retries_transient_spawn_oserror,
# test_run_guardian_warns_on_each_transient_retry,
# test_run_guardian_does_not_retry_real_oserror).


def test_retry_wrapper_passes_through_genuine_response_untouched(monkeypatch):
    """A real broker response -- including a genuine "unauthorized"
    rejection -- never raises BrokerUnavailable, so the wrapper must call
    through exactly once and never warn."""
    calls = {"n": 0}

    def real_response(*args, **kwargs):
        calls["n"] += 1
        return {"ok": False, "error": "peer uid 999999 is unauthorized"}

    monkeypatch.setattr(restart_broker, "request_restart", real_response)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _request_restart_retrying_transient_failures(
            "jasper-voice.service", verb="restart",
        )
    assert result == {"ok": False, "error": "peer uid 999999 is unauthorized"}
    assert calls["n"] == 1
    assert not [
        w for w in caught
        if issubclass(w.category, _TransientBrokerConnectRetryWarning)
    ]


def test_retry_wrapper_retries_transient_connection_failures_then_succeeds(
    monkeypatch,
):
    """BrokenPipeError then ConnectionResetError as the BrokerUnavailable
    cause -- the two "peer already closed on us" signatures the accept-
    then-close race produces -- are each retried, warning once per retry,
    until the real response comes through."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            try:
                raise BrokenPipeError("synthetic transient failure")
            except BrokenPipeError as cause:
                raise restart_broker.BrokerUnavailable(str(cause)) from cause
        if calls["n"] == 2:
            try:
                raise ConnectionResetError("synthetic transient failure")
            except ConnectionResetError as cause:
                raise restart_broker.BrokerUnavailable(str(cause)) from cause
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "request_restart", flaky)
    with pytest.warns(_TransientBrokerConnectRetryWarning) as record:
        result = _request_restart_retrying_transient_failures("jasper-voice.service")
    assert result == {"ok": True}
    assert calls["n"] == 3
    retries = [
        w for w in record
        if issubclass(w.category, _TransientBrokerConnectRetryWarning)
    ]
    assert len(retries) == 2
    assert "attempt 1/" in str(retries[0].message)


def test_retry_wrapper_does_not_retry_a_response_the_broker_actually_wrote(
    monkeypatch,
):
    """A BrokerUnavailable reporting bytes the broker really sent ("invalid
    broker response", "broker response was not an object") is a real
    protocol-level answer -- the handler cannot have written them without
    having run, so load is ruled out and it must surface on the very first
    attempt.

    Note this is NOT true of every causeless BrokerUnavailable: "empty broker
    response" carries no cause either, and IS a load artefact (shape 2 above).
    """
    calls = {"n": 0}

    def not_transient(*args, **kwargs):
        calls["n"] += 1
        raise restart_broker.BrokerUnavailable("invalid broker response: junk")

    monkeypatch.setattr(restart_broker, "request_restart", not_transient)
    with pytest.raises(
        restart_broker.BrokerUnavailable, match="invalid broker response",
    ):
        _request_restart_retrying_transient_failures("jasper-voice.service")
    assert calls["n"] == 1


def test_retry_wrapper_retries_a_peer_close_without_answer(monkeypatch):
    """Shape 2 of the race: the client's sendall won, so there is no chained
    exception -- just a causeless "empty broker response" after the peer hung
    up. Retrying only the chained shapes is what left this flaky under load."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise restart_broker.BrokerUnavailable(_BROKER_CLOSED_WITHOUT_ANSWER)
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "request_restart", flaky)
    with pytest.warns(_TransientBrokerConnectRetryWarning):
        assert _request_restart_retrying_transient_failures("jasper-voice") == {
            "ok": True,
        }
    assert calls["n"] == 2


def test_peer_close_without_answer_uses_the_sentinel_the_retry_matches(
    tmp_path, monkeypatch,
):
    """Pins the exact message request_restart raises when the peer accepts,
    reads the request, then closes without writing.

    _BROKER_CLOSED_WITHOUT_ANSWER has to match it by string because there is
    no exception to chain, so a product reword would silently narrow the retry
    back to shape 1 and re-open the flake. No _BrokerServer here, so this
    runs on every platform.
    """
    # Bound relative to a chdir'd tmp_path: an absolute pytest tmp_path blows
    # AF_UNIX's ~104-char sun_path limit on macOS, where this test also runs.
    monkeypatch.chdir(tmp_path)
    sock_path = "silent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)
    # Bounded so a failure before the client connects can't strand the thread
    # (and its FD) for the rest of the session — in the one file whose whole
    # subject is FD/thread pressure.
    listener.settimeout(5)

    def accept_read_then_close():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(4096)  # drain it, so the client's sendall wins the race

    peer = threading.Thread(target=accept_read_then_close, daemon=True)
    peer.start()
    try:
        with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
            restart_broker.request_restart("jasper-voice", socket_path=sock_path)
        assert str(excinfo.value) == _BROKER_CLOSED_WITHOUT_ANSWER
        assert excinfo.value.__cause__ is None
        assert _transient_broker_cause(excinfo.value) is excinfo.value
    finally:
        peer.join(timeout=5)
        assert not peer.is_alive()
        listener.close()


def test_retry_wrapper_does_not_retry_a_non_transient_cause(monkeypatch):
    """A BrokerUnavailable chained from an unrelated OSError (the socket
    genuinely absent) is a real bug or misconfiguration, not FD/thread
    pressure -- it must surface on the very first attempt, never be
    retried into a confusing multi-second delay."""
    calls = {"n": 0}

    def not_transient(*args, **kwargs):
        calls["n"] += 1
        try:
            raise FileNotFoundError("no such socket")
        except FileNotFoundError as cause:
            raise restart_broker.BrokerUnavailable(str(cause)) from cause

    monkeypatch.setattr(restart_broker, "request_restart", not_transient)
    with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
        _request_restart_retrying_transient_failures("jasper-voice.service")
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)
    assert calls["n"] == 1


def test_retry_wrapper_bounded_retries_then_raises_loudly(monkeypatch):
    """A persistently-broken connection must not retry forever: bounded at
    _BROKER_CONNECT_RETRIES + 1 total attempts, then the last exception is
    raised loudly -- never swallowed.

    One warning per RETRY, not per attempt: the final attempt is followed by
    a re-raise, so announcing a retry there would be a promise the helper does
    not keep (and would burn a pointless backoff sleep). Mirrors
    test_wifi_guardian_script.py's `if attempt < _SPAWN_RETRIES` guard.
    """
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    def always_transient(*args, **kwargs):
        calls["n"] += 1
        try:
            raise BrokenPipeError("synthetic, persistent failure")
        except BrokenPipeError as cause:
            raise restart_broker.BrokerUnavailable(str(cause)) from cause

    monkeypatch.setattr(restart_broker, "request_restart", always_transient)
    with pytest.warns(_TransientBrokerConnectRetryWarning) as record:
        with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
            _request_restart_retrying_transient_failures("jasper-voice.service")
    assert isinstance(excinfo.value.__cause__, BrokenPipeError)
    assert calls["n"] == _BROKER_CONNECT_RETRIES + 1 == 6
    retries = [
        w for w in record
        if issubclass(w.category, _TransientBrokerConnectRetryWarning)
    ]
    assert len(retries) == _BROKER_CONNECT_RETRIES == 5
    assert "attempt 5/5" in str(retries[-1].message)


def test_retry_wrapper_retries_a_raw_transient_connection_failure(monkeypatch):
    """The hand-rolled socket round trip raises BrokenPipeError directly
    rather than wrapped in BrokerUnavailable, so the shared core must accept
    the bare exception too -- while still passing an unrelated OSError (a
    genuinely absent socket) straight through."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    def flaky_roundtrip():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("synthetic transient failure")
        return b'{"ok": false}\n'

    with pytest.warns(_TransientBrokerConnectRetryWarning):
        assert _retry_transient_broker_io(flaky_roundtrip) == b'{"ok": false}\n'
    assert calls["n"] == 2

    def absent_socket():
        raise FileNotFoundError("no such socket")

    with pytest.raises(FileNotFoundError):
        _retry_transient_broker_io(absent_socket)


def test_direct_fallback_guard_hands_a_transient_cause_back_to_the_retry(
    monkeypatch,
):
    """manage_units swallows BrokerUnavailable and answers from
    _direct_systemctl, whose argv is byte-identical to the broker's -- so
    without the guard a transient failure passes every "prefers the broker"
    assertion while the broker was never used. The guard must re-raise the
    original failure (cause intact) so the retry core sees it and the final
    answer genuinely comes from the broker."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    monkeypatch.setattr(os, "geteuid", lambda: 0)  # root: the fallback is armed
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            try:
                raise BrokenPipeError("synthetic transient failure")
            except BrokenPipeError as cause:
                raise restart_broker.BrokerUnavailable(str(cause)) from cause
        return {"ok": True, "via": "broker"}

    monkeypatch.setattr(restart_broker, "request_restart", flaky)
    _forbid_silent_direct_fallback(monkeypatch)

    with pytest.warns(_TransientBrokerConnectRetryWarning):
        resp = _retry_transient_broker_io(
            lambda: restart_broker.manage_units("jasper-mux", verb="restart"),
        )
    assert resp == {"ok": True, "via": "broker"}
    assert calls["n"] == 2


def test_direct_fallback_guard_fails_loudly_without_a_pending_failure(monkeypatch):
    """Reaching the direct fallback with no BrokerUnavailable in flight is a
    real preference regression, not load -- it must assert immediately rather
    than be retried into a confusing multi-second delay."""
    _forbid_silent_direct_fallback(monkeypatch)
    with pytest.raises(AssertionError, match="direct systemctl fallback"):
        restart_broker._direct_systemctl(
            "restart", ["jasper-mux.service"], no_block=True, timeout=5.0,
        )


def _send_late(monkeypatch, answered):
    """Make request_restart's sendall land AFTER the peer has answered.

    That ordering is what the deny path hits under load: peer credentials
    come from the socket itself, so the broker can reject and close before a
    byte is sent. `answered` is an Event the peer sets once it is done with
    the connection — having replied or not, depending on the caller.

    Waiting on it rather than sleeping a guessed interval is what makes these
    deterministic, and the reason is NOT that a slow peer would fail loudly.
    Measured with the wait neutered so the client's send always wins: only
    test_a_failed_send_with_no_answer_still_raises goes red. The other two —
    including test_a_reply_already_delivered_survives_a_failed_send, the test
    that pins the fix — go VACUOUSLY GREEN, taking the ordinary path and
    never touching the salvage at all. A peer starved past a sleep window on
    a loaded box would therefore retire the coverage silently instead of
    reporting it, which is the exact failure mode this file exists to
    prevent.

    Patches only restart_broker's own `socket` name, so the server side and
    every other socket in the process are untouched.
    """
    class LateSendingSocket(socket.socket):
        def sendall(self, data, *args, **kwargs):
            assert answered.wait(5), "peer never answered; ordering not forced"
            # Re-arm, so a retried attempt waits for its OWN connection to be
            # answered rather than sailing through on the previous one's
            # signal — otherwise only the first attempt is actually ordered.
            answered.clear()
            return super().sendall(data, *args, **kwargs)

    class _SocketModuleWithLateSend:
        """Everything the real socket module has, except a slow-sending
        socket class. The delegation is not optional: the broker's own
        _peer_cred resolves SOL_SOCKET through this same name (SO_PEERCRED
        itself is captured at import), so a shim carrying only what
        request_restart uses crashed the handler instead of delaying the
        client.
        """

        socket = LateSendingSocket

        def __getattr__(self, name):
            return getattr(socket, name)

    monkeypatch.setattr(restart_broker, "socket", _SocketModuleWithLateSend())


def test_a_reply_already_delivered_survives_a_failed_send(tmp_path, monkeypatch):
    """A failed sendall must not discard an answer the peer already wrote.

    The broker answers its two pre-auth rejections from peer credentials
    alone, so it can reply and close before the client's send completes. The
    reply is then sitting in the client's receive queue -- measured on Linux
    AF_UNIX: sendall raised 200/200 and the reply was still readable 200/200.
    Reporting the send error without looking is what turned "unauthorized
    peer" into "broken pipe" for 6% of denials under 8-wide load on a Pi 5.

    No _BrokerServer here (a plain listener replies and hangs up), so this
    pins the client's control flow on every platform, not just Linux.
    """
    monkeypatch.chdir(tmp_path)  # AF_UNIX sun_path is ~104 chars on macOS
    sock_path = "denied.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)
    listener.settimeout(5)

    answered = threading.Event()

    def reply_without_reading():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            with conn:  # answer and hang up; never read the request
                conn.sendall(b'{"ok": false, "error": "unauthorized peer"}\n')
        finally:
            answered.set()  # the close above is what fails the client's send

    peer = threading.Thread(target=reply_without_reading, daemon=True)
    peer.start()
    _send_late(monkeypatch, answered)
    try:
        resp = restart_broker.request_restart("jasper-voice", socket_path=sock_path)
        assert resp["ok"] is False
        assert "unauthorized" in resp["error"]
    finally:
        peer.join(timeout=5)
        assert not peer.is_alive()
        listener.close()


def test_a_failed_send_with_no_answer_still_raises(tmp_path, monkeypatch):
    """The recovery above must not swallow a genuine send failure: with
    nothing to read, the original error still surfaces as BrokerUnavailable
    rather than degrading into a causeless "empty broker response" (which the
    retry policy treats very differently)."""
    monkeypatch.chdir(tmp_path)
    sock_path = "silent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)
    listener.settimeout(5)

    answered = threading.Event()

    def close_without_answering():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            conn.close()
        finally:
            answered.set()

    peer = threading.Thread(target=close_without_answering, daemon=True)
    peer.start()
    _send_late(monkeypatch, answered)
    try:
        with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
            restart_broker.request_restart("jasper-voice", socket_path=sock_path)
        assert isinstance(
            excinfo.value.__cause__, (BrokenPipeError, ConnectionResetError),
        )
    finally:
        peer.join(timeout=5)
        assert not peer.is_alive()
        listener.close()


@requires_peercred
def test_a_denied_peer_receives_its_rejection_not_a_broken_pipe(
    tmp_path, monkeypatch,
):
    """The two pre-auth rejections answer from peer credentials alone, so the
    handler can reply and close before the client's send completes. The reply
    is NOT lost when that happens -- measured on Linux AF_UNIX against the
    pre-fix server, sendall raised 200/200 and the reply was still readable
    200/200. It was only skipped, because request_restart used to wrap the
    send and the read in one `try`. The denied caller therefore saw "broken
    pipe" and the journal's `event=restart_broker.denied` was the only
    evidence left of why: 6% of denials (147/2400) under 8-wide concurrent
    load on a Pi 5, 0% idle.

    End to end against a real broker, with the ordering forced by the Event
    rather than left to load, so the salvage is always the path exercised.
    The signal comes from the server's `shutdown_request` — the moment the
    connection is actually closed, and the one hook socketserver still calls
    when the handler thread fails to spawn, so a transient race unblocks the
    client into a retry instead of stalling it.
    """
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {999999})
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    assert server is not None

    # Signal from the instance, not the class: socketserver closes the
    # connection in shutdown_request, after the handler has replied, so this
    # is the moment the client's send can start failing.
    closed = threading.Event()
    real_shutdown_request = server.shutdown_request

    def shutdown_request_then_signal(request):
        try:
            return real_shutdown_request(request)
        finally:
            closed.set()

    server.shutdown_request = shutdown_request_then_signal
    _send_late(monkeypatch, closed)
    try:
        resp = _request_restart_retrying_transient_failures(
            "jasper-voice", socket_path=sock_path,
        )
        assert resp["ok"] is False
        assert "unauthorized" in resp["error"]
    finally:
        server.shutdown()
        server.server_close()


@requires_peercred
def test_a_handler_that_ran_without_crashing_does_not_block_the_retry(broker):
    """The `crashed` half of the conjunction is load-bearing on its own.

    A handler can run to completion and the client still see a transient
    failure, so "a handler ran" must NOT by itself veto the retry -- that
    over-strict rule passed every serial loop and then failed 1-in-64 under
    8-wide concurrent load. Delete the `crashed` conjunct and this test goes
    red: the round trip below enters a handler that never crashes, so an
    entered-only rule would refuse to retry and re-raise the blip.
    """
    sock_path, _calls, _ = broker
    attempts = {"n": 0}

    def round_trip_then_blip():
        attempts["n"] += 1
        resp = restart_broker.request_restart(
            "jasper-voice", socket_path=sock_path,
        )  # a real, clean handler run: entered +1, crashed +0
        if attempts["n"] == 1:
            raise BrokenPipeError("synthetic post-handler blip")
        return resp

    with pytest.warns(_TransientBrokerConnectRetryWarning):
        resp = _retry_transient_broker_io(round_trip_then_blip)
    assert resp["ok"] is True
    assert attempts["n"] == 2


@requires_peercred
def test_a_crashed_handler_is_not_retried_away(tmp_path, monkeypatch):
    """A handler that runs and raises before replying reaches the client
    through the very signatures this file retries on -- observed as both
    ConnectionResetError and a causeless "empty broker response", depending on
    where the crash lands relative to the client's send.

    It is a real broker bug, so it must surface on the first attempt instead
    of being retried away and mislabelled transient. Which signature appears
    is a race, so this asserts the property that matters: no retry happened.
    """
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {os.getuid(), 0})
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: _FakeProc(),
    )
    # Crash INSIDE handle() rather than replacing it: replacing the method
    # would also drop the autouse entry counter, which is the very thing under
    # test. _peer_cred is handle()'s first call and only OSError is caught
    # there, so a RuntimeError propagates out exactly like a real handler bug.
    def crash(_connection):
        raise RuntimeError("synthetic handler crash")

    monkeypatch.setattr(restart_broker, "_peer_cred", crash)
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
                _request_restart_retrying_transient_failures(
                    "jasper-voice", socket_path=sock_path,
                )
        assert _transient_broker_cause(excinfo.value) is not None, (
            "precondition: the crash must land on a signature the retry would "
            f"otherwise have taken, but got {excinfo.value!r}"
        )
        assert not [
            w for w in caught
            if issubclass(w.category, _TransientBrokerConnectRetryWarning)
        ], "a crashed handler was retried and reported as a transient failure"
    finally:
        server.shutdown()
        server.server_close()


@requires_peercred
def test_retry_recovers_the_real_accept_then_close_race(tmp_path, monkeypatch):
    """End-to-end against the actual #1909 mechanism rather than a synthetic
    stand-in: socketserver fails to spawn the handler thread, so
    `_handle_request_noblock` closes the already-accepted socket without ever
    reading it.

    Pins the assumption the whole retry policy rests on: whichever of the two
    shapes the race lands in — and under concurrent load it lands in both —
    the client-visible failure is one the retry recognises. Asserting a
    specific exception class here would be asserting which side of a race won.
    Also pins the claim that a retried round trip leaves the exact `calls`
    assertions intact, because the handler never ran.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(list(argv)) or _FakeProc(),
    )
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {os.getuid(), 0})

    budget = {"n": 0}
    spawn_handler = restart_broker._BrokerServer.process_request

    def failing_process_request(self, request, client_address):
        if budget["n"] > 0:
            budget["n"] -= 1
            raise RuntimeError("can't start new thread")
        return spawn_handler(self, request, client_address)

    monkeypatch.setattr(
        restart_broker._BrokerServer, "process_request", failing_process_request,
    )
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    try:
        budget["n"] = 1
        with pytest.raises(restart_broker.BrokerUnavailable) as excinfo:
            restart_broker.request_restart("jasper-voice", socket_path=sock_path)
        assert _transient_broker_cause(excinfo.value) is not None, (
            f"the race surfaced as {excinfo.value!r} "
            f"(cause {excinfo.value.__cause__!r}), which the retry would not "
            f"recognise — a third shape needs adding to _transient_broker_cause"
        )
        assert calls == []  # the handler never ran, so nothing was executed

        budget["n"] = 1
        with pytest.warns(_TransientBrokerConnectRetryWarning):
            resp = _request_restart_retrying_transient_failures(
                "jasper-voice", socket_path=sock_path,
            )
        assert resp["ok"] is True
        assert calls == [
            ["systemctl", "restart", "--no-block", "jasper-voice.service"],
        ]
    finally:
        server.shutdown()
        server.server_close()


@requires_peercred
def test_unauthorized_peer_uid_rejected(tmp_path, monkeypatch):
    """A peer whose uid is not in the allowlist is refused before any
    systemctl runs."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(list(argv)) or _FakeProc(),
    )
    # Allow nobody the test runner is (use an impossible uid set).
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {999999})
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    try:
        resp = _request_restart_retrying_transient_failures(
            "jasper-voice.service", verb="restart", socket_path=sock_path,
        )
        assert resp["ok"] is False
        assert "unauthorized" in resp["error"]
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_json_request_rejected(broker):
    sock_path, calls, _ = broker

    # Hand-rolled because request_restart cannot send a malformed body — which
    # means this frame has to spell out both shapes of the race itself, where
    # request_restart would have normalised them. Shape 1 arrives as a raw
    # exception the shared core already accepts; shape 1 is not the only one,
    # so shape 2's empty read is reported the same way request_restart reports
    # it, rather than being left to become an IndexError on the parse below.
    def roundtrip() -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(sock_path)
            sock.sendall(b"this is not json\n")
            data = sock.recv(4096)
        if not data:
            raise restart_broker.BrokerUnavailable(_BROKER_CLOSED_WITHOUT_ANSWER)
        return data

    data = _retry_transient_broker_io(roundtrip)
    resp = json.loads(data.decode().splitlines()[0])
    assert resp["ok"] is False
    assert calls == []


# --------------------------------------------------------------------------
# Client: BrokerUnavailable + the root fallback.
# --------------------------------------------------------------------------


def test_request_restart_raises_when_socket_absent(tmp_path):
    with pytest.raises(restart_broker.BrokerUnavailable):
        restart_broker.request_restart(
            "jasper-voice.service", socket_path=str(tmp_path / "nope.sock"),
            timeout=0.5,
        )


def test_manage_units_falls_back_to_direct_systemctl_when_root(tmp_path, monkeypatch):
    """Broker unreachable + euid 0 -> direct systemctl (the PR1 transition
    safety net), logged loudly."""
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    direct_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        direct_calls.append(list(argv))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    resp = restart_broker.manage_units(
        "jasper-voice", verb="restart", reason="fallback test", timeout=0.5,
    )
    assert resp["ok"] is True
    assert direct_calls == [
        ["systemctl", "restart", "--no-block", "jasper-voice.service"],
    ]


def test_root_fallback_uses_same_narrow_timeout_exception(tmp_path, monkeypatch):
    """The transition-only direct path cannot bypass the broker's ceiling."""
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    seen: list[float] = []

    def fake_run(_argv, **kwargs):
        seen.append(kwargs["timeout"])
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    restart_broker.manage_units(
        "jasper-grouping-reconcile.service",
        verb="restart",
        no_block=False,
        timeout=9999,
    )
    restart_broker.manage_units(
        "jasper-source-intent-reconcile.service",
        verb="start",
        no_block=False,
        timeout=9999,
    )
    restart_broker.manage_units(
        "jasper-source-intent-reconcile.service",
        verb="start",
        no_block=True,
        timeout=9999,
    )
    assert seen == [
        restart_broker._EXEC_TIMEOUT_CEILING_SEC,
        restart_broker._SOURCE_INTENT_EXEC_TIMEOUT_CEILING_SEC,
        restart_broker._EXEC_TIMEOUT_CEILING_SEC,
    ]


def test_camilla_start_exemption_mirrors_its_callers_derived_bound():
    """The broker must not silently truncate the bound its caller derived.

    jasper-camilla.service Requires= a Type=oneshot hardware reconciler whose
    RemainAfterExit is unset, so a camilla start re-queues it in full — measured
    25.5-26.0 s of a 28.7-30.3 s restart on jts4. Clamping the caller's derived
    bound back to the ordinary 120 s ceiling would re-create the same false
    timeout that bound removes.
    """
    from jasper.fanin.coupling_reconcile import _CAMILLA_START_TIMEOUT_SEC

    assert (
        restart_broker._CAMILLA_START_EXEC_TIMEOUT_CEILING_SEC
        == _CAMILLA_START_TIMEOUT_SEC
    )
    assert _CAMILLA_START_TIMEOUT_SEC > restart_broker._EXEC_TIMEOUT_CEILING_SEC


def test_extended_exec_ceilings_are_per_unit_and_per_verb():
    """Each exemption is exactly one (unit, verb); nothing else is widened."""
    assert set(restart_broker._EXTENDED_EXEC_TIMEOUT_CEILING_SEC) == {
        (restart_broker._SOURCE_INTENT_RECONCILE_UNIT, "start"),
        (restart_broker._CAMILLA_UNIT, "start"),
    }

    def clamp(unit, verb, *, no_block=False, units=None):
        return restart_broker._clamp_exec_timeout(
            9999, verb=verb, units=units or [unit], no_block=no_block
        )

    ordinary = restart_broker._EXEC_TIMEOUT_CEILING_SEC
    camilla = restart_broker._CAMILLA_UNIT
    # The exempt shape passes through; every neighbouring shape does not.
    assert clamp(camilla, "start") == (
        restart_broker._CAMILLA_START_EXEC_TIMEOUT_CEILING_SEC
    )
    assert clamp(camilla, "restart") == ordinary
    assert clamp(camilla, "stop") == ordinary
    assert clamp(camilla, "start", no_block=True) == ordinary
    assert clamp(camilla, "start", units=[camilla, "jasper-fanin.service"]) == ordinary
    assert clamp("jasper-fanin.service", "start") == ordinary


def test_manage_units_no_fallback_when_non_root(tmp_path, monkeypatch):
    """Broker unreachable + non-root -> error dict, never a direct systemctl.
    This is the post-user-drop behaviour: the broker is the only path."""
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: ran.append(list(argv)),
    )
    resp = restart_broker.manage_units(
        "jasper-voice", verb="restart", timeout=0.5,
    )
    assert resp["ok"] is False
    assert "unavailable" in resp["error"]
    assert ran == []  # NO direct systemctl when non-root


@pytest.mark.parametrize("call", ["request_restart", "manage_units"])
def test_default_socket_path_is_resolved_at_call_time(call, tmp_path, monkeypatch):
    """Both client entry points must read DEFAULT_SOCKET_PATH when called, not
    when defined.

    `socket_path` used to default to the module attribute directly, which
    Python binds once at def time -- so every `monkeypatch.setattr(
    restart_broker, "DEFAULT_SOCKET_PATH", ...)` in this file was a silent
    no-op and the call went to the real /run/jasper-control/restart.sock. That
    is how test_manage_units_prefers_broker_over_fallback passed while only
    ever exercising the fallback (EACCES on a box running jasper-control,
    ENOENT elsewhere -- it fell back either way), and it meant a suite run as
    root would have driven the LIVE broker: _allowed_uids() includes uid 0, so
    the real daemons would have been restarted for real.

    Proved by putting a real listener at the rebound path and requiring the
    call to actually reach it -- a connect error names no path, and a spy on
    request_restart would only ever prove the manage_units half. No
    _BrokerServer here, so this runs on every platform. (chdir + a relative
    name because an absolute pytest tmp_path blows AF_UNIX's ~104-char
    sun_path limit on macOS.)
    """
    monkeypatch.chdir(tmp_path)
    rebound = "rebound.sock"
    monkeypatch.setattr(restart_broker, "DEFAULT_SOCKET_PATH", rebound)
    monkeypatch.setattr(os, "geteuid", lambda: 1234)  # root fallback disarmed

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(rebound)
    listener.listen(1)
    listener.settimeout(5)  # so a failure can never wedge the thread forever
    received: list[bytes] = []

    def answer_once():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            received.append(conn.recv(4096))
            conn.sendall(b'{"ok": true, "via": "rebound listener"}\n')

    peer = threading.Thread(target=answer_once, daemon=True)
    peer.start()
    try:
        resp = getattr(restart_broker, call)("jasper-voice", timeout=2.0)
        assert resp == {"ok": True, "via": "rebound listener"}, (
            f"{call} did not reach the listener at the rebound default"
        )
        assert received and b"jasper-voice.service" in received[0]
    finally:
        peer.join(timeout=5)
        assert not peer.is_alive()
        listener.close()


def test_root_fallback_rejects_an_unknown_verb_without_running_systemctl(
    tmp_path, monkeypatch,
):
    """The closed verb vocabulary must hold on the fallback path too.

    The broker's own rejection is covered by
    test_unknown_verb_rejected_without_running_anything, but _direct_systemctl
    runs as ROOT and enforces the vocabulary separately. Mutation testing
    (neutering its `verb not in ALLOWED_VERBS` guard) left the whole file
    green, so nothing pinned it.
    """
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)  # root: fallback is armed
    ran: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(list(argv)))

    resp = restart_broker.manage_units("jasper-voice", verb="exec", timeout=0.5)

    assert resp["ok"] is False
    assert "unknown verb" in resp["error"]
    assert ran == []  # never reached systemctl


def test_manage_units_empty_units_is_noop():
    resp = restart_broker.manage_units(verb="restart")
    assert resp["ok"] is True
    assert resp["units"] == []


@pytest.mark.parametrize("reset_ok", [True, False])
def test_reset_then_manage_runs_the_action_whatever_the_reset_did(
    monkeypatch, reset_ok,
):
    """The single owner of "clear the budget, then act anyway".

    ``reset-failed`` is denied for START_ONLY units and exits nonzero against
    an already-GC'd oneshot (#3237), so its verdict may never gate — or be
    mistaken for — the action's. This is the pin every caller of
    :func:`restart_broker.reset_then_manage` inherits instead of restating.
    """
    calls: list[tuple[str, str, bool, float]] = []

    def fake_manage(*units, verb="restart", reason="", no_block=True, timeout=5.0):
        calls.append((units[0], verb, no_block, timeout))
        if verb == "reset-failed":
            return {"ok": reset_ok, "error": "" if reset_ok else "denied"}
        return {"ok": True, "action": verb, "units": list(units)}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage)

    resp = restart_broker.reset_then_manage(
        "jasper-fanin.service", verb="restart", reason="t", timeout=8.0,
    )

    assert resp == {
        "ok": True, "action": "restart", "units": ["jasper-fanin.service"],
    }
    assert calls == [
        ("jasper-fanin.service", "reset-failed", False,
         restart_broker._RESET_TIMEOUT_SEC),
        ("jasper-fanin.service", "restart", True, 8.0),
    ]


def test_manage_units_prefers_broker_over_fallback(broker, monkeypatch):
    """When the broker IS reachable, manage_units uses it and never touches
    the direct path — even as root."""
    sock_path, calls, _ = broker
    monkeypatch.setattr(restart_broker, "DEFAULT_SOCKET_PATH", sock_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    # Unlike every other round trip here, manage_units swallows
    # BrokerUnavailable rather than raising it, and its root fallback runs the
    # SAME argv through the SAME _build_argv — so both assertions below pass
    # identically whichever path answered. Without this guard a transient
    # connect failure would not flake the test, it would silently stop testing
    # the preference the docstring claims.
    _forbid_silent_direct_fallback(monkeypatch)
    resp = _retry_transient_broker_io(
        lambda: restart_broker.manage_units("jasper-mux", verb="restart"),
    )
    assert resp["ok"] is True
    # Positive proof of provenance: only the broker's reply carries `status`
    # (_direct_systemctl returns ok/action/units/rc/stderr and nothing else).
    assert resp["status"] == "confirmed"
    # Exactly one call, made by the broker (not a fallback duplicate).
    assert calls == [["systemctl", "restart", "--no-block", "jasper-mux.service"]]


# --------------------------------------------------------------------------
# exec-timeout bound: the client's timeout becomes the broker's systemctl
# exec bound (clamped), and the client waits a margin past it — so a blocking
# restart can't outlive the client's wait, and no client can pin a broker
# thread past the hard ceiling.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, restart_broker._DEFAULT_EXEC_TIMEOUT_SEC),    # missing -> default
    ("not-a-number", restart_broker._DEFAULT_EXEC_TIMEOUT_SEC),  # junk -> default
    (0.1, 1.0),                                           # clamped up to the floor
    (12.0, 12.0),                                         # in-range passthrough
    (370.0, restart_broker._EXEC_TIMEOUT_CEILING_SEC),
    (9999, restart_broker._EXEC_TIMEOUT_CEILING_SEC),
])
def test_clamp_exec_timeout_keeps_normal_actions_under_global_ceiling(raw, expected):
    assert restart_broker._clamp_exec_timeout(
        raw,
        verb="restart",
        units=["jasper-grouping-reconcile.service"],
        no_block=False,
    ) == expected


@pytest.mark.parametrize(
    ("verb", "units", "no_block", "expected"),
    [
        (
            "start",
            ["jasper-source-intent-reconcile.service"],
            False,
            restart_broker._SOURCE_INTENT_EXEC_TIMEOUT_CEILING_SEC,
        ),
        (
            "start",
            ["jasper-source-intent-reconcile.service"],
            True,
            restart_broker._EXEC_TIMEOUT_CEILING_SEC,
        ),
        (
            "restart",
            ["jasper-source-intent-reconcile.service"],
            False,
            restart_broker._EXEC_TIMEOUT_CEILING_SEC,
        ),
        (
            "start",
            [
                "jasper-source-intent-reconcile.service",
                "jasper-grouping-reconcile.service",
            ],
            False,
            restart_broker._EXEC_TIMEOUT_CEILING_SEC,
        ),
    ],
)
def test_extended_timeout_requires_exact_blocking_source_intent_start(
    verb,
    units,
    no_block,
    expected,
):
    assert restart_broker._clamp_exec_timeout(
        9999,
        verb=verb,
        units=units,
        no_block=no_block,
    ) == expected


@requires_peercred
def test_broker_bounds_systemctl_to_client_exec_timeout(tmp_path, monkeypatch):
    """request_restart(timeout=T) makes the broker run systemctl with that
    bound (clamped to the ceiling), so its verdict always lands before the
    client's socket — which waits T + margin — gives up."""
    seen: dict[str, float | None] = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {os.getuid(), 0})
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    try:
        resp = _request_restart_retrying_transient_failures(
            "jasper-voice.service", verb="restart", no_block=False,
            timeout=7.0, socket_path=sock_path,
        )
        assert resp["ok"] is True
        assert seen["timeout"] == 7.0  # client's timeout became the exec bound

        # A normal over-ceiling request is clamped to the 120-second global
        # bound, not the source-intent exception.
        _request_restart_retrying_transient_failures(
            "jasper-voice.service", verb="restart", no_block=False,
            timeout=9999, socket_path=sock_path,
        )
        assert seen["timeout"] == restart_broker._EXEC_TIMEOUT_CEILING_SEC

        # The parsed broker request grants the extended bound only to the exact
        # blocking
        # source-intent start shape.
        _request_restart_retrying_transient_failures(
            "jasper-source-intent-reconcile.service",
            verb="start",
            no_block=False,
            timeout=9999,
            socket_path=sock_path,
        )
        assert seen["timeout"] == (
            restart_broker._SOURCE_INTENT_EXEC_TIMEOUT_CEILING_SEC
        )

        _request_restart_retrying_transient_failures(
            "jasper-source-intent-reconcile.service",
            verb="start",
            no_block=True,
            timeout=9999,
            socket_path=sock_path,
        )
        assert seen["timeout"] == restart_broker._EXEC_TIMEOUT_CEILING_SEC
    finally:
        server.shutdown()
        server.server_close()
