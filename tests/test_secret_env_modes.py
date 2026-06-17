"""WS1 Phase 3b-2 — pin the group-`jasper` widening of the secret env files a
non-root jasper-control (and the jasper-doctor it spawns) reads off disk.

jasper-control drops to a non-root user; its /system/diagnostics spawns
`jasper-doctor`, which fresh-reads EVERY env_load.ENV_FILES path (incl. the
provider API keys + Google secret + HA token via Config.from_env), and its
/state handler fresh-reads home_assistant.env (the HA bearer token) directly.
For those reads to keep working, the wizard-written secret files must be 0640
group `jasper` (not the 0600 owner-only default). If a future edit reverts a
writer to 0600, the drop silently breaks /state + the doctor (they degrade to
"not configured"), so this test guards the contract — see the repo's
"pin promises with tests" rule and docs/HANDOFF-privilege-separation.md.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from jasper.web import _common

ROOT = Path(__file__).resolve().parents[1]


def test_secret_env_mode_is_group_readable_0640():
    assert _common.SECRET_ENV_MODE == 0o640, (
        "secret env files must be 0640 (group `jasper` read) so the non-root "
        "jasper-control + spawned jasper-doctor can read them."
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
    "jasper/web/home_assistant_setup.py": "home_assistant.env",
}


def test_secret_wizards_use_secret_env_mode():
    for rel, fname in SECRET_WIZARDS.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "SECRET_ENV_MODE" in src, (
            f"{rel} must write {fname} with SECRET_ENV_MODE (0640 group jasper)"
        )
        # No secret wizard should still pass mode=0o600 to write_env_file.
        assert "mode=0o600" not in src, (
            f"{rel} still writes a secret env file at 0o600 — widen to "
            "SECRET_ENV_MODE so the non-root jasper-control can read it."
        )


def test_install_widens_secret_env_on_upgrade():
    """The upgrade path: install must group-widen the secret files (an older
    build wrote them 0600) AND chgrp jasper.env — else the drop breaks /state +
    the doctor on existing Pis that never re-save a wizard."""
    full = (ROOT / "deploy/lib/install/env-migrations.sh").read_text(encoding="utf-8")
    assert "widen_control_secret_env_modes() {" in full, (
        "env-migrations.sh must define widen_control_secret_env_modes"
    )
    # Scope the checks to the widen function's body (it's the last function in
    # the file), so an unrelated reference (migrate_wifi_guardian writes the
    # stash) doesn't false-match.
    mig = full.split("widen_control_secret_env_modes() {", 1)[1]
    for fname in (
        "voice_provider.env",
        "spotify_credentials.env",
        # NOTE: google_credentials.env is NOT in this list anymore — WS1 Phase 4a
        # moved it to the group-`jasper-secrets` compartment (voice+web only),
        # so it is deliberately NOT widened to the broad `jasper` group. Its
        # relocation/perms are pinned by test_systemd_hardening's
        # test_secrets_compartment_phase4a + test_install_creates_google_dir_setgid.
        "home_assistant.env",
        "control_token",
        "jasper.env",
        # Non-secret state jasper-control also reads off disk for /state:
        "sound_profile.json",
        "sound_settings.json",
    ):
        assert fname in mig, f"widening must cover {fname}"
    assert "chgrp jasper" in mig and "chmod 0640" in mig, (
        "widening must chgrp jasper + chmod 0640 the files"
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
