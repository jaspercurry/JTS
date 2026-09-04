# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — peering domain."""
from __future__ import annotations

import shutil
from pathlib import Path
from ...env_load import read_env_file_state
from ...peering.config import PEERING_OFF_VALUES, PEERING_ON_VALUES
from ._registry import doctor_check
from ._shared import CheckResult, _run

# Closed vocabulary for this module's `CheckResult.reason` (AGENTS.md: tests
# pin status + reason, never `detail` prose). Named by the fact a consumer
# would branch on, not by which code path produced it — an absent env file
# and an explicit `JASPER_PEERING=off` are the same fact (peering is off) and
# share one code.
REASON_PEERING_OFF = "peering_off"
REASON_PEERING_ON = "peering_on"
REASON_PEERING_ENV_UNREADABLE = "peering_env_unreadable"
REASON_PEERING_MODE_UNKNOWN = "peering_mode_unknown"

REASON_DISCOVERY_TOOL_MISSING = "discovery_tool_missing"
REASON_DISCOVERY_BROWSE_FAILED = "discovery_browse_failed"

@doctor_check(order=69, group="peering")
def check_peering_mode() -> CheckResult:
    """Verify /var/lib/jasper/peering.env is parseable.

    Off by default; the household opts in via /rooms/. Both OFF (deliberate)
    and ON (configured) are `ok` — the warn cases catch broken env files
    only."""
    label = "peering: mode"
    p = Path("/var/lib/jasper/peering.env")
    env = read_env_file_state(str(p))
    if env.status == "missing":
        return CheckResult(
            label, "ok",
            "off (default) — enable at http://<hostname>/rooms/",
            reason=REASON_PEERING_OFF,
        )
    if env.status == "unreadable":
        return CheckResult(
            label, "warn", f"can't read {p}: {env.error}",
            reason=REASON_PEERING_ENV_UNREADABLE,
        )
    raw = env.values.get("JASPER_PEERING", "").lower()
    if raw in PEERING_OFF_VALUES:
        return CheckResult(label, "ok", "off (configured)", reason=REASON_PEERING_OFF)
    if raw in PEERING_ON_VALUES:
        return CheckResult(
            label, "ok",
            "on — jasper-control runs the peering daemon",
            reason=REASON_PEERING_ON,
        )
    return CheckResult(
        label, "warn",
        f"unknown JASPER_PEERING={raw!r}; defaults to off. "
        "Edit /var/lib/jasper/peering.env or use /rooms/.",
        reason=REASON_PEERING_MODE_UNKNOWN,
    )

@doctor_check(order=70, group="peering")
def check_peering_discovery() -> CheckResult:
    """Browse `_jasper-peer._udp` to count sibling JTS speakers visible on the
    LAN.

    Informational when peering is OFF (this speaker does not advertise, so
    zero peers is expected). When peering is ON it is the smoke test that
    mDNS-SD is working."""
    label = "peering: discovery"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify peer discovery.",
            reason=REASON_DISCOVERY_TOOL_MISSING,
        )
    proc = _run([bin_path, "-rt", "_jasper-peer._udp"], timeout=4.0)
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            "running? (`systemctl status avahi-daemon`).",
            reason=REASON_DISCOVERY_BROWSE_FAILED,
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
    """Read /var/lib/jasper/peer_id, or '' when missing.

    Best-effort: check_peering_discovery uses it to drop this speaker from the
    visible-peer count, and a missing file only inflates that count by one."""
    try:
        return Path("/var/lib/jasper/peer_id").read_text().strip()
    except OSError:
        return ""
