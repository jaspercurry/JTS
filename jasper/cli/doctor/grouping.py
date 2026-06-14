"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from pathlib import Path

from ...install_profile import is_satellite_install_profile, read_install_profile
from ._registry import doctor_check
from ._shared import CheckResult, _camilla_block_field, _run


def _endpoint_tier() -> bool:
    return is_satellite_install_profile(read_install_profile())


def _endpoint_player() -> str:
    from ...multiroom.reconcile import (
        DEFAULT_ENDPOINT_SNAPCLIENT_PLAYER,
        ENDPOINT_SNAPCLIENT_PLAYER_ENV,
    )
    import os

    return os.environ.get(
        ENDPOINT_SNAPCLIENT_PLAYER_ENV,
        DEFAULT_ENDPOINT_SNAPCLIENT_PLAYER,
    )


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
        is down), OR a bonded LEADER, always, today: no music producer
        feeds the snapfifo yet (HANDOFF-multiroom.md §2, Increments 3–5),
        so snapserver reads green `active` while the FIFO is empty and
        followers get silence. This is §7's "make it visible, not
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
    install_profile = read_install_profile()
    endpoint_tier = is_satellite_install_profile(install_profile)
    if endpoint_tier and cfg.role == "leader":
        return CheckResult(
            label,
            "warn",
            "endpoint install tier cannot be grouping leader — "
            "reconciler stops snapcast; reassign this member as follower "
            "from /rooms",
        )

    # Enabled + valid: probe the units the plan wants running and derive
    # runtime health through the shared pure function.
    from ...multiroom.reconcile import plan_for_install_profile

    units = [
        it.unit for it in plan_for_install_profile(
            cfg, install_profile=install_profile,
        ).intents
    ]
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


@doctor_check(order=74, group="grouping")
def check_grouping_rate_adjust() -> CheckResult:
    """inv-5 (docs/HANDOFF-multiroom.md §2): an ACTIVE bond LEADER's local
    CamillaDSP must run ``enable_rate_adjust: false``.

    snapclient's sample-stuffing is the single rate-tracker for the synced
    chain; a second rate-adjuster in the leader's CamillaDSP (the one
    daemon writing the shared stream) fights it and oscillates (the
    documented ``rate_adjust`` + ``AsyncSinc`` trap). A FOLLOWER is
    deliberately out of scope (canonical model, Increment 5): its local
    CamillaDSP is not in the bonded playback path — it feeds only the
    inv-B fallback lane, an ALSA loopback with a real clock, where
    rate_adjust=true is CORRECT. This reads the ACTIVE config, so it
    catches every generator and a config generated BEFORE the bond formed
    (stale → still rate_adjust on; the reconciler regenerates on bond
    form, so a warn here means that apply failed — check its journal)."""
    from ...multiroom.config import is_active_member, load_config
    from .correction import _active_camilla_config_path

    label = "grouping: rate_adjust"
    if _endpoint_tier():
        return CheckResult(label, "ok", "endpoint tier has no local CamillaDSP")
    cfg = load_config()
    if not (is_active_member(cfg) and cfg.role == "leader"):
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
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.leader_config import playback_is_pipe
    from ...multiroom.reconcile import SNAPFIFO
    from .correction import _active_camilla_config_path

    label = "grouping: leader pipe"
    if _endpoint_tier():
        return CheckResult(label, "ok", "endpoint tier cannot host snapserver pipe")
    cfg = load_config()
    if not (is_active_member(cfg) and cfg.role == "leader"):
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
    )

    label = "grouping: channel pick"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")
    if _endpoint_tier():
        player = _endpoint_player()
        if player == "alsa:device=default" and cfg.channel in {"left", "right", "sub"}:
            return CheckResult(
                label,
                "warn",
                "endpoint direct ALSA player is default stereo but this member "
                f"is assigned channel={cfg.channel}; configure "
                "JASPER_ENDPOINT_SNAPCLIENT_PLAYER to a channel-selecting ALSA "
                "device before using this endpoint in a phase-sensitive bond",
            )
        return CheckResult(
            label,
            "ok",
            f"endpoint direct player={player}; outputd channel-pick n/a",
        )

    path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if not path.exists():
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


