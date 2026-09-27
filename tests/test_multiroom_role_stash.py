# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""RoleStash — the CamillaDSP role-swap stash ladder shared by
leader_config and follower_config (#4805 R-132). Each arm binds its own
instance to its own path; this pins the read/write/clear roundtrip both
arms rely on."""
from __future__ import annotations

import pytest

from jasper.multiroom.role_stash import RoleStash


@pytest.mark.parametrize(
    "stash_path_name", ["grouping-prior-camilla.txt", "grouping-follower-prior-camilla.txt"]
)
def test_role_stash_round_trip(tmp_path, stash_path_name):
    stash = RoleStash(str(tmp_path / stash_path_name))
    assert stash.read_stash() is None  # missing file -> None, no raise
    stash.write_stash("/var/lib/camilladsp/configs/sound_current.yml")
    assert stash.read_stash() == "/var/lib/camilladsp/configs/sound_current.yml"
    stash.clear_stash()
    assert stash.read_stash() is None
    stash.clear_stash()  # idempotent
