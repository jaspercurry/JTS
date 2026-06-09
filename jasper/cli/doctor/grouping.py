"""jasper-doctor checks — grouping domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from ._registry import doctor_check
from ._shared import CheckResult, _run

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
