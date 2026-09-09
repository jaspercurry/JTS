# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shell-evaluated assignments; values retain their exact text."""

import shlex
from collections.abc import Mapping


def render_shell_assignments(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
