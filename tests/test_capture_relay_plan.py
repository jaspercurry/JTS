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
from jasper.capture_relay.client import RelayClient, RelayError, RelayResponse
from jasper.capture_relay.integrity import authenticated_phone_event
from jasper.capture_relay.session import (
    STATUS_POLL_TRANSIENT_GRACE_S,
    STATUS_POLL_WARN_BUDGET,
    CaptureActivityProbe,
    CaptureAborted,
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureFailed,
    CaptureStopped,
    CaptureTimeout,
    RelayCapacityUnavailable,
    classify_status,
    mint_session,
    parse_begin_capture,
    register_session,
    run_capture,
    run_capture_plan,
)
from jasper.capture_relay.spec import (
    LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS,
    MAX_CAPTURE_PLAN_ATTEMPTS,
    CapturePlan,
    CapturePlanEntry,
    build_crossover_sweep_spec,
)

_BINDING = "placement_abcdefghijklmnopqrstuv"

_PAGE = {
    "schema_version": 1,
    "capture_protocol_version": 3,
    "supported_capture_protocol_versions": [3],
    "capture_page_build": "20260727.2",
}

# A page build from before the protocol-1/2 deletion: it advertises only the
# deleted versions, so the handshake must refuse it before any tone.
_PAGE_STALE = {
    "schema_version": 1,
    "capture_protocol_version": 2,
    "supported_capture_protocol_versions": [1, 2],
    "capture_page_build": "20260711.1",
}


