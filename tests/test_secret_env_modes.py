# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WS1 — pin env-file modes for cross-user daemon reads.

The broad `/var/lib/jasper` state files that jasper-control still fresh-reads
must be 0640 for the shared `jasper` group. Phase 4a/4b moved the real secrets
into sibling compartments (`jasper-secrets`, `jasper-intsecrets`); those files
still use mode 0640, but their group membership and relocation are pinned by the
Phase 4 hardening/migration tests instead of this broad-widening guard.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from jasper.web import _common

ROOT = Path(__file__).resolve().parents[1]


def test_secret_env_mode_is_group_readable_0640():
    assert _common.SECRET_ENV_MODE == 0o640, (
        "cross-daemon secret env files must be 0640; the owning group is set "
        "by the broad state dir or the Phase 4 compartment."
    )


def test_write_env_file_secret_mode_is_group_readable(tmp_path):
    """Behavioural: a file written with SECRET_ENV_MODE is group-readable."""
    p = tmp_path / "secret.env"
    _common.write_env_file(str(p), {"K": "v"}, mode=_common.SECRET_ENV_MODE)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o640, f"expected 0640, got {oct(mode)}"
    assert mode & stat.S_IRGRP, "group must be able to read the secret env file"
    assert not (mode & (stat.S_IROTH | stat.S_IWOTH)), "world must have no access"


def test_write_env_file_default_stays_owner_only():
    """The DEFAULT mode stays 0600 — only the explicitly-flagged secret files a
    cross-user daemon reads are widened, not every env file."""
    assert "mode: int = 0o600" in (
        ROOT / "jasper/web/_common.py"
    ).read_text(encoding="utf-8"), "write_env_file default must stay 0o600"


# Each secret wizard must WRITE its creds file with SECRET_ENV_MODE (forward
# fix), so a save doesn't revert it to the 0600 default and re-break the drop.
SECRET_WIZARDS = {
    "jasper/web/voice_setup.py": "voice_provider.env",
    "jasper/web/spotify_setup.py": "spotify_credentials.env",
    "jasper/web/google_setup.py": "google_credentials.env",
    "jasper/web/transit_setup.py": "google_routes.env",
    "jasper/web/home_assistant_setup.py": "home_assistant.env",
}


def test_secret_wizards_use_secret_env_mode():
    for rel, fname in SECRET_WIZARDS.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "SECRET_ENV_MODE" in src, (
            f"{rel} must write {fname} with SECRET_ENV_MODE (0640 group-readable)"
        )
        # No secret wizard should still pass mode=0o600 to write_env_file.
        assert "mode=0o600" not in src, (
            f"{rel} still writes a secret env file at 0o600 — widen to "
            "SECRET_ENV_MODE so the non-root jasper-control can read it."
        )


