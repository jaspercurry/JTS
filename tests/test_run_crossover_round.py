# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins ``scripts/run-crossover-round.py`` — above all, its apply gate.

Every assertion here runs the real script as a subprocess against a fake
``ssh`` on ``PATH`` and a real HTTP server on loopback, so no Pi, no
turntable, and no network are involved. The script resolves its own repo root
from its own location, so it is copied into a temp checkout beside a fake
``bank-crossover-round.sh`` and a ``.env.local`` — which is also what lets the
``PI_HOST`` precedence be checked without touching the operator's own file.

**The gate is the point.** A measurement run must POST the apply endpoint
never, and an ``--apply`` naming a fingerprint that is not the live candidate
must POST NOTHING AT ALL — not the apply, not even the CSRF mint that would
precede it. Both are asserted against a server that records every request it
receives, because "did not apply" is only true if nothing was sent.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from jasper.active_speaker.crossover_v2.position_cycle import read_position_cycle

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-crossover-round.py"

CSRF_PAGE = '<html><meta name="jts-csrf" content="tok-abcdefghijklmnopqrstuvwxyz012345"></html>'
FINGERPRINT = "cand-4f2a9b"

CANDIDATE = {
    "fingerprint": FINGERPRINT,
    "predicted_ripple_db": 1.8,
    "headroom_cost_db": 0.0,
    "alignment": {"polarity": "keep", "delay_status": "measured"},
}


# --------------------------------------------------------------------------- #
# the fake speaker
# --------------------------------------------------------------------------- #


class _Seen(NamedTuple):
    """What a server recorded, frozen at the moment of asking."""

    requests: tuple[tuple[str, str], ...]   # (method, path)
    posts: tuple[tuple[str, Any], ...]      # (path, body)
    hosts: tuple[str, ...]                  # the Host header each request sent


class _Wizard(ThreadingHTTPServer):
    """A correction wizard that records every request and scripts its phase.

    ``daemon_threads = True`` means ``server_close()`` returns WITHOUT joining
    handler threads, so a handler can still be appending to ``requests`` after
    a test believes the server is shut. That is the hazard; the two remedies
    are :meth:`join_handlers`, which waits for them, and :meth:`seen`, which
    hands out a frozen copy so a late handler cannot change a list mid-
    assertion.

    **Both remedies are only in force through** :func:`_serving`, which is why
    every server in this file is started that way and none hand-rolls
    ``serve_forever``/``shutdown``. Seven of them did until the round-2 gate
    found it: those paths skipped ``join_handlers`` entirely, so the docstring
    that claimed the protection was describing four servers out of eleven.

    Why it matters here rather than in general: two full-file runs during this
    PR showed a server holding a request its own subprocess could not have
    sent. Port reuse was tested and REFUTED, the mechanism is still
    unattributed, and the point of these two is that such a thing shows up as
    a failure instead of a silently wrong list.
    """

    daemon_threads = True

    def __init__(self, **behaviour: Any) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self._lock = threading.Lock()
        self._handlers: list[threading.Thread] = []
        self.requests: list[tuple[str, str]] = []          # (method, path)
        self.posts: list[tuple[str, Any]] = []             # (path, body)
        self.hosts: list[str] = []                         # the Host header sent
        #: When set, the open POST waits for this path to exist before it
        #: answers -- the sequencing the two-process walk double needs.
        self.open_gate: Path | None = behaviour.get("open_gate")
        self.open_status: int = behaviour.get("open_status", 200)
        self.apply_status: int = behaviour.get("apply_status", 200)
        self.apply_body: dict[str, Any] = behaviour.get(
            "apply_body", {"status": "applied", "expected_post_apply_offset_db": -0.2}
        )
        self.v2: dict[str, Any] = dict(
            behaviour.get("v2", {"phase": "review", "session_id": "before",
                                 "candidate": CANDIDATE})
        )
        #: What the block becomes once a stage has been opened and polled once
        #: — a NEW session id and a terminal phase, which is what the runner's
        #: completion rule is looking for.
        self.after_open: dict[str, Any] = dict(
            behaviour.get("after_open", {"phase": "review", "session_id": "after",
                                         "candidate": CANDIDATE})
        )
        self.opened = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def process_request_thread(self, request, client_address):
        with self._lock:
            self._handlers.append(threading.current_thread())
        super().process_request_thread(request, client_address)

    def record(self, method: str, path: str, host: str) -> None:
        with self._lock:
            self.requests.append((method, path))
            self.hosts.append(host)

    def record_post(self, path: str, body: Any) -> None:
        with self._lock:
            self.posts.append((path, body))

    def seen(self) -> _Seen:
        """A frozen snapshot of EVERY recorded list -- never the live ones.

        Returns all three together rather than per-list accessors: a caller
        that reached past this for one of them (``hosts`` was read live until
        the round-2 gate found it) is the case this exists to prevent.
        """
        with self._lock:
            return _Seen(tuple(self.requests), tuple(self.posts), tuple(self.hosts))

    def join_handlers(self) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for thread in handlers:
            thread.join(timeout=30)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "jts_csrf=tok-abcdefghijklmnopqrstuvwxyz012345; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        server: _Wizard = self.server
        server.record("GET", self.path, self.headers.get("Host") or "")
        if self.path == "/sound/speaker/crossover/":
            self._send(200, CSRF_PAGE.encode(), "text/html")
            return
        if self.path == "/sound/speaker/crossover/status":
            block = server.after_open if server.opened else server.v2
            self._send(200, json.dumps({"crossover_v2": block}).encode(),
                       "application/json")
            return
        self._send(404, b"{}", "application/json")

    def do_POST(self) -> None:
        server: _Wizard = self.server
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = raw.decode()
        server.record("POST", self.path, self.headers.get("Host") or "")
        server.record_post(self.path, body)
        if self.path.endswith("/v2/apply"):
            self._send(server.apply_status, json.dumps(server.apply_body).encode(),
                       "application/json")
            return
        if server.open_gate is not None:
            # Hold the open until the remote walk is ready to be hung up, so an
            # abort cannot race the remote's own start-up.
            deadline = time.monotonic() + 60
            while not server.open_gate.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        if server.open_status != 200:
            self._send(server.open_status,
                       json.dumps({"ok": False, "error": "synthetic refusal"}).encode(),
                       "application/json")
            return
        server.opened = True
        self._send(200, json.dumps({"capture": {"status": "awaiting_capture"}}).encode(),
                   "application/json")


