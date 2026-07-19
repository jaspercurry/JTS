# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-spanning capture-plan tests (capture protocol v3, SPEC W2.3).

Drives the Pi-side plan runner — begin → Pi-owned admission → authorize →
armed → per-index blob upload → pull/decrypt/verify → consume → result, times
``capture_target`` — against a faithful in-memory relay backend mirroring the
Worker's indexed-blob behaviour (``?index=N`` keys, per-index ``blobs`` status
summary, verbatim host events). The "phone" is a scripted driver reacting to
host events exactly as the future v3 capture page will, so the whole
choreography is proven with no network, no live Worker, and no page.

The budget stays PI-OWNED: the admission integration tests below wire the real
``jasper.active_speaker.repeat_admission`` ledger into ``authorize_begin`` /
``consume_capture`` and prove refusal-at-cap and abort-mid-set persistence
against the durable state file.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import urllib.parse
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayResponse
from jasper.capture_relay.integrity import authenticated_phone_event
from jasper.capture_relay.session import (
    CaptureActivityProbe,
    CaptureAborted,
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureFailed,
    CaptureStopped,
    CaptureTimeout,
    classify_status,
    mint_session,
    parse_begin_capture,
    register_session,
    run_capture,
    run_capture_plan,
)
from jasper.capture_relay.spec import (
    CapturePlan,
    CapturePlanEntry,
    build_crossover_sweep_spec,
)

_BINDING = "placement_abcdefghijklmnopqrstuv"

_PAGE_V3 = {
    "schema_version": 1,
    "capture_protocol_version": 3,
    "supported_capture_protocol_versions": [1, 2, 3],
    "capture_page_build": "20260716.1",
}

_PAGE_V2 = {
    "schema_version": 1,
    "capture_protocol_version": 2,
    "supported_capture_protocol_versions": [1, 2],
    "capture_page_build": "20260711.1",
}


