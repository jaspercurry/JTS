# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the shared-memory ring and the lanes that feed it.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime_camilla`` -> ``_fanin`` -> ``_outputd`` -> ``_ring``, so this
module is the last link and may import from all three.

Closed vocabulary for this module's `CheckResult.reason`: one snake_case
constant per distinct decision branch of the checks below, its value unique
across the doctor and prefixed by the check that emits it. `detail` stays the
human sentence (free to reword); `reason` is what tests and self-healing
consumers pin instead (ADR-0233 rule 3).

A branch that formed NO verdict — subsystem not installed, not applicable to
this box, or the evidence source unreachable so nothing was observed — is
`skipped` with a reason, never `ok`. An `ok` reason means an actual verdict a
consumer would branch on (a feature the box turned off, a floor that is
deliberately not renderable).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ... import ring_assets
from ...audio_hardware.dac import latency_floor_for
from ...fanin_coupling import RING_SLOT_FRAMES
from ...output_hardware import active_dac_profile_id
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _run
from .audio_runtime_camilla import _camilla_statefile
from .audio_runtime_fanin import _requires_roleful_graph
from .audio_runtime_outputd import _outputd_reconciled_env

# Aliases of the ring_assets SSOT; tests monkeypatch these names.
_JTS_RING_ALSA_PLUGIN_DIR = ring_assets.RING_ALSA_PLUGIN_DIR
_JTS_RING_IOPLUG_SO = ring_assets.RING_IOPLUG_SO
_JTS_RING_CONF_D = ring_assets.RING_CONF_D
_JTS_RING_SHM_DIR = ring_assets.RING_SHM_DIR
# Every PCM the ring conf.d defines, with the tool that probes its direction and
# the ring file it names. Names and filenames come from ``jasper.ring_assets``;
# only the probe TOOL (which of arecord/aplay opens this direction) is local.
_JTS_RING_PCMS = (
    (ring_assets.RING_A_CONF_PCM, "arecord", os.path.basename(ring_assets.RING_A_PROGRAM_FILE)),
    (ring_assets.RING_B_CONF_PCM, "aplay", os.path.basename(ring_assets.RING_B_CONTENT_FILE)),
    (
        ring_assets.RING_ACTIVE_CONF_PCM,
        "aplay",
        os.path.basename(ring_assets.RING_ACTIVE_CONTENT_FILE),
    ),
)
assert tuple(name for name, _tool, _ring in _JTS_RING_PCMS) == ring_assets.RING_CONF_PCMS

REASON_SPLIT_BONDED_RETURN_RING = "split_bonded_return_ring"
REASON_SPLIT_MARKER_CONTRADICTED = "split_marker_contradicted"
REASON_SPLIT_GROUPED_DAC_CONTENT_LANE = "split_grouped_dac_content_lane"
REASON_SPLIT_RING_UNCONSUMED = "split_ring_unconsumed"
REASON_SPLIT_RING_UNFED = "split_ring_unfed"

REASON_RING_PATH_NOT_CENTRAL_RING = "ring_path_not_central_ring"
REASON_RING_PATH_LAGS_MARKER = "ring_path_lags_marker"

REASON_RING_ASSET_MISSING = "ring_asset_missing"
REASON_RING_OPEN_PROBE_SKIPPED = "ring_open_probe_skipped"
REASON_RING_IOPLUG_UNOPENABLE = "ring_ioplug_unopenable"
REASON_RING_IOPLUG_ABSENT = "ring_ioplug_absent"
REASON_RING_IOPLUG_WIRE_UNSUPPORTED = "ring_ioplug_wire_unsupported"
REASON_RING_IOPLUG_UNVOUCHED = "ring_ioplug_unvouched"
REASON_RING_IOPLUG_UNREADABLE = "ring_ioplug_unreadable"
REASON_RING_IOPLUG_STALE = "ring_ioplug_stale"

REASON_WRITER_LOCK_NO_PROC = "writer_lock_no_proc"
REASON_WRITER_LOCK_TWO_WRITERS = "writer_lock_two_writers"
REASON_WRITER_LOCK_ORPHANED = "writer_lock_orphaned"
REASON_WRITER_LOCK_PROC_UNREADABLE = "writer_lock_proc_unreadable"

REASON_RING_READER_STALLED = "ring_reader_stalled"
REASON_RING_READER_NO_LIVE_RING = "ring_reader_no_live_ring"
REASON_RING_READER_STALL_DROPS = "ring_reader_stall_drops"

REASON_RING_GEOMETRY_MODULES_UNAVAILABLE = "ring_geometry_modules_unavailable"
REASON_RING_SLOTS_ENV_INVALID = "ring_slots_env_invalid"
REASON_RING_CONF_SLOTS_INDETERMINATE = "ring_conf_slots_indeterminate"
REASON_RING_SLOTS_ENV_CONF_MISMATCH = "ring_slots_env_conf_mismatch"
REASON_RING_HEADER_ABSENT = "ring_header_absent"
REASON_RING_HEADER_CONF_MISMATCH = "ring_header_conf_mismatch"

REASON_RING_FLOOR_NO_ACTIVE_DAC = "ring_floor_no_active_dac"
REASON_RING_FLOOR_NOT_DECLARED = "ring_floor_not_declared"
REASON_RING_FLOOR_NOT_RENDERABLE = "ring_floor_not_renderable"
REASON_RING_FLOOR_CONF_PERIOD_INDETERMINATE = "ring_floor_conf_period_indeterminate"
REASON_RING_FLOOR_UNRENDERED = "ring_floor_unrendered"
REASON_RING_FLOOR_RENDERED = "ring_floor_rendered"

REASON_RENDERER_LANES_UNARMED = "renderer_lanes_unarmed"
REASON_RENDERER_LANES_STATUS_UNREADABLE = "renderer_lanes_status_unreadable"
REASON_RENDERER_LANES_STATUS_NO_INPUTS = "renderer_lanes_status_no_inputs"
REASON_RENDERER_LANE_UNKNOWN_TO_FANIN = "renderer_lane_unknown_to_fanin"
REASON_RENDERER_LANE_SOURCE_NOT_RING = "renderer_lane_source_not_ring"
REASON_RENDERER_LANE_RING_BLOCK_MISSING = "renderer_lane_ring_block_missing"
REASON_RENDERER_LANE_DETACHED = "renderer_lane_detached"
REASON_RENDERER_LANE_NEVER_FED = "renderer_lane_never_fed"

REASON_TRANSPORT_PARK_EVIDENCE_UNAVAILABLE = "transport_park_evidence_unavailable"
REASON_TRANSPORT_ENDPOINT_UNPROVEN = "transport_endpoint_unproven"
REASON_TRANSPORT_CONVERGE_REFUSED = "transport_converge_refused"
REASON_TRANSPORT_ENDPOINT_ARMED_WITHOUT_ACTIVE_MODE = (
    "transport_endpoint_armed_without_active_mode"
)
REASON_TRANSPORT_TOPOLOGY_UNCLASSIFIED = "transport_topology_unclassified"
REASON_TRANSPORT_PARKED = "transport_parked"

REASON_RECONCILE_IN_FLIGHT = "reconcile_in_flight"


def _jts_ring_path_for(pcm: str) -> str | None:
    """The SHM ring-file path a given inert PCM's open probe would create,
    or None if the PCM name is not one of ours. The basenames in _JTS_RING_PCMS
    mirror deploy/alsa/conf.d/60-jts-ring.conf's `path` values."""
    for name, _tool, ring_basename in _JTS_RING_PCMS:
        if name == pcm:
            return os.path.join(_JTS_RING_SHM_DIR, ring_basename)
    return None


def _jts_ring_probe_wire(pcm: str) -> tuple[int, str] | None:
    """``(channels, format)`` the open-probe must ask this PCM for, or ``None``
    when this conf.d block's wire is indeterminate (unreadable file, missing
    block, or a torn declaration — nothing safe to ask ALSA for).

    From the conf.d block itself, NOT from
    :func:`~jasper.fanin_coupling.resolve_ring_wire`: the ioplug advertises
    exactly what the file on disk declares as its hw_params constraint, and conf
    rendering and ring coupling are independently gated, so a box can carry a
    per-box-rendered conf.d while sitting coupling-inert (or the reverse). An
    absent ``format``/``channels`` key means the ioplug default, which both
    parsers encode, so a never-rendered file answers correctly too. The ring PCMs
    can legitimately differ on channels, hence the per-PCM lookup.
    """
    channels = ring_assets.ring_conf_channels(pcm, _JTS_RING_CONF_D)
    sample_format = ring_assets.ring_conf_format(pcm, _JTS_RING_CONF_D)
    if channels is None or sample_format is None:
        return None
    return channels, sample_format