@contextlib.contextmanager
def _serving(server: _Wizard):
    """Serve, then shut down and JOIN before the caller reads anything."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=30)
        server.join_handlers()
        server.server_close()


@pytest.fixture
def wizard():
    with _serving(_Wizard()) as server:
        yield server


# --------------------------------------------------------------------------- #
# the fake checkout
# --------------------------------------------------------------------------- #


def _executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def checkout(tmp_path: Path):
    """A repo the script can resolve itself from, with the Pi side faked."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    shutil.copy2(ROOT / "scripts" / "_lib.sh", scripts / "_lib.sh")
    shutil.copy2(ROOT / "scripts" / "_pi_target.py", scripts / "_pi_target.py")
    # All three keys, which is what scripts/onboard.sh actually writes -- and
    # what makes a SPLIT identity reachable: export only PI_HOST and the ssh
    # target is yours while the speaker's name is still this file's.
    (repo / ".env.local").write_text(
        "PI_HOST=checkout.invalid\nPI_USER=checkout-user\n"
        "JASPER_HOSTNAME=checkout.invalid\n",
        encoding="utf-8",
    )
    # The real script untars the speaker's evidence bundle into <dest>/bundle/,
    # and the pose index is DERIVED from the per-take records inside it — so a
    # double that only made the directory would make every happy-path round look
    # like a round whose walk was refused. ``FAKE_BANK_TAKES`` is how many
    # accepted lateral takes the bundle carries (0 = a bundle with none).
    _executable(scripts / "bank-crossover-round.sh", """\
        #!/usr/bin/env bash
        printf '%s\\t%s\\t%s\\t%s\\n' "$1" "${PI_HOST:-}" "${PI_USER:-}" "${SINCE:-}" \\
            >> "$FAKE_BANK_LOG"
        mkdir -p "$1"
        # The REAL banked layout: publish_json_artifact prefixes the writer's
        # relative path with the store's `evidence/v1/artifacts/` namespace, and
        # the bank untars the whole bundle. A double that skipped that prefix is
        # what let a wrong glob look right (see the position_cycle contract test).
        positions="$1/bundle/sess-1/evidence/v1/artifacts/crossover_v2/capture-1/positions"
        mkdir -p "$positions"
        # A counting `while`, never `for i in $(seq 1 "$takes")`: BSD seq counts
        # DOWN when last < first, so `seq 1 0` emits `1 0` and "zero takes"
        # quietly became two of them.
        takes="${FAKE_BANK_TAKES:-3}"
        i=1
        while [ "$i" -le "$takes" ]; do
            printf -v n '%02d' "$i"
            printf '%s' "{\\"schema_version\\":1,
                \\"kind\\":\\"jts_crossover_v2_position_evidence\\",
                \\"capture_session_id\\":\\"capture-1\\",\\"phase\\":\\"lateral\\",
                \\"pose_id\\":\\"lateral_$n\\",\\"index\\":$i,\\"attempt\\":1,
                \\"take_id\\":\\"lateral_${n}_a01\\",\\"role\\":\\"onax\\",
                \\"position_deg\\":0,\\"regime\\":\\"per_driver\\",
                \\"wav_sha256\\":\\"sha-$i\\"}" \\
                > "$positions/lateral_${n}_a01.json"
            i=$((i+1))
        done
        exit "${FAKE_BANK_EXIT:-0}"
        """)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # TWO processes, because ssh is two processes. The client is what the
    # runner can signal; the REMOTE is a separate program that never receives
    # the runner's SIGTERM. What reaches it is a HANGUP, delivered when the
    # client dies and the transport (a PTY, from `-tt`) closes — which is the
    # entire mechanism by which stopping a walk from the laptop stops the walk
    # on the speaker. A double that modelled both as one process would pass
    # while the real arm stood still at an unknown angle.
    _executable(fake_bin / "ssh", """\
        #!/usr/bin/env bash
        remote="${*: -1}"
        printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
        case "$remote" in
            *"jasper-angle-capture serve"*)
                # Is a PTY allocated? That is what decides whether the remote
                # hears anything at all when this client dies.
                pty=""
                for a in "$@"; do [ "$a" = "-tt" ] && pty=1; done
                "$FAKE_REMOTE_CMD" &
                REMOTE=$!
                if [ -n "$pty" ]; then
                    # sshd closes the PTY and the kernel hangs up the remote's
                    # process group. It is NEVER handed the TERM sent here.
                    trap 'kill -HUP "$REMOTE" 2>/dev/null; wait "$REMOTE" 2>/dev/null; exit 255' TERM INT
                else
                    # No PTY, no hangup: the remote is ORPHANED and keeps
                    # running against a session that has moved on.
                    trap 'exit 255' TERM INT
                fi
                wait "$REMOTE"
                exit $?
                ;;
            *jasper-angle-capture*) exit "${FAKE_STAGE_EXIT:-0}" ;;
        esac
        exit 0
        """)
    # The ordinary remote: ends on its own with the walk's exit code, and
    # publishes `serve`'s refusal sentence when it is stopping short -- that
    # line, not the rc, is where the stall name lives now.
    _executable(tmp_path / "remote-quick", """\
        #!/usr/bin/env bash
        sleep "${FAKE_WALK_SLEEP:-0}"
        if [ -n "${FAKE_WALK_REASON:-}" ]; then
            printf 'refused (%s): the turntable walk stopped\\n' "$FAKE_WALK_REASON" >&2
        fi
        exit "${FAKE_WALK_EXIT:-0}"
        """)
    return repo, fake_bin, tmp_path


def _run(checkout, wizard, args: list[str], **env_overrides: str):
    repo, fake_bin, tmp_path = checkout
    ssh_log = tmp_path / "ssh.log"
    bank_log = tmp_path / "bank.log"
    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        "PYTHONPATH": str(ROOT),
        "FAKE_SSH_LOG": str(ssh_log),
        "FAKE_BANK_LOG": str(bank_log),
        "FAKE_REMOTE_CMD": str(tmp_path / "remote-quick"),
    })
    env.update(env_overrides)
    proc = subprocess.run(
        # The interpreter running pytest, never a bare ``python3``: the script
        # imports the product for its own vocabulary, so it needs the same
        # environment the suite is running in.
        [sys.executable, str(repo / "scripts" / SCRIPT.name),
         "--base-url", wizard.url, "--poll-s", "0.05", "--stage-timeout-s", "10",
         *args],
        capture_output=True, text=True, timeout=180, env=env,
    )
    ssh_lines = ssh_log.read_text().splitlines() if ssh_log.exists() else []
    bank_lines = bank_log.read_text().splitlines() if bank_log.exists() else []
    return proc, ssh_lines, bank_lines


