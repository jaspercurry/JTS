# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared streaming-download double standing in for the object
``urllib.request.urlopen()`` returns: a context manager exposing
``headers`` and a chunked ``read()`` over a fixed payload."""

from __future__ import annotations


class FakeResponse:
    def __init__(self, payload: bytes, *, content_length: str | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + n]
        self._offset += len(chunk)
        return chunk
