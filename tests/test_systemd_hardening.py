# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard: the Tier-A daemons keep their WS1 phase-1 hardening stanza.

A compromise of an always-on, network-facing `jasper-*` daemon is a full-root
device compromise today (they all run as root). Phase 1 of the privilege-
separation work (docs/HANDOFF-privilege-separation.md) hardens each so a root
RCE can no longer write the filesystem, load kernel modules, change kernel
tunables, or enter new namespaces — measured on hardware to drop
`systemd-analyze security` from 8.7-9.6 (EXPOSED/UNSAFE) to ~6.2-6.6 (MEDIUM).

This test pins that contract: an edit that removes `ProtectSystem=strict` or any
of the phase-1 directives from a Tier-A unit fails CI. It deliberately encodes
the per-unit nuances (the reason a uniform block would break things), so the
exceptions are explicit, not silent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Tier-A unit -> its file (jasper-web lives in deploy/, the rest in deploy/systemd/).
TIER_A = {
    "jasper-voice": ROOT / "deploy/systemd/jasper-voice.service",
    "jasper-control": ROOT / "deploy/systemd/jasper-control.service",
    "jasper-web": ROOT / "deploy/jasper-web.service",
    "jasper-mux": ROOT / "deploy/systemd/jasper-mux.service",
    "jasper-input": ROOT / "deploy/systemd/jasper-input.service",
}

ACCESSORY_ADAPTERS = {
    "jasper-wiim-remote-mic": ROOT / "deploy/systemd/jasper-wiim-remote-mic.service",
}

ACCESSORY_RECONCILERS = {
    "jasper-accessory-reconcile": (
        ROOT / "deploy/systemd/jasper-accessory-reconcile.service"
    ),
}

# Per-unit TimeoutStartSec overrides; anything absent expects the 60s default.
RECONCILE_ONESHOT_TIMEOUTS = {
    "jasper-fanin-coupling-auto": "120",
}

RECONCILE_ONESHOTS = {
    "jasper-aec-reconcile": ROOT / "deploy/systemd/jasper-aec-reconcile.service",
    "jasper-accessory-reconcile": (
        ROOT / "deploy/systemd/jasper-accessory-reconcile.service"
    ),
    "jasper-grouping-reconcile": (
        ROOT / "deploy/systemd/jasper-grouping-reconcile.service"
    ),
    # P3/P4 default-flip: the boot-time fan-in coupling + USB combo resolver.
    "jasper-fanin-coupling-auto": (
        ROOT / "deploy/systemd/jasper-fanin-coupling-auto.service"
    ),
}

# Directives every Tier-A unit must carry (key -> required value, or None = any value).
REQUIRED_ALL = {
    "ProtectSystem": "strict",
    "ProtectHome": None,
    "PrivateTmp": "true",
    "NoNewPrivileges": "true",
    "ProtectKernelTunables": "true",
    "ProtectKernelModules": "true",
    "ProtectControlGroups": "true",
    "RestrictNamespaces": "true",
    "RestrictSUIDSGID": "true",
    "LockPersonality": "true",
    "SystemCallArchitectures": "native",
    "RestrictAddressFamilies": None,
}

# ProtectKernelLogs is intentionally OMITTED on the two units that shell out to
# diagnostic/network tools reading the kernel log ring buffer (dmesg). Required
# everywhere else.
KERNEL_LOGS_EXEMPT = {"jasper-control", "jasper-web"}

# ProtectHome=tmpfs (hide /root) on the daemons that need no home dir.
TMPFS_HOME = {"jasper-voice", "jasper-mux"}


def _directives(path: Path) -> list[tuple[str, str]]:
    """All `Key=Value` directive lines in a unit file (comments/blank stripped)."""
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("[") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        out.append((key.strip(), value.strip()))
    return out


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_tier_a_unit_exists(unit, path):
    assert path.is_file(), f"{unit}: expected unit at {path}"


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_tier_a_required_directives(unit, path):
    directives = _directives(path)
    keys = {k for k, _ in directives}
    pairs = set(directives)
    missing = []
    for key, want in REQUIRED_ALL.items():
        if want is None:
            if key not in keys:
                missing.append(f"{key}=<any>")
        elif (key, want) not in pairs:
            missing.append(f"{key}={want}")
    assert not missing, (
        f"{unit} ({path.name}) lost WS1 phase-1 hardening directive(s): "
        f"{missing}. See docs/HANDOFF-privilege-separation.md."
    )


