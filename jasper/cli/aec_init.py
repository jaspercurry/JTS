# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Silently restore and verify the volatile XVF3800 chip-AEC profile.

Production boot/reconcile samples the live native-reference queue, resolves
``SYS_DELAY = commissioned K - median(queue)``, applies the one fixed profile,
and verifies every chip write.  It never plays audio, resets the chip, searches
parameters, writes flash, or changes the commissioning artifact.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.chip_aec import health as chip_aec_health
from jasper.chip_aec import shipped as shipped_alignment
from jasper import output_hardware
from jasper.atomic_io import atomic_write_text
from jasper.audio_hardware import dac as dac_registry
from jasper.audio_profile_state import (
    DEFAULT_AEC_MODE_PATH,
    PROFILE_CUSTOM,
    normalize_audio_input_profile,
)
from jasper.env_load import parse_env_file
from jasper.mics.xvf3800 import CHIP_AEC_ENABLED_ENV

# The declaration outputd loads through `EnvironmentFile=` (its runtime output
# contract: sink, backend, active width, final-edge format). Imported, never
# restated as a literal here: an unreadable path leaves the ordering guard below
# fail-OPEN, so a drifted copy would silently disable it with every test still
# green. jasper-audio-hardware-reconcile is the writer; the per-box override is
# the JASPER_OUTPUTD_ENV_FILE both reconcilers already honour.
# tests/test_aec_init.py pins this against jasper-outputd.service's own
# EnvironmentFile= line.
from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_ENV_PATH
from jasper.chip_aec.alignment import (
    QUEUE_MAX_MEDIAN_DRIFT,
    AlignmentIdentity,
    identity_divergence,
    load_artifact,
    median_samples,
    queue_median_drift,
    queue_window_is_stable,
    required_queue_samples,
    runtime_sys_delay,
)
from jasper.log_event import log_event
from jasper.mics import xvf3800
from jasper.route_latency.status_socket import OUTPUTD_STATUS_SOCKET, read_status_socket

logger = logging.getLogger("jasper.aec_init")
COMMISSION_REQUIRED_EXIT = 2
# Where this run publishes its `jasper.chip_aec.health` verdict, as the shell
# assignments jasper-aec-reconcile evals and copies into /etc/jasper/jasper.env.
# Absent means this run reached no verdict (a bypass/corpus run, or one killed
# before its `finally` — a SIGKILL, an OOM kill, an unmet `Requires=`). The
# reconciler clears it before every restart and pairs absence with the exit
# status: a zero exit without a record is a fully applied alignment, a non-zero
# one is a fault.
ALIGNMENT_RECORD_PATH = "/run/jasper-aec-init/alignment"
OUTPUTD_ENV_STALE_EXIT = 3
OUTPUTD_UNIT = "jasper-outputd.service"
# How long to let a queued outputd restart land before refusing to commission.
# Generous next to a Type=notify restart (TimeoutStopSec=5s plus one ALSA open).
#
# The binding budget is the CALLER's, not this oneshot's own
# DefaultTimeoutStartSec: jasper-aec-reconcile's activate_managed_chip_aec runs
# `systemctl restart jasper-aec-init.service` BLOCKING, so aec-init's whole
# runtime is spent inside jasper-aec-reconcile.service's TimeoutStartSec. A
# SIGTERM there is not a clean park — the reconciler has no trap, so the box is
# left deaf with the alignment status stuck at `checking` and the persistent
# voice-input-absent marker in place. Worst case inside aec-init:
#   _find_device            <= 10 s
#   this settle wait        <= 10 s + one SYSTEMCTL_QUERY_TIMEOUT_SEC overrun
#   collect_reference_queue <= 30 s
# = <= 55 s, against the 120 s the reconciler unit now allows. Move either
# number and re-derive both; tests/test_aec_init.py pins the relationship.
OUTPUTD_ENV_SETTLE_SEC = 10.0
# Per-call bound on the `systemctl show` probe so a wedged systemd cannot hold
# the settle wait open past its own deadline. A timeout is treated as STALE, not
# as unknown: an unanswerable systemctl is not evidence that outputd is current.
SYSTEMCTL_QUERY_TIMEOUT_SEC = 5.0
_MIXER_UNITY = re.compile(r"\[0\.00dB\].*\[on\]", re.IGNORECASE)
# How many mix periods outputd may legitimately have in flight to the
# chip-reference writer at the instant STATUS is read.  One write carries
# exactly one mix period of audio at the chip's own rate, so in steady state it
# cannot span more than one period of wall time: the writer holds at most the
# period it is writing, and outputd can publish the next one before the writer
# dequeues it.  This is a pipelining depth, not a cadence — it is the same at
# every mix period, which is why a coarse cadence (where a write blocks for most
# of a period, so STATUS lands mid-write about half the time) reads a lag of one
# as often as zero.  A deeper backlog means the writer is losing ground on a
# rate-locked producer, which the error counters and write ages then confirm.
MAX_REFERENCE_PERIODS_IN_FLIGHT = 2
# Where the writer's own recent per-write observations live in STATUS, and the
# keys one entry carries.  outputd publishes raw observations; every acceptance
# rule over them is here and in jasper/chip_aec/alignment.py.
RECENT_WRITES_KEY = "recent_writes"
# How long to wait between STATUS reads.  Polling harder buys nothing: outputd's
# state server is one thread that sleeps 500 ms between accepts and answers one
# command per connection, so a sequential reader gets about two reads a second
# whatever it asks for (measured on jts3: 11 reads in 5.14 s).  What makes a
# window reachable at that rate is the ring — each read carries every write
# since the last one, rather than the single latest reading.
QUEUE_POLL_INTERVAL_SEC = 0.25
# How long the reference queue has to hold still, independent of how many
# readings that takes.  Sample count alone bounds the median's precision but
# says nothing about drift: a queue walking steadily away from its start can
# hold a narrow spread across a short window and still be worthless to K, which
# is measured once and reused for months.  1.75 s is the span the pre-#2253
# window covered (eight readings at its fixed 0.25 s poll leaves seven
# intervals), rounded up, so the drift a fine-cadence box had to survive is
# unchanged.
QUEUE_MIN_WINDOW_SEC = 2.0
# How far back the window reaches.  The window SLIDES: readings older than this
# are dropped on every pass, so one outlier write ages out instead of holding
# the spread — and with it the required reading count — up for the whole
# collection budget.  Eight seconds is four times the minimum window, so a box
# always has a window to offer, and it bounds the retained readings at the
# finest cadence in the fleet (375 writes/s x 8 s = 3000 entries).
#
# It is also what caps how wide a spread a box can answer for, since the
# retained count is cadence x this horizon: 375 readings at jts3's 46.9
# writes/s supports a spread up to 109 frames against the 86 it measures, and
# 3000 at jts.local's 375 writes/s supports 309 against 11.  A box past its cap
# spends the whole collection budget and then fails with its own numbers —
# naming the ceiling, not a reading shortfall a longer budget could close —
# rather than quietly publishing a median it did not earn.
QUEUE_WINDOW_MAX_SEC = 8.0
REFERENCE_WRITER_COUNTER_NAMES = (
    "open_error_count",
    "retry_count",
    "write_underrun_count",
    "write_xrun_count",
    "write_error_count",
    "write_recovery_count",
    "dropped_periods_due_to_full_queue",
    "dropped_periods_due_to_disconnected_writer",
    "dropped_periods_while_unavailable",
)


