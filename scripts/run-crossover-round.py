#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run one crossover-v2 measurement round end to end, from the laptop.

Every campaign has rebuilt this as a chain of throwaway shell — ``stage1.sh``
launches a walk driver, sleeps, POSTs the session open, greps a log for
"released index=N", sleeps again; ``cycle.sh`` does the same around the verify
and then banks. The pieces those scripts drove are all product now
(``jasper-angle-capture``, ``bank-crossover-round.sh``,
the wizard's own endpoints), so this composes them instead of re-implementing
them. It builds NOTHING new on the Pi.

**The apply gate is why this file exists.** The chained scripts had no gate,
and a round applied a candidate nobody had sanctioned — one measured round,
lost. So: a measurement run NEVER applies. It ends with the candidate's
fingerprint and its numbers printed and banked, and stops. Applying is a
SECOND, separate invocation that must NAME the fingerprint it means to put on
the speaker::

    run-crossover-round.py --apply 4f2a…

and the runner re-reads the LIVE candidate first and refuses — before any POST
leaves the laptop — when the fingerprint it finds is not the one named. The
endpoint runs that same comparison server-side; what changes here is that the
value compared is one a person typed, and that a mistyped or stale one never
becomes a request at all.

One measurement stage, in order::

    stage the walk   ssh … jasper-angle-capture stage --mover arm   (--angles)
    launch the walk  ssh … jasper-angle-capture serve     (--attest-rig-clear)
    open the session POST /correction/crossover/v2/{session,verify}
    await the walk   the arm harness exits; its rc is the walk's verdict
    await the stage  poll the envelope until this session's phases are accepted
    bank             bash scripts/bank-crossover-round.sh <campaign>/<label>
    index the poses  position_cycle.json — derived from what the bank pulled
    summarise        the candidate's fingerprint + numbers, printed and banked

**``--per-position N`` takes N captures at each pose in ONE walk.** It stages
each angle N times adjacently, so the arm settles and releases N times without
travelling: what varies between the takes is time (and whatever the operator
changed between them), never the pose. It is sugar over a staged list an
operator could type — the stops are an ordered tuple with no uniqueness rule and
``angle_capture.both_at`` already ships adjacent same-angle stops — and the value
it adds over typing it is the arithmetic a hand-typed list gets wrong:
``--complete-after`` counts RELEASES, so it must scale with N, and a short one
completes the walk partway through at rc 0. It governs a staged MEASURE walk;
this runner's own arithmetic accepts any regime that composes ONE stop per
angle (``per_driver`` and ``summed`` today) and refuses ``both``, at two,
below. A session walk plays only ``per_driver`` at every pose, though, and
refuses any other regime when it opens
(``angle_capture.session_lateral_walk``) — so ``summed`` clears every gate
here and still dies at session open.

**Every staged round banks ``position_cycle.json``, cycled or not** — one sorted
index of the poses this round actually measured, DERIVED from the bundle the
bank just pulled. Nothing here writes a fact of its own: the speaker stamps the
true bearing on every accepted take
(``crossover_v2.spatial.lateral_pose_record`` -> ``positions/{take_id}.json``
inside the evidence bundle), and this projects those records. A mapping written
from the staged angles would be a SECOND writer of one fact — the runner's
intent beside the speaker's record — and the two disagree exactly when it
matters, on a refused or retaken pose.

The pose IS banked; nothing SURFACED it. ``jasper-round-views`` reads the CLOUD
positions block, so a lateral walk's bearings sat in per-take sidecars no view
opened. The evidence packet's ``lateral_poses`` block now opens them, through
``position_cycle.read_lateral_take`` — the same accept rule this index uses, so
the two cannot come to disagree about what a lateral take is. This index stays
the convenient sorted form at the round root, which is why a round whose bundle
carries no lateral takes gets a named refusal here rather than a document
assembled from intent.

The walk starts BEFORE the open because ``jasper-angle-capture serve``'s first poll is
what checks a staged walk is still waiting — see its module docstring. It is
launched only when ``--attest-rig-clear`` is given: the attestation is the
operator's to make and this runner never invents one. Without it the round runs
the same phases minus the walk, for a human-moved or ordinary session -- and
``--angles`` is refused outright, because the walk it would stage is an ARM
walk that nothing would serve.

**Nothing here re-maps another tool's verdict.** ``jasper-angle-capture
serve``'s exit code and the stall it named, and ``bank-crossover-round.sh``'s
0/3/4, are reported verbatim, by their owners' own names, as the deciding
value on this runner's own per-phase exit code. A failing walk stops the round
before it banks: the walk's rc is the verdict, and a bank on top of it would be
a second one.

**The examples below pass no ``--complete-after``, and that is the recipe.**
The session closes ITSELF when it has served every hold it planned, and the walk
reads that terminal status through its own session latch — the two closes are
exclusive, so nothing is left un-posted. A laptop-side number cannot be the
honest one: ``--complete-after`` counts RELEASES, the staged stop count is only
a FLOOR (the session's own non-walk captures are gated holds too, and how many
there are is the tier's, decided on the speaker), so a walk told to complete at
its own stop count can close the group before the session is done. Pass it when
a WALK has to close a wired stage's held set, which is the case it exists for.

Usage::

    # measure (stage 1), with the lab arm walking five angles
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \\
        --campaign captures/my-night --label r1 --tier remote \\
        --angles 0,7,-7,22,-22 --regime per_driver \\
        --attest-rig-clear --expect-angles 7,-7,22,-22

    # the same five angles, three takes at each — one walk, fifteen stops
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \\
        --campaign captures/my-night --label r2 --tier remote \\
        --angles 0,7,-7,22,-22 --per-position 3 --regime per_driver \\
        --attest-rig-clear --expect-angles 7,-7,22,-22

    # …read the printed candidate, decide, THEN apply it by name
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py --apply <fp>

    # the post-apply check (stage 2). ``--expect-angles`` is a SUBSET check
    # over the GATE TARGETS of released entries — every angle named must appear
    # in what the walk was aimed at; extra served angles are fine. Naming 0
    # asserts the walk was aimed at the design axis and released there, which
    # has been true on BOTH sides of the 2026-08-24 geometry ruling: stage 2's
    # anchor has always published a 0° target (every begin is gated, including
    # the 0° ones), so this flag cannot tell you whether a 0° POSITION was
    # banked. What the ruling changed is upstream of the flag — the walk now
    # prompts a 0° ``cloud_verify`` pose whose sweep joins the group. Read the
    # round's own ``positions/*.json`` for the bearing that was banked.
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \\
        --campaign captures/my-night --label r1-verify --stage verify \\
        --attest-rig-clear --expect-angles 0,7,-7,22,-22

``PI_HOST`` / ``PI_USER`` exported by the caller win over ``.env.local`` — the
resolution is ``scripts/_lib.sh``'s own (issue #2689), and the resolved host is
named in the run trail. The same values are exported into the bank script, so
both halves of a round can never target different speakers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import _pi_target
except ModuleNotFoundError as exc:
    if exc.name != "_pi_target":
        raise
    from scripts import _pi_target

from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH,
    REASON_ANSWER_LOST,
    SESSION_PATH,
    STAGE_KEY,
    STAGE_POST_APPLY,
    TIERS,
    VERIFY_PATH,
    WizardClient,
    apply_by_fingerprint,
    error_of,
    wait_for_round,
)
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_PRESCRIPTION_KEY,
)
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_FILENAME,
    PositionCycleError,
    expand_angle_spec,
    position_cycle_document,
    staged_stops,
)
from jasper.active_speaker.crossover_v2.topology_prescription import (
    TOPOLOGY_PRESCRIPTION_KEY,
)
# The seam's OWN composition table — how many stops one angle becomes at a given
# regime. Imported rather than restated because a second copy is precisely the
# defect the refusal below exists to prevent: this runner's stop count IS the
# `--complete-after` floor, and a table that drifted from the one
# `jasper-angle-capture` actually composes with would make the floor silently
# wrong. Private on purpose at its own module — it is one tool's internal rule —
# and read here rather than copied for the same reason a bound is asked of its
# owner everywhere else in this file.
from jasper.cli.angle_capture import _REGIME_STOPS