def _jts_ring_probeable_pcms() -> list[tuple[str, str]]:
    """The ``(pcm, tool)`` pairs an open-probe may safely touch right now.

    EMPTY unless ``jasper-fanin`` is inactive: fan-in is Ring A's only writer, so
    a box whose fan-in is down is carrying audio through no ring. Within that,
    only PCMs whose ring FILE is absent. That is a GATE, not a guarantee — the
    ``exists`` check races a daemon restarting underneath it — but a probe that
    loses the race still unlinks only what it created, and the ioplug's SPSC
    guard refuses it.
    """
    # Active, or unknown (no systemctl on this host): fan-in may be writing
    # Ring A, so nothing is probeable.
    if evidence.unit_active("jasper-fanin.service") is not False:
        return []
    probeable = []
    for pcm, tool, _ring_basename in _JTS_RING_PCMS:
        ring_path = _jts_ring_path_for(pcm)
        if ring_path and not os.path.exists(ring_path):
            probeable.append((pcm, tool))
    return probeable


def _jts_ring_pcm_resolves(pcm: str, tool: str) -> tuple[bool, str]:
    """Open-probe one inert jts_ring PCM. Success means ALSA resolved the
    conf.d name AND dlopen()ed the ioplug .so AND the writer-dead/no-reader
    silence path terminated. A 1-second probe against an absent ring is
    safe: the ioplug free-runs (playback) or emits timer-paced silence
    (capture) rather than blocking.

    THE ONLY DETECTION for the -DPIC / arch-mismatch class (a structurally
    invalid .so that passes presence checks and ALSA cannot dlopen).

    Leaves no residue: the ioplug open path is create-or-attach
    (O_RDWR|O_CREAT|O_EXCL), so probing an ABSENT ring CREATES the ring file, and
    a doctor-created ring poisons the next arm (a valid-magic ring carrying the
    conf.d PLACEHOLDER geometry is a fail-closed open error; only magic-less
    files are reclaimed). So the ring path's existence is snapshotted before the
    probe and ONLY a file the probe itself created is unlinked.

    Returns (ok, detail). detail carries the tail of stderr on failure so a
    broken registration is legible, not just "probe failed".
    """
    if not shutil.which(tool):
        return False, f"{tool} not found"
    wire = _jts_ring_probe_wire(pcm)
    if wire is None:
        return False, (
            f"ring conf.d ({_JTS_RING_CONF_D}) has no readable pcm.{pcm} "
            "format/channels to probe with — indeterminate wire (unreadable "
            "or torn); redeploy to reinstall it"
        )
    channels, sample_format = wire
    ring_path = _jts_ring_path_for(pcm)
    pre_existed = ring_path is not None and os.path.exists(ring_path)
    # arecord -> /dev/null (discard captured silence); aplay -> /dev/zero
    # (feed silence in). 48 kHz / 1 s.
    sink = "/dev/null" if tool == "arecord" else "/dev/zero"
    # 4 s, not 6: up to three PCMs are probed in one row and a doctor row is cut
    # off at 15 s. 4 s still leaves 3 s of slack over a 1 s capture/playback.
    try:
        proc = _run(
            [tool, "-D", pcm, "-c", str(channels), "-r", "48000",
             "-f", sample_format, "-d", "1", sink],
            timeout=4.0,
        )
    except subprocess.TimeoutExpired:
        return False, "open probe hung (>4 s) — ioplug no-reader/no-writer path may be broken"
    finally:
        # Best-effort: a failure to unlink must not turn a clean probe into a
        # doctor error.
        if ring_path and not pre_existed and os.path.exists(ring_path):
            try:
                os.unlink(ring_path)
            except OSError:
                pass
    if proc.returncode == 0:
        return True, "resolved"
    err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if len(err) > 160:
        err = err[:157] + "..."
    return False, err or f"{tool} exit {proc.returncode}"


def _transport_park_snapshot() -> dict[str, Any]:
    """The park verdict this run, read once for the two checks that consume it."""
    from ...control import transport_park

    return evidence.get("transport_park", transport_park.snapshot)


def _grouped_dac_content_lane_parked() -> bool:
    """Is this box the bonded shape whose post-DSP hop is not a ring at all?

    Fail-soft to False: an unreadable topology must not silence a real split.
    The park check's own ``unavailable`` branch reports that read failure.
    """
    from ...control import transport_park

    try:
        state = _transport_park_snapshot()
    except Exception:  # noqa: BLE001 - a park read must never crash a sibling
        return False
    return any(
        park.get("park_class") == transport_park.PARK_GROUPED_DAC_CONTENT_LANE
        for park in (state.get("parks") or [])
    )


def _crossed_transport_pair(label: str, reason: str, stranded: str) -> CheckResult:
    """A crossed rung: a `warn` while a pass in flight explains it, the silence
    `fail` once none is.

    The ladder's OWN in-flight signal, not a clock: the reconcile entry lock is
    held for the whole pass, so a held lock is the pass itself saying it is
    between rungs.
    """
    from jasper.fanin.coupling_reconcile import reconcile_in_progress

    remedy = (
        "Converge the pair: sudo /opt/jasper/.venv/bin/"
        "jasper-fanin-coupling-reconcile shm_ring."
    )
    if reconcile_in_progress() is True:
        return CheckResult(
            label,
            "warn",
            f"{stranded} — a reconcile pass holds the coupling entry lock right "
            f"now, so an arm ladder still in flight explains it. {remedy}",
            reason=REASON_RECONCILE_IN_FLIGHT,
        )
    return CheckResult(
        label,
        "fail",
        f"CROSSED TRANSPORT PAIR: {stranded} — this speaker is SILENT while "
        f"every daemon looks healthy. {remedy}",
        speaker_silent=True,
        reason=reason,
    )