@pytest.mark.parametrize("unit,path", sorted(ACCESSORY_ADAPTERS.items()))
def test_accessory_adapter_unit_is_hardened(unit, path):
    directives = _directives(path)
    pairs = set(directives)
    keys = {k for k, _ in directives}
    for key, want in REQUIRED_ALL.items():
        if want is None:
            assert key in keys, f"{unit}: missing {key}=<any>"
        else:
            assert (key, want) in pairs, f"{unit}: missing {key}={want}"
    assert ("ProtectKernelLogs", "true") in pairs
    assert ("User", "jasper-input") in pairs
    assert ("Group", "jasper") in pairs
    assert ("SupplementaryGroups", "bluetooth") in pairs
    assert ("CapabilityBoundingSet", "") in pairs
    assert ("SystemCallFilter", "@system-service") in pairs
    assert ("PrivateDevices", "true") in pairs
    assert ("ProtectClock", "true") in pairs
    assert ("ProtectHostname", "true") in pairs
    assert ("ProtectProc", "invisible") in pairs
    assert ("ProcSubset", "pid") in pairs
    assert ("RestrictRealtime", "true") in pairs
    assert ("MemoryDenyWriteExecute", "true") in pairs
    assert ("RestrictAddressFamilies", "AF_UNIX AF_INET") in pairs
    assert ("IPAddressDeny", "any") in pairs
    assert ("IPAddressAllow", "localhost") in pairs
    assert ("RemoveIPC", "true") in pairs
    assert ("UMask", "0077") in pairs


@pytest.mark.parametrize("unit,path", sorted(ACCESSORY_RECONCILERS.items()))
def test_accessory_reconciler_unit_is_hardened_root_oneshot(unit, path):
    directives = _directives(path)
    pairs = set(directives)
    keys = {k for k, _ in directives}
    for key, want in REQUIRED_ALL.items():
        if want is None:
            assert key in keys, f"{unit}: missing {key}=<any>"
        else:
            assert (key, want) in pairs, f"{unit}: missing {key}={want}"
    assert ("Type", "oneshot") in pairs
    assert ("User", "root") not in pairs
    assert ("Group", "jasper") in pairs
    assert ("ProtectKernelLogs", "true") in pairs
    assert ("CapabilityBoundingSet", "") in pairs
    assert ("SystemCallFilter", "@system-service") in pairs
    assert ("PrivateDevices", "true") in pairs
    assert ("ProtectClock", "true") in pairs
    assert ("ProtectHostname", "true") in pairs
    assert ("ProtectProc", "invisible") in pairs
    assert ("ProcSubset", "pid") in pairs
    assert ("RestrictRealtime", "true") in pairs
    assert ("MemoryDenyWriteExecute", "true") in pairs
    assert ("RestrictAddressFamilies", "AF_UNIX") in pairs
    assert ("RemoveIPC", "true") in pairs
    assert ("UMask", "0022") in pairs
    rwpaths = " ".join(v for k, v in pairs if k == "ReadWritePaths")
    assert "/var/lib/jasper" in rwpaths
    assert "/etc/systemd/system" in rwpaths


@pytest.mark.parametrize("unit,path", sorted(RECONCILE_ONESHOTS.items()))
def test_reconcile_oneshots_have_bounded_start_timeout(unit, path):
    """Short-lived reconcilers must not remain `activating` forever if a future
    blocking child sneaks in; timeout turns that into an observable failure."""
    pairs = set(_directives(path))
    assert ("Type", "oneshot") in pairs
    # jasper-fanin-coupling-auto holds the #1252 entry lock across its full
    # pass (worst case ~75s+: <=10s lock wait + usbsink/outputd restarts + the
    # coordinated camilla stop->fanin->start sequence + a possible 15s
    # audio-hardware-reconcile kick); a kill mid-sequence leaves CamillaDSP
    # cleanly stopped where OnFailure cannot catch it, so its timeout must
    # outlast the pass (review #1252 SF-1). The other reconcilers keep 60.
    expected_timeout = RECONCILE_ONESHOT_TIMEOUTS.get(unit, "60")
    assert ("TimeoutStartSec", expected_timeout) in pairs, (
        f"{unit}: reconcile oneshots need a finite start timeout so startup "
        f"dependency mistakes fail visibly instead of wedging voice offline "
        f"(expected TimeoutStartSec={expected_timeout})."
    )


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_protect_kernel_logs_present_unless_exempt(unit, path):
    has = ("ProtectKernelLogs", "true") in set(_directives(path))
    if unit in KERNEL_LOGS_EXEMPT:
        assert not has, (
            f"{unit} is documented as ProtectKernelLogs-exempt (spawns dmesg-reading "
            "subprocesses); if that changed, update KERNEL_LOGS_EXEMPT + the doc."
        )
    else:
        assert has, f"{unit} must set ProtectKernelLogs=true."


@pytest.mark.parametrize("unit", sorted(TMPFS_HOME))
def test_tmpfs_home_where_no_home_needed(unit):
    assert ("ProtectHome", "tmpfs") in set(_directives(TIER_A[unit])), (
        f"{unit} should hide /root via ProtectHome=tmpfs (it needs no home dir)."
    )