class ChipInitError(RuntimeError):
    pass


class ReferenceCountersMovedError(ChipInitError):
    """The reference writer's error counters advanced mid-window."""


class CommissionRequired(ChipInitError):
    pass


class ChipProfileError(ChipInitError):
    pass


class OutputdEnvStale(ChipInitError):
    """The live outputd predates its own declaration, so STATUS is not current."""


class _SystemctlTimeout(RuntimeError):
    """`systemctl show` did not answer inside its own per-call budget."""


@dataclass(frozen=True)
class ReferenceWrite:
    """One completed chip-reference write, as outputd's writer observed it."""

    delay: int
    # Cumulative frames_written AFTER this write, so it is strictly increasing
    # across the ring and is the entry's identity.  The writer's priming write
    # at PCM open carries no reference sequence, which is why the sequence is
    # not the key.
    frames: int
    sequence: int | None
    age_ms: int


@dataclass(frozen=True)
class ReferenceSnapshot:
    """What one STATUS read says about the chip-reference writer."""

    counters: tuple[int, ...]
    writes: tuple[ReferenceWrite, ...]


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def native_reference_pcm(card: str) -> str:
    return (
        f"{xvf3800.CHIP_AEC_REFERENCE_PCM_ACCESS}:CARD={card},"
        f"DEV={xvf3800.CHIP_AEC_REFERENCE_DEVICE_INDEX}"
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChipInitError(f"outputd STATUS missing {name}")
    return value


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ChipInitError(f"outputd STATUS {name} is invalid")
    return value


def _reference_writes(writer: Mapping[str, Any]) -> tuple[ReferenceWrite, ...]:
    """Parse the writer's recent per-write observations out of STATUS.

    An outputd too old to publish the ring omits the key entirely.  Never
    substitute the single latest reading for it: a reader capped at two STATUS
    reads a second cannot assemble a window that way on any box, so guessing
    would trade a loud refusal for a boot that burns its whole budget and then
    blames the queue.  Same shape as `build_identity`'s `dac.format` refusal —
    the box needs a newer outputd, and says so.

    An EMPTY ring is not that: a writer that has just opened its PCM has
    nothing to report yet, and the next read will.
    """

    entries = writer.get(RECENT_WRITES_KEY)
    if not isinstance(entries, list):
        raise ChipInitError(
            "outputd STATUS has no chip-ref sample ring — deploy an outputd "
            "that reports it before commissioning chip AEC"
        )
    writes: list[ReferenceWrite] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ChipInitError("outputd STATUS chip-ref sample ring is malformed")
        sequence = entry.get("reference_sequence")
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ChipInitError(
                "outputd STATUS chip-ref sample ring has an invalid reference_sequence"
            )
        writes.append(
            ReferenceWrite(
                _integer(
                    entry.get("snd_pcm_delay_frames"),
                    f"{RECENT_WRITES_KEY}.snd_pcm_delay_frames",
                ),
                _integer(
                    entry.get("frames_written"),
                    f"{RECENT_WRITES_KEY}.frames_written",
                    positive=True,
                ),
                sequence,
                _integer(entry.get("age_ms"), f"{RECENT_WRITES_KEY}.age_ms"),
            )
        )
    if any(
        later.frames <= earlier.frames
        for earlier, later in zip(writes, writes[1:], strict=False)
    ):
        raise ChipInitError("outputd STATUS chip-ref sample ring is not in write order")
    return tuple(writes)


def validate_reference_status(
    status: Mapping[str, Any], *, expected_pcm: str
) -> ReferenceSnapshot:
    if status.get("backend") != "alsa":
        raise ChipInitError("outputd ALSA backend is not active")
    refs = _mapping(status.get("reference_outputs"), "reference_outputs")
    expected = {
        "speaker_reference_source": "outputd_final_electrical",
        "speaker_reference_is_fallback": False,
        "speaker_reference_channels": xvf3800.CHIP_AEC_REFERENCE_CHANNELS,
        "chip_ref_pcm": expected_pcm,
        "chip_ref_sample_rate": xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ,
        "chip_ref_period_frames": xvf3800.CHIP_AEC_REFERENCE_PERIOD_FRAMES,
        "chip_ref_buffer_frames": xvf3800.CHIP_AEC_REFERENCE_BUFFER_FRAMES,
        "chip_ref_transform": xvf3800.CHIP_AEC_REFERENCE_TRANSFORM,
    }
    for name, wanted in expected.items():
        if refs.get(name) != wanted:
            raise ChipInitError(
                f"outputd reference {name}={refs.get(name)!r}; expected {wanted!r}"
            )
    writer = _mapping(refs.get("chip_ref_writer"), "chip_ref_writer")
    if (
        writer.get("desired") is not True
        or writer.get("active") is not True
        or writer.get("status") != "active"
    ):
        raise ChipInitError("native chip-reference writer is not current and active")
    lag = _integer(writer.get("reference_sequence_lag"), "reference_sequence_lag")
    if lag > MAX_REFERENCE_PERIODS_IN_FLIGHT:
        raise ChipInitError(
            f"native chip-reference writer is {lag} mix periods behind outputd"
        )
    for name in ("snd_pcm_delay_sample_age_ms", "last_write_age_ms"):
        if _integer(writer.get(name), name) > 250:
            raise ChipInitError(f"native chip-reference writer {name} is stale")
    return ReferenceSnapshot(
        tuple(
            _integer(writer.get(name), name)
            for name in REFERENCE_WRITER_COUNTER_NAMES
        ),
        _reference_writes(writer),
    )


def _log_ordering_probe_anomaly(
    outcome: str, *, returncode: int, value: str
) -> None:
    """WARN that the ordering guard has gone inert for a reason that is not a state.

    Without this the guard fails silent: it returns "not stale", commissioning
    proceeds, and nothing anywhere says the check did not actually run. Only the
    anomalous paths land here — a unit that has simply never started this boot is
    a legitimate quiet case.
    """

    log_event(
        logger,
        "chip_aec_init.ordering_probe",
        outcome=outcome,
        unit=OUTPUTD_UNIT,
        returncode=returncode,
        # Truncated: systemctl's stderr/stdout is not a trusted length.
        value=value[:120],
        action="ordering guard is inert; inspect systemctl and jasper-outputd",
        level=logging.WARNING,
    )


def outputd_main_start_realtime(
    *, env: Mapping[str, str] | None = None
) -> float | None:
    """Return the epoch instant outputd's main process started, or None if unknown.

    ``ExecMainStartTimestamp`` is the CLOCK_REALTIME sibling of the monotonic
    property, which is what lets the caller compare it against a file mtime
    without mixing clock domains.  ``--timestamp=unix`` renders it as
    ``@<seconds>`` instead of a locale-formatted string; the exact rendering is
    load-bearing, so it is pinned against hardware in tests/test_aec_init.py.

    None means four things, all of which leave the guard inert:

    * The unit has never started this boot.  systemd prints an EMPTY value for
      that (probed on jts3/jts4, systemd 257) — quiet, because it is legitimate.
    * ``systemctl`` exited non-zero.  That is the unsupported-``--timestamp=unix``
      class (added in 247; Trixie ships 257, so it is theoretical).  WARN-logged.
    * The value was non-empty but did not parse.  WARN-logged.
    * ``systemctl`` could not even be exec'd.  A missing binary
      (``FileNotFoundError``) means this box could never run outputd anyway —
      quiet, same as the never-started case.  Any other ``OSError`` (``EACCES``,
      ``ENOEXEC``, ``EAGAIN``, ``ENOMEM``, ...) means the binary IS there but the
      probe itself failed on what is a systemd host — WARN-logged, same as the
      two above.

    The middle two are unconditionally anomalies; the fourth splits between a
    quiet branch (``FileNotFoundError``, a state like the first) and a
    WARN-logged one (any other ``OSError``).  Every anomalous branch is
    logged: an inert guard is otherwise invisible, and silence would look
    exactly like success.

    The asymmetry against `_SystemctlTimeout` is deliberate.  A timeout means
    systemctl is unanswerable *while the flag is known-supported*, so there is no
    reason to believe outputd is current — fail closed.  A non-zero exit is the
    unsupported-flag class, where failing closed would park every speaker on an
    older systemd for a guard it cannot run — fail open to the pre-guard
    behaviour, and say so in the journal.

    None does NOT mean "outputd is stopped".  systemd RETAINS this timestamp after
    a unit stops, so a stopped outputd that ran this boot still reports its old
    start instant, and a declaration newer than it reads as stale.  That is
    stricter than passing the case through, and it is the safe direction.

    Raises:
        _SystemctlTimeout: the query did not answer inside its own budget.
    """

    source = os.environ if env is None else env
    systemctl = source.get("JASPER_SYSTEMCTL") or "systemctl"
    try:
        result = subprocess.run(
            [
                systemctl,
                "show",
                "--timestamp=unix",
                "-p",
                "ExecMainStartTimestamp",
                "--value",
                OUTPUTD_UNIT,
            ],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_QUERY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise _SystemctlTimeout(str(exc)) from exc
    except FileNotFoundError:
        # No systemctl at all: not a systemd host (tests, dev shells). Not an
        # anomaly worth a WARN on a box where the daemon could never run anyway.
        return None
    except OSError as exc:
        # systemctl IS present but exec'ing it failed some other way (EACCES,
        # ENOEXEC, EAGAIN, ENOMEM, ...) on what is a systemd host — unlike the
        # missing-binary case above, that is worth surfacing rather than
        # silently treating this box as one where the daemon could never run.
        _log_ordering_probe_anomaly(
            "systemctl_oserror", returncode=-1, value=str(exc)
        )
        return None
    raw = result.stdout.strip()
    if result.returncode:
        _log_ordering_probe_anomaly(
            "systemctl_failed", returncode=result.returncode, value=raw
        )
        return None
    if not raw:
        # Never started this boot — systemd's empty value. Legitimate, so quiet.
        return None
    digits = raw.removeprefix("@")
    if not digits.isdigit():
        _log_ordering_probe_anomaly("unparseable", returncode=0, value=raw)
        return None
    return float(digits)


def outputd_env_staleness(env: Mapping[str, str] | None = None) -> str:
    """Return why the live outputd predates its declaration, or "" when it does not.

    Compares two CLOCK_REALTIME INSTANTS — the env file's mtime against outputd's
    recorded start.  Both move together under an NTP step, so a step cannot flip
    the verdict; comparing a realtime age against a monotonic one could, and did
    (a forward step made the guard certify exactly the stale outputd the
    invariant forbids, which is routine on an RTC-less Pi at boot).

    An env at most as old as the start fails closed.  systemd reads
    ``EnvironmentFile=`` just BEFORE forking the main process, so a write that
    ties the recorded fork instant was not loaded — and a tie is not proof that
    it was.  ``--timestamp=unix`` reports whole seconds, so a start instant
    truncates DOWN; within one second of an env write that can only over-report
    staleness (a bounded false defer), never certify a stale edge.

    Honest residual, inherent to comparing recorded stamps: only a BACKWARD clock
    step can invert their order, and only when it lands between the two stamps AND
    exceeds their separation.  Forward steps are non-decreasing, so they cannot
    invert anything — both stamps stay in the order they were recorded.  A
    qualifying backward step goes either way depending on which stamp it lands
    after: it can hide a genuinely stale outputd, or falsely defer a fresh one.

    This is not closable by ordering the readers.  The verdict is a pure function
    of two stamps already written by OTHER processes (systemd for the start,
    jasper-audio-hardware-reconcile for the mtime), so no `After=` on this unit or
    on jasper-aec-reconcile can change it — an earlier revision carried
    `After=time-sync.target` for exactly that and it was a belt attached to
    nothing.  It is also vanishingly rare on this fleet: fake-hwclock restores a
    PAST time at boot, so NTP's first correction is a FORWARD step, which is the
    harmless direction.
    """

    source = os.environ if env is None else env
    path = source.get("JASPER_OUTPUTD_ENV_FILE") or DEFAULT_OUTPUTD_ENV_PATH
    try:
        env_mtime = os.stat(path).st_mtime
    except OSError:
        # No declaration on this box, so there is nothing to be stale against.
        return ""
    try:
        started = outputd_main_start_realtime(env=source)
    except _SystemctlTimeout:
        return (
            f"systemctl show {OUTPUTD_UNIT} did not answer within "
            f"{SYSTEMCTL_QUERY_TIMEOUT_SEC:.0f}s, so the output declaration "
            "outputd is running cannot be confirmed"
        )
    if started is None:
        return ""
    if env_mtime < started:
        return ""
    return (
        f"{OUTPUTD_UNIT} started {env_mtime - started:.1f}s before {path} was "
        "written, so it has not loaded the current output declaration"
    )


def require_outputd_env_loaded(
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = OUTPUTD_ENV_SETTLE_SEC,
    interval: float = 0.5,
) -> None:
    """Refuse to certify outputd STATUS that predates outputd's own declaration.

    jasper-audio-hardware-reconcile writes ``outputd.env`` and then kicks
    jasper-outputd and jasper-aec-reconcile as two SEPARATE ``--no-block``
    systemd transactions (``restart_audio_if_needed``), and udev's
    99-jasper-aec-reconcile.rules starts the reconciler in a transaction of its
    own; jasper-aec-init.service's ``After=jasper-outputd.service`` only orders
    jobs that already share one transaction, and the reconciler starts this
    oneshot by itself.  So nothing guarantees the outputd answering STATUS has
    loaded the declaration on disk — the old process stays up, healthy, and
    answering with the PREVIOUS geometry and final-edge format.

    Binding (or rejecting) a commissioning identity against that is the debt
    ADR-0169 closes.  Wait a bounded while for the queued restart to land,
    then fail rather than commission.

    The deadline is monotonic on purpose: it is a DURATION, so it wants the clock
    that cannot step.  The staleness verdict itself compares realtime instants —
    see ``outputd_env_staleness``.  Worst case here is ``timeout`` plus one
    ``SYSTEMCTL_QUERY_TIMEOUT_SEC`` probe, because the deadline is checked after
    a poll rather than during one; that overrun is inside the caller budget
    derived at ``OUTPUTD_ENV_SETTLE_SEC``.
    """

    deadline = time.monotonic() + timeout
    while True:
        reason = outputd_env_staleness(env)
        if not reason:
            return
        if time.monotonic() >= deadline:
            raise OutputdEnvStale(reason)
        time.sleep(interval)


def _merge_writes(
    window: list[tuple[float, ReferenceWrite]],
    writes: Sequence[ReferenceWrite],
    now: float,
    floor: float,
) -> list[tuple[float, ReferenceWrite]]:
    """Fold one read's ring into the sliding window and drop what aged out.

    Each observation is placed on the local monotonic clock from the age STATUS
    reported for it.  Within one read those ages share a snapshot instant, so
    the relative spacing is exact; across reads the only error is how long the
    read itself took, which is milliseconds against a window measured in
    seconds.

    ``floor`` is the instant the window may reach back to.  It matters because
    the ring is HISTORY: clearing the window after a seam (a moved error
    counter, a restarted writer) would otherwise be undone by the very next
    read, which hands back the same pre-seam observations.  The floor is set to
    the instant a seam was seen, so what rebuilds the window is only what the
    writer produced after it.

    A ring whose newest write is older than the window's newest means the
    writer's cumulative frame counter went backwards — outputd restarted and
    built a new state under the poll, so readings from before and after belong
    to different processes and no median spans them.
    """

    if writes and window and writes[-1].frames < window[-1][1].frames:
        raise ChipInitError("chip-reference writer did not progress cleanly")
    merged = list(window)
    for write in writes:
        # Entries at or below the newest already in the window were folded in by
        # an earlier read; the rings of two reads overlap by design.
        if merged and write.frames <= merged[-1][1].frames:
            continue
        merged.append((now - write.age_ms / 1_000, write))
    # One filter, both jobs: nothing older than the sliding horizon, and nothing
    # from before the floor.
    horizon = max(now - QUEUE_WINDOW_MAX_SEC, floor)
    return [item for item in merged if item[0] >= horizon]


def collect_reference_queue(
    expected_pcm: str,
    *,
    socket_path: str = OUTPUTD_STATUS_SOCKET,
    interval: float = QUEUE_POLL_INTERVAL_SEC,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """Collect a run of per-write queue readings precise enough to carry K.

    outputd records ``snd_pcm_delay`` once per completed chip-reference write
    and publishes the recent ones as a ring, so a window is assembled from the
    WRITER's observations rather than from however often this loop manages to
    ask.  That is what makes the window reachable: outputd answers about two
    STATUS reads a second, while its writer writes 47-375 times a second
    depending on the mix cadence.

    The window slides.  Every read folds in the fresh entries and drops anything
    older than `QUEUE_WINDOW_MAX_SEC`, so a single outlier write leaves the
    window on its own instead of holding the spread up until the budget runs
    out.  It is accepted once it has spanned `QUEUE_MIN_WINDOW_SEC` AND
    `queue_window_is_stable` holds — the median estimated as precisely as the
    reference window's, and the two halves agreeing that it did not move.  Both
    this acceptance and `runtime_sys_delay`'s re-validation call that one rule,
    so boot cannot reject the window commissioning accepted.

    A queue that will not settle fails inside the budget with its own numbers,
    and logs them: a reference queue that cannot hold still cannot carry K, and
    the operator needs to see which of the three conditions it missed.
    """

    # Both consumers of a live STATUS — this oneshot and jasper-aec-commission —
    # funnel through here, and the queue positions returned below are as
    # identity-critical as the dac.format build_identity reads out of the same
    # snapshot, so the ordering guard belongs on this one path.  It runs before
    # the poll so a stale verdict propagates as itself instead of being folded
    # into last_error by the loop's own ChipInitError handler.
    require_outputd_env_loaded()

    deadline = time.monotonic() + timeout
    window: list[tuple[float, ReferenceWrite]] = []
    # The counter baseline and the instant it was taken, as ONE value — the
    # window's floor is that instant and nothing else, so the two cannot drift
    # apart and there is no floor to plant before a baseline exists.
    #
    # It has to be the baseline instant rather than the collection's start.
    # The ring is history: its oldest entries predate this read, and the error
    # counters are cumulative, so a seam anywhere before the baseline was taken
    # is one nothing can see — a reading admitted from before it would sit in
    # an interval this loop never watched both ends of, and a step at the
    # leading edge is invisible to the split-half drift bound by construction.
    # With the floor here the invariant is exact: every retained reading lies
    # between two instants at which the counters were observed equal, and a
    # cumulative counter cannot tick in between and come back.  What the ring
    # buys is not reach into the past — it is that no write made WHILE watching
    # is missed at two reads a second.
    baseline: tuple[tuple[int, ...], float] | None = None
    status_reads = 0
    last_error = "no STATUS response"
    last_error_cls: type[ChipInitError] = ChipInitError
    while time.monotonic() < deadline:
        try:
            status = read_status_socket(socket_path)
            # Sampled AFTER the read on purpose.  outputd's state server sleeps
            # up to its accept poll BEFORE it takes the snapshot, so that wait
            # precedes the observation rather than following it; what separates
            # this instant from the snapshot is only building, writing and
            # reading the reply.  Sampling before the read would carry the whole
            # accept wait into the placement instead, and would move the floor
            # EARLIER — the loosening direction on both counts.  The bias is
            # measured in tests/test_aec_init.py against a fixture that models
            # the server's real order (accept wait, snapshot, reply).
            now = time.monotonic()
            status_reads += 1
            snapshot = validate_reference_status(status, expected_pcm=expected_pcm)
            if baseline is None:
                baseline = (snapshot.counters, now)
                window = []
            elif snapshot.counters != baseline[0]:
                raise ReferenceCountersMovedError(
                    "chip-reference writer error counters moved"
                )
            window = _merge_writes(window, snapshot.writes, now, baseline[1])
        except (OSError, ValueError, ChipInitError) as exc:
            # An xrun, a reopen, or a restart splits the readings on either side
            # of it into different pipeline states, so the window starts over
            # and waits for a fresh baseline — which carries the floor past the
            # seam, or the next read's ring would hand the discarded pre-seam
            # observations straight back.
            window = []
            baseline = None
            last_error = str(exc)
            last_error_cls = type(exc) if isinstance(exc, ChipInitError) else ChipInitError
        else:
            delays = tuple(item.delay for _at, item in window)
            held = _window_span(window)
            if held >= QUEUE_MIN_WINDOW_SEC and queue_window_is_stable(delays):
                _log_reference_queue(
                    "stable",
                    delays=delays,
                    held=held,
                    status_reads=status_reads,
                    interval=interval,
                )
                return status, delays
            last_error = _queue_shortfall(delays, held)
            last_error_cls = ChipInitError
        time.sleep(interval)
    _log_reference_queue(
        "unstable",
        delays=tuple(item.delay for _at, item in window),
        held=_window_span(window),
        status_reads=status_reads,
        interval=interval,
        # The reason travels in the event too: a refusal whose numbers are all
        # zero (an outputd with no ring, a writer that never went active) is
        # indistinguishable from any other under the instrument otherwise.
        reason=last_error,
        level=logging.WARNING,
    )
    raise last_error_cls(f"native chip-reference writer not ready: {last_error}")


def _window_span(window: Sequence[tuple[float, ReferenceWrite]]) -> float:
    """How long the retained readings span, on the local monotonic clock."""

    if not window:
        return 0.0
    instants = [at for at, _write in window]
    return max(instants) - min(instants)


def _log_reference_queue(
    outcome: str,
    *,
    delays: Sequence[int],
    held: float,
    status_reads: int,
    interval: float,
    reason: str = "",
    level: int = logging.INFO,
) -> None:
    """Record the window one collection closed on — including the ones it did not.

    A refusal is the case this instrument was built for, and it needs the
    covariate that separates "the queue would not hold still" from "the reader
    could not sample it": how many STATUS reads it actually got, at what poll
    interval, for how many readings.
    """

    spread = max(delays) - min(delays) if delays else 0
    fields: dict[str, Any] = {
        "outcome": outcome,
        "samples": len(delays),
        "spread": spread,
        "required": required_queue_samples(spread) if delays else 0,
        "median": median_samples(delays) if delays else 0,
        "median_drift": queue_median_drift(delays),
        "held_sec": round(held, 2),
        "status_reads": status_reads,
        "poll_interval_ms": round(interval * 1_000, 1),
    }
    if reason:
        fields["reason"] = reason
    log_event(logger, "chip_aec_init.reference_queue", level=level, **fields)


def _queue_shortfall(delays: Sequence[int], held: float) -> str:
    if not delays:
        return "chip-reference writer has published no queue readings yet"
    spread = max(delays) - min(delays)
    required = required_queue_samples(spread)
    # The window slides, so the readings it can ever hold are this box's write
    # rate times the retention horizon — no matter how much budget is left.
    # Reporting a plain reading shortfall when the required count is past that
    # ceiling would send an operator looking for a longer budget that can never
    # close it: the queue is simply wider than this output cadence can carry K
    # at.  The rate comes from the window's own readings, so no cadence is
    # assumed here.
    reachable = len(delays) / held * QUEUE_WINDOW_MAX_SEC if held > 0 else 0.0
    if reachable and required > reachable:
        return (
            f"chip-reference queue spread {spread} needs {required} readings, "
            f"more than the {reachable:.0f} this cadence produces inside the "
            f"{QUEUE_WINDOW_MAX_SEC:g}s retention window; the queue is wider "
            "than this output cadence can carry K at"
        )
    return (
        f"chip-reference queue spread {spread} needs "
        f"{required} readings over "
        f"{QUEUE_MIN_WINDOW_SEC:g}s with its halves within "
        f"{QUEUE_MAX_MEDIAN_DRIFT} frames; held {len(delays)} over "
        f"{held:.1f}s with halves {queue_median_drift(delays)} apart"
    )


def _chip_text(dev, name: str) -> str:
    values = dev.read(name)
    text = "".join(str(value) for value in values).replace("\x00", "").strip()
    if not text:
        raise ChipInitError(f"XVF {name} is empty")
    return text


def xvf_factory_serial(dev) -> str:
    try:
        serial = dev.factory_serial()
    except Exception as exc:  # noqa: BLE001 - normalize the host-wrapper failure
        raise ChipInitError(f"XVF factory identity is unavailable: {exc}") from exc
    if not isinstance(serial, str) or not serial.strip():
        raise ChipInitError("XVF factory identity is unavailable")
    return serial.strip()


def output_hardware_key(
    source: Mapping[str, str],
    state: output_hardware.OutputHardwareState | None = None,
) -> str:
    output_id = source.get("JASPER_AUDIO_DAC_ID", "").strip()
    profile = dac_registry.by_id(output_id)
    snapshot = (
        output_hardware.load_state(
            source.get("JASPER_OUTPUT_HARDWARE_STATE_PATH") or None
        )
        if state is None
        else state
    )
    if profile is None:
        raise ChipInitError("output profile identity is unknown")
    if snapshot is None:
        raise ChipInitError("output hardware identity is unavailable")
    if snapshot.status != "ready" or snapshot.profile_id != output_id:
        raise ChipInitError("output hardware identity does not match the active profile")
    if not snapshot.selected_card_id:
        raise ChipInitError("output hardware card identity is unavailable")
    if profile.connection == "i2s":
        return f"i2s:{output_id}:{snapshot.selected_card_id}"

    selected = tuple(
        child
        for child in snapshot.child_devices
        if child.card_id == snapshot.selected_card_id
    )
    if len(selected) != 1 or not selected[0].serial:
        raise ChipInitError("USB output factory identity is unavailable")
    return f"usb-serial:{selected[0].serial}"


def build_identity(
    dev,
    plan: xvf3800.ChipBeamPlan,
    status: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    output_state: output_hardware.OutputHardwareState | None = None,
) -> AlignmentIdentity:
    source = os.environ if env is None else env
    dac = _mapping(status.get("dac"), "dac")
    sink = status.get("sink_mode")
    pcm = dac.get("pcm")
    if not isinstance(sink, str) or not isinstance(pcm, str):
        raise ChipInitError("outputd output identity is incomplete")
    # Never substitute a default here.  An outputd too old to report the
    # final-edge format has to be replaced, not guessed around: a guess would
    # certify an artifact against an edge nobody verified, and re-running the
    # commissioner would not fix it either.
    output_format = dac.get("format")
    if not isinstance(output_format, str) or not output_format.strip():
        raise ChipInitError(
            "outputd STATUS has no dac.format — deploy an outputd that reports "
            "the final-edge format before commissioning chip AEC"
        )
    try:
        return AlignmentIdentity(
            xvf_variant=source.get("JASPER_XVF_VARIANT", ""),
            xvf_serial=xvf_factory_serial(dev),
            xvf_firmware=_chip_text(dev, "BLD_REPO_HASH"),
            beam_plan=plan.plan_id,
            fixed_profile=xvf3800.chip_aec_fixed_profile_fingerprint(plan),
            output_id=source.get("JASPER_AUDIO_DAC_ID", ""),
            output_hardware_key=output_hardware_key(source, output_state),
            output_pcm=f"{sink}:{pcm}",
            output_format=output_format,
            output_rate=_integer(dac.get("sample_rate"), "dac.sample_rate", positive=True),
            # outputd treats an unset or blank active width as the ordinary
            # two-channel sink; preserve that identity contract here.
            output_channels=int(
                source.get("JASPER_OUTPUTD_ACTIVE_CHANNELS", "").strip() or "2"
            ),
            output_period=_integer(dac.get("period_frames"), "dac.period_frames", positive=True),
            output_buffer=_integer(dac.get("buffer_frames"), "dac.buffer_frames", positive=True),
        )
    except (TypeError, ValueError) as exc:
        raise ChipInitError(f"live alignment identity is invalid: {exc}") from exc


def _matches(expected: Sequence[int | float], actual: object) -> bool:
    if not isinstance(actual, Sequence) or isinstance(actual, str | bytes):
        actual = (actual,)
    return len(expected) == len(actual) and all(
        abs(float(want) - float(got)) <= (1e-4 if isinstance(want, float) else 0)
        for want, got in zip(expected, actual, strict=True)
    )


def write_required(dev, name: str, values: list[int | float]) -> None:
    try:
        dev.write(name, values)
        actual = dev.read(name)
    except Exception as exc:  # noqa: BLE001
        raise ChipProfileError(f"{name}={values} failed: {exc}") from exc
    if not _matches(values, actual):
        raise ChipProfileError(f"{name} readback mismatch: {actual!r}")


def set_uac_unity(card: str) -> None:
    for control in ("PCM,0", "PCM,1"):
        result = subprocess.run(
            ["amixer", "-c", card, "sset", control, "60", "unmute"],
            capture_output=True,
            text=True,
        )
        readback = subprocess.run(
            ["amixer", "-c", card, "sget", control],
            capture_output=True,
            text=True,
        )
        lines = [
            line
            for line in readback.stdout.splitlines()
            if "Playback " in line and "[" in line
        ]
        if result.returncode or readback.returncode or not lines or any(
            _MIXER_UNITY.search(line) is None for line in lines
        ):
            raise ChipProfileError(f"{control} did not verify at 0 dB/unmuted")


def apply_profile(
    dev,
    plan: xvf3800.ChipBeamPlan,
    sys_delay: int,
    *,
    card: str,
    arm: bool = True,
) -> None:
    profile = xvf3800.chip_aec_profile_commands(plan, sys_delay=sys_delay)
    if profile[0] != ("SHF_BYPASS", [1]) or profile[-1] != ("SHF_BYPASS", [0]):
        raise ChipProfileError("canonical profile is not SHF-bracketed")
    for name, values in profile[:-1]:
        write_required(dev, name, values)
    set_uac_unity(card)
    if arm:
        write_required(dev, *profile[-1])


def _safe_bypass(dev) -> None:
    try:
        write_required(dev, "SHF_BYPASS", [1])
    except ChipProfileError:
        pass


def apply_bypass_profile(dev, *, card: str) -> None:
    for name, values in xvf3800.CHIP_AEC_BYPASS_PROFILE_COMMANDS:
        write_required(dev, name, values)
    set_uac_unity(card)


def _find_device(xvf_host):
    for _ in range(10):
        device = xvf_host.find()
        if device is not None:
            return device
        time.sleep(1)
    raise ChipInitError("XVF3800 did not enumerate")


def shipped_class_alignment(
    dev, plan: xvf3800.ChipBeamPlan, status: Mapping[str, Any], *, absent: str
) -> shipped_alignment.ShippedAlignment:
    """Return the alignment shipped for this box's hardware class, or refuse.

    Nothing is banked on this box, so every way this can fail lands on the
    disposition the absent artifact already had — commission required, which
    the reconciler discloses and runs software AEC3 for — never a fault.

    A miss names the nearest row and the class fields it disagrees on: after a
    firmware flash or a geometry change, "no shipped row" and "the row is one
    field away" are the same park otherwise.
    """

    try:
        live = build_identity(dev, plan, status)
    except ChipInitError as exc:
        raise CommissionRequired(absent) from exc
    entry = shipped_alignment.for_identity(live)
    if entry is not None:
        return entry
    # The caller only reaches here with a non-empty REGISTRY, and the divergence
    # that ranks the rows is the same one the message names.
    nearest, changed = min(
        ((row, row.divergence(live)) for row in shipped_alignment.REGISTRY),
        key=lambda pair: len(pair[1]),
    )
    raise CommissionRequired(
        f"{absent}; nearest shipped class {nearest.label} differs in "
        + ", ".join(changed)
    )


@dataclass(frozen=True)
class BankedAlignment:
    """The banked proof one run resolved, and what it says about its source.

    ``commissioned_sys_delay`` is the delay banked alongside K — by this unit's
    own artifact or by the row shipped for its hardware class — journalled so a
    boot's delay can be read against the commissioner's.  ``sys_delay`` is what
    the live reference queue resolves that K to, and is what the chip is written
    with; it is not bounded against the commissioned one (ADR-0223).

    ``shipped_label`` names the hardware-class row this ran from when nothing is
    banked on the unit; ``identity_diff`` is what the unit's own commissioned
    identity disagrees with live.  `jasper.chip_aec.health` turns them into the
    household verdict — they are facts here, never prose.
    """

    k_samples: int
    commissioned_sys_delay: int
    sys_delay: int
    queue: tuple[int, ...]
    shipped_label: str
    identity_diff: tuple[str, ...]


def resolve_banked_alignment(
    dev, plan: xvf3800.ChipBeamPlan, *, card: str
) -> BankedAlignment:
    """Resolve the K this run applies, against the live native-reference queue.

    ADR-0101: a proof that stopped describing this box is applied and disclosed,
    not parked. This returns what moved; `jasper.chip_aec.health` judges it into
    the `disclosed_stale` the reconciler publishes.

    Raises:
        CommissionRequired: this box has no banked K it can run from.
    """

    shipped_label = ""
    changed: tuple[str, ...] = ()
    commissioned_identity: AlignmentIdentity | None = None
    absent: str | None = None
    try:
        artifact = load_artifact()
        k_samples = artifact.k_samples
        commissioned_sys_delay = artifact.sys_delay
        commissioned_identity = artifact.identity
    except (OSError, ValueError) as exc:
        # Nothing banked on this box. ADR-0101 (#2984): if the shipped table
        # knows this hardware class, run from the proof measured on a sibling;
        # otherwise there is no K to run from at all. Which one it is needs the
        # live identity, so the verdict waits for STATUS — but an EMPTY table can
        # match nothing, so it does not spend the queue budget discovering that.
        if not shipped_alignment.REGISTRY:
            raise CommissionRequired(str(exc)) from exc
        absent = str(exc)
    try:
        status, queue = collect_reference_queue(native_reference_pcm(card))
    except ChipInitError as exc:
        if absent is None or isinstance(exc, OutputdEnvStale):
            raise
        # Nothing is banked either way, so a queue that will not settle leaves
        # the disposition the absent artifact already had.
        raise CommissionRequired(absent) from exc
    if absent is not None:
        shipped = shipped_class_alignment(dev, plan, status, absent=absent)
        k_samples = shipped.k_samples
        commissioned_sys_delay = shipped.sys_delay
        shipped_label = shipped.label
    elif commissioned_identity is not None:
        changed = identity_divergence(
            commissioned_identity, build_identity(dev, plan, status)
        )
    try:
        delay = runtime_sys_delay(k_samples, queue)
    except ValueError as exc:
        # A shipped K that will not resolve on this box (the driver cap refuses
        # the delay, or the queue is unstable) leaves the same disposition its
        # absent artifact already had — this box needs its own commissioning,
        # not an inspection of a healthy daemon.
        if absent is not None:
            raise CommissionRequired(f"{absent}; {exc}") from exc
        raise ChipInitError(str(exc)) from exc
    return BankedAlignment(
        k_samples, commissioned_sys_delay, delay, queue, shipped_label, changed
    )


def alignment_selection(env: Mapping[str, str] | None = None) -> str:
    """The audio-input profile this alignment pass ran under.

    The init unit does not load the operator's mode file, so read it here: the
    record it stamps is what tells a consumer whether the verdict describes the
    selection now in force.
    """

    source = os.environ if env is None else env
    path = source.get("JASPER_AEC_MODE_FILE") or str(DEFAULT_AEC_MODE_PATH)
    raw = parse_env_file(path).get("JASPER_AUDIO_INPUT_PROFILE", "")
    return normalize_audio_input_profile(raw, default="") or PROFILE_CUSTOM


def publish_alignment_record(
    health: chip_aec_health.AlignmentHealth | None,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Publish this run's verdict for jasper-aec-reconcile, or clear it.

    Best-effort: on a disclosed pass the profile is applied and the box is
    hearing by the time this runs, so a failed ``/run`` write must not turn a
    disclosure back into a park.  `main` calls it exactly once per run, so the
    record cannot outlive the pass it describes.
    """

    source = os.environ if env is None else env
    path = source.get("JASPER_AEC_ALIGNMENT_RECORD_FILE") or ALIGNMENT_RECORD_PATH
    try:
        if health is None:
            Path(path).unlink(missing_ok=True)
        else:
            atomic_write_text(path, health.to_shell())
    except OSError as exc:
        log_event(
            logger,
            "chip_aec_init.alignment_record_unpublished",
            path=path,
            reason=str(exc),
            level=logging.WARNING,
        )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s aec-init %(levelname)s %(message)s")
    corpus = _truthy("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED")
    mode = (
        "corpus"
        if corpus
        else "chip_aec"
        if _truthy(CHIP_AEC_ENABLED_ENV)
        else "lab_bypass"
    )
    dev = None
    selection = alignment_selection()
    record: chip_aec_health.AlignmentHealth | None = None
    try:
        from jasper.xvf import xvf_host

        dev = _find_device(xvf_host)
        card = xvf3800.alsa_card_name()
        if mode == "chip_aec":
            plan = xvf3800.chip_beam_plan_from_env(os.environ)
            if plan is None:
                raise ChipInitError(
                    "detected XVF has no validated production beam plan"
                )
            banked = resolve_banked_alignment(dev, plan, card=card)
            apply_profile(dev, plan, banked.sys_delay, card=card)
            record = chip_aec_health.alignment_health(
                chip_aec_health.APPLIED,
                selection=selection,
                shipped_label=banked.shipped_label,
                identity_diff=banked.identity_diff,
            )
            disclosure = (
                "" if record.status == chip_aec_health.STATUS_READY
                else record.reason
            )
            log_event(
                logger,
                "chip_aec_init",
                level=logging.WARNING if disclosure else logging.INFO,
                outcome=record.status,
                sys_delay=banked.sys_delay,
                commissioned_sys_delay=banked.commissioned_sys_delay,
                k_samples=banked.k_samples,
                queue_median=median_samples(banked.queue),
                queue_spread=max(banked.queue) - min(banked.queue),
                queue_samples=len(banked.queue),
                **({"reason": disclosure} if disclosure else {}),
            )
        elif mode == "corpus":
            plan = xvf3800.chip_beam_plan_from_env(os.environ)
            if plan is None:
                raise ChipInitError(
                    "detected XVF has no validated lab beam plan"
                )
            delay = int(os.environ.get("JASPER_AEC_CORPUS_CHIP_SYS_DELAY", "12"))
            apply_profile(dev, plan, delay, card=card)
            log_event(
                logger,
                "chip_aec_init",
                outcome="ready",
                mode="corpus",
                sys_delay=delay,
            )
        else:
            # Only explicit custom/lab routing reaches this path.
            apply_bypass_profile(dev, card=card)
            log_event(logger, "chip_aec_init", outcome="bypassed", mode=mode)
        return 0
    except OutputdEnvStale as exc:
        # Distinct from a fault: nothing is broken, the output declaration just
        # has not reached the running daemon yet. Keep it a separate exit code so
        # the reconciler's disposition (and /state) names the ordering problem
        # instead of sending a household at the commissioner.
        record = chip_aec_health.alignment_health(
            chip_aec_health.OUTPUTD_ENV_STALE, selection=selection
        )
        if dev is not None:
            _safe_bypass(dev)
        log_event(
            logger, "chip_aec_init", outcome="deferred",
            reason=str(exc),
            action=f"wait for {OUTPUTD_UNIT} to restart, then run jasper-aec-reconcile",
            level=logging.ERROR,
        )
        return OUTPUTD_ENV_STALE_EXIT
    except CommissionRequired as exc:
        record = chip_aec_health.alignment_health(
            chip_aec_health.COMMISSION_REQUIRED, selection=selection
        )
        if dev is not None:
            _safe_bypass(dev)
        log_event(
            logger, "chip_aec_init", outcome="parked",
            reason=str(exc), action="run jasper-aec-commission", level=logging.ERROR,
        )
        return COMMISSION_REQUIRED_EXIT
    except Exception as exc:  # noqa: BLE001
        record = chip_aec_health.alignment_health(
            chip_aec_health.REAPPLY_FAILED, selection=selection
        )
        if dev is not None:
            _safe_bypass(dev)
        log_event(
            logger, "chip_aec_init", outcome="failed", reason=str(exc),
            action="inspect jasper-aec-init and outputd", level=logging.ERROR,
        )
        return 1
    finally:
        # Only a chip-AEC pass reaches a verdict; a corpus/bypass run clears the
        # record rather than leaving the previous pass's standing.
        publish_alignment_record(record if mode == "chip_aec" else None)
        if dev is not None:
            try:
                if hasattr(dev, "close"):
                    dev.close()
                else:
                    dev.dev.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