@doctor_check()
def check_content_transport_coherence() -> CheckResult:
    """The post-DSP hop's three ends must agree: graph, bridge, and ring path.

    One check over both rungs of the ring arm ladder: every disagreement
    between them is the same finding — the speaker emits nothing while every
    daemon looks healthy — under the same remedy.

    * the loaded CamillaDSP graph writes a post-DSP ring while
      ``JASPER_OUTPUTD_CONTENT_BRIDGE`` is not ``shm_ring``: nobody consumes
      the ring;
    * the bridge is ``shm_ring`` while the graph writes somewhere else: outputd
      waits on a ring CamillaDSP is not filling;
    * both name the central ring while ``JASPER_OUTPUTD_SHM_RING_PATH`` lags
      the endpoint marker. outputd enforces a biconditional — the active ring
      file may be read only by an armed active endpoint and vice versa — and
      bails at startup with ``RestartPreventExitStatus=78``, so that rung reads
      PERSISTED evidence: ``check_outputd_service`` returns the systemd failure
      first and never reaches the contradiction.

    KEYED ON THE BRIDGE, not on ``JASPER_FANIN_CAMILLA_COUPLING``: under
    ADR-0100 that file selects nothing, while the bridge
    (``rust/jasper-outputd/src/config.rs``) decides what outputd reads.

    TWO TERMS, NOT THREE, on the first two rungs: a ``writer_alive:false``
    conjunct would make them never fire. outputd publishes that reader-reported
    metric only inside its ``shm_ring`` block, which exists iff the bridge is
    ``shm_ring`` (``rust/jasper-outputd/src/state.rs``) — absent exactly when
    it would be needed.

    Out of scope: a box with NEITHER end on the ring (coherent; see
    :func:`check_ring_transport_park` and :func:`check_fanin_coupling`), and a
    bonded member in either round-trip spelling — the marker-armed one plays the
    bond off the return ring, the legacy FIFO one is
    :func:`check_ring_transport_park`'s named park.

    PER-RUNG STAND-DOWNS, not one gate over all three: the legacy-FIFO park is
    keyed on ``JASPER_OUTPUTD_DAC_CONTENT_FIFO`` alone, so a parked box can
    still carry a ring bridge whose path lags the marker — outputd refuses that
    pair at startup whatever the lane is doing. It stands the SPLIT rungs down
    (its ``direct`` bridge beside a stereo-ring graph reads as one) and leaves
    the path rung live.

    The ladder moves these keys one rung at a time, so a run taken inside one
    legitimately reads crossed; :func:`_crossed_transport_pair` tells that
    window from a wedge by the reconcile entry lock.
    """
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_OUTPUTD_ENV_PATH,
        output_endpoint_evidence_from_statefiles,
    )
    from jasper.env_file import read_value
    from jasper.fanin.coupling_reconcile import _outputd_ring_path_for
    from jasper.fanin_coupling import (
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_RING_PATH_ENV_VAR,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        dac_content_marker_contradicted,
        dac_content_ring_served,
        outputd_bridge_is_ring,
        resolve_outputd_ring_path,
    )

    label = "content transport coherence"
    # LAYERED, because a bonded box's grouping env carries the marker and the
    # unit reads that file last — the stand-downs have to see what outputd sees.
    outputd_env = _outputd_reconciled_env()
    if dac_content_ring_served(outputd_env):
        return CheckResult(
            label,
            "skipped",
            "bonded member on the dac-content return ring; its content source "
            "is the bond, not this box's post-DSP ring",
            reason=REASON_SPLIT_BONDED_RETURN_RING,
        )
    if dac_content_marker_contradicted(outputd_env):
        return CheckResult(
            label,
            "skipped",
            "dac-content marker declared beside a content bridge; see the "
            "transport-park check",
            reason=REASON_SPLIT_MARKER_CONTRADICTED,
        )
    # `(unset, = the ring)` rather than a bare `(unset)`: an undeclared bridge IS
    # the ring (config.rs), so a reader who saw only "unset" beside a ring graph
    # would think the pair disagreed when it agrees.
    bridge = (
        str(outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip()
        or "(unset, = the ring)"
    )
    # The bridge key alone: the marker stand-downs above already returned, so
    # `outputd_content_is_central_ring` has the same answer from here down.
    outputd_on_ring = outputd_bridge_is_ring(
        outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
    )

    # BOTH STATEFILES, first-recognized-endpoint-wins: an active leader keeps a
    # program-bake graph in the primary statefile and its real output endpoint in
    # camilla#2's, so a box whose primary names no registered endpoint while
    # camilla#2 names the ACTIVE ring must still be judged. The primary path
    # comes from the run's one statefile read so an operator's
    # `JASPER_CAMILLA_STATEFILE` override keeps working.
    endpoint_evidence = output_endpoint_evidence_from_statefiles(
        _camilla_statefile(), DEFAULT_CAMILLA2_STATEFILE_PATH
    )
    playback_device = (endpoint_evidence.devices or {}).get("playback_device")
    graph_on_ring = playback_device in (
        RING_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
    pair = (
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge}, loaded graph playback="
        f"{playback_device or '(none)'}"
    )
    if graph_on_ring != outputd_on_ring:
        # THE SPLIT RUNGS ONLY: the legacy-FIFO member runs the `direct` bridge
        # its writer no longer emits while its own graph still loads the stereo
        # ring, which reads as a split about a box `check_ring_transport_park`
        # already names. The path rung below is a different fact and stays live.
        if _grouped_dac_content_lane_parked():
            return CheckResult(
                label,
                "skipped",
                "grouped dac_content lane; see the transport-park check",
                reason=REASON_SPLIT_GROUPED_DAC_CONTENT_LANE,
            )
        if graph_on_ring:
            return _crossed_transport_pair(
                label,
                REASON_SPLIT_RING_UNCONSUMED,
                f"the loaded graph writes {playback_device} but "
                f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge}, which names no "
                "transport outputd can serve — it parks instead of reading, and "
                "nothing consumes the ring",
            )
        return _crossed_transport_pair(
            label,
            REASON_SPLIT_RING_UNFED,
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge} but the loaded graph "
            f"writes {playback_device or '(none)'}, so outputd waits on a ring "
            "CamillaDSP is not filling",
        )
    if not outputd_on_ring:
        return CheckResult(
            label, "ok", f"{pair}; no central ring path to read",
            reason=REASON_RING_PATH_NOT_CENTRAL_RING,
        )
    # The SUBJECT stays outputd.env's own text: the marker and the ring path are
    # single-writer keys of that file, and `_outputd_ring_path_for` is contracted
    # on one snapshot of the file being reconciled.
    try:
        outputd_text = Path(DEFAULT_OUTPUTD_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        outputd_text = ""
    carried = resolve_outputd_ring_path(
        read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR)
    )
    derived = _outputd_ring_path_for(outputd_text)
    if carried != derived:
        return _crossed_transport_pair(
            label,
            REASON_RING_PATH_LAGS_MARKER,
            f"{OUTPUTD_RING_PATH_ENV_VAR}={carried} but this box's endpoint "
            f"marker derives {derived}; outputd refuses that pair at startup "
            "(exit 78, no restart)",
        )
    return CheckResult(
        label, "ok", f"{pair}, {OUTPUTD_RING_PATH_ENV_VAR}={carried}"
    )


@doctor_check(exclusive_group="audio-probe")
def check_ring_platform_assets() -> CheckResult:
    """Verify the jts_ring transport platform assets are present.

    Three assets: the compiled ioplug .so, the conf.d PCM definitions
    (jasper.ring_assets.RING_CONF_PCMS), and the /dev/shm/jts-ring directory.
    Since ADR-0100 they are load-bearing on every box.

    Statuses:
      ok    — .so + conf.d + shm dir present.
              CAVEAT: presence passes on a STALE .so left by a failed rebuild —
              it is structurally valid, so it dlopens and registers.
              `check_ring_ioplug_provenance` separates the two.
      fail  — an asset is missing, or a present ioplug ALSA cannot load (the
              -DPIC / arch-mismatch class), which only the open-probe can tell
              apart from a healthy one.

    THE OPEN-PROBE RUNS ONLY WHERE IT CANNOT DISTURB ANYTHING —
    :func:`_jts_ring_probeable_pcms` decides and answers nothing on a box
    carrying audio.

    The "is the ring coherent + alive" verdict belongs to `check_fanin_coupling`
    and `check_ring_geometry_coherence`.
    """
    label = "ring platform"
    # Module-level constants (tests monkeypatch them) so the presence snapshot
    # honors a repointed path.
    presence = ring_assets.ring_asset_presence(
        plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR,
        conf_d=_JTS_RING_CONF_D,
        shm_dir=_JTS_RING_SHM_DIR,
    )
    missing = list(presence.missing())

    if missing:
        return CheckResult(
            label,
            "fail",
            "a ring-platform asset is missing: "
            + "; ".join(missing)
            + " — the ring is this box's only transport, so its graph cannot "
            "resolve the ring devices and nothing carries audio; redeploy "
            "(bash scripts/deploy-to-pi.sh) to rebuild them.",
            reason=REASON_RING_ASSET_MISSING,
        )

    present_detail = "ioplug + conf.d + /dev/shm/jts-ring present"
    probeable = _jts_ring_probeable_pcms()
    if not probeable:
        return CheckResult(
            label,
            "ok",
            f"{present_detail} (open-probe skipped — fan-in is running or a ring "
            "file is in use; see the 'fan-in coupling' and 'ring geometry' checks)",
            reason=REASON_RING_OPEN_PROBE_SKIPPED,
        )
    failures = []
    for pcm, tool in probeable:
        ok, detail = _jts_ring_pcm_resolves(pcm, tool)
        if not ok:
            failures.append(f"{pcm}: {detail}")
    if failures:
        return CheckResult(
            label,
            "fail",
            "the ring ioplug is installed but ALSA cannot open through it: "
            + "; ".join(failures)
            + " — a structurally invalid plugin (the -DPIC / arch-mismatch "
            "class) passes a presence check and still cannot carry audio; "
            "redeploy (bash scripts/deploy-to-pi.sh) to rebuild it.",
            reason=REASON_RING_IOPLUG_UNOPENABLE,
        )
    return CheckResult(
        label,
        "ok",
        f"{present_detail}; open-probe resolved "
        + ", ".join(pcm for pcm, _tool in probeable),
    )


