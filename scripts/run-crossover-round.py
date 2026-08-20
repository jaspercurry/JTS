#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run one crossover-v2 measurement round end to end, from the laptop.

Every campaign has rebuilt this as a chain of throwaway shell — ``stage1.sh``
launches a walk driver, sleeps, POSTs the session open, greps a log for
"released index=N", sleeps again; ``cycle.sh`` does the same around the verify
and then banks. The pieces those scripts drove are all product now
(``jasper-angle-capture``, ``jasper-arm-walk``, ``bank-crossover-round.sh``,
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
    launch the walk  ssh … jasper-arm-walk                (--attest-rig-clear)
    open the session POST /correction/crossover/v2/{session,verify}
    await the walk   the arm harness exits; its rc is the walk's verdict
    await the stage  poll the envelope until this session's phases are accepted
    bank             bash scripts/bank-crossover-round.sh <campaign>/<label>
    summarise        the candidate's fingerprint + numbers, printed and banked

The walk starts BEFORE the open because ``jasper-arm-walk``'s first poll is
what checks a staged walk is still waiting — see its module docstring. It is
launched only when ``--attest-rig-clear`` is given: the attestation is the
operator's to make and this runner never invents one. Without it the round
runs the same phases minus the walk, for a human-moved or ordinary session.

**Nothing here re-maps another tool's verdict.** ``jasper-arm-walk``'s exit
codes and ``bank-crossover-round.sh``'s 0/1/2/3/4 are reported verbatim, by
their owners' own names, as the deciding value on this runner's own per-phase
exit code. A failing walk stops the round before it banks: the walk's rc is
the verdict, and a bank on top of it would be a second one.

Usage::

    # measure (stage 1), with the lab arm walking five angles
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \\
        --campaign captures/my-night --label r1 --tier remote \\
        --angles 0,7,-7,22,-22 --regime per_driver \\
        --attest-rig-clear --expect-angles 7,-7,22,-22 --complete-after 5

    # …read the printed candidate, decide, THEN apply it by name
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py --apply <fp>

    # the post-apply check (stage 2)
    PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \\
        --campaign captures/my-night --label r1-verify --stage verify \\
        --attest-rig-clear --expect-angles 7,-7,22,-22 --complete-after 5

``PI_HOST`` / ``PI_USER`` exported by the caller win over ``.env.local``, which
wins over the ``jts.local`` default — the resolution is ``scripts/_lib.sh``'s
own, with the caller's exports re-applied over it exactly as
``bank-crossover-round.sh`` does (issue #2689), and the resolved host is named
in the run trail. The same values are exported into the bank script, so both
halves of a round can never target different speakers.
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
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jasper.active_speaker.arm_walk import (
    CSRF_PAGE_PATH,
    STATUS_PATH,
)
from jasper.active_speaker.arm_walk import EXIT_NAMES as ARM_WALK_EXIT_NAMES
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_PRESCRIPTION_KEY,
)
from jasper.active_speaker.crossover_v2.journey import (
    CAPTURE_PHASES,
    PHASE_CLOSING,
)
from jasper.active_speaker.crossover_v2_flow import TIERS
from jasper.web.correction_crossover_v2 import (
    VERIFY_STAGE_KEY,
    VERIFY_STAGE_POST_APPLY,
)

# --------------------------------------------------------------------------- #
# the endpoints and the vocabulary — the product's own, never restated
# --------------------------------------------------------------------------- #

#: The two stage-opening POSTs and the apply. Members of ``correction_setup``'s
#: ``_POST_ROUTES`` under the wizard's ``/correction`` prefix — pinned against
#: that frozenset by ``tests/test_run_crossover_round.py`` so a renamed route
#: fails here instead of 404ing mid-round.
SESSION_PATH = "/correction/crossover/v2/session"
VERIFY_PATH = "/correction/crossover/v2/verify"
APPLY_PATH = "/correction/crossover/v2/apply"

#: The phases a stage is still WORKING in. Derived from the flow's own tuple, so
#: a phase added to the journey counts as running here without an edit — the
#: alternative, a hand-listed terminal set, would silently call a new capture
#: phase "finished" and bank a round mid-measurement.
RUNNING_PHASES = frozenset(CAPTURE_PHASES) | {PHASE_CLOSING}

#: Where ``install.sh`` puts the runtime the two Pi-side CLIs live in.
PI_VENV_BIN = "/opt/jasper/.venv/bin"

#: How long a terminated walk is given to park the arm before it is killed.
#: The park is a move plus ``arm_walk.PARK_SETTLE_S`` plus a readback, all
#: bounded by the adapter's own subprocess timeout; this is that with room.
WALK_PARK_GRACE_S = 90.0

_CSRF_META_RE = re.compile(r'<meta name="jts-csrf" content="([^"]+)"')

# --------------------------------------------------------------------------- #
# exit codes — part of the contract, because the caller of this tool is a script
# --------------------------------------------------------------------------- #

EXIT_OK = 0
#: ``jasper-angle-capture stage`` refused or could not bank the walk. Its own
#: exit code (2 refused / 3 filesystem) is the deciding value on the line.
EXIT_STAGE = 3
#: ``POST …/v2/session`` did not open. The wizard's own words are on the line.
EXIT_OPEN = 4
#: The arm walk did not finish clean. ``jasper-arm-walk``'s rc and ITS name for
#: that rc are the deciding value; nothing is re-mapped and nothing is banked.
EXIT_WALK = 5
#: ``POST …/v2/verify`` did not open.
EXIT_VERIFY = 6
#: The stage never left its running phases inside ``--stage-timeout-s``.
EXIT_INCOMPLETE = 7
#: The session itself reported a failure. Its own error is on the line.
EXIT_SESSION_FAILED = 8
#: ``bank-crossover-round.sh`` refused: 2 dirty captures, 3 an incomplete bank,
#: 4 a destination that was already used. Its rc is the deciding value.
EXIT_BANK = 9
#: The apply POST was refused, blocked, or failed. Nothing was rolled back here
#: — the endpoint's own transaction owns that.
EXIT_APPLY = 10
#: The fingerprint named on the command line is not the live candidate's.
#: NOTHING was POSTed. This is the gate.
EXIT_FINGERPRINT = 11

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
}


