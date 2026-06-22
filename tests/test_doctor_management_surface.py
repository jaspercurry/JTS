# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's management-surface probe.

check_management_surface exercises the browser path (loopback nginx with
`Host: <JASPER_HOSTNAME>` → system wizard → jasper-control's
management-host guard). The network is mocked; the on-Pi smoke test is
jasper-doctor itself plus the deploy-time probe in
scripts/deploy-to-pi.sh.
"""
from __future__ import annotations

import io
import sqlite3
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    make_turn_id,
)
from jasper.cli.doctor import web as doctor_web
from jasper.cli.doctor import research as doctor_research
from jasper.research import DONE, ResearchJob, ResearchJobStore


def _install_nginx_site(monkeypatch, tmp_path):
    site = tmp_path / "jasper.conf"
    site.write_text("# nginx site\n")
    monkeypatch.setattr(doctor_web, "NGINX_SITE", site)


@contextmanager
def _urlopen_returns(status: int, body: bytes):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self, n=-1):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()) as m:
        yield m


def test_skips_when_nginx_site_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_web, "NGINX_SITE", tmp_path / "absent.conf")
    r = doctor_web.check_management_surface()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_ok_on_200(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    with _urlopen_returns(200, b"{}") as m:
        r = doctor_web.check_management_surface()
    assert r.status == "ok"
    assert "jts3.local" in r.detail
    # The probe must carry the speaker hostname as the Host header —
    # that is the whole point of the check.
    req = m.call_args[0][0]
    assert req.get_header("Host") == "jts3.local"


def test_403_fails_with_guard_hint(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_web.MANAGEMENT_PROBE_URL, 403, "Forbidden", None,
        io.BytesIO(b'{"error": "host_not_allowed"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "host_not_allowed" in r.detail
    assert "event=http.reject" in r.detail


def test_502_fails_naming_control(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_web.MANAGEMENT_PROBE_URL, 502, "Bad Gateway", None,
        io.BytesIO(b'{"error": "jasper-control unreachable: ..."}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "jasper-control" in r.detail


def test_connection_refused_fails_naming_nginx(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "nginx" in r.detail


def test_conversation_history_skips_when_capture_disabled(monkeypatch, tmp_path):
    settings = tmp_path / "conversation_history.env"
    settings.write_text(f"{CAPTURE_ENABLED_ENV}=0\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings))

    r = doctor_web.check_conversation_history()

    assert r.status == "ok"
    assert "skipped" in r.detail


def test_conversation_history_warns_when_enabled_db_missing(monkeypatch, tmp_path):
    settings = tmp_path / "conversation_history.env"
    db_path = tmp_path / "missing.db"
    settings.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings))

    r = doctor_web.check_conversation_history()

    assert r.status == "warn"
    assert "capture enabled" in r.detail
    assert str(db_path) in r.detail


def test_conversation_history_ok_with_existing_db(monkeypatch, tmp_path):
    db_path = tmp_path / "conversation_history.db"
    settings = tmp_path / "conversation_history.env"
    settings.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings))
    store = ConversationStore(str(db_path))
    store.add(
        ConversationTurn(
            id=make_turn_id("2026-06-19T20:15:00Z", 1),
            ts_utc="2026-06-19T20:15:00Z",
            provider="gemini",
            user_text="hello",
            assistant_text="hi",
            tool_calls_json=None,
            data_json=None,
            session_id=1,
        ),
    )
    store.close()

    r = doctor_web.check_conversation_history()

    assert r.status == "ok"
    assert "1 turns" in r.detail


def test_research_check_ok_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(tmp_path / "missing.db"))

    r = doctor_research.check_research()

    assert r.status == "ok"
    assert "disabled" in r.detail


def test_research_check_warns_when_configured_store_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "missing.db"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(db_path))

    r = doctor_research.check_research()

    assert r.status == "warn"
    assert "openai configured" in r.detail
    assert str(db_path) in r.detail
    assert db_path.exists() is False


def test_research_check_warns_when_configured_store_query_fails(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "research.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE wrong_table (query TEXT)")
    conn.close()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JASPER_RESEARCH_DB", str(db_path))

    r = doctor_research.check_research()

    assert r.status == "warn"
    assert "openai configured" in r.detail
    assert str(db_path) in r.detail
    assert "no such table" in r.detail


def test_research_check_ok_with_existing_store_without_private_text(
    monkeypatch,
    tmp_path,
):
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
    assert "openai configured" in r.detail
    assert "private prompt" not in r.detail
    assert "private answer" not in r.detail
