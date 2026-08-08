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
from typing import Any

from jasper import output_hardware
from jasper.audio_hardware import dac as dac_registry

# The declaration outputd loads through `EnvironmentFile=` (its runtime output
# contract: sink, backend, active width, final-edge format). Imported, never
# restated as a literal here: an unreadable path leaves the ordering guard below
# fail-OPEN, so a drifted copy would silently disable it with every test still
# green. jasper-audio-hardware-reconcile is the writer; the per-box override is
# the JASPER_OUTPUTD_ENV_FILE both reconcilers already honour.
# tests/test_aec_init.py pins this against jasper-outputd.service's own
# EnvironmentFile= line.
from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_ENV_PATH
from jasper.chip_aec_alignment import (
    AlignmentIdentity,
    load_artifact,
    median_samples,
    queue_window_is_stable,
    required_queue_samples,
    runtime_sys_delay,
)
from jasper.log_event import log_event
from jasper.mics import xvf3800
from jasper.route_latency.status_socket import OUTPUTD_STATUS_SOCKET, read_status_socket

logger = logging.getLogger("jasper.aec_init")
COMMISSION_REQUIRED_EXIT = 2
# Kept in step with the `init_status` branch in jasper-aec-reconcile's
# activate_managed_chip_aec by tests/test_aec_reconcile.py.
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
# The queue poll targets half the writer's own cadence, so no write's
# snd_pcm_delay reading is skipped, and is clamped at both ends.  The floor keeps
# a very fine mix period off a spin on the STATUS socket — such a box produces
# far more writes than a window needs, so skipping some of them costs nothing.
# The ceiling keeps a very coarse one from spending the budget unsampled.
MIN_QUEUE_POLL_INTERVAL_SEC = 0.01
MAX_QUEUE_POLL_INTERVAL_SEC = 0.25
# How long the reference queue has to hold still, independent of how many
# readings that takes.  Sample count alone bounds the median's precision but
# says nothing about drift: a queue walking steadily away from its start can
# hold a narrow spread across a short window and still be worthless to K, which
# is measured once and reused for months.  Two seconds is the span the
# pre-#2253 window covered (eight readings at its fixed 0.25 s poll), so the
# drift a fine-cadence box had to survive is unchanged — reading it faster now
# buys precision, not a shorter look.
QUEUE_MIN_WINDOW_SEC = 2.0
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


class CommissionRequired(ChipInitError):
    pass


class ChipProfileError(ChipInitError):
    pass


class OutputdEnvStale(ChipInitError):
    """The live outputd predates its own declaration, so STATUS is not current."""


class _SystemctlTimeout(RuntimeError):
    """`systemctl show` did not answer inside its own per-call budget."""


@dataclass(frozen=True)
class ReferenceSample:
    delay: int
    frames: int
    sequence: int
    counters: tuple[int, ...]


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