def _trail(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


MEASURE_ARGS = [
    "--tier", "remote", "--angles", "0,7,-7", "--regime", "per_driver",
    "--attest-rig-clear", "--expect-angles", "7,-7", "--complete-after", "3",
]


# --------------------------------------------------------------------------- #
# phase ordering
# --------------------------------------------------------------------------- #


def test_a_round_runs_its_phases_in_order(checkout, wizard, tmp_path):
    """Stage, launch, open, walk, await, bank, candidate — in that order.

    The walk is launched BEFORE the session opens because the arm harness's
    first poll is what checks a staged walk is still waiting; a runner that
    opened first would make that check unreachable.
    """
    trail = tmp_path / "trail.jsonl"
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    assert [row["step"] for row in _trail(trail)] == [
        "identity", "target", "stage", "walk_launched", "open", "walk",
        "await", "bank", "position_cycle", "candidate",
    ]
    assert all(row["ok"] for row in _trail(trail))
    assert len(bank_lines) == 1


def test_the_staged_walk_and_the_arm_walk_carry_what_the_operator_wrote(
    checkout, wizard, tmp_path
):
    """Angles, regime, expectations and attestation are FORWARDED, not derived."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    walk_cmd = next(
        line for line in ssh_lines if "jasper-angle-capture serve" in line
    )
    assert "stage --mover arm" in stage_cmd
    assert "--angles=0,7,-7" in stage_cmd and "--regime=per_driver" in stage_cmd
    assert "--attest-rig-clear" in walk_cmd
    assert "--expect-angles 7,-7" in walk_cmd
    assert "--complete-after 3" in walk_cmd
    # R-1's pair is absent unless asked for, so an ordinary round stages the
    # command it always did.
    assert "--polarity" not in stage_cmd and "--inverted-role" not in stage_cmd
    assert "--delayed-role" not in stage_cmd and "--delay-us" not in stage_cmd
    assert "--level-matched" not in stage_cmd


def test_a_leading_negative_angle_list_reaches_the_staging_seam_as_equals_form(
    checkout, wizard, tmp_path
):
    """Python 3.12's argparse (still what ships on the Pi) rejects a
    SPACE-form value that starts with ``-`` and is not itself a bare negative
    number -- "expected one argument" -- while 3.14's accepts it; a
    comma-joined angle list beginning with a negative angle is exactly that
    shape. ``stage_walk`` emits ``--angles=...``/``--regime=...`` (equals-form)
    so the remote command parses identically on either interpreter.
    """
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--tier", "remote", "--angles=-22,-7,0,7,22", "--regime", "per_driver",
         "--attest-rig-clear", "--expect-angles=-22,22", "--complete-after", "5"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--angles=-22,-7,0,7,22" in stage_cmd


def test_the_reverse_null_pair_is_forwarded_to_the_staging_seam(
    checkout, wizard, tmp_path
):
    """R-1 over SSH: without this the one command that drives a round could
    stage only normal-polarity walks, putting the reverse-null confirmation out
    of its reach."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS,
         "--polarity", "inverted", "--inverted-role", "tweeter"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--polarity=inverted" in stage_cmd
    assert "--inverted-role=tweeter" in stage_cmd


def test_the_confirmation_coordinate_is_forwarded_to_the_staging_seam(
    checkout, wizard, tmp_path
):
    """R-1's DISPOSE half over SSH. Without it the one command that drives a
    round could propose a delay and never confirm it acoustically — the
    compute-only shape compute-then-confirm exists to prevent."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS,
         "--polarity", "inverted", "--inverted-role", "tweeter",
         "--delayed-role", "tweeter", "--delay-us", "250.0"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--delayed-role=tweeter" in stage_cmd
    assert "--delay-us 250.0" in stage_cmd


def test_the_level_match_is_forwarded_to_the_staging_seam(
    checkout, wizard, tmp_path
):
    """Without it the one command that drives a round could stage a
    reverse-null walk on a cabinet whose branches are 10 dB apart and grade
    the shallow null that follows as a failure of the alignment."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS,
         "--polarity", "inverted", "--inverted-role", "tweeter",
         "--level-matched"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--level-matched" in stage_cmd


def test_without_an_attestation_no_walk_is_launched(checkout, wizard, tmp_path):
    """The attestation is the operator's. The runner never makes one up."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
    )

    assert proc.returncode == 0, proc.stderr
    assert not any("jasper-angle-capture serve" in line for line in ssh_lines)
    assert len(bank_lines) == 1  # the rest of the round still runs


def test_staging_angles_without_the_attestation_is_refused_up_front(
    checkout, wizard, tmp_path
):
    """A staged arm walk that nothing will serve is a dead configuration.

    Without ``--attest-rig-clear`` no walk is launched, so the session would
    open, hold at its first position for the gate's full ten minutes, and end
    as a misnamed idle-ceiling exit. Nothing downstream can rescue it, so it is
    refused before it costs the ten minutes — and before a walk is staged on
    the speaker for a round that will not run.
    """
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,7,-7"],
    )

    assert proc.returncode == 2
    assert "--attest-rig-clear" in proc.stderr
    requests = wizard.seen().requests
    assert ssh_lines == [] and bank_lines == [] and requests == ()


# --------------------------------------------------------------------------- #
# takes per position
# --------------------------------------------------------------------------- #


def _cycle(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "camp" / "r1" / "position_cycle.json").read_text())


def test_per_position_stages_each_angle_that_many_times_adjacently(
    checkout, wizard, tmp_path
):
    """Adjacent, so the arm settles and releases without travelling.

    Interleaving them (``0,7,0,7,0,7``) would walk the arm six times and measure
    the drift the takes exist to hold still.
    """
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
         "--angles", "0,7,-7", "--per-position", "3", "--regime", "per_driver",
         "--attest-rig-clear", "--expect-angles", "7,-7", "--complete-after", "9"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--angles=0,0,0,7,7,7,-7,-7,-7" in stage_cmd


def test_the_walk_gets_the_expectations_the_operator_wrote_not_the_expansion(
    checkout, wizard, tmp_path
):
    """``--expect-angles`` is a set the walk must SERVE, so repeats add nothing
    to it — ``_final_code`` checks membership in ``_served_angles``."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
         "--angles", "0,7,-7", "--per-position", "3",
         "--attest-rig-clear", "--expect-angles", "7,-7", "--complete-after", "9"],
    )

    assert proc.returncode == 0, proc.stderr
    walk_cmd = next(
        line for line in ssh_lines if "jasper-angle-capture serve" in line
    )
    assert "--expect-angles 7,-7" in walk_cmd
    assert "--complete-after 9" in walk_cmd


def test_the_index_is_derived_from_the_bundle_the_bank_pulled(
    checkout, wizard, tmp_path
):
    """Speaker-written facts only — the runner's staged angles never enter it.

    The pose IS banked (``lateral_pose_record`` -> ``positions/{take_id}.json``
    inside the evidence bundle); the round-grading views read the cloud block
    and never see it. The index projects those records into one sorted file at
    the round root — the same records the evidence packet's ``lateral_poses``
    block reads, through the same accept rule.
    """
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
         "--angles", "0,7", "--per-position", "3", "--trail", str(trail),
         "--attest-rig-clear", "--complete-after", "6"],
        FAKE_BANK_TAKES="6",
    )

    assert proc.returncode == 0, proc.stderr
    document = _cycle(tmp_path)
    assert [t["take_id"] for t in document["takes"]] == [
        f"lateral_0{i}_a01" for i in range(1, 7)
    ]
    assert document["sources"] == [
        "bundle/sess-1/evidence/v1/artifacts/crossover_v2/capture-1/positions"
    ]
    # Nothing the runner INTENDED is in the document — no angles, no
    # per_position, no staged list.
    assert set(document) == {"kind", "schema_version", "derived_at", "sources",
                             "takes"}
    # It reads back through its own strict reader, not just as JSON.
    assert read_position_cycle(
        tmp_path / "camp" / "r1" / "position_cycle.json") == document
    # The staged count is REPORTED beside the derived one, never folded in.
    row = next(r for r in _trail(trail) if r["step"] == "position_cycle")
    assert row["staged"] == 6 and row["takes"] == 6


def test_a_shortfall_between_staged_and_derived_is_visible_not_filled_in(
    checkout, wizard, tmp_path
):
    """Six stops staged, four takes accepted: the index carries the four the
    speaker recorded, and the trail names both numbers."""
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
         "--angles", "0,7", "--per-position", "3", "--trail", str(trail),
         "--attest-rig-clear", "--complete-after", "6"],
        FAKE_BANK_TAKES="4",
    )

    assert proc.returncode == 0, proc.stderr
    assert len(_cycle(tmp_path)["takes"]) == 4
    row = next(r for r in _trail(trail) if r["step"] == "position_cycle")
    assert row["staged"] == 6 and row["takes"] == 4


def test_an_ordinary_staged_round_indexes_its_poses_too(
    checkout, wizard, tmp_path
):
    """Not a cycling feature: no view surfaces a lateral bearing for ANY staged
    round, so every staged round gets the index."""
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    assert len(_cycle(tmp_path)["takes"]) == 3


def test_a_bundle_with_no_lateral_takes_is_named_never_filled_in_from_intent(
    checkout, wizard, tmp_path
):
    """The walk was refused at take time, or its poses were never accepted.

    The runner had every staged angle in hand and still writes nothing: a
    document assembled from them would be the intent-shaped record this design
    exists to avoid, and it would look exactly like evidence.
    """
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
        FAKE_BANK_TAKES="0",
    )

    assert proc.returncode == 0, proc.stderr  # the round still measured
    assert not (tmp_path / "camp" / "r1" / "position_cycle.json").exists()
    row = next(r for r in _trail(trail) if r["step"] == "position_cycle")
    assert row["ok"] is False and row["staged"] == 3
    assert "no lateral take records" in row["detail"]


def test_a_round_that_staged_no_walk_writes_no_index(checkout, wizard, tmp_path):
    """Nothing was staged, so there is no walk to index."""
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
    )

    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "camp" / "r1" / "position_cycle.json").exists()


def test_a_refused_bank_writes_no_index(checkout, wizard, tmp_path):
    """The bank's rc is the round's verdict, and its bundle is the index's
    input — indexing on top of a refused bank would read a tree nobody trusts."""
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
        FAKE_BANK_EXIT="2",
    )

    assert proc.returncode == 9
    assert not (tmp_path / "camp" / "r1" / "position_cycle.json").exists()


