# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Privacy-safe research health snapshots for /state and jasper-doctor."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from ..env_load import merged_env_files
from .catalog import PROVIDERS
from .scheduler import DEFAULT_DB_PATH, DONE, FAILED, RUNNING, ResearchJobStore

DB_PATH_ENV = "JASPER_RESEARCH_DB"
SCHEMA_VERSION = 1


def snapshot(
    *,
    environ: dict[str, str] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded research status without prompt/result text."""
    env = _research_env(environ)
    provider = _configured_provider(env)
    runtime_provider = _runtime_provider(runtime)
    if runtime_provider is not None:
        provider = runtime_provider

    db_path = (env.get(DB_PATH_ENV) or DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
    store = ResearchJobStore(
        db_path,
        read_only=True,
        warn_unavailable=False,
    )
    try:
        store_available = store.available
        jobs = store.all() if store_available else []
    finally:
        store.close()

    counts = _counts(jobs) if store_available else None
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": bool(provider and provider["configured"]),
        "provider": provider,
        "store": {
            "available": store_available,
            "path": db_path,
        },
        "counts": counts,
        "oldest_created_at": _oldest_created_at(jobs),
        "newest_created_at": _newest_created_at(jobs),
        "newest_finished_at": _newest_finished_at(jobs),
        "recent_failure": _recent_failure(jobs),
        "runtime": _runtime_summary(runtime),
    }


def _research_env(environ: dict[str, str] | None) -> dict[str, str]:
    if environ is not None:
        return dict(environ)
    env = dict(os.environ)
    env.update(merged_env_files())
    return env


def _configured_provider(env: dict[str, str]) -> dict[str, Any] | None:
    for entry in PROVIDERS:
        if not (env.get(entry.key_env) or "").strip():
            continue
        model = (env.get(entry.model_env) or entry.default_model).strip()
        return {
            "id": entry.id,
            "label": entry.label,
            "configured": True,
            "model": model,
        }
    return None


def _runtime_provider(runtime: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(runtime, dict) or runtime.get("configured") is not True:
        return None
    provider_id = runtime.get("provider")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {
            "id": None,
            "label": None,
            "configured": True,
            "model": _text_or_none(runtime.get("model")),
        }
    entry = next((candidate for candidate in PROVIDERS if candidate.id == provider_id), None)
    return {
        "id": provider_id,
        "label": entry.label if entry is not None else provider_id,
        "configured": True,
        "model": _text_or_none(runtime.get("model")),
    }


def _runtime_summary(runtime: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(runtime, dict):
        return None
    pending = runtime.get("pending_announcements")
    if not isinstance(pending, int):
        pending = None
    return {
        "voice_reachable": True,
        "pending_announcements": pending,
        "confirmation_window_active": runtime.get("confirmation_window_active") is True,
    }


def _counts(jobs) -> dict[str, int]:
    running = sum(1 for job in jobs if job.status == RUNNING)
    done = sum(1 for job in jobs if job.status == DONE)
    failed = sum(1 for job in jobs if job.status == FAILED)
    pending = sum(
        1
        for job in jobs
        if job.status in (DONE, FAILED) and not job.announced
    )
    return {
        "total": len(jobs),
        "pending": pending,
        "running": running,
        "done": done,
        "failed": failed,
    }


def _oldest_created_at(jobs) -> str | None:
    values = [job.created_at for job in jobs]
    return _iso(min(values)) if values else None


def _newest_created_at(jobs) -> str | None:
    values = [job.created_at for job in jobs]
    return _iso(max(values)) if values else None


def _newest_finished_at(jobs) -> str | None:
    values = [job.finished_at for job in jobs if job.finished_at is not None]
    return _iso(max(values)) if values else None


def _recent_failure(jobs) -> dict[str, Any] | None:
    failures = [job for job in jobs if job.status == FAILED]
    if not failures:
        return None
    latest = max(failures, key=lambda job: job.finished_at or job.created_at)
    when = latest.finished_at or latest.created_at
    return {
        "job_id": latest.id,
        "finished_at": _iso(when),
        "age_seconds": max(0.0, round(time.time() - when, 1)),
        "announced": latest.announced,
        "read": latest.read,
        "error_present": bool(latest.error),
    }


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