class FakePlanRelayBackend:
    """In-memory transport mirroring the Worker's v3 (indexed-blob) contract.

    ``plan_ceiling`` models the DEPLOYED Worker's capture-plan capacity, served
    from ``GET /capabilities`` exactly as the real one does. ``None`` models a
    relay deployed before that endpoint existed: it 404s, which is the only
    version signal a pre-capacity relay ever carried.
    """

    def __init__(self, *, plan_ceiling: int | None = MAX_CAPTURE_PLAN_ATTEMPTS) -> None:
        self.sessions: dict[str, dict] = {}
        self.host_events: dict[str, list[dict]] = {}
        self.plan_ceiling = plan_ceiling

    def __call__(self, method, url, headers, body):
        split = urllib.parse.urlsplit(url)
        parts = [p for p in split.path.split("/") if p]
        query = urllib.parse.parse_qs(split.query)
        auth = headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""

        def jr(status, obj):
            return RelayResponse(status, {}, json.dumps(obj).encode())

        if parts == ["capabilities"] and method == "GET":
            if self.plan_ceiling is None:
                return jr(404, {"error": "not_found"})
            return jr(
                200,
                {
                    "schema_version": 1,
                    "max_capture_plan_attempts": self.plan_ceiling,
                },
            )

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
        self.page = dict(page or _PAGE)
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
        # Wire indexes this phone asks to RE-measure once, immediately after
        # they are accepted (flow-simplification §2.6's "Retake this
        # measurement" control). Empty for every pre-retake test, so their
        # scripts stay byte-identical.
        self.retake_indexes: set[int] = set()
        self.retakes_posted: list[tuple[int, int]] = []
        # How many times this phone has signalled the SET complete (work order
        # D1). Zero for every plan whose host does not hold the set open.
        self.completions_posted = 0

    def _post(self, event):
        self.sequence += 1
        self.backend.phone_post(
            self.session.session_id,
            event,
            session=self.session,
            sequence=self.sequence,
        )

    def begin(self, index, attempt, *, retake=False):
        self.begun = (index, attempt)
        begin_capture = {"index": index, "attempt": attempt}
        if retake:
            begin_capture["retake"] = True
            self.retakes_posted.append((index, attempt))
        event = {
            "begin_capture": begin_capture,
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
        begin_capture = {"index": index, "attempt": attempt}
        if (index, attempt) in self.retakes_posted:
            # The real page re-sends its begin payload verbatim on ``armed``,
            # marker included — the runner must compare slot identity only.
            begin_capture["retake"] = True
        self._post(
            {
                "armed": True,
                "begin_capture": begin_capture,
                "capture_page": self.page,
                "acknowledgement": self._acknowledgement(),
                "device": {"label": "UMIK-1"},
            }
        )
        self.armed_for = self.begun

    def abort(self, reason="backgrounded"):
        self._post({"aborted": True, "abort_reason": reason})
        self.finished = True

    def complete_set(self):
        """The household's "all spots measured — Continue" tap.

        Mirrors ``capture-page/js/main.js``'s ``completePlanCaptureSet``: on an
        accepted verdict carrying ``awaiting_confirm`` the page posts this
        signal instead of a begin for the next entry — which a held set does
        not have, since its final slot IS the capture target (two-stage
        commission work order D1).
        """
        self._post({"complete_capture_set": True, "capture_page": self.page})
        self.completions_posted += 1

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
            if host.get("accepted") and index in self.retake_indexes:
                # Once per index: the household tapping "Retake this
                # measurement" on the capture that just completed.
                self.retake_indexes.discard(index)
                self.begin(index, attempt + 1, retake=True)
            elif host.get("accepted") and host.get("awaiting_confirm"):
                # ORDER IS LOAD-BEARING, exactly as on the real page: a held
                # set's final slot is also its target, so an accepted verdict
                # here would otherwise route to a forward begin the runner must
                # refuse. Retake still wins above it — the confirm screen keeps
                # offering it, which is the whole point of the window.
                self.complete_set()
            elif (index, attempt) in self.retakes_posted:
                # A retake's slot is accepted either way — an accepted retake
                # replaced the take, a rejected one left the original standing
                # — so the page moves on rather than re-trying the slot.
                self.begin(index + 1, attempt + 1)
            elif host.get("accepted"):
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
    one (the "applying" hold between MEASURE and VERIFY — owner ruling,
    2026-07-20: machine-paced auto-apply, never a human review wait)."""
    return (
        CapturePlanEntry(index=0, kind_label="measure", duration_ms=4000),
        CapturePlanEntry(
            index=1,
            kind_label="verify",
            duration_ms=4000,
            screen={"title": "Applying", "auto_advance": "on_apply"},
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
    # host's own auto-apply runs (owner ruling, 2026-07-20 — never a human
    # review). The phone re-posts the SAME begin as liveness (a deferred
    # retry rearms the deadline every poll, independent of the budget's exact
    # size); the hold must outlast the tight 120s inactivity budget and then
    # complete once apply releases it — the Chrome round-2 blocker was a 120s
    # watchdog destroying the accepted MEASURE mid-hold.
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


def test_on_apply_hold_collapses_at_its_own_budget_when_phone_vanishes():
    # The phone vanishes during the apply hold (never posts the VERIFY begin).
    # Owner ruling (2026-07-20): the hold now covers a machine-paced auto-apply
    # transaction, not a human review, so its OWN budget (REVIEW_HOLD_BUDGET_S,
    # 30s) is actually SHORTER than the generic tight timeout_s used here
    # (120s) — this proves the rescope applies its OWN budget rather than
    # falling back to the (now longer) generic one, phase awaiting_begin, so
    # the caller can tear down promptly instead of waiting out the full 120s
    # for a hold that was never going to resolve on its own. Proves the
    # rescope is in force even before a first deferral.
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
    # Held to the apply hold's OWN (now shorter) budget, not the generic 120s
    # timeout_s passed above.
    assert "within 30s" in str(ei.value)
    assert clock["t"] >= 30.0
    assert clock["t"] < 120.0


def test_on_apply_entry_tolerates_a_long_tap_delay_between_authorize_and_arm():
    """The VERIFY hold-then-tap contract's host-side half (§2.2).

    Once the apply lands and the begin is ADMITTED, the redesigned page does
    not arm immediately — it renders the walk-back confirmation and waits for
    the household's tap. That wait sits in ``awaiting_arm``, whose budget is
    the general ``timeout_s`` (``DEFAULT_TIMEOUT_S`` = 120 s in production),
    NOT the tighter apply-hold budget the deferral used. Pin that a 60 s delay
    — the acceptance criterion — is comfortably tolerated, so the page's
    begin-first ordering cannot be undone by a budget nobody re-checked.
    """
    from jasper.capture_relay.session import DEFAULT_TIMEOUT_S, REVIEW_HOLD_BUDGET_S

    tap_delay_s = 60.0
    # The criterion only means anything because the apply-hold budget is
    # SHORTER than the tap: sitting in awaiting_begin instead would die.
    assert REVIEW_HOLD_BUDGET_S < tap_delay_s < DEFAULT_TIMEOUT_S

    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(
        backend, capture_target=2, max_attempts=4, entries=_review_plan_entries()
    )
    clock, monotonic, sleep = _steady_clock()
    authorized_at: dict[int, float] = {}

    def authorize(index, _attempt):
        authorized_at.setdefault(index, clock["t"])

    def on_armed(state):
        attempt = state.begin_capture["attempt"]
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt), index=attempt - 1
        )

    real_step = phone.step

    def step_taps_late_on_the_verify_entry(host_index=2):
        host = backend.sessions[session.session_id]["host_event"] or {}
        key = (host.get("index"), host.get("attempt"))
        if (
            host.get("phase") == "capture_authorized"
            and key == (host_index, 2)
            and clock["t"] < authorized_at.get(host_index, 0.0) + tap_delay_s
        ):
            return  # the household is walking back to the mark
        real_step()

    phone.step = step_taps_late_on_the_verify_entry

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=lambda _i, _a, _r: {"accepted": True},
        timeout_s=DEFAULT_TIMEOUT_S,
        poll_interval_s=0.0,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, True),
    ]
    assert clock["t"] >= authorized_at[2] + tap_delay_s


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


# --- transient status-poll outages (issue #2083) -------------------------------
#
# The status poll is the FIRST statement of the runner's loop, and it used to be
# bare: one 10 s HTTP stall raised straight out of a live measurement, the Pi
# purged the session, and the household — mid-walk, phone still on the step
# screen — was shown "Link expired". These pin the tolerance that fixes it AND
# the two boundaries that must survive it: a sustained outage still ends the
# session, and a dead-session answer is still fatal on the first read.


def _stalling_status(client, clock, *, stalls, stall_s, exc=None):
    """Make the next ``stalls`` status polls behave like a stalled HTTP call.

    Each one burns ``stall_s`` of the test clock (the way a real request that
    times out does — the wall time is spent INSIDE the call, which is exactly
    why the runner's grace is wall-clock rather than a retry count) and then
    raises. Returns a counter dict so a test can prove how many polls were
    actually attempted.
    """
    real_status = client.status
    budget = {"left": stalls}
    counts = {"stalled": 0, "ok": 0}

    def status(*args, **kwargs):
        if budget["left"] > 0:
            budget["left"] -= 1
            counts["stalled"] += 1
            clock["t"] += stall_s
            raise (exc if exc is not None else TimeoutError("relay stalled"))
        counts["ok"] += 1
        return real_status(*args, **kwargs)

    client.status = status
    return counts, budget


def _walk_clock():
    """A clock the RUNNER never advances on its own — only a stall or an
    explicit tick moves it — so a test's wall-time claims are exact."""
    clock = {"t": 0.0}
    return clock, (lambda: clock["t"]), (lambda _s: None)


def test_one_transient_status_stall_does_not_kill_a_live_session(caplog):
    """THE INCIDENT (issue #2083). A single 10 s stall on one status poll, in
    the middle of an otherwise healthy three-capture walk, must be survived —
    not turned into a dead session and a false "Link expired" screen."""
    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, authorized, _consumed = _plan_callbacks(
        backend, session
    )
    clock, monotonic, sleep = _walk_clock()
    counts, _budget = _stalling_status(client, clock, stalls=1, stall_s=10.0)

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(sleep=sleep, monotonic=monotonic),
    )

    # The walk finished, unchanged — same captures, same admissions.
    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
    ]
    assert authorized == [(1, 1), (2, 2), (3, 3)]
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"
    # The stall really did happen, and it is not silent: a tolerated outage is
    # still a WARNING naming how long it had been going.
    assert counts["stalled"] == 1
    assert "capture_relay.status_poll_transient" in caplog.text
    assert "capture_relay.status_poll_gave_up" not in caplog.text


