# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor research domain."""
from __future__ import annotations

import sqlite3

import pytest

from jasper.cli.doctor import research as doctor_research
from jasper.research import DONE, ResearchJob, ResearchJobStore


@pytest.mark.parametrize(
    "api_key, store", [(None, "absent"), ("sk-test", "absent"), ("sk-test", "broken")],
    ids=["disabled", "store-missing", "store-unqueryable"],
)
def test_research_verdicts(monkeypatch, tmp_path, api_key, store):
    db_path = tmp_path / "research.db"
    if store == "broken":
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE wrong_table (query TEXT)")
        conn.close()
    if api_key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(db_path))

    r = doctor_research.check_research()

    assert r.status == ("ok" if api_key is None else "warn")
    if api_key is not None:
        assert str(db_path) in r.detail


def test_research_ok_store_never_echoes_private_text(monkeypatch, tmp_path):
    db_path = tmp_path / "research.db"
    store = ResearchJobStore(str(db_path))
    assert store.add(
        ResearchJob(
            id="done1",
            query="private prompt",
            status=DONE,
            result="private answer",
            error=None,
            created_at=1.0,
            finished_at=2.0,
            announced=True,
            read=False,
        ),
    )
    store.close()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(db_path))

    r = doctor_research.check_research()

    assert r.status == "ok"
    assert "private prompt" not in r.detail
    assert "private answer" not in r.detail
