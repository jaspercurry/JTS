# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The PortAudio stand-in a function-local `import sounddevice` reads."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock


def stub_sounddevice(monkeypatch, module=None):
    """Install `module` (a bare `MagicMock` by default) as `sounddevice`."""
    monkeypatch.setitem(sys.modules, "sounddevice", module or MagicMock())