def _resolved_ring_wire():
    """The ring wire an arm would render into the conf.d, or ``None``.

    The same two calls the arm's own capability gate makes
    (:func:`jasper.fanin.coupling_reconcile.ring_wire_caps_ready`). ``None`` when
    the box declares a wire neither language recognizes; that refusal is
    ``resolve_wire_for_gate``'s to report.
    """
    try:
        from ...fanin.ring_health import resolve_wire_for_gate

        wire, _problem = resolve_wire_for_gate(evidence.saved_topology_for_wire())
    except (ImportError, OSError):
        return None
    return wire


@doctor_check()
def check_ring_ioplug_provenance() -> CheckResult:
    """Is the INSTALLED ioplug the one the installer built, and what can it parse?

    ``check_ring_platform_assets`` reports presence and its open-probe passes on
    any structurally-valid plugin, so a STALE ``.so`` (the ioplug build degrades
    to a WARN and leaves the previous one installed) reads ``ok`` there. The
    installer records the sha and conf.d fields of the plugin it installed, and
    revokes that record on every path where it did NOT produce the installed
    file.

    THE VERDICT IS WEIGHED BY THE BOX'S OWN WIRE, because that decides whether an
    unvouched plugin costs anything:

    * a wire that renders no conf.d field beyond the ioplug's own defaults needs
      nothing from any installed plugin, so "cannot vouch" and "stale" are
      informational ``ok`` rows carrying their reason;
    * a wire that declares a non-default sample FORMAT is refused at the arm by
      ``ring_wire_caps_ready``, which is a ``fail``: a stale/mismatched ioplug
      otherwise presents as CamillaDSP crash-looping on ``-EINVAL`` at ``open()``
      against the ring, and the manual
      ``jasper-fanin-coupling-reconcile shm_ring`` remedy skips that gate.

    SCOPE: ``ring_wire_capabilities`` answers which keys the WIRE forces onto the
    conf.d, not which keys the conf.d on disk declares, so a box pinned narrow
    resolves an empty capability set while its rendered conf.d still carries a
    ``format`` line (#2597).

    Skips when the ``.so`` is absent: that is ``check_ring_platform_assets``'s
    missing-asset verdict.
    """
    label = "ring ioplug provenance"
    so_path = ring_assets.ring_ioplug_so_path(plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR)
    if not os.path.exists(so_path):
        return CheckResult(
            label, "skipped", f"{so_path} absent (see 'ring platform')",
            reason=REASON_RING_IOPLUG_ABSENT,
        )
    wire = _resolved_ring_wire()
    if wire is not None:
        support = ring_assets.ring_ioplug_wire_supported(
            wire, plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR
        )
        if not support.ok:
            return CheckResult(
                label,
                "fail",
                "the ring arm will be REFUSED on this box: "
                f"{support.detail}. Cost until then: a roleful box's content "
                "lane parks (ADR-0178), catching what would otherwise be a "
                "CamillaDSP crash-loop at open(). Command: bash "
                "scripts/deploy-to-pi.sh",
                reason=REASON_RING_IOPLUG_WIRE_UNSUPPORTED,
            )
    record = ring_assets.read_ring_ioplug_provenance()
    if not record.recorded:
        return CheckResult(
            label,
            "ok",
            f"{so_path} is installed but UNVOUCHED: no usable record at "
            f"{ring_assets.RING_IOPLUG_PROVENANCE}. Either this box has not been "
            "redeployed since the installer began recording, or a deploy revoked "
            "the record because its ioplug build failed — check the deploy "
            "transcript for a jts_ring ioplug build WARN. Redeploy (bash "
            "scripts/deploy-to-pi.sh) to rebuild and record.",
            reason=REASON_RING_IOPLUG_UNVOUCHED,
        )
    installed = ring_assets.ring_ioplug_so_sha256(
        plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR
    )
    if installed is None:
        return CheckResult(
            label, "skipped",
            f"{so_path} could not be read to compare against the record",
            reason=REASON_RING_IOPLUG_UNREADABLE,
        )
    if installed != record.sha256:
        return CheckResult(
            label,
            "ok",
            f"ioplug older than the installed plugin: {so_path} hashes "
            f"{installed[:12]}… but the installer recorded {record.sha256[:12]}…, "
            "so the plugin on disk is not the one "
            "the last successful install produced. The ioplug build degrades to a "
            "WARN and leaves the previous .so in place — redeploy and check the "
            "transcript for a jts_ring ioplug build failure.",
            reason=REASON_RING_IOPLUG_STALE,
        )
    caps = ", ".join(sorted(record.caps)) or "none"
    return CheckResult(
        label,
        "ok",
        f"{so_path} matches the installer's record (sha {record.sha256[:12]}…); "
        f"conf.d fields it can parse: [{caps}]",
    )


# How long to wait before CONFIRMING a suspected two-writer observation.
#
# MUST exceed the C ioplug's writer-lock acquisition budget
# (``JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS``, 500 ms): ``acquire_writer_lock``
# opens the lock file FIRST and only then spins on ``flock`` until that budget
# expires, so for up to that long a healthy box legitimately has TWO processes
# holding an fd on one ``.writer.lock``. Pinned against the header by
# ``tests/test_ring_slot_ceiling_pin.py``.
_WRITER_LOCK_CONFIRM_DELAY_SEC = 0.75
# Resolved at CALL time below, so a test can repoint it at a synthetic tree.
_PROC_ROOT = "/proc"