def test_takes_without_a_staged_walk_are_refused_up_front(
    checkout, wizard, tmp_path
):
    """The takes ARE stops in a staged walk. Without ``--angles`` the round
    would run its ordinary shape while the operator believed it was cycling —
    a night's evidence silently answering a different question."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--per-position", "3"],
    )

    assert proc.returncode == 2
    assert "--angles" in proc.stderr
    assert ssh_lines == [] and bank_lines == [] and wizard.seen().requests == ()


@pytest.mark.parametrize("per_position", ["0", "-1"])
def test_fewer_than_one_take_per_position_is_refused(
    checkout, wizard, tmp_path, per_position
):
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,7", "--attest-rig-clear",
         "--per-position", per_position],
    )

    assert proc.returncode == 2
    assert "at least 1" in proc.stderr
    assert ssh_lines == [] and bank_lines == [] and wizard.seen().requests == ()


@pytest.mark.parametrize("regime", ["per_driver", "summed"])
def test_takes_are_taken_for_every_regime_that_stages_ONE_stop_per_angle(
    checkout, wizard, tmp_path, regime
):
    """``_REGIME_STOPS`` maps every member of ``REGIMES`` to a 1-tuple of
    itself, so ``summed`` composes exactly one stop per angle just as
    ``per_driver`` does — the token count is its exact stop count too, and the
    floor is sound. Refusing it (as an earlier version did, on a stated
    mechanism that was simply false) blocked a reversible experiment for no
    reason at all."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,7", "--per-position", "3", "--attest-rig-clear",
         "--regime", regime, "--complete-after", "6"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--angles=0,0,0,7,7,7" in stage_cmd and f"--regime={regime}" in stage_cmd


def test_takes_are_refused_for_a_regime_that_stages_more_than_one_stop(
    checkout, wizard, tmp_path
):
    """``jasper-angle-capture`` composes stops as ``angle x
    _REGIME_STOPS[regime]``, so ``both`` — and only ``both`` — is TWO stops per
    token, where the ``--complete-after`` floor, which counts tokens, would be
    exactly half the real stop count. Refused rather than multiplied: a
    multiplier here would be this file's second opinion about another tool's
    composition rule.

    What is declined is the FLAG, never the experiment — measurement-loop
    doctrine §5. The refusal has to say so and name the way through, because a
    message that only said "no" would be the nanny gate that doctrine forbids:
    the repeat was always the seam's own shape, so a hand-written staged list
    takes N captures per pose at any regime.
    """
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,7", "--per-position", "3", "--attest-rig-clear",
         "--regime", "both", "--complete-after", "12"],
    )

    assert proc.returncode == 2
    assert "--regime both" in proc.stderr
    # The stated mechanism has to be the REAL one — the composed count — not a
    # claim about which single regime is blessed. That sentence was false once.
    assert "2 stops per angle" in proc.stderr
    assert "0,0,0,7,7,7" in proc.stderr  # the way through, not just the "no"
    assert ssh_lines == [] and bank_lines == [] and wizard.seen().requests == ()


def test_a_hand_staged_repeat_list_is_taken_at_any_regime(
    checkout, wizard, tmp_path
):
    """The escape hatch the refusal above points at, exercised.

    ``--regime both`` with the repeats written out reaches the seam unchanged —
    this runner counts nothing on the operator's behalf, so nothing of its
    arithmetic is in the way.
    """
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,0,0,7,7,7", "--attest-rig-clear",
         "--regime", "both", "--complete-after", "12"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--angles=0,0,0,7,7,7" in stage_cmd and "--regime=both" in stage_cmd


def test_takes_are_refused_on_the_verify_stage_which_serves_its_own_poses(
    checkout, wizard, tmp_path
):
    """``_take_staged_angle_walk`` is reached only from the MEASURING open;
    stage 2's positions and count come from ``plan_shape.verify_capture_target``.
    A walk staged for it is taken by nobody, so the takes never happen."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "v1",
         "--stage", "verify", "--angles", "0,7", "--per-position", "3",
         "--attest-rig-clear", "--complete-after", "6"],
    )

    assert proc.returncode == 2
    assert "--stage verify" in proc.stderr and "tier's own poses" in proc.stderr
    assert ssh_lines == [] and bank_lines == [] and wizard.seen().requests == ()


def test_a_complete_after_below_the_staged_stop_count_is_refused_with_the_remedy(
    checkout, wizard, tmp_path
):
    """``--complete-after`` counts RELEASES, so a walk told to complete on
    fewer of them than it has stops posts its all-spots-measured signal partway
    through and exits ``ok`` — a round that measured a walk nobody asked for,
    with no failing code to say so. The refusal carries the number to pass."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,7,-7", "--per-position", "3", "--attest-rig-clear",
         "--complete-after", "3"],
    )

    assert proc.returncode == 2
    assert "9 stops" in proc.stderr and "3 per position" in proc.stderr
    assert "pass 9 or higher, or omit it" in proc.stderr
    assert ssh_lines == [] and bank_lines == [] and wizard.seen().requests == ()


def test_the_stop_count_floor_does_not_refuse_an_ordinary_round(
    checkout, wizard, tmp_path
):
    """One release per stop is the shipped shape; the floor must not move it."""
    proc, _, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    assert len(bank_lines) == 1


def test_an_empty_angle_field_is_DROPPED_exactly_as_the_seam_drops_it(
    checkout, wizard, tmp_path
):
    """``jasper.cli.angle_capture._parse_angles`` keeps only ``field.strip()``
    fields — a trailing comma is tolerated there by design — so refusing one
    here would make the runner a second, stricter reader of the same field."""
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--angles", "0,,7,", "--attest-rig-clear", "--per-position", "2",
         "--complete-after", "4"],
    )

    assert proc.returncode == 0, proc.stderr
    stage_cmd = next(line for line in ssh_lines if "jasper-angle-capture" in line)
    assert "--angles=0,0,7,7" in stage_cmd


@pytest.mark.parametrize("per_position", ["3", "1", "0"])
def test_apply_refuses_takes_per_position_at_any_value(
    checkout, wizard, per_position
):
    """WAS IT PASSED, not IS IT TRUTHY. ``--per-position 0`` is a value the
    operator typed, and a truthiness test would let it reach a path that
    measures nothing and silently ignore it — ``1`` likewise, since it happens
    to equal the default."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard, ["--apply", FINGERPRINT, "--per-position", per_position],
    )

    assert proc.returncode == 2
    assert "--per-position" in proc.stderr
    assert ssh_lines == [] and bank_lines == []
    assert wizard.seen().requests == ()


def test_a_verify_stage_opens_the_post_apply_check(checkout, wizard, tmp_path):
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "v1", "--stage", "verify"],
    )

    assert proc.returncode == 0, proc.stderr
    posts = wizard.seen().posts
    assert posts == (("/sound/speaker/crossover/v2/verify", {"stage": "post_apply"}),)


def test_an_alignment_prescription_is_posted_verbatim(checkout, wizard, tmp_path):
    """The gate that judges a prescription is the open's own, not this one's."""
    document = {"delay_us": -120.0, "basis_delay_us": -100.0,
                "basis_artifacts": ["round-7"], "polarity": "invert"}
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document))

    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--alignment-prescription", str(path)],
    )

    assert proc.returncode == 0, proc.stderr
    posts = wizard.seen().posts
    _, body = posts[0]
    assert body["alignment_prescription"] == document
    assert body["tier"] == "remote"  # always explicit; an absent tier inherits


def test_an_unreadable_prescription_is_refused_before_anything_is_staged(
    checkout, wizard, tmp_path
):
    """An argument the operator wrote wrongly ends as argparse's refusal."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--alignment-prescription", str(tmp_path / "missing.json"), *MEASURE_ARGS],
    )

    assert proc.returncode == 2  # argparse's own usage exit
    assert "Traceback" not in proc.stderr
    posts = wizard.seen().posts
    assert ssh_lines == [] and bank_lines == [] and posts == ()


def test_a_topology_prescription_is_posted_verbatim(checkout, wizard, tmp_path):
    """The gate that judges a prescription is the open's own, not this one's."""
    document = {"fc_hz": 1800.0, "order": 4, "basis_artifacts": ["round-7"]}
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document))

    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--topology-prescription", str(path)],
    )

    assert proc.returncode == 0, proc.stderr
    posts = wizard.seen().posts
    _, body = posts[0]
    assert body["topology_prescription"] == document
    assert body["tier"] == "remote"  # always explicit; an absent tier inherits