def test_transient_status_failures_reset_on_every_success():
    """The grace is per-OUTAGE, not per-session. Two stalls far apart, each
    inside the window on its own, must BOTH be survived — which they only can
    be if a successful poll clears the counter. Without the reset the second
    stall is measured from the first and ends the session."""
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )
    clock, monotonic, sleep = _walk_clock()
    real_status = client.status
    # Two separate one-poll outages, each burning MOST of the grace, separated
    # by healthy polls. Their combined 20 s dwarfs the 15 s window, so a runner
    # that never reset would give up inside the second one.
    stall_at = {3, 9}
    polls = {"n": 0}
    counts = {"stalled": 0}

    def status(*args, **kwargs):
        polls["n"] += 1
        if polls["n"] in stall_at:
            counts["stalled"] += 1
            clock["t"] += 10.0
            raise TimeoutError("relay stalled")
        return real_status(*args, **kwargs)

    client.status = status

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(sleep=sleep, monotonic=monotonic),
    )

    assert counts["stalled"] == 2  # both outages were really exercised
    assert clock["t"] == 20.0 > STATUS_POLL_TRANSIENT_GRACE_S
    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
    ]


def test_a_flapping_relay_cannot_flood_the_journal(caplog):
    """A relay that alternates fail/succeed must not emit one WARNING per poll
    for the life of the session.

    The success-reset that makes the tolerance correct is exactly what defeats
    a per-outage dedupe here: every failure is the FIRST of a fresh outage, so
    the shape ``last_deferral`` uses for repeated deferrals would not bound
    this at all. The budget is session-wide; past it the lines drop to DEBUG
    and the running total keeps the scale visible.
    """
    caplog.set_level(logging.DEBUG, logger="jasper.capture_relay.session")
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )
    clock, monotonic, sleep = _walk_clock()
    real_status = client.status
    polls = {"n": 0}
    counts = {"stalled": 0}

    def status(*args, **kwargs):
        # Three failures per success, repeating. Every success resets the
        # grace, so the session survives indefinitely and the only thing that
        # accumulates is log lines — which is the subject here.
        polls["n"] += 1
        if polls["n"] % 4:
            counts["stalled"] += 1
            raise TimeoutError("relay flapping")
        return real_status(*args, **kwargs)

    client.status = status

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(sleep=sleep, monotonic=monotonic),
    )

    # The flap was survived end to end — every failure reset the grace, so the
    # session never gave up.
    assert [o.accepted for o in outcomes] == [True, True, True]
    transient = [
        r for r in caplog.records
        if "capture_relay.status_poll_transient" in r.getMessage()
    ]
    # Enough failures to matter, and MORE than the budget — otherwise this
    # would pass without the bound existing.
    assert counts["stalled"] > STATUS_POLL_WARN_BUDGET
    assert len(transient) == counts["stalled"]  # every one is still recorded…
    warnings = [r for r in transient if r.levelno == logging.WARNING]
    assert len(warnings) == STATUS_POLL_WARN_BUDGET  # …but the WARNINGs are capped
    # The scale stays readable after the level drops.
    assert f"tolerated_total={counts['stalled']}" in transient[-1].getMessage()


