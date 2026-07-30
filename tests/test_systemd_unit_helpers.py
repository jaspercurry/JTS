# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from tests.systemd_unit_helpers import value_for, values_for


def test_scalar_value_uses_last_assignment():
    assert value_for("Restart=no\nRestart=on-failure\n", "Restart") == "on-failure"


def test_list_values_accumulate_honor_reset_and_join_continuations():
    unit = """
Wants=first.service "two words.service"
Wants=third.service \\
    fourth.service
Wants=
Wants=fifth.service
"""

    assert values_for(unit, "Wants") == ("fifth.service",)