def test_an_unreadable_topology_prescription_is_refused_before_anything_is_staged(
    checkout, wizard, tmp_path
):
    """An argument the operator wrote wrongly ends as argparse's refusal."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--topology-prescription", str(tmp_path / "missing.json"), *MEASURE_ARGS],
    )

    assert proc.returncode == 2  # argparse's own usage exit
    assert "Traceback" not in proc.stderr
    posts = wizard.seen().posts
    assert ssh_lines == [] and bank_lines == [] and posts == ()


def test_alignment_and_topology_prescriptions_compose(checkout, wizard, tmp_path):
    """A round may pin alignment AND topology at once; both land verbatim."""
    alignment_document = {"delay_us": -120.0, "basis_delay_us": -100.0,
                          "basis_artifacts": ["round-7"], "polarity": "invert"}
    topology_document = {"fc_hz": 1800.0, "order": 4, "basis_artifacts": ["round-7"]}
    alignment_path = tmp_path / "alignment.json"
    topology_path = tmp_path / "topology.json"
    alignment_path.write_text(json.dumps(alignment_document))
    topology_path.write_text(json.dumps(topology_document))

    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--alignment-prescription", str(alignment_path),
         "--topology-prescription", str(topology_path)],
    )

    assert proc.returncode == 0, proc.stderr
    posts = wizard.seen().posts
    _, body = posts[0]
    assert body["alignment_prescription"] == alignment_document
    assert body["topology_prescription"] == topology_document


# --------------------------------------------------------------------------- #
# THE APPLY GATE
# --------------------------------------------------------------------------- #


def test_a_measurement_round_never_posts_the_apply_endpoint(
    checkout, wizard, tmp_path
):
    """The whole reason this script exists. A round measures; it does not apply."""
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    requests = wizard.seen().requests
    assert not any(path.endswith("/v2/apply") for _, path in requests)
    # …and the candidate it declined to apply is on stdout, by fingerprint.
    assert FINGERPRINT in proc.stdout
    assert "predicted_ripple_db = 1.8" in proc.stdout


def test_apply_refuses_a_fingerprint_that_is_not_live_and_sends_nothing(
    checkout, wizard, tmp_path
):
    """A named fingerprint that is not the live candidate ends on the laptop.

    Nothing is POSTed — not the apply, and not the CSRF mint that precedes
    one. The endpoint's own freshness guard would also refuse this, which is
    exactly why the assertion is about what was SENT rather than about the
    exit code alone.
    """
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard, ["--apply", "cand-somethingelse", "--trail", str(trail)]
    )

    assert proc.returncode == 11  # EXIT_FINGERPRINT, literal on purpose
    requests = wizard.seen().requests
    assert not any(method == "POST" for method, _ in requests)
    assert not any(path == "/sound/speaker/crossover/" for _, path in requests)
    row = _trail(trail)[-1]
    assert row["step"] == "apply" and row["ok"] is False
    assert row["named"] == "cand-somethingelse" and row["live"] == FINGERPRINT


def test_an_empty_apply_fingerprint_is_refused_before_it_is_compared(
    checkout, wizard
):
    """``--apply ''`` must never reach the comparison, let alone the wire.

    An absent live candidate also reads as ``""``, so an empty argument would
    compare EQUAL to it, sail through the gate, and POST — the one thing this
    tool promises never to do on a fingerprint nobody named. argparse's own
    refusal is the answer; the assertion that matters is that nothing was sent.
    """
    proc, ssh_lines, bank_lines = _run(checkout, wizard, ["--apply", ""])

    assert proc.returncode == 2  # argparse usage exit, not the gate's rc 11
    requests = wizard.seen().requests
    assert requests == ()
    assert ssh_lines == [] and bank_lines == []
    assert "Traceback" not in proc.stderr


def test_apply_refuses_when_no_candidate_is_published(checkout, tmp_path):
    server = _Wizard(v2={"phase": "measure", "session_id": "s0", "candidate": None})
    with _serving(server):
        proc, _, _ = _run(checkout, server, ["--apply", FINGERPRINT])

    assert proc.returncode == 11
    requests = server.seen().requests
    assert not any(method == "POST" for method, _ in requests)


def test_apply_posts_the_named_fingerprint_when_it_is_the_live_one(
    checkout, wizard, tmp_path
):
    proc, ssh_lines, bank_lines = _run(checkout, wizard, ["--apply", FINGERPRINT])

    assert proc.returncode == 0, proc.stderr
    posts = wizard.seen().posts
    assert posts == (
        ("/sound/speaker/crossover/v2/apply",
         {"expected_candidate_fingerprint": FINGERPRINT}),
    )
    # An apply measures nothing and banks nothing.
    assert ssh_lines == [] and bank_lines == []


def test_a_blocked_apply_is_a_failure_even_though_it_answers_409(
    checkout, tmp_path
):
    """``blocked`` is the ONE status that moves the code off 200.

    Which is why the code alone cannot decide: ``apply_failed`` is always 200,
    and a refusal is a 400 carrying no ``status`` at all. Only 200 AND
    ``applied`` is right on every row.
    """
    server = _Wizard(apply_status=409,
                     apply_body={"status": "blocked",
                                 "issue": {"id": "stage_2_cannot_open"}})
    with _serving(server):
        proc, _, _ = _run(checkout, server, ["--apply", FINGERPRINT])

    assert proc.returncode == 10  # EXIT_APPLY


def test_an_apply_that_answers_200_but_did_not_apply_is_a_failure(checkout):
    server = _Wizard(apply_status=200, apply_body={"status": "apply_failed"})
    with _serving(server):
        proc, _, _ = _run(checkout, server, ["--apply", FINGERPRINT])

    assert proc.returncode == 10


# --------------------------------------------------------------------------- #
# propagation — nobody else's verdict is re-mapped
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bank_exit,expected_rc,summarised", [
    (0, 0, True),    # clean
    (1, 9, False),   # bash's own failure — no longer overloaded as "nothing to grade"
    (3, 9, False),   # the bank could not pull the round's own identity
    (4, 9, False),   # the destination was already used
])
def test_the_banks_own_exit_contract_decides_the_round(
    checkout, wizard, tmp_path, bank_exit, expected_rc, summarised
):
    trail = tmp_path / "trail.jsonl"
    proc, _, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--trail", str(trail)],
        FAKE_BANK_EXIT=str(bank_exit),
    )

    assert proc.returncode == expected_rc, proc.stderr
    assert len(bank_lines) == 1
    row = next(r for r in _trail(trail) if r["step"] == "bank")
    assert row["bank_exit"] == bank_exit
    # The trail's own verdict, asserted separately from the process rc because
    # they are written by two different lines in `bank()` — the `ok=` flag and
    # the abort gate. Only pinning the rc leaves the flag free to disagree with
    # it, which is exactly how a stale `in (0, 1)` survives a green suite.
    assert row["ok"] is (expected_rc == 0)
    # A refused bank stops the round before the candidate is summarised at
    # all; the trail is the observable now that nothing is written beside it.
    assert any(r["step"] == "candidate" for r in _trail(trail)) is summarised


def test_the_rounds_flatness_verdicts_reach_the_operator_and_the_trail(
    checkout, tmp_path
):
    """The round prints WHAT IT MEASURED, not only what it would apply.

    The candidate block answers "what would go on the speaker"; a driver
    deciding whether to run another round is asking the other question, and
    before this it had to read ``state.json`` by hand to answer it. Asserted
    on the trail's structured row rather than on the printed sentence: the
    prose is for the operator, the row is the contract.
    """
    spec = {
        "passed": False, "max_db": -4.85, "max_hz": 11480.0,
        "graded_band_hz": [357.14, 20000.0],
        "bands": [
            {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "graded_lo_hz": 357.14,
             "graded_hi_hz": 2000.0, "tolerance_db": 1.5, "passed": True,
             "max_deviation_db": 1.02, "max_deviation_hz": 412.0},
            {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "graded_lo_hz": 8000.0,
             "graded_hi_hz": 20000.0, "tolerance_db": 2.5, "passed": False,
             "max_deviation_db": -4.85, "max_deviation_hz": 11480.0},
        ],
        "tilt": {"step_db": 2.37, "high_band_hz": [250.0, 2000.0],
                 "low_band_hz": [8000.0, 16000.0], "n_bands": 2,
                 "evaluable": True},
    }
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "round_receipt": {"adoption": "keep_for_iteration", "spec": spec},
    })
    trail = tmp_path / "trail.jsonl"
    with _serving(server):
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail)],
        )

    assert proc.returncode == 0, proc.stderr
    row = next(r for r in _trail(trail) if r["step"] == "flatness")
    assert row["passed"] is False
    assert row["worst_db"] == -4.85
    assert row["worst_hz"] == 11480.0
    assert row["tilt_db"] == 2.37
    # The graded span, because the top band no longer ends where its name says.
    assert row["graded_band_hz"] == [357.14, 20000.0]
    # Every band reaches the operator, with the frequency of its worst bin —
    # the numbers a next round is aimed with.
    assert "8000.0-20000.0 Hz" in proc.stdout
    assert "11480.0 Hz" in proc.stdout


def test_the_controllability_ledger_reaches_the_operator_and_the_trail(
    checkout, tmp_path
):
    """Where commands realize as commanded, across the banked rounds.

    The flatness block answers "how flat is it now" for ONE round; this
    answers "in which bands do our commands land where we aim them", which is
    what a driver placing the next experiment is asking. Asserted on the
    trail's structured row — the prose is for the operator, the row is the
    contract.
    """
    ledger = {
        "n_rounds": 2,
        "rounds": [
            {
                "bands": {
                    "low": {"band_hz": [250.0, 2000.0], "n_bins": 12,
                             "ratio": 0.61, "graded": True},
                    "high": {"band_hz": [8000.0, 16000.0], "n_bins": 0,
                             "ratio": None, "graded": False},
                },
                "spec": "flat-v1",
                "spec_misses": ["high"],
            },
            {
                "bands": {
                    "low": {"band_hz": [250.0, 2000.0], "n_bins": 14,
                             "ratio": 0.58, "graded": True},
                    "high": {"band_hz": [8000.0, 16000.0], "n_bins": 3,
                             "ratio": 0.22, "graded": True},
                },
                "spec": "flat-v1",
                "spec_misses": [],
            },
        ],
    }
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "controllability": ledger,
    })
    trail = tmp_path / "trail.jsonl"
    with _serving(server):
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail)],
        )

    assert proc.returncode == 0, proc.stderr
    row = next(r for r in _trail(trail) if r["step"] == "controllability")
    assert row["n_rounds"] == 2
    # The whole per-round table, because the ledger's claim IS the shape
    # across bands and rounds.
    assert row["rounds"] == ledger["rounds"]
    # A band with no bins yet reaches the operator as ratio=None, never as a
    # zero — round 1's high band hasn't landed a fit.
    assert "high  8000.0-16000.0 Hz  ratio=None  n_bins=0  graded=False" in proc.stdout
    # The second round did land in that band, with the spec it was graded
    # against and how many bands it missed.
    assert "round 2  spec=flat-v1  misses=0" in proc.stdout
    assert "high  8000.0-16000.0 Hz  ratio=0.22  n_bins=3  graded=True" in proc.stdout


def test_a_round_with_no_ledger_prints_no_controllability_block(
    checkout, tmp_path
):
    """No history is not a history of zero: the block is absent, not empty."""
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "controllability": None,
    })
    trail = tmp_path / "trail.jsonl"
    with _serving(server):
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail)],
        )

    assert proc.returncode == 0, proc.stderr
    assert not any(r["step"] == "controllability" for r in _trail(trail))


def test_a_round_with_no_graded_spec_prints_no_flatness_block(checkout, tmp_path):
    """No report is not a report of zero: the block is absent, not empty."""
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "round_receipt": {"adoption": "keep", "spec": None},
    })
    trail = tmp_path / "trail.jsonl"
    with _serving(server):
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail)],
        )

    assert proc.returncode == 0, proc.stderr
    assert not any(r["step"] == "flatness" for r in _trail(trail))


def test_a_failing_arm_walk_stops_the_round_and_keeps_its_own_name(
    checkout, wizard, tmp_path
):
    """The rc AND the stall `serve` named ride through, and nothing banks."""
    trail = tmp_path / "trail.jsonl"
    # Exports a speaker that is NOT the one .env.local names, so the printed
    # bank command can only carry `caller.invalid` if the prefix is really
    # there. Drop the prefix and the pasted line re-resolves to
    # `checkout.invalid` — a different Pi — which is the whole hazard.
    proc, _, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--trail", str(trail),
         *MEASURE_ARGS],
        FAKE_WALK_EXIT="1", FAKE_WALK_REASON="stuck",
        PI_HOST="caller.invalid", PI_USER="caller-user",
    )

    assert proc.returncode == 5  # EXIT_WALK — this runner's own phase code
    row = next(r for r in _trail(trail) if r["step"] == "walk")
    assert row["arm_walk_exit"] == 1 and row["arm_walk_exit_name"] == "stuck"
    assert bank_lines == []
    # The evidence is still on the Pi, and the operator is told how to keep it
    # — WITH this round's own speaker on the front. Without that prefix the
    # pasted command re-resolves through .env.local and can bank a different
    # Pi, on the one path where a human is asked to run it by hand.
    assert "bank-crossover-round.sh" in proc.stderr
    assert "PI_HOST=caller.invalid PI_USER=caller-user SINCE=" in proc.stderr
    # Absolute, and the checkout the runner actually resolved itself from --
    # the line prints wherever the operator happened to be standing.
    repo, _, _ = checkout
    assert str(repo / "scripts" / "bank-crossover-round.sh") in proc.stderr


def test_an_ssh_transport_failure_is_not_reported_as_the_arms_fault(
    checkout, wizard, tmp_path
):
    """255 is ssh's code, and the harness cannot produce it.

    Its exit codes are the shared 0/1/3 plus 128+signum, so within 255 is
    unambiguously the link failing. Calling that ``walk_failed`` on the line an
    operator reads would send them to the rig to look at an arm that never
    misbehaved.
    """
    trail = tmp_path / "trail.jsonl"
    proc, _, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
        FAKE_WALK_EXIT="255",
    )

    assert proc.returncode == 12  # EXIT_SSH_TRANSPORT, not EXIT_WALK's 5
    # The stderr summary and the trail row agree, which is the whole point.
    assert "ssh_transport_failed" in proc.stderr
    row = next(r for r in _trail(trail) if r["step"] == "walk")
    assert row["arm_walk_exit"] == 255
    assert row["arm_walk_exit_name"] == "ssh_transport_failed"
    assert bank_lines == []


# The chatter ahead of this run's own output. The non-ASCII row is the point of
# the parametrization: ``since_byte`` is an ``st_size``, so slicing the decoded
# text by it overshoots by one position per multi-byte character upstream.
@pytest.mark.parametrize("earlier", [
    "some earlier chatter\n",
    "moved to +22° — settling\n",
])
def test_the_walk_exit_name_is_read_from_the_record_not_from_the_code(
    tmp_path, earlier
):
    """``serve`` exits 0/1/3, so the STALL has to come off its refusal line."""
    runner = _runner()
    log = tmp_path / "walk.log"

    assert runner._walk_exit_name(255, log) == "ssh_transport_failed"
    # utf-8 explicitly, because the offset below is `len(...encode())` and the
    # fixed reader decodes as utf-8: letting the locale pick would make this
    # case pass or break on the host's encoding rather than on the code.
    log.write_text(
        earlier + "refused (walk_not_staged): nothing was staged\n"
        "refused (stuck): the turntable walk stopped at loop code 6\n",
        encoding="utf-8",
    )
    # The LAST refusal, and only when the walk actually stopped short.
    assert runner._walk_exit_name(1, log) == "stuck"
    assert runner._walk_exit_name(0, log) == "ok"
    # Parked by a signal, or killed: no record to read, so the rc is all there
    # is -- guessed at by nobody.
    assert runner._walk_exit_name(143, tmp_path / "absent.log") == "143"
    # ...and a re-run under the same label never inherits the previous run's
    # reason: only bytes written after the launch are this walk's.
    assert runner._walk_exit_name(143, log, log.stat().st_size) == "143"

    # ...and the offset is BYTES, which is what `st_size` hands over. Sliced as
    # CHARACTERS it runs PAST the boundary by one position per multi-byte
    # character upstream — straight through this run's own refusal line, whose
    # reason then degrades to the bare rc.
    rerun = tmp_path / "rerun.log"
    rerun.write_bytes(earlier.encode() + b"refused (stuck): stopped short\n")
    assert runner._walk_exit_name(1, rerun, len(earlier.encode())) == "stuck"


def test_a_refused_stage_stops_before_anything_opens(checkout, wizard, tmp_path):
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
        FAKE_STAGE_EXIT="2",
    )

    assert proc.returncode == 3  # EXIT_STAGE
    assert not any("jasper-angle-capture serve" in line for line in ssh_lines)
    posts = wizard.seen().posts
    assert posts == () and bank_lines == []


def test_a_session_that_will_not_open_stops_the_round(checkout, tmp_path):
    server = _Wizard(open_status=400)
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
        )

    assert proc.returncode == 4  # EXIT_OPEN
    assert bank_lines == []


def test_an_aborted_round_hangs_up_its_walk_and_the_walk_PARKS(checkout, tmp_path):
    """The whole chain, end to end, with nothing modelled away.

    The remote here is the REAL signal handling — a process that installs
    ``arm_walk.install_park_on_signals``' own handlers and writes a marker from the
    ``finally`` those handlers unwind into. So this asserts what an operator
    cares about: after an aborted round, the arm went home.

    Each link is separately falsifiable. Take SIGHUP out of
    ``arm_walk.PARK_ON_SIGNALS`` and the marker is absent (the remote dies on
    Python's default disposition). Drop ``-tt`` from the launch and no signal
    reaches the remote at all. Model client and remote as ONE process — as the
    first version of this double did — and the test passes while a real arm
    stands still at an unknown angle.
    """
    remote_ready = tmp_path / "remote-ready"
    parked = tmp_path / "remote-parked"
    # The remote is a python program with its own shebang -- no shell wrapper,
    # so nothing has to survive two levels of quoting.
    _executable(tmp_path / "remote-walk", f"""#!{sys.executable}
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from jasper.active_speaker.arm_walk import install_park_on_signals

install_park_on_signals()
try:
    open({str(remote_ready)!r}, "w").write("ready")
    while True:
        time.sleep(0.02)
finally:
    open({str(parked)!r}, "w").write("parked")
""")

    # The open is held until the remote has its handlers installed, so an
    # abort cannot beat the remote's own start-up.
    server = _Wizard(open_status=400, open_gate=remote_ready)
    started = time.monotonic()
    with _serving(server):
        trail = tmp_path / "trail.jsonl"
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail), *MEASURE_ARGS],
            FAKE_REMOTE_CMD=str(tmp_path / "remote-walk"),
        )

    assert proc.returncode == 4, proc.stderr  # EXIT_OPEN
    assert remote_ready.exists(), "the remote never started"
    # THE assertion: the arm went home, on the remote, in its own unwind.
    assert parked.exists(), (
        "the walk was not hung up, or was hung up and died without parking"
    )
    # Hung up, not waited out — the remote would have run forever.
    assert time.monotonic() - started < 120
    assert [r["step"] for r in _trail(trail)][-1] == "walk_stopped"
    assert bank_lines == []


def test_the_runner_never_claims_the_park_it_cannot_see(checkout, tmp_path):
    """The trail reports the transport dropping, not an arm parking.

    The park happens on the speaker after the local client is gone, so a
    ``walk_stopped`` row asserting the arm is home would be a reassurance
    nothing here can observe — which is exactly what the first version of this
    file printed while the arm stood still.
    """
    server = _Wizard(open_status=400)
    with _serving(server):
        trail = tmp_path / "trail.jsonl"
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail), *MEASURE_ARGS],
            FAKE_WALK_SLEEP="120",
        )

    assert proc.returncode == 4
    row = next(r for r in _trail(trail) if r["step"] == "walk_stopped")
    detail = row["detail"]
    assert "hangs up" in detail and "walk log" in detail
    assert "the arm parks on its way out" not in detail


def test_a_session_failure_is_named_rather_than_waited_out(checkout, tmp_path):
    server = _Wizard(after_open={"phase": "measure", "session_id": "after",
                                 "failure": {"code": "program_unplayable"}})
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
        )

    assert proc.returncode == 8  # EXIT_SESSION_FAILED
    assert bank_lines == []


def test_a_round_that_graded_before_it_refused_is_still_banked(checkout, tmp_path):
    """#3486, witnessed live: the campaign's best round banked nothing.

    A Full round's adoption tail runs INSIDE the group-closing capture's own
    call, so a ``restore`` row returns as that capture's verdict and lands in
    the state's ``failure`` block — after the round has graded and banked its
    receipt. ``await_stage`` read the failure before it read the completion, so
    the one round in the campaign that first passed every spec band exited
    ``session_failed`` with the bank skipped, and survived only because the
    operator ran the printed hand command.

    doctrine §3: *every round, kept or restored or refused, banks its
    measurement into the series state* — and the rounds that end in a restore
    are exactly the ones that were losing their evidence by default.

    **The rc is unchanged.** The round really did refuse, and a caller chaining
    rounds must still see that; what moves is only whether the evidence is
    pulled before the runner says so.
    """
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "failure": {"code": "correction_level_shortfall"},
        "round_receipt": {"round_id": "after", "adoption": "restore",
                          "row": "row5_trusted_safe_regressed"},
    })
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
        )

    assert proc.returncode == 8  # EXIT_SESSION_FAILED — the round did refuse
    assert len(bank_lines) == 1  # …and its evidence came off the Pi anyway


def test_a_bank_that_fails_on_a_refused_round_keeps_the_rounds_own_verdict(
    checkout, tmp_path
):
    """The other half of #3486's ordering: the pull can itself fail.

    Exactly the rounds the fix exists for — graded, then refused — reach the
    bank with a verdict of their own, and a bank that then fails was replacing
    it with ``bank_refused``: the round's ``session_failed`` disappeared from
    the rc a chaining caller reads. ``EXIT_BANK`` says "the ROUND was fine and
    only the pull was not", which is a different sentence and not this one.

    And the evidence is still on the Pi with nothing having pulled it, so the
    one command that keeps it must be printed here too — the arm that skipped
    it was the arm reached by the restore-ending rounds this whole ordering
    was written for.
    """
    server = _Wizard(after_open={
        "phase": "review", "session_id": "after", "candidate": CANDIDATE,
        "failure": {"code": "correction_level_shortfall"},
        "round_receipt": {"round_id": "after", "adoption": "restore",
                          "row": "row5_trusted_safe_regressed"},
    })
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
            FAKE_BANK_EXIT="2",
        )

    assert proc.returncode == 8  # EXIT_SESSION_FAILED, not EXIT_BANK's 9
    assert len(bank_lines) == 1  # the bank did run, and did not keep anything
    repo, _, _ = checkout
    assert str(repo / "scripts" / "bank-crossover-round.sh") in proc.stderr
    assert "SINCE=" in proc.stderr


def test_a_stage_that_never_finishes_times_out_instead_of_banking(checkout, tmp_path):
    """A previous round's terminal phase must not read as this round's finish."""
    server = _Wizard(after_open={"phase": "cloud_measure", "session_id": "after"})
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
             "--stage-timeout-s", "0.4"],
        )

    assert proc.returncode == 7  # EXIT_INCOMPLETE
    assert bank_lines == []


def test_a_terminal_phase_left_by_a_PRIOR_session_does_not_end_this_one(
    checkout, tmp_path
):
    """The session id must MOVE before a terminal phase counts as completion."""
    server = _Wizard(
        v2={"phase": "review", "session_id": "same", "candidate": CANDIDATE},
        after_open={"phase": "review", "session_id": "same", "candidate": CANDIDATE},
    )
    with _serving(server):
        proc, _, bank_lines = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote",
             "--stage-timeout-s", "0.4"],
        )

    assert proc.returncode == 7  # EXIT_INCOMPLETE, not a banked non-round
    assert bank_lines == []


# --------------------------------------------------------------------------- #
# which speaker
# --------------------------------------------------------------------------- #


def test_an_exported_PI_HOST_beats_the_checkouts_env_local(checkout, wizard, tmp_path):
    """The #2689 shape: ``_lib.sh`` sources ``.env.local`` over the export."""
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
        PI_HOST="caller.invalid", PI_USER="caller-user",
    )

    assert proc.returncode == 0, proc.stderr
    assert all("caller-user@caller.invalid" in line for line in ssh_lines)
    # …and the bank is handed the SAME speaker, so one round cannot measure
    # one Pi and bank another.
    dest, bank_host, bank_user, _since = bank_lines[0].split("\t")
    assert (bank_host, bank_user) == ("caller.invalid", "caller-user")
    assert dest == str(tmp_path / "camp" / "r1")


