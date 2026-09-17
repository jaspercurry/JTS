# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging

from jasper.cli import sound as sound_cli
from tests._log_events import event_fields


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
    fields = event_fields(caplog, "sound.reconcile_current_dsp")
    assert fields["result"] == "failed"
    assert fields["reason"] == "RuntimeError"
