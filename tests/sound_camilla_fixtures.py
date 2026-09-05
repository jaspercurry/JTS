# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CamillaDSP test double for the sound-setup and live-edit suites."""

from __future__ import annotations


class FakeCamilla:
    """Enough of CamillaDSP 4.1 to tell a parameter write from a pipeline
    replace: it keeps the graph it is running, hands it back the way
    CamillaDSP's readback does, and normalizes a candidate through the same
    round-trip so the two are comparable — which is the property
    ``jasper.sound.live_edit.plan_live_edit_for`` depends on.
    """

    def __init__(self, current_path: str, *, fail_set: bool = False) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []
        self.active_raw_values: list[str] = []
        self.ducks: list[bool] = []
        self.fail_set = fail_set
        self.running: str | None = None

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        if self.fail_set and not best_effort:
            raise RuntimeError("reload failed")
        return True

    @staticmethod
    def _normalized(text: str) -> str:
        import yaml as _yaml

        return str(_yaml.safe_dump(_yaml.safe_load(text)))

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False, duck: bool = True,
    ) -> bool:
        self.running = self._normalized(config)
        self.active_raw_values.append(config)
        self.ducks.append(duck)
        if self.fail_set and not best_effort:
            raise RuntimeError("live update failed")
        return True

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str | None:
        return self.running

    async def normalize_config_raw(
        self, config: str, *, best_effort: bool = False,
    ) -> str | None:
        return self._normalized(config)