def test_without_an_export_the_checkouts_env_local_is_used(checkout, wizard, tmp_path):
    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    assert all("checkout-user@checkout.invalid" in line for line in ssh_lines)
    assert bank_lines[0].split("\t")[1:3] == ["checkout.invalid", "checkout-user"]


def test_the_speakers_own_name_is_the_host_header_and_the_walks_hostname(
    checkout, wizard, tmp_path
):
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--hostname", "jts9.local", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    walk_cmd = next(
        line for line in ssh_lines if "jasper-angle-capture serve" in line
    )
    assert "--hostname jts9.local" in walk_cmd
    # ...and it is what actually went out on the wire. Asserting only the ssh
    # argument would have let a wrong Host header pass under this test's name.
    hosts = wizard.seen().hosts
    assert hosts and set(hosts) == {"jts9.local"}


def test_exporting_only_PI_HOST_keeps_both_halves_on_your_speaker(
    checkout, wizard, tmp_path
):
    """The speaker's NAME follows the ssh target you named, not the checkout's.

    Taking the target from your export and the name from ``.env.local`` would
    ssh to one speaker carrying another's Host header -- the wizard 403s and
    the diagnosis lands on the wrong thing. ``_lib.sh`` hands the caller's
    targeting over as one record (issue #2689), so there is no split left to
    disclose here.
    """
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
        PI_HOST="caller.invalid",
    )

    assert proc.returncode == 0, proc.stderr
    row = _trail(trail)[0]
    assert row["step"] == "identity"
    assert (row["host"], row["host_from"]) == ("caller.invalid", "your export")
    assert (row["hostname"], row["hostname_from"]) == ("caller.invalid", "your export")
    assert row["split"] is False


