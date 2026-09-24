# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist speaker topology intent and the statefile proof stamps (ADR-0283)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import advisory_file_lock, atomic_write_text
from .log_event import log_event
from .output_hardware import load_state as load_output_hardware_state
from .output_topology import (
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    TopologyRouting,
    subwoofer_speaker_groups,
    topology_config_fingerprint,
    topology_hardware_from_state,
    unknown_output_hardware,
)
from .paths import OUTPUT_TOPOLOGY_PATH as DEFAULT_TOPOLOGY_PATH
from .speaker_layout import BASS_MANAGEMENT_CORNER_HZ_DEFAULT
from .transition_log import TransitionLog

logger = logging.getLogger(__name__)

OUTPUT_TOPOLOGY_LOCK_TIMEOUT_SEC = 15.0


def new_topology_draft(
    *,
    topology_id: str = "default",
    name: str = "Speaker outputs",
    hardware: OutputHardware | None = None,
) -> OutputTopology:
    if hardware is None:
        observed = load_output_hardware_state()
        if observed is not None and observed.physical_output_count > 0:
            try:
                hardware = OutputHardware.from_mapping(
                    topology_hardware_from_state(observed)
                )
            except OutputTopologyError:
                log_event(
                    logger, "output_topology.observed_hardware_invalid",
                    level=logging.WARNING, profile_id=observed.profile_id,
                )
    return OutputTopology(
        topology_id=topology_id,
        name=name,
        hardware=hardware or unknown_output_hardware(),
        speaker_groups=(),
        routing=TopologyRouting(),
    )


def topology_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_TOPOLOGY_PATH")
        or DEFAULT_TOPOLOGY_PATH
    )


def topology_lock_path(path: str | Path | None = None) -> Path:
    """Return the process-shared writer lock next to the topology artifact."""

    target = topology_path(path)
    return target.with_name(f".{target.name}.lock")


# The two stamps jasper-camilla's ExecCondition= gate compares, both beside the
# CamillaDSP statefile and both written by the root convergence that owns it.
# Push, not pull (ADR-0226 rule 1): the fingerprints are computed by the Python
# that already holds the topology, and the gate is shell reading two files.
# Suffixes are duplicated in `deploy/bin/jasper-camilla-topology-gate` and
# pinned against it by tests/test_camilla_topology_gate_script.py.
STATEFILE_TOPOLOGY_STAMP_SUFFIX = ".topology"
STATEFILE_UNPROVED_STAMP_SUFFIX = ".topology.unproved"

# Digests compare only within one version. The gate compares two fingerprints of
# the SAME topology, so a code change to what `topology_config_fingerprint`
# hashes moves whichever stamp the new build wrote and not the other, and that
# would read as a wiring change nobody made. Bump with that projection: the
# literal pinned in tests/test_output_topology_store.py fails until you do. Goes with
# the gate (ADR-0283).
TOPOLOGY_STAMP_VERSION = 1


def topology_stamp_version(stamp: str) -> str:
    """Which projection minted one stamp.

    An unprefixed stamp is version 1's: the prefix landed while the projection
    it names was still 1, so those digests compare. Duplicated in
    ``deploy/bin/jasper-camilla-topology-gate``, which does the comparing.
    """

    version, separator, _digest = stamp.partition(":")
    return version if separator else "v1"


def topology_fingerprint_stamp(topology: OutputTopology) -> str:
    """The value both gate stamps carry: this topology, and what hashed it."""

    return f"v{TOPOLOGY_STAMP_VERSION}:{topology_config_fingerprint(topology)}"


def statefile_topology_stamp_path(statefile_path: str | Path) -> Path:
    """Where the fingerprint a written statefile was PROVED against lives."""

    target = Path(statefile_path)
    return target.with_name(target.name + STATEFILE_TOPOLOGY_STAMP_SUFFIX)


def statefile_unproved_stamp_path(statefile_path: str | Path) -> Path:
    """Where the fingerprint an UNFINISHED convergence was for lives.

    Written before a convergence attempts to prove a graph and removed only
    when it succeeds, so it survives a pass that returned a refusal AND a pass
    that was killed mid-flight. Absent means the sibling proof stamp is current.
    """

    target = Path(statefile_path)
    return target.with_name(target.name + STATEFILE_UNPROVED_STAMP_SUFFIX)


