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
import tempfile
from dataclasses import dataclass
from pathlib import Path
logger = logging.getLogger(__name__)
DEFAULT_PATH = "/var/lib/jasper/wifi_guardian.env"
_KEY_SSID = "JASPER_WIFI_SSID"
_KEY_PSK = "JASPER_WIFI_PSK"
_KEY_MGMT = "JASPER_WIFI_KEY_MGMT"
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
    # Strip a single layer of matched quotes — nmcli output can wrap
    # PSKs containing spaces in quotes when migrated in via the
    # install.sh helper. Don't strip whitespace from the value itself;
    # WPA PSKs are 8-63 ASCII chars and trailing/leading spaces are
    # technically legal even if uncommon.
    value = value.rstrip("\r\n")
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
    """Best-effort atomic write with durability beyond ``os.replace``.
    Steps:
      1. Write into a tempfile in the same directory (atomic rename
         needs same-filesystem source + target).
      2. ``os.fsync`` the tempfile FD before close → contents on disk.
      3. ``chmod 0600`` (PSK is in the file).
      4. ``os.replace`` (atomic on POSIX same-FS).
      5. ``os.fsync`` the *parent directory* FD → the rename itself
         is on disk. This is the step ``mic_mute_persistence`` skips;
         we need it because the whole point of this stash is recovery
         from filesystem loss.
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
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".wifi_guardian.", suffix=".tmp", dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # fsync the parent directory so the rename itself is durable. On
    # ext4 with default ``data=ordered`` this isn't strictly necessary
    # for the file *contents* (they're already on disk from step 2),
    # but the directory entry pointing at the new inode lives in the
    # parent dir's data block — without this fsync, a dirty shutdown
    # immediately after wizard save can leave the rename rolled back.
    # That's the exact failure class the guardian exists to recover
    # from; defending against it on the write path too is consistent.
    try:
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as e:
        # Best-effort — some filesystems (FAT32, tmpfs in tests)
        # don't support directory fsync. The file contents are still
        # on disk from step 2; only the rename durability degrades.
        logger.debug(
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
