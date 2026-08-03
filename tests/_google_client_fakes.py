# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared Google client scaffolding for Calendar and Gmail tool tests."""

from __future__ import annotations

from typing import Any

from jasper import google_creds as gc
from jasper.google_creds import GoogleAccount, GoogleClients, GoogleRegistry


class FakeExecutable:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def execute(self) -> Any:
        return self.payload


def make_google_clients(
    monkeypatch,
    *,
    accounts: tuple[str, ...] = ("jasper", "brittany"),
    service: Any = None,
) -> GoogleClients:
    """Build clients whose first account is default and whose service is fake."""

    monkeypatch.setattr(gc, "load_credentials", lambda account, **kw: object())
    registry = GoogleRegistry()
    for index, name in enumerate(accounts):
        registry.add_or_update(GoogleAccount(name=name), make_default=index == 0)
    return GoogleClients(
        registry=registry,
        client_id="x",
        client_secret="y",
        service_factory=lambda api_name, version, creds: service,
    )
