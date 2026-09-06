# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's conversation-history capture: the single write path
that turns a finished turn (or a research delivery) into a persisted
`ConversationTurn` row, gated on live capture settings and mic-mute.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..conversation_history import (
    ConversationSettings,
    ConversationStore,
    ConversationTurn,
    make_turn_id,
    prune_for_settings,
    read_settings as read_conversation_settings,
)

logger = logging.getLogger("jasper.voice_daemon")


def _conversation_ts_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ConversationCapture:
    def __init__(
        self, *, store: ConversationStore | None, voice_provider: str,
    ) -> None:
        self.store = store
        self.store_path = store.db_path if store is not None else None
        self.turn_seq = 0
        self._voice_provider = voice_provider

    def _store_for_settings(
        self,
        settings: ConversationSettings,
    ) -> ConversationStore | None:
        if not settings.capture_enabled:
            return None
        store = self.store
        if (
            store is not None
            and self.store_path == settings.db_path
            and store.available
        ):
            return store
        if store is not None:
            store.close()
            self.store = None
            self.store_path = None
        store = ConversationStore(settings.db_path)
        self.store = store
        self.store_path = settings.db_path
        return store if store.available else None

    def close(self) -> None:
        store = self.store
        self.store = None
        self.store_path = None
        if store is None:
            return
        store.close()

    def record(
        self,
        user_text: str | None,
        assistant_text: str | None,
        *,
        data_json: dict | None = None,
        provider: str | None = None,
        session_id: int | None,
        mic_muted: bool,
    ) -> None:
        """Persist one conversation-history row.

        The single write path for ordinary wake turns and feature-fed
        entries such as research delivery. Fail-soft by design: capture
        must never block turn teardown or a proactive announcement.
        """
        if mic_muted:
            return
        if user_text is None and assistant_text is None and data_json is None:
            return
        try:
            settings = read_conversation_settings()
        except (OSError, TypeError, ValueError) as e:
            logger.warning(
                "conversation capture: settings unavailable (%s: %s)",
                type(e).__name__,
                e,
            )
            return
        if not settings.capture_enabled:
            return
        store = self._store_for_settings(settings)
        if store is None:
            logger.debug("conversation capture: skipped (store unavailable)")
            return
        data_text: str | None = None
        if isinstance(data_json, dict):
            try:
                data_text = json.dumps(data_json, separators=(",", ":"))
            except (TypeError, ValueError) as e:
                logger.warning(
                    "conversation capture: data_json encode failed (%s: %s)",
                    type(e).__name__,
                    e,
                )
                data_text = None
        if user_text is None and assistant_text is None and data_text is None:
            return

        ts_utc = _conversation_ts_utc()
        self.turn_seq = (self.turn_seq % 999) + 1
        turn = ConversationTurn(
            id=make_turn_id(ts_utc, self.turn_seq),
            ts_utc=ts_utc,
            provider=provider or self._voice_provider,
            user_text=user_text,
            assistant_text=assistant_text,
            tool_calls_json=None,
            data_json=data_text,
            session_id=session_id,
        )
        if store.add(turn):
            try:
                prune_for_settings(store, settings, anchor_ts_utc=ts_utc)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    "conversation capture: retention prune failed (%s: %s)",
                    type(e).__name__,
                    e,
                )