# --------------------------------------------------------------------------- #
# the endpoints and the vocabulary — the product's own, never restated
# --------------------------------------------------------------------------- #


#: Where ``install.sh`` puts the runtime the two Pi-side CLIs live in.
PI_VENV_BIN = "/opt/jasper/.venv/bin"

#: How long the LOCAL ssh client is given to go away after a terminate.
#:
#: Deliberately not called a park grace, because this process cannot observe a
#: park. Terminating the client drops the transport; sshd then hangs up the
#: remote walk, which parks on its way out (``arm_walk.PARK_ON_SIGNALS``) —
#: entirely after the client has exited, on the speaker, into the walk's own
#: log and journal. What this bounds is only the wait for a client that will
#: not die, so it is generous rather than tuned.
WALK_CLIENT_EXIT_GRACE_S = 90.0


# --------------------------------------------------------------------------- #
# exit codes — part of the contract, because the caller of this tool is a script
# --------------------------------------------------------------------------- #

EXIT_OK = 0
#: ``jasper-angle-capture stage`` refused or could not bank the walk. Its own
#: exit code (jasper/cli/_refusal.py's vocabulary) is the deciding value on
#: the line.
EXIT_STAGE = 3
#: ``POST …/v2/session`` did not open. The wizard's own words are on the line.
EXIT_OPEN = 4
#: The arm walk did not finish clean. ``jasper-angle-capture serve``'s rc and
#: the stall IT named are the deciding value; nothing is re-mapped and nothing
#: is banked.
EXIT_WALK = 5
#: ``POST …/v2/verify`` did not open.
EXIT_VERIFY = 6
#: The stage did not stop: it never left its running phases inside
#: ``--stage-timeout-s``, or the status read it is polled with went unanswered.
EXIT_INCOMPLETE = 7
#: The session itself reported a failure. Its own error is on the line.
EXIT_SESSION_FAILED = 8
#: ``bank-crossover-round.sh`` refused: 3 an incomplete bank, 4 a destination
#: that was already used. Its rc is the deciding value.
EXIT_BANK = 9
#: The apply POST was refused, blocked, or failed. Nothing was rolled back here
#: — the endpoint's own transaction owns that.
EXIT_APPLY = 10
#: The fingerprint named on the command line is not the live candidate's.
#: NOTHING was POSTed. This is the gate.
EXIT_FINGERPRINT = 11
#: The ssh transport failed, so the walk never reported anything. Its own code
#: rather than EXIT_WALK: "walk_failed" on the summary line reads as the arm
#: misbehaving, and a dropped link is not the arm's doing.
EXIT_SSH_TRANSPORT = 12
#: Nothing named the speaker this round is for. sysexits EX_CONFIG, the same
#: code ``scripts/_lib.sh`` refuses with, because it is the same refusal: on a
#: multi-speaker LAN a guessed ``jts.local`` measures whichever box claimed the
#: name (#3498).
EXIT_CONFIG = 78

EXIT_NAMES: Mapping[int, str] = {
    EXIT_OK: "ok",
    EXIT_STAGE: "stage_refused",
    EXIT_OPEN: "open_failed",
    EXIT_WALK: "walk_failed",
    EXIT_VERIFY: "verify_failed",
    EXIT_INCOMPLETE: "stage_incomplete",
    EXIT_SESSION_FAILED: "session_failed",
    EXIT_BANK: "bank_refused",
    EXIT_APPLY: "apply_failed",
    EXIT_FINGERPRINT: "fingerprint_mismatch",
    EXIT_SSH_TRANSPORT: "ssh_transport_failed",
    EXIT_CONFIG: "no_target",
}


class NoTarget(RuntimeError):
    """Nothing names the speaker: ``_lib.sh`` refused and no export answers."""


# --------------------------------------------------------------------------- #
# the trail — one call, two surfaces
# --------------------------------------------------------------------------- #