def _ring_writer_lock_holders(
    *,
    proc_root: str | None = None,
    shm_dir: str | None = None,
) -> tuple[dict[str, dict[int, bool]], int]:
    """Which live pids hold an fd on a ring ``.writer.lock``, by lock path.

    Returns ``({lock_path: {pid: target_was_unlinked}}, unreadable_pid_count)``.

    fds, NOT ``/proc/*/maps``: the Rust reader mmaps the same ring file
    ``PROT_READ|PROT_WRITE`` (``rust/jasper-ring/src/lib.rs`` ``mmap_fd``), so on
    every armed box ">1 mapper" is the healthy state. The WRITER LOCK is the
    discriminator — only the C ioplug's writer ever opens it; Rust takes the
    ``.open.lock`` transaction lock and never this one.

    Grouped by PATHNAME because that is the shape of the residual
    :func:`check_ring_writer_lock_exclusivity` documents: an orphaned incumbent
    and the fresh file that replaced it share one pathname across two inodes.
    ``/proc``'s ``" (deleted)"`` suffix names which half is the orphan.
    """
    root = ring_assets.RING_SHM_DIR if shm_dir is None else shm_dir
    procfs = _PROC_ROOT if proc_root is None else proc_root
    prefix = root.rstrip("/") + "/"
    holders: dict[str, dict[int, bool]] = {}
    unreadable = 0
    try:
        entries = os.listdir(procfs)
    except OSError:
        return holders, unreadable
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = os.path.join(procfs, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            # Non-root doctor runs see only their own processes. Counted so the
            # verdict can say it was partially blind instead of claiming clean.
            unreadable += 1
            continue
        except OSError:
            # ENOENT — the pid exited between the two listdirs; it holds nothing.
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            unlinked = target.endswith(" (deleted)")
            if unlinked:
                target = target[: -len(" (deleted)")]
            if not target.startswith(prefix):
                continue
            if not target.endswith(ring_assets.RING_WRITER_LOCK_SUFFIX):
                continue
            per_path = holders.setdefault(target, {})
            # One pid with several fds on one lock is still ONE writer; an
            # unlinked target anywhere in that pid's fds marks it orphaned.
            per_path[pid] = per_path.get(pid, False) or unlinked
    return holders, unreadable


@doctor_check()
def check_ring_writer_lock_exclusivity() -> CheckResult:
    """Do two live writers hold one ring's writer lock?

    An ``flock``'s identity is the PATHNAME, not the inode, so UNLINKING
    ``<ring>.writer.lock`` while a writer holds it voids exclusivity SILENTLY —
    two live writers proceed with no log line between them
    (``c/jts-ring-ioplug/jts_ring_shm.c``). Nothing stops an operator or a
    cleanup script from ``rm -rf``-ing the tmpfs directory under a live writer,
    and the grouping reconciler's active-leader arm DEPENDS on that lock as a
    safety signal (``jasper/multiroom/reconcile.py``).

    THE HEADER CANNOT ANSWER THIS: ``writer_pid`` is a SINGLE header slot (offset
    56, ``_Static_assert``-pinned), so a second writer's attach overwrites it.
    This reads the KERNEL's view — who holds an fd on the lock.

    Statuses:
      ok    — no lock path has more than one live holder (the normal state,
              including "no writer anywhere" on an unarmed box).
      skipped — no ``/proc`` on this host, or it was only partially readable so
              the sweep observed no complete answer.
      warn  — a holder's lock file has been UNLINKED out from under it (one
              writer, but exclusivity is already void: the next opener will
              create a fresh inode and will NOT be excluded).
      fail  — two or more live pids hold one ring's writer lock, confirmed on a
              second sample ``_WRITER_LOCK_CONFIRM_DELAY_SEC`` later so an
              ordinary create-or-attach race cannot masquerade as the defect.
    """
    label = "ring writer-lock exclusivity"
    if not os.path.isdir(_PROC_ROOT):
        return CheckResult(
            label,
            "skipped",
            f"no {_PROC_ROOT} on this host",
            reason=REASON_WRITER_LOCK_NO_PROC,
        )

    holders, unreadable = _ring_writer_lock_holders()
    suspects = {path: pids for path, pids in holders.items() if len(pids) > 1}
    if suspects:
        time.sleep(_WRITER_LOCK_CONFIRM_DELAY_SEC)
        confirmed_holders, confirm_unreadable = _ring_writer_lock_holders()
        unreadable = max(unreadable, confirm_unreadable)
        confirmed: dict[str, dict[int, bool]] = {}
        for path, pids in suspects.items():
            still = confirmed_holders.get(path, {})
            # Only pids present in BOTH samples count: a contender that gave up
            # inside the C budget is gone by now, a real second writer is not.
            both = {
                pid: still.get(pid, False) or held
                for pid, held in pids.items()
                if pid in still
            }
            if len(both) > 1:
                confirmed[path] = both
        if confirmed:
            parts = []
            for path, pids in sorted(confirmed.items()):
                who = ", ".join(
                    f"pid {pid}{' (lock file unlinked)' if unlinked else ''}"
                    for pid, unlinked in sorted(pids.items())
                )
                parts.append(f"{path}: {who}")
            return CheckResult(
                label,
                "fail",
                "TWO LIVE WRITERS on one ring — exclusivity is void and the "
                "ring's SPSC contract is broken: "
                + "; ".join(parts)
                + ". Most likely the lock file was unlinked while a writer held "
                "it — anything that rm -rf's /dev/shm/jts-ring voids "
                "exclusivity silently. Stop the extra writer, then re-arm the "
                "lane.",
                reason=REASON_WRITER_LOCK_TWO_WRITERS,
            )

    orphaned = [
        (path, pid)
        for path, pids in holders.items()
        for pid, unlinked in pids.items()
        if unlinked
    ]
    if orphaned:
        detail = "; ".join(f"{path}: pid {pid}" for path, pid in sorted(orphaned))
        return CheckResult(
            label,
            "warn",
            "a ring writer holds a lock file that has been UNLINKED, so "
            f"exclusivity is already void for the next opener — {detail}. "
            "Re-arm the lane to recreate the lock before a second writer "
            "attaches.",
            reason=REASON_WRITER_LOCK_ORPHANED,
        )
    if unreadable:
        return CheckResult(
            label,
            "skipped",
            f"could not read /proc/<pid>/fd for {unreadable} process(es), so "
            "this sweep was partially blind — run jasper-doctor as root for a "
            "complete answer.",
            reason=REASON_WRITER_LOCK_PROC_UNREADABLE,
        )
    held = sum(len(pids) for pids in holders.values())
    return CheckResult(
        label,
        "ok",
        f"{len(holders)} ring writer lock(s) held by {held} process(es); "
        "no ring has more than one live writer",
    )


@doctor_check()
def check_ring_reader_stall() -> CheckResult:
    """A ring being WRITTEN but not READ, judged from the SHARED HEADER.

    THE SHARED HEADER IS THE PRIMARY OBSERVER: every ring's writer is the C
    ioplug (counters are process-local fields printed at close) and its
    reader is blocked in ``writei`` during exactly this fault, so only the
    header is free to read. Ring A alone falls back to fan-in's own STATUS
    witness (below) when the header itself cannot be judged.

    THE CONJUNCTION: ``writer_heartbeat_ns`` FRESH while ``reader_heartbeat_ns``
    is STALE. Not ``read_seq``-flat — the writer advances ``read_seq`` on the
    absent reader's behalf at demotion, so a ``read_seq`` clause goes false
    exactly when the drops begin. See
    :class:`jasper.ring_assets.RingStallVerdict`.

    Judges every ring below and reports per-ring so an operator knows which
    daemon to look at. ``present=False`` keeps absent/idle rings silent, which
    covers the unarmed fleet.

    RING A ALSO CARRIES FAN-IN'S OWN WITNESS (issue #1524), read through the
    evidence memo's fan-in STATUS (``evidence.fanin_status()``) so this costs
    no second socket read of a daemon another check already asked this run:
    fan-in's ``output.ring.stall_active`` is the fallback for when the shared
    header cannot be judged at all (no coherent SHM header — fan-in
    restarting, ring cleared), the one thing the header-based judge above
    cannot see for itself; and fan-in's cumulative ``stuck_reader_drops`` /
    ``drop_no_reader`` counters ride along as Ring A detail. Those counters
    are cumulative SINCE FAN-IN START, so they are detail, not a verdict on
    their own — an episode the reader already recovered from must not stay
    red until the next fan-in restart.

    Returns:
      - ok when no ring is stalled, carrying `ring_reader_stall_drops` when
        Ring A's counters are non-zero from an episode that has since
        recovered
      - skipped on a box where no ring is being written and fan-in's STATUS
        carries no ring block either, so there is nothing to judge
      - warn naming each stalled ring, its two heartbeat ages (or, for Ring A
        with no coherent header, fan-in's STATUS witness), and the reader
        daemon to check. WARN not FAIL: the ring self-recovers the instant the
        reader resumes, and the household's remedy is the same either way.
    """
    from jasper.multiroom.grouping_ring import GROUPING_RING_FILE
    from jasper.ring_assets import (
        RING_A_PROGRAM_FILE,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONTENT_FILE,
        ring_stall_verdict,
    )

    name = "ring reader stall"
    ring_a_label = "Ring A (fan-in -> CamillaDSP)"
    other_rings = (
        ("Ring B (CamillaDSP -> outputd)", RING_B_CONTENT_FILE),
        ("ACTIVE ring (CamillaDSP -> outputd)", RING_ACTIVE_CONTENT_FILE),
        ("GROUPING ring (snapclient -> CamillaDSP)", GROUPING_RING_FILE),
    )
    stalled: list[str] = []
    judged: list[str] = []

    # Ring A: judged by its own header when coherent, else by fan-in's own
    # witness (issue #1524) — the same evidence memo key every other fan-in
    # check reads, so asking for it this pass costs nothing extra.
    ring_a_verdict = ring_stall_verdict(RING_A_PROGRAM_FILE)
    ring_a_stalled = False
    status = evidence.fanin_status()
    output = status.payload.get("output") if status.payload else None
    fanin_ring = output.get("ring") if isinstance(output, dict) else None
    if ring_a_verdict.present:
        judged.append(ring_a_label)
        if ring_a_verdict.stalled:
            ring_a_stalled = True
            stalled.append(f"{ring_a_label}: {ring_a_verdict.detail}")
    elif isinstance(fanin_ring, dict):
        judged.append(ring_a_label)
        if bool(fanin_ring.get("stall_active")):
            ring_a_stalled = True
            stalled.append(
                f"{ring_a_label}: fan-in STATUS reports stall_active "
                "(no coherent ring header to judge)"
            )

    drops_detail = ""
    drops_reason = ""
    if isinstance(fanin_ring, dict) and not ring_a_stalled:
        stuck = int(fanin_ring.get("stuck_reader_drops") or 0)
        no_reader = int(fanin_ring.get("drop_no_reader") or 0)
        if stuck or no_reader:
            drops_detail = (
                f"; Ring A cumulative since fan-in start: "
                f"stuck_reader_drops={stuck}, drop_no_reader={no_reader}"
            )
            drops_reason = REASON_RING_READER_STALL_DROPS

    for label, path in other_rings:
        verdict = ring_stall_verdict(path)
        if not verdict.present:
            continue
        judged.append(label)
        if verdict.stalled:
            stalled.append(f"{label}: {verdict.detail}")

    if stalled:
        return CheckResult(name, "warn", "; ".join(stalled), reason=REASON_RING_READER_STALLED)
    if not judged:
        return CheckResult(
            name,
            "skipped",
            "no ring is being written (no armed ring on this box, or all idle)",
            reason=REASON_RING_READER_NO_LIVE_RING,
        )
    return CheckResult(
        name,
        "ok",
        f"reader keeping up on {len(judged)} live ring(s): {', '.join(judged)}"
        + drops_detail,
        reason=drops_reason,
    )


@doctor_check()
def check_ring_geometry_coherence() -> CheckResult:
    """Verify the Ring-A geometry agrees across env, conf.d, and on-disk.

    The geometry must match or CamillaDSP's ioplug attach fails hard (hw_params
    EINVAL → crash-loop → start-limit-hit). ``n_slots`` is checked on THREE axes,
    and the on-disk header is then compared on EVERY axis the attach compares:

      1. fan-in's resolved ``JASPER_FANIN_RING_SLOTS`` (jasper.env -> fanin.env
         systemd env chain, default 2)
      2. the conf.d ``jts_ring_capture`` ``n_slots`` (the ioplug attach authority)
      3. the on-disk ``program.ring`` header vs that conf.d block, on ``n_slots``,
         ``period_frames`` (the ring slot IS one outputd period, so a stale period
         fails the attach even with matching slots), ``sample_format`` and
         ``channels``

    UNCONDITIONAL since ADR-0100: Ring A is the only fan-in → CamillaDSP
    transport, so the graph opens it on every box. A mismatch is ``fail`` (the
    graph cannot run); a torn conf.d is ``warn``; a ring file with no header
    yet is ``ok`` — the ordinary state between fan-in restarts, and the next
    start creates it coherently.
    """
    label = "ring geometry"
    try:
        from jasper.fanin.ring_health import (
            FANIN_ENV_PATH,
            resolve_effective_fanin_ring_slots,
        )
        from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    except ImportError as e:  # pragma: no cover - always importable in prod
        return CheckResult(
            label,
            "skipped",
            f"ring modules unavailable: {e}",
            reason=REASON_RING_GEOMETRY_MODULES_UNAVAILABLE,
        )

    # Axis 1: fan-in's resolved env slot count (fail-loud on a bad value).
    try:
        fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        fanin_text = ""
    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.value is None:
        return CheckResult(
            label, "fail",
            f"effective {RING_SLOTS_ENV_VAR} from {resolution.source} is invalid: "
            f"{resolution.error}. fan-in will refuse to create Ring A, which is "
            "this box's only transport. Clear the stale value.",
            reason=REASON_RING_SLOTS_ENV_INVALID,
        )
    fanin_slots = resolution.value

    # Axis 2: the conf.d attach authority.
    conf_slots = ring_assets.ring_conf_n_slots(
        ring_assets.RING_A_CONF_PCM, _JTS_RING_CONF_D
    )
    if conf_slots is None:
        return CheckResult(
            label, "warn",
            f"conf.d ({_JTS_RING_CONF_D}) has no single n_slots for "
            f"pcm.{ring_assets.RING_A_CONF_PCM}; Ring A geometry is indeterminate — "
            "redeploy to reinstall the ring conf.d.",
            reason=REASON_RING_CONF_SLOTS_INDETERMINATE,
        )
    if fanin_slots != conf_slots:
        return CheckResult(
            label, "fail",
            f"Ring A slot mismatch: JASPER_FANIN_RING_SLOTS resolves to {fanin_slots} "
            f"but conf.d pcm.{ring_assets.RING_A_CONF_PCM} pins n_slots={conf_slots}. "
            "CamillaDSP's ioplug attach fails (hw_params EINVAL) "
            "and crash-loops. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it self-heals a stale env), "
            "or match the two values.",
            reason=REASON_RING_SLOTS_ENV_CONF_MISMATCH,
        )

    # Axis 3: the on-disk ring header (what the writer actually created).
    header = ring_assets.read_ring_header(ring_assets.RING_A_PROGRAM_FILE)
    if not header.valid:
        # No coherent on-disk ring yet (fan-in between restarts, or the ring was
        # cleared). env/conf.d agree, so the next writer create is coherent.
        return CheckResult(
            label, "ok",
            f"env + conf.d agree (n_slots={fanin_slots}) but {ring_assets.RING_A_PROGRAM_FILE} "
            "has no valid ring header yet (fan-in restarting / ring cleared). It "
            "will be created coherently on the next fan-in start.",
            reason=REASON_RING_HEADER_ABSENT,
        )
    # Every axis the ioplug attach compares — n_slots, period_frames,
    # sample_format and channels — through the SAME comparator the coupling
    # reconciler's stale-file guard and CONFIRM self-heal use, so the doctor
    # cannot call a file coherent that the reconciler is about to delete.
    verdict = ring_assets.ring_header_matches_conf(
        ring_assets.RING_A_PROGRAM_FILE,
        ring_assets.RING_A_CONF_PCM,
        conf_d=_JTS_RING_CONF_D,
    )
    if not verdict.ok:
        return CheckResult(
            label, "fail",
            f"{verdict.detail}. A stale ring file from a prior {verdict.axis} "
            "geometry blocks the ioplug attach. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
            "(it deletes a geometry-mismatched ring file before re-arming).",
            reason=REASON_RING_HEADER_CONF_MISMATCH,
        )
    return CheckResult(
        label, "ok",
        f"Ring A geometry coherent across env + conf.d + on-disk header "
        f"(n_slots={header.n_slots}, period_frames={header.period_frames}, "
        f"wire {header.sample_format_name}/{header.channels}ch); rate "
        f"{header.rate} Hz",
    )


@doctor_check()
def check_ring_conf_floor_render() -> CheckResult:
    """Verify the ring conf.d slot period matches the active DAC's declared floor.

    The ring slot IS one outputd DAC period, so a box whose DAC declares a
    :class:`~jasper.audio_hardware.dac.LatencyFloor` has its conf.d
    ``period_frames`` RENDERED from that floor by
    ``jasper-audio-hardware-reconcile``. The floor is read from the DAC registry
    (``latency_floor_for``), the period from the conf.d file itself.

    Statuses:
      skipped — no active DAC in the reconciler's record, so there is no
              declared floor to read.
      ok    — no declared floor (the shipped default stands, by rule); a
              declared floor that is not ``RING_SLOT_FRAMES`` (a product
              boundary, not drift — see below); or the conf.d already
              declares the floor's period.
      warn  — a renderable floor the conf.d has NOT been rendered to, or an
              indeterminate conf.d period. Never fail: an unrendered conf.d
              is inert until something arms shm_ring against it.

    An ``ok`` that leaves shm_ring's floor-optimal period unreached SAYS WHY
    (issue #2294).

    WHAT THOSE BRANCHES MAY AND MAY NOT CLAIM. They read the DECLARED floor,
    which is not outputd's RESOLVED period: the two diverge through
    ``JASPER_OUTPUTD_PERIOD_FRAMES`` in ``/etc/jasper/jasper.env``, which
    outranks the reconciler's floor-derived value, so a floorless box CAN ring.
    They may therefore say what is not RENDERED and name both routes to a ring;
    they must not say the ring is unavailable. Nothing preflights the
    conf.d/resolved-period divergence — it surfaces as outputd's hard ioplug
    ``open()`` error at attach.

    THE FLOOR IS NOT THE ONLY REASON. A ROLEFUL (active-crossover) box rings on
    the ACTIVE ring, which the unattended pass arms only for a box already
    carrying a proven graph (``ring_roleful_unattended_ready``). So on such a box
    a green period line means "the conf.d is ready", NOT "this box will ring",
    and every OK branch carries the rolefulness sentence. The WARN branches do
    not: each is a concrete render failure with one remedy.

    The product boundary: Ring A's slot size is fan-in's COMPILE-TIME
    ``RING_SLOT_FRAMES`` (``rust/jasper-ring/src/layout.rs``, no env override),
    so only a floor that EQUALS it is renderable. A DAC declaring any other floor
    never gets a rendered conf.d, and its outputd period/buffer geometry still
    applies through ``outputd.env``. Known limit, issue #2147, so ok not warn.
    """
    label = "ring conf floor"
    from ...audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES

    dac_id = active_dac_profile_id()
    if dac_id is None:
        return CheckResult(
            label, "skipped",
            "the output-hardware record names no active DAC, so there "
            "is no declared floor to render",
            reason=REASON_RING_FLOOR_NO_ACTIVE_DAC,
        )
    floor = latency_floor_for(dac_id)
    # Says "roleful box", NOT "commissioned box": step 1 accepts either roleful
    # boot graph — an applied baseline or the all-muted startup anchor a
    # mid-commission box boots from.
    roleful_note = (
        " This box is ROLEFUL (active crossover), so even a rendered conf.d "
        "does not make it ring on its own. The unattended default pass now "
        "does the whole thing by itself for a box carrying a proven graph — a "
        "hardware-fingerprint-matched applied baseline, or the all-muted "
        "startup anchor: it re-emits the graph at the ring endpoint, "
        "re-derives the hardware markers, and arms. So this box converges at "
        "the next boot, deploy or DAC hotplug with no command at all. A box "
        "carrying NEITHER graph still refuses, and its explicit ladder is "
        "`jasper-active-speaker baseline-reemit --endpoint ring`, then "
        "jasper-audio-hardware-reconcile, then "
        "`jasper-fanin-coupling-reconcile shm_ring` — that first step works on "
        "a mid-commission box, re-staging the all-muted startup anchor when no "
        "applied baseline is saved yet."
        if _requires_roleful_graph()
        else ""
    )
    if floor is None:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares no latency floor — {_JTS_RING_CONF_D} keeps its "
            "shipped default (no declared floor, nothing to render). Absent a "
            f"floor outputd resolves its packaged default period "
            f"{DEFAULT_OUTPUTD_PERIOD_FRAMES}, which is not the fixed ring slot "
            f"{RING_SLOT_FRAMES}, so shm_ring needs one of two things here: "
            f"JASPER_OUTPUTD_PERIOD_FRAMES={RING_SLOT_FRAMES} (plus a matching "
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES) in /etc/jasper/jasper.env, which "
            f"outranks this default; or a declared {RING_SLOT_FRAMES}-frame "
            "floor on this DAC, which is the codified form of the same values. "
            "Until either, this DAC's outputd period stays off the ring slot. "
            "(Issue #2147 would make the ring slot floor-derived and retire "
            "the question.)" + roleful_note,
            reason=REASON_RING_FLOOR_NOT_DECLARED,
        )
    if floor.outputd_period_frames != RING_SLOT_FRAMES:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares outputd period "
            f"{floor.outputd_period_frames} != the fixed ring transport slot "
            f"{RING_SLOT_FRAMES}, so its conf.d is deliberately not rendered "
            "until issue #2147 makes the ring slot floor-derived. shm_ring "
            f"needs outputd's RESOLVED period to be {RING_SLOT_FRAMES}, which "
            "on this DAC only an operator JASPER_OUTPUTD_PERIOD_FRAMES in "
            "/etc/jasper/jasper.env can produce; absent that override, this "
            "DAC's own floor still governs outputd's period/buffer geometry."
            + roleful_note,
            reason=REASON_RING_FLOOR_NOT_RENDERABLE,
        )
    conf_period = ring_assets.ring_conf_period_frames(_JTS_RING_CONF_D)
    if conf_period is None:
        return CheckResult(
            label,
            "warn",
            f"{dac_id} declares outputd_period_frames="
            f"{floor.outputd_period_frames} but {_JTS_RING_CONF_D} has no single "
            "period_frames (absent or torn); the ring slot geometry is "
            "indeterminate — redeploy (bash scripts/deploy-to-pi.sh) to "
            "reinstall it.",
            reason=REASON_RING_FLOOR_CONF_PERIOD_INDETERMINATE,
        )
    if conf_period != floor.outputd_period_frames:
        return CheckResult(
            label,
            "warn",
            f"{_JTS_RING_CONF_D} pins period_frames={conf_period} but {dac_id} "
            f"declares a latency floor of {floor.outputd_period_frames}; the ring "
            "slot is one outputd DAC period, so shm_ring cannot arm against this "
            "conf.d. Run: sudo systemctl start "
            "jasper-audio-hardware-reconcile.service (it renders the conf.d from "
            "the declared floor).",
            reason=REASON_RING_FLOOR_UNRENDERED,
        )
    return CheckResult(
        label,
        "ok",
        f"period_frames={conf_period} matches {dac_id}'s declared latency floor"
        + roleful_note,
        reason=REASON_RING_FLOOR_RENDERED,
    )


