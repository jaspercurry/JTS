# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper.tool_prompt_overrides — wizard-owned prompt override state."""
from __future__ import annotations

import json
import os
import stat

from jasper.tool_prompt_overrides import read_prompt_overrides, write_prompt_overrides


def test_missing_file_reads_empty(tmp_path):
    assert read_prompt_overrides(tmp_path / "missing.json") == {}


def test_malformed_file_reads_empty(tmp_path, caplog):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with caplog.at_level("WARNING"):
        assert read_prompt_overrides(p) == {}
    assert any("tool_prompt_overrides" in r.message for r in caplog.records)


def test_write_round_trips_sorted_json_and_mode(tmp_path):
    p = tmp_path / "overrides.json"
    write_prompt_overrides(p, {"b": "Prompt B", "a": "Prompt A", "blank": ""})
    assert read_prompt_overrides(p) == {"a": "Prompt A", "b": "Prompt B"}
    assert list(json.loads(p.read_text()).keys()) == ["a", "b"]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