def test_a_sustained_status_outage_still_ends_the_session(caplog):
    """The other half of the promise. Tolerating a blip must not turn a relay
    that is genuinely gone into an unbounded poll: once the consecutive run
    outlasts the grace, the ORIGINAL exception is re-raised, so every caller
    that classifies it (``expired_time_budget``, the v2 host's relay-death arm)
    sees exactly what it always saw."""
    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )
    clock, monotonic, sleep = _walk_clock()
    counts, budget = _stalling_status(client, clock, stalls=99, stall_s=10.0)

    with pytest.raises(TimeoutError):
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(sleep=sleep, monotonic=monotonic),
        )

    # Bounded: it gave up on the SECOND stall (10 s is inside the 15 s window,
    # 20 s is past it), nowhere near the 99 it was offered.
    assert counts["stalled"] == 2
    assert budget["left"] == 97
    assert "capture_relay.status_poll_gave_up" in caplog.text


def test_a_dead_session_answer_is_never_retried():
    """The boundary the tolerance must not cross. 401/403/404/410 is the relay
    stating a fact that re-polling cannot change (it is how a purged or expired
    session answers — see ``expired_time_budget`` and the page's own
    ``isDeadSessionError``), so it stays fatal on the FIRST read. Retrying it
    would spend the grace re-asking a question already answered, and delay the
    honest terminal the household is waiting for."""
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend)
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )
    clock, monotonic, sleep = _walk_clock()
    counts, _budget = _stalling_status(
        client, clock, stalls=99, stall_s=10.0, exc=RelayError("gone", 404)
    )

    with pytest.raises(RelayError) as excinfo:
        run_capture_plan(
            client,
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(sleep=sleep, monotonic=monotonic),
        )

    assert excinfo.value.status == 404
    assert counts["stalled"] == 1  # raised on the first, never re-polled
    assert clock["t"] == 10.0  # and burned no extra grace doing it


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


# --- voluntary retakes (flow-simplification §2.6) -----------------------------


