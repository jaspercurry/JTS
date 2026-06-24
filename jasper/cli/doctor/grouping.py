# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

from ._registry import doctor_check
from ._shared import CheckResult, _camilla_block_field, _run

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
    from ...multiroom.leader_config import active_leader_pipe_path
    from ...multiroom.reconcile import SNAP_STREAM_ID
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
    from ...multiroom.config import load_config as _load_grouping_config
    from ...multiroom.leader_config import active_leader_pipe_path
    from ...multiroom.reconcile import SNAP_STREAM_ID, plan
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


@doctor_check(order=71.5, group="grouping")
def check_grouping_snapcast_installed() -> CheckResult:
    """Grouping needs the snapcast binaries — snapserver hosts the stream,
    snapclient plays it. install.sh ships the JTS snap units but deliberately
    does NOT apt-install the binaries (off-by-default, like the usbsink overlay),
    on the theory that installing them is "the grouping opt-in's job". There is
    no automated opt-in step today, so a box where grouping was enabled but
    snapcast was never installed has the units present yet failing on every
    start — invisible until you bond, and on an ACTIVE leader it was the
    2026-06-23 reboot-loop trigger (now fail-closed in the reconciler). Surface
    it: OFF skips (snapcast deliberately absent); ON fails if either binary is
    missing, with the one-line remediation."""
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


@doctor_check(order=74, group="grouping")
def check_grouping_rate_adjust() -> CheckResult:
    """inv-5 (docs/HANDOFF-multiroom.md §2): an ACTIVE bond LEADER's local
    CamillaDSP must run ``enable_rate_adjust: false``.

    snapclient's sample-stuffing is the single rate-tracker for the synced
    chain; a second rate-adjuster in the leader's CamillaDSP (the one
    daemon writing the shared stream) fights it and oscillates (the
    documented ``rate_adjust`` + ``AsyncSinc`` trap). The no-rate-adjust rule is
    SPECIFIC to the leader's pipe-writing CamillaDSP (a File/pipe sink has no
    output clock, so snapclient is the sole tracker). A FOLLOWER is out of this
    check's scope because it correctly runs ``rate_adjust: true``: a passive
    follower's CamillaDSP sits outside the bonded path, and an ACTIVE follower's
    CamillaDSP IS in the bonded path (distributed-active Slice 3 — it captures
    the round-trip loopback and runs Layer A) but is itself the sole rate-tracker
    of that loopback, so ``rate_adjust: true`` is REQUIRED there, not forbidden.
    This reads the ACTIVE config, so it
    catches every generator and a config generated BEFORE the bond formed
    (stale → still rate_adjust on; the reconciler regenerates on bond
    form, so a warn here means that apply failed — check its journal)."""
    from ...multiroom.config import is_active_leader, load_config
    from .correction import _active_camilla_config_path

    label = "grouping: rate_adjust"
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
        rate_adjust = _devices_rate_adjust_from_text(path.read_text())
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {config_path}: {e}")

    if rate_adjust is True:
        return CheckResult(
            label, "warn",
            f"{config_path} has enable_rate_adjust:true but this is an active "
            "bond LEADER — snapclient + local rate_adjust will oscillate; "
            "the reconciler's bond apply did not land (check "
            "jasper-grouping-reconcile's journal, or re-save /sound)",
        )
    return CheckResult(label, "ok", f"rate_adjust off for active leader ({config_path})")


