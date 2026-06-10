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
        is down), OR a leader whose snap units are up but whose outputd is
        not tapping the snapfifo — snapserver reads green `active` while the
        FIFO is empty and followers get silence. This is §7's "make it
        visible, not invisible": a green config with silent breakage
        underneath is exactly what we refuse to show. Runtime health is
        derived by the same pure `derive_grouping_runtime` the /state
        surface uses, with the leader's current tap injected."""
    from ...multiroom.config import load_config as _load_grouping_config
    from ...multiroom.reconcile import (
        _read_outputd_snapfifo_path as _read_tap,
    )
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
    # Only a valid leader taps outputd into the snapfifo; read its current
    # tap so a dry stream surfaces as degraded. Followers/solo skip this.
    tap = _read_tap() if cfg.role == "leader" else ""
    runtime = derive_grouping_runtime(cfg, states, leader_tap_path=tap)

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
    """inv-5 (docs/HANDOFF-multiroom.md §2): an ACTIVE bond member's local
    CamillaDSP must run ``enable_rate_adjust: false``.

    snapclient's sample-stuffing is the single rate-tracker for the synced
    chain; a second rate-adjuster in the member's local CamillaDSP fights it
    and oscillates (the documented ``rate_adjust`` + ``AsyncSinc`` trap). This
    is the UNIVERSAL backstop for inv-5 — it reads the ACTIVE config, so it
    catches every generator (correction / sound / active-speaker) and a config
    generated BEFORE the bond was formed (stale → still rate_adjust on).

    Skip (ok) when this speaker is solo / off / invalid (not an active member);
    warn when it IS an active member but the active config still has
    ``enable_rate_adjust: true`` — the fix is to regenerate the config
    (re-run /correction or /sound) so the member emits rate_adjust off."""
    from ...multiroom.config import is_active_member, load_config
    from .correction import _active_camilla_config_path

    label = "grouping: rate_adjust"
    cfg = load_config()
    if not is_active_member(cfg):
        return CheckResult(label, "ok", "solo / not an active bond member (n/a)")

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
            "bond member — snapclient + local rate_adjust will oscillate; "
            "regenerate the config (re-run /correction or /sound)",
        )
    return CheckResult(label, "ok", f"rate_adjust off for active member ({config_path})")


def _config_has_channel_select(text: str) -> bool:
    """True if the config's top-level ``mixers:`` block defines a
    ``channel_select:`` mixer (the channel-split). Reads via the shared
    :func:`_camilla_block_field` scanner — presence is value-is-not-None (a
    mixer name's value is the nested channels/mapping block, scanned as "")."""
    return _camilla_block_field(text, "mixers", "channel_select") is not None


@doctor_check(order=75, group="grouping")
def check_grouping_channel_split() -> CheckResult:
    """A bonded member that plays a SINGLE channel (left/right/sub/mono) must
    have the ``channel_select`` mixer in its ACTIVE config — else it plays the
    full stereo program (the WRONG channel), silently.

    This is the observability backstop for the channel-split: unlike
    rate_adjust (which oscillates audibly), a missing channel-split is
    *silent* — the speaker just plays both channels. So a wrong-channel member
    is invisible without this check. Skip (ok) when solo / off / invalid, or
    when the channel is ``stereo`` (passthrough — no channel_select expected).
    Warn when a non-stereo active member's active config lacks
    ``channel_select`` — regenerate (re-run /sound or /correction). NOTE:
    auto-apply on bond-form is owned by the inv-2 reconciler (see §2 "inv-2
    realization"); until it lands this check is how the gap stays visible."""
    from ...multiroom.config import is_active_member, load_config
    from .correction import _active_camilla_config_path

    label = "grouping: channel-split"
    cfg = load_config()
    if not is_active_member(cfg) or cfg.channel == "stereo":
        return CheckResult(label, "ok", "solo / stereo member (n/a)")

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "warn", f"could not read config_path from {statefile}")
    path = Path(config_path)
    if not path.exists():
        return CheckResult(label, "warn", f"active config missing: {config_path}")
    try:
        has_split = _config_has_channel_select(path.read_text())
    except OSError as e:
        return CheckResult(label, "warn", f"could not read {config_path}: {e}")

    if not has_split:
        return CheckResult(
            label, "warn",
            f"{config_path} has no channel_select but this member plays "
            f"channel={cfg.channel} — it will play the full stereo program "
            "(wrong channel); regenerate the config (re-run /sound or /correction)",
        )
    return CheckResult(label, "ok", f"channel_select present for channel={cfg.channel}")