def test_a_voluntary_retake_re_measures_the_just_accepted_slot():
    """The ONE admitted extra begin shape, end-to-end through the real runner.

    A begin for ``accepted_count`` carrying ``retake: true`` is admitted, runs
    the full authorize → arm → upload → verdict path for that slot, and — this
    is the load-bearing part — does NOT advance ``accepted_count``. The set
    still needs its full ``capture_target`` distinct slots afterwards, so a
    retake can never finish a session early.
    """
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend, max_attempts=5)
    phone.retake_indexes = {2}
    authorize, on_armed, consume, authorized, _consumed = _plan_callbacks(
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

    assert [(o.index, o.attempt, o.accepted, o.retake) for o in outcomes] == [
        (1, 1, True, False),
        (2, 2, True, False),
        (2, 3, True, True),  # the retake — same slot, next attempt
        (3, 4, True, False),  # …and the set still runs its third slot
    ]
    assert authorized == [(1, 1), (2, 2), (2, 3), (3, 4)]
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"


def test_a_rejected_retake_leaves_the_original_take_standing():
    """Fail-safe: choosing to redo a measurement can never cost you the one
    you already had.

    The runner half of that promise is that a rejected retake neither rewinds
    ``accepted_count`` nor re-opens the slot — the session simply carries on to
    the next capture with the original take still counted. (The host half —
    REPLACING retained evidence only on acceptance — is the v2 conductor's,
    pinned in tests/test_crossover_v2_conductor.py.)
    """
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_plan_session(backend, max_attempts=5)
    phone.retake_indexes = {2}
    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session, verdicts={3: {"accepted": False, "code": "clipped"}},
    )

    outcomes = run_capture_plan(
        client,
        session,
        authorize_begin=authorize,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert [(o.index, o.attempt, o.accepted, o.retake) for o in outcomes] == [
        (1, 1, True, False),
        (2, 2, True, False),
        (2, 3, False, True),  # the retake failed…
        (3, 4, True, False),  # …and slot 2 stayed accepted regardless
    ]
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"


def _scripted_begin_client(backend, session, script):
    """A relay client whose every status poll first lets ``script`` post one
    phone event.

    ``script(host_event, post)`` is called with the Pi's latest host event and
    a ``post(payload)`` helper that signs and places a phone event; it returns
    nothing. Lets a test drive an exact begin sequence — including begins a
    well-behaved page would never send — without a full scripted phone.
    """
    seq = itertools.count(1)

    def post(payload):
        backend.phone_post(
            session.session_id,
            {**payload, "capture_page": dict(_PAGE)},
            session=session,
            sequence=next(seq),
        )

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            script(backend.sessions[session.session_id]["host_event"] or {}, post)
        return backend(method, url, headers, body)

    return RelayClient("https://relay.test", transport=transport)


def test_an_unmarked_begin_for_the_just_accepted_slot_is_still_refused():
    """The retake shape is opt-in and explicit: without the marker, a begin for
    an already-accepted index is the same out-of-order refusal it always was.

    This is what keeps an OLD page — which never sends the key — behaving
    exactly as it did before the contract grew.
    """
    backend = FakePlanRelayBackend()
    _client, session, _phone = _mint_plan_session(backend, driver=False)
    sent: set[str] = set()

    def script(host, post):
        phase = host.get("phase")
        if "begin" not in sent:
            sent.add("begin")
            post({"begin_capture": {"index": 1, "attempt": 1}})
        elif phase == "capture_authorized" and "arm" not in sent:
            sent.add("arm")
            post({
                "armed": True,
                "begin_capture": {"index": 1, "attempt": 1},
                "acknowledgement": {
                    "schema_version": 1,
                    "id": session.spec.acknowledgement.id,
                    "binding_id": session.spec.acknowledgement.binding_id,
                    "accepted": True,
                },
            })
        elif phase == "capture_result" and "again" not in sent:
            sent.add("again")
            # Slot 1 is accepted; re-begin it with NO retake marker.
            post({"begin_capture": {"index": 1, "attempt": 2}})

    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )
    with pytest.raises(CaptureFailed):
        run_capture_plan(
            _scripted_begin_client(backend, session, script),
            session,
            authorize_begin=authorize,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal["phase"] == "capture_refused"
    assert refusal["code"] == "begin_out_of_order"


def test_a_retake_is_refused_once_the_next_captures_begin_has_been_seen():
    """The retake window is "while awaiting the next entry's begin", and it
    really does shut.

    The case this guards is the v2 crossover apply hold: the household's
    "continue" tap posts the NEXT entry's begin, which the conductor DEFERS
    while the correction is fitted and applied. The runner stays in
    ``awaiting_begin`` throughout, so without this guard a late retake would
    still parse as ``index == accepted_count`` and land evidence after the
    cloud it belongs to had already been fitted.
    """
    backend = FakePlanRelayBackend()
    _client, session, _phone = _mint_plan_session(backend, driver=False)
    sent: set[str] = set()
    deferrals: list[int] = []

    def script(host, post):
        phase = host.get("phase")
        if "begin" not in sent:
            sent.add("begin")
            post({"begin_capture": {"index": 1, "attempt": 1}})
        elif phase == "capture_authorized" and "arm" not in sent:
            sent.add("arm")
            post({
                "armed": True,
                "begin_capture": {"index": 1, "attempt": 1},
                "acknowledgement": {
                    "schema_version": 1,
                    "id": session.spec.acknowledgement.id,
                    "binding_id": session.spec.acknowledgement.binding_id,
                    "accepted": True,
                },
            })
        elif phase == "capture_result" and "next" not in sent:
            sent.add("next")
            post({"begin_capture": {"index": 2, "attempt": 2}})
        elif phase == "capture_deferred" and "late" not in sent:
            sent.add("late")
            post({"begin_capture": {"index": 1, "attempt": 3, "retake": True}})

    authorize, on_armed, consume, _authorized, _consumed = _plan_callbacks(
        backend, session
    )

    def authorize_deferring_slot_2(index, attempt):
        if index == 2:
            deferrals.append(attempt)
            raise CaptureBeginDeferred("awaiting_apply", "Applying…")
        authorize(index, attempt)

    with pytest.raises(CaptureFailed):
        run_capture_plan(
            _scripted_begin_client(backend, session, script),
            session,
            authorize_begin=authorize_deferring_slot_2,
            on_armed=on_armed,
            consume_capture=consume,
            **_run_kwargs(),
        )

    assert deferrals, "the next entry's begin must actually have been seen"
    refusal = backend.sessions[session.session_id]["host_event"]
    assert refusal["phase"] == "capture_refused"
    assert refusal["code"] == "begin_out_of_order"


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
            "capture_page": dict(_PAGE),
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
        "capture_page": dict(_PAGE),
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


def test_run_capture_plan_requires_a_capture_plan_spec():
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
    with pytest.raises(CaptureFailed, match="requires a capture_plan"):
        run_capture_plan(
            client,
            session,
            authorize_begin=lambda _i, _a: None,
            on_armed=lambda: None,
            consume_capture=lambda _i, _a, _r: {"accepted": True},
            **_run_kwargs(),
        )


def test_plan_session_against_a_stale_page_fails_before_any_stimulus():
    backend = FakePlanRelayBackend()
    client, session, _phone = _mint_plan_session(backend, driver=False)
    backend.phone_post(
        session.session_id,
        {
            "begin_capture": {"index": 1, "attempt": 1},
            "capture_page": dict(_PAGE_STALE),
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


# --- single-capture path unchanged (contract) -----------------------------------


def test_single_capture_ignores_a_begin_capture_field():
    """The single-capture runner never reads `begin_capture` — a phone event
    carrying the plan field flows through the plan-free path untouched. This
    is the contract that lets ONE protocol serve both shapes: the runner is
    selected by `capture_plan` presence, not by a version number."""
    backend = FakePlanRelayBackend()
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
    )
    assert spec.capture_plan is None
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
            "begin_capture": {"index": 1, "attempt": 1},  # ignored, no plan
            "capture_page": dict(_PAGE),
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
    ) == (2, 3, False)
    # The voluntary-retake marker (flow-simplification §2.6) is the ONE
    # optional key, and only in its single true shape — a page that means "not
    # a retake" omits it, so there is exactly one wire shape per meaning.
    assert parse_begin_capture(
        {"index": 2, "attempt": 3, "retake": True},
        capture_target=3,
        max_attempts=4,
    ) == (2, 3, True)
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
        {"index": 1, "attempt": 1, "retake": False},  # omit it instead
        {"index": 1, "attempt": 1, "retake": "yes"},
        {"index": 1, "attempt": 1, "retake": 1},
        {"index": 1, "attempt": 1, "retake": True, "extra": True},
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
            "capture_page": dict(_PAGE),
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


# --- cross-deployment capacity gate (PR-3a) ----------------------------------
#
# `MAX_CAPTURE_PLAN_ATTEMPTS` is the Worker's blob-index space as well as the
# Pi's plan ceiling, and the two deploy independently (a Cloudflare Worker vs a
# Pi package). These tests pin the fail-closed direction: the Pi refuses an
# oversized plan at SESSION SETUP against a relay that cannot carry it, instead
# of discovering the skew when the phone's ninth upload 400s mid-choreography.


def _sized_plan(entries: int, *, max_attempts: int | None = None) -> CapturePlan:
    return CapturePlan(
        capture_target=entries,
        max_attempts=max_attempts if max_attempts is not None else entries,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(
                index=i, kind_label="cloud_measure", duration_ms=20_000
            )
            for i in range(entries)
        ),
    )