def read_topology_fingerprint_stamp(path: str | Path) -> str | None:
    """One stamped fingerprint, or ``None`` when absent or unreadable.

    Fail-soft on purpose: an unreadable stamp is UNKNOWN, and the gate treats
    unknown as "allow" — refusing on a permissions regression would take the
    speaker down for a fact nobody observed.
    """

    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def write_topology_fingerprint_stamp(path: str | Path, fingerprint: str) -> bool:
    """Publish one stamp atomically. False when it could not be written.

    A failed write is logged, not raised — every caller is on the boot path —
    and the line is the ONLY place that fact exists: a stamp nobody could write
    leaves the gate reading unknown, which allows.
    """

    try:
        atomic_write_text(Path(path), fingerprint + "\n", mode=0o644)
    except OSError as exc:
        log_event(
            logger,
            "camilla_topology_stamp.write_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True


def clear_topology_fingerprint_stamp(path: str | Path) -> bool:
    """Retire one stamp. False when it is still on disk.

    An unremovable stamp does NOT make the gate refuse: the unproved stamp left
    behind carries the same fingerprint the proof stamp just took, so the two
    compare EQUAL and the start is allowed. What is lost is the NEXT pass's
    evidence, not this boot's — hence the event.
    """

    try:
        Path(path).unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        log_event(
            logger,
            "camilla_topology_stamp.clear_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True


def stamp_statefile_topology(
    statefile_path: str | Path, topology: OutputTopology | None
) -> None:
    """Record which topology a WRITTEN statefile was PROVED against.

    Stamped after every apply that reached the statefile, not only when the
    pointer moves: the statefile may already name the right config while the
    stamp is missing (a box upgraded from a build before the stamp existed) or
    stale (a topology change that resolved to the same config).
    ``jasper-camilla-topology-gate`` compares it with the unproved sibling
    :func:`stamp_statefile_convergence` writes.

    Best effort: a stamp that cannot be written leaves the gate reading unknown,
    which allows. Never raises — this is on the boot path.
    """

    if topology is None:
        return
    write_topology_fingerprint_stamp(
        statefile_topology_stamp_path(statefile_path),
        topology_fingerprint_stamp(topology),
    )


def stamp_statefile_convergence(
    statefile_path: str | Path, topology: OutputTopology, *, proved: bool
) -> None:
    """Open, or close, one attempt to prove this topology's boot graph.

    ``proved=False`` at the TOP of the pass, as soon as the saved topology is
    read and before any decision is taken; ``proved=True`` only once a statefile
    write has succeeded. What is left behind names the topology whose graph
    nobody proved — a pass that refused, a pass that took some other early
    exit, and a pass killed mid-flight (the OOM killer included) at any point
    AFTER this stamp landed. A pass that died BEFORE it landed leaves nothing,
    and nothing is unknown, which allows.

    ``jasper-camilla-topology-gate`` refuses a CamillaDSP start when this stamp
    and the proof stamp are both present and DIFFERENT: the statefile then names
    a graph belonging to some other topology than the one a convergence was
    working on. Equal means the statefile already holds the right graph and the
    pass failed over something else, so the start goes ahead.

    Best effort, never raises: on the boot path, and a stamp nobody could write
    leaves the gate reading unknown, which allows.
    """

    stamp = statefile_unproved_stamp_path(statefile_path)
    if proved:
        clear_topology_fingerprint_stamp(stamp)
        return
    write_topology_fingerprint_stamp(stamp, topology_fingerprint_stamp(topology))


@dataclass(frozen=True)
class OutputTopologySnapshot:
    """One topology and the revision of the exact bytes that produced it."""

    topology: OutputTopology
    revision: str


def load_output_topology_snapshot(
    path: str | Path | None = None,
) -> OutputTopologySnapshot:
    """Load topology and revision from one immutable byte snapshot."""

    target = topology_path(path)
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return OutputTopologySnapshot(new_topology_draft(), "missing")
    except OSError as exc:
        raise OutputTopologyError(
            f"could not read output topology {target}: {exc}"
        ) from exc
    revision = "sha256:" + hashlib.sha256(data).hexdigest()
    try:
        raw = json.loads(data.decode("utf-8"))
        topology = OutputTopology.from_mapping(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # UnicodeDecodeError is not an OSError; SD-card bit rot can produce it.
        raise OutputTopologyError(
            f"output topology {target} is not valid JSON: {exc}"
        ) from exc
    except ValueError as exc:
        raise OutputTopologyError(
            f"output topology {target} is invalid: {exc}"
        ) from exc
    return OutputTopologySnapshot(topology, revision)


class OutputTopologyMutation:
    """One admitted read-modify-write transaction for saved topology intent."""

    def __init__(self, target: Path) -> None:
        self.target = target

    def snapshot(self) -> OutputTopologySnapshot:
        """Read topology and revision from one immutable byte snapshot."""

        return load_output_topology_snapshot(self.target)

    def save(self, topology: OutputTopology) -> str:
        """Publish one topology and return its precomputed byte revision."""

        return save_output_topology(topology, self.target)


@contextmanager
def output_topology_mutation(
    path: str | Path | None = None,
    *,
    timeout_sec: float = OUTPUT_TOPOLOGY_LOCK_TIMEOUT_SEC,
):
    """Serialize a bounded topology mutation across threads and processes."""

    target = topology_path(path)
    with advisory_file_lock(
        topology_lock_path(target),
        timeout_sec=timeout_sec,
    ):
        yield OutputTopologyMutation(target)


def load_output_topology_strict(path: str | Path | None = None) -> OutputTopology:
    """Load persisted topology for safety-authorizing paths.

    A missing topology means "not configured yet" and remains an empty draft.
    A corrupt or unreadable topology is different: callers that may authorize a
    runtime graph must fail closed instead of silently treating it as no saved
    roleful/protected outputs.
    """
    return load_output_topology_snapshot(path).topology


# A corrupt or unreadable topology is a persistent *state*, not a per-call
# event, and `load_output_topology` is called on a steady cadence inside
# long-lived daemons (jasper-control's audio_health route sampler, every 60 s).
# Unguarded that is ~1,440 identical WARN lines/day drowning the journal in an
# already-degraded state (#2140). So log the transitions — into failure and
# back out — plus a reminder slow enough to stay readable and frequent enough
# that a persistent failure is never silent. A short-lived process (doctor, a
# wizard request) starts with empty state and always logs its first failure.
LOAD_FAILURE_REMINDER_SEC = 3600.0
# Bounded because this lives for the process's lifetime. Production resolves
# one path; the cap only matters for callers that pass explicit paths.
_LOAD_FAILURE_STATE_MAX = 8


def _now() -> float:
    """Wrapped `time.monotonic` so tests can drive the reminder window."""
    return time.monotonic()


# The shared transition-or-reminder gate (jasper.transition_log), also consumed
# by the crossover level-run poller. The clock is looked up through this module
# so a test may monkeypatch `_now`.
_load_failures = TransitionLog(
    reminder_sec=LOAD_FAILURE_REMINDER_SEC,
    max_keys=_LOAD_FAILURE_STATE_MAX,
    clock=lambda: _now(),
)


def _load_failure_is_loggable(target: Path, signature: str) -> bool:
    """Whether this failure is a transition or a due reminder, not a repeat."""

    return _load_failures.should_log(str(target), signature)


def _load_failure_cleared(target: Path) -> bool:
    """Whether ``target`` just recovered from a failure that was logged."""

    return _load_failures.cleared(str(target))


def load_output_topology(path: str | Path | None = None) -> OutputTopology:
    """Load persisted topology, failing soft to a detected empty draft."""

    target = topology_path(path)
    try:
        topology = load_output_topology_strict(target)
    except OutputTopologyError as exc:
        if _load_failure_is_loggable(target, f"{type(exc).__name__}:{exc}"):
            log_event(
                logger, "output_topology.load_failed", level=logging.WARNING,
                path=str(target), error=type(exc).__name__, detail=str(exc),
                repeat_suppression_sec=int(LOAD_FAILURE_REMINDER_SEC),
            )
        return new_topology_draft()
    if _load_failure_cleared(target):
        log_event(logger, "output_topology.load_recovered", path=str(target))
    return topology


def save_output_topology(
    topology: OutputTopology,
    path: str | Path | None = None,
) -> str:
    """Persist topology atomically and return the exact published revision."""

    target = topology_path(path)
    data = json.dumps(topology.to_dict(), indent=2, sort_keys=True) + "\n"
    # /var/lib/jasper is group jasper but NOT setgid, so a root-run recovery
    # write must publish under the directory's group for the non-root management
    # daemons.
    atomic_write_text(target, data, mode=0o640, durable=True)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def bass_management_corner_hz() -> float | None:
    """This speaker's live bass-management crossover corner (Hz), or ``None``
    when it declares no subwoofer.

    Fail-soft through :func:`load_output_topology`: an unreadable topology
    resolves to "no subwoofer", never a raise, because a display and a room
    correction must both survive a momentarily unreadable state file. A
    subwoofer group with no explicit per-channel corner runs at the default
    corner the active-speaker emitter falls back to.
    """

    groups = subwoofer_speaker_groups(load_output_topology())
    if not groups:
        return None
    for group in groups:
        for channel in group.channels:
            if channel.crossover_fc_hz is not None:
                return float(channel.crossover_fc_hz)
    return float(BASS_MANAGEMENT_CORNER_HZ_DEFAULT)
