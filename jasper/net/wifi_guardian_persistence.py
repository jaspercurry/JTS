# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist + restore the WiFi profile guardian stash.
2026-05-23 incident: a USB-C power yank during a power-splitter swap
left the Pi's root ext4 partition with an in-flight write to
``/etc/NetworkManager/system-connections/<SSID>.nmconnection``. Journal
recovery on the dirty mount discarded the file entirely. The Pi rebooted
into a state with no WiFi profile at all, was unreachable on the LAN, and
required HDMI + USB-keyboard console recovery (~1 hour).
The behavioural fix — graceful shutdown — is being adopted separately.
This module is the software floor under it: a wizard-owned stash of
``(SSID, PSK, key_mgmt)`` that lets ``jasper-wifi-guardian`` recreate the
NetworkManager keyfile on next boot if it ever disappears for any reason.
File format mirrors ``aec_mode.env`` / ``wake_model.env`` / ``mic_mute.env``
(env-var style):
    JASPER_WIFI_SSID=MyNetwork
    JASPER_WIFI_PSK=correct horse battery staple
    JASPER_WIFI_KEY_MGMT=wpa-psk
Written atomically (tempfile + rename + fsync) so a crash mid-write leaves
either the old contents or the new ones — never half a file. The
``fsync(parent_dir_fd)`` after rename is the meaningful delta from
``mic_mute_persistence``: this file is the *recovery* path for filesystem
loss, so durability of the rename is the whole point. Cost is 5-30 ms on
slow SD cards, paid on wizard save only.
Failure mode: a missing, unreadable, or malformed file means the guardian
no-ops. The Pi keeps booting; doctor surfaces the drift; the wizard fixes
it on the next save. No silent stomping of working state.
PSK never appears in any log line emitted by this module — values are
referenced by key name only, and read/write errors log the path, not the
contents.
"""
from __future__ import annotations
import logging
import os
import tempfile  # noqa: F401 — kept so tests can patch the shared
                 # tempfile.mkstemp that atomic_write_text calls internally
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text, fsync_directory

logger = logging.getLogger(__name__)
DEFAULT_PATH = "/var/lib/jasper/wifi_guardian.env"
_KEY_SSID = "JASPER_WIFI_SSID"
_KEY_PSK = "JASPER_WIFI_PSK"
_KEY_MGMT = "JASPER_WIFI_KEY_MGMT"
# Field order for `nmcli -t -f ... connection show --active`. NAME is LAST
# because it is the only variable-content field: a real SSID may contain a
# literal colon (`Home:2.4G`), which nmcli escapes as `\:` but which a
# NAME-first split still mis-parses into the wrong fields. TYPE and DEVICE
# never contain a colon, so splitting off exactly two fields leaves NAME as the
# remainder. Mirrors deploy/bin/jasper-wifi-guardian's TYPE-first order.
NMCLI_ACTIVE_WIFI_FIELDS = "TYPE,DEVICE,NAME"
_WIFI_CONNECTION_TYPES = ("802-11-wireless", "wifi")


def nm_unescape(value: str) -> str:
    r"""Reverse ``nmcli -t``'s ``\:`` escaping of literal colons in values.

    A literal backslash would itself be escaped as ``\\``, but SSIDs with
    backslashes are not a real-world case, so — matching the bash guardian's
    ``nm_unescape`` — only the colon escape is reversed.
    """
    return value.replace("\\:", ":")


def active_wifi_connection(terse_output: str) -> tuple[str | None, str | None]:
    """Parse ``nmcli -t -f TYPE,DEVICE,NAME connection show --active`` output.

    Returns ``(profile_name, device)`` for the first active Wi-Fi row, or
    ``(None, None)`` when no row is Wi-Fi.
    """
    for raw in terse_output.splitlines():
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[0] in _WIFI_CONNECTION_TYPES:
            return nm_unescape(parts[2]) or None, parts[1] or None
    return None, None


@dataclass(frozen=True)
class WifiStash:
    """An immutable snapshot of the stashed WiFi profile intent.
    ``key_mgmt`` mirrors NM's ``802-11-wireless-security.key-mgmt``
    field: ``wpa-psk`` for WPA2, ``sae`` for WPA3 (nmcli figures it
    out from the beacon at connect time either way), ``none`` for open
    networks. ``wpa-eap`` is rejected upstream by the wizard hooks —
    enterprise auth is explicitly out of scope.
    """
    ssid: str
    psk: str
    key_mgmt: str
def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value
def read_stash(path: str | os.PathLike) -> WifiStash | None:
    """Read the stashed WiFi intent from disk.
    Returns ``None`` for any of: missing file, unreadable file,
    SSID key absent or empty, or ``key_mgmt`` set to ``wpa-eap``
    (the wizard never writes this, but a hand-edited file might
    and the guardian should not attempt to recreate it).
    Never logs the PSK on any code path.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("wifi guardian persistence: read %s failed (%s)", p, e)
        return None
    ssid: str | None = None
    psk: str = ""
    key_mgmt: str = ""
    for line in text.splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == _KEY_SSID:
            ssid = value
        elif key == _KEY_PSK:
            psk = value
        elif key == _KEY_MGMT:
            key_mgmt = value
    if not ssid:
        return None
    # ``wpa-eap`` is rejected at read-time so a stash written by hand
    # never tricks the guardian into trying ``nmcli dev wifi connect`` on
    # an enterprise SSID. The wizard's own write path also rejects it.
    if key_mgmt == "wpa-eap":
        logger.info(
            "wifi guardian persistence: %s has key_mgmt=wpa-eap "
            "(enterprise) — ignoring; guardian will skip", p,
        )
        return None
    # Default to ``none`` when the key is absent. This matches NM's
    # treatment of open networks. nmcli detects the actual security
    # mode from the beacon at connect time, so the field is mostly
    # advisory; we use it to decide whether to pass ``password ARG``.
    return WifiStash(ssid=ssid, psk=psk, key_mgmt=key_mgmt or "none")
