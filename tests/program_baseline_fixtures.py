# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Shared banked-baseline identity for measurement-plan tests."""

import pytest


def fake_program_baselines(monkeypatch):
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.baseline_candidate_id",
                        lambda: "banked-base")


@pytest.fixture(autouse=True)
def banked_program_baselines(monkeypatch):
    fake_program_baselines(monkeypatch)