@doctor_check(order=75, group="grouping")
def check_grouping_leader_pipe() -> CheckResult:
    """A bonded LEADER's ACTIVE CamillaDSP config must write snapserver's
    pipe (``devices.playback`` = File → SNAPFIFO) — else snapserver streams
    an empty FIFO and every member (including the leader's own round-trip)
    hears silence while every unit shows green. The silent-wrong-config
    class this check exists for (HANDOFF-multiroom.md §2, Increment 5)."""
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
    """KEY=value lines → dict (reconciler-written file; no quoting)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _parse_systemd_environment(text: str) -> dict[str, str]:
    """Parse ``systemctl show -p Environment`` output into a key/value map."""
    text = text.strip()
    if text.startswith("Environment="):
        text = text[len("Environment="):]
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    env: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        env[key] = value.strip().strip('"').strip("'")
    return env


def _resolved_jasper_voice_env() -> tuple[dict[str, str] | None, str]:
    """Return jasper-voice's systemd-resolved environment, if available."""
    try:
        proc = _run(
            [
                "systemctl", "show", "-p", "Environment", "--value",
                "jasper-voice.service",
            ],
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return None, str(e)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return None, detail or f"systemctl exited {proc.returncode}"
    return _parse_systemd_environment(proc.stdout), ""


@doctor_check(order=75.3, group="grouping")
def check_grouping_channel_pick() -> CheckResult:
    """An ACTIVE member's outputd round-trip lane must be wired with THIS
    speaker's channel — the canonical home of the channel drop (outputd
    ``ChannelPick``, Increment 3; the local-CamillaDSP weave it replaced
    is gone). A missing or drifted env is SILENT (the speaker plays the
    full stereo program — the wrong channel), so this drift check is the
    only way a wrong-channel member is visible."""
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.reconcile import (
        MEMBER_CONTENT_FIFO,
        OUTPUTD_CONTENT_BRIDGE_ENV,
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        OUTPUTD_GROUPING_ENV_FILE,
        is_active_speaker_box,
    )

    label = "grouping: channel pick"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")

    active_endpoint = is_active_speaker_box()
    path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if not path.exists():
        if active_endpoint:
            return CheckResult(
                label, "ok",
                "active endpoint uses the snapclient/CamillaDSP loopback path "
                "(no outputd channel-pick lane)",
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
    fifo = env.get(OUTPUTD_DAC_CONTENT_FIFO_ENV, "")
    channel = env.get(OUTPUTD_DAC_CONTENT_CHANNEL_ENV, "")
    if active_endpoint:
        if fifo or channel:
            return CheckResult(
                label, "warn",
                f"active endpoint should have outputd channel-pick lane cleared "
                f"(fifo={fifo or '(unset)'} channel={channel or '(unset)'}) — "
                "active speakers receive the round-trip through the "
                "snapclient/CamillaDSP loopback path; run jasper-grouping-reconcile",
            )
        return CheckResult(
            label, "ok",
            "active endpoint uses snapclient/CamillaDSP loopback channel pick",
        )
    if fifo != MEMBER_CONTENT_FIFO or channel != want_channel:
        return CheckResult(
            label, "warn",
            f"outputd lane env drifted (fifo={fifo or '(unset)'} "
            f"channel={channel or '(unset)'}, want fifo={MEMBER_CONTENT_FIFO} "
            f"channel={want_channel}) — this member would play the wrong "
            "channel; run jasper-grouping-reconcile",
        )
    # Writer/validator coherence pin (the jts3 2026-06-11 boot-loop
    # incident): while bonded, this file must ALSO pin
    # CONTENT_BRIDGE=direct — without it, a lab rate_match retune in a
    # lower env layer composes into the combination outputd fail-closes
    # on. A stale pre-pin file (e.g. upgraded across the fix without a
    # reconcile) is exactly what this catches.
    if env.get(OUTPUTD_CONTENT_BRIDGE_ENV) != "direct":
        return CheckResult(
            label, "warn",
            f"{OUTPUTD_GROUPING_ENV_FILE} lacks the "
            f"{OUTPUTD_CONTENT_BRIDGE_ENV}=direct pin while bonded — a "
            "rate_match retune in a lower env layer would fail-close "
            "outputd at its next restart; run jasper-grouping-reconcile",
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

    if not active:
        # Solo must resolve to fan-in. With grouping-voice.env layered last,
        # stale bonded overrides can target an unarmed socket or leave voice
        # parked after unbond.
        if (
            voice_runtime_env is not None
            and voice_socket
            and voice_socket != FANIN_TTS_SOCKET
        ):
            return CheckResult(
                label, "warn",
                f"solo but jasper-voice runtime env resolves "
                f"{VOICE_TTS_SOCKET_ENV} to {voice_socket} instead of "
                f"{FANIN_TTS_SOCKET} — assistant voice targets an "
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

    if voice_runtime_env is None:
        return CheckResult(
            label, "warn",
            "bonded but could not read jasper-voice resolved env via "
            f"`systemctl show -p Environment`: {voice_runtime_error}",
        )

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
    secret that authenticates the cross-device ``/grouping/set`` fan-out
    (docs/HANDOFF-control-plane-auth.md §6).

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
    anchor → bounded residual lip-sync lag (the "Stage D" gap,
    docs/HANDOFF-airplay.md "AirPlay 2 latency is sender-authored").

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
        f"shairport backend buffer {SHAIRPORT_BACKEND_BUFFER_SEC:.1f}s"
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
    crossover instance an active leader runs alongside the always-on camilla#1
    (docs/HANDOFF-distributed-active.md "Stage B"). It is shipped INERT — not
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
# See HANDOFF-multiroom.md §2 "Canonical signal flow" + "Stranded by this
# design".