# --------------------------------------------------------------------------- #
# the trail — one call, two surfaces
# --------------------------------------------------------------------------- #


class Trail:
    """A step line on stdout and the same fields as a JSONL row.

    The same shape ``jasper-arm-walk``'s trail holds, and for the same reason:
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


#: Read ``scripts/_lib.sh``'s resolution rather than parsing ``.env.local`` a
#: second time: that file is the checkout's single source of truth for "which
#: Pi", and a second reader is a second answer waiting to happen.
_LIB_QUERY = (
    'set -u; . "$0" >/dev/null 2>&1; '
    'printf "%s\\n%s\\n%s\\n" "$PI_HOST" "$PI_USER" "${JASPER_HOSTNAME:-}"'
)


def resolve_target(hostname_override: str | None = None) -> Target:
    """The speaker, with the caller's own exports winning.

    ``_lib.sh`` sources ``.env.local`` with ``set -a``, which overwrites an
    already-exported ``PI_HOST``/``PI_USER`` before its own ``${PI_HOST:-…}``
    fallback can prefer the caller's (issue #2689, repo-wide). So the caller's
    exports are read here FIRST and re-applied over whatever the library
    resolved — the same fix, and the same order, as
    ``bank-crossover-round.sh``.
    """
    caller_host = os.environ.get("PI_HOST") or ""
    caller_user = os.environ.get("PI_USER") or ""
    caller_hostname = os.environ.get("JASPER_HOSTNAME") or ""
    lib_host = lib_user = lib_hostname = ""
    proc = subprocess.run(
        ["bash", "-c", _LIB_QUERY, str(REPO_ROOT / "scripts" / "_lib.sh")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode == 0:
        lines = proc.stdout.splitlines()
        lib_host, lib_user, lib_hostname = (lines + ["", "", ""])[:3]
    host = caller_host or lib_host or "jts.local"
    user = caller_user or lib_user or "pi"
    hostname = (
        hostname_override or caller_hostname or lib_hostname or host
    )
    return Target(host, user, hostname)


# --------------------------------------------------------------------------- #
# the wizard, over the LAN
# --------------------------------------------------------------------------- #


class Wizard:
    """The correction wizard, reached from the laptop with the speaker's Host.

    The transport ``jasper-arm-walk``'s ``LoopbackSession`` holds, moved off
    the speaker: the management-host guard keys on ``Host:``, and the mutating
    POSTs carry the double-submit CSRF pair — the cookie from the jar and the
    token from the page's ``<meta name="jts-csrf">`` — exactly as a browser's
    ``fetch()`` does. ``LoopbackSession``'s own public surface is the position
    gate's three verbs, so this states the two generic ones a round needs
    (``v2_block`` is built on them) rather than reaching into a Pi-side
    module's private methods.
    """

    def __init__(
        self,
        *,
        base_url: str,
        host_header: str,
        timeout_s: float = 120.0,
        opener: Any | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._host = host_header
        self._timeout = timeout_s
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self._csrf: str | None = None

    def _open(
        self,
        path: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        request = urllib.request.Request(
            self._base + path,
            data=data,
            headers={"Host": self._host, **(headers or {})},
            method="POST" if data is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")
        except (OSError, ValueError) as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    def _csrf_token(self) -> str:
        """Minted once and reused — but only once actually MINTED.

        Caching a failed mint would 403 every later POST in the run with
        nothing saying why; ``arm_walk`` hit exactly that and this holds its
        fix.
        """
        if self._csrf:
            return self._csrf
        _, body = self._open(CSRF_PAGE_PATH)
        match = _CSRF_META_RE.search(body)
        self._csrf = match.group(1) if match else None
        return self._csrf or ""

    def get_json(self, path: str) -> tuple[int, Any]:
        status, body = self._open(path)
        return status, _as_json(body)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        status, body = self._open(
            path,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self._csrf_token(),
            },
        )
        return status, _as_json(body)

    def v2_block(self) -> dict[str, Any]:
        """``status["crossover_v2"]`` — phase, candidate, failure, session id.

        ``{}`` when the envelope is unreadable or carries no v2 block, which
        every caller here treats as "nothing known yet" rather than as a
        verdict.
        """
        status, payload = self.get_json(STATUS_PATH)
        if status != 200 or not isinstance(payload, Mapping):
            return {}
        block = payload.get("crossover_v2")
        return dict(block) if isinstance(block, Mapping) else {}


def _as_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return body


def _error_of(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("error") or payload.get("status") or payload)[:200]
    return str(payload)[:200]


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


def stage_walk(target: Target, angles: str, regime: str, trail: Trail) -> int:
    """``jasper-angle-capture stage`` on the Pi. Its refusal is its own.

    ``--angles`` and ``--regime`` are passed through as the operator wrote
    them: bounds, whole-degree-ness and the regime vocabulary are the seam's,
    and a second validator here would be a second answer to the same question.
    """
    remote = (
        f"sudo {PI_VENV_BIN}/jasper-angle-capture stage --mover arm "
        f"--angles {shlex.quote(angles)} --regime {shlex.quote(regime)} --json"
    )
    proc = subprocess.run(
        [*SSH_OPTS, target.ssh_target, remote],
        capture_output=True, text=True, timeout=120,
    )
    ok = proc.returncode == 0
    trail.emit(
        "stage", ok=ok, angles=angles, regime=regime, mover="arm",
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
    """``jasper-arm-walk`` on the Pi, in the background, output to ``log_path``.

    ``-tt`` so a terminate reaches the walk itself rather than only the local
    ssh client: the walk's SIGTERM handler IS its park, and an abandoned arm at
    an unknown angle is the one state the rig must never be left in. Local
    stdin is ``DEVNULL`` so forcing a remote tty cannot put the operator's own
    terminal into raw mode.
    """
    argv = [
        f"sudo -u pi {PI_VENV_BIN}/jasper-arm-walk",
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
    handle = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [*SSH_OPTS, "-tt", target.ssh_target, remote],
        stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
    )
    trail.emit(
        "walk_launched", host=target.ssh_target, expect_angles=expect_angles or "(none)",
        complete_after=complete_after, log=str(log_path),
    )
    return proc


def stop_walk(proc: subprocess.Popen[bytes], trail: Trail) -> None:
    """Terminate a walk this round can no longer use, and let it park."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=WALK_PARK_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        trail.emit("walk_stopped", ok=False,
                   detail=f"the walk did not park inside {WALK_PARK_GRACE_S:.0f}s")
        return
    trail.emit("walk_stopped", detail="terminated; the arm parks on its way out")


