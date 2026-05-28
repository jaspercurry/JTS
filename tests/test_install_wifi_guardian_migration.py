"""Test the install.sh `migrate_wifi_guardian` shell helper.

The helper seeds /var/lib/jasper/wifi_guardian.env from the currently-
active NM WiFi profile during install, covering the SSH-driven setup
case where the operator brought up WiFi via raspi-config or `nmcli`
directly before ever opening the /wifi/ wizard.

We exercise it by sourcing install.sh under bash with a fake nmcli on
PATH and JTS env vars (STATE_DIR, etc.) pointing at tmp_path.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "deploy" / "install.sh"


def _write_fake_nmcli(
    bin_dir: Path,
    *,
    active: str,
    secrets: str,
) -> None:
    """Write a fake nmcli that responds to the two queries the
    migration helper makes.

    `active`: response for `nmcli -t -f NAME,TYPE connection show --active`
    `secrets`: response for `nmcli -s -t -f 802-11-... connection show NAME`
    """
    fake = bin_dir / "nmcli"
    fake.write_text(rf"""#!/bin/bash
# Detect whether `--active` is in argv.
active_flag=0
for a in "$@"; do
    [[ "$a" == "--active" ]] && active_flag=1
done
if [[ "$active_flag" == "1" ]]; then
    cat <<'NMCLI_ACTIVE'
{active}
NMCLI_ACTIVE
    exit 0
fi

# Show-secrets variant for `connection show <NAME>`.
secrets_flag=0
for a in "$@"; do
    [[ "$a" == "-s" ]] && secrets_flag=1
done
if [[ "$secrets_flag" == "1" ]]; then
    cat <<'NMCLI_SECRETS'
{secrets}
NMCLI_SECRETS
    exit 0
