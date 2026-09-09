# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/sound/pair/ — the "Stereo pair" surface: directory + wake-response toggle.

Discovery browses the ALWAYS-ON `_jasper-control._tcp` mDNS service
(advertised unconditionally by deploy/avahi/jasper-control.service) — NOT
the wake-peering-gated `_jasper-peer._udp`, which only exists when
JASPER_PEERING=on. So the directory lists every speaker regardless of
whether wake-peering is enabled. :mod:`jasper.web.rooms_peers` owns that
browse and every cross-speaker control call this module fans out.

Room is NOT edited here: the speaker-identity home (/speaker/) owns name +
room; this page only reads identity and links there.

POST /peering read-modify-writes /var/lib/jasper/peering.env, REUSING
jasper.peering.config's readers/constants so the env parse contract keeps
one owner.

The page renders client-side: the body is a single `#app` mount point plus
the ES module at /assets/rooms/js/main.js, which fetches /rooms.json on load
and every 7 s. Every peer field is mDNS-provided (untrusted), so this server
interpolates none of the discovered data.

URL surface (after nginx strips the /sound/pair/ prefix); every POST is
CSRF-verified:
  GET  /            page render (mount point + ES module)
  GET  /rooms.json  the directory + self status incl. the wake-response
                    `peering` block
  POST /peering     write the wake-response state into peering.env +
                    restart voice/control
  POST /bond        form a stereo pair from {peer_addr}; the server mints a
                    bond id, builds the member plan, then fans the grouping
                    config out SERVER-side to each member's jasper-control.
                    Before any write, every enabled member must return
                    readiness.allowed=true from lightweight GET /grouping;
                    POST /grouping/set rechecks the same target-side guard.
                    Advanced callers may still post a full {members:[...]}
                    body for same-bond edits.
  POST /unbond      dissolve this speaker's bond: disable self + every
                    sibling sharing this bond_id
  POST /swap        exchange a 2-speaker pair's left/right channels; roles
                    and bond untouched
  POST /trim        set the pair balance absolutely (target=pair,
                    balance_db)
