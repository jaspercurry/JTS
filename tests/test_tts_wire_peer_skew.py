# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What protects the wide assistant wire from a stale peer (U2 PR-3, #2223).

PR-2 gave the assistant IPC a second payload verb, ``AUDIO32``, spoken only by
a box whose declaration is wide on both halves. Its review banked one exposure
for this PR, and it is the worst failure class this repo has: **a fan-in binary
that predates ``AUDIO32`` does not mis-read it — it rejects it and hangs up.**
``read_command`` returns ``Err("unknown TTS command: AUDIO32 …")``, the
connection task logs ``event=fanin.tts_socket.protocol_error`` and returns,
dropping the stream.

The consequence is not a degraded reply, it is **silence including the failure
cue**, because cues travel the same socket. Nothing announces it. So the
question this file answers is not "does the width convert correctly" (PR-2 owns
that) but "can a box ever reach the state where a new ``jasper-voice`` speaks
to an old ``jasper-fanin``".

**It can — on one path, and this file does NOT close it.** Say that plainly,
because an earlier revision of this docstring claimed the guarantee was
structural and it is not. What the two orderings below actually cover is the
SUCCESSFUL deploy and the boot transaction; both were INHERITED from code
written for other reasons, and a future edit could drop either without any
current test noticing, which is what they are asserted for here:

1. **Boot** — ``jasper-voice.service`` declares ``After=jasper-fanin.service``.
2. **Deploy (success)** — the installer STOPS ``jasper-voice`` (first entry of
   the canonical park set) before it restarts ``jasper-fanin``, so the voice
   daemon that comes back is always newer than the fan-in it talks to.

**The residual, stated rather than waived.** An **aborted** deploy — a fan-in
build failure on a Pi 5-class box, which never reaches the park step — can
leave the new Python installed beside a still-running old fan-in binary. Any
lone ``jasper-voice`` restart after that (a routine event: the AEC reconciler,
a wizard save, a watchdog) then puts a new voice against an old peer. Nothing
holds it closed: fan-in is not in the park set, ``jasper-voice.service``
declares ``Wants=`` and not ``Requires=``, and — the part that makes it
genuinely invisible — the ``Err`` arm that logs
``event=fanin.tts_socket.protocol_error`` bumps **no metric**, so the skew does
not reach ``/state`` or the doctor either. On a wide-declared box that is a
silent assistant, cue included.

