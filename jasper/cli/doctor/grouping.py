"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from pathlib import Path

from ._registry import doctor_check
from ._shared import CheckResult, _camilla_block_field, _run


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
    from ...multiroom.reconcile import (
        plan as _grouping_plan,
    )
    from ...multiroom.state import derive_grouping_runtime

    label = "grouping: mode"
    cfg = _load_grouping_config()
    if not cfg.enabled:
        return CheckResult(label, "ok", "single-speaker (grouping off)")
    if cfg.error is not None:
        return CheckResult(label, "warn", cfg.error)

    # Enabled + valid: probe the units the plan wants running and derive
    # runtime health through the shared pure function.
    units = [it.unit for it in _grouping_plan(cfg).intents]
    out = _run(["systemctl", "is-active", *units]).stdout.splitlines()
    states = (
        {u: (out[i].strip() or "unknown") for i, u in enumerate(units)}
        if len(out) == len(units)
        else {u: "unknown" for u in units}
    )
    # Leader producer feed (Increment 5): the ACTIVE CamillaDSP config is
    # scanned for the pipe sink — daemon-adjacent truth (camilla's own
    # statefile names the config), never an env-intent mirror.
    from ...multiroom.leader_config import active_leader_pipe_path

    runtime = derive_grouping_runtime(
        cfg, states, leader_tap_path=active_leader_pipe_path(),
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
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        OUTPUTD_GROUPING_ENV_FILE,
    )

    label = "grouping: channel pick"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")

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
    return CheckResult(label, "ok", f"outputd lane wired, channel={want_channel}")


@doctor_check(order=75.6, group="grouping")
def check_grouping_tts_interim() -> CheckResult:
    """KNOWN GAP while bonded (Increment 5 PR-1 interim): assistant voice
    rides the synced stream — delayed by the sync buffer and audible on
    ALL bonded speakers — because TTS still mixes in fanin (pre-stream).
    Increment 5 PR-2 (the outputd TTS mixer) moves every member's own TTS
    to its own final output and removes this check. A standing warn while
    bonded is deliberate: a known degradation must stay visible, not
    normalized (the no-silent-failure rule)."""
    from ...multiroom.config import is_active_member, load_config

    label = "grouping: TTS interim"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")
    return CheckResult(
        label, "warn",
        f"bonded: assistant voice is delayed ~{cfg.buffer_ms} ms by the sync "
        "buffer and plays on all bonded speakers (TTS rides the stream until "
        "Increment 5 PR-2 lands the outputd TTS mixer)",
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
