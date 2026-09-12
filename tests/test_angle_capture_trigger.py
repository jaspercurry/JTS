# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import pytest
from jasper.cli import angle_capture as cli


@pytest.mark.parametrize("verb", ["plan", "stage", "show", "withdraw"])
def test_spool_verbs_are_retired(verb):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args([verb])
    assert exc.value.code == 2


def test_serve_still_parses():
    args = cli.build_parser().parse_args(["serve", "--hostname", "jts.local", "--attest-rig-clear"])
    assert args.command == "serve"
    assert args.mover == "turntable"