class Trail:
    """A step line on stdout and the same fields as a JSONL row.

    The same shape ``serve``'s own trail holds, and for the same reason:
    one call site, so what an operator reads and what a later analysis parses
    can never disagree about a number. This one prints instead of writing to a
    journal — it runs on the laptop, where there is no journal to write to.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._handle = (
            open(path, "a", buffering=1, encoding="utf-8") if path else None
        )

    def emit(self, step: str, *, ok: bool = True, **fields: Any) -> None:
        """One of this runner's own steps. ``step``, never ``phase``.

        ``phase`` belongs to the flow — it is the state machine's word, and it
        rides these rows as a FIELD. A row whose own key were also ``phase``
        would put two different facts under one name.
        """
        rendered = " ".join(f"{k}={_render(v)}" for k, v in fields.items())
        print(
            f"round: {step} {'ok' if ok else 'FAILED'}"
            + (f" {rendered}" if rendered else ""),
            flush=True,
        )
        if self._handle is not None:
            row = {"t": round(time.time(), 3), "step": step, "ok": ok, **fields}
            self._handle.write(json.dumps(row, default=str) + "\n")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _render(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


# --------------------------------------------------------------------------- #
# the speaker this checkout talks to
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Target:
    """Which speaker, reached how, calling itself what."""

    host: str
    user: str
    hostname: str

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"


#: ``_lib.sh``'s ``JTS_TARGET_FROM`` in the trail's own words.
_LIB_TARGET_SOURCES: Mapping[str, str] = {
    "caller": "your export",
    "file": ".env.local",
}


def resolve_target(hostname_override: str | None = None,
                   trail: Trail | None = None) -> Target:
    """The speaker, with the caller's own exports winning.

    ``_lib.sh`` owns that precedence (issue #2689), sourced through the
    shared ``scripts/_pi_target.py`` (one ``.env.local`` reader for every
    laptop script, rather than a second one here). The caller's exports are
    still read here so the trail can name WHERE each half came from, and so a
    ``_lib.sh`` that could not run at all falls back to them rather than to
    the default speaker.

    Raises :class:`NoTarget` when neither answers: a round measures ONE
    speaker, and there is no honest guess at which (#3498).
    """
    caller_host = os.environ.get("PI_HOST") or ""
    caller_user = os.environ.get("PI_USER") or ""
    caller_hostname = os.environ.get("JASPER_HOSTNAME") or ""
    caller_target = caller_host or caller_hostname
    lib_host = lib_user = lib_hostname = lib_from = ""
    # A resolution that did not happen is REPORTED, never absorbed: measuring
    # the default speaker because a `bash` was missing or `_lib.sh` blew up is
    # the quiet wrong-Pi ending this whole file is trying to make impossible.
    detail = ""
    try:
        lib_host, lib_user, lib_hostname, lib_from = (
            _pi_target.resolve_lib_target())
    except _pi_target.LibTargetError as exc:
        detail = str(exc)
    if detail and trail is not None:
        trail.emit(
            "resolve_target", ok=False, detail=detail,
            using="the caller's own exports" if caller_target else "nothing",
        )
    if detail and not caller_target:
        raise NoTarget(
            "no target speaker: nothing names the box this round would "
            "measure. Export PI_HOST=<host> or JASPER_HOSTNAME=<name>, or run "
            "scripts/onboard.sh <host> / scripts/use <host> for this checkout"
        )

    # `_lib.sh` names its own source, so the trail can name a file the
    # operator could actually go look at rather than guess one from whether
    # `.env.local` happens to exist.
    lib_source = _LIB_TARGET_SOURCES.get(lib_from, "the built-in default")

    def _pick(override: str, caller: str, lib: str, default: str) -> tuple[str, str]:
        for value, source in ((override, "--hostname"), (caller, "your export"),
                              (lib, lib_source), (default, "the default")):
            if value:
                return value, source
        return default, "the default"

    # One record, the same way _lib.sh resolves it: a lone exported
    # JASPER_HOSTNAME supplies the ssh target too, so it is YOUR export the
    # target came from — not the .env.local the library happened to read.
    # No default: the refusal above leaves the caller or `_lib.sh` as the only
    # two ways to get here, and both name a host.
    host, host_source = _pick("", caller_target, lib_host, "")
    user, user_source = _pick("", caller_user, lib_user, "pi")
    hostname, hostname_source = _pick(
        hostname_override or "", caller_hostname, lib_hostname, host)
    if not (hostname_override or caller_hostname or lib_hostname):
        # The name fell back to the ssh target itself — same value, same
        # origin, so it is not a second source to disclose.
        hostname_source = host_source
    if trail is not None:
        # WHERE each half came from. Nothing here guesses which of the two
        # is the one you meant — that is the operator's to know — but it
        # must not be invisible.
        #
        # ``split`` compares SOURCES, not values, so it is true even when both
        # sources happen to name the same speaker. That over-warns on purpose:
        # two sources agreeing on a value is a property of one checkout, not a
        # guarantee, and the cheap direction to be wrong in is the one that
        # says "look at this".
        trail.emit(
            "identity", host=host, host_from=host_source,
            user=user, user_from=user_source,
            hostname=hostname, hostname_from=hostname_source,
            split=host_source != hostname_source,
        )
    return Target(host, user, hostname)


# --------------------------------------------------------------------------- #
# the wizard, over the LAN
# --------------------------------------------------------------------------- #
#
# The transport, the JSON on top of it and the two round verbs are
# `jasper.active_speaker.wizard_client` — one implementation, used from the
# speaker by the arm walk and `jasper-round`, and from the laptop by this.
# Reached across the LAN rather than over loopback, so `base_url` is the Pi's
# address while `host_header` stays the speaker's own name: the two are
# separate facts (AGENTS.md's PI_HOST vs JASPER_HOSTNAME split) and the
# management-host guard reads the second.


# --------------------------------------------------------------------------- #
# the phases
# --------------------------------------------------------------------------- #


#: The same non-interactive options ``bank-crossover-round.sh``'s ``remote()``
#: uses, plus the keepalive AGENTS.md's deploy path holds: a severed transport
#: surfaces as an ssh error in about a minute instead of an unbounded hang.
SSH_OPTS = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
]


def stage_walk(
    target: Target,
    angles: str,
    regime: str,
    trail: Trail,
    *,
    polarity: str | None = None,
    inverted_role: str | None = None,
    delayed_role: str | None = None,
    delay_us: float | None = None,
    level_matched: bool = False,
) -> int:
    """``jasper-angle-capture stage`` on the Pi. Its refusal is its own.

    ``--angles`` and ``--regime`` are passed through as the operator wrote
    them: bounds, whole-degree-ness and the regime vocabulary are the seam's,
    and a second validator here would be a second answer to the same question.
    ``--per-position`` repeats each token verbatim before it gets here for that
    same reason — see ``position_cycle.expand_angle_spec``.

    ``polarity``/``inverted_role`` are R-1's pair, and they travel the same way:
    forwarded verbatim, judged by the staging seam. Without them this runner
    could stage only normal-polarity walks, which put the reverse-null
    confirmation out of reach of the one command that drives a round.
    """
    flags = ""
    if polarity is not None:
        flags += f" --polarity={shlex.quote(polarity)}"
    if inverted_role is not None:
        flags += f" --inverted-role={shlex.quote(inverted_role)}"
    if delayed_role is not None:
        flags += f" --delayed-role={shlex.quote(delayed_role)}"
    if delay_us is not None:
        flags += f" --delay-us {delay_us!r}"
    if level_matched:
        flags += " --level-matched"
    remote = (
        f"sudo {PI_VENV_BIN}/jasper-angle-capture stage --mover arm "
        f"--angles={shlex.quote(angles)} --regime={shlex.quote(regime)}"
        f"{flags} --json"
    )
    proc = subprocess.run(
        [*SSH_OPTS, target.ssh_target, remote],
        capture_output=True, text=True, timeout=120,
    )
    ok = proc.returncode == 0
    trail.emit(
        "stage", ok=ok, angles=angles, regime=regime, mover="arm",
        polarity=polarity or "normal", inverted_role=inverted_role or "",
        delayed_role=delayed_role or "", delay_us=delay_us if delay_us else 0.0,
        level_matched=level_matched,
        stops=staged_stops(angles),
        angle_capture_exit=proc.returncode,
        detail=(proc.stdout or proc.stderr).strip()[-300:],
    )
    return proc.returncode


def launch_walk(
    target: Target,
    *,
    expect_angles: str,
    complete_after: int | None,
    settle_s: float | None,
    log_path: Path,
    trail: Trail,
) -> subprocess.Popen[bytes]:
    """``jasper-angle-capture serve`` on the Pi, backgrounded, output to ``log_path``.

    ``-tt`` forces a remote PTY, and that PTY is the whole mechanism by which
    stopping the LOCAL client stops the REMOTE walk: killing an ssh client only
    ever kills the client, but dropping the transport makes sshd close the PTY,
    which hangs up the walk's process group. SIGHUP is therefore the signal the
    walk actually receives here — never the SIGTERM this process sends — and it
    parks only because SIGHUP is in ``arm_walk.PARK_ON_SIGNALS``. Without the
    PTY there is no signal at all: the orphaned walk keeps serving position
    holds until its own idle ceiling, into a session that has moved on.

    Local stdin is ``DEVNULL`` so forcing a remote tty cannot put the
    operator's own terminal into raw mode.
    """
    # ``pi``, not ``target.user``: the walk drives the turntable adapter, which
    # opens a serial port, so the identity is the one holding ``dialout`` --
    # ``User=pi`` in the shipped jasper-turntable-autostop@.service, which
    # ``serve``'s own help names as load-bearing. An operator who ssh's in as
    # somebody else still needs the walk to run as pi. Do not "fix" this to
    # follow the ssh user.
    argv = [
        f"sudo -u pi {PI_VENV_BIN}/jasper-angle-capture serve",
        "--mover turntable",
        "--attest-rig-clear",
        f"--hostname {shlex.quote(target.hostname)}",
    ]
    if expect_angles:
        argv.append(f"--expect-angles {shlex.quote(expect_angles)}")
    if complete_after is not None:
        argv.append(f"--complete-after {complete_after}")
    if settle_s is not None:
        argv.append(f"--settle-s {settle_s}")
    remote = " ".join(argv)
    # The child keeps its own dup of the fd, so this process closes its copy as
    # soon as the spawn is decided either way -- a raised Popen would otherwise
    # leak it for the life of the run.
    handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [*SSH_OPTS, "-tt", target.ssh_target, remote],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
        )
    finally:
        handle.close()
    trail.emit(
        "walk_launched", host=target.ssh_target, expect_angles=expect_angles or "(none)",
        complete_after=complete_after, log=str(log_path),
    )
    return proc


def stop_walk(proc: subprocess.Popen[bytes], trail: Trail) -> None:
    """Drop the transport so the remote walk hangs up, and say only that.

    What this observes is the local ssh client exiting. The park happens
    afterwards and elsewhere — on the speaker, in the walk's own unwind — so
    this reports the signal as SENT, never the arm as parked. Claiming the
    latter would put a reassurance in the trail that nothing here can see.
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=WALK_CLIENT_EXIT_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        trail.emit(
            "walk_stopped", ok=False,
            detail=(
                f"the local ssh client outlived {WALK_CLIENT_EXIT_GRACE_S:.0f}s "
                "and was killed; whether the remote walk was hung up, and "
                "whether it parked, is unknown from here -- check the walk log"
            ),
        )
        return
    trail.emit(
        "walk_stopped",
        detail=(
            "the ssh transport is down, which hangs up the remote walk; it "
            "parks in its own unwind -- the walk log is where that is confirmed"
        ),
    )


#: ssh's own "the transport failed" code. It is not a walk verdict and the
#: harness cannot produce it — its codes are the shared 0/1/3 plus 128+signum —
#: so naming it ``walk_failed`` would blame the arm for a dropped link.
SSH_TRANSPORT_EXIT = 255

#: ``serve``'s one-sentence refusal as it lands in the walk log. The stall name
#: is READ from the record rather than derived from the rc: ``serve`` exits the
#: shared 0/1/3 (jasper/cli/_refusal.py), so the number no longer distinguishes
#: ``stuck`` from ``walk_not_staged`` — only the ``reason`` does.
_SERVE_REFUSAL = re.compile(r"^refused \(([a-z_]+)\):", re.MULTILINE)


def _walk_exit_name(code: int, log_path: Path, since_byte: int = 0) -> str:
    """The stall the walk named, or ssh's own name for the code ssh owns.

    ``since_byte`` is where THIS run's output starts: the log is opened for
    append, so a re-run under the same ``--label`` sits behind the previous
    run's refusal and would otherwise inherit its reason. It is an ``st_size``,
    so the slice is taken on BYTES and decoded after -- one non-ASCII byte
    upstream would otherwise shift a character slice past this run's own line.
    """
    if code == SSH_TRANSPORT_EXIT:
        return "ssh_transport_failed"
    if code == 0:
        return "ok"
    try:
        tail = log_path.read_bytes()[since_byte:]
    except OSError:
        tail = b""
    named = _SERVE_REFUSAL.findall(tail.decode("utf-8", "ignore"))
    # No refusal in the log is the signal-parked ending (128+signum, printed by
    # nobody) or a walk killed outright: the rc is then all there is to report.
    return named[-1] if named else str(code)


def await_stage(
    wizard: WizardClient,
    *,
    prior_session_id: str,
    timeout_s: float,
    poll_s: float,
    trail: Trail,
) -> int:
    """:func:`wait_for_round`, with this runner's trail rows and exit codes."""
    result = wait_for_round(
        wizard, prior_session_id=prior_session_id,
        timeout_s=timeout_s, poll_s=poll_s,
    )
    phase = str(result["phase"])
    if result["status"] == "failed":
        trail.emit("await", ok=False, phase=phase,
                   failure=_render(result["failure"]))
        return EXIT_SESSION_FAILED
    if result["status"] == "lost":
        trail.emit("await", ok=False, reason=REASON_ANSWER_LOST,
                   detail="the status read was not answered")
        return EXIT_INCOMPLETE
    if result["status"] == "timed_out":
        trail.emit("await", ok=False, phase=phase or "(unreadable)",
                   waited_s=round(timeout_s, 1))
        return EXIT_INCOMPLETE
    trail.emit("await", phase=phase, session_id=result["session_id"])
    return EXIT_OK


def round_graded_this_session(wizard: WizardClient, prior_session_id: str) -> bool:
    """Did THIS session's round grade and bank a receipt? (#3486)

    The one fact that separates "the stage failed with nothing to pull" from
    "the round graded, then refused". A Full round's adoption tail runs inside
    the group-closing capture's own call, so a ``restore`` row comes back AS
    that capture's verdict and lands in the state's ``failure`` block — after
    ``coordinator._write_round_receipt`` has already banked the round. Read
    ``failure`` alone, precisely those rounds lose their evidence, and the
    restored/edge rounds are disproportionately the interesting ones: the
    campaign that filed this lost its first full spec pass that way.

    ``round_id`` is the stage-2 capture session id (``coordinator._round_identity``),
    so the equality below is what stops a PREVIOUS round's receipt — carried
    forward in durable state — from vouching for this one.
    """
    block = wizard.v2_block()
    session_id = str(block.get("session_id") or "")
    receipt = block.get("round_receipt")
    if not session_id or session_id == prior_session_id:
        return False
    return (
        isinstance(receipt, Mapping)
        and str(receipt.get("round_id") or "") == session_id
    )


def bank(dest: Path, *, since: str, target: Target, trail: Trail) -> int:
    """``bank-crossover-round.sh`` into ``dest``, with ITS verdict reported.

    The resolved host is exported rather than left to the script's own
    resolution, so a round cannot measure one speaker and bank another.
    """
    env = dict(os.environ)
    env.update({"PI_HOST": target.host, "PI_USER": target.user, "SINCE": since})
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "bank-crossover-round.sh"), str(dest)],
        env=env,
    )
    # 0 clean, 3 incomplete, 4 the destination was already used. Anything else
    # is bash's own failure and aborts too: 1 used to mean "no dump-ring
    # sidecars to grade", which was benign, and that ring is gone — so 1 is no
    # longer overloaded and no longer a pass. The NUMBER is what rides the
    # trail: a label spelled here would be this file's opinion of another
    # tool's contract, wrong the day that tool renumbers.
    ok = proc.returncode == 0
    trail.emit("bank", ok=ok, dest=str(dest), bank_exit=proc.returncode)
    return proc.returncode