@doctor_check()
def check_renderer_ring_lanes() -> CheckResult:
    """Every ARMED renderer-ingress lane is attached, fed, and coherent.

    An unarmed box — the shipped fleet state — reports ``skipped``: there is
    no armed lane to judge.

    On an armed box it answers three questions, which have different remedies:

    1. **Is the lane ATTACHED?** A detached lane renders silence, and the
       ``detach_reason`` token names which remedy: ``geometry`` is a conf.d /
       fan-in shear, ``refused`` is almost always the renderer user's
       ``jts-ring`` membership or a missing ``UMask=0007``, ``unavailable`` is a
       ring that has not been created yet.
    2. **Is anything WRITING it?** ``writer_alive`` false on an attached lane is
       the ordinary "renderer is not playing" state and never fails on its own.
    3. **Has it EVER been written?** ``startup_empty_reads`` vs ``empty_reads``
       discriminates: all-startup empty reads means the renderer has never
       successfully opened its ring, which looks identical to "paused" on every
       other signal.

    Never FAILS on a paused renderer; WARNs on a lane that is detached or has
    never been fed.
    """
    from jasper import renderer_lanes as rl

    label_name = "renderer ring lanes"
    armed = rl.read_armed_labels()
    if not armed:
        return CheckResult(
            label_name,
            "skipped",
            "no renderer lane armed (fleet default)",
            reason=REASON_RENDERER_LANES_UNARMED,
        )

    read = evidence.fanin_status()
    if read.payload is None:
        return CheckResult(
            label_name,
            "skipped",
            f"{len(armed)} lane(s) armed ({', '.join(armed)}) but fan-in STATUS is "
            f"unreadable ({type(read.error).__name__}) — cannot confirm they are "
            "attached",
            reason=REASON_RENDERER_LANES_STATUS_UNREADABLE,
        )
    inputs = read.payload.get("inputs")
    if not isinstance(inputs, list):
        return CheckResult(
            label_name,
            "warn",
            f"{len(armed)} lane(s) armed ({', '.join(armed)}) but fan-in STATUS "
            "carries no inputs[] to judge — every shipped fan-in emits that key "
            "unconditionally (rust/jasper-fanin/src/state.rs), so the running "
            "binary is not the installed one: sudo systemctl restart jasper-fanin, "
            "then redeploy if it persists",
            reason=REASON_RENDERER_LANES_STATUS_NO_INPUTS,
        )
    by_label = {
        inp.get("label"): inp for inp in inputs if isinstance(inp, dict)
    }

    # `(reason, sentence)` pairs: the row reports every lane's sentence and
    # takes its machine reason from the first problem found.
    problems: list[tuple[str, str]] = []
    healthy: list[str] = []
    for lane_label in armed:
        entry = by_label.get(lane_label)
        if entry is None:
            problems.append((
                REASON_RENDERER_LANE_UNKNOWN_TO_FANIN,
                f"{lane_label}: armed but fan-in reports no such lane — restart "
                "jasper-fanin to pick up the lane map",
            ))
            continue
        if entry.get("source") != rl_source_ring():
            problems.append((
                REASON_RENDERER_LANE_SOURCE_NOT_RING,
                f"{lane_label}: armed but fan-in reports source="
                f"{entry.get('source')!r} — jasper-fanin has not restarted since "
                "the lane map changed",
            ))
            continue
        ring = entry.get("ring")
        if not isinstance(ring, dict):
            problems.append((
                REASON_RENDERER_LANE_RING_BLOCK_MISSING,
                f"{lane_label}: source=ring but no ring{{}} block",
            ))
            continue
        if not ring.get("attached"):
            reason = ring.get("detach_reason", "unknown")
            problems.append((
                REASON_RENDERER_LANE_DETACHED,
                f"{lane_label}: DETACHED (reason={reason}, retries="
                f"{ring.get('retries')}) — {_ring_detach_remedy(str(reason))}",
            ))
            continue
        # All-startup empty reads with no filled slot means the renderer has
        # never opened its ring.
        steady = ring.get("empty_reads") or 0
        startup = ring.get("startup_empty_reads") or 0
        frames = entry.get("frames_read") or 0
        if not frames and startup:
            if _ring_lane_is_on_demand(lane_label):
                # An on-demand lane (ephemeral aplay writers) is fed only while a
                # measurement is playing, so armed-attached-never-fed is its
                # RESTING state.
                #
                # STATED RESIDUAL: a writer that can NEVER open its ring (missing
                # jts-ring membership, geometry shear) has the SAME signature and
                # is reported healthy here. Accepted because correction playback
                # is operator-initiated and fails loudly at the point of use; the
                # detail below must keep the hint so a doctor reading never
                # implies the writer path was PROVEN.
                healthy.append(
                    f"{lane_label}(attached, on-demand, no measurement "
                    "played yet — a writer that cannot open looks identical "
                    "here; run a measurement to confirm)"
                )
                continue
            problems.append((
                REASON_RENDERER_LANE_NEVER_FED,
                f"{lane_label}: attached but NEVER FED (startup_empty_reads="
                f"{startup}, frames_read=0) — the renderer has not opened its "
                f"ring. Check that {_ring_lane_unit(lane_label)} restarted after "
                "the arm, and that its ALSA device resolves",
            ))
            continue
        healthy.append(
            f"{lane_label}(writer_alive={ring.get('writer_alive')}, "
            f"occupancy={ring.get('occupancy')}, empty_reads={steady}, "
            f"epoch_resets={ring.get('epoch_resets')})"
        )

    if problems:
        first_reason, _ = problems[0]
        return CheckResult(
            label_name,
            "warn",
            "; ".join(text for _reason, text in problems),
            reason=first_reason,
        )
    return CheckResult(label_name, "ok", "; ".join(healthy))