def await_stage(
    wizard: Wizard,
    *,
    prior_session_id: str,
    timeout_s: float,
    poll_s: float,
    trail: Trail,
) -> int:
    """Poll until THIS session's phases are all accepted, or it fails.

    Two conditions, and the first one is why a sleep was never enough: the
    session id must have MOVED off the one that was there before the open, or a
    previous round's terminal phase would read as this round's completion the
    moment the poll started. Then the phase must leave the running set, which
    is the flow's own answer to "is every phase this session ran accepted"
    (``_phase_from_state`` reads ``session_phases`` against
    ``accepted_phases``).
    """
    deadline = time.monotonic() + timeout_s
    phase = ""
    while time.monotonic() < deadline:
        block = wizard.v2_block()
        phase = str(block.get("phase") or "")
        failure = block.get("failure")
        if failure:
            trail.emit("await", ok=False, phase=phase, failure=_render(failure))
            return EXIT_SESSION_FAILED
        session_id = str(block.get("session_id") or "")
        if session_id and session_id != prior_session_id and phase not in RUNNING_PHASES:
            trail.emit("await", phase=phase, session_id=session_id)
            return EXIT_OK
        time.sleep(poll_s)
    trail.emit("await", ok=False, phase=phase or "(unreadable)",
               waited_s=round(timeout_s, 1))
    return EXIT_INCOMPLETE


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
    # 0 clean, 1 nothing to grade (no dump-ring sidecars — the operator never
    # armed it, which is not a dirty round), 2 dirty, 3 incomplete, 4 the
    # destination was already used. Only the last three abort. The NUMBER is
    # what rides the trail: a label spelled here would be this file's opinion
    # of another tool's contract, wrong the day that tool renumbers.
    ok = proc.returncode in (0, 1)
    trail.emit("bank", ok=ok, dest=str(dest), bank_exit=proc.returncode)
    return proc.returncode


