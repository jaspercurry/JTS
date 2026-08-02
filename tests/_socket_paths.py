# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Short Unix-socket paths shared by socket integration tests."""

from __future__ import annotations

import os
import secrets
import tempfile

import pytest


def short_unix_socket_path(prefix: str = "jts-pt") -> str:
    """Return a unique path below the shortest common AF_UNIX limit."""

    return f"/tmp/{prefix}-{secrets.token_hex(4)}.sock"


@pytest.fixture(name="short_sock_path")
def short_socket_path_fixture():
    """Yield a short control-socket path and remove its temporary directory."""

    directory = tempfile.mkdtemp(prefix="jts-sock-")
    path = os.path.join(directory, "control.sock")
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