class FakePlanRelayBackend:
    """In-memory transport mirroring the Worker's v3 (indexed-blob) contract."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.host_events: dict[str, list[dict]] = {}

    def __call__(self, method, url, headers, body):
        split = urllib.parse.urlsplit(url)
        parts = [p for p in split.path.split("/") if p]
        query = urllib.parse.parse_qs(split.query)
        auth = headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""

        def jr(status, obj):
            return RelayResponse(status, {}, json.dumps(obj).encode())

        if parts == ["sessions"] and method == "POST":
            reg = json.loads(body)
            self.sessions[reg["session_id"]] = {
                "capture_spec": reg["capture_spec"],
                "upload_token": reg["upload_token"],
                "pull_token": reg["pull_token"],
                "max_upload_bytes": reg["max_upload_bytes"],
                "state": "pending",
                "event": None,
                "host_event": None,
                "integrity": None,
                "blobs": {},  # index -> {"blob": bytes, "integrity": {...}}
            }
            self.host_events[reg["session_id"]] = []
            return jr(201, {"session_id": reg["session_id"], "state": "pending"})

        if len(parts) >= 2 and parts[0] == "sessions":
            sid = parts[1]
            sub = parts[2] if len(parts) > 2 else ""
            s = self.sessions.get(sid)
            if not s:
                return jr(404, {"error": "not_found"})
            if token != s["pull_token"]:
                return jr(401, {"error": "unauthorized"})
            if sub == "status" and method == "GET":
                blobs_summary = {
                    key: {
                        "size": len(entry["blob"]),
                        "integrity": entry["integrity"],
                    }
                    for key, entry in s["blobs"].items()
                }
                return jr(
                    200,
                    {
                        "state": s["state"],
                        "size": len((s["blobs"].get("0") or {}).get("blob", b"")),
                        "integrity": s["integrity"],
                        "event": s["event"],
                        "host_event": s["host_event"],
                        "expires_at": 0,
                        **({"blobs": blobs_summary} if blobs_summary else {}),
                    },
                )
            if sub == "host-event" and method == "POST":
                event = json.loads(body)
                s["host_event"] = event
                self.host_events[sid].append(event)
                return jr(200, {"ok": True})
            if sub == "blob" and method == "GET":
                index = (query.get("index") or ["0"])[-1]
                entry = s["blobs"].get(index)
                if not entry:
                    return jr(409, {"error": "not_ready"})
                return RelayResponse(
                    200,
                    {
                        "x-plaintext-length": str(
                            entry["integrity"]["plaintext_len"]
                        ),
                        "x-plaintext-sha256": entry["integrity"]["sha256"],
                    },
                    entry["blob"],
                )
            if sub == "" and method == "DELETE":
                del self.sessions[sid]
                return RelayResponse(204, {}, b"")
        return jr(404, {"error": "not_found"})

    # --- phone simulation ---

    def phone_post(self, sid, event, *, session, sequence):
        self.sessions[sid]["event"] = authenticated_phone_event(
            session.content_key, session.session_id, event, sequence=sequence
        )

    def phone_upload(self, sid, content_key, wav, *, index):
        iv = os.urandom(crypto.IV_BYTES)
        blob = iv + AESGCM(content_key).encrypt(iv, wav, None)
        s = self.sessions[sid]
        integrity = {
            "plaintext_len": len(wav),
            "sha256": hashlib.sha256(wav).hexdigest(),
        }
        s["blobs"][str(index)] = {"blob": blob, "integrity": integrity}
        if index == 0:
            # Mirrors the Worker: index 0 aliases the legacy un-indexed slot.
            s["integrity"] = integrity
            s["state"] = "ready"

    def phases(self, sid) -> list[str]:
        return [str(e.get("phase")) for e in self.host_events[sid]]


class PhonePlanDriver:
    """Scripted v3 phone: reacts to Pi host events before each status poll."""

    def __init__(self, backend, session, *, page=None, setup=None):
        self.backend = backend
        self.session = session
        self.page = dict(page or _PAGE_V3)
        self.sequence = 0
        self.begun: tuple[int, int] | None = None
        self.armed_for: tuple[int, int] | None = None
        self.reacted: set[tuple[int, int]] = set()
        self.abort_after_results = 0  # abort after N results when > 0
        self.results_seen = 0
        self.finished = False
        # W6.13: the page-side fix (capture-page/js/main.js's
        # beginAndAwaitAuthorization) PIGGYBACKS the household-mic setup on
        # every begin_capture post, rather than only inside the later `armed`
        # event — the relay's mutable event slot is last-write-wins, so
        # whichever event a Pi poll actually observes must carry `setup` for
        # PollState.setup to accumulate it from round 1. Opt-in (default
        # None) so every existing test's begin() payload stays
        # byte-identical; set to model that fix's shape.
        self.setup = setup

    def _post(self, event):
        self.sequence += 1
        self.backend.phone_post(
            self.session.session_id,
            event,
            session=self.session,
            sequence=self.sequence,
        )

    def begin(self, index, attempt):
        self.begun = (index, attempt)
        event = {
            "begin_capture": {"index": index, "attempt": attempt},
            "capture_page": self.page,
        }
        if self.setup is not None:
            event["setup"] = self.setup
        self._post(event)

    def _acknowledgement(self):
        required = self.session.spec.acknowledgement
        return {
            "schema_version": 1,
            "id": required.id,
            "binding_id": required.binding_id,
            "accepted": True,
        }

    def arm(self):
        index, attempt = self.begun
        self._post(
            {
                "armed": True,
                "begin_capture": {"index": index, "attempt": attempt},
                "capture_page": self.page,
                "acknowledgement": self._acknowledgement(),
                "device": {"label": "UMIK-1"},
            }
        )
        self.armed_for = self.begun

    def abort(self, reason="backgrounded"):
        self._post({"aborted": True, "abort_reason": reason})
        self.finished = True

    def step(self):
        if self.finished:
            return
        host = self.backend.sessions[self.session.session_id]["host_event"] or {}
        phase = host.get("phase")
        if self.begun is None:
            self.begin(1, 1)
            return
        key = (host.get("index"), host.get("attempt"))
        if (
            phase == "capture_authorized"
            and key == self.begun
            and self.armed_for != self.begun
        ):
            self.arm()
            return
        if phase == "capture_result" and key == self.begun and key not in self.reacted:
            self.reacted.add(key)
            self.results_seen += 1
            if self.abort_after_results and (
                self.results_seen >= self.abort_after_results
            ):
                self.abort()
                return
            index, attempt = key
            if host.get("accepted"):
                self.begin(index + 1, attempt + 1)
            else:
                self.begin(index, attempt + 1)
        if phase in ("capture_set_complete", "capture_set_exhausted"):
            self.finished = True


def _mint_plan_session(
    backend, *, capture_target=3, max_attempts=4, driver=True, entries=None
):
    plan = (
        CapturePlan(
            capture_target=capture_target,
            max_attempts=max_attempts,
            schema_version=2,
            entries=tuple(entries),
        )
        if entries is not None
        else CapturePlan(capture_target=capture_target, max_attempts=max_attempts)
    )
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=plan,
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    if not driver:
        return client, session, None
    phone = PhonePlanDriver(backend, session)

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            phone.step()
        return backend(method, url, headers, body)

    return (
        RelayClient("https://relay.test", transport=transport),
        session,
        phone,
    )


def _wav(attempt: int) -> bytes:
    return b"RIFF" + bytes([attempt]) * 96


def _run_kwargs(**overrides):
    ticks = itertools.count()
    kwargs = dict(
        poll_interval_s=0.0,
        timeout_s=500.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
    )
    kwargs.update(overrides)
    return kwargs


def _plan_callbacks(backend, session, *, verdicts=None):
    """authorize/on_armed/consume wiring shared by the transport-level tests."""
    authorized: list[tuple[int, int]] = []
    consumed: list[tuple[int, int, bytes]] = []
    verdicts = dict(verdicts or {})

    def authorize(index, attempt):
        authorized.append((index, attempt))

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id,
            session.content_key,
            _wav(attempt),
            index=attempt - 1,
        )

    def consume(index, attempt, result):
        consumed.append((index, attempt, result.wav))
        return verdicts.get(attempt, {"accepted": True, "estimated_snr_db": 30.5})

    return authorize, on_armed, consume, authorized, consumed


# --- multi-capture lifecycle ---------------------------------------------------


def test_full_plan_round_trip_three_accepted_captures(caplog):
    caplog.set_level(logging.INFO, logger="jasper.capture_relay.session")
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, authorized, consumed = _plan_callbacks(
        backend, session
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
    ]
    # Each attempt's blob was pulled from ITS index and decrypted bit-identical.
    assert [wav for (_i, _a, wav) in consumed] == [_wav(1), _wav(2), _wav(3)]
    assert [o.result.wav for o in outcomes] == [_wav(1), _wav(2), _wav(3)]
    # Pi-owned admission ran once per begin — the persisted event slot was
    # never double-processed across polls (index dedup).
    assert authorized == [(1, 1), (2, 2), (3, 3)]
    # The phone saw the full authorize/result choreography, then the terminal.
    assert backend.phases(session.session_id) == [
        "capture_authorized",
        "capture_result",
        "capture_authorized",
        "capture_result",
        "capture_authorized",
        "capture_result",
        "capture_set_complete",
    ]
    result_events = [
        e
        for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_result"
    ]
    assert result_events[0]["accepted"] is True
    assert result_events[0]["index"] == 1
    assert result_events[0]["estimated_snr_db"] == 30.5  # verdict fields relayed
    assert "capture_relay.plan_complete" in caplog.text


def test_first_round_result_setup_reflects_whatever_event_carried_it(caplog):
    """W6.13: capture-page/js/main.js's beginAndAwaitAuthorization now
    piggybacks ``setup`` on every begin_capture post (the page-side fix for
    the v2 crossover flow's "no calibration-picker screen" gap —
    jasper.web.correction_crossover_v2.resolve_relay_calibration read nothing
    for the CHECK-phase capture because the household-mic hint only ever rode
    the LATER `armed` event). Pin the Pi-side half: PollState.setup is a
    generic field read off WHATEVER event the phone last posted, not
    special-cased to `armed` — so CaptureResult.setup for the very FIRST
    round (CHECK) carries the calibration when it rides the phone's
    `begin_capture` post (this test — the piggyback shape) rather than only
    its later `armed` post
    (test_full_plan_round_trip_three_accepted_captures's shape, which never
    sets `setup` at all)."""
    caplog.set_level(logging.INFO, logger="jasper.capture_relay.session")
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1
    )
    phone.setup = {
        "calibration": {
            "mode": "stored",
            "calibration_id": "cal-household",
            "model": "minidsp_umik2",
        },
    }
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert outcomes[0].index == 1
    assert outcomes[0].attempt == 1
    assert outcomes[0].result.setup == phone.setup


def test_rejected_attempt_retries_same_slot_with_fresh_attempt_and_blob_index():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, authorized, consumed = _plan_callbacks(
        backend,
        session,
        verdicts={2: {"accepted": False, "reject_reason": "snr_insufficient"}},
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    # Slot 2 was rejected on attempt 2 and retried as attempt 3; slot 3 rode
    # attempt 4 — four blobs at four distinct relay indexes.
    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, False),
        (2, 3, True),
        (3, 4, True),
    ]
    assert sorted(backend.sessions[session.session_id]["blobs"]) == [
        "0",
        "1",
        "2",
        "3",
    ]
    rejected = [
        e
        for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_result" and e.get("accepted") is False
    ]
    assert rejected[0]["reject_reason"] == "snr_insufficient"
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"


def test_set_exhausted_when_attempt_budget_spent_before_target():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(
        backend, capture_target=3, max_attempts=4
    )
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend,
        session,
        verdicts={
            2: {"accepted": False, "reject_reason": "snr_insufficient"},
            3: {"accepted": False, "reject_reason": "snr_insufficient"},
        },
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    # 4 attempts spent, only 2 accepted — the runner returns every outcome and
    # tells the phone the set is exhausted; the host decides what to do next.
    assert [(o.attempt, o.accepted) for o in outcomes] == [
        (1, True),
        (2, False),
        (3, False),
        (4, True),
    ]
    assert backend.phases(session.session_id)[-1] == "capture_set_exhausted"
    terminal = backend.host_events[session.session_id][-1]
    assert terminal["accepted"] == 2
    assert terminal["capture_target"] == 3
    assert terminal["attempts"] == 4


# --- per-capture entries (heterogeneous v3 plans, SPEC crossover-measurement- --
# --- productization-design.md §5.7) --------------------------------------------


def _entries_plan(capture_target=2, max_attempts=2, **entry_overrides):
    """A schema_version 2 plan with one entry per index, custom durations."""
    durations = entry_overrides.pop("durations", None) or [
        5000 + 1000 * i for i in range(capture_target)
    ]
    entries = tuple(
        CapturePlanEntry(
            index=i,
            kind_label=("check", "measure", "verify")[min(i, 2)],
            duration_ms=durations[i],
        )
        for i in range(capture_target)
    )
    return CapturePlan(
        capture_target=capture_target,
        max_attempts=max_attempts,
        schema_version=2,
        entries=entries,
    )


def test_plan_callbacks_receive_the_active_entry_when_they_accept_one():
    backend = FakePlanRelayBackend()
    plan = _entries_plan(capture_target=2, max_attempts=2)
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=plan,
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    backend_client = RelayClient("https://relay.test", transport=backend)
    register_session(backend_client, session)
    phone = PhonePlanDriver(backend, session)

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            phone.step()
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)

    seen_authorize_entries: list[CapturePlanEntry | None] = []
    seen_consume_entries: list[CapturePlanEntry | None] = []

    def authorize(index, attempt, entry=None):
        seen_authorize_entries.append(entry)

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    def consume(index, attempt, result, entry=None):
        seen_consume_entries.append(entry)
        return {"accepted": True}

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert [o.accepted for o in outcomes] == [True, True]
    assert seen_authorize_entries == list(plan.entries)
    assert seen_consume_entries == list(plan.entries)


def test_plan_callbacks_with_old_arity_are_unaffected_by_entries():
    # jasper/web/correction_crossover_flow.py's existing authorize_begin(index,
    # attempt) / consume_capture(index, attempt, result) predate entries and
    # must keep working UNCHANGED against a plan that now carries them.
    backend = FakePlanRelayBackend()
    plan = _entries_plan(capture_target=2, max_attempts=2)
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=plan,
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    register_session(RelayClient("https://relay.test", transport=backend), session)
    phone = PhonePlanDriver(backend, session)

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            phone.step()
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)
    authorize, on_armed, consume, authorized, consumed = _plan_callbacks(
        backend, session
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert [o.accepted for o in outcomes] == [True, True]
    assert authorized == [(1, 1), (2, 2)]


def test_entries_plan_recording_deadline_stays_the_global_backstop():
    # S1 (design §5.7): entry.duration_ms is the capture's DECLARED acoustic
    # length — presentation/locator data — NEVER the hard deadline. The
    # awaiting_upload recording+upload backstop is the runner's own timeout_s
    # for every plan, entries or not. Proven by parking the phone in "armed,
    # never uploads" with a tiny 3s entry: the timeout must fire on the 20s
    # timeout_s budget (many fake-tick polls), not the entry's 3s (a
    # handful), and the error message must name the deadline actually in
    # force.
    backend = FakePlanRelayBackend()
    entries = (CapturePlanEntry(index=0, kind_label="check", duration_ms=3000),)
    plan = CapturePlan(
        capture_target=1, max_attempts=1, schema_version=2, entries=entries
    )
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=plan,
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    poll_count = {"n": 0}
    phone = PhonePlanDriver(backend, session)

    def arm_but_never_upload():
        if phone.finished:
            return
        host = backend.sessions[session.session_id]["host_event"] or {}
        phase = host.get("phase")
        if phone.begun is None:
            phone.begin(1, 1)
            return
        key = (host.get("index"), host.get("attempt"))
        if (
            phase == "capture_authorized"
            and key == phone.begun
            and phone.armed_for != phone.begun
        ):
            phone.arm()
        # else: sit tight forever — no blob ever lands.

    phone.step = arm_but_never_upload

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            poll_count["n"] += 1
            phone.step()
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)
    register_session(client, session)

    with pytest.raises(CaptureTimeout) as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda _state: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(timeout_s=20.0),
        )
    assert ei.value.phase == "awaiting_upload"
    # The message reports the deadline that was actually in force — the
    # global timeout_s backstop, never the entry's 3s declared length.
    assert "within 20s" in str(ei.value)
    assert poll_count["n"] > 10, (
        "the awaiting_upload deadline must stay the global timeout_s "
        "backstop (20s of fake ticks); an entry-duration override would "
        "have fired after only a handful of polls"
    )


def test_v1_plan_without_entries_is_byte_identical_no_deadline_change():
    # Same "arm but never upload" shape as above, but on a v1 plan (no
    # entries) — the same flat timeout_s backstop governs (a SHORT timeout_s
    # times out within a bounded number of polls), exactly as before
    # per-capture entries existed.
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1, driver=False
    )
    poll_count = {"n": 0}
    phone = PhonePlanDriver(backend, session)

    def arm_but_never_upload():
        if phone.finished:
            return
        host = backend.sessions[session.session_id]["host_event"] or {}
        phase = host.get("phase")
        if phone.begun is None:
            phone.begin(1, 1)
            return
        key = (host.get("index"), host.get("attempt"))
        if (
            phase == "capture_authorized"
            and key == phone.begun
            and phone.armed_for != phone.begun
        ):
            phone.arm()

    phone.step = arm_but_never_upload

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            poll_count["n"] += 1
            phone.step()
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)

    with pytest.raises(CaptureTimeout) as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda _state: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(timeout_s=3.0),
        )
    assert ei.value.phase == "awaiting_upload"
    # A v1 plan's deadline is governed by the flat timeout_s (3s here), the
    # same as before per-capture entries existed — bounded and fast.
    assert poll_count["n"] < 30


def test_deferred_begin_is_non_terminal_and_a_retry_succeeds():
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1
    )
    calls = {"n": 0}

    def authorize(_index, _attempt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CaptureBeginDeferred(
                "not_ready", "Waiting for the previous step to finish."
            )
        # second call: admits normally.

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    def consume(_index, _attempt, _result):
        return {"accepted": True}

    real_step = phone.step

    def step_retries_on_deferred():
        host = backend.sessions[session.session_id]["host_event"] or {}
        if host.get("phase") == "capture_deferred" and phone.begun == (1, 1):
            # Distinct soft-hold, not a refusal: the SAME (index, attempt)
            # pair is legal to re-post — the Pi never marked it processed.
            phone.begun = None
        real_step()

    phone.step = step_retries_on_deferred

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [(1, 1, True)]
    assert calls["n"] == 2  # deferred once, then admitted — attempt never bumped
    phases = backend.phases(session.session_id)
    assert "capture_deferred" in phases
    assert phases[-1] == "capture_set_complete"
    deferred_events = [
        e
        for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_deferred"
    ]
    assert deferred_events == [
        {
            "phase": "capture_deferred",
            "index": 1,
            "attempt": 1,
            "code": "not_ready",
            "error": "Waiting for the previous step to finish.",
        }
    ]


def test_repeated_identical_deferrals_post_and_log_once_per_hold(caplog):
    # S2 dedupe: the phone re-posts the same begin throughout a hold, so N
    # consecutive deferrals for the same (index, code) must produce exactly
    # ONE INFO log_event and ONE capture_deferred host event; a changed code
    # is a new state and gets a second of each. Identical repeats stay at
    # DEBUG with no host POST.
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1
    )
    codes = iter(["not_ready", "not_ready", "not_ready", "waiting_apply"])

    def authorize(_index, _attempt):
        code = next(codes, None)
        if code is not None:
            raise CaptureBeginDeferred(code, f"hold ({code})")
        # fifth call: admits normally.

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    real_step = phone.step

    def step_retries_on_deferred():
        host = backend.sessions[session.session_id]["host_event"] or {}
        if host.get("phase") == "capture_deferred" and phone.begun == (1, 1):
            phone.begun = None  # re-post the SAME (index, attempt)
        real_step()

    phone.step = step_retries_on_deferred

    with caplog.at_level(logging.DEBUG, logger="jasper.capture_relay.session"):
        outcomes = run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(),
        )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [(1, 1, True)]
    # Host events: one per (index, code) state change — never one per retry.
    deferred_events = [
        e
        for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_deferred"
    ]
    assert [e["code"] for e in deferred_events] == ["not_ready", "waiting_apply"]
    plan_deferred_records = [
        r
        for r in caplog.records
        if "event=capture_relay.plan_deferred" in r.getMessage()
    ]
    assert [
        r.levelno for r in plan_deferred_records
    ] == [logging.INFO, logging.DEBUG, logging.DEBUG, logging.INFO], (
        "first deferral INFO, two identical repeats DEBUG, code change INFO"
    )


# --- W6.10 blocker #1: the on_apply REVIEW hold rescopes the inactivity clock --


def test_on_apply_literal_matches_the_v2_flow_vocabulary():
    # session.py keeps a local "on_apply" literal so the generic runner does not
    # import the v2 flow upward; pin the two equal so a rename can't silently
    # break the REVIEW-hold rescope.
    from jasper.capture_relay.session import AUTO_ADVANCE_ON_APPLY
    from jasper.active_speaker.crossover_v2_flow import (
        AUTO_ADVANCE_ON_APPLY as FLOW_ON_APPLY,
    )

    assert AUTO_ADVANCE_ON_APPLY == FLOW_ON_APPLY == "on_apply"


def _review_plan_entries():
    """A 2-entry heterogeneous plan: a normal capture, then an on_apply-gated
    one (the "waiting for apply" REVIEW hold between MEASURE and VERIFY)."""
    return (
        CapturePlanEntry(index=0, kind_label="measure", duration_ms=4000),
        CapturePlanEntry(
            index=1,
            kind_label="verify",
            duration_ms=4000,
            screen={"title": "Waiting for apply", "auto_advance": "on_apply"},
        ),
    )


def _steady_clock():
    """A monotonic clock the test advances 1.0 per poll (via sleep)."""
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(_s):
        clock["t"] += 1.0

    return clock, monotonic, sleep


def test_on_apply_hold_survives_past_the_120s_budget_then_apply_proceeds():
    # MEASURE accepted, then the on_apply VERIFY entry is deferred while the
    # household reviews. The phone re-posts the SAME begin as liveness; the hold
    # must outlast the OLD 120s inactivity budget and then complete once apply
    # releases it — the Chrome round-2 blocker was a 120s watchdog destroying
    # the accepted MEASURE mid-review.
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=2, max_attempts=4, entries=_review_plan_entries()
    )
    clock, monotonic, sleep = _steady_clock()
    applied = {"at": None}

    def authorize(index, _attempt):
        if index == 2 and clock["t"] < 150.0:  # deferred-by-design until "apply"
            raise CaptureBeginDeferred("awaiting_apply", "waiting for apply")
        if index == 2 and applied["at"] is None:
            applied["at"] = clock["t"]

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    real_step = phone.step

    def step_reposts_during_hold():
        host = backend.sessions[session.session_id]["host_event"] or {}
        key = (host.get("index"), host.get("attempt"))
        if host.get("phase") == "capture_deferred" and key == (2, 2):
            phone.begin(2, 2)  # re-post the SAME begin (liveness) — not index 1
            return
        real_step()

    phone.step = step_reposts_during_hold

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=lambda _i, _a, _r: {"accepted": True},
        timeout_s=120.0,
        poll_interval_s=0.0,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, True),
    ]
    # The hold demonstrably outlasted the old 120s budget before apply released.
    assert applied["at"] is not None and applied["at"] >= 150.0


def test_on_apply_hold_collapses_at_review_budget_when_phone_vanishes():
    # The phone vanishes during the review hold (never posts the VERIFY begin).
    # The session must NOT collapse at 120s — it holds to the long REVIEW budget
    # (~900s) and only then times out, phase awaiting_begin, so the caller can
    # tear down. Proves the rescope is in force even before a first deferral.
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=2, max_attempts=4, entries=_review_plan_entries()
    )
    clock, monotonic, sleep = _steady_clock()

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    # Drive index 1 by hand (the shared PhonePlanDriver.step auto-advances to
    # index 2 on an accepted result — exactly what "the phone vanished" must
    # NOT do), then walk away: never begin index 2.
    seen_result = {"v": False}

    def step_index1_then_vanish():
        host = backend.sessions[session.session_id]["host_event"] or {}
        phase = host.get("phase")
        key = (host.get("index"), host.get("attempt"))
        if seen_result["v"]:
            return
        if phone.begun is None:
            phone.begin(1, 1)
            return
        if phase == "capture_authorized" and key == (1, 1) and phone.armed_for != (1, 1):
            phone.arm()
            return
        if phase == "capture_result" and key == (1, 1):
            seen_result["v"] = True  # index 1 done — walk away, never begin index 2

    phone.step = step_index1_then_vanish

    with pytest.raises(CaptureTimeout) as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=on_armed,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            timeout_s=120.0,
            poll_interval_s=0.0,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert ei.value.phase == "awaiting_begin"
    # Held to the REVIEW budget, not the tight 120s one.
    assert "within 900s" in str(ei.value)
    assert clock["t"] >= 900.0


def test_first_begin_timeout_widens_only_the_first_window():
    # The first begin (reading placement instructions) gets first_begin_timeout_s,
    # not the general timeout_s — a v2 fold-in so Chrome doesn't die reading the
    # microphone-check screen.
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1, driver=False
    )
    clock, monotonic, sleep = _steady_clock()

    with pytest.raises(CaptureTimeout) as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda _state: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            timeout_s=120.0,
            first_begin_timeout_s=300.0,
            poll_interval_s=0.0,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert ei.value.phase == "awaiting_begin"
    assert "within 300s" in str(ei.value)
    assert clock["t"] >= 300.0


def test_deferred_begin_does_not_end_the_session_on_stop():
    # A deferred hold keeps the plan alive across polls; a Stop mid-deferral
    # is still cooperative CaptureStopped, not a refusal-style CaptureFailed.
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=1, max_attempts=1
    )
    stop = {"requested": False}

    def authorize(_index, _attempt):
        stop["requested"] = True
        raise CaptureBeginDeferred("not_ready", "still waiting")

    real_step = phone.step

    def step_never_retries():
        # Only the initial begin — no retry, so the runner stays parked
        # in the deferred awaiting_begin state until Stop is observed.
        if phone.begun is None:
            real_step()

    phone.step = step_never_retries

    with pytest.raises(CaptureStopped):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=lambda _state: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            stop_requested=lambda: stop["requested"],
            **_run_kwargs(),
        )


# --- begin ordering: dedup / replay / out-of-order / budget --------------------


def test_replayed_begin_for_finished_attempt_is_refused():
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend)
    authorize, on_armed, consume, _authorized, consumed = _plan_callbacks(
        backend, session
    )

    real_step = phone.step

    def replay_after_first_result():
        host = backend.sessions[session.session_id]["host_event"] or {}
        if host.get("phase") == "capture_result" and phone.results_seen == 0:
            phone.results_seen = 1
            phone.begin(1, 1)  # replay the already-consumed begin
            return
        real_step()

    phone.step = replay_after_first_result

    with pytest.raises(CaptureFailed, match="already processed"):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal["phase"] == "capture_refused"
    assert refusal["code"] == "begin_replayed"
    # Capture 1 was consumed before the replay — its verdict is untouched.
    assert [(i, a) for (i, a, _w) in consumed] == [(1, 1)]


@pytest.mark.parametrize(
    ("index", "attempt", "code"),
    [
        (1, 2, "begin_out_of_order"),  # wrong first attempt
        (2, 2, "begin_out_of_order"),  # skips slot 1 (index <= attempt is valid shape)
        (2, 1, "begin_malformed"),  # slot unreachable before its attempt
        (1, 9, "begin_malformed"),  # attempt beyond the plan budget
        (0, 1, "begin_malformed"),
    ],
)
def test_out_of_order_or_malformed_first_begin_is_refused(index, attempt, code):
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    backend.phone_post(
        session.session_id,
        {
            "begin_capture": {"index": index, "attempt": attempt},
            "capture_page": dict(_PAGE_V3),
        },
        session=session,
        sequence=1,
    )
    authorize, on_armed, consume, authorized, _consumed = _plan_callbacks(
        backend, session
    )

    with pytest.raises(CaptureFailed):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    assert authorized == []  # refused BEFORE any admission ran
    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal["phase"] == "capture_refused"
    assert refusal["code"] == code


@pytest.mark.parametrize("failure_mode", ["unsigned", "tampered"])
def test_unauthenticated_begin_never_reaches_admission(failure_mode):
    """Belt-and-suspenders for the v3 vocabulary: a `begin_capture` that is
    not carried by a valid authenticated envelope — raw/unsigned, or a
    MAC-tampered payload — fails the session BEFORE the injected
    `authorize_begin` (the Pi-owned budget) is ever consulted. The guarantee
    is inherited from the shared protocol-v2 verifier; this pins it
    explicitly for the plan runner."""
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    event = {
        "begin_capture": {"index": 1, "attempt": 1},
        "capture_page": dict(_PAGE_V3),
    }
    if failure_mode == "unsigned":
        # Raw event straight into the relay slot — no authenticated envelope.
        backend.sessions[session.session_id]["event"] = event
    else:
        backend.phone_post(
            session.session_id, event, session=session, sequence=1
        )
        envelope = backend.sessions[session.session_id]["event"][
            "authenticated_event"
        ]
        # Relay-side payload edit: attempt 1 -> 2 without re-MACing.
        envelope["payload"] = envelope["payload"].replace(
            '"attempt":1', '"attempt":2'
        )

    authorized: list[tuple[int, int]] = []
    consumed: list[tuple[int, int]] = []
    with pytest.raises(CaptureFailed, match="control integrity"):
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda i, a: authorized.append((i, a)),
            on_armed=lambda: None,
            consume_capture=lambda i, a, _r: consumed.append((i, a)),
            **_run_kwargs(),
        )

    assert authorized == []  # admission never ran — no budget touched
    assert consumed == []
    assert backend.sessions[session.session_id]["host_event"]["phase"] == (
        "capture_incompatible"
    )


def test_begin_refusal_from_host_admission_is_published_with_its_name():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)

    def refuse(_index, _attempt):
        raise CaptureBeginRefused(
            "repeat_admission_refused",
            "the crossover repeat set already used four attempts",
        )

    _auth, on_armed, consume, _a, _c = _plan_callbacks(backend, session)
    with pytest.raises(CaptureBeginRefused, match="four attempts"):
        run_capture_plan(
            client,
            session,
            authorize_begin=refuse,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal == {
        "phase": "capture_refused",
        "code": "repeat_admission_refused",
        "error": "the crossover repeat set already used four attempts",
        "index": 1,
        "attempt": 1,
    }


# --- Pi-owned budget: the REAL repeat_admission ledger --------------------------


def _admission_fixture(tmp_path):
    from jasper.active_speaker import repeat_admission

    path = tmp_path / "repeat_admission.json"
    comparison = {"comparison_set_id": "cmp-1", "fingerprint": "f" * 64}
    repeat_admission.activate(comparison, path=path)
    return repeat_admission, path, comparison


def _admission_callbacks(backend, session, repeat_admission, path, comparison):
    reservations: dict[int, dict] = {}

    def authorize(index, attempt):
        try:
            reservations[attempt] = repeat_admission.reserve(
                comparison,
                target_id="mono:woofer",
                target_fingerprint="6" * 64,
                path=path,
            )
        except (RuntimeError, ValueError) as exc:
            raise CaptureBeginRefused("repeat_admission_refused", str(exc)) from exc

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id,
            session.content_key,
            _wav(attempt),
            index=attempt - 1,
        )

    def consume(index, attempt, result):
        reservation = reservations[attempt]
        repeat_admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="6" * 64,
            token=reservation["token"],
            result={"accepted": True, "estimated_snr_db": 31.0},
            status="active",
            path=path,
        )
        return {"accepted": True}

    return authorize, on_armed, consume


def test_budget_refusal_at_cap_comes_from_the_durable_admission_ledger(tmp_path):
    repeat_admission, path, comparison = _admission_fixture(tmp_path)
    # The set already consumed its four bounded attempts in earlier sessions.
    for _ in range(repeat_admission.MAX_ATTEMPTS):
        reservation = repeat_admission.reserve(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="6" * 64,
            path=path,
        )
        repeat_admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="6" * 64,
            token=reservation["token"],
            result={"accepted": False, "reject_reason": "snr_insufficient"},
            status="active",
            path=path,
        )

    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume = _admission_callbacks(
        backend, session, repeat_admission, path, comparison
    )

    with pytest.raises(CaptureBeginRefused, match="four attempts"):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal["phase"] == "capture_refused"
    assert refusal["code"] == "repeat_admission_refused"
    assert "four attempts" in refusal["error"]


def test_abort_mid_set_persists_accepted_captures_in_the_ledger(tmp_path):
    repeat_admission, path, comparison = _admission_fixture(tmp_path)
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend)
    phone.abort_after_results = 1  # phone aborts right after capture 1's result
    authorize, on_armed, consume = _admission_callbacks(
        backend, session, repeat_admission, path, comparison
    )

    with pytest.raises(CaptureAborted, match="backgrounded"):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    # The aborted SESSION does not roll back the accepted capture: the durable
    # admission ledger still holds attempt 1's accepted result, no inflight.
    entry = repeat_admission.snapshot(comparison, path=path)["targets"][
        "mono:woofer"
    ]
    assert entry["attempts"] == 1
    assert entry["inflight"] is None
    assert entry["results"][-1]["accepted"] is True


def test_stop_mid_set_persists_accepted_captures_without_failure_cue(
    tmp_path, caplog
):
    repeat_admission, path, comparison = _admission_fixture(tmp_path)
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume = _admission_callbacks(
        backend, session, repeat_admission, path, comparison
    )
    stop = {"requested": False}
    cues: list[str] = []

    def consume_then_stop(index, attempt, result):
        verdict = consume(index, attempt, result)
        stop["requested"] = True  # wizard/phone Stop lands after capture 1
        return verdict

    with caplog.at_level(logging.INFO), pytest.raises(CaptureStopped):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume_then_stop,
            stop_requested=lambda: stop["requested"],
            play_cue=cues.append,
            **_run_kwargs(),
        )

    assert cues == []  # explicit Stop is control flow, not a failure
    assert "event=capture_relay.stopped" in caplog.text
    entry = repeat_admission.snapshot(comparison, path=path)["targets"][
        "mono:woofer"
    ]
    assert entry["attempts"] == 1
    assert entry["results"][-1]["accepted"] is True


# --- protocol guards ------------------------------------------------------------


def test_run_capture_plan_requires_a_v3_plan_spec():
    backend = FakePlanRelayBackend()
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    with pytest.raises(CaptureFailed, match="capture protocol 3"):
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(),
        )


def test_v3_session_against_todays_v2_page_fails_before_any_stimulus():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    backend.phone_post(
        session.session_id,
        {
            "begin_capture": {"index": 1, "attempt": 1},
            "capture_page": dict(_PAGE_V2),
        },
        session=session,
        sequence=1,
    )
    authorize, on_armed, consume, authorized, _c = _plan_callbacks(
        backend, session
    )
    from jasper.capture_relay.session import CapturePageIncompatible

    with pytest.raises(CapturePageIncompatible, match="expected protocol 3"):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )
    assert authorized == []
    assert backend.sessions[session.session_id]["host_event"]["phase"] == (
        "capture_incompatible"
    )


def test_armed_without_the_authorized_begin_context_fails_loud():
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend)

    def arm_without_context():
        index, attempt = phone.begun
        phone._post(
            {
                "armed": True,
                "capture_page": phone.page,
                "acknowledgement": phone._acknowledgement(),
            }
        )
        phone.armed_for = phone.begun

    phone.arm = arm_without_context
    authorize, on_armed, consume, _a, consumed = _plan_callbacks(
        backend, session
    )

    armed_calls: list[object] = []
    with pytest.raises(CaptureFailed, match="authorized capture context"):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=lambda state: armed_calls.append(state),
            consume_capture=consume,
            **_run_kwargs(),
        )
    assert armed_calls == []  # no stimulus without the exact capture context
    assert consumed == []


def test_authorized_capture_that_never_arms_times_out_in_its_phase():
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend)
    phone.arm = lambda: None  # phone begins but never arms
    authorize, on_armed, consume, authorized, _c = _plan_callbacks(
        backend, session
    )

    with pytest.raises(CaptureTimeout, match="never armed") as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(timeout_s=20.0),
        )
    assert ei.value.phase == "awaiting_arm"
    assert authorized == [(1, 1)]


def test_phone_that_never_begins_times_out_in_the_begin_phase():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    with pytest.raises(CaptureTimeout, match="never began") as ei:
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(timeout_s=20.0),
        )
    assert ei.value.phase == "awaiting_begin"


# --- v2 path unchanged (contract) -----------------------------------------------


def test_v2_single_capture_ignores_a_begin_capture_field():
    """The v2 runner never reads `begin_capture` — a phone event carrying the
    v3 field flows through today's single-capture path untouched (the
    v2-unchanged contract, SPEC W2.3 compat matrix)."""
    backend = FakePlanRelayBackend()
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
    )
    assert spec.capture_protocol_version == 2
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    wav = b"RIFF" + bytes(range(64))
    backend.phone_post(
        session.session_id,
        {
            "armed": True,
            "begin_capture": {"index": 1, "attempt": 1},  # ignored by v2
            "capture_page": dict(_PAGE_V2),
            "acknowledgement": {
                "schema_version": 1,
                "id": spec.acknowledgement.id,
                "binding_id": _BINDING,
                "accepted": True,
            },
        },
        session=session,
        sequence=1,
    )

    def on_armed():
        backend.phone_upload(
            session.session_id, session.content_key, wav, index=0
        )

    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    assert result.wav == wav


# --- schema / classify / client / probe -----------------------------------------


def test_parse_begin_capture_schema_is_strict():
    assert parse_begin_capture(
        {"index": 2, "attempt": 3}, capture_target=3, max_attempts=4
    ) == (2, 3)
    for payload in (
        None,
        [],
        {"index": 1},
        {"attempt": 1},
        {"index": 1, "attempt": 1, "extra": True},
        {"index": True, "attempt": 1},
        {"index": 1, "attempt": "1"},
        {"index": 4, "attempt": 4},  # index beyond capture_target
        {"index": 1, "attempt": 5},  # attempt beyond max_attempts
        {"index": 3, "attempt": 2},  # slot before its attempt
    ):
        with pytest.raises(CaptureBeginRefused) as ei:
            parse_begin_capture(payload, capture_target=3, max_attempts=4)
        assert ei.value.code == "begin_malformed"


def test_classify_status_exposes_begin_capture_and_blob_summary():
    state = classify_status(
        {
            "state": "pending",
            "event": {"begin_capture": {"index": 1, "attempt": 1}},
            "blobs": {"0": {"size": 4, "integrity": {}}},
        }
    )
    assert state.begin_capture == {"index": 1, "attempt": 1}
    assert state.blobs == {"0": {"size": 4, "integrity": {}}}
    plain = classify_status({"state": "pending", "event": {"armed": True}})
    assert plain.begin_capture is None
    assert plain.blobs is None


def test_client_pull_blob_keys_the_request_by_capture_index():
    seen: list[str] = []

    def transport(method, url, headers, body):
        seen.append(url)
        return RelayResponse(
            200,
            {"x-plaintext-length": "4", "x-plaintext-sha256": "ab" * 32},
            b"blob",
        )

    client = RelayClient("https://relay.test", transport=transport)
    client.pull_blob("cap_1", "pull")
    client.pull_blob("cap_1", "pull", capture_index=0)
    client.pull_blob("cap_1", "pull", capture_index=3)
    # Index 0 stays byte-identical to the v2 request; only >0 adds the query.
    assert seen == [
        "https://relay.test/sessions/cap_1/blob",
        "https://relay.test/sessions/cap_1/blob",
        "https://relay.test/sessions/cap_1/blob?index=3",
    ]
    with pytest.raises(ValueError, match="capture_index"):
        client.pull_blob("cap_1", "pull", capture_index=-1)
    with pytest.raises(ValueError, match="capture_index"):
        client.pull_blob("cap_1", "pull", capture_index=True)


def test_activity_probe_is_per_index_aware_for_plans():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    backend.phone_post(
        session.session_id,
        {
            "armed": True,
            "begin_capture": {"index": 2, "attempt": 2},
            "capture_page": dict(_PAGE_V3),
        },
        session=session,
        sequence=1,
    )
    # Attempt 1's blob already exists (legacy state == ready for the session),
    # which must NOT read as "capture 2's recorder finished".
    backend.phone_upload(
        session.session_id, session.content_key, _wav(1), index=0
    )
    probe = CaptureActivityProbe(client, session, capture_index=1)
    probe.assert_active()

    # v2 semantics would have aborted host playback here:
    with pytest.raises(CaptureAborted, match="ended before host playback"):
        CaptureActivityProbe(client, session).assert_active()

    # Once THIS capture's blob lands, the plan-aware probe flags it too.
    backend.phone_upload(
        session.session_id, session.content_key, _wav(2), index=1
    )
    with pytest.raises(CaptureAborted, match="ended before host playback"):
        probe.assert_active()


def test_plan_session_tap_link_and_spec_round_trip_via_relay():
    backend = FakePlanRelayBackend()
    _client, session, _phone = _mint_plan_session(backend, driver=False)
    stored = backend.sessions[session.session_id]["capture_spec"]
    parsed = json.loads(stored)
    assert parsed["capture_protocol_version"] == 3
    assert parsed["capture_plan"] == {
        "schema_version": 1,
        "capture_target": 3,
        "max_attempts": 4,
    }
    # The tap-link contract (fragment-carried secrets) is untouched by v3.
    assert session.tap_link.startswith("https://capture.test/#s=")


def test_replace_based_plan_spec_matches_builder_output():
    # The follow-up page PR flips the marker by passing capture_plan to the
    # builder; pin that the field rides validation like any other.
    base = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
    )
    upgraded = replace(
        base,
        capture_plan=CapturePlan(capture_target=3, max_attempts=4),
        capture_protocol_version=3,
    ).validate()
    built = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=CapturePlan(capture_target=3, max_attempts=4),
    )
    assert upgraded.to_dict() == built.to_dict()
