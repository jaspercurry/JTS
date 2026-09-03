# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import errno
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from ...env_load import parse_env_text
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _camilla_block_field,
    _parse_systemd_environment,
    _run,
)

_OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"


def _read_outputd_status(
    socket_path: str = _OUTPUTD_STATUS_SOCKET,
) -> dict | None:
    """Best-effort local outputd STATUS read for grouping verdicts."""
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(socket_path)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _devices_rate_adjust_from_text(text: str) -> bool | None:
    """``devices.enable_rate_adjust`` from a CamillaDSP config — True/False, or
    None when absent / unparseable. Reads via the shared
    :func:`_camilla_block_field` scanner."""
    value = _camilla_block_field(text, "devices", "enable_rate_adjust")
    if value is None:
        return None
    value = value.lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False
    return None

@doctor_check(order=71, group="grouping")
def check_grouping() -> CheckResult:
    """Verify /var/lib/jasper/grouping.env is consistent AND actually up.

    Off by default; the user opts in via the grouping web wizard. We
    return `ok` for OFF (deliberate). For ON we `warn` on two failure
    classes:
      - **config invalid** — the fail-LOUD "enabled but broken" state
        that jasper.multiroom.config carries on GroupingConfig.error;
      - **runtime degraded** — configured-valid but a snap unit the
        reconciler's plan wants running is not `active` (e.g. a follower
        whose snapclient can't reach its leader, a leader whose snapserver
        is down), OR a bonded leader whose active CamillaDSP config does not
        write the snapserver pipe. This is §7's "make it visible, not
        invisible": a green config with silent breakage underneath is
        exactly what we refuse to show. Runtime health is derived by the
        same pure `derive_grouping_runtime` the /state surface uses."""
    from ...multiroom.config import load_config as _load_grouping_config
    from ...multiroom.state import derive_grouping_runtime

    label = "grouping: mode"
    cfg = _load_grouping_config()
    if not cfg.enabled:
        return CheckResult(label, "ok", "single-speaker (grouping off)")
    if cfg.error is not None:
        return CheckResult(label, "warn", cfg.error)

    # Enabled + valid: probe the units the plan wants running and derive
    # runtime health through the shared pure function.
    from ...multiroom.reconcile import plan

    units = [it.unit for it in plan(cfg).intents]
    out = _run(["systemctl", "is-active", *units]).stdout.splitlines()
    states = (
        {u: (out[i].strip() or "unknown") for i, u in enumerate(units)}
        if len(out) == len(units)
        else {u: "unknown" for u in units}
    )
    # Leader producer feed (Increment 5): the ACTIVE CamillaDSP config is
    # scanned for the pipe sink — daemon-adjacent truth (camilla's own
    # statefile names the config), never an env-intent mirror. The
    # stream-client probe adds the 2026-06-11 silent-bond classes (stale
    # group→stream binding / muted client / leader's own client absent);
    # RPC failure maps to an explicit unreachable verdict, same as
    # /state — the doctor and the dashboard must tell one story.
    from ...multiroom.config import SNAP_STREAM_ID
    from ...multiroom.leader_config import active_leader_pipe_path
    from ...multiroom.snapcast_rpc import read_stream_clients
    from ...multiroom.state import _self_client_name

    stream_clients = None
    if cfg.role == "leader":
        stream_clients = read_stream_clients()
        if stream_clients is None:
            stream_clients = "unreachable"

    runtime = derive_grouping_runtime(
        cfg, states,
        leader_tap_path=active_leader_pipe_path(),
        stream_clients=stream_clients,
        self_name=_self_client_name(),
        want_stream=SNAP_STREAM_ID,
    )

    base = (
        f"on — role={cfg.role} channel={cfg.channel} "
        f"bond_id={cfg.bond_id} buffer_ms={cfg.buffer_ms}"
    )
    if cfg.role == "follower":
        base += f" leader_addr={cfg.leader_addr}"

    status = "warn" if runtime["health"] == "degraded" else "ok"
    return CheckResult(label, status, f"{base} — {runtime['detail']}")


@doctor_check(order=71.2, group="grouping")
def check_grouping_pair_lock() -> CheckResult:
    """Surface the composite pair-lock truth used by ``/state.grouping``.

    This deliberately reuses ``derive_grouping_runtime`` rather than
    re-scoring the bond in the doctor. The verdict includes the honest
    Snapcast limitation: local FIFO bytes and client connection/volume are
    observable today, but follower buffer-fill/drift/time-lock are not
    exposed by Snapcast's documented JSON-RPC surface.
    """
    from ...multiroom.config import (
        SNAP_STREAM_ID,
        load_config as _load_grouping_config,
    )
    from ...multiroom.leader_config import active_leader_pipe_path
    from ...multiroom.reconcile import plan
    from ...multiroom.snapcast_rpc import read_stream_clients
    from ...multiroom.state import derive_grouping_runtime, _self_client_name

    label = "grouping: pair lock"
    cfg = _load_grouping_config()
    if not cfg.enabled:
        return CheckResult(label, "ok", "single-speaker (grouping off)")
    if cfg.error is not None:
        return CheckResult(label, "warn", cfg.error)

    units = [it.unit for it in plan(cfg).intents]
    out = _run(["systemctl", "is-active", *units]).stdout.splitlines()
    states = (
        {u: (out[i].strip() or "unknown") for i, u in enumerate(units)}
        if len(out) == len(units)
        else {u: "unknown" for u in units}
    )

    stream_clients = None
    if cfg.role == "leader":
        stream_clients = read_stream_clients()
        if stream_clients is None:
            stream_clients = "unreachable"

    runtime = derive_grouping_runtime(
        cfg,
        states,
        leader_tap_path=active_leader_pipe_path() if cfg.role == "leader" else "",
        stream_clients=stream_clients,
        self_name=_self_client_name(),
        want_stream=SNAP_STREAM_ID,
        local_outputd_status=_read_outputd_status(),
    )
    pair_lock = runtime.get("pair_lock") or {}
    status = str(pair_lock.get("status") or "unknown")
    detail = str(pair_lock.get("detail") or "pair-lock verdict unavailable")
    if status == "degraded":
        return CheckResult(label, "warn", detail)
    if status == "unknown":
        return CheckResult(label, "warn", detail)
    return CheckResult(label, "ok", detail)