def bank_position_cycle(dest: Path, *, staged: int, trail: Trail) -> None:
    """Derive ``position_cycle.json`` from the round that was just banked.

    AFTER the bank, because the input IS the bank: the bundle the bank
    untarred carries one record per accepted take, each stamped by the speaker
    with the bearing the microphone was actually at, and this projects them
    into one sorted index at the round root. Nothing here writes a fact of its
    own — see
    ``position_cycle.position_cycle_document``.

    ``staged`` is how many stops the walk was staged with. It is reported
    ALONGSIDE the derived count and never folded into the document: a shortfall
    between them is the interesting thing (poses that were refused, retaken, or
    never reached) and an index that quietly used the staged number to fill it
    in would be the intent-shaped record this design exists to avoid.

    Best-effort in exactly the way the candidate write is — a round that measured
    is not un-measured by a filesystem that would not take one more file, and the
    trail says so rather than the exit code. A failure is a printed line and an
    ``ok=false`` row naming what was missing, never a quiet return.
    """
    path = dest / POSITION_CYCLE_FILENAME
    try:
        document = position_cycle_document(dest)
        path.write_text(json.dumps(document, indent=2) + "\n")
    except (PositionCycleError, OSError) as exc:
        trail.emit("position_cycle", ok=False, staged=staged, detail=str(exc)[:300])
        print(f"round: the poses could not be indexed: {exc}", file=sys.stderr)
        return
    trail.emit(
        "position_cycle", banked=str(path), staged=staged,
        takes=len(document["takes"]), sources=document["sources"],
    )


