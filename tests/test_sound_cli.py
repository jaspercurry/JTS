# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging

from jasper.cli import sound as sound_cli


def test_reconcile_current_dsp_fail_open_returns_json_and_logs_event(
    monkeypatch,
    capsys,
    caplog,
):
    async def _boom(**kwargs):
        raise RuntimeError("camilla unavailable")

    monkeypatch.setattr(sound_cli, "reconcile_current_dsp", _boom)
    caplog.set_level(logging.WARNING, logger="jasper.cli.sound")

    rc = sound_cli.main(["reconcile-current-dsp", "--fail-open", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["reason"] == "RuntimeError"
    assert payload["message"] == "camilla unavailable"
    assert "event=sound.reconcile_current_dsp" in caplog.text
    assert "result=failed" in caplog.text
    assert "reason=RuntimeError" in caplog.text
