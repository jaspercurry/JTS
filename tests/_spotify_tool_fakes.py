# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared renderer, account, and router doubles for Spotify tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


class FakeRenderer:
    def __init__(
        self,
        renderers=None,
        currentsong=None,
        selected_source=None,
        selected_source_error=None,
    ) -> None:
        self._renderers = renderers or {}
        self._currentsong = currentsong or {}
        self._selected_source = selected_source
        self._selected_source_error = selected_source_error
        self.pause_airplay = AsyncMock()

    async def active_renderers(self) -> dict:
        return self._renderers

    async def get_currentsong(self) -> dict:
        return self._currentsong

    async def selected_source(self):
        if self._selected_source_error is not None:
            raise self._selected_source_error
        return self._selected_source


class FakeAccountClient:
    def __init__(self, name: str, sp, playlists=None) -> None:
        self.account = MagicMock()
        self.account.name = name
        self.account.playlists = playlists if playlists is not None else {}
        self.sp = sp


class FakeRouter:
    def __init__(
        self,
        transport_match=None,
        active_account=None,
        empty_reason: str = "no_accounts",
        rebuild_clients=None,
        revoked_names=None,
        *,
        populate_clients: bool = True,
    ) -> None:
        self._transport_match = transport_match
        self._active_account = active_account
        self.clients = (
            {"jasper": active_account or transport_match}
            if populate_clients and (active_account or transport_match)
            else {}
        )
        self._empty_reason = empty_reason
        self._rebuild_clients = rebuild_clients
        self._revoked_names = list(revoked_names or [])
        self.refresh_calls = 0

    async def resolve_for_transport(self, client_name: str, title: str):
        return self._transport_match

    async def active(self, *, airplay_active: bool):
        return self._active_account

    async def refresh_if_empty(self) -> bool:
        self.refresh_calls += 1
        if self.clients:
            return True
        if self._rebuild_clients:
            self.clients = dict(self._rebuild_clients)
            if not self._active_account:
                self._active_account = next(iter(self.clients.values()))
            return True
        return False

    def empty_reason(self) -> str:
        return "" if self.clients else self._empty_reason

    def revoked_account_names(self) -> list:
        return list(self._revoked_names)