def summarise_spec(block: Mapping[str, Any], trail: Trail) -> None:
    """Print the round's own flatness verdicts, band by band.

    Read off ``round_receipt.spec`` — the gauge the spec axis already
    computed for this round — so this print and the decision it accompanies
    cannot state different numbers. Nothing is derived here.

    It rides beside the candidate rather than inside it because it answers
    the other question: the candidate says what WOULD be applied, and this
    says what the last graded round MEASURED. A driver chaining rounds needs
    the tilt and the worst band to decide whether to run another one, and
    reading them out of ``state.json`` by hand is how a round gets chained on
    a number nobody looked at.
    """
    receipt = block.get("round_receipt")
    spec = receipt.get("spec") if isinstance(receipt, Mapping) else None
    if not isinstance(spec, Mapping):
        return
    graded = spec.get("graded_band_hz")
    print("\n=== flatness (last graded round) ===")
    print(
        f"  passed = {spec.get('passed')}"
        f"   worst = {_render(spec.get('max_db'))} dB"
        f" @ {_render(spec.get('max_hz'))} Hz"
        f"   graded = {_render(graded)} Hz"
    )
    for band in spec.get("bands") or ():
        if not isinstance(band, Mapping):
            continue
        print(
            f"  {_render(band.get('graded_lo_hz'))}-"
            f"{_render(band.get('graded_hi_hz'))} Hz"
            f"  passed={band.get('passed')}"
            f"  max={_render(band.get('max_deviation_db'))} dB"
            f" @ {_render(band.get('max_deviation_hz'))} Hz"
            f"  tol=+/-{_render(band.get('tolerance_db'))} dB"
        )
    tilt = spec.get("tilt")
    if isinstance(tilt, Mapping) and tilt.get("evaluable"):
        print(
            f"  tilt = {_render(tilt.get('step_db'))} dB"
            f"   high {_render(tilt.get('high_band_hz'))} Hz"
            f"   low {_render(tilt.get('low_band_hz'))} Hz"
        )
    trail.emit(
        "flatness",
        passed=spec.get("passed"),
        worst_db=spec.get("max_db"),
        worst_hz=spec.get("max_hz"),
        tilt_db=tilt.get("step_db") if isinstance(tilt, Mapping) else None,
        graded_band_hz=graded,
    )


def summarise_controllability(block: Mapping[str, Any], trail: Trail) -> None:
    """Print the raw per-band realization rows each banked round carries.

    Read off ``controllability`` — what the speaker banked, unpooled — so this
    print and the evidence behind it cannot state different numbers. Nothing is
    derived here: ``ratio`` is the fraction of commanded depth that arrived on
    THAT round, and pooling several rounds into a mean, a spread or a label is
    the reader's (ADR-0198).

    It answers the question ``flatness`` above cannot, because that block is
    one round: not "how flat is it now" but "in which bands did our commands
    land where we aimed them, round by round".
    """
    ledger = block.get("controllability")
    if not isinstance(ledger, Mapping):
        return
    rounds = [r for r in ledger.get("rounds") or () if isinstance(r, Mapping)]
    if not rounds:
        return
    n_rounds = ledger.get("n_rounds")
    plural = "" if n_rounds == 1 else "s"
    print(
        f"\n=== controllability ({_render(n_rounds)} banked round{plural}) ==="
    )
    for index, entry in enumerate(rounds, start=1):
        raw = entry.get("bands")
        bands = raw if isinstance(raw, Mapping) else {}
        misses = len(entry.get("spec_misses") or ())
        print(
            f"  round {index}  spec={entry.get('spec') or '?'}  misses={misses}"
        )
        for band_id, row in sorted(bands.items()):
            row = row if isinstance(row, Mapping) else {}
            span = row.get("band_hz")
            where = (
                f"{_render(span[0])}-{_render(span[1])} Hz"
                if isinstance(span, (list, tuple)) and len(span) == 2
                else "-"
            )
            print(
                f"    {band_id}  {where}"
                f"  ratio={_render(row.get('ratio'))}"
                f"  n_bins={_render(row.get('n_bins'))}"
                f"  graded={row.get('graded')}"
            )
    trail.emit(
        "controllability",
        n_rounds=n_rounds,
        # The whole per-round table, not a hand-picked scalar off it: the
        # claim IS the shape across bands and rounds, and a trail row carrying
        # one band's number would pin the wrong thing.
        rounds=rounds,
    )


def summarise_candidate(wizard: WizardClient, trail: Trail) -> str:
    """Print the live candidate the round just produced.

    Every scalar the candidate block carries is printed, sorted — never a
    hand-picked subset, which would silently stop showing a number the flow
    started publishing.

    It is not written beside the round: the bank already carries the
    speaker's own write-once ``candidate.json`` under
    ``bundle/<id>/evidence/v1/artifacts/crossover_v2/<sid>/``, and a second
    copy at the round root answered no question the first could not, and it
    had no reader.
    """
    block = wizard.v2_block()
    summarise_spec(block, trail)
    summarise_controllability(block, trail)
    candidate = block.get("candidate")
    if not isinstance(candidate, Mapping) or not candidate.get("fingerprint"):
        trail.emit("candidate", ok=False, detail="no candidate is published yet")
        return ""
    fingerprint = str(candidate["fingerprint"])
    trail.emit("candidate", fingerprint=fingerprint, phase=block.get("phase"))
    print(f"\n=== candidate {fingerprint} ===")
    for key in sorted(candidate):
        if key == "fingerprint":
            continue
        print(f"  {key} = {_render(candidate[key])}")
    print(
        "\nNothing has been applied. To put THIS candidate on the speaker:\n"
        # The interpreter and path the operator actually used, so the line is
        # copy-pasteable from wherever they ran this rather than from the one
        # working directory a hard-coded example would assume.
        f"  {sys.executable} {sys.argv[0]} --apply {fingerprint}\n"
    )
    return fingerprint