def test_install_widens_secret_env_on_upgrade():
    """The upgrade path: install must still group-widen the broad files
    jasper-control reads directly. Phase 4 compartment files are handled by
    their migration helpers instead."""
    full = (ROOT / "deploy/lib/install/env-migrations.sh").read_text(encoding="utf-8")
    assert "widen_control_secret_env_modes() {" in full, (
        "env-migrations.sh must define widen_control_secret_env_modes"
    )
    # Scope the checks to the widen function's body (it's the last function in
    # the file), so an unrelated reference (migrate_wifi_guardian writes the
    # stash) doesn't false-match.
    mig = full.split("widen_control_secret_env_modes() {", 1)[1]
    loop_match = re.search(r"for f in (?P<items>.*?); do", mig, flags=re.S)
    assert loop_match is not None, "widening must iterate an explicit file list"
    widened_files = set(loop_match.group("items").replace("\\", " ").split())
    for fname in (
        "voice_provider.env",
        # NOTE: google_credentials.env is NOT in this list anymore — WS1 Phase 4a
        # moved it to the group-`jasper-secrets` compartment (voice+web only),
        # so it is deliberately NOT widened to the broad `jasper` group. Its
        # relocation/perms are pinned by test_systemd_hardening's
        # test_secrets_compartment_phase4a + test_install_creates_google_dir_setgid.
        # WS1 Phase 4b likewise moved spotify_credentials.env +
        # home_assistant.env to the group-`jasper-intsecrets` compartment
        # (voice/control/mux/web only), pinned by test_secrets_compartment_phase4b.
        "control_token",
        "household_secret",
        # Non-secret state jasper-control also reads off disk for /state:
        "sound_profile.json",
        "sound_settings.json",
    ):
        assert fname in widened_files, f"widening loop must cover {fname}"
    for fname in (
        "google_credentials.env",
        "google_routes.env",
        "spotify_credentials.env",
        "home_assistant.env",
    ):
        assert fname not in widened_files, (
            f"{fname} moved to a Phase 4 compartment and must not be "
            "re-widened to the broad jasper group"
        )
    assert "jasper_env" in mig and "chgrp jasper" in mig, (
        "widening must still chgrp /etc/jasper/jasper.env"
    )
    assert "chgrp jasper" in mig and "chmod 0640" in mig, (
        "widening must chgrp jasper + chmod 0640 the files"
    )
    assert '[[ -L "${path}" ]]' in mig, (
        "widening must refuse symlinks in the group-writable state directory"
    )
    # The WiFi PSK stash is DELIBERATELY excluded — jasper-control needs only the
    # SSID (not the PSK value), so the PSK stays owner-only 0600 (least privilege).
    assert "wifi_guardian.env" not in mig, (
        "wifi_guardian.env (PSK) must NOT be group-widened — jasper-control "
        "derives the SSID without the PSK"
    )

    sh = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    # Called in BOTH main() profiles (full + streambox).
    assert sh.count("widen_control_secret_env_modes") >= 2, (
        "widen_control_secret_env_modes must be called in both main() paths"
    )


def test_widen_control_secret_env_modes_skips_symlinks(tmp_path: Path):
    """Behavioural guard for the root migration in a group-writable state dir."""
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    regular = state_dir / "control_token"
    regular.write_text("token\n")
    outside = tmp_path / "outside_secret"
    outside.write_text("do-not-touch\n")
    link = state_dir / "household_secret"
    link.symlink_to(outside)
    ops_log = tmp_path / "ops.log"

    lib = ROOT / "deploy/lib/install/env-migrations.sh"
    helper = subprocess.run(
        ["bash", "-c", rf"sed -n '/^widen_control_secret_env_modes()/,/^}}/p' '{lib}'"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "widen_control_secret_env_modes()" in helper
    stubs = r"""
getent() { return 0; }
chgrp() { printf 'chgrp:%s\n' "${@: -1}" >> "${OPS_LOG}"; }
chmod() { printf 'chmod:%s\n' "${@: -1}" >> "${OPS_LOG}"; }
"""
    proc = subprocess.run(
        ["/bin/bash", "-c", f"{stubs}\n{helper}\nwiden_control_secret_env_modes"],
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "ENV_DIR": str(env_dir),
            "STATE_DIR": str(state_dir),
            "OPS_LOG": str(ops_log),
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    ops = ops_log.read_text()
    assert str(regular) in ops
    assert str(link) not in ops
    assert str(outside) not in ops
    assert f"skipping symlink {link}" in proc.stdout


def test_widen_control_secret_env_modes_fails_if_jasper_env_chgrp_fails(
    tmp_path: Path,
):
    """The installer must not claim jasper.env is readable when chgrp failed."""
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text("JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n")

    lib = ROOT / "deploy/lib/install/env-migrations.sh"
    helper = subprocess.run(
        ["bash", "-c", rf"sed -n '/^widen_control_secret_env_modes()/,/^}}/p' '{lib}'"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    stubs = r"""
getent() { return 0; }
chgrp() {
  if [[ "${@: -1}" == "${ENV_DIR}/jasper.env" ]]; then
    return 1
  fi
  return 0
}
chmod() { return 0; }
"""
    proc = subprocess.run(
        ["/bin/bash", "-c", f"{stubs}\n{helper}\nwiden_control_secret_env_modes"],
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "ENV_DIR": str(env_dir),
            "STATE_DIR": str(state_dir),
        },
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert f"failed to chgrp {jasper_env} to jasper" in proc.stderr