# --------------------------------------------------------------------------
# WS1 Phase 3b — the non-root user drop. 3b-1 dropped voice/mux/input; 3b-2
# dropped jasper-control (polkit rule for its broker/supervisor systemctl+reboot
# + group-readable secret env for the jasper-doctor it spawns); 3b-3 dropped
# jasper-web (polkit rule for its NetworkManager wifi management + bluetooth /
# systemd-journal groups). All five Tier-A daemons now run non-root.
# --------------------------------------------------------------------------

# unit -> (expected User=, expected SupplementaryGroups set)
DROPPED = {
    # 4a: `jasper-secrets` grants read of the split-out LLM API keys + Google
    # client secret/token tree in /var/lib/jasper-secrets (mux/control/input are
    # NOT in it — exact-set assertion below is the exclusion guard). 4b:
    # `jasper-intsecrets` grants HA + Spotify integration secrets to
    # voice/control/mux/web (input excluded). `audio` = ALSA.
    "jasper-voice": ("jasper-voice", {"audio", "jasper-secrets", "jasper-intsecrets"}),
    "jasper-mux": ("jasper-mux", {"jasper-intsecrets"}),
    "jasper-input": ("jasper-input", {"input"}),
    # 3b-2: control's privileged restarts/reboots are granted by polkit
    # (deploy/polkit/49-jasper-control.rules), not a group; it opens no
    # ALSA/input device. The one supplementary group is `systemd-journal` —
    # several /state cards (airplay_health, dial, wifi_guardian) read the journal.
    # Deliberately NOT in jasper-secrets: it reads the active provider NAME from
    # the (now keyless) voice_provider.env, never the keys (Phase 4a).
    "jasper-control": ("jasper-control", {"systemd-journal", "jasper-intsecrets"}),
    # 3b-3: web's NetworkManager writes (the /wifi/ wizard) are granted by polkit
    # (deploy/polkit/49-jasper-web.rules), not a group. Its supplementary groups
    # are `audio` (ALSA correction_substream for active-speaker commissioning),
    # `bluetooth` (BlueZ Adapter1 Alias for the /speaker rename — a D-Bus policy
    # grant), `systemd-journal` (journalctl -k Wi-Fi diagnostics), and, since 4a,
    # `jasper-secrets` (the wizards write + render the secret files). No
    # CAP_NET_ADMIN — NL80211 scan-repair routes through a root helper.
    "jasper-web": (
        "jasper-web",
        {"audio", "bluetooth", "systemd-journal", "jasper-secrets", "jasper-intsecrets"},
    ),
}


@pytest.mark.parametrize("unit,expected", sorted(DROPPED.items()))
def test_user_drop(unit, expected):
    expected_user, expected_supp = expected
    directives = _directives(TIER_A[unit])
    pairs = set(directives)
    assert ("User", expected_user) in pairs, (
        f"{unit}: WS1 Phase 3b-1 requires User={expected_user} (non-root drop)."
    )
    assert ("Group", "jasper") in pairs, (
        f"{unit}: must join the shared `jasper` group for cross-daemon "
        "/run socket + /var/lib/jasper access."
    )
    # CapabilityBoundingSet= (empty value) drops ALL capabilities — none of the
    # dropped daemons need one (no RT/mlock/raw-USB; audio + input come from
    # supplementary groups).
    assert ("CapabilityBoundingSet", "") in pairs, (
        f"{unit}: must set CapabilityBoundingSet= (empty) to drop all caps."
    )
    assert ("SystemCallFilter", "@system-service") in pairs, (
        f"{unit}: must set SystemCallFilter=@system-service."
    )
    supp = set()
    for k, v in directives:
        if k == "SupplementaryGroups":
            supp.update(v.split())
    assert supp == expected_supp, (
        f"{unit}: expected SupplementaryGroups {expected_supp or '(none)'}, got "
        f"{supp or '(none)'}."
    )


def test_control_keeps_runtimedir_and_avahi_rwpaths_after_drop():
    """jasper-control's 3b-2 drop must KEEP the directives the non-root user
    relies on: Group=jasper (broker socket reachable by mux/web), the
    RuntimeDirectory for the broker socket bind, and ReadWritePaths covering
    /etc/avahi/services (the peering advert it renders). ProtectHome must stay
    read-only (NOT tmpfs) for the diagnostic subprocesses it spawns."""
    pairs = set(_directives(TIER_A["jasper-control"]))
    assert ("Group", "jasper") in pairs
    assert ("RuntimeDirectory", "jasper-control") in pairs, (
        "the broker binds /run/jasper-control/restart.sock via RuntimeDirectory."
    )
    assert ("ProtectHome", "read-only") in pairs, (
        "control spawns jasper-doctor (home/ALSA/dmesg introspection); ProtectHome "
        "must stay read-only, NOT tmpfs."
    )
    rwpaths = " ".join(v for k, v in pairs if k == "ReadWritePaths")
    assert "/etc/avahi/services" in rwpaths, (
        "control renders the peering advert into /etc/avahi/services."
    )
    # ProtectKernelLogs stays OMITTED (the spawned doctor reads dmesg).
    assert ("ProtectKernelLogs", "true") not in pairs, (
        "ProtectKernelLogs must stay omitted for the doctor's dmesg fingerprint read."
    )