def apply_candidate(wizard: WizardClient, fingerprint: str, trail: Trail) -> int:
    """:func:`apply_by_fingerprint`, with this runner's trail rows and codes.

    What actually closes the hole is upstream of the guard — that an apply is a
    separate invocation naming the fingerprint at all. The chain that applied a
    candidate nobody had sanctioned filled that field in for itself.
    """
    result = apply_by_fingerprint(wizard, fingerprint)
    named = str(result["expected_candidate_fingerprint"])
    if result["status"] == "applied":
        payload = result["payload"]
        trail.emit(
            "apply", fingerprint=named, http=result["http"],
            outcome=result["outcome"],
            expected_post_apply_offset_db=(
                payload.get("expected_post_apply_offset_db")
                if isinstance(payload, Mapping) else None
            ),
        )
        return EXIT_OK
    if result["reason"] == REASON_ANSWER_LOST:
        # NOT a refusal: the apply POST left the laptop and nothing came back,
        # so whether the graph changed is unknown here and the crossover status
        # is what settles it.
        trail.emit("apply", ok=False, fingerprint=named, reason=REASON_ANSWER_LOST,
                   detail=error_of(result["payload"]))
        return EXIT_APPLY
    if result["refused_by"] == "wizard":
        trail.emit("apply", ok=False, fingerprint=named, http=result["http"],
                   outcome=result["outcome"] or "(none)",
                   detail=error_of(result["payload"]))
        return EXIT_APPLY
    trail.emit("apply", ok=False, named=named or "(empty)",
               live=result["candidate_fingerprint"], reason=result["reason"],
               detail="refused before any request left the laptop")
    return EXIT_FINGERPRINT


# --------------------------------------------------------------------------- #
# the round
# --------------------------------------------------------------------------- #


def _open_body(args: argparse.Namespace) -> tuple[str, dict[str, Any], int]:
    """The path, the body, and the exit code that names a failure to open it.

    The verify body carries no tier, and ``--tier`` is ignored for it: stage 2
    reads the instrument the MEASURING session recorded in durable state, so
    the household's one choice at the tier chooser governs both stages. A tier
    posted here would be a second, later answer to a question already settled.
    """
    if args.stage == "verify":
        return VERIFY_PATH, {STAGE_KEY: STAGE_POST_APPLY}, EXIT_VERIFY
    body: dict[str, Any] = {"tier": args.tier}
    if args.alignment_prescription is not None:
        # Passed through as read. The gate that judges a prescription is the
        # session open's own (shape, declared window, half-period lobe at Fc);
        # checking it here would be a second, weaker one.
        body[ALIGNMENT_PRESCRIPTION_KEY] = args.alignment_prescription
    if args.topology_prescription is not None:
        # Same rule as the alignment prescription above: passed through as
        # read, never judged here. TOPOLOGY_PRESCRIPTION_KEY is
        # topology_prescription.py's own constant, imported rather than
        # respelled, so the two names can never drift apart.
        body[TOPOLOGY_PRESCRIPTION_KEY] = args.topology_prescription
    return SESSION_PATH, body, EXIT_OPEN


def _open_failure_detail(status: int, payload: Any, target: Target) -> str:
    """The wizard's own words, plus the one cause its words cannot name.

    A 403 from the management-host guard says the Host header was not a name
    this speaker answers to — but not which name was sent or where it came
    from, and the pair is resolved from two independent sources (see
    ``resolve_target``). So a 403 gets both values appended. It is a
    possibility named for the reader, not a verdict: the guard also refuses for
    reasons that have nothing to do with a split identity.
    """
    detail = error_of(payload)
    if status == 403:
        detail += (
            f" [two things answer 403 here. The management-host guard, for a "
            f"Host it does not answer to: this round ssh'd to {target.host} "
            f"and sent Host: {target.hostname}, so if those are two different "
            f"speakers that is the cause and --hostname sets the second. Or "
            f"the CSRF pair, if the token could not be minted from "
            f"{CSRF_PAGE_PATH} — the wizard still starting looks like this]"
        )
    return detail


def run_round(args: argparse.Namespace, target: Target, wizard: WizardClient,
              trail: Trail) -> int:
    dest = args.campaign / args.label
    # UTC with the suffix spelled out: journalctl reads a naive timestamp in
    # the PI's timezone, so a laptop in a westward zone hands it a FUTURE
    # window and the bank pulls zero journal lines at exit 0.
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prior_session_id = str(wizard.v2_block().get("session_id") or "")
    trail.emit("target", host=target.ssh_target, hostname=target.hostname,
               base_url=target.base_url, stage=args.stage, dest=str(dest),
               since=since, prior_session_id=prior_session_id or "(none)")

    if args.angles:
        # Expanded HERE rather than at parse time so the trail's ``stage`` row
        # names the list that was actually staged, which is the one an operator
        # comparing it against ``--complete-after`` needs to see.
        rc = stage_walk(
            target,
            expand_angle_spec(args.angles, args.per_position),
            args.regime,
            trail,
            polarity=args.polarity,
            inverted_role=args.inverted_role,
            delayed_role=args.delayed_role,
            delay_us=args.delay_us,
            level_matched=args.level_matched,
        )
        if rc != 0:
            return EXIT_STAGE

    walk: subprocess.Popen[bytes] | None = None
    walk_log = args.campaign / f"{args.label}-arm-walk.log"
    walk_log_start = walk_log.stat().st_size if walk_log.exists() else 0
    if args.attest_rig_clear:
        args.campaign.mkdir(parents=True, exist_ok=True)
        walk = launch_walk(
            target,
            expect_angles=args.expect_angles,
            complete_after=args.complete_after,
            settle_s=args.settle_s,
            log_path=walk_log,
            trail=trail,
        )

    try:
        path, body, open_exit = _open_body(args)
        status, payload = wizard.post_json(path, body)
        if status != 200:
            trail.emit("open", ok=False, path=path, http=status,
                       detail=_open_failure_detail(status, payload, target))
            return open_exit
        trail.emit("open", path=path, http=status, body=body,
                   capture=_render(payload.get("capture") if isinstance(payload, Mapping)
                                 else payload))

        if walk is not None:
            walk_rc = walk.wait()
            walk_ok = walk_rc == 0
            trail.emit(
                "walk", ok=walk_ok, arm_walk_exit=walk_rc,
                arm_walk_exit_name=_walk_exit_name(
                    walk_rc, walk_log, walk_log_start
                ),
            )
            if not walk_ok:
                _say_bank_by_hand(dest, since, target)
                # ssh's own failure is not the walk's verdict, and the exit
                # name an operator reads has to agree with the trail line
                # above it.
                return (EXIT_SSH_TRANSPORT if walk_rc == SSH_TRANSPORT_EXIT
                        else EXIT_WALK)

        rc = await_stage(
            wizard, prior_session_id=prior_session_id,
            timeout_s=args.stage_timeout_s, poll_s=args.poll_s, trail=trail,
        )
        # The BANK is ordered on whether a round graded, not on the verdict
        # (#3486). A round that graded and then refused — every ``restore`` row
        # — has all its evidence on the Pi and the receipt already banked, so
        # skipping the pull loses exactly what doctrine §3 says every round
        # keeps. ``rc`` is untouched and still returned below: the round's own
        # verdict is the round's, and banking is not a second one.
        if rc != EXIT_OK and not round_graded_this_session(
            wizard, prior_session_id,
        ):
            _say_bank_by_hand(dest, since, target)
            return rc
    finally:
        # A walk this round is no longer driving must not outlive it: the
        # session it would serve next is a DIFFERENT one, and an arm still
        # moving for a finished round is the shape that banks angles nobody
        # asked for. The stop is a transport drop, and the park it triggers
        # happens on the speaker — see ``stop_walk``. A no-op once the walk has
        # exited on its own, which is every ordinary path through here.
        if walk is not None:
            stop_walk(walk, trail)

    bank_rc = bank(dest, since=since, target=target, trail=trail)
    if bank_rc != 0:
        # Nothing was kept, so the operator gets the hand command here too —
        # and a round that had already refused keeps ITS rc. ``EXIT_BANK``
        # says "the round was fine and only the pull was not"; overwriting a
        # ``session_failed`` with it drops the round's own verdict from what a
        # chaining caller reads, on exactly the restore-ending rounds #3486
        # ordered the bank for.
        _say_bank_by_hand(dest, since, target)
        return EXIT_BANK if rc == EXIT_OK else rc

    if args.angles:
        bank_position_cycle(
            dest,
            staged=staged_stops(expand_angle_spec(args.angles, args.per_position)),
            trail=trail,
        )
    summarise_candidate(wizard, trail)
    return rc