"""
from __future__ import annotations

import asyncio  # noqa: F401 — kept so tests can patch rooms_setup.asyncio.run
import logging
import math
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..identity import reader as identity
from ..control import household_credential
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom.state import read_grouping_state
from ..peering import config as peering_config
from ..log_event import log_event
from ..env_file import write_env_file
from . import rooms_peers
from ._common import (
    begin_request,
    dispatch_get,
    dispatch_post,
    read_json_body,
    restart_voice_daemon,
    restart_systemd_units,
    send_html_response,
    send_json_response,
)
from .chrome import canonical_header, canonical_page

logger = logging.getLogger(__name__)


ROOMS_PAGE_CSS_HREF = "/assets/rooms/rooms.css"

# Short so a powered-off paired speaker cannot stall the 7 s /rooms.json poll.
# Mutations keep the full control timeout; the snapshot only needs
# fresh-enough display state and can surface "unavailable" quickly.
BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC = 0.75

# Only slow snapshots log, so a left-open tab does not spam the journal.
ROOMS_SNAPSHOT_SLOW_MS = 1000


def _read_peering_block() -> dict:
    """This speaker's wake-response (peering) state for /rooms.json, read
    FRESH from /var/lib/jasper/peering.env on every call. REUSES
    jasper.peering.config readers — the env parse is NOT re-derived here.

    Returns {"enabled": bool, "primary": bool}:
      enabled  — JASPER_PEERING is on (the speaker joins wake arbitration so
                 only one device answers "Hey Jarvis").
      primary  — JASPER_PEER_PRIMARY is set (small bias to win ties).

    Fail-soft: peering_config.read_state returns {} on a missing/unreadable
    file, so this never raises.

    The path is passed explicitly so it resolves at call time — fresh on
    every poll, and patchable via peering_config.PEERING_ENV_FILE."""
    state = peering_config.read_state(peering_config.PEERING_ENV_FILE)
    return {
        "enabled": peering_config.state_enabled(state),
        "primary": peering_config.state_primary(state),
    }


def _build_rooms_payload() -> dict:
    """Assemble the /rooms.json body: this speaker's identity + grouping
    status + wake-response state, plus the sibling directory (self
    excluded). The discovery, grouping, and peering reads are each
    fail-soft, so this never raises.

    Shape (consumed by /assets/rooms/js/main.js):
      {
        "self": {name, hostname, room, address,
                 grouping: <read_grouping_state() dict
                            + airplay_latency_fit: {applicable, tight?, …}
                            + balance: {applicable, ok?, balance_db?, …}>,
                 peering: {enabled, primary}},
        "peers": [{name, room, address, home_url, system_url}, ...]
      }

    Peer `address` stays raw LAN IP for POST /bond / /swap / /trim control
    calls. Peer `home_url` / `system_url` are derived from the advertised
    hostname and end in `.local`, never from the IP address.
    """
    started = time.perf_counter()
    stages: dict[str, int] = {}

    stage = time.perf_counter()
    me = identity.read_identity()
    stages["identity_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    own = rooms_peers.self_addresses()
    stages["self_addr_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    grouping = with_airplay_latency_fit(read_grouping_state())
    stages["grouping_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    balance = _pair_balance_snapshot(grouping, own)
    stages["balance_ms"] = round((time.perf_counter() - stage) * 1000)
    if balance.get("applicable"):
        grouping = dict(grouping)
        grouping["balance"] = balance

    stage = time.perf_counter()
    peering = _read_peering_block()
    stages["peering_ms"] = round((time.perf_counter() - stage) * 1000)
    self_addr = rooms_peers._self_address(own)
    self_block = {
        "name": me.name,
        "hostname": me.hostname,
        "room": me.room,
        "address": self_addr,
        "grouping": grouping,
        "peering": peering,
    }

    stage = time.perf_counter()
    discovered = rooms_peers.discover_speakers_cached()
    stages["discovery_ms"] = round((time.perf_counter() - stage) * 1000)

    peers: list[dict] = []
    self_hostname_label = me.hostname.split(".")[0].casefold()
    for s in discovered:
        addr = s.get("address") or ""
        # Drop self by address, then by EXACT SRV-hostname match for the case
        # where the UDP-route trick missed our address. The hostname match
        # must stay exact and off the free-form display name: substring
        # matching once made a speaker "jts" drop a peer "jts3".
        if addr and addr in own:
            continue
        peer_host = (s.get("hostname") or "").casefold()
        if self_hostname_label and peer_host == self_hostname_label:
            continue
        web_host = rooms_peers._local_web_host(s.get("hostname") or "")
        peers.append(
            {
                "name": s.get("name") or "",
                "room": s.get("room") or "",
                "address": addr,
                "home_url": f"http://{web_host}/" if web_host else "",
                "system_url": f"http://{web_host}/system/" if web_host else "",
            }
        )
    peers.sort(
        key=lambda p: (p.get("room") or "", p.get("name") or "", p.get("address") or "")
    )
    payload = {
        "self": self_block,
        "peers": peers,
        "view": _rooms_view(grouping, peers, self_addr),
    }
    total_ms = round((time.perf_counter() - started) * 1000)
    if total_ms >= ROOMS_SNAPSHOT_SLOW_MS:
        log_event(
            logger,
            "rooms.snapshot",
            total_ms=total_ms,
            peer_count=len(peers),
            **stages,
        )
    return payload


def _rooms_view(grouping: dict, peers: list[dict], self_addr: str) -> dict:
    """Backend-owned view model for the rooms page, so the browser does not
    rediscover the grouping state machine or decide which advanced topology
    controls belong in the primary flow.
    """
    bonded = bool(grouping.get("enabled") and grouping.get("bond_id"))
    if bonded and grouping.get("error"):
        state = "degraded"
    elif bonded:
        state = "paired"
    else:
        state = "solo"
    balance = grouping.get("balance")
    can_balance = (
        state == "paired"
        and isinstance(balance, dict)
        and bool(balance.get("applicable"))
        and bool(balance.get("ok"))
    )
    return {
        "state": state,
        "bonded": bonded,
        "can_create_pair": (
            state == "solo"
            and bool(self_addr)
            and any(p.get("address") for p in peers)
        ),
        "can_balance_pair": can_balance,
    }


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------
#
# Page-specific visuals live in deploy/assets/rooms/rooms.css
# (page_css_href); shared component classes come from /assets/app.css. No
# inline <script> with behaviour — only the type="module" loader tag.


def _render_page(*, csrf_token: str = "") -> bytes:
    # `id="app"` is the mount contract with the ES module, which clears the
    # placeholder on first render so a failed module load degrades to a
    # message rather than a blank page.
    #
    # canonical_page emits the CSRF <meta name="jts-csrf"> tag the ES module
    # reads (via http.js jsonHeaders()) for the wake-response POST /peering.
    body = f"""
{canonical_header("Stereo pair", back_href="/sound/", back_label="Sound")}
<main class="page">
  <div id="app" aria-busy="true">
    <p class="rooms-loading">Looking for speakers on this network…</p>
  </div>