def rl_source_ring() -> str:
    """The STATUS ``source`` token a ring lane publishes."""
    from jasper.fanin.status import FANIN_INPUT_SOURCE_RING

    return FANIN_INPUT_SOURCE_RING


def _ring_lane_is_on_demand(label: str) -> bool:
    """Whether this lane's writers are ephemeral spawns (no renderer unit).

    Unitless lanes are fed only while a measurement plays, so the never-fed
    WARN's daemon-renderer wiring diagnosis does not apply to them. Routing a
    lane here costs the residual stated at that call site: resting and
    broken-at-open are indistinguishable.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label(label)
    return lane is not None and lane.unit is None


def _ring_lane_unit(label: str) -> str:
    """The restartable thing a lane's remedy strings should name.

    Total by design: a remedy string must never interpolate ``None``.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label(label)
    if lane is None:
        return "the renderer"
    if lane.unit is None:
        return f"{lane.renderer} (spawn-time writers; nothing to restart)"
    return lane.unit


def _ring_detach_remedy(reason: str) -> str:
    """The remediation for each detach reason. One line each, actionable."""
    if reason == "geometry":
        from jasper import renderer_lanes as rl

        return (
            f"the on-disk ring header disagrees with {rl.RENDERER_LANES_CONF_D}; "
            "re-run `jasper-audio-config renderer-lanes --disarm <lane> --arm "
            "<lane>` (which clears a stale ring) or revert the fan-in geometry "
            "override"
        )
    if reason == "refused":
        from jasper import renderer_lanes as rl

        return (
            f"the ring could not be mapped — usually the renderer user missing "
            f"from group {rl.RING_GROUP!r}, or its unit missing UMask=0007 "
            f"(which leaves a new ring 0640, group-unwritable). Redeploy and "
            "restart the renderer"
        )
    if reason == "orphaned":
        return (
            "the ring at this path was REPLACED while fan-in held it open (an "
            "arm/disarm, or a geometry change, clears and recreates it). The "
            "lane re-latches onto the live file on its own within ~2 s, so a "
            "single sighting is self-healing; persistent means something is "
            "recreating the ring in a loop"
        )
    return (
        "the ring file does not exist yet — normal briefly at boot; persistent "
        "means the renderer has never opened its device"
    )