def test_exporting_only_JASPER_HOSTNAME_moves_the_ssh_target_with_it(
    checkout, wizard, tmp_path
):
    """The legacy operator form names one speaker, and the trail says so.

    ``_lib.sh`` promotes a lone JASPER_HOSTNAME to the ssh target as well, so
    both halves are the caller's -- reporting the target as ``.env.local``'s
    would name a source that contributed nothing, on a record with no split.
    """
    trail = tmp_path / "trail.jsonl"
    proc, ssh_lines, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
        JASPER_HOSTNAME="caller.invalid",
    )

    assert proc.returncode == 0, proc.stderr
    assert all("@caller.invalid" in line for line in ssh_lines)
    row = _trail(trail)[0]
    assert (row["host"], row["host_from"]) == ("caller.invalid", "your export")
    assert (row["hostname"], row["hostname_from"]) == ("caller.invalid", "your export")
    assert row["split"] is False


def test_a_hostname_override_against_the_checkouts_host_is_still_disclosed(
    checkout, wizard, tmp_path
):
    """A genuine two-source identity stays visible: --hostname is only a name."""
    trail = tmp_path / "trail.jsonl"
    proc, _, _ = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), "--hostname", "jts9.local", *MEASURE_ARGS],
    )

    assert proc.returncode == 0, proc.stderr
    row = _trail(trail)[0]
    assert (row["host"], row["host_from"]) == ("checkout.invalid", ".env.local")
    assert (row["hostname"], row["hostname_from"]) == ("jts9.local", "--hostname")
    assert row["split"] is True