SERVICE_USERS_SH = ROOT / "deploy/lib/install/service-users.sh"


def test_install_creates_every_dropped_user():
    """The install↔unit contract: every User= a dropped unit declares must be
    created by service-users.sh, with matching supplementary groups — a unit
    referencing a user the installer didn't create fails to start (brick)."""
    sh = SERVICE_USERS_SH.read_text()
    assert "groupadd -r jasper" in sh, "must create the shared `jasper` group"
    lines = sh.splitlines()
    for unit, (user, supp) in DROPPED.items():
        useradd = [ln for ln in lines if "useradd" in ln and f" {user}" in ln]
        assert useradd, f"service-users.sh must `useradd ... {user}` (for {unit})"
        line = useradd[0]
        assert "-g jasper" in line, f"{user} primary group must be jasper"
        for g in supp:
            # Always-present groups (audio / input / systemd-journal) land via
            # the useradd -G. A package-owned group — bluetooth, created by bluez
            # during install_deps, which runs AFTER create_jasper_service_users —
            # is added via a guarded `usermod -aG` instead, so a missing group
            # can't fail the useradd and abort the fresh install under `set -e`.
            in_useradd = f"-G {g}" in line or f"-G {g}," in line or f",{g}" in line
            in_usermod = f"usermod -aG {g} {user}" in sh
            assert in_useradd or in_usermod, (
                f"{user} must be in supplementary group {g} (via useradd -G or a "
                "guarded usermod -aG, matching the unit's SupplementaryGroups=)"
            )


def test_secrets_compartment_phase4a():
    """WS1 Phase 4a — the high-value secrets (LLM API keys, Google client
    secret/token tree, Google Routes API key) live in the group-`jasper-secrets` compartment, readable
    only by jasper-voice + jasper-web. Pin: (1) the group is created; (2) voice +
    web source voice_keys.env + the RELOCATED google_credentials.env; (3) only
    jasper-web (which WRITES the compartment via the /voice + /google wizards)
    gets it in ReadWritePaths — jasper-voice only READS (keys via EnvironmentFile,
    Google tree via the group), so under ProtectSystem=strict it needs NO write
    grant; (4) the daemons that must NOT see the keys (mux/control/input) don't
    source voice_keys.env."""
    secret_keys = "/var/lib/jasper-secrets/voice_keys.env"
    secret_google = "/var/lib/jasper-secrets/google_credentials.env"
    secret_routes = "/var/lib/jasper-secrets/google_routes.env"

    # (1) group created in service-users.sh (before the useradd -G that uses it).
    sh = SERVICE_USERS_SH.read_text()
    assert "groupadd -r jasper-secrets" in sh, (
        "service-users.sh must create the jasper-secrets group"
    )

    # (2) both voice + web source the secret files (and are in the group, asserted
    #     by test_user_drop's exact-set check).
    for unit in ("jasper-voice", "jasper-web"):
        pairs = list(_directives(TIER_A[unit]))
        envfiles = " ".join(v for k, v in pairs if k == "EnvironmentFile")
        assert secret_keys in envfiles, f"{unit} must source {secret_keys}"
        assert secret_google in envfiles, f"{unit} must source {secret_google}"
        assert secret_routes in envfiles, f"{unit} must source {secret_routes}"
        # The OLD broad path must be gone (no dual-source re-exposure).
        assert "/var/lib/jasper/google_credentials.env" not in envfiles, (
            f"{unit} still sources the pre-4a broad google_credentials.env path"
        )

    # (3) WRITE grant is asymmetric on purpose: web writes the compartment (OAuth
    #     + wizard saves) so it needs ReadWritePaths; voice only reads, so it must
    #     NOT have the write grant (least privilege + no hard dep on a
    #     non-StateDirectory path for the most critical daemon).
    web_rw = " ".join(
        v for k, v in _directives(TIER_A["jasper-web"]) if k == "ReadWritePaths"
    )
    assert "/var/lib/jasper-secrets" in web_rw, (
        "jasper-web writes the compartment (OAuth tokens + wizard saves) and "
        "needs it in ReadWritePaths"
    )
    voice_rw = " ".join(
        v for k, v in _directives(TIER_A["jasper-voice"]) if k == "ReadWritePaths"
    )
    assert "/var/lib/jasper-secrets" not in voice_rw, (
        "jasper-voice only READS the compartment (keys via EnvironmentFile, "
        "Google tree via the group); it must NOT get a write grant"
    )

    # (4) the excluded daemons must NOT source the secret env files.
    for unit in ("jasper-mux", "jasper-control", "jasper-input"):
        pairs = list(_directives(TIER_A[unit]))
        envfiles = " ".join(v for k, v in pairs if k == "EnvironmentFile")
        assert secret_keys not in envfiles, (
            f"{unit} must NOT source {secret_keys} (Phase 4a compartmentalization)"
        )
        assert secret_routes not in envfiles, (
            f"{unit} must NOT source {secret_routes} (billable Routes API key)"
        )