fi
exit 0
""")
    fake.chmod(0o755)


def _run_migrate(
    tmp_path: Path,
    *,
    active: str = "",
    secrets: str = "",
    pre_stash: str | None = None,
    with_nmcli: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Source install.sh and run migrate_wifi_guardian against tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if pre_stash is not None:
        (state_dir / "wifi_guardian.env").write_text(pre_stash)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("awk", "cat", "chmod", "mktemp", "mv"):
        target = shutil.which(name)
        assert target is not None, f"{name} is required for this test"
        (bin_dir / name).symlink_to(target)
    if with_nmcli:
        _write_fake_nmcli(bin_dir, active=active, secrets=secrets)

    # Build a shell wrapper that sources only the helper definitions
    # (NOT the full install run — install.sh isn't designed to be
    # sourced in isolation since it has side-effecting top-level
    # statements). We extract the function body via sed.
    helper = subprocess.run(
        ["bash", "-c",
         rf"sed -n '/^migrate_wifi_guardian()/,/^}}/p' '{INSTALL_SH}'"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "migrate_wifi_guardian()" in helper, (
        "couldn't extract helper from install.sh — has the function "
        "been renamed or restructured?"
    )

    # Run the helper with a hermetic PATH so a real Pi's /usr/bin/nmcli
    # cannot leak into the "nmcli missing" test case.
    path = str(bin_dir)
    env = {
        "PATH": path,
        "STATE_DIR": str(state_dir),
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{helper}\nmigrate_wifi_guardian"],
        env=env, capture_output=True, text=True,
    )


def _stash_lines(tmp_path: Path) -> dict[str, str]:
    """Parse the resulting stash into a dict for assertions."""
    p = tmp_path / "state" / "wifi_guardian.env"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_migrate_wifi_guardian_seeds_from_active_profile(tmp_path):
    """The canonical SSH-driven setup case: operator ran `nmcli connect`
    via SSH; install.sh runs later and seeds the guardian stash from
    the live NM profile."""
    proc = _run_migrate(
        tmp_path,
        active="Home:802-11-wireless\n",
        secrets=(
            "802-11-wireless.ssid:Home\n"
            "802-11-wireless-security.psk:homepsk\n"
            "802-11-wireless-security.key-mgmt:wpa-psk\n"
        ),
    )
    assert proc.returncode == 0, proc.stderr
    fields = _stash_lines(tmp_path)
    assert fields["JASPER_WIFI_SSID"] == "Home"
    assert fields["JASPER_WIFI_PSK"] == "homepsk"
    assert fields["JASPER_WIFI_KEY_MGMT"] == "wpa-psk"


def test_migrate_wifi_guardian_seeded_file_is_mode_0600(tmp_path):
    """The stash contains the PSK — must be root-readable only."""
    _run_migrate(
        tmp_path,
        active="Home:802-11-wireless\n",
        secrets=(
            "802-11-wireless.ssid:Home\n"
            "802-11-wireless-security.psk:p\n"
            "802-11-wireless-security.key-mgmt:wpa-psk\n"
        ),
    )
    stash = tmp_path / "state" / "wifi_guardian.env"
    mode = os.stat(stash).st_mode & 0o777
    assert mode == 0o600


def test_migrate_wifi_guardian_idempotent_when_stash_exists(tmp_path):
    """Stash already exists → no-op. Don't overwrite operator's
    wizard-saved file just because nmcli has a different opinion."""
    pre = (
        "JASPER_WIFI_SSID=WizardNet\n"
        "JASPER_WIFI_PSK=wizardpsk\n"
        "JASPER_WIFI_KEY_MGMT=wpa-psk\n"
    )
    _run_migrate(
        tmp_path,
        active="DifferentNet:802-11-wireless\n",
        secrets=(
            "802-11-wireless.ssid:DifferentNet\n"
            "802-11-wireless-security.psk:differentpsk\n"
            "802-11-wireless-security.key-mgmt:wpa-psk\n"
        ),
        pre_stash=pre,
    )
    fields = _stash_lines(tmp_path)
    # Stash kept the wizard-saved values; the active profile is ignored.
    assert fields["JASPER_WIFI_SSID"] == "WizardNet"
    assert fields["JASPER_WIFI_PSK"] == "wizardpsk"


def test_migrate_wifi_guardian_noop_without_nmcli(tmp_path):
    """nmcli missing → no-op, no error. Ethernet-only / headless-CI hosts."""
    proc = _run_migrate(tmp_path, with_nmcli=False)
    assert proc.returncode == 0
    assert not (tmp_path / "state" / "wifi_guardian.env").exists()


def test_migrate_wifi_guardian_noop_without_active_wifi(tmp_path):
    """No active wifi connection → no-op (Ethernet-only Pi). Don't
    create an empty stash."""
    proc = _run_migrate(
        tmp_path,
        active="eth0:802-3-ethernet\n",
    )
    assert proc.returncode == 0
    assert not (tmp_path / "state" / "wifi_guardian.env").exists()


def test_migrate_wifi_guardian_skips_enterprise(tmp_path):
    """key_mgmt=wpa-eap → don't write a stash the guardian would
    refuse to act on."""
    proc = _run_migrate(
        tmp_path,
        active="EnterpriseNet:802-11-wireless\n",
        secrets=(
            "802-11-wireless.ssid:EnterpriseNet\n"
            "802-11-wireless-security.psk:\n"
            "802-11-wireless-security.key-mgmt:wpa-eap\n"
        ),
    )
    assert proc.returncode == 0
    assert not (tmp_path / "state" / "wifi_guardian.env").exists()


def test_migrate_wifi_guardian_psk_not_in_stdout(tmp_path):
    """The success log line must not include the PSK — install.sh
    output streams to journalctl during deploy and ends up in the
    install transcript pasted into bug reports."""
    secret_psk = "ultra-secret-install-time-psk"
    proc = _run_migrate(
        tmp_path,
        active="Home:802-11-wireless\n",
        secrets=(
            f"802-11-wireless.ssid:Home\n"
            f"802-11-wireless-security.psk:{secret_psk}\n"
            f"802-11-wireless-security.key-mgmt:wpa-psk\n"
        ),
    )
    assert proc.returncode == 0
    # The PSK is in the file (mode 0600) but not in stdout/stderr.
    assert secret_psk not in proc.stdout
    assert secret_psk not in proc.stderr
    # The file does contain it.
    assert secret_psk in (tmp_path / "state" / "wifi_guardian.env").read_text()
