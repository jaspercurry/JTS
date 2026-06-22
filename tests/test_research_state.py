# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sqlite3

from jasper.control import state_aggregate
from jasper.research import DONE, FAILED, RUNNING, ResearchJob, ResearchJobStore
from jasper.research.state import snapshot


def _job(
    job_id: str,
    *,
    query: str,
    status: str,
    result: str | None = None,
    error: str | None = None,
    created_at: float,
    finished_at: float | None = None,
    announced: bool = False,
) -> ResearchJob:
    return ResearchJob(
        id=job_id,
        query=query,
        status=status,
        result=result,
        error=error,
        created_at=created_at,
        finished_at=finished_at,
        announced=announced,
        read=False,
    )


def test_research_state_counts_timestamps_and_omits_private_text(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    store = ResearchJobStore(str(db_path))
    assert store.add(
        _job(
            "run1",
            query="private running prompt",
            status=RUNNING,
            created_at=1_800_000_000.0,
        ),
    )
    assert store.add(
        _job(
            "done1",
            query="private done prompt",
            status=DONE,
            result="private answer text",
            created_at=1_800_000_010.0,
            finished_at=1_800_000_020.0,
            announced=False,
        ),
    )
    assert store.add(
        _job(
            "fail1",
            query="private failed prompt",
            status=FAILED,
            error="provider included sensitive detail",
            created_at=1_800_000_030.0,
            finished_at=1_800_000_040.0,
            announced=True,
        ),
    )
    store.close()

    snap = snapshot(
        environ={
            "JASPER_RESEARCH_DB": str(db_path),
            "OPENAI_API_KEY": "sk-test",
            "JASPER_RESEARCH_OPENAI_MODEL": "gpt-5.4-mini",
        },
    )

    assert snap["schema_version"] == 1
    assert snap["enabled"] is True
    assert snap["provider"] == {
        "id": "openai",
        "label": "OpenAI Responses",
        "configured": True,
        "model": "gpt-5.4-mini",
    }
    assert snap["store"] == {"available": True, "path": str(db_path)}
    assert snap["counts"] == {
        "total": 3,
        "pending": 1,
        "running": 1,
        "done": 1,
        "failed": 1,
    }
    assert snap["oldest_created_at"] == "2027-01-15T08:00:00Z"
    assert snap["newest_finished_at"] == "2027-01-15T08:00:40Z"
    assert snap["recent_failure"] == {
        "job_id": "fail1",
        "finished_at": "2027-01-15T08:00:40Z",
        "age_seconds": snap["recent_failure"]["age_seconds"],
        "announced": True,
        "read": False,
        "error_present": True,
    }

    rendered = json.dumps(snap)
    assert "private running prompt" not in rendered
    assert "private answer text" not in rendered
    assert "provider included sensitive detail" not in rendered


def test_research_state_marks_query_failure_unavailable_without_private_text(
    tmp_path,
) -> None:
    db_path = tmp_path / "research.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE wrong_table (query TEXT, result TEXT, error TEXT)")
    conn.execute(
        "INSERT INTO wrong_table VALUES ('private prompt', 'private answer', 'private error')",
    )
    conn.close()

    snap = snapshot(
        environ={
            "JASPER_RESEARCH_DB": str(db_path),
            "OPENAI_API_KEY": "sk-test",
        },
    )

    assert snap["store"]["available"] is False
    assert snap["store"]["path"] == str(db_path)
    assert "no such table" in snap["store"]["error"]
    assert snap["counts"] is None
    rendered = json.dumps(snap)
    assert "private prompt" not in rendered
    assert "private answer" not in rendered
    assert "private error" not in rendered


def test_research_state_uses_voice_runtime_provider_without_secret_read(
    tmp_path,
) -> None:
    db_path = tmp_path / "missing.db"

    snap = snapshot(
        environ={"JASPER_RESEARCH_DB": str(db_path)},
        runtime={
            "configured": True,
            "provider": "openai",
            "model": "gpt-5.4",
            "pending_announcements": 2,
            "confirmation_window_active": True,
        },
    )

    assert snap["enabled"] is True
    assert snap["provider"]["id"] == "openai"
    assert snap["provider"]["model"] == "gpt-5.4"
    assert snap["store"] == {"available": False, "path": str(db_path)}
    assert snap["counts"] is None
    assert snap["runtime"] == {
        "voice_reachable": True,
        "pending_announcements": 2,
        "confirmation_window_active": True,
    }
    assert db_path.exists() is False


def test_state_aggregate_research_helper_is_privacy_safe(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "research.db"
    store = ResearchJobStore(str(db_path))
    assert store.add(
        _job(
            "done1",
            query="secret prompt",
            status=DONE,
            result="secret answer",
            created_at=1_800_000_000.0,
            finished_at=1_800_000_001.0,
        ),
    )
    store.close()
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(db_path))

    snap = state_aggregate._research_state({
        "configured": True,
        "provider": "openai",
        "model": "gpt-5.4",
    })

    # The helper reads process/env-load defaults, so this test pins the
    # privacy side of the /state shape without depending on the host env.
    assert "secret prompt" not in json.dumps(snap)
    assert "secret answer" not in json.dumps(snap)