</main>
<script type="module" src="/assets/rooms/js/main.js"></script>
"""
    return canonical_page(
        "Stereo pair", body,
        csrf_token=csrf_token,
        page_css_href=ROOMS_PAGE_CSS_HREF,
    )


# ----------------------------------------------------------------------
# Handlers.
# ----------------------------------------------------------------------


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, *, status: int = 200) -> None:
    send_json_response(handler, payload, status=status)


# Max JSON body on the POST routes; the real payloads are ~30 B, so anything
# larger is rejected before it is read off the wire.
_PEERING_BODY_LIMIT = 4096


def _save_peering(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /peering: write the wake-response state into peering.env
    and restart voice + jasper-control so both daemons pick it up.

    REUSES jasper.peering.config for the PEERING_ENV_FILE and state readers
    so there is ONE owner of the peering env contract.

    Read-modify-write: write_env_file does a full-file replace, so without
    the merge a save would clobber JASPER_PEER_ROOM (owned by /speaker/) and
    operator-set arbitration knobs like JASPER_PEER_ARB_WINDOW_MS.

    Fail-soft: a parse/IO error returns a 4xx/5xx JSON error and never raises
    out of the handler."""
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        log_event(logger, "rooms.peering.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    enabled = bool(parsed.get("enabled"))
    primary = bool(parsed.get("primary"))

    # Resolve the path ONCE so the merge cannot read one file and write
    # another, which would clobber the keys it means to preserve.
    env_path = peering_config.PEERING_ENV_FILE

    values: dict[str, str] = dict(peering_config.read_state(env_path))
    values["JASPER_PEERING"] = "on" if enabled else "off"
    if primary:
        values["JASPER_PEER_PRIMARY"] = "1"
    elif "JASPER_PEER_PRIMARY" in values:
        del values["JASPER_PEER_PRIMARY"]

    try:
        # mode=0o644 — no secrets, just config.
        write_env_file(env_path, values, mode=0o644)
    except OSError as e:
        log_event(logger, "rooms.peering.save.error", level=logging.ERROR, exc_info=True)
        _send_json(
            handler, {"ok": False, "error": f"write failed: {e}"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return

    log_event(
        logger,
        "rooms.peering.save",
        mode=values["JASPER_PEERING"],
        primary=int(primary),
    )

    # jasper-voice reads JASPER_PEERING to know whether to call the peering
    # UDS; jasper-control reads it to know whether to start its peering daemon
    # thread. Both restarts are best-effort and non-blocking.
    restart_voice_daemon()
    restart_systemd_units("jasper-control")

    _send_json(
        handler,
        {"ok": True, "peering": {"enabled": enabled, "primary": primary}},
    )


def _save_bond(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /bond: form a bond by configuring every member's role.

    The browser sends ``{peer_addr}`` and the backend builds the stereo
    topology (this speaker leader/left, peer follower/right); advanced
    same-bond edits may send ``{members: [...]}`` explicitly. The leader is
    always this speaker, so followers get its STABLE mDNS handle
    (:func:`_leader_handle`) as ``leader_addr``, never a NIC IP.

    A partial failure is surfaced per member, not auto-rolled-back — the
    household retries, and `/state` shows the half-formed bond as degraded.
    """
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        log_event(logger, "rooms.bond.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    members = parsed.get("members")
    if members is None:
        peer_addr = str(parsed.get("peer_addr") or "").strip()
        if not peer_addr:
            _send_json(
                handler,
                {"ok": False, "error": "peer_addr is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        members = rooms_peers._stereo_pair_members_from_intent(peer_addr)
    if not isinstance(members, list) or not members:
        _send_json(
            handler, {"ok": False, "error": "members must be a non-empty list"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    requested_bond_id = str(parsed.get("bond_id") or "").strip()
    fresh_bond = not requested_bond_id
    bond_id = requested_bond_id or rooms_peers._generate_bond_id()
    leader_addr = rooms_peers._leader_handle()

    # Record each member's slot so the positional results from
    # _fan_out_grouping pair back to the right member.
    results: list[dict] = [None] * len(members)  # type: ignore[list-item]
    targets: list[tuple[str, dict]] = []
    target_idx: list[int] = []
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            results[i] = {"ok": False, "detail": "member must be an object"}
            continue
        addr = str(m.get("addr") or "").strip()
        role = str(m.get("role") or "").strip()
        channel = str(m.get("channel") or "").strip()
        body = {
            "enabled": True,
            "role": role,
            "channel": channel,
            "bond_id": bond_id,
            "leader_addr": "" if role == "leader" else leader_addr,
            # Explicit empties CLEAR stale state: a member that led a previous
            # bond must not keep pointing at its old sibling or roster. The
            # leader gets the real peer/roster below.
            "peer_addr": "",
            "peer_name": "",
            "roster": [],
        }
        if fresh_bond:
            # A new pair must not inherit stale balance trim from a previous
            # bond/unbond cycle. Existing-bond edits omit trim_db so a
            # calibrated L/R balance is preserved.
            body["trim_db"] = 0.0
        if role == "leader":
            # The LEADER records every OTHER member so _unbond can disable ALL
            # of them. peer_addr / peer_name stay the PRIMARY L/R sibling so
            # swap/trim/balance keep operating on the stereo pair.
            roster: list[dict] = []
            for j, mm in enumerate(members):
                if j == i or not isinstance(mm, dict):
                    continue
                m_addr = str(mm.get("addr") or "").strip()
                if not m_addr:
                    continue
                roster.append({
                    "addr": m_addr,
                    "name": str(mm.get("name") or "").strip(),
                    "channel": str(mm.get("channel") or "").strip(),
                })
            body["roster"] = roster
            others = [
                mm for j, mm in enumerate(members)
                if j != i and isinstance(mm, dict)
                and str(mm.get("addr") or "").strip()
            ]
            primary = next(
                (mm for mm in others
                 if str(mm.get("channel") or "").strip() in ("left", "right")),
                others[0] if others else None,
            )
            if primary is not None:
                body["peer_addr"] = str(primary.get("addr") or "").strip()
                body["peer_name"] = str(primary.get("name") or "").strip()
        targets.append((addr, body))
        target_idx.append(i)

    known = rooms_peers.self_addresses()
    preflight = rooms_peers._map_peers(
        lambda t: rooms_peers._preflight_grouping_target(t[0], t[1], known),
        targets,
    )
    blocked = [
        {
            "addr": addr,
            "role": body.get("role"),
            "ok": ok,
            "detail": detail,
        }
        for (addr, body), (ok, detail) in zip(targets, preflight)
        if not ok
    ]
    if blocked:
        for r in blocked:
            log_event(
                logger,
                "rooms.bond.preflight_failed",
                bond=bond_id,
                addr=r.get("addr") or "?",
                role=r.get("role") or "?",
                detail=r["detail"],
                level=logging.WARNING,
            )
        _send_json(
            handler,
            {
                "ok": False,
                "bond_id": bond_id,
                "error": "one or more speakers are not ready to join a group",
                "results": blocked,
            },
            status=HTTPStatus.CONFLICT,
        )
        return

    # Mint the household credential BEFORE the fan-out so each member's
    # /grouping/set carries it and adopts it on receipt, locking down every
    # subsequent cross-device grouping change. Idempotent: re-bonding the same
    # household reuses the existing secret.
    try:
        household_credential.ensure()
    except OSError as exc:
        # A write failure must not fail the bond: members fail-safe-accept, so
        # the bond still forms with the credential unminted, leaving
        # /grouping/set open until a later bond succeeds. The WARN plus the
        # doctor's "bonded but household credential missing" check surface the
        # degraded auth.
        log_event(
            logger, "household_credential.ensure_failed",
            error=str(exc), level=logging.WARNING,
        )
    token = rooms_peers.request_control_token(handler)
    for slot, (addr, body), (ok, detail) in zip(
        target_idx, targets, rooms_peers._fan_out_grouping(targets, known=known, token=token)
    ):
        results[slot] = {"addr": addr, "role": body["role"], "ok": ok, "detail": detail}

    all_ok = all(r["ok"] for r in results)
    # On a headless speaker the HTTP response is not a diagnostic surface, so
    # a half-formed bond must name WHICH member failed and WHY in the journal.
    # Failures only, so a healthy pair logs nothing here.
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.bond.member_failed",
                bond=bond_id,
                addr=r.get("addr") or "?",
                role=r.get("role") or "?",
                detail=r["detail"],
                level=logging.WARNING,
            )
    log_event(
        logger,
        "rooms.bond.save",
        bond=bond_id,
        members=len(members),
        ok=all_ok,
    )
    _send_json(
        handler,
        {"ok": all_ok, "bond_id": bond_id, "results": results},
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


def _unbond(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /unbond: dissolve the bond THIS speaker is in.

    A peer in a DIFFERENT bond is left alone, never disabled. Self is ALWAYS
    in the disable set, so "leave the bond" works locally even when no peer is
    reachable: HTTP 200 when self disabled OK, 502 otherwise."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    # Self is excluded from the candidates (it is in `known`) and disabled
    # explicitly below, not rediscovered. An unreachable peer, or one in a
    # different bond, is simply not added to the disable set.
    known = rooms_peers.self_addresses()
    roster = grouping.get("roster")
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    candidate_groupings: list = []
    if isinstance(roster, list) and roster:
        # The leader recorded EVERY follower at bond time, so the roster is
        # authoritative: no orphaned follower and no foreign-claimer ambiguity.
        # The disable is aimed at each recorded address even when offline, so
        # a powered-off follower is not left stranded.
        peer_addrs = [
            a for a in (
                str(m.get("addr") or "").strip()
                for m in roster if isinstance(m, dict)
            ) if a
        ]
    elif roster_addr:
        # Legacy pair-roster: disable exactly the recorded sibling, never a
        # foreign device that happens to claim our bond_id — a transient
        # claimer would get its grouping DISABLED, worse than the read-path
        # ambiguity. When the resolver cannot confirm the peer, the disable is
        # still aimed at its last known address and the fan-out reports the
        # failure.
        resolved_addr, _pg, _err = resolve_bond_peer(grouping, known)
        peer_addrs = [resolved_addr or roster_addr]
    else:
        candidate_addrs = [
            a for a in (
                str(s.get("address") or "").strip()
                for s in rooms_peers.discover_speakers_cached()
            ) if a and a not in known
        ]
        candidate_groupings = rooms_peers._map_peers(
            lambda a: rooms_peers._get_member_grouping(a, known), candidate_addrs,
        )
        peer_addrs = [
            a for a, pg in zip(candidate_addrs, candidate_groupings)
            if pg is not None
            and str(pg.get("bond_id") or "").strip() == bond_id
        ]

    # Self first (empty addr → loopback), then each matching peer.
    disabled_body = {"enabled": False, "trim_db": 0.0}
    targets: list[tuple[str, dict]] = [("", dict(disabled_body))]
    targets += [(addr, dict(disabled_body)) for addr in peer_addrs]
    addrs = [t[0] for t in targets]

    # Read the household credential ONCE before the fan-out: each member's
    # /grouping/set (enabled=false) clears its own secret, and self (loopback)
    # clears ours, so a per-member live read could race the clear and strip a
    # peer of the credential it needs to authenticate the very unbond that
    # dissolves it.
    household = household_credential.current()
    fan_results = rooms_peers._fan_out_grouping(
        targets, known=known, token=rooms_peers.request_control_token(handler),
        household=household,
    )
    results = [
        {"addr": addr, "ok": ok, "detail": detail}
        for addr, (ok, detail) in zip(addrs, fan_results)
    ]
    dissolved = [r["addr"] for r in results if r["ok"]]
    self_ok = results[0]["ok"]  # self is always targets[0]

    # Name each member we could not disable so a half-dissolved bond — a
    # follower offline at dissolve time, left stranded — is visible in the
    # journal, not just in the aggregate.
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.unbond.member_failed",
                bond=bond_id,
                addr=r["addr"] or "(self)",
                detail=r["detail"],
                level=logging.WARNING,
            )
    # Candidates whose discovery GET failed: a same-bond follower offline at
    # dissolve time never becomes a disable target and stays grouped, so the
    # count explains that report without a per-candidate line.
    unreachable = sum(1 for pg in candidate_groupings if pg is None)
    log_event(
        logger,
        "rooms.unbond",
        bond=bond_id,
        # Keyed on the branch taken, not on the legacy peer_addr, which a
        # full-roster bond also sets to its primary L/R sibling.
        path=(
            "full" if (isinstance(roster, list) and roster)
            else "legacy" if roster_addr
            else "discovery"
        ),
        roster_n=len(roster or []),
        unreachable=unreachable,
        peers=len(peer_addrs),
        self_ok=self_ok,
        dissolved=len(dissolved),
    )
    _send_json(
        handler,
        {"ok": self_ok, "bond_id": bond_id, "dissolved": dissolved, "results": results},
        status=HTTPStatus.OK if self_ok else HTTPStatus.BAD_GATEWAY,
    )


def resolve_bond_peer(
    grouping: dict, known: set[str] | None = None, *,
    grouping_reader=None,
) -> tuple[str, dict | None, str]:
    """Resolve THIS speaker's one pair sibling → (addr, peer_grouping, err).

    Roster-first: the bond flow records the chosen peer on the leader
    (``peer_addr`` + ``peer_name`` in grouping.env), so pair operations
    resolve THE peer the household picked. When the recorded IP no longer
    answers for OUR bond, a recorded ``peer_name`` is re-found in the live
    directory (DHCP moved the IP). With a roster, a FOREIGN device
    transiently claiming our bond_id cannot create ambiguity — that was the
    observed failure mode, a device cycling through bond states making
    swap/trim/balance fail with "found 2" — and an unreachable roster peer is
    a hard, NAMED error, never an excuse to guess. Bonds recorded before the
    roster existed fall back to the legacy inference (every discovered device
    claiming our bond_id), which still errors on ambiguity.

    ``grouping_reader`` is the one I/O policy seam: mutations use the default
    full-timeout reader, read-only UI snapshots a shorter one, without
    duplicating peer-resolution rules.

    ``err`` is "" on success; on failure addr is "" and grouping None.
    """
    if known is None:
        known = rooms_peers.self_addresses()
    read_grouping = grouping_reader or rooms_peers._get_member_grouping
    bond_id = str(grouping.get("bond_id") or "").strip()
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    roster_name = str(grouping.get("peer_name") or "").strip()

    if roster_addr:
        pg = read_grouping(roster_addr, known)
        if (pg is not None
                and str(pg.get("bond_id") or "").strip() == bond_id):
            return roster_addr, pg, ""
        if roster_name:
            for row in rooms_peers.discover_speakers_cached():
                if str(row.get("name") or "").strip() != roster_name:
                    continue
                addr = str(row.get("address") or "").strip()
                if not addr or addr in known or addr == roster_addr:
                    continue
                pg2 = read_grouping(addr, known)
                if (pg2 is not None
                        and str(pg2.get("bond_id") or "").strip()
                        == bond_id):
                    log_event(
                        logger,
                        "rooms.peer_addr_drift",
                        name=roster_name,
                        old=roster_addr,
                        new=addr,
                    )
                    return addr, pg2, ""
        label = roster_name or roster_addr
        return "", None, (
            f"paired speaker '{label}' is unreachable (last known "
            f"{roster_addr}) — check its power and network, or re-pair "
            "at /rooms"
        )

    candidate_addrs = [
        a for a in (
            str(sp.get("address") or "").strip()
            for sp in rooms_peers.discover_speakers_cached()
        ) if a and a not in known
    ]
    candidate_groupings = rooms_peers._map_peers(
        lambda a: read_grouping(a, known), candidate_addrs,
    )
    peers = [
        (a, pg) for a, pg in zip(candidate_addrs, candidate_groupings)
        if pg is not None
        and str(pg.get("bond_id") or "").strip() == bond_id
    ]
    if len(peers) != 1:
        return "", None, (
            "needs exactly one reachable paired speaker "
            f"(found {len(peers)}) — re-pairing at /rooms records the "
            "pair and removes the ambiguity"
        )
    return peers[0][0], peers[0][1], ""


def _swap_channels(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /swap: exchange the two members' channels (left ↔ right).

    A channel-assignment edit, never a leadership change: roles, bond_id and
    leader_addr are untouched. Each member's outputd ChannelPick drops the
    other side once its reconciler applies the change (about a one-period
    blip per speaker).

    Deliberately scoped to the 2-speaker left/right pair: it requires exactly
    ONE same-bond peer and a {left, right} channel set, since a mono or
    multi-member bond has no well-defined "swap" and 400s with the reason."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    known = rooms_peers.self_addresses()
    peer_addr_r, peer_grouping, perr = resolve_bond_peer(grouping, known)
    if perr:
        _send_json(
            handler,
            {"ok": False, "error": f"channel swap {perr}"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    peers = [(peer_addr_r, peer_grouping)]

    peer_addr, peer_grouping = peers[0]
    self_channel = str(grouping.get("channel") or "").strip()
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    repairing = (
        self_channel == peer_channel and self_channel in ("left", "right")
    )
    if repairing:
        # A same-channel pair is the residue of an interrupted swap whose
        # rollback also failed. A strict left/right precondition would make
        # Swap the one button that CANNOT fix the state Swap created, so this
        # completes the interrupted intent instead: any {left,right}
        # assignment beats a stuck same-channel pair, and one more tap swaps
        # again if it lands backwards.
        swapped_self, swapped_peer = self_channel, (
            "right" if self_channel == "left" else "left"
        )
    elif {self_channel, peer_channel} == {"left", "right"}:
        swapped_self, swapped_peer = peer_channel, self_channel
    else:
        _send_json(
            handler,
            {"ok": False, "error": (
                "channel swap needs a left/right pair (this speaker is "
                f"{self_channel or '?'}, peer is {peer_channel or '?'})"
            )},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    def _body(g: dict, channel: str) -> dict:
        return {
            "enabled": True,
            "role": str(g.get("role") or ""),
            "channel": channel,
            "bond_id": bond_id,
            "leader_addr": str(g.get("leader_addr") or ""),
        }

    targets: list[tuple[str, dict]] = [
        ("", _body(grouping, swapped_self)),
        (peer_addr, _body(peer_grouping, swapped_peer)),
    ]
    token = rooms_peers.request_control_token(handler)
    fan_results = rooms_peers._fan_out_grouping(targets, known=known, token=token)
    results = [
        {"addr": addr, "channel": body["channel"], "ok": ok, "detail": detail}
        for (addr, body), (ok, detail) in zip(targets, fan_results)
    ]
    all_ok = all(r["ok"] for r in results)
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.swap.member_failed",
                bond=bond_id,
                addr=r["addr"] or "(self)",
                detail=r["detail"],
                level=logging.WARNING,
            )
    # The two writes fan out CONCURRENTLY, so exactly-one-failed leaves the
    # pair SAME-channel — audibly wrong, and it blocks a retry because the
    # {left,right} precondition no longer holds. Best-effort rollback returns
    # the member that DID flip to its original channel; a failed rollback is
    # surfaced, never silent.
    rolled_back = None
    if not all_ok and any(r["ok"] for r in results):
        ok_idx = 0 if results[0]["ok"] else 1
        rb_addr = targets[ok_idx][0]
        rb_grouping = grouping if ok_idx == 0 else peer_grouping
        rb_channel = self_channel if ok_idx == 0 else peer_channel
        rb_ok, rb_detail = rooms_peers.post_grouping_to_member(
            rb_addr, _body(rb_grouping, rb_channel), known, token=token,
        )
        rolled_back = bool(rb_ok)
        log_event(
            logger,
            "rooms.swap.rollback",
            bond=bond_id,
            addr=rb_addr or "(self)",
            channel=rb_channel,
            ok=rb_ok,
            detail=rb_detail,
            level=logging.WARNING,
        )
    log_event(
        logger,
        "rooms.swap",
        bond=bond_id,
        self=f"{self_channel}->{swapped_self}",
        peer=f"{peer_channel}->{swapped_peer}",
        repaired=repairing,
        ok=all_ok,
    )
    payload = {"ok": all_ok, "bond_id": bond_id, "results": results}
    if repairing:
        payload["repaired"] = True
    if rolled_back is not None:
        payload["rolled_back"] = rolled_back
    _send_json(
        handler,
        payload,
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


def _trim_float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _balance_trims_from_db(balance_db: float) -> tuple[float, float, bool]:
    """Map a signed balance slider to absolute trims.

    ``balance_db`` is positive toward RIGHT and negative toward LEFT. The
    louder side stays at 0 dB and the opposite side is attenuated, so the pair
    keeps as much digital headroom as the requested relative balance allows.
    """
    from ..multiroom.config import TRIM_DB_MIN, TRIM_DB_MAX

    requested = float(balance_db)
    left = min(TRIM_DB_MAX, -requested)
    right = min(TRIM_DB_MAX, requested)
    left_clamped = max(TRIM_DB_MIN, left)
    right_clamped = max(TRIM_DB_MIN, right)
    return round(left_clamped, 1), round(right_clamped, 1), (
        left != left_clamped or right != right_clamped
    )


def _balance_db_from_trims(left_trim_db: float, right_trim_db: float) -> float:
    """Signed slider value: positive means right is louder than left."""
    return round(float(right_trim_db) - float(left_trim_db), 1)


def _get_member_grouping_for_balance_snapshot(
    addr: str, known: set[str] | None = None,
) -> dict | None:
    return rooms_peers._get_member_grouping(
        addr, known, timeout=BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC,
    )


def _pair_balance_snapshot(grouping: dict, known: set[str] | None = None) -> dict:
    """Compact live balance state for the /sound/pair/ slider.

    The snapshot is present only for a two-speaker left/right bond. It resolves
    the peer through the same roster-first path used by swap/trim so the UI does
    not display a stale peer trim.
    """
    if not grouping.get("enabled") or grouping.get("error"):
        return {"applicable": False}
    self_channel = str(grouping.get("channel") or "").strip()
    if self_channel not in ("left", "right"):
        return {"applicable": False}
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not bond_id:
        return {"applicable": False}
    if known is None:
        known = rooms_peers.self_addresses()
    peer_addr, peer_grouping, perr = resolve_bond_peer(
        grouping, known,
        grouping_reader=_get_member_grouping_for_balance_snapshot,
    )
    if perr:
        return {"applicable": True, "ok": False, "error": perr}
    assert peer_grouping is not None
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    if {self_channel, peer_channel} != {"left", "right"}:
        return {
            "applicable": True,
            "ok": False,
            "error": (
                "balance needs one left and one right speaker "
                f"(this speaker is {self_channel or '?'}, peer is "
                f"{peer_channel or '?'})"
            ),
        }
    self_trim = round(_trim_float(grouping.get("trim_db")), 1)
    peer_trim = round(_trim_float(peer_grouping.get("trim_db")), 1)
    if self_channel == "left":
        left_trim, right_trim = self_trim, peer_trim
    else:
        left_trim, right_trim = peer_trim, self_trim
    return {
        "applicable": True,
        "ok": True,
        "left_trim_db": left_trim,
        "right_trim_db": right_trim,
        "balance_db": _balance_db_from_trims(left_trim, right_trim),
        "self_channel": self_channel,
        "peer_channel": peer_channel,
        "peer_addr": peer_addr,
    }


def _grouping_body_with_trim(grouping: dict, trim_db: float) -> dict:
    return {
        "enabled": True,
        "role": str(grouping.get("role") or ""),
        "channel": str(grouping.get("channel") or ""),
        "bond_id": str(grouping.get("bond_id") or ""),
        "leader_addr": str(grouping.get("leader_addr") or ""),
        "trim_db": trim_db,
    }


def _set_pair_balance(handler: BaseHTTPRequestHandler, parsed: dict) -> None:
    """Handle POST /trim with ``target=pair`` and absolute ``balance_db``.

    One slider value rewrites BOTH member trims to the loudness-maximizing
    attenuate-only pair: one side is always 0 dB, the other is <= 0 dB.
    """
    if "balance_db" not in parsed:
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be a number"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    try:
        balance_db = float(parsed.get("balance_db"))
    except (TypeError, ValueError):
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be a number"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    if not math.isfinite(balance_db):
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be finite"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    grouping = read_grouping_state()
    if (not grouping.get("enabled") or grouping.get("error")
            or not str(grouping.get("bond_id") or "").strip()):
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    self_channel = str(grouping.get("channel") or "").strip()
    if self_channel not in ("left", "right"):
        _send_json(
            handler,
            {"ok": False, "error": "balance needs a left/right pair"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    known = rooms_peers.self_addresses()
    peer_addr, peer_grouping, perr = resolve_bond_peer(grouping, known)
    if perr:
        _send_json(
            handler,
            {"ok": False, "error": f"balance {perr}"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    assert peer_grouping is not None
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    if {self_channel, peer_channel} != {"left", "right"}:
        _send_json(
            handler,
            {"ok": False, "error": (
                "balance needs a left/right pair (this speaker is "
                f"{self_channel or '?'}, peer is {peer_channel or '?'})"
            )},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    left_trim, right_trim, clamped = _balance_trims_from_db(balance_db)
    trims_by_channel = {"left": left_trim, "right": right_trim}
    members = [
        (
            "",
            grouping,
            trims_by_channel[self_channel],
            round(_trim_float(grouping.get("trim_db")), 1),
        ),
        (
            peer_addr,
            peer_grouping,
            trims_by_channel[peer_channel],
            round(_trim_float(peer_grouping.get("trim_db")), 1),
        ),
    ]
    members.sort(key=lambda item: item[0] == "")  # peer first

    token = rooms_peers.request_control_token(handler)
    results: list[dict] = []
    applied: list[tuple[str, dict, float]] = []
    rollbacks: list[dict] = []
    all_ok = True
    for addr, member_grouping, trim, original_trim in members:
        ok, detail = rooms_peers.post_grouping_to_member(
            addr,
            _grouping_body_with_trim(member_grouping, trim),
            known,
            token=token,
        )
        results.append({
            "addr": addr,
            "channel": str(member_grouping.get("channel") or ""),
            "trim_db": trim,
            "ok": ok,
            "detail": detail,
        })
        all_ok = all_ok and ok
        if not ok:
            break
        applied.append((addr, member_grouping, original_trim))

    if not all_ok and applied:
        for rb_addr, rb_grouping, original_trim in reversed(applied):
            rb_ok, rb_detail = rooms_peers.post_grouping_to_member(
                rb_addr,
                _grouping_body_with_trim(rb_grouping, original_trim),
                known,
                token=token,
            )
            rollback = {
                "addr": rb_addr,
                "channel": str(rb_grouping.get("channel") or ""),
                "trim_db": original_trim,
                "ok": rb_ok,
                "detail": rb_detail,
            }
            rollbacks.append(rollback)
            log_event(
                logger,
                "rooms.balance.rollback",
                addr=rb_addr or "(self)",
                channel=rollback["channel"],
                trim=f"{original_trim:.1f}",
                ok=rb_ok,
                detail=rb_detail,
                level=logging.WARNING,
            )

    log_event(
        logger,
        "rooms.balance",
        requested=f"{balance_db:.1f}",
        left=f"{left_trim:.1f}",
        right=f"{right_trim:.1f}",
        clamped=clamped,
        ok=all_ok,
    )
    payload = {
        "ok": all_ok,
        "balance": {
            "applicable": True,
            "ok": all_ok,
            "left_trim_db": left_trim,
            "right_trim_db": right_trim,
            "balance_db": _balance_db_from_trims(left_trim, right_trim),
            "clamped": clamped,
        },
        "results": results,
    }
    if rollbacks:
        payload["rollbacks"] = rollbacks
    _send_json(
        handler,
        payload,
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


def _set_member_trim(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /trim: set the pair balance absolutely.

    Body ``{target: "pair", balance_db}`` — the rooms slider's only trim
    write."""
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        _send_json(handler, {"ok": False, "error": err},
                   status=HTTPStatus.BAD_REQUEST)
        return
    _set_pair_balance(handler, parsed)


def _get_rooms_json(handler: BaseHTTPRequestHandler) -> None:
    _send_json(handler, _build_rooms_payload())


def _get_index(handler: BaseHTTPRequestHandler) -> None:
    ctx = begin_request(handler)
    send_html_response(handler, _render_page(csrf_token=ctx["csrf_token"]))


_GET_ROUTES = {
    "/": _get_index,
    "/rooms.json": _get_rooms_json,
}
_POST_ROUTES = {
    "/peering": _save_peering,
    "/bond": _save_bond,
    "/unbond": _unbond,
    "/swap": _swap_channels,
    "/trim": _set_member_trim,
}


class _Handler(BaseHTTPRequestHandler):
    """No state paths are captured here, so every request re-reads mDNS,
    grouping and peering.env."""

    def log_message(self, fmt, *args):  # noqa: ANN001, A003
        logger.info("rooms-wizard: " + fmt, *args)

    def do_GET(self):  # noqa: N802
        dispatch_get(self, _GET_ROUTES)

    def do_POST(self):  # noqa: N802
        dispatch_post(self, _POST_ROUTES, guard="header")


def _make_handler():
    """Return the request handler class (kept as a function for the
    existing call convention; _Handler captures no per-call state)."""
    return _Handler


# ----------------------------------------------------------------------
# Server setup.
# ----------------------------------------------------------------------


def make_server(target) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer. `target` is either an (host, port)
    tuple (direct bind) or an already-bound socket (from systemd socket
    activation — see jasper/web/__main__.py)."""
    from ..platform.systemd import make_http_server
    return make_http_server(target, _make_handler())