def validate_reference_status(
    status: Mapping[str, Any], *, expected_pcm: str
) -> ReferenceSample:
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
    mix = _mapping(status.get("mix"), "mix")
    return ReferenceSample(
        _integer(writer.get("snd_pcm_delay_frames"), "snd_pcm_delay_frames"),
        _integer(writer.get("frames_written"), "frames_written", positive=True),
        _integer(mix.get("reference_sequence"), "reference_sequence", positive=True),
        tuple(
            _integer(writer.get(name), name)
            for name in REFERENCE_WRITER_COUNTER_NAMES
        ),
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

    None means three things, all of which leave the guard inert:

    * The unit has never started this boot.  systemd prints an EMPTY value for
      that (probed on jts3/jts4, systemd 257) — quiet, because it is legitimate.
    * ``systemctl`` exited non-zero.  That is the unsupported-``--timestamp=unix``
      class (added in 247; Trixie ships 257, so it is theoretical).  WARN-logged.
    * The value was non-empty but did not parse.  WARN-logged.

    The last two are anomalies rather than states, so they are logged: an inert
    guard is otherwise invisible, and silence would look exactly like success.

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
    except OSError:
        # No systemctl at all: not a systemd host (tests, dev shells). Not an
        # anomaly worth a WARN on a box where the daemon could never run anyway.
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
    seconds = int(digits)
    # A literal zero is not what systemd 257 prints for never-run (it prints
    # empty, probed) — kept as defence against another version rendering it that
    # way, and treated as the same "nothing has run" case.
    return float(seconds) if seconds else None


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

    Binding (or rejecting) a commissioning identity against that is the debt the
    2026-08-05 final-edge-format PR-2 panel named.  Wait a bounded while for the
    queued restart to land, then fail rather than commission.

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


def mix_period_seconds(status: Mapping[str, Any]) -> float | None:
    """Return outputd's mix period in seconds, or None when STATUS cannot say.

    This is the chip-reference writer's own cadence: outputd hands it exactly one
    packet per mix period, so the period is both how often a fresh
    ``snd_pcm_delay`` reading exists and how much audio one blocking write
    carries.  Read from the same snapshot the queue readings come from, so it
    describes the box being sampled rather than any box's assumed geometry.
    """

    dac = status.get("dac")
    if not isinstance(dac, Mapping):
        return None
    period, rate = dac.get("period_frames"), dac.get("sample_rate")
    if type(period) is not int or type(rate) is not int or period < 1 or rate < 1:
        return None
    return period / rate


def queue_poll_interval(status: Mapping[str, Any], fallback: float) -> float:
    """Return the poll interval this box's mix cadence asks for."""

    period = mix_period_seconds(status)
    if period is None:
        return fallback
    return min(max(period / 2, MIN_QUEUE_POLL_INTERVAL_SEC), MAX_QUEUE_POLL_INTERVAL_SEC)


def collect_reference_queue(
    expected_pcm: str,
    *,
    socket_path: str = OUTPUTD_STATUS_SOCKET,
    interval: float = MAX_QUEUE_POLL_INTERVAL_SEC,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """Collect a run of per-write queue readings precise enough to carry K.

    The window grows until it has held still for `QUEUE_MIN_WINDOW_SEC` AND its
    median is estimated as precisely as the reference window's was.
    `required_queue_samples` owns the precision half, and both this acceptance
    and `runtime_sys_delay`'s re-validation read it, so there is one definition
    of a usable window.  A fine mix cadence meets the precision bar in eight
    readings and spends the rest of the window watching for drift; a coarse one,
    whose blocking writes sweep most of a burst, buys the same precision by
    sampling for a few seconds.  A window whose spread keeps growing satisfies
    neither and fails inside the budget with the numbers in the message: a
    reference queue that will not hold still cannot carry K.
    """

    # Both consumers of a live STATUS — this oneshot and jasper-aec-commission —
    # funnel through here, and the queue positions returned below are as
    # identity-critical as the dac.format build_identity reads out of the same
    # snapshot, so the ordering guard belongs on this one path.  It runs before
    # the poll so a stale verdict propagates as itself instead of being folded
    # into last_error by the loop's own ChipInitError handler.
    require_outputd_env_loaded()

    deadline = time.monotonic() + timeout
    accepted: list[ReferenceSample] = []
    opened = time.monotonic()
    last_error = "no STATUS response"
    while time.monotonic() < deadline:
        fresh = True
        try:
            status = read_status_socket(socket_path)
            interval = queue_poll_interval(status, interval)
            sample = validate_reference_status(status, expected_pcm=expected_pcm)
            if accepted:
                if sample.counters != accepted[0].counters:
                    raise ChipInitError("chip-reference writer error counters moved")
                if (
                    sample.frames < accepted[-1].frames
                    or sample.sequence < accepted[-1].sequence
                ):
                    raise ChipInitError("chip-reference writer did not progress cleanly")
                # A repeat of the write already recorded: on a coarse cadence the
                # poll runs at half the writer's period, so about every other read
                # lands on a write already in the window.  Not progress — and not
                # a stall either: the write and delay-sample ages validated above
                # are what fail a wedged writer, and they grow in wall time no
                # matter how fast this loop reads.
                fresh = sample.frames > accepted[-1].frames
        except (OSError, ValueError, ChipInitError) as exc:
            accepted.clear()
            last_error = str(exc)
        else:
            if fresh:
                if not accepted:
                    opened = time.monotonic()
                accepted.append(sample)
                delays = tuple(item.delay for item in accepted)
                held = time.monotonic() - opened
                if held >= QUEUE_MIN_WINDOW_SEC and queue_window_is_stable(delays):
                    log_event(
                        logger,
                        "chip_aec_init.reference_queue",
                        outcome="stable",
                        samples=len(delays),
                        spread=max(delays) - min(delays),
                        median=median_samples(delays),
                        held_sec=round(held, 2),
                        poll_interval_ms=round(interval * 1_000, 1),
                    )
                    return status, delays
                last_error = _queue_shortfall(delays, held)
        time.sleep(interval)
    raise ChipInitError(f"native chip-reference writer not ready: {last_error}")


def _queue_shortfall(delays: Sequence[int], held: float) -> str:
    spread = max(delays) - min(delays)
    return (
        f"chip-reference queue spread {spread} needs "
        f"{required_queue_samples(spread)} readings over "
        f"{QUEUE_MIN_WINDOW_SEC:g}s, held {len(delays)} over {held:.1f}s"
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s aec-init %(levelname)s %(message)s")
    corpus = _truthy("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED")
    mode = (
        "corpus"
        if corpus
        else "chip_aec"
        if _truthy("JASPER_AEC_CHIP_AEC_ENABLED")
        else "lab_bypass"
    )
    dev = None
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
            try:
                artifact = load_artifact()
            except (OSError, ValueError) as exc:
                raise CommissionRequired(str(exc)) from exc
            status, queue = collect_reference_queue(native_reference_pcm(card))
            identity = build_identity(dev, plan, status)
            if artifact.identity != identity:
                raise CommissionRequired("artifact identity does not match live hardware")
            try:
                delay = runtime_sys_delay(artifact.k_samples, queue)
            except ValueError as exc:
                raise ChipInitError(str(exc)) from exc
            apply_profile(dev, plan, delay, card=card)
            log_event(
                logger,
                "chip_aec_init",
                outcome="ready",
                sys_delay=delay,
                k_samples=artifact.k_samples,
                queue_median=median_samples(queue),
                queue_spread=max(queue) - min(queue),
                queue_samples=len(queue),
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
        if dev is not None:
            _safe_bypass(dev)
        log_event(
            logger, "chip_aec_init", outcome="parked",
            reason=str(exc), action="run jasper-aec-commission", level=logging.ERROR,
        )
        return COMMISSION_REQUIRED_EXIT
    except Exception as exc:  # noqa: BLE001
        if dev is not None:
            _safe_bypass(dev)
        log_event(
            logger, "chip_aec_init", outcome="failed", reason=str(exc),
            action="inspect jasper-aec-init and outputd", level=logging.ERROR,
        )
        return 1
    finally:
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