def _say_bank_by_hand(dest: Path, since: str, target: Target) -> None:
    """Nothing was banked; say how to keep the evidence anyway.

    Reached the two ways a round ends with its evidence unpulled: no round
    receipt was written for this session at all (a walk that stopped, a stage
    that never finished), or the bank itself refused. A round that graded and
    then refused banks itself (#3486); its evidence is complete and its verdict
    is on the receipt. The evidence is still on the Pi until the next round
    overwrites the dump ring, so the operator gets the one command that keeps it
    rather than a decision made for them.

    **It carries the host this round actually used.** Without it the pasted
    command re-resolves through ``.env.local``, which routinely names a
    different speaker than the one an exported ``PI_HOST`` just measured — so
    the one line printed on the one path where a human is asked to bank by hand
    would be the line that banks the wrong Pi. Absolute repo path for the same
    reason: this prints wherever the operator happened to be standing.
    """
    script = REPO_ROOT / "scripts" / "bank-crossover-round.sh"
    print(
        f"\nround: nothing was banked. The evidence is still on the Pi:\n"
        f"  PI_HOST={shlex.quote(target.host)} PI_USER={shlex.quote(target.user)} "
        f"SINCE={shlex.quote(since)} bash {shlex.quote(str(script))} "
        f"{shlex.quote(str(dest))}\n",
        file=sys.stderr,
    )


