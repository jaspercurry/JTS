# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.peering.avahi.

Renders the template into a tmp_path so we don't touch real
/etc/avahi/services. avahi-daemon reload is monkey-patched to a
no-op so tests don't shell out.

Reload ownership note: ``render_and_install`` now lets the shared
``jasper.avahi_service.render_service`` own the reload (it reloads only on
``RenderResult.WROTE``), so the render-path reload fires through
``jasper.avahi_service.reload_avahi``. ``uninstall`` still drives peering's
own ``_reload_avahi``. The autouse fixture suppresses BOTH so no test
shells out; the unchanged-render test asserts against the render-path
reload (``avahi_service.reload_avahi``).
"""
from __future__ import annotations


import pytest

from jasper import avahi_service
from jasper.peering import avahi as avahi_mod


_TEMPLATE = """<?xml version="1.0" standalone='no'?>
<service-group>
  <name replace-wildcards="yes">JTS peer on %h</name>
  <service>
    <type>_jasper-peer._udp</type>
    <port>5354</port>
    <txt-record>peer_id=__PEER_ID__</txt-record>
    <txt-record>room=__ROOM__</txt-record>
    <txt-record>primary=__PRIMARY__</txt-record>
    <txt-record>proto=1</txt-record>
  </service>
</service-group>
"""


@pytest.fixture(autouse=True)
def _no_reload(monkeypatch):
    """Suppress the real avahi-daemon reload during tests, on BOTH paths:
    the render path (now owned by avahi_service.render_service →
    avahi_service.reload_avahi) and the uninstall path (peering's own
    _reload_avahi)."""
    monkeypatch.setattr(avahi_mod, "_reload_avahi", lambda: None)
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: None)


def test_render_substitutes_all_tokens(tmp_path):
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    ok = avahi_mod.render_and_install(
        peer_id="alice-uuid",
        room="kitchen",
        primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    assert ok is True
    text = rendered.read_text()
    assert "peer_id=alice-uuid" in text
    assert "room=kitchen" in text
    assert "primary=1" in text
    # And none of the original tokens remain.
    for token in ("__PEER_ID__", "__ROOM__", "__PRIMARY__"):
        assert token not in text


def test_primary_renders_as_01_not_truefalse(tmp_path):
    """The XML TXT record consumer (firmware, doctor) expects 0/1
    not true/false. Pin the format so a refactor doesn't accidentally
    change it."""
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    avahi_mod.render_and_install(
        peer_id="alice-uuid", room="bedroom", primary=False,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    assert "primary=0" in rendered.read_text()


def test_missing_template_returns_false(tmp_path):
    """A missing template (fresh install before install.sh ran) must
    not crash — return False and let the daemon log + continue.
    Browsing + arbitrating still work without advertising."""
    rendered = tmp_path / "rendered.xml"
    ok = avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=False,
        template_path=str(tmp_path / "missing.xml"),
        rendered_path=str(rendered),
    )
    assert ok is False
    assert not rendered.exists()


def test_unknown_token_refused(tmp_path):
    """Catch template drift — a template with a new __FOO__ token
    must be refused, not installed half-rendered."""
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE + "<!-- __NEWTOKEN__ -->")
    rendered = tmp_path / "rendered.xml"

    ok = avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=False,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    assert ok is False
    assert not rendered.exists()


def test_uninstall_is_idempotent(tmp_path):
    """Removing a non-existent file shouldn't raise (called on every
    mode=off transition, even when peering was never on)."""
    avahi_mod.uninstall(rendered_path=str(tmp_path / "nope.xml"))  # no raise


def test_uninstall_removes_existing(tmp_path):
    target = tmp_path / "rendered.xml"
    target.write_text("anything")
    avahi_mod.uninstall(rendered_path=str(target))
    assert not target.exists()


def test_skip_write_when_unchanged(tmp_path, monkeypatch):
    """Idempotent re-render: if the rendered output matches what's on
    disk, skip the write. Avoids spamming avahi reload on every
    daemon restart.

    The reload is now owned by avahi_service.render_service (it fires only
    on RenderResult.WROTE), so the no-reload assertion patches the
    render-path reload — jasper.avahi_service.reload_avahi — not peering's
    own _reload_avahi (which now only drives uninstall)."""
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    # First call writes.
    avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    original = rendered.read_text()
    rendered.stat().st_mtime_ns

    # Track reload calls during second invocation. render_service drives
    # the reload now, so patch avahi_service.reload_avahi.
    reload_calls = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: reload_calls.append(1))

    # Second call with same params — should be a no-op write (UNCHANGED).
    avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    # File content unchanged; reload not triggered.
    assert rendered.read_text() == original
    # NOTE: We can't reliably assert mtime equality (filesystems
    # have varying precision) — the load-bearing thing is no reload.
    assert reload_calls == []


def test_render_reload_is_driven_by_render_service(tmp_path, monkeypatch):
    """On an actual write, the reload fires through the shared
    avahi_service.reload_avahi (render_service owns it) — not peering's own
    _reload_avahi. This pins the reload-ownership move: render_and_install
    passes reload=reload_avahi down and no longer drives the reload itself.
    """
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    render_path_reloads: list = []
    peering_reloads: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: render_path_reloads.append(1))
    monkeypatch.setattr(avahi_mod, "_reload_avahi", lambda: peering_reloads.append(1))

    ok = avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
        reload_avahi=True,
    )
    assert ok is True
    # The write went through render_service, so its reload fired once;
    # peering's own _reload_avahi (uninstall-only now) was NOT called.
    assert render_path_reloads == [1]
    assert peering_reloads == []


def test_render_reload_false_suppresses_reload(tmp_path, monkeypatch):
    """reload_avahi=False is forwarded to render_service as reload=False, so
    even a real write does not reload (install.sh batches its own)."""
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    render_path_reloads: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: render_path_reloads.append(1))

    ok = avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
        reload_avahi=False,
    )
    assert ok is True
    assert rendered.exists()
    assert render_path_reloads == []