# The PCM-resolution probe, run in a CHILD interpreter.
#
# WHAT IT DELIBERATELY DOES NOT DO: play, record, or reach `prepare`. The ioplug
# attaches the SHM ring in its `prepare` callback
# (`jts_ring_prepare` / `jts_ring_capture_prepare` in
# `c/jts-ring-ioplug/pcm_jts_ring.c`); its plugin-open function only parses the
# conf.d block, allocates, and calls `snd_pcm_ioplug_create`, touching `path`
# only as a string. So open-then-close resolves the name AND dlopen()s the
# ioplug — the whole of the failure this check exists to predict — while
# touching no ring file, taking no writer flock, and stamping no pid. That is
# what makes it safe to run against a LIVE bond, which the sibling
# `_jts_ring_pcm_resolves` probe in the audio_runtime domain is not: that one
# runs `aplay`/`arecord` for a second, which does reach `prepare` and would
# create-or-attach the ring, so it is gated (`_jts_ring_probeable_pcms`) on
# fan-in being inactive AND the ring file being absent. There is no equivalent
# state to gate on here — a bonded endpoint's grouping
# ring is live whenever snapclient holds it — so the probe itself has to be the
# thing that cannot perturb. `tests/test_grouping_ring_observability.py` pins
# the attach-site claim against the C source.
#
# A CHILD INTERPRETER rather than an in-process ctypes call, matching the
# audio_runtime probe's isolation posture: `snd_pcm_open` dlopens third-party
# plugin code, and jasper-doctor runs dozens of other checks whose results must
# not be lost to a fault inside one of them.
_GROUPING_PCM_PROBE = """\
import ctypes, sys
lib = ctypes.CDLL("libasound.so.2")
lib.snd_pcm_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
                             ctypes.c_int, ctypes.c_int]
lib.snd_pcm_open.restype = ctypes.c_int
lib.snd_pcm_close.argtypes = [ctypes.c_void_p]
lib.snd_pcm_close.restype = ctypes.c_int
handle = ctypes.c_void_p()
# stream 0 = SND_PCM_STREAM_PLAYBACK, mode 1 = SND_PCM_NONBLOCK.
rc = lib.snd_pcm_open(ctypes.byref(handle), sys.argv[1].encode(), 0, 1)
if rc == 0:
    lib.snd_pcm_close(handle)
print(rc)
"""

#: Budget for the probe child. Generous against a cold interpreter start on a
#: Pi; the probe itself does no I/O beyond parsing /etc/alsa/conf.d and one
#: dlopen, so exhausting this means something is wedged, not slow.
_GROUPING_PCM_PROBE_TIMEOUT_SEC = 15.0