def test_secrets_compartment_phase4b():
    """WS1 Phase 4b — integration secrets (Home Assistant token + Spotify
    credentials/cache tree) live in the group-`jasper-intsecrets` compartment.
    Pin: (1) the group is created; (2) Spotify creds are sourced from the
    relocated file by every Spotify consumer; (3) the old broad env paths are
    gone; (4) voice/control/mux/web all have the write grant because spotipy can
    persist refreshed tokens from each; (5) jasper-input is excluded."""
    int_spotify = "/var/lib/jasper-intsecrets/spotify_credentials.env"
    int_ha = "/var/lib/jasper-intsecrets/home_assistant.env"
    int_dir = "/var/lib/jasper-intsecrets"

    sh = SERVICE_USERS_SH.read_text()
    assert "groupadd -r jasper-intsecrets" in sh, (
        "service-users.sh must create the jasper-intsecrets group"
    )

    for unit in ("jasper-voice", "jasper-control", "jasper-mux", "jasper-web"):
        pairs = list(_directives(TIER_A[unit]))
        envfiles = " ".join(v for k, v in pairs if k == "EnvironmentFile")
        assert int_spotify in envfiles, f"{unit} must source {int_spotify}"
        assert "/var/lib/jasper/spotify_credentials.env" not in envfiles, (
            f"{unit} still sources the pre-4b broad spotify_credentials.env path"
        )
        rwpaths = " ".join(v for k, v in pairs if k == "ReadWritePaths")
        assert int_dir in rwpaths, (
            f"{unit} must be able to write {int_dir}; Spotify token refreshes "
            "persist through spotipy cache handlers in voice/control/mux/web."
        )

    voice_envfiles = " ".join(
        v for k, v in _directives(TIER_A["jasper-voice"]) if k == "EnvironmentFile"
    )
    assert int_ha in voice_envfiles, "jasper-voice must source relocated HA env"
    assert "/var/lib/jasper/home_assistant.env" not in voice_envfiles, (
        "jasper-voice still sources the pre-4b broad home_assistant.env path"
    )

    input_pairs = list(_directives(TIER_A["jasper-input"]))
    input_envfiles = " ".join(v for k, v in input_pairs if k == "EnvironmentFile")
    input_rwpaths = " ".join(v for k, v in input_pairs if k == "ReadWritePaths")
    assert int_spotify not in input_envfiles
    assert int_ha not in input_envfiles
    assert int_dir not in input_rwpaths


def test_streambox_spotify_uses_intsecrets_compartment():
    """The streambox profile serves /spotify/ too, so its web unit and install
    path must use the same Phase 4b compartment as the full profile. This guards
    against split-brain saves where the shared wizard writes
    /var/lib/jasper-intsecrets but the profile-specific unit still sources the
    retired /var/lib/jasper path."""
    int_spotify = "/var/lib/jasper-intsecrets/spotify_credentials.env"
    int_dir = "/var/lib/jasper-intsecrets"
    old_spotify = "/var/lib/jasper/spotify_credentials.env"

    streambox = ROOT / "deploy/jasper-web-streambox.service"
    directives = list(_directives(streambox))
    envfiles = " ".join(v for k, v in directives if k == "EnvironmentFile")
    rwpaths = " ".join(v for k, v in directives if k == "ReadWritePaths")

    assert int_spotify in envfiles
    assert old_spotify not in envfiles
    assert int_dir in rwpaths

    install_sh = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    streambox_branch = install_sh.split(
        'if [[ "${install_profile}" == "streambox" ]]; then', 1,
    )[1].split("return 0", 1)[0]
    assert "migrate_secrets_phase4b" in streambox_branch, (
        "streambox installs must create/migrate the Phase 4b compartment before "
        "installing the profile-scoped jasper-web unit"
    )


def test_streambox_web_unit_stays_root_until_validated():
    """The streambox web unit intentionally stays root in 3b-3 — it's a Pi class
    (Pi Zero 2 W) the drop could not be hardware-validated on. install.sh installs
    the web polkit rule + group-writable dirs in BOTH profiles, so dropping it is
    a one-line `User=`/`Group=` edit here once validated. Guard against an
    accidental half-drop (a User= without the validation) by pinning the
    deferral — when it's deliberately dropped, this test is updated in the same PR."""
    streambox = ROOT / "deploy/jasper-web-streambox.service"
    assert streambox.is_file(), f"missing {streambox}"
    assert not any(k == "User" for k, _ in _directives(streambox)), (
        "deploy/jasper-web-streambox.service gained User= — the streambox web "
        "drop needs streambox-hardware validation first (see "
        "docs/HANDOFF-privilege-separation.md Phase 3b-3)."
    )


