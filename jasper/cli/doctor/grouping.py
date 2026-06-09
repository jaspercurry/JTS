"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import re
from pathlib import Path

from ._registry import doctor_check
from ._shared import CheckResult, _run


def _devices_rate_adjust_from_text(text: str) -> bool | None:
    """Parse ``devices.enable_rate_adjust`` from a CamillaDSP config. PURE.

    Returns True/False, or None when the key is absent / unparseable. Mirrors
    :func:`jasper.cli.doctor.audio._devices_volume_limit_from_text`'s
    devices-block scanner (top-level ``devices:`` then an indented key)."""
    in_devices = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            in_devices = stripped == "devices:"
            continue
        if not in_devices:
            continue
        match = re.match(r"^\s+enable_rate_adjust:\s*([^#]+)", raw)
        if not match:
            continue
        value = match.group(1).strip().strip("'\"").lower()
        if value in {"true", "yes", "on", "1"}:
            return True
        if value in {"false", "no", "off", "0"}:
            return False
        return None
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
    from ...multiroom.config import disables_local_rate_adjust, load_config
    from .correction import _active_camilla_config_path

    label = "grouping: rate_adjust"
    cfg = load_config()
    if not disables_local_rate_adjust(cfg):
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