def summarise_candidate(wizard: Wizard, dest: Path, trail: Trail) -> str:
    """Print the live candidate and bank it beside the round it came from.

    Every scalar the candidate block carries is printed, sorted — never a
    hand-picked subset, which would silently stop showing a number the flow
    started publishing. Written AFTER the bank: the bank refuses a destination
    that already has something in it.
    """
    block = wizard.v2_block()
    candidate = block.get("candidate")
    if not isinstance(candidate, Mapping) or not candidate.get("fingerprint"):
        trail.emit("candidate", ok=False, detail="no candidate is published yet")
        return ""
    fingerprint = str(candidate["fingerprint"])
    path = dest / "candidate.json"
    try:
        path.write_text(json.dumps(dict(candidate), indent=2, sort_keys=True) + "\n")
        banked: str | bool = str(path)
    except OSError as exc:
        banked = False
        print(f"round: candidate could not be banked: {exc}", file=sys.stderr)
    trail.emit("candidate", fingerprint=fingerprint, phase=block.get("phase"),
               banked=banked)
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


def apply_candidate(wizard: Wizard, fingerprint: str, trail: Trail) -> int:
    """The gate, then the apply. A mismatch POSTs nothing at all.

    **The endpoint runs the same comparison server-side** and would refuse the
    same request, so this is not a second opinion about the candidate. What it
    adds is that nothing is SENT: a mistyped or stale fingerprint ends on the
    laptop rather than becoming a state-changing request against a speaker,
    and both values are named where the operator can read them instead of
    arriving as one sentence inside a 400.

    What actually closes the hole is upstream of either check — that an apply
    is a separate invocation naming the fingerprint at all. The chain that
    applied a candidate nobody had sanctioned filled that field in for itself.
    """
    live = ""
    block = wizard.v2_block()
    candidate = block.get("candidate")
    if isinstance(candidate, Mapping):
        live = str(candidate.get("fingerprint") or "")
    if live != fingerprint:
        # An empty block and a published-but-different candidate are the same
        # refusal and DIFFERENT diagnoses: the first is usually an unreadable
        # envelope (wrong host, wizard down), and reporting it as "no candidate
        # yet" would send an operator to measure again over a broken link.
        trail.emit("apply", ok=False, named=fingerprint,
                   live=live or ("(no candidate is published)" if block
                                 else "(the envelope carried no v2 state)"),
                   detail="refused before any request left the laptop")
        return EXIT_FINGERPRINT
    status, payload = wizard.post_json(
        APPLY_PATH, {"expected_candidate_fingerprint": fingerprint}
    )
    # ``apply_failed`` comes back 200 and ``blocked`` comes back 409, so the
    # body's own status is the verdict — the HTTP code alone is not.
    outcome = (
        str(payload.get("status") or "")
        if isinstance(payload, Mapping) else ""
    )
    if status != 200 or outcome != "applied":
        trail.emit("apply", ok=False, fingerprint=fingerprint, http=status,
                   outcome=outcome or "(none)", detail=_error_of(payload))
        return EXIT_APPLY
    trail.emit(
        "apply", fingerprint=fingerprint, http=status, outcome=outcome,
        expected_post_apply_offset_db=(
            payload.get("expected_post_apply_offset_db")
            if isinstance(payload, Mapping) else None
        ),
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# the round
# --------------------------------------------------------------------------- #


def _open_body(args: argparse.Namespace) -> tuple[str, dict[str, Any], int]:
    """The path, the body, and the exit code that names a failure to open it."""
    if args.stage == "verify":
        return VERIFY_PATH, {VERIFY_STAGE_KEY: VERIFY_STAGE_POST_APPLY}, EXIT_VERIFY
    body: dict[str, Any] = {"tier": args.tier}
    if args.alignment_prescription is not None:
        # Passed through as read. The gate that judges a prescription is the
        # session open's own (shape, declared window, half-period lobe at Fc);
        # checking it here would be a second, weaker one.
        body[ALIGNMENT_PRESCRIPTION_KEY] = args.alignment_prescription
    return SESSION_PATH, body, EXIT_OPEN


def run_round(args: argparse.Namespace, target: Target, wizard: Wizard,
              trail: Trail) -> int:
    dest = args.campaign / args.label
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prior_session_id = str(wizard.v2_block().get("session_id") or "")
    trail.emit("target", host=target.ssh_target, hostname=target.hostname,
               base_url=target.base_url, stage=args.stage, dest=str(dest),
               since=since, prior_session_id=prior_session_id or "(none)")

    if args.angles:
        rc = stage_walk(target, args.angles, args.regime, trail)
        if rc != 0:
            return EXIT_STAGE

    walk: subprocess.Popen[bytes] | None = None
    if args.attest_rig_clear:
        args.campaign.mkdir(parents=True, exist_ok=True)
        walk = launch_walk(
            target,
            expect_angles=args.expect_angles,
            complete_after=args.complete_after,
            settle_s=args.settle_s,
            log_path=args.campaign / f"{args.label}-arm-walk.log",
            trail=trail,
        )

    try:
        path, body, open_exit = _open_body(args)
        status, payload = wizard.post_json(path, body)
        if status != 200:
            trail.emit("open", ok=False, path=path, http=status,
                       detail=_error_of(payload))
            return open_exit
        trail.emit("open", path=path, http=status, body=body,
                   relay=_render(payload.get("relay") if isinstance(payload, Mapping)
                                 else payload))

        if walk is not None:
            walk_rc = walk.wait()
            walk_ok = walk_rc == 0
            trail.emit(
                "walk", ok=walk_ok, arm_walk_exit=walk_rc,
                arm_walk_exit_name=ARM_WALK_EXIT_NAMES.get(walk_rc, str(walk_rc)),
            )
            if not walk_ok:
                _say_bank_by_hand(dest, since)
                return EXIT_WALK

        rc = await_stage(
            wizard, prior_session_id=prior_session_id,
            timeout_s=args.stage_timeout_s, poll_s=args.poll_s, trail=trail,
        )
        if rc != EXIT_OK:
            _say_bank_by_hand(dest, since)
            return rc
    finally:
        # A walk this round is no longer driving must not outlive it: the
        # session it would serve next is a DIFFERENT one, and an arm still
        # moving for a finished round is the shape that banks angles nobody
        # asked for. A no-op once the walk has exited on its own, which is
        # every ordinary path through here.
        if walk is not None:
            stop_walk(walk, trail)

    bank_rc = bank(dest, since=since, target=target, trail=trail)
    if bank_rc not in (0, 1):
        return EXIT_BANK

    summarise_candidate(wizard, dest, trail)
    return EXIT_OK


def _say_bank_by_hand(dest: Path, since: str) -> None:
    """A stopped round banks nothing; say how to keep the evidence anyway.

    The walk's rc, or the stage's failure to finish, IS the round's verdict,
    and banking on top of it would put a second verdict on the same run. The
    evidence is still on the Pi until the next round overwrites the dump ring,
    so the operator gets the one command that keeps it rather than a decision
    made for them.
    """
    print(
        f"\nround: nothing was banked. The evidence is still on the Pi:\n"
        f"  SINCE={shlex.quote(since)} bash scripts/bank-crossover-round.sh "
        f"{shlex.quote(str(dest))}\n",
        file=sys.stderr,
    )


def _json_document(raw: str) -> Any:
    """One ``--alignment-prescription`` file, read and parsed at parse time.

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
            "last session's (default: %(default)s)"
        ),
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
        "--angles", default="",
        help=(
            "stage an arm walk at these whole degrees before opening "
            "(e.g. 0,7,-7,22,-22). Omitted, nothing is staged"
        ),
    )
    parser.add_argument(
        "--regime", default="per_driver",
        help="what a staged walk plays at each angle (default: %(default)s)",
    )
    parser.add_argument(
        "--attest-rig-clear", action="store_true",
        help=(
            "YOUR attestation, forwarded to jasper-arm-walk: the arm's travel "
            "is clear and the saved zero is the acoustic axis. Without it no "
            "walk is launched — this runner never attests on your behalf"
        ),
    )
    parser.add_argument(
        "--expect-angles", default="",
        help="passed through to jasper-arm-walk: the angles the walk must serve",
    )
    parser.add_argument(
        "--complete-after", type=int, default=None,
        help="passed through to jasper-arm-walk: releases before it closes the group",
    )
    parser.add_argument(
        "--settle-s", type=float, default=None,
        help="passed through to jasper-arm-walk: settle after each move",
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
        for flag, value in (("--campaign", args.campaign), ("--label", args.label),
                            ("--angles", args.angles or None)):
            if value:
                parser.error(f"--apply runs no measurement, so {flag} means nothing")
    elif not args.campaign or not args.label:
        parser.error("--campaign and --label are required to measure a round")

    target = resolve_target(args.hostname)
    wizard = Wizard(
        base_url=args.base_url or target.base_url, host_header=target.hostname
    )
    trail = Trail(args.trail)
    try:
        if args.apply is not None:
            trail.emit("target", host=target.ssh_target, hostname=target.hostname,
                       base_url=args.base_url or target.base_url, stage="apply")
            code = apply_candidate(wizard, args.apply, trail)
        else:
            code = run_round(args, target, wizard, trail)
    finally:
        trail.close()
    print(
        f"round finished: {EXIT_NAMES.get(code, str(code))} (rc {code})",
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