@doctor_check(order=75.6, group="grouping")
def check_grouping_tts_lane() -> CheckResult:
    """A bonded member's assistant TTS must route to its OWN outputd
    (member-local, instant), not ride the synced stream (delayed by the
    sync buffer + audible on every bonded speaker — the retired
    Increment 5 PR-1 interim behavior). The reconciler wires this with
    TWO env files that must agree: grouping-voice.env points jasper-voice
    at outputd's TTS socket, and grouping-outputd.env arms outputd's TTS
    server on that socket. Drift between them is the worst shape — voice
    writing to a socket nobody serves makes the assistant SILENT, which
    the no-silent-failure rule says must be visible here.

    (Replaces ``check_grouping_tts_interim``, the standing bonded warn
    that existed while TTS still mixed in fanin pre-stream — Increment 5
    PR-2 closed that gap.)"""
    from ...multiroom.config import is_active_member, load_config
    from ...multiroom.reconcile import (
        OUTPUTD_GROUPING_ENV_FILE,
        OUTPUTD_TTS_SOCKET,
        OUTPUTD_TTS_SOCKET_ENV,
        VOICE_GROUPING_ENV_FILE,
        VOICE_TTS_SOCKET_ENV,
    )

    label = "grouping: TTS lane"
    cfg = load_config()
    if _endpoint_tier():
        return CheckResult(label, "ok", "endpoint tier has no local assistant TTS")

    voice_path = Path(VOICE_GROUPING_ENV_FILE)
    voice_env: dict[str, str] = {}
    if voice_path.exists():
        try:
            voice_env = _parse_env_file(voice_path.read_text())
        except OSError as e:
            return CheckResult(label, "warn", f"could not read {voice_path}: {e}")
    voice_socket = voice_env.get(VOICE_TTS_SOCKET_ENV, "")

    if not is_active_member(cfg):
        # Solo must NOT carry a stale outputd override: outputd's TTS
        # server is only armed while bonded, so a leftover pointer would
        # have voice writing to a socket nobody serves — silent assistant.
        if voice_socket:
            return CheckResult(
                label, "warn",
                f"solo but {VOICE_GROUPING_ENV_FILE} still points "
                f"{VOICE_TTS_SOCKET_ENV} at {voice_socket} — assistant "
                "voice targets an un-armed socket; run "
                "jasper-grouping-reconcile",
            )
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")

    outputd_env: dict[str, str] = {}
    outputd_path = Path(OUTPUTD_GROUPING_ENV_FILE)
    if outputd_path.exists():
        try:
            outputd_env = _parse_env_file(outputd_path.read_text())
        except OSError as e:
            return CheckResult(label, "warn", f"could not read {outputd_path}: {e}")
    lane_armed = bool(outputd_env.get(OUTPUTD_TTS_SOCKET_ENV))

    if voice_socket == OUTPUTD_TTS_SOCKET and not lane_armed:
        return CheckResult(
            label, "warn",
            f"bonded: voice targets {OUTPUTD_TTS_SOCKET} but "
            f"{OUTPUTD_GROUPING_ENV_FILE} does not arm "
            f"{OUTPUTD_TTS_SOCKET_ENV} — assistant voice is BROKEN "
            "(writing to a socket nobody serves); run "
            "jasper-grouping-reconcile",
        )
    if voice_socket != OUTPUTD_TTS_SOCKET:
        return CheckResult(
            label, "warn",
            f"bonded but {VOICE_GROUPING_ENV_FILE} does not point "
            f"{VOICE_TTS_SOCKET_ENV} at {OUTPUTD_TTS_SOCKET} — assistant "
            f"voice rides the synced stream (delayed ~{cfg.buffer_ms} ms, "
            "plays on all bonded speakers); run jasper-grouping-reconcile",
        )
    return CheckResult(
        label, "ok", f"member-local TTS wired ({OUTPUTD_TTS_SOCKET})",
    )


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


# NOTE: the former ``check_grouping_tts_separation`` (order 78) was REMOVED
# 2026-06-11 with the rest of the retired outputd-as-producer machinery
# (`SnapfifoSink` / `SNAPFIFO_PRODUCER_WIRED` / the reconciler tap limb): the
# canonical design feeds the snapserver pipe from the leader's CamillaDSP, so
# the check's premise was dead. Its operator story now lives in
# ``check_grouping``'s runtime detail — a bonded leader reads degraded with
# "leader streaming is not built yet — no music producer feeds the snapfifo".
# See HANDOFF-multiroom.md §2 "Canonical signal flow" + "Stranded by this
# design".
