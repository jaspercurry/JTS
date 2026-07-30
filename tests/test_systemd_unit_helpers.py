# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from tests.systemd_unit_helpers import assignments_for, value_for, values_for


def test_scalar_value_uses_last_assignment():
    assert value_for("Restart=no\nRestart=on-failure\n", "Restart") == "on-failure"


def test_list_values_accumulate_honor_reset_and_join_continuations():
    accumulated = """
Wants=first.service "two words.service"
Wants=third.service \\
    fourth.service
"""
    assert values_for(accumulated, "Wants") == (
        "first.service",
        "two words.service",
        "third.service",
        "fourth.service",
    )

    reset = accumulated + """
Wants=
Wants=fifth.service
"""
    assert values_for(reset, "Wants") == ("fifth.service",)


def test_raw_assignments_accumulate_and_honor_reset():
    unit = "ExecStartPre=/bin/one --flag\nExecStartPre=\nExecStartPre=/bin/two --flag\n"
    assert assignments_for(unit, "ExecStartPre") == ("/bin/two --flag",)