# --------------------------------------------------------------------------
# Remaining WS1 scope - Tier-B / adjacent privileged support units.
#
# These are not Tier-A network-facing daemons, but they still run privileged
# boot/udev/recovery work. The next WS1 increments should move them only one
# validated vertical slice at a time. Guard against accidental half-drops:
# adding User= here must come with the matching hardware validation, installer
# user/group contract, and docs/HANDOFF-privilege-separation.md update.
# --------------------------------------------------------------------------

DEFERRED_PRIVILEGED_SUPPORT_UNITS = {
    "jasper-aec-reconcile": ROOT / "deploy/systemd/jasper-aec-reconcile.service",
    "jasper-aec-init": ROOT / "deploy/systemd/jasper-aec-init.service",
    "jasper-audio-hardware-reconcile": (
        ROOT / "deploy/systemd/jasper-audio-hardware-reconcile.service"
    ),
    "jasper-dongle-recover": ROOT / "deploy/systemd/jasper-dongle-recover.service",
    "jasper-wifi-guardian": ROOT / "deploy/systemd/jasper-wifi-guardian.service",
    "jasper-wifi-recover": ROOT / "deploy/systemd/jasper-wifi-recover.service",
    "jasper-wifi-scan-repair": (
        ROOT / "deploy/systemd/jasper-wifi-scan-repair.service"
    ),
    "jasper-grouping-reconcile": (
        ROOT / "deploy/systemd/jasper-grouping-reconcile.service"
    ),
    "jasper-identity-reconcile": (
        ROOT / "deploy/systemd/jasper-identity-reconcile.service"
    ),
    # The composite ConfigFS gadget owner replaces the retired
    # jasper-usbsink-init. It is a root oneshot of exactly that class
    # (modprobe libcomposite, write ConfigFS descriptors, rmmod the gadget
    # modules on stop) and stays root by design — no User= until a validated
    # WS1 slice moves it.
    "jasper-usbgadget": ROOT / "deploy/systemd/jasper-usbgadget.service",
    "jasper-bootloop-guard": ROOT / "deploy/systemd/jasper-bootloop-guard.service",
}

APPLE_DONGLE_UDEV_RULE = ROOT / "deploy/udev/99-jasper-apple-dongle.rules"
TIER_B_DAC_MIXER_UNITS = {
    "jasper-dac-init": ROOT / "deploy/systemd/jasper-dac-init.service",
    "jasper-headphone-monitor": (
        ROOT / "deploy/systemd/jasper-headphone-monitor.service"
    ),
}


@pytest.mark.parametrize(
    "unit,path", sorted(DEFERRED_PRIVILEGED_SUPPORT_UNITS.items()),
)
def test_privileged_support_units_stay_root_until_validated(unit, path):
    assert path.is_file(), f"{unit}: expected unit at {path}"
    assert not any(k == "User" for k, _ in _directives(path)), (
        f"{unit}: gained User= without updating the WS1 Tier-B plan and "
        "validation guard. Drop these units one vertical slice at a time; see "
        "docs/HANDOFF-privilege-separation.md Remaining WS1 scope."
    )


USBNET_DHCP_UNIT = ROOT / "deploy/systemd/jasper-usbnet-dhcp.service"


def test_usbnet_dhcp_unit_is_hardened_scoped_dnsmasq():
    """The scoped dnsmasq for the USB management network ships the hardening
    set the brief and HANDOFF-usb-gadget.md promise, and its device-activated
    lifecycle bounds (BindsTo/After/WantedBy on the usb0 device unit +
    MemoryMax + RuntimeDirectory).

    review core-5/surfaces-2: neither new root unit was pinned by any hardening
    test, which is also how the CapabilityBoundingSet privilege-drop kill
    (core-1) shipped uncaught. This is the guard.
    """
    assert USBNET_DHCP_UNIT.is_file()
    pairs = set(_directives(USBNET_DHCP_UNIT))
    keys = {k for k, _ in pairs}

    # Core sandbox.
    assert ("ProtectSystem", "strict") in pairs
    assert ("NoNewPrivileges", "true") in pairs
    assert ("ProtectKernelModules", "true") in pairs
    assert ("ProtectKernelTunables", "true") in pairs
    assert ("RestrictSUIDSGID", "true") in pairs

    # Capability bounding set: the raw DHCP caps PLUS CAP_SETUID/CAP_SETGID,
    # which the process needs to drop to nobody:nogroup (a failed drop is fatal
    # to dnsmasq — core-1). Ambient keeps only the net caps across the drop.
    boundings = [v for k, v in pairs if k == "CapabilityBoundingSet"]
    assert boundings, "jasper-usbnet-dhcp must set CapabilityBoundingSet="
    bounding = boundings[-1].split()
    for cap in ("CAP_NET_BIND_SERVICE", "CAP_NET_ADMIN", "CAP_NET_RAW",
                "CAP_SETUID", "CAP_SETGID"):
        assert cap in bounding, (
            f"{cap} must be in the CapabilityBoundingSet — without "
            "CAP_SETUID/CAP_SETGID dnsmasq's privilege drop to nobody fails "
            "fatally (review core-1)."
        )
    ambients = [v for k, v in pairs if k == "AmbientCapabilities"]
    assert ambients, "jasper-usbnet-dhcp must set AmbientCapabilities="
    ambient = ambients[-1].split()
    assert "CAP_SETUID" not in ambient and "CAP_SETGID" not in ambient, (
        "CAP_SETUID/CAP_SETGID must NOT be ambient — the process must not "
        "retain the ability to change identity after dropping to nobody."
    )

    # Bounded + device-activated lifecycle.
    assert "MemoryMax" in keys, "the DHCP server must be MemoryMax-bounded"
    assert "RuntimeDirectory" in keys, "lease file lives in a tmpfs RuntimeDirectory"
    assert ("BindsTo", "sys-subsystem-net-devices-usb0.device") in pairs
    assert ("WantedBy", "sys-subsystem-net-devices-usb0.device") in pairs
    after = [v for k, v in pairs if k == "After"]
    assert any("sys-subsystem-net-devices-usb0.device" in v for v in after), (
        "the unit must order After= the usb0 device unit"
    )