def _register_plan(backend, plan: CapturePlan, *, record: list | None = None):
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

    def transport(method, url, headers, body):
        if record is not None:
            record.append((method, urllib.parse.urlsplit(url).path))
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)
    register_session(client, session)
    return session


def test_oversized_plan_is_refused_against_a_pre_capacity_relay():
    # The skew case: a Worker deployed before GET /capabilities existed. Its
    # 404 is the version signal, and the Pi reads it as the legacy ceiling.
    backend = FakePlanRelayBackend(plan_ceiling=None)
    needed = LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS + 1
    with pytest.raises(RelayCapacityUnavailable) as excinfo:
        _register_plan(backend, _sized_plan(needed))
    message = str(excinfo.value)
    # Actionable: what is deployed, what is needed, and the fix.
    assert "predates the capture-plan capacity release" in message
    assert "no /capabilities endpoint" in message
    assert str(LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS) in message
    assert str(needed) in message
    assert "wrangler deploy" in message
    assert "https://relay.test" in message
    # And the pre-capacity relay never received the oversized spec at all.
    assert backend.sessions == {}


def test_oversized_plan_is_refused_when_the_relay_advertises_too_small_a_ceiling():
    # A relay that DOES version itself but is behind the Pi.
    backend = FakePlanRelayBackend(plan_ceiling=16)
    with pytest.raises(RelayCapacityUnavailable) as excinfo:
        _register_plan(backend, _sized_plan(21))
    assert "advertises a capture-plan ceiling of 16" in str(excinfo.value)
    assert backend.sessions == {}