def _probe_grouping_pcm(pcm: str) -> tuple[int | None, str]:
    """Open-and-close ``pcm`` in a child interpreter.

    Returns ``(rc, "")`` with alsa-lib's own return code, or ``(None, reason)``
    when the probe could not be run at all (no interpreter, no libasound, an
    unparseable answer) — which is a warn, not a verdict about the PCM.
    """
    try:
        proc = _run(
            [sys.executable, "-c", _GROUPING_PCM_PROBE, pcm],
            timeout=_GROUPING_PCM_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run the PCM probe: {exc}"
    answer = (proc.stdout or "").strip().splitlines()
    try:
        return int(answer[-1]), ""
    except (IndexError, ValueError):
        stderr = " ".join((proc.stderr or "").split())[-200:]
        return None, f"PCM probe returned no result{f': {stderr}' if stderr else ''}"


@doctor_check(order=71.3, group="grouping")
def check_grouping_ring_device() -> CheckResult:
    """Does ``pcm.jts_ring_grouping`` actually open on this box?

    THE GAP THIS CLOSES. The grouping ring's conf.d block installs
    unconditionally on every box, but the ioplug ``.so`` it names is BUILT — and
    when that build fails transiently the installer degrades to ``return 0``
    rather than aborting the deploy. The two halves then disagree silently: a
    name is declared that nothing can resolve, and the first thing to notice is
    snapclient taking ``-EINVAL`` at open on the day the household bonds a pair.
    Nothing predicted it, because presence of the conf.d proves nothing about
    the plugin and no check opened the name. This one does.

    SAFE AGAINST A LIVE BOND, which is the constraint that shapes it — see the
    note above :data:`_GROUPING_PCM_PROBE`. It opens and immediately closes; the
    ring is attached at ``prepare``, which this never reaches, so a bonded
    endpoint carrying audio is not touched.

    SEVERITY IS WEIGHED BY WHAT A BROKEN NAME COSTS *THIS* BOX, the same way
    ``check_ring_platform_assets`` weighs its own missing assets: on a BONDED box
    the name is load-bearing right now, so a failure to resolve is ``fail``; on a
    solo box nothing opens it yet, so the same defect is a ``warn`` that gets
    fixed by the next deploy before it can cost anyone a bond.

    NO BUSY CASE, and that follows from the safety property rather than being a
    separate policy. Every ``-EBUSY`` the ring can produce comes from
    ``jts_ring_writer_open`` / ``jts_ring_reader_open`` — the single-writer and
    single-reader guards — and both are reached only from the ``prepare``
    callbacks this probe never enters. A ring busy with live bonded audio is
    therefore indistinguishable here from an idle one: both simply resolve. The
    outcomes are name-resolves, name-does-not-resolve, and probe-could-not-run.

    Statuses:
      - ok   — the name resolved and the ioplug loaded.
      - warn — the probe could not be run (no libasound on this host), or the
               name did not resolve on a box that is not bonded.
      - fail — the conf.d block is missing, or the name did not resolve on a
               bonded box.
    """
    from ...multiroom.config import load_config as _load_grouping_config
    from ...multiroom.grouping_ring import GROUPING_RING_CONF_D, GROUPING_RING_PCM
    from ...ring_assets import RING_ALSA_PLUGIN_DIR, RING_IOPLUG_SO

    label = "grouping ring device"
    if not Path(GROUPING_RING_CONF_D).is_file():
        return CheckResult(
            label,
            "fail",
            f"{GROUPING_RING_CONF_D} is not installed — pcm.{GROUPING_RING_PCM} "
            "cannot resolve; redeploy (bash scripts/deploy-to-pi.sh)",
        )
    rc, reason = _probe_grouping_pcm(GROUPING_RING_PCM)
    if rc is None:
        return CheckResult(label, "warn", reason)
    if rc == 0:
        return CheckResult(
            label, "ok", f"pcm.{GROUPING_RING_PCM} resolves and the ioplug loads"
        )
    named = errno.errorcode.get(-rc, "") if rc < 0 else ""
    bonded = _load_grouping_config().enabled
    return CheckResult(
        label,
        "fail" if bonded else "warn",
        f"pcm.{GROUPING_RING_PCM} did not open: rc={rc}"
        + (f" ({named})" if named else "")
        + " — the conf.d block is installed but alsa-lib could not resolve it; "
        f"check that {RING_IOPLUG_SO} is present in {RING_ALSA_PLUGIN_DIR} and "
        "redeploy to rebuild it"
        + ("" if bonded else " (this box is not bonded, so nothing opens it yet)"),
    )


@doctor_check(order=71.5, group="grouping")
def check_grouping_snapcast_installed() -> CheckResult:
    """Grouping needs the snapcast binaries — snapserver hosts the stream,
    snapclient plays it. install.sh ships the JTS snap units but deliberately
    does NOT apt-install the binaries (off-by-default, like the usbsink overlay),
    and the grouping reconciler owns the opt-in install. This check reads the
    runtime truth directly: OFF skips (snapcast deliberately absent); ON fails if
    either binary is missing after provisioning, with the one-line remediation."""
    from ...multiroom.config import load_config as _load_grouping_config

    label = "grouping: snapcast installed"
    cfg = _load_grouping_config()
    if not cfg.enabled:
        return CheckResult(label, "ok", "grouping off (snapcast not required)")
    missing = [b for b in ("snapserver", "snapclient") if shutil.which(b) is None]
    if missing:
        return CheckResult(
            label,
            "fail",
            f"grouping is configured but {', '.join(missing)} not installed — the "
            "snap units fail on every start (grouping silently does nothing; on an "
            "active leader the reconciler fails closed to solo). Install with: "
            "sudo apt install snapserver snapclient",
        )
    return CheckResult(label, "ok", "snapserver + snapclient present")


@doctor_check(order=71.6, group="grouping")
def check_grouping_snapcast_version() -> CheckResult:
    """Warn when the installed snapclient differs from the version this
    design validated against
    (:data:`jasper.multiroom.provision.VALIDATED_SNAPCAST_VERSION` — Trixie's
    apt repo at authoring time). The apt package is deliberately UNPINNED in
    ``jasper.multiroom.provision.ensure_snapcast_installed`` (a pin would turn
    a routine Trixie point release into a failed install — the household
    loses grouping, and it blocks security updates — for a mismatch that is
    not a safety hazard). This check is the resulting visibility: it probes
    the binary directly (bounded, mirroring this file's
    ``_resolved_jasper_voice_env`` idiom), never pins anything, and is
    warn-only — there is nothing here to enforce.

    Skips (``ok``) when: grouping is off (mirrors
    ``check_grouping_snapcast_installed`` — a warn about a disabled
    subsystem's version is not actionable); snapclient is not installed; the
    probe cannot even run (a timeout, a vanished binary); the probe exits
    non-zero (e.g. a partial-upgrade linker error — the exit detail itself is
    surfaced, since that IS the useful fact here); or its output has no
    version-shaped token. The exit code is checked BEFORE the regex ever
    runs — a non-zero exit's own error text can coincidentally contain a
    version-shaped substring (a linked library's version, not snapclient's
    own), and parsing it anyway would fabricate a comparison from a process
    that determined nothing."""
    from ...multiroom.config import load_config as _load_grouping_config
    from ...multiroom.provision import VALIDATED_SNAPCAST_VERSION

    label = "grouping: snapcast version"
    cfg = _load_grouping_config()
    if not cfg.enabled:
        return CheckResult(label, "ok", "grouping off (snapcast not required)")
    if shutil.which("snapclient") is None:
        return CheckResult(label, "ok", "snapclient not installed (n/a)")

    try:
        proc = _run(["snapclient", "--version"])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return CheckResult(
            label, "ok",
            f"could not determine the installed snapclient version: {e}",
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return CheckResult(
            label, "ok",
            "could not determine the installed snapclient version: "
            f"snapclient exited {proc.returncode}"
            + (f": {detail}" if detail else ""),
        )
    match = re.search(r"\d+\.\d+\.\d+", (proc.stdout or "") + (proc.stderr or ""))
    if match is None:
        return CheckResult(
            label, "ok",
            "could not determine the installed snapclient version from "
            "`snapclient --version` output",
        )
    installed = match.group(0)
    if installed != VALIDATED_SNAPCAST_VERSION:
        return CheckResult(
            label, "warn",
            f"installed snapclient {installed} differs from the "
            f"{VALIDATED_SNAPCAST_VERSION} this design validated against — "
            "not pinned by design (a pin would block security updates and "
            "fail installs on routine Trixie point releases); this is "
            "visibility only, not a fault",
        )
    return CheckResult(
        label, "ok", f"installed snapclient matches validated {installed}",
    )


@doctor_check(order=74, group="grouping")
def check_grouping_rate_adjust() -> CheckResult:
    """inv-5: no CamillaDSP **in a bonded chain**
    runs ``enable_rate_adjust: true`` — on either role.

    snapclient's sample-stuffing is the single rate-tracker for the synced
    chain; a second rate-adjuster in the leader's CamillaDSP (the one
    daemon writing the shared stream) fights it and oscillates (the
    documented ``rate_adjust`` + ``AsyncSinc`` trap). The no-rate-adjust rule is
    SPECIFIC to the leader's pipe-writing CamillaDSP (a File/pipe sink has no
    output clock, so snapclient is the sole tracker). An ACTIVE follower's
    CamillaDSP IS in the bonded path
    (distributed-active Slice 3 — it captures the grouping ring and runs
    Layer A) but is not a tracker there either. snapclient is — in BOTH endpoint
    roles, follower and active leader, each writing its own box's grouping ring.
    CamillaDSP's ``enable_rate_adjust`` follows the SINK it plays into, and on a
    ring CAPTURE the request cannot be actuated at all — a ring PCM is an
    ioplug, so CamillaDSP builds no HCtl and finds no mixer element to steer.
    A stray ``true`` on an active follower is therefore INERT, which is exactly
    why this stays ``warn`` rather than ``fail``: it is an observability lie, not
    a hazard, and the check's job is to catch a bond apply that did not land.

    A DUMB (passive, single-DAC) follower is the one bonded shape still OUT of
    scope, and deliberately so: it plays the round-tripped stream through
    outputd's ``dac_content`` lane, and its own CamillaDSP stays on the solo
    fallback feed — whose sink is Ring B, so
    :func:`jasper.camilla_config_contract.resolve_enable_rate_adjust` emits
    ``false`` there too. Its local CamillaDSP never joins the bonded chain, so
    there is no bond apply here to catch.

    This reads the ACTIVE config, so it
    catches every generator and a config generated BEFORE the bond formed
    (stale → still rate_adjust on; the reconciler regenerates on bond
    form, so a warn here means that apply failed — check its journal)."""
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.reconcile import is_active_speaker_box
    from .correction import _active_camilla_config_path

    label = "grouping: rate_adjust"
    cfg = load_config()
    # "In the bonded chain" for the instance this check reads (the ACTIVE
    # statefile's config — camilla#1 on a passive leader and on an active
    # follower, camilla#2 on an active leader): a LEADER of either kind bakes the
    # program to the snapfifo, and an ACTIVE-speaker FOLLOWER captures the
    # grouping ring and runs Layer A. A dumb follower is neither.
    # `is_active_speaker_box` is total and
    # fail-soft to False, so an unreadable topology narrows the scope rather than
    # inventing a warn.
    in_bonded_chain = is_active_member(cfg) and (
        cfg.role == "leader" or is_active_speaker_box()
    )
    if not in_bonded_chain:
        return CheckResult(
            label, "ok", "no local CamillaDSP in a bonded chain here (n/a)"
        )

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "warn", f"could not read config_path from {statefile}")
    path = Path(config_path)
    if not path.exists():
        return CheckResult(label, "warn", f"active config missing: {config_path}")
    try:
        rate_adjust = _devices_rate_adjust_from_text(path.read_text())
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {config_path}: {e}")

    if rate_adjust is True:
        return CheckResult(
            label, "warn",
            f"{config_path} has enable_rate_adjust:true but this box is a "
            "bonded member — snapclient is the chain's only rate-tracker; "
            "the reconciler's bond apply did not land (check "
            "jasper-grouping-reconcile's journal, or re-save /sound)",
        )
    if rate_adjust is None:
        # ``_devices_rate_adjust_from_text`` returns None for ABSENT *or*
        # unparseable, and neither supports the claim "rate_adjust off" — the
        # old ``ok`` here reported a fact the file does not carry. The invariant
        # is unconfirmed, so say so.
        return CheckResult(
            label, "warn",
            f"could not confirm enable_rate_adjust in {config_path} — the "
            "devices block has no readable enable_rate_adjust key, so this "
            "bonded member's rate-tracking cannot be verified",
        )
    return CheckResult(label, "ok", f"rate_adjust off for bonded member ({config_path})")


@doctor_check(order=75, group="grouping")
def check_grouping_leader_pipe() -> CheckResult:
    """A bonded LEADER's ACTIVE CamillaDSP config must write snapserver's
    pipe (``devices.playback`` = File → SNAPFIFO) — else snapserver streams
    an empty FIFO and every member (including the leader's own round-trip)
    hears silence while every unit shows green. The silent-wrong-config
    class this check exists for."""
    from ...multiroom.config import is_active_leader, load_config
    from ...multiroom.leader_config import playback_is_pipe
    from ...multiroom.reconcile import SNAPFIFO
    from .correction import _active_camilla_config_path

    label = "grouping: leader pipe"
    cfg = load_config()
    if not is_active_leader(cfg):
        return CheckResult(label, "ok", "not an active bond leader (n/a)")

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "warn", f"could not read config_path from {statefile}")
    path = Path(config_path)
    if not path.exists():
        return CheckResult(label, "warn", f"active config missing: {config_path}")
    try:
        is_pipe = playback_is_pipe(path.read_text(), SNAPFIFO)
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {config_path}: {e}")

    if not is_pipe:
        return CheckResult(
            label, "warn",
            f"{config_path} does not write the snapserver pipe ({SNAPFIFO}) "
            "but this is an active bond leader — the stream is silent; the "
            "reconciler's bond apply did not land (check "
            "jasper-grouping-reconcile's journal)",
        )
    return CheckResult(label, "ok", f"leader CamillaDSP writes {SNAPFIFO}")


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse reconciler-written env text through the canonical parser."""
    return parse_env_text(text)


def _resolved_jasper_voice_env() -> tuple[dict[str, str] | None, str]:
    """Return the environment jasper-voice actually starts with, or None.

    ``systemctl show -p Environment`` reports a unit's inline ``Environment=``
    directives ONLY — never the ``EnvironmentFile=`` layers where the
    reconciler-owned grouping keys live (issue #2387). jasper-voice.service
    layers ``grouping-voice.env`` LAST, so that file is overlaid here the same
    way, read fresh rather than taken from this process's environment.

    None means no authority answered: the grouping file exists but cannot be
    read, or systemctl was unreadable AND the file carried nothing. The
    fail-soft readers (``parse_env_file``, ``resolved_tts_routing_env``) are
    deliberately not used here — they collapse an unreadable file into ``{}``,
    which is the same false green as #2387 wearing a different hat, and a
    doctor that cannot read its authority must say so.
    """
    from ...env_load import read_env_file_state
    from ...multiroom.reconcile import VOICE_GROUPING_ENV_FILE

    unit_env: dict[str, str] | None = None
    error = ""
    try:
        proc = _run(
            [
                "systemctl", "show", "-p", "Environment", "--value",
                "jasper-voice.service",
            ],
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        error = str(e)
    else:
        if proc.returncode == 0:
            unit_env = _parse_systemd_environment(proc.stdout)
        else:
            error = (proc.stderr or proc.stdout).strip() or (
                f"systemctl exited {proc.returncode}"
            )
    grouping = read_env_file_state(VOICE_GROUPING_ENV_FILE)
    if grouping.status == "unreadable":
        return None, f"{VOICE_GROUPING_ENV_FILE}: {grouping.error}"
    # The unit layers this file LAST, so it wins over the inline directives.
    resolved = {**(unit_env or {}), **grouping.values}
    if unit_env is None and not resolved:
        return None, error
    return resolved, error


@doctor_check(order=75.3, group="grouping")
def check_grouping_channel_pick() -> CheckResult:
    """An ACTIVE member's outputd round-trip lane must be wired with THIS
    speaker's channel — the canonical home of the channel drop (outputd
    ``ChannelPick``, Increment 3; the local-CamillaDSP weave it replaced
    is gone). A missing or drifted env is SILENT (the speaker plays the
    full stereo program — the wrong channel), so this drift check is the
    only way a wrong-channel member is visible."""
    from ...fanin_coupling import dac_content_lane_marker_armed
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.dac_content_ring import DAC_CONTENT_RING_PERIOD_FRAMES
    from ...multiroom.reconcile import (
        LANE_REFUSED_ACTIVE_ENDPOINT,
        LANE_REFUSED_FLAT_OUTPUT_DENIED,
        LANE_REFUSED_PERIOD,
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_GROUPING_ENV_FILE,
        _output_topology_state,
        box_outputd_period_frames,
        member_lane_decision,
    )

    label = "grouping: channel pick"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")

    # The reconciler's OWN rule, asked with the reconciler's own inputs, so this
    # check can never expect a lane the writer would not have armed.
    active_box_state, flat_output_allowed = _output_topology_state()
    if active_box_state is None:
        # The reconciler took its own `active_speaker_topology_unknown` branch
        # and never asked the lane rule at all, so reporting "the topology does
        # not permit a flat graph" would name the wrong fault and offer no
        # remedy for the real one.
        return CheckResult(
            label, "warn",
            "this box's saved output topology could not be read, so the "
            "grouping reconciler preserved the running graph instead of wiring "
            "the round-trip lane (blocked_reason=active_speaker_topology_unknown)",
        )
    active_endpoint = active_box_state is True
    period = box_outputd_period_frames()
    decision = member_lane_decision(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=period,
    )
    path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if not path.exists():
        if active_endpoint:
            return CheckResult(
                label, "ok",
                "active endpoint uses the snapclient/CamillaDSP grouping ring "
                "(jts_ring_grouping; no outputd channel-pick lane)",
            )
        return CheckResult(
            label, "warn",
            f"{OUTPUTD_GROUPING_ENV_FILE} missing but this is an active bond "
            "member — outputd is not wired for the round-trip lane (run "
            "jasper-grouping-reconcile)",
        )
    try:
        env = _parse_env_file(path.read_text())
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {path}: {e}")

    want_channel = cfg.channel or "stereo"
    # The lane is armed by a BARE marker, so this asks the same predicate
    # outputd's own accept-set backs rather than comparing a path: the ring file
    # is derived at both ends from one constant and no env names it.
    lane_armed = dac_content_lane_marker_armed(env)
    channel = env.get(OUTPUTD_DAC_CONTENT_CHANNEL_ENV, "")
    if decision.reason == LANE_REFUSED_ACTIVE_ENDPOINT:
        if lane_armed or channel:
            return CheckResult(
                label, "warn",
                f"active endpoint should have outputd channel-pick lane cleared "
                f"(lane={'armed' if lane_armed else '(disarmed)'} "
                f"channel={channel or '(unset)'}) — active speakers receive the "
                "round-trip through the grouping ring jts_ring_grouping; run "
                "jasper-grouping-reconcile",
            )
        return CheckResult(
            label, "ok",
            "active endpoint picks its channel off jts_ring_grouping",
        )
    # The two refusals below are the reconciler's own decisions, not drift, so
    # neither prescribes a reconcile: it would change nothing.
    if decision.reason == LANE_REFUSED_PERIOD:
        return CheckResult(
            label, "warn",
            f"this box runs outputd at period_frames="
            f"{period if period is not None else '(unresolved)'}, but the "
            f"dac-content return ring's slot is {DAC_CONTENT_RING_PERIOD_FRAMES}"
            " — outputd would refuse the pair, so this member stays solo",
        )
    if decision.reason == LANE_REFUSED_FLAT_OUTPUT_DENIED:
        return CheckResult(
            label, "warn",
            "this box's saved output topology does not permit a flat "
            "final-output graph, so the round-trip lane stays cleared and this "
            "member plays nothing from the bond",
        )
    if not lane_armed or channel != want_channel:
        return CheckResult(
            label, "warn",
            f"outputd lane env drifted (lane="
            f"{'armed' if lane_armed else '(disarmed)'} "
            f"channel={channel or '(unset)'}, want lane=armed "
            f"channel={want_channel}) — this member would play the wrong "
            "channel; run jasper-grouping-reconcile",
        )
    return CheckResult(label, "ok", f"outputd lane wired, channel={want_channel}")


@doctor_check(order=75.35, group="grouping")
def check_grouping_sub_corner() -> CheckResult:
    """A "sub" member's outputd lane must carry the low-pass corner so it
    plays only the low end. The reconciler emits
    ``JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ`` ONLY for channel="sub"; a missing
    key on a sub member means outputd would fall back to its safe default
    rather than the configured corner — drift worth surfacing. n/a for any
    non-sub member (the key is intentionally absent there)."""
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_SUB_HZ_ENV,
        OUTPUTD_GROUPING_ENV_FILE,
        is_active_speaker_box,
    )

    label = "grouping: sub corner"
    cfg = load_config()
    if not is_active_member(cfg) or cfg.channel != "sub":
        return CheckResult(label, "ok", "not an active sub member (n/a)")
    # An active-speaker box bonds via CamillaDSP (the active-endpoint path),
    # which CLEARS the outputd dumb lane — JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ
    # lives only on the dumb-follower lane, so its absence here is correct, not
    # drift. (An active box can't actually be a sub today — program_channel_for
    # fail-closes — so this is the contradictory-config case; either way the
    # dumb-lane corner check does not apply.)
    if is_active_speaker_box():
        return CheckResult(
            label, "ok",
            "active-speaker box — a sub low-pass would ride CamillaDSP, not "
            "the outputd dumb lane (n/a)",
        )

    path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if not path.exists():
        return CheckResult(
            label, "warn",
            f"{OUTPUTD_GROUPING_ENV_FILE} missing but this is an active sub "
            "member — outputd is not wired with the low-pass corner (run "
            "jasper-grouping-reconcile)",
        )
    try:
        env = _parse_env_file(path.read_text())
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {path}: {e}")

    corner = env.get(OUTPUTD_DAC_CONTENT_SUB_HZ_ENV, "")
    if not corner:
        return CheckResult(
            label, "warn",
            f"{OUTPUTD_DAC_CONTENT_SUB_HZ_ENV} missing while channel=sub — "
            f"outputd would fall back to its safe default instead of the "
            f"configured {cfg.crossover_hz} Hz; run jasper-grouping-reconcile",
        )
    return CheckResult(label, "ok", f"sub low-pass corner wired, {corner} Hz")


@doctor_check(order=75.36, group="grouping")
def check_grouping_local_vs_wireless_sub() -> CheckResult:
    """A speaker must NOT carry BOTH a LOCAL sub (an output_topology
    ``subwoofer`` group on its own spare DAC channel) AND a WIRELESS sub
    (it is the leader of a bond whose roster has a ``channel="sub"`` member,
    OR it is itself a ``channel="sub"`` follower). Two bass producers at
    one speaker is a contradictory, confusing config — bass is doubled or
    fights, and the operator has no single place to reason about the
    crossover. The two subsystems are independent writers (the active-
    speaker topology vs. the grouping bond), so the compiler can't and
    shouldn't hard-block it; this is the one place the contradiction is
    visible. n/a (ok) when at most one sub source is configured.

    Local-sub detection reads the persisted output topology
    (``routing.subwoofer_group_ids`` — the same field the active emitter
    pins to a spare physical output). Wireless-sub detection reuses the
    bond predicates so it can never disagree with the rest of the grouping
    doctor about what the bond is doing."""
    from ...multiroom.config import is_active_leader, is_active_member, load_config
    from ...output_topology import load_output_topology

    label = "grouping: local vs wireless sub"

    has_local_sub = bool(load_output_topology().routing.subwoofer_group_ids)

    cfg = load_config()
    is_wireless_sub_follower = is_active_member(cfg) and cfg.channel == "sub"
    leads_wireless_sub = is_active_leader(cfg) and any(
        m.channel == "sub" for m in cfg.roster
    )
    has_wireless_sub = is_wireless_sub_follower or leads_wireless_sub

    if has_local_sub and has_wireless_sub:
        which = (
            "is itself a wireless sub follower (channel=sub)"
            if is_wireless_sub_follower
            else "leads a bond with a wireless sub member"
        )
        return CheckResult(
            label, "warn",
            "this speaker has a LOCAL sub (an output-topology subwoofer group "
            f"on its own DAC) AND {which} — two bass producers at one speaker "
            "is a contradictory config (doubled / fighting low end). Remove one: "
            "drop the subwoofer group from the active-speaker topology, or "
            "un-pair the wireless sub from http://jts.local/rooms",
        )
    if has_local_sub:
        return CheckResult(label, "ok", "local sub only (no wireless sub)")
    if has_wireless_sub:
        return CheckResult(label, "ok", "wireless sub only (no local sub)")
    return CheckResult(label, "ok", "no local or wireless sub (n/a)")


@doctor_check(order=75.6, group="grouping")
def check_grouping_tts_lane() -> CheckResult:
    """A bonded non-sub passive member's assistant TTS must route to its OWN
    outputd (member-local, instant), not ride the synced stream (delayed by the
    sync buffer + audible on every bonded speaker — the retired Increment 5
    PR-1 interim behavior). Active endpoints are the crossover safety
    exception, and wireless sub followers are parked with outputd TTS unarmed.
    The route matrix wires grouping-voice.env and grouping-outputd.env so the
    voice socket, voice park flag, and outputd TTS server state agree.

    (Replaces ``check_grouping_tts_interim``, the standing bonded warn
    that existed while TTS still mixed in fanin pre-stream — Increment 5
    PR-2 closed that gap.)"""
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.reconcile import (
        OUTPUTD_GROUPING_ENV_FILE,
        VOICE_GROUPING_ENV_FILE,
        is_active_speaker_box,
    )
    from ...multiroom.tts_route import (
        VOICE_PARK_ENV,
        expected_grouping_tts_route,
    )
    from ...tts_routing import (
        FANIN_TTS_SOCKET,
        OUTPUTD_TTS_SOCKET,
        OUTPUTD_TTS_SOCKET_ENV,
        VOICE_TTS_SOCKET_ENV,
    )

    label = "grouping: TTS lane"
    cfg = load_config()
    active = is_active_member(cfg)
    active_endpoint = is_active_speaker_box() if active else False
    route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)

    voice_runtime_env, voice_runtime_error = _resolved_jasper_voice_env()
    voice_socket = (
        voice_runtime_env.get(VOICE_TTS_SOCKET_ENV, "")
        if voice_runtime_env is not None
        else ""
    )
    voice_parked = (
        voice_runtime_env is not None
        and voice_runtime_env.get(VOICE_PARK_ENV, "") == "1"
    )

    # Ahead of the solo/bonded split: every guard below reads this env, so an
    # unresolvable authority must never render as a clean verdict (#2387).
    if voice_runtime_env is None:
        return CheckResult(
            label, "warn",
            "could not resolve jasper-voice's env from its unit directives "
            f"or {VOICE_GROUPING_ENV_FILE} ({voice_runtime_error}) — the "
            "grouped-voice guards cannot run",
        )

    if not active:
        # Solo must resolve to fan-in. With grouping-voice.env layered last,
        # stale bonded overrides can target an unarmed socket or leave voice
        # parked after unbond. A PRESENT-but-empty key is drift too: systemd
        # resolves it to an empty socket path, which breaks TTS playout.
        if (
            VOICE_TTS_SOCKET_ENV in voice_runtime_env
            and voice_socket != FANIN_TTS_SOCKET
        ):
            return CheckResult(
                label, "warn",
                f"solo but jasper-voice runtime env resolves "
                f"{VOICE_TTS_SOCKET_ENV} to {voice_socket or '(unset)'} "
                f"instead of {FANIN_TTS_SOCKET} — assistant voice targets an "
                "un-armed socket; run "
                "jasper-grouping-reconcile",
            )
        if voice_parked:
            return CheckResult(
                label, "warn",
                f"solo but jasper-voice runtime env still carries "
                f"{VOICE_PARK_ENV}=1 — voice may remain parked; run "
                "jasper-grouping-reconcile",
            )
        return CheckResult(label, "ok", route.ok_detail)

    outputd_env: dict[str, str] = {}
    outputd_path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if outputd_path.exists():
        try:
            outputd_env = _parse_env_file(outputd_path.read_text())
        except OSError as e:
            return CheckResult(label, "warn", f"could not read {outputd_path}: {e}")
    outputd_socket = outputd_env.get(OUTPUTD_TTS_SOCKET_ENV, "")
    lane_armed = bool(outputd_socket)

    if route.voice_parked and not voice_parked:
        return CheckResult(
            label, "warn",
            f"{route.kind} route expects {VOICE_PARK_ENV}=1, but "
            "jasper-voice runtime env does not carry the park flag; run "
            "jasper-grouping-reconcile",
        )
    if not route.voice_parked and voice_parked:
        return CheckResult(
            label, "warn",
            f"{route.kind} route should not park voice, but jasper-voice "
            f"runtime env still carries {VOICE_PARK_ENV}=1; run "
            "jasper-grouping-reconcile",
        )

    if not route.outputd_tts_armed:
        if lane_armed:
            return CheckResult(
                label, "warn",
                f"{route.kind} route must keep outputd's TTS socket unarmed "
                f"but {OUTPUTD_TTS_SOCKET_ENV}={outputd_socket!r}; run "
                "jasper-grouping-reconcile",
            )
        if (
            route.expected_voice_socket is not None
            and voice_socket
            and voice_socket != route.expected_voice_socket
        ):
            return CheckResult(
                label, "warn",
                f"{route.kind} route expects jasper-voice runtime env "
                f"{VOICE_TTS_SOCKET_ENV}={route.expected_voice_socket}, "
                f"but it resolves to {voice_socket}; run "
                "jasper-grouping-reconcile",
            )
        return CheckResult(label, "ok", route.ok_detail)

    if voice_socket == OUTPUTD_TTS_SOCKET and outputd_socket != OUTPUTD_TTS_SOCKET:
        return CheckResult(
            label, "warn",
            f"bonded: jasper-voice runtime env targets {OUTPUTD_TTS_SOCKET} but "
            f"{OUTPUTD_GROUPING_ENV_FILE} does not arm "
            f"{OUTPUTD_TTS_SOCKET_ENV} — assistant voice is BROKEN "
            "(writing to a socket nobody serves); run "
            "jasper-grouping-reconcile",
        )
    if voice_socket != OUTPUTD_TTS_SOCKET:
        return CheckResult(
            label, "warn",
            f"bonded but jasper-voice runtime env resolves "
            f"{VOICE_TTS_SOCKET_ENV} to {voice_socket or '(unset)'} instead of "
            f"{OUTPUTD_TTS_SOCKET} — assistant "
            f"voice rides the synced stream (delayed ~{cfg.buffer_ms} ms, "
            "plays on all bonded speakers); check "
            f"{VOICE_GROUPING_ENV_FILE} precedence and run jasper-grouping-reconcile",
        )
    return CheckResult(label, "ok", route.ok_detail)


@doctor_check(order=75.7, group="grouping")
def check_grouping_pair_channels() -> CheckResult:
    """Cross-MEMBER channel coherence — the one drift no member-local check
    can see. A same-channel pair ({left,left} / {right,right}) is the
    residue of an interrupted swap whose rollback also failed: audibly
    wrong, yet each member's env matches its OWN config, so runtime health
    and the channel-pick check both read green. The FOLLOWER owns this
    probe (it already knows its leader's address; the leader would need
    mDNS discovery) — one GET of the leader's /grouping, compared against
    our own channel. Remediation is one tap: /rooms Swap repairs a
    same-channel pair to left/right."""
    from ...multiroom.config import is_active_member, load_config

    label = "grouping: pair channels"
    cfg = load_config()
    if not is_active_member(cfg) or cfg.role != "follower":
        return CheckResult(label, "ok", "solo / not a bonded follower (n/a)")
    if cfg.channel not in ("left", "right"):
        return CheckResult(
            label, "ok", f"channel={cfg.channel or '?'} (not an L/R pair, n/a)",
        )
    from ...control import client as control_client
    from ...multiroom.state import parse_grouping_response

    try:
        resp = control_client.get(
            "/grouping",
            base_url=f"http://{cfg.leader_addr}:8780",
            timeout=2.0,
        )
        leader = parse_grouping_response(resp.json()) or {}
    except Exception as e:  # noqa: BLE001 — connectivity has its own check
        return CheckResult(
            label, "ok",
            f"could not compare (leader {cfg.leader_addr} unreachable: {e} "
            "— connectivity is covered by the grouping health check)",
        )
    leader_channel = str(leader.get("channel") or "")
    if str(leader.get("bond_id") or "") != cfg.bond_id:
        return CheckResult(
            label, "warn",
            f"leader {cfg.leader_addr} reports bond "
            f"{leader.get('bond_id') or '(none)'} but this follower is in "
            f"{cfg.bond_id} — re-pair from /rooms",
        )
    if leader_channel == cfg.channel:
        return CheckResult(
            label, "warn",
            f"BOTH speakers play the {cfg.channel} channel — an interrupted "
            "swap left the pair on one side; press Swap on /rooms (it "
            "repairs a same-channel pair to left/right)",
        )
    return CheckResult(
        label, "ok",
        f"this={cfg.channel} leader={leader_channel or '?'} (coherent)",
    )


@doctor_check(order=75.8, group="grouping")
def check_grouping_household_credential() -> CheckResult:
    """A BONDED member must hold the household credential — the device-to-device
    secret that authenticates the cross-device ``/grouping/set`` fan-out.

    A bonded member with NO secret is the recovery shape (the 2026-05-23
    ext4-loss class, or an adopt that never landed): its ``/grouping/set`` is
    fail-safe-OPEN to any LAN caller until it re-pairs, and this is the only
    place that loss is visible. A solo speaker needs no credential (absence =
    not-yet-paired), so it reads ``ok``. Strictly secret-free — it reports only
    whether the file is present, never reads or echoes the value (mirrors
    ``check_control_token``)."""
    from ...control import household_credential
    from ...multiroom.config import is_active_member, load_config

    label = "grouping: household credential"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not a bonded member (n/a)")
    if household_credential.is_paired():
        return CheckResult(
            label, "ok",
            "present — cross-device /grouping/set is authenticated",
        )
    return CheckResult(
        label, "warn",
        "bonded but the household credential is missing — cross-device "
        "/grouping/set is unauthenticated (fail-safe open) until this speaker "
        "re-pairs; re-save the bond from http://jts.local/rooms to restore it",
    )


@doctor_check(order=75.9, group="grouping")
def check_grouping_airplay_latency() -> CheckResult:
    """A bonded LEADER receiving AirPlay must fit its hidden downstream
    delay (~150 ms pipeline + the Snapcast ``buffer_ms``) inside the budget
    the AirPlay sender negotiated, or its own output lands AFTER the AirPlay
    anchor → bounded residual lip-sync lag (the "Stage D" gap).

    OBSERVABILITY ONLY — this never changes the offset. Skips (``ok``) on
    solo / follower. For a bonded leader it reads the sender's most-recent
    notified latency from shairport's journal (ABSENCE => the default ~2.0 s
    budget, the free regime — fail-soft, so an unreadable journal reads as
    comfortable, never a false warn) and warns ONLY when the budget is
    genuinely too short to hide the delay. Pinned to the same pure
    :func:`jasper.multiroom.airplay_latency.assess_fit` the /state surface
    uses, so the doctor and the dashboard tell one story."""
    from ...multiroom.airplay_latency import assess_fit, read_notified_frames
    from ...multiroom.config import is_active_leader, load_config

    label = "grouping: AirPlay latency fit"
    cfg = load_config()
    if not is_active_leader(cfg):
        return CheckResult(label, "ok", "not an active bond leader (n/a)")

    from ...multiroom.airplay_latency import SHAIRPORT_BACKEND_BUFFER_SEC

    fit = assess_fit(cfg.buffer_ms, read_notified_frames())
    budget_desc = (
        f"budget ~{fit.budget_sec:.3f}s ({fit.budget_source}) vs "
        f"need ~{fit.need_sec:.3f}s (150 ms + buffer_ms={cfg.buffer_ms}) + "
        f"shairport backend buffer {SHAIRPORT_BACKEND_BUFFER_SEC:.3f}s"
    )
    if fit.tight:
        # No local control grows the budget (AP2 latency is sender-authored)
        # and buffer_ms has no wizard knob, so the remediation is honest about
        # the lever that exists: lower JASPER_GROUPING_BUFFER_MS (default 400)
        # in /var/lib/jasper/grouping.env if it was raised. Do NOT point at a
        # /rooms control — none writes buffer_ms.
        return CheckResult(
            label, "warn",
            f"AirPlay budget too short for the bonded round-trip: {budget_desc} "
            f"=> shairport drops the offset => ~{fit.residual_lag_sec * 1000:.0f} ms "
            "residual lip-sync lag (it also logs 'stream latency too short to "
            "accommodate an offset'). The sender's budget can't be grown locally; "
            "if JASPER_GROUPING_BUFFER_MS (grouping.env, default 400) was raised, "
            "lowering it shrinks the need.",
        )
    return CheckResult(label, "ok", f"fits — {budget_desc}")


@doctor_check(order=75.95, group="grouping")
def check_crossover_unit_installed() -> CheckResult:
    """An ACTIVE LEADER must have camilla#2's endpoint-crossover unit
    installed and parseable.

    camilla#2 (``jasper-camilla-crossover.service``, :1235) is the per-driver
    crossover instance an active leader runs alongside the always-on camilla#1.
    It is shipped INERT — not
    enabled, not yet reconciler-gated — so this check only asserts the dormant
    infrastructure is *present and valid* on the one box that will eventually
    run it; it does NOT assert the unit is active (a later PR arms it).

    Active leader = an active/roleful output topology (so this box runs a
    per-driver crossover at all) AND a bonded leader (so it is the leader half
    of the pair). Any other box — an ordinary speaker, a passive leader, an
    active follower — skips cleanly with ``ok``: the unit file is installed
    everywhere by install.sh, but it is only *meaningful* on an active leader,
    and a normal box that never enables it needs no health signal here.

    A missing or unparseable unit on an active leader is a real gap (the
    reconciler PR would have nothing to arm), so it warns."""
    from ...multiroom.config import is_active_leader, load_config
    from ...output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    label = "grouping: crossover unit"
    cfg = load_config()
    if not is_active_leader(cfg):
        return CheckResult(label, "ok", "not an active bond leader (n/a)")

    # Active/roleful topology is the second half of "active leader": only a
    # box that runs a per-driver crossover needs camilla#2. A passive leader
    # (full-range, no roleful outputs) skips. Imported lazily and read through
    # the shared runtime contract, same as check_active_speaker_runtime_graph.
    from ...active_speaker.runtime_contract import (
        active_topology_requires_roleful_graph,
    )

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        # No usable topology means this is not a commissioned active speaker,
        # so camilla#2 is not its concern. Skip rather than warn — the active
        # speaker runtime graph check owns topology-validity reporting.
        return CheckResult(label, "ok", "no active-speaker topology (n/a)")
    if not active_topology_requires_roleful_graph(topology):
        return CheckResult(label, "ok", "passive leader — no crossover (n/a)")

    unit = "jasper-camilla-crossover.service"
    # `systemctl cat` is the canonical "is the unit installed?" probe used
    # across the doctor (renderers / audio use systemctl rather than raw
    # Path.exists, so a unit found anywhere on systemd's search path counts).
    # returncode 0 = systemd found and could read the unit.
    if _run(["systemctl", "cat", unit]).returncode != 0:
        return CheckResult(
            label, "warn",
            f"active leader but {unit} is not installed — the endpoint-"
            "crossover instance cannot be armed; re-run the JTS installer "
            "(bash scripts/deploy-to-pi.sh)",
        )

    # `systemd-analyze verify` is the parse check. It is not always present
    # (dev hosts); when it is, a non-zero exit means the unit is malformed.
    # When it is absent we fall back to installed-only (above) — a parse
    # probe we cannot run must never produce a false warning.
    if shutil.which("systemd-analyze") is None:
        return CheckResult(
            label, "ok",
            f"installed ({unit}); systemd-analyze unavailable, parse unchecked",
        )
    verify = _run(["systemd-analyze", "verify", unit])
    if verify.returncode != 0:
        detail = (verify.stderr or verify.stdout or "").strip().replace("\n", " ")
        return CheckResult(
            label, "warn",
            f"{unit} failed systemd-analyze verify: {detail[:200]}",
        )
    return CheckResult(
        label, "ok", f"installed + parseable ({unit}), INERT until armed",
    )


# NOTE: the former ``check_grouping_tts_separation`` (order 78) was REMOVED
# 2026-06-11 with the rest of the retired outputd-as-producer machinery
# (`SnapfifoSink` / `SNAPFIFO_PRODUCER_WIRED` / the reconciler tap limb): the
# canonical design feeds the snapserver pipe from the leader's CamillaDSP, so
# the check's premise was dead. Its operator story now lives in
# ``check_grouping``'s runtime detail — a bonded leader reads degraded with
# "leader streaming is not built yet — no music producer feeds the snapfifo".
