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
    # are `bluetooth` (BlueZ Adapter1 Alias for the /speaker rename — a D-Bus
    # policy grant) and `systemd-journal` (journalctl -k Wi-Fi diagnostics) and,
    # since 4a, `jasper-secrets` (the wizards write + render the secret files). No
    # CAP_NET_ADMIN — the NL80211 scan-repair degrades fail-soft.
    "jasper-web": (
        "jasper-web",
        {"bluetooth", "systemd-journal", "jasper-secrets", "jasper-intsecrets"},
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
    """WS1 Phase 4a — the high-value secrets (LLM API keys + Google client
    secret/token tree) live in the group-`jasper-secrets` compartment, readable
    only by jasper-voice + jasper-web. Pin: (1) the group is created; (2) voice +
    web source voice_keys.env + the RELOCATED google_credentials.env; (3) only
    jasper-web (which WRITES the compartment via the /voice + /google wizards)
    gets it in ReadWritePaths — jasper-voice only READS (keys via EnvironmentFile,
    Google tree via the group), so under ProtectSystem=strict it needs NO write
    grant; (4) the daemons that must NOT see the keys (mux/control/input) don't
    source voice_keys.env."""
    secret_keys = "/var/lib/jasper-secrets/voice_keys.env"
    secret_google = "/var/lib/jasper-secrets/google_credentials.env"

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

    # (4) the excluded daemons must NOT source the secret-keys file.
    for unit in ("jasper-mux", "jasper-control", "jasper-input"):
        pairs = list(_directives(TIER_A[unit]))
        envfiles = " ".join(v for k, v in pairs if k == "EnvironmentFile")
        assert secret_keys not in envfiles, (
            f"{unit} must NOT source {secret_keys} (Phase 4a compartmentalization)"
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