def _capabilities_override(response: RelayResponse):
    """A backend whose /capabilities answers with a fixed canned response."""

    class Overridden(FakePlanRelayBackend):
        def __call__(self, method, url, headers, body):
            if urllib.parse.urlsplit(url).path == "/capabilities":
                return response
            return super().__call__(method, url, headers, body)

    return Overridden()


def test_oversized_plan_is_refused_on_a_malformed_capabilities_document():
    # Fail CLOSED like a missing document — but say something DIFFERENT, since
    # "your relay is broken" and "your relay is old" need different fixes.
    backend = _capabilities_override(
        RelayResponse(
            200,
            {},
            json.dumps(
                {"schema_version": 1, "max_capture_plan_attempts": "lots"}
            ).encode(),
        )
    )
    with pytest.raises(RelayCapacityUnavailable) as excinfo:
        _register_plan(backend, _sized_plan(21))
    message = str(excinfo.value)
    assert "no usable ceiling" in message
    assert "predates" not in message
    assert "Redeploy the relay Worker" in message
    assert backend.sessions == {}


def test_oversized_plan_is_refused_and_names_interception_on_a_non_json_200():
    """An HTML interstitial must not be reported as an un-deployed relay.

    A captive portal / proxy / WAF answering 2xx for `/capabilities` is fixed
    by NEITHER deploying nor redeploying the Worker, so blaming the release
    would send the operator down the wrong path. This is the one refusal whose
    remedy is not a wrangler command.
    """
    backend = _capabilities_override(
        RelayResponse(200, {}, b"<html><body>Sign in to continue</body></html>")
    )
    with pytest.raises(RelayCapacityUnavailable) as excinfo:
        _register_plan(backend, _sized_plan(21))
    message = str(excinfo.value)
    assert "intercepting" in message
    assert "Check the network path to the relay" in message
    assert "predates" not in message
    assert "wrangler" not in message
    assert backend.sessions == {}


def _tap_position_plan(entries: int) -> CapturePlan:
    """An N-entry plan whose every entry is a prompted, tap-gated position.

    The transport shape the crossover cloud emits: one entry per prompted mic
    move, each carrying its own screen copy and gating on the operator's tap.
    No new phone-side mechanism is involved — ``screen`` + ``auto_advance`` are
    the shipped per-entry fields — so this test's job is to prove the RUNNER
    walks an arbitrary-length tap plan, index by index, exposing each entry.
    """
    return CapturePlan(
        capture_target=entries,
        max_attempts=entries + 4,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(
                index=i,
                kind_label="cloud_measure",
                duration_ms=16_000,
                screen={
                    "title": f"Spot {i + 1} of {entries}",
                    "body": f"Move the phone to spot {i + 1}.",
                    "auto_advance": "tap",
                },
            )
            for i in range(entries)
        ),
    )


