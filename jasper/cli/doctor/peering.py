"""jasper-doctor checks — peering domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import shutil
from pathlib import Path
from ._registry import doctor_check
from ._shared import CheckResult, _run

@doctor_check(order=69, group="peering")
def check_peering_mode() -> CheckResult:
    """Verify /var/lib/jasper/peering.env is parseable.

    Off by default; the user opts in via the /peers/ web wizard. We
    return `ok` for both OFF (deliberate) and ON (configured) — the
    `warn`/`fail` cases catch broken env files only."""
    label = "peering: mode"
    p = Path("/var/lib/jasper/peering.env")
    if not p.exists():
        return CheckResult(
            label, "ok",
            "off (default) — enable at http://<hostname>/peers/",
        )
    raw = ""
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("JASPER_PEERING="):
                raw = line.split("=", 1)[1].strip().strip("'\"").lower()
                break
    except OSError as e:
        return CheckResult(label, "warn", f"can't read {p}: {e}")
    if raw in ("", "off", "false", "0", "no", "disabled"):
        return CheckResult(label, "ok", "off (configured)")
    if raw in ("on", "true", "1", "yes", "enabled"):
        return CheckResult(
            label, "ok",
            "on — jasper-control runs the peering daemon",
        )
    return CheckResult(
        label, "warn",
        f"unknown JASPER_PEERING={raw!r}; defaults to off. "
        "Edit /var/lib/jasper/peering.env or use the /peers/ wizard.",
    )

@doctor_check(order=70, group="peering")
def check_peering_discovery() -> CheckResult:
    """Browse `_jasper-peer._udp` to count sibling JTS speakers
    visible on the LAN.

    Informational when peering is OFF (we don't advertise; expected
    to see zero peers). When peering is ON, this is the smoke test
    that mDNS-SD is working — if siblings are reachable, this Pi
    should see them here."""
    label = "peering: discovery"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify peer discovery.",
        )
    proc = _run([bin_path, "-rt", "_jasper-peer._udp"], timeout=4.0)
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            "running? (`systemctl status avahi-daemon`).",
        )
    # Count distinct peer_id TXT records.
    peer_ids: set[str] = set()
    for line in proc.stdout.splitlines():
        # avahi-browse -r output includes lines like:
        #     txt = ["peer_id=abc-uuid" "room=kitchen" "primary=0" "proto=1"]
        if "peer_id=" in line:
            for token in line.replace('"', " ").split():
                if token.startswith("peer_id="):
                    peer_ids.add(token[len("peer_id="):].strip(",[]"))
    # Drop our own peer_id if we know it (so the count is "siblings").
    local_id = _local_peer_id()
    if local_id:
        peer_ids.discard(local_id)
    if not peer_ids:
        return CheckResult(
            label, "ok",
            "0 sibling peers visible (single-device mode)",
        )
    sample = ", ".join(sorted(peer_ids)[:3])
    return CheckResult(
        label, "ok",
        f"{len(peer_ids)} sibling peer(s) visible: {sample}",
    )

def _local_peer_id() -> str:
    """Read /var/lib/jasper/peer_id (returns '' if missing).

    Best-effort — used by check_peering_discovery to filter ourselves
    out of the visible-peer count. A missing file is fine (peering
    template install never ran), the count is just slightly inflated."""
    try:
        return Path("/var/lib/jasper/peer_id").read_text().strip()
    except OSError:
        return ""