def write_stash(
    path: str | os.PathLike,
    ssid: str,
    psk: str,
    key_mgmt: str,
) -> None:
    """Atomic write via ``atomic_io``, plus durability beyond ``os.replace``.

    ``atomic_write_text`` covers tempfile-in-same-dir, ``chmod 0600`` before
    the rename (PSK is in the file), and ``os.replace``. The extra step here
    — ``fsync`` on the *parent directory* — is the delta from
    ``mic_mute_persistence``: without it, a dirty shutdown right after a
    wizard save can roll back the rename even though the file's own content
    already landed on disk (ext4 ``data=ordered`` writes a rename's data
    before its metadata commit). This stash exists specifically to survive
    filesystem loss, so that gap is the one worth closing.

    Raises ``ValueError`` for inputs the guardian won't act on
    (empty SSID, ``wpa-eap`` enterprise auth). Logs and re-raises
    OSError so callers can surface "we couldn't write the stash" in
    the wizard response without crashing the connect itself.
    Never logs the PSK on any code path.
    """
    if not ssid:
        raise ValueError("ssid must be non-empty")
    if key_mgmt == "wpa-eap":
        # The wizard explicitly defers enterprise. Reject at write
        # time too — defensive matching of the read-side filter.
        raise ValueError("wpa-eap (enterprise) is out of scope for the guardian")
    p = Path(path)
    body = (
        f"{_KEY_SSID}={ssid}\n"
        f"{_KEY_PSK}={psk}\n"
        f"{_KEY_MGMT}={key_mgmt or 'none'}\n"
    )
    # group_from_parent=False: this is a root-only secret, not a
    # group-readable state file (mode 0600 already excludes the group).
    atomic_write_text(p, body, mode=0o600, group_from_parent=False)
    try:
        fsync_directory(p.parent)
    except OSError as e:
        # Any error only degrades rename durability: the file contents are
        # already on disk, and the wizard's connect must not fail.
        logger.warning(
            "wifi guardian persistence: parent fsync on %s failed (%s) — "
            "contents written, rename durability degraded",
            p.parent, e,
        )
def clear_stash(path: str | os.PathLike) -> None:
    """Remove the stash file if present. Used by the wizard's Forget
    handler when the operator forgets the SSID the stash points at.
    Missing-file is success — same semantics as ``rm -f``.
    """
    p = Path(path)
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning(
            "wifi guardian persistence: clear %s failed (%s)", p, e,
        )