@pytest.mark.parametrize("entries", [6, 15, 19])
def test_runner_walks_an_n_entry_tap_plan_exposing_every_entry(entries):
    backend = FakePlanRelayBackend()
    plan = _tap_position_plan(entries)
    client, session, phone = _mint_plan_session(
        backend, capture_target=plan.capture_target,
        max_attempts=plan.max_attempts, entries=plan.entries,
    )
    authorize, on_armed, consume, authorized, consumed = _plan_callbacks(
        backend, session
    )
    seen: list = []

    def authorize_with_entry(index, attempt, entry=None):
        seen.append((index, attempt, entry.screen["title"] if entry else None))
        authorize(index, attempt)

    outcomes = run_capture_plan(
        client, session,
        authorize_begin=authorize_with_entry,
        on_armed=on_armed,
        consume_capture=consume,
        **_run_kwargs(),
    )

    assert len(outcomes) == entries
    assert all(outcome.accepted for outcome in outcomes)
    # index == accepted_count + 1 and attempt is the GLOBAL counter, so a clean
    # walk pairs them 1:1 — and every entry's own screen reached the callback.
    assert seen == [
        (i + 1, i + 1, f"Spot {i + 1} of {entries}") for i in range(entries)
    ]
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"
    # One blob key per attempt, all inside the relay's index space.
    assert [index for index, _a, _w in consumed] == list(range(1, entries + 1))


def test_runner_retakes_one_position_mid_plan_without_losing_the_others():
    """A rejected position spends an attempt but not a target slot, so the phone
    re-posts the SAME index with the next attempt — the mechanism the geometry
    retake rides. Blob indexes follow the ATTEMPT, so a mid-plan retake shifts
    every later blob index up by one."""
    backend = FakePlanRelayBackend()
    plan = _tap_position_plan(8)
    client, session, phone = _mint_plan_session(
        backend, capture_target=plan.capture_target,
        max_attempts=plan.max_attempts, entries=plan.entries,
    )
    authorize, on_armed, consume, authorized, consumed = _plan_callbacks(
        # `_plan_callbacks` keys verdicts by ATTEMPT: reject the 4th, which at
        # this point in a clean walk is position 4's first take.
        backend, session, verdicts={4: {"accepted": False, "code": "retake"}},
    )
    outcomes = run_capture_plan(
        client, session,
        authorize_begin=authorize, on_armed=on_armed, consume_capture=consume,
        **_run_kwargs(),
    )

    assert [(o.index, o.attempt, o.accepted) for o in outcomes] == [
        (1, 1, True), (2, 2, True), (3, 3, True),
        (4, 4, False), (4, 5, True),
        (5, 6, True), (6, 7, True), (7, 8, True), (8, 9, True),
    ]
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"


def test_oversized_plan_registers_against_a_current_relay():
    # The happy path the whole gate exists to permit: PR-3b's worst-case entry
    # count (21) with the full retake budget, against a deployed Worker.
    backend = FakePlanRelayBackend()
    plan = _sized_plan(21, max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS)
    record: list = []
    session = _register_plan(backend, plan, record=record)
    assert session.session_id in backend.sessions
    assert ("GET", "/capabilities") in record
    stored = json.loads(backend.sessions[session.session_id]["capture_spec"])
    assert stored["capture_plan"]["capture_target"] == 21
    assert stored["capture_plan"]["max_attempts"] == MAX_CAPTURE_PLAN_ATTEMPTS


@pytest.mark.parametrize("entries", [1, 3], ids=["re-verify", "driver-set"])
def test_shipped_plan_sizes_register_without_probing_capabilities(entries):
    """The existing 3-entry and 1-entry flows stay byte-identical on the wire.

    A plan at or below the legacy ceiling issues the exact request sequence it
    always has — no capabilities GET — so this change is invisible to every
    flow a pre-capacity Pi could already emit, and those flows keep working
    against a pre-capacity relay.
    """
    backend = FakePlanRelayBackend(plan_ceiling=None)
    record: list = []
    plan = _sized_plan(entries, max_attempts=LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS)
    session = _register_plan(backend, plan, record=record)
    assert record == [("POST", "/sessions")]
    assert session.session_id in backend.sessions