Detecting it is [#2378](https://github.com/jaspercurry/JTS/issues/2378) and is
deliberately NOT solved here. A capability handshake was the obvious reach and
was declined for this PR: R-WIDTH forbids cross-daemon format negotiation, and
a fan-in STATUS read at voice startup would add a runtime dependency whose own
failure mode — STATUS unreadable while fan-in restarts — silently narrows the
fleet's one wide box. That is a design decision #2378 owns, not a line this
file can add.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

import pytest

from jasper.audio_io import _OutputdStreamAdapter

REPO = Path(__file__).resolve().parents[1]

VOICE_UNIT = REPO / "deploy" / "systemd" / "jasper-voice.service"
PARK_UNITS_FRAGMENT = REPO / "deploy" / "lib" / "jasper-core-graph-park-units.sh"
INSTALL_SYSTEMD_UNITS = REPO / "deploy" / "lib" / "install" / "systemd-units.sh"

_PARK_CALL = "park_audio_clients_for_core_graph_restart"
_FANIN_RESTART = "systemctl restart jasper-fanin.service"


# ---------------------------------------------------------------------------
# 1. The failure mode itself, reproduced against a peer that rejects AUDIO32.
# ---------------------------------------------------------------------------


class _PreAudio32Peer:
    """A fan-in that speaks the pre-PR-2 protocol: ``AUDIO`` yes, ``AUDIO32`` no.

    Models `read_command`'s dispatch exactly at the one point that matters — an
    unrecognized verb is an `Err`, and the connection task's `Err` arm returns,
    which drops the stream. It accepts control lines and narrow payloads so a
    green narrow assertion means "this peer works", not "this peer is broken".
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.accepted_audio_bytes = 0
        self.rejected_line: bytes | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        stream = self._sock.makefile("rb")
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                if line.startswith(b"AUDIO32 "):
                    # The pre-PR-2 tree has no AUDIO32 arm: unknown command ->
                    # protocol_error -> return -> socket dropped.
                    self.rejected_line = line
                    return
                if line.startswith(b"AUDIO "):
                    stream.read(int(line.split()[1]))
                    self.accepted_audio_bytes += int(line.split()[1])
        finally:
            try:
                stream.close()
            finally:
                self._sock.close()

    def join(self) -> None:
        self._thread.join(timeout=5.0)
        assert not self._thread.is_alive(), "peer thread did not finish"


def _write_until_error(adapter, payload: bytes, *, attempts: int = 64):
    """Write until the socket reports the peer is gone, or run out of attempts.

    Bounded rather than single-shot on purpose: a closed peer does not fail the
    FIRST write. The kernel accepts what fits in the send buffer and only
    reports EPIPE once the RST has been processed, so a one-write assertion
    would be flaky in exactly the direction that hides the bug.
    """
    for _ in range(attempts):
        try:
            adapter.write(payload)
        except OSError as e:
            return e
    return None


@pytest.fixture
def peer_pair():
    ours, theirs = socket.socketpair()
    peer = _PreAudio32Peer(theirs)
    try:
        yield ours, peer
    finally:
        try:
            ours.close()
        except OSError:
            pass


def test_a_pre_audio32_peer_accepts_the_narrow_wire(peer_pair):
    """The control: this peer is a working fan-in for every narrow-wire box.

    Without it, the wide assertion below could pass because the harness is
    broken rather than because the verb was rejected.
    """
    ours, peer = peer_pair
    adapter = _OutputdStreamAdapter(ours, wire_wide=False)
    payload = b"\x01\x02\x03\x04" * 512
    error = _write_until_error(adapter, payload)
    assert error is None, f"a narrow wire must not disturb a pre-AUDIO32 peer: {error}"
    assert peer.accepted_audio_bytes > 0
    assert peer.rejected_line is None


def test_a_pre_audio32_peer_hangs_up_on_the_wide_wire(peer_pair):
    """The banked exposure, in-tree: rejection is a dead socket, not a bad sample.

    The documented mode is an `OSError` out of the writer, and the BASE CLASS
    is asserted on measured evidence rather than on caution: a 20-run survey on
    this developer's macOS host produced `BrokenPipeError` (EPIPE, errno 32)
    20/20, while the review of this PR observed `[Errno 57] Socket is not
    connected` (ENOTCONN) once in five on another host. Same rejection, two
    errnos — which write trips it, and with what, depends on the kernel's send
    buffer and on when the RST lands. Pinning either would make this a
    host-portability trap; what must never change is that it RAISES rather than
    silently discarding the reply.
    """
    ours, peer = peer_pair
    adapter = _OutputdStreamAdapter(ours, wire_wide=True)
    payload = b"\x01\x02\x03\x04" * 512
    error = _write_until_error(adapter, payload)
    assert isinstance(error, OSError), (
        "a peer that rejects AUDIO32 must surface as a socket error, never as "
        "a write that appears to succeed"
    )
    peer.join()
    assert peer.rejected_line is not None
    assert peer.rejected_line.startswith(b"AUDIO32 ")
    assert peer.accepted_audio_bytes == 0


def test_the_rejected_connection_cannot_carry_a_failure_cue_either(peer_pair):
    """WHY the ordering below is load-bearing and a retry would not be enough.

    A cue is not a separate channel — it is another segment on this same
    socket. Once the peer has hung up, the cue write fails the same way, so the
    household gets no speech AND no audible reason for it. That is the
    silent-failure class AGENTS.md forbids, and it is unreachable only because
    a stale peer is unreachable.
    """
    ours, peer = peer_pair
    adapter = _OutputdStreamAdapter(ours, wire_wide=True)
    payload = b"\x01\x02\x03\x04" * 512
    assert isinstance(_write_until_error(adapter, payload), OSError)
    peer.join()
    # The cue would take this same adapter. It cannot get through.
    assert isinstance(_write_until_error(adapter, payload), OSError), (
        "the failure cue shares the connection the rejected payload killed"
    )


# ---------------------------------------------------------------------------
# 2. The orderings that make a stale peer unreachable. Asserted, not inherited.
# ---------------------------------------------------------------------------


def test_voice_is_ordered_after_fanin_at_boot():
    """`After=jasper-fanin.service` on the voice unit.

    Present today for other reasons (the TTS socket must exist before voice
    connects). It also carries the width ordering, so it is pinned here with
    that reason attached.

    ORDERING ONLY, and the distinction is the residual in the module docstring:
    systemd `After=` sequences a boot transaction that starts BOTH units. It
    says nothing about a lone `systemctl restart jasper-voice` against an
    already-running fan-in, which is the aborted-deploy path #2378 owns. This
    test pins what the directive does, not a guarantee it does not give.
    """
    text = VOICE_UNIT.read_text()
    after = [ln for ln in text.splitlines() if ln.startswith("After=")]
    assert after, "jasper-voice.service must declare an After= ordering"
    assert any("jasper-fanin.service" in ln for ln in after), (
        "jasper-voice.service must be ordered After=jasper-fanin.service: a "
        "voice daemon that starts first can speak AUDIO32 at a fan-in the "
        "deploy has not replaced, which is a silent assistant including the cue"
    )


def test_voice_is_parked_before_the_installer_restarts_fanin():
    """The deploy-time half: voice is stopped before the new fan-in comes up.

    Checked per shell function rather than over the whole file, so a future
    function that restarts fan-in without parking first cannot hide behind an
    unrelated earlier park call.

    THE SUCCESS PATH ONLY. A deploy that aborts before this step — a fan-in
    build failure — never parks anything, so this ordering is simply not
    reached; see the module docstring's residual and #2378. Two edges are known
    narrow and left so deliberately: the function split keys on a bash function
    header, and the unit names are matched as written, so a restart moved to a
    helper or spelled without `.service` would not be seen here.
    """
    text = INSTALL_SYSTEMD_UNITS.read_text()
    bodies = re.split(r"^(?=[a-z_][a-z0-9_]*\(\) \{$)", text, flags=re.MULTILINE)
    restarting = [b for b in bodies if _FANIN_RESTART in b]
    assert len(restarting) >= 2, (
        "expected the full-install and streambox paths to restart fan-in; "
        f"found {len(restarting)}"
    )
    for body in restarting:
        name = body.splitlines()[0]
        assert _PARK_CALL in body, (
            f"{name} restarts jasper-fanin without parking audio clients first"
        )
        assert body.index(_PARK_CALL) < body.index(_FANIN_RESTART), (
            f"{name} must call {_PARK_CALL} BEFORE restarting jasper-fanin, so "
            "jasper-voice is stopped while the new fan-in binary comes up"
        )


def test_the_park_set_stops_voice():
    """The park set is what the ordering above depends on containing.

    `tests/test_core_graph_park_units_contract.py` owns the list's SSOT shape;
    this asserts the one membership the assistant wire's width depends on, with
    the width reason attached so a future trim of the list meets it here.
    """
    text = PARK_UNITS_FRAGMENT.read_text()
    assert "jasper-voice.service" in text, (
        "jasper-voice.service must stay in JASPER_CORE_GRAPH_PARK_UNITS: it is "
        "what stops a running voice daemon from outliving the fan-in restart "
        "and speaking a verb the old binary rejects"
    )