@doctor_check()
def check_ring_transport_park() -> CheckResult:
    """No topology this box declares is one the ring cannot serve (ADR-0178).

    ADR-0100 makes ``shm_ring`` the only central transport; ADR-0178 names the
    four shapes it cannot carry and the tracked issue each waits on. A parked box
    emits NOTHING and no automatic path recovers it, hence ``fail``. The
    classification, issue numbers and remedy text all come from
    ``jasper.control.transport_park``, the reader
    ``/state.resilience.transport_park`` and the household audio card also use.

    Three shapes land between ``ok`` and a park — the ADR-0184 coverage seam, a
    converge refusal, and ADR-0189's mirror of the seam. All three are operator
    signals their own reader declares to be neither a park nor a household
    claim, so each is an ``ok`` carrying its reason. ``unclassified`` stays a
    ``warn``: the ring demonstrably cannot serve that box.
    """
    label = "ring transport parks"

    state = _transport_park_snapshot()
    status = state.get("status")

    if status == "unavailable":
        return CheckResult(
            label,
            "skipped",
            "the saved output topology or outputd's env could not be read "
            f"({state.get('error')}) — a transport park cannot be ruled out.",
            reason=REASON_TRANSPORT_PARK_EVIDENCE_UNAVAILABLE,
        )

    if status == "ok":
        if state.get("unproven_endpoint"):
            # ADR-0184's coverage seam: a width resolves so no topology class
            # fires, and the box is not active-crossover so the endpoint class is
            # scoped out.
            return CheckResult(
                label,
                "ok",
                "the wide ring resolves a width for this box, but outputd's "
                "active-ring endpoint marker is not armed and this is not an "
                "active-crossover layout, so none of the four named parks "
                "describes it (ADR-0184). Unproven, not parked — nothing is "
                "claimed to the household. Report the saved layout "
                "(/sound/setup/) if sound is missing.",
                reason=REASON_TRANSPORT_ENDPOINT_UNPROVEN,
            )
        refusal = state.get("converge_refused")
        if refusal:
            # The marker IS armed and every topology class passed, so the box is
            # ring-ELIGIBLE and still going nowhere. The reason is the snapshot's
            # own sentence, carried verbatim.
            return CheckResult(
                label,
                "ok",
                f"the ring can serve this box, but {refusal}. Not parked — the "
                "graph it already had keeps playing, so nothing is claimed to "
                "the household. Re-emit the active-speaker baseline onto the "
                "ring endpoint if sound is missing.",
                reason=REASON_TRANSPORT_CONVERGE_REFUSED,
            )
        if state.get("endpoint_armed_without_active_modes"):
            # ADR-0189: the marker is armed on a layout that declares no active
            # mode. Composite sinks are excluded in the classifier, so reaching
            # here means reconfiguration lag or a genuine mismatch.
            return CheckResult(
                label,
                "ok",
                "outputd's active-ring endpoint marker is armed, but this "
                "layout declares no active-crossover mode, so nothing here "
                "should have armed it (ADR-0189). Not parked — whatever graph "
                "is loaded keeps playing. A reconcile in flight clears this on "
                "its next pass; if it persists, report the saved layout "
                "(/sound/setup/).",
                reason=REASON_TRANSPORT_ENDPOINT_ARMED_WITHOUT_ACTIVE_MODE,
            )
        return CheckResult(
            label, "ok", "this box is in none of the four named transport parks"
        )

    if status == "unclassified":
        return CheckResult(
            label,
            "warn",
            "this box's declared topology resolves no ring geometry of either "
            "kind, and none of the four named parks describes it — so it is "
            "neither servable by the single transport nor tracked by an issue "
            "yet. Report the saved layout (/sound/setup/) so it can be named.",
            reason=REASON_TRANSPORT_TOPOLOGY_UNCLASSIFIED,
        )

    parks = state.get("parks") or []
    named = []
    for park in parks:
        part = f"{park.get('park_class')} ({park.get('detail')})"
        issue = park.get("issue")
        if issue:
            part = f"{part}. TRACKED: {issue}"
        remedy = park.get("remedy")
        if remedy:
            part = f"{part}. REMEDY: {remedy}"
        named.append(part)

    return CheckResult(
        label,
        "fail",
        "PARKED — no ring serves this box, so it emits nothing: "
        + "; ".join(named),
        speaker_silent=True,
        reason=REASON_TRANSPORT_PARKED,
    )