@pytest.mark.parametrize("unit,path", sorted(TIER_B_DAC_MIXER_UNITS.items()))
def test_dac_mixer_units_run_as_recon_user(unit, path):
    """WS1 Tier-B DAC mixer slice: the service/daemon pin paths are non-root.

    The matching installer contract is below. The udev RUN+= fast path is tested
    separately because it deliberately remains root-owned for immediate hotplug
    recovery.
    """
    pairs = set(_directives(path))
    assert ("User", "jasper-recon") in pairs, (
        f"{unit}: Apple DAC mixer pinning should run as jasper-recon."
    )
    assert ("Group", "jasper") in pairs, (
        f"{unit}: jasper-recon primary group must stay jasper."
    )
    assert ("SupplementaryGroups", "audio") in pairs, (
        f"{unit}: amixer needs /dev/snd/controlC* via the audio group."
    )
    assert ("CapabilityBoundingSet", "") in pairs, (
        f"{unit}: must set CapabilityBoundingSet= (empty) to drop all caps."
    )
    assert ("SystemCallFilter", "@system-service") in pairs, (
        f"{unit}: must set SystemCallFilter=@system-service."
    )
    assert ("NoNewPrivileges", "true") in pairs, (
        f"{unit}: must set NoNewPrivileges=true."
    )


def test_install_creates_recon_user_for_dac_mixer_slice():
    sh = SERVICE_USERS_SH.read_text()
    lines = sh.splitlines()
    useradd = [ln for ln in lines if "useradd" in ln and " jasper-recon" in ln]
    assert useradd, "service-users.sh must create jasper-recon for DAC mixer units"
    assert "-g jasper" in useradd[0], "jasper-recon primary group must be jasper"
    assert "-G audio" in useradd[0], (
        "jasper-recon must be in supplementary group audio for amixer."
    )
    assert "usermod -aG audio jasper-recon" in sh, (
        "upgrade path must add audio to an existing jasper-recon user."
    )


def test_apple_dongle_udev_mixer_fast_path_remains_root_exception():
    """The DAC mixer-pin slice must account for the udev hotplug fast path too.

    `jasper-dac-init.service` is not the only root-owned Headphone=100% writer:
    the Apple dongle udev rule also runs amixer on sound-card add, and udev
    RUN+= executes as root. The service and monitor paths now run as
    jasper-recon, but this root fast path stays explicit until a fixed helper or
    systemd oneshot replacement is hardware-validated against real replug.
    """
    text = APPLE_DONGLE_UDEV_RULE.read_text(encoding="utf-8")
    assert (
        'RUN+="/usr/bin/amixer -c $env{JASPER_DONGLE_CARDNUM} '
        'sset Headphone 100%% unmute"'
    ) in text
    assert (
        'ACTION=="add", SUBSYSTEM=="usb", DEVTYPE=="usb_device", '
        'ATTR{idVendor}=="05ac", ATTR{idProduct}=="110a", '
        'ATTR{power/control}="on"'
    ) in text
    assert 'SUBSYSTEM=="usb", ATTRS{idVendor}=="05ac"' not in text


def test_camilla_unit_rate_limits_external_log_floods():
    """External Camilla WARN floods must not consume persistent journal history."""
    text = (ROOT / "deploy/systemd/jasper-camilla.service").read_text(
        encoding="utf-8"
    )
    assert "LogRateLimitIntervalSec=60s" in text
    assert "LogRateLimitBurst=120" in text


def test_snapclient_unit_rate_limits_leader_offline_log_floods():
    """Optional grouping must not let a refused-connection loop eat forensics."""
    text = (ROOT / "deploy/systemd/jasper-snapclient.service").read_text(
        encoding="utf-8"
    )
    assert "LogRateLimitIntervalSec=60s" in text
    assert "LogRateLimitBurst=30" in text