def _json_document(raw: str) -> Any:
    """One prescription file — ``--alignment-prescription`` or
    ``--topology-prescription`` — read and parsed at parse time.

    An unreadable path or malformed JSON is an argument the operator wrote
    wrongly, so it is argparse's refusal — a sentence and the usage — rather
    than a traceback from somewhere between a staged walk and an open. What
    the document SAYS is never judged here; that is the open's own gate.
    """
    try:
        return json.loads(Path(raw).read_text())
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{raw}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-crossover-round.py",
        description=(
            "Run one crossover-v2 round end to end — stage, walk, open, await, "
            "bank — and print the candidate it produced. Never applies; "
            "--apply is a separate invocation that names the fingerprint."
        ),
    )
    parser.add_argument(
        "--apply", metavar="FINGERPRINT", default=None,
        help=(
            "apply the candidate with THIS fingerprint, and nothing else. "
            "Refused, with nothing sent, when the live candidate is a "
            "different one. Runs no measurement"
        ),
    )
    parser.add_argument(
        "--campaign", type=Path, default=None,
        help="the campaign directory a round banks into (required to measure)",
    )
    parser.add_argument(
        "--label", default=None,
        help="this round's name inside the campaign directory (required to measure)",
    )
    parser.add_argument(
        "--stage", choices=("measure", "verify"), default="measure",
        help=(
            "measure opens a new session; verify opens the post-apply check "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--tier", choices=sorted(TIERS), default="remote",
        help=(
            "the commission instrument this session measures with, sent "
            "explicitly on every open — an absent tier silently inherits the "
            "last session's (default: %(default)s). Ignored by --stage verify, "
            "which takes the instrument the MEASURING session recorded"
        ),
    )
    parser.add_argument(
        "--polarity", default=None, choices=("normal", "inverted"),
        help="stage the walk with one driver branch sign-flipped (R-1). "
             "Pair with --inverted-role. The reverse-null confirmation needs "
             "it; a normal round does not.",
    )
    parser.add_argument(
        "--inverted-role", default=None,
        help="which driver branch --polarity inverted flips, e.g. tweeter",
    )
    parser.add_argument(
        "--delayed-role", default=None,
        help="which driver branch carries the confirmation delay (R-1's "
             "DISPOSE half). Pair with --delay-us.",
    )
    parser.add_argument(
        "--delay-us", type=float, default=None,
        help="the confirmation coordinate in microseconds, non-negative",
    )
    parser.add_argument(
        "--level-matched", action="store_true",
        help="stage the walk with the speaker's own per-driver level match "
             "applied to the measurement graph, so branches of unequal "
             "sensitivity can null. The reverse-null confirmation needs it; a "
             "normal round does not.",
    )
    parser.add_argument(
        "--alignment-prescription", type=_json_document, default=None,
        metavar="FILE",
        help=(
            "a JSON document posted verbatim as the session's alignment "
            "prescription; the open's own gate judges it"
        ),
    )
    parser.add_argument(
        "--topology-prescription", type=_json_document, default=None,
        metavar="FILE",
        help=(
            "a JSON document posted verbatim as the session's topology "
            "prescription; the open's own gate judges it"
        ),
    )
    parser.add_argument(
        "--angles", default="",
        help=(
            "stage an arm walk at these whole degrees before opening "
            "(e.g. 0,7,-7,22,-22). Omitted, nothing is staged. Requires "
            "--attest-rig-clear: a staged walk is an ARM walk, and one nobody "
            "will serve is refused rather than run"
        ),
    )
    parser.add_argument(
        # ``None`` rather than ``1`` so "the operator passed it" is answerable:
        # --apply refuses it at ANY value, and a default of 1 would make the
        # explicit ``--apply --per-position 1`` indistinguishable from not
        # passing it at all.
        "--per-position", type=int, default=None, metavar="N",
        help=(
            "take N captures at EACH staged angle, adjacently, in one walk — "
            "the arm settles and releases N times without travelling, so what "
            "varies between the takes is time and whatever you changed between "
            "them, never the pose. Requires --angles, --stage measure, and a "
            "regime that composes one stop per angle (per_driver or summed; "
            "both composes two, so it is refused). There is no ceiling here: "
            "how many stops a session can carry is the plan's own, and it "
            "refuses by name. --complete-after counts RELEASES, so it must "
            "scale with N (default: 1)"
        ),
    )
    parser.add_argument(
        "--regime", default="per_driver",
        help=(
            "what a staged walk plays at each angle. A session walk plays "
            "only per_driver at every pose today, and refuses any other "
            "regime when it opens -- this runner does not enforce that "
            "itself (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--attest-rig-clear", action="store_true",
        help=(
            "YOUR attestation, forwarded to the walk: the arm's travel "
            "is clear and the saved zero is the acoustic axis. Without it no "
            "walk is launched — this runner never attests on your behalf — "
            "and --angles is refused, since nothing would serve the walk it "
            "stages"
        ),
    )
    parser.add_argument(
        "--expect-angles", default="",
        help="passed through to the walk: the angles it must serve",
    )
    parser.add_argument(
        "--complete-after", type=int, default=None,
        help="passed through to the walk: releases before it closes the group",
    )
    parser.add_argument(
        "--settle-s", type=float, default=None,
        help="passed through to the walk: settle after each move",
    )
    parser.add_argument(
        "--hostname", default=None,
        help=(
            "the speaker's own name, used as the Host header and given to the "
            "walk (default: JASPER_HOSTNAME, else the ssh host)"
        ),
    )
    parser.add_argument(
        "--base-url", default=None,
        help="where the wizard is reached (default: http://$PI_HOST)",
    )
    parser.add_argument(
        "--stage-timeout-s", type=float, default=900.0,
        help="how long a stage may take to finish after its walk (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-s", type=float, default=5.0,
        help="how often the envelope is read (default: %(default)s)",
    )
    parser.add_argument(
        "--trail", type=Path, default=None,
        help="append one JSON object per step to this file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.apply is not None:
        # An EMPTY fingerprint is refused here rather than compared. A live
        # candidate that is absent also reads as "", so `"" == ""` would sail
        # through the gate and POST — the one thing this tool promises never to
        # do on a fingerprint the operator did not actually name.
        if not args.apply.strip():
            parser.error("--apply needs the fingerprint you mean to put on the speaker")
        for flag, value in (("--campaign", args.campaign), ("--label", args.label),
                            ("--angles", args.angles or None)):
            if value:
                parser.error(f"--apply runs no measurement, so {flag} means nothing")
        # Its own check rather than a row in that loop, because the question is
        # WAS IT PASSED and not IS IT TRUTHY: `--per-position 0` is a value the
        # operator typed, and a truthiness test would let it through to be
        # silently ignored by a path that measures nothing.
        if args.per_position is not None:
            parser.error(
                "--apply runs no measurement, so --per-position means nothing"
            )
    elif not args.campaign or not args.label:
        parser.error("--campaign and --label are required to measure a round")
    elif args.per_position is not None and args.per_position < 1:
        parser.error(
            f"--per-position is how many captures to take at each pose, so it "
            f"is at least 1; got {args.per_position}"
        )
    elif args.per_position is not None and not args.angles:
        # The takes are stops in a staged walk. Without --angles there is no
        # walk to put them in, and the round would run its ordinary shape while
        # the operator believed it was cycling — the failure mode this refuses
        # is a night's evidence that silently answers a different question.
        parser.error(
            "--per-position repeats each STAGED angle, so it needs --angles"
        )
    elif args.per_position is not None and args.stage != "measure":
        # Stage 2 serves the tier's OWN poses: `_take_staged_angle_walk` is
        # reached only from the measuring open, and the verify plan's positions
        # and count come from `plan_shape.verify_capture_target`. A walk staged
        # for it is taken by nobody, so the takes would never happen.
        parser.error(
            f"--stage {args.stage} serves the tier's own poses, so nothing "
            f"would take a staged walk; --per-position governs a staged "
            f"measure walk"
        )
    elif args.per_position is not None and len(_REGIME_STOPS.get(args.regime, ())) != 1:
        # `staged_stops` counts TOKENS (angle x _REGIME_STOPS[regime]), the exact
        # stop count for every single-regime entry in that TABLE. The TABLE is
        # asked, never a hardcoded list of regime names — a named list is what
        # went stale and refused `summed` for no reason once before. `both`
        # pairs two stops per token, so the --complete-after floor below would
        # be half the real count; an unknown regime composes zero, and the seam
        # refuses it moments later anyway. Refused rather than multiplied: a
        # multiplier here would be this file's own opinion about another tool's
        # composition rule. NOT a nanny gate (measurement-loop doctrine §5): the
        # FLAG is declined, never the experiment — stage the repeats by hand
        # (--angles 0,0,0,7,7,7) at any regime.
        composed = len(_REGIME_STOPS.get(args.regime, ()))
        parser.error(
            f"--per-position counts one stop per angle, and --regime "
            f"{args.regime} composes "
            + (f"{composed} stops per angle"
               if composed
               else "a stop count this runner cannot read")
            + ", so the --complete-after floor below would be wrong. The WALK "
              "is not refused — stage the repeats yourself "
              "(--angles 0,0,0,7,7,7) and this runner counts nothing on your "
              "behalf"
        )
    elif args.angles and not args.attest_rig_clear:
        # A staged walk is an ARM walk, and without the attestation no arm walk
        # is launched — so the session would open, hold at its first position
        # for the gate's full 600 s, and end as a misnamed idle-ceiling exit ten
        # minutes later. Nothing downstream can rescue that configuration, so it
        # is refused before it costs the ten minutes.
        parser.error(
            "--angles stages a walk for the arm, which only runs with "
            "--attest-rig-clear; add it, or drop --angles"
        )
    # One resolution point for the unset default, AFTER every check that had to
    # know whether the operator typed it. Everything downstream sees an int.
    if args.per_position is None:
        args.per_position = 1

    if args.apply is None and args.angles and args.complete_after is not None:
        # --complete-after counts RELEASES (arm_walk._complete_due), so a walk
        # told to complete on fewer of them than it has stops posts its
        # all-spots-measured signal partway through and exits `ok` — a round that
        # measured a walk nobody asked for, with no failing code to say so. It is
        # only a FLOOR: the session's own non-walk captures are gated holds too,
        # so the honest number is usually higher, and this runner cannot know it
        # (the count is the tier's, decided on the speaker). Refusing the
        # arithmetic it CAN do beats refusing none.
        #
        # `staged_stops` counts TOKENS, which is the exact stop count for every
        # regime composing ONE stop per angle — every single-regime entry in
        # `_REGIME_STOPS`. For `both` it is half, and this still runs there,
        # safely: a regime that
        # stages MORE stops per angle can only make the real count larger, so
        # this can never refuse a `--complete-after` that would have been fine —
        # it just catches less. `--per-position` is refused outright for `both`
        # (above), because there the floor is what the flag's whole arithmetic
        # rests on rather than a bonus.
        stops = staged_stops(expand_angle_spec(args.angles, args.per_position))
        if args.complete_after < stops:
            parser.error(
                f"--complete-after counts releases and this walk stages {stops} "
                f"stops ({args.per_position} per position), so "
                f"{args.complete_after} would complete it partway through; pass "
                f"{stops} or higher, or omit it"
            )

    # The trail opens FIRST: resolving which speaker this is can itself fail,
    # and that failure is the first thing worth a row.
    trail = Trail(args.trail)
    try:
        target = resolve_target(args.hostname, trail)
        wizard = WizardClient(
            base_url=args.base_url or target.base_url, host_header=target.hostname
        )
        if args.apply is not None:
            trail.emit("target", host=target.ssh_target, hostname=target.hostname,
                       base_url=args.base_url or target.base_url, stage="apply")
            code = apply_candidate(wizard, args.apply, trail)
        else:
            code = run_round(args, target, wizard, trail)
    except NoTarget as exc:
        print(exc, file=sys.stderr)
        code = EXIT_CONFIG
    finally:
        trail.close()
    print(
        f"round finished: {EXIT_NAMES.get(code, str(code))} (rc {code})",
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
