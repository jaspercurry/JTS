"""leader_config — the grouping reconciler's CamillaDSP apply arm
(Increment 5). Pure parts only: the restore ladder decision + the
prior-config stash. The async apply flows do real CamillaDSP websocket
I/O and are validated on hardware (the doctor's `leader pipe` check +
grouping runtime health are their backstops)."""
from __future__ import annotations

from jasper.multiroom.leader_config import (
    BONDED_CONFIG_PATH,
    SOLO_RESTORE_PATH,
    _clear_stash,
    _write_stash,
    read_stash,
    restore_action,
)


def test_restore_action_none_on_the_common_solo_reconcile():
    """No stash + CamillaDSP already on a solo config ⇒ nothing to do.
    This is every reconcile run on a solo speaker — it MUST be a no-op
    (no CamillaDSP churn)."""
    assert restore_action(
        stash=None, stash_file_exists=False, bonded_active=False,
    ) == "none"


def test_restore_action_prefers_the_stash():
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_file_exists=True,
        bonded_active=True,
    ) == "stash"
    # Stash wins even if camilla already flipped off the bonded config
    # (a half-finished prior unwind retries to the user's real config).
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_file_exists=True,
        bonded_active=False,
    ) == "stash"


def test_restore_action_re_emits_when_stash_is_missing_or_gone():
    # Bonded active but no stash at all (stash lost): re-emit solo.
    assert restore_action(
        stash=None, stash_file_exists=False, bonded_active=True,
    ) == "re_emit"
    # Stash exists but its file was deleted (config dir cleaned): re-emit.
    assert restore_action(
        stash="/var/lib/camilladsp/configs/sound_current.yml",
        stash_file_exists=False,
        bonded_active=True,
    ) == "re_emit"


def test_stash_round_trip(tmp_path):
    path = str(tmp_path / "prior.txt")
    assert read_stash(path) is None  # missing file → None, no raise
    _write_stash("/var/lib/camilladsp/configs/sound_current.yml", path)
    assert read_stash(path) == "/var/lib/camilladsp/configs/sound_current.yml"
    _clear_stash(path)
    assert read_stash(path) is None
    _clear_stash(path)  # idempotent


def test_bonded_and_restore_names_are_jts_generated():
    """The /sound preserve logic must recognise the reconciler's configs
    as JTS-generated — else a profile save while bonded would refuse with
    the custom-config error (or worse, an unlisted name would be treated
    as hand-rolled). Pins the _JTS_GENERATED_RE registration."""
    from jasper.multiroom.leader_config import CONFIG_DIR
    from jasper.sound.camilla_yaml import is_jts_generated_config

    assert is_jts_generated_config(BONDED_CONFIG_PATH, config_dir=CONFIG_DIR)
    assert is_jts_generated_config(SOLO_RESTORE_PATH, config_dir=CONFIG_DIR)