def test_an_unnamed_target_refuses_instead_of_measuring_the_default_speaker(
    checkout, wizard, tmp_path
):
    """#3498: `jts.local` is whichever box on the LAN claimed the name, so a
    round with nothing naming its speaker has no honest guess to make."""
    repo, _fake_bin, _tmp = checkout
    (repo / ".env.local").unlink()
    trail = tmp_path / "trail.jsonl"

    proc, ssh_lines, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1",
         "--trail", str(trail), *MEASURE_ARGS],
    )

    assert proc.returncode == 78  # EXIT_CONFIG
    assert ssh_lines == [] and bank_lines == []
    row = _trail(trail)[0]
    assert row["step"] == "resolve_target"
    assert row["ok"] is False


def test_a_403_open_names_the_host_mismatch_it_could_be(checkout, tmp_path):
    """A 403 is what the management-host guard returns; it cannot say which."""
    server = _Wizard(open_status=403)
    with _serving(server):
        trail = tmp_path / "trail.jsonl"
        proc, _, _ = _run(
            checkout, server,
            ["--campaign", str(tmp_path / "camp"), "--label", "r1",
             "--trail", str(trail), "--hostname", "jts9.local", "--tier", "remote"],
            PI_HOST="caller.invalid",
        )

    assert proc.returncode == 4  # EXIT_OPEN
    detail = next(r for r in _trail(trail) if r["step"] == "open")["detail"]
    assert "caller.invalid" in detail and "jts9.local" in detail


# --------------------------------------------------------------------------- #
# the contract is the product's
# --------------------------------------------------------------------------- #


def test_the_bank_window_is_utc_with_its_zone_spelled_out(
    checkout, wizard, tmp_path
):
    """``journalctl --since`` reads a NAIVE timestamp in the Pi's timezone.

    A laptop west of the speaker would hand it a future window, and the bank
    would pull zero journal lines while exiting 0 — evidence missing, nothing
    said. The suffix is what makes the two machines agree.
    """
    proc, _, bank_lines = _run(
        checkout, wizard,
        ["--campaign", str(tmp_path / "camp"), "--label", "r1", "--tier", "remote"],
    )

    assert proc.returncode == 0, proc.stderr
    since = bank_lines[0].split("\t")[3]
    assert since.endswith(" UTC"), since
    # Parseable as the UTC instant it claims to be, and not in the future.
    stamp = datetime.strptime(since, "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=timezone.utc)
    assert stamp <= datetime.now(timezone.utc) + timedelta(seconds=5)


def _runner():
    """The runner imported as a module, for the contract assertions."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_round_runner", SCRIPT)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: a `@dataclass` under `from __future__ import
    # annotations` resolves its field types through `sys.modules[__module__]`,
    # so a module executed outside it raises inside dataclasses itself.
    sys.modules[spec.name] = runner
    try:
        spec.loader.exec_module(runner)
    finally:
        sys.modules.pop(spec.name, None)
    return runner


def test_an_apply_whose_answer_is_lost_is_not_reported_as_a_wizard_refusal(
    tmp_path,
):
    """Nothing refused: the POST left the laptop and no answer came back.

    The row an operator reads back must not say the wizard blocked it -- the
    graph may or may not have changed, and only the crossover status settles
    that.
    """
    from jasper.active_speaker.wizard_client import REASON_ANSWER_LOST

    runner = _runner()

    class _LostApply:
        def v2_block(self):
            return {"candidate": {"fingerprint": FINGERPRINT}}

        def apply(self, expected_fingerprint):
            return 0, "URLError: [Errno 111] Connection refused"

    path = tmp_path / "trail.jsonl"
    trail = runner.Trail(path)
    code = runner.apply_candidate(_LostApply(), FINGERPRINT, trail)
    trail.close()

    assert code == runner.EXIT_APPLY
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["reason"] for row in rows] == [REASON_ANSWER_LOST]
    assert rows[0]["ok"] is False
