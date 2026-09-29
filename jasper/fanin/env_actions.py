# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable
from pathlib import Path

from jasper.atomic_io import CONFIG_FILE_MODE, env_key_action, locked_upsert_env_file
from jasper.service_state.audio_runtime_settings import RuntimeEnvAction
from jasper.env_file import remove, upsert


def _write_env_actions(
    path: Path,
    build_actions: Callable[[str], tuple[RuntimeEnvAction, ...]],
) -> tuple[str, bool]:
    """Fold ``build_actions`` onto ``path`` under its per-file advisory lock.

    The shared text-preserving writer, in this module's ``RuntimeEnvAction``
    vocabulary. Returns ``(text, changed)``; an empty result deletes the file
    instead of publishing zero bytes. Raises ``OSError`` on a write failure or
    a lock-acquire timeout (``TimeoutError`` is one).
    """
    return locked_upsert_env_file(
        path,
        lambda text: [env_key_action(action) for action in build_actions(text)],
        mode=CONFIG_FILE_MODE,
        delete_when_empty=True,
    )


def _apply_action(text: str, action: RuntimeEnvAction) -> tuple[str, bool]:
    if action.action == "set":
        return upsert(text, action.key, action.value)
    return remove(text, action.key)


def _apply_actions(
    text: str, actions: tuple[RuntimeEnvAction, ...]
) -> tuple[str, bool]:
    """Fold a sequence of env actions onto ``text``; changed = any moved the file."""
    changed = False
    for action in actions:
        text, moved = _apply_action(text, action)
        changed = changed or moved
    return text, changed