# The root audio daemons whose UDS the now-non-root voice/mux must connect to.
# A UNIX socket needs WRITE permission to connect(), so these stay root but join
# the `jasper` group with UMask=0007 — making their umask-derived sockets
# root:jasper 0770 instead of 0755 (which only root could connect to). Removing
# either directive silently re-bricks the non-root voice/mux (the exact failure
# caught during 3b-1 hardware validation), so it gets its own guard.
_CROSS_USER_IPC_DAEMONS = {
    "jasper-fanin": ROOT / "deploy/systemd/jasper-fanin.service",
    "jasper-outputd": ROOT / "deploy/systemd/jasper-outputd.service",
}


@pytest.mark.parametrize("unit,path", sorted(_CROSS_USER_IPC_DAEMONS.items()))
def test_cross_user_ipc_socket_contract(unit, path):
    pairs = set(_directives(path))
    assert ("Group", "jasper") in pairs, (
        f"{unit}: must join Group=jasper so its UDS is group-`jasper` (the "
        "non-root jasper-voice/jasper-mux connect to it)."
    )
    assert ("UMask", "0007") in pairs, (
        f"{unit}: must set UMask=0007 so its bind()'d socket is 0770 (group "
        "write) — connect() needs write permission; the umask-default 0755 "
        "only let root connect, which crash-looped non-root voice in 3b-1."
    )
    assert ("RuntimeDirectoryMode", "0750") in pairs, (
        f"{unit}: RuntimeDirectory must be 0750 (root:jasper) so the `jasper` "
        "group can traverse to the socket."
    )


# WS1 — the shared-state writers set UMask=0007 so files they CREATE in
# /var/lib/jasper are group-`jasper`-writable (0660), not the umask-default 0644.
# usage.db / wake-events.sqlite3 / timers.db / speaker_volume.json are written by
# more than one of these same-group daemons; jasper-voice (the sole
# StateDirectory=jasper owner since S2) re-chowns the tree to jasper-voice on its
# restart, so the others (same `jasper` group) write shared files via the group
# bit — a 0644 file would be group-read-only and the non-owner would hit "attempt
# to write a readonly database" (the 2026-06-19 incident). Same directive as the
# fanin/outputd socket UMask contract above; here it's about the files, not the socket.
_SHARED_STATE_WRITERS = {
    "jasper-voice": TIER_A["jasper-voice"],
    "jasper-mux": TIER_A["jasper-mux"],
    "jasper-control": TIER_A["jasper-control"],
}


@pytest.mark.parametrize("unit,path", sorted(_SHARED_STATE_WRITERS.items()))
def test_shared_state_writers_set_group_write_umask(unit, path):
    pairs = set(_directives(path))
    assert ("UMask", "0007") in pairs, (
        f"{unit}: must set UMask=0007 so files it creates in /var/lib/jasper are "
        "group-`jasper`-writable (0660). Without it shared state lands 0644 and a "
        "non-owner same-group daemon hits 'attempt to write a readonly database' "
        "(the 2026-06-19 incident). docs/HANDOFF-privilege-separation.md."
    )


def test_single_statedirectory_owner():
    """S2: jasper-voice is the SOLE StateDirectory=jasper owner. mux dropping its
    StateDirectory is what removes the owner-flip re-chown race; if mux ever
    re-declares it, the race (and the 2026-06-19 readonly-DB class) returns."""
    voice = set(_directives(TIER_A["jasper-voice"]))
    mux = _directives(TIER_A["jasper-mux"])
    assert ("StateDirectory", "jasper") in voice, (
        "jasper-voice must remain the StateDirectory=jasper owner (it creates + "
        "owns /var/lib/jasper for every group-`jasper` daemon)."
    )
    assert ("StateDirectory", "jasper") not in set(mux), (
        "jasper-mux must NOT declare StateDirectory=jasper (S2: single owner). "
        "Co-ownership makes systemd re-chown /var/lib/jasper to whichever daemon "
        "restarted last, flipping ownership and breaking the non-owner's writes."
    )
    mux_rwp = " ".join(v for k, v in mux if k == "ReadWritePaths")
    assert "/var/lib/jasper" in mux_rwp, (
        "with no StateDirectory, mux must reach /var/lib/jasper via ReadWritePaths "
        "(its source pin + speaker_volume.json writes under ProtectSystem=strict)."
    )
    # The USB lean-lane (which live-swapped a CamillaDSP config from mux) was
    # deleted in the USB dead-pipeline sweep, so mux no longer writes
    # /var/lib/camilladsp/configs and must NOT keep that write path (tightening
    # attack surface under ProtectSystem=strict).
    assert "/var/lib/camilladsp/configs" not in mux_rwp, (
        "mux no longer live-swaps CamillaDSP configs (lean lane deleted); drop "
        "/var/lib/camilladsp/configs from its ReadWritePaths."
    )
