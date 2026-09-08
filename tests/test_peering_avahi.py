# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Peering Avahi files use temporary paths and a stubbed reload."""
from __future__ import annotations


import pytest

from jasper.net import avahi_service
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
    calls = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: calls.append(1))
    return calls


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


@pytest.mark.parametrize("reload", [True, False])
def test_uninstall_reloads_only_after_removal(tmp_path, _no_reload, reload):
    target = tmp_path / "rendered.xml"
    target.write_text("anything")
    avahi_mod.uninstall(rendered_path=str(target), reload_avahi=reload)
    assert not target.exists()
    assert _no_reload == ([1] if reload else [])
    _no_reload.clear()
    avahi_mod.uninstall(rendered_path=str(target), reload_avahi=reload)
    assert _no_reload == []


def test_uninstall_failure_does_not_reload(tmp_path, monkeypatch, _no_reload):
    target = tmp_path / "rendered.xml"
    target.write_text("anything")

    def fail_unlink(path):
        raise PermissionError(path)

    monkeypatch.setattr(avahi_mod.os, "unlink", fail_unlink)
    avahi_mod.uninstall(rendered_path=str(target))
    assert target.exists()
    assert _no_reload == []


def test_skip_write_when_unchanged(tmp_path, _no_reload):
    """Idempotent re-render: if the rendered output matches what's on
    disk, skip the write. Avoids spamming avahi reload on every
    daemon restart."""
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
    _no_reload.clear()

    # Second call with same params — should be a no-op write (UNCHANGED).
    avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
    )
    # File content unchanged; reload not triggered.
    assert rendered.read_text() == original
    # Can't reliably assert mtime equality (filesystems have varying precision) — the load-bearing thing is no reload.
    assert _no_reload == []


@pytest.mark.parametrize("reload", [True, False])
def test_render_reloads_after_write(tmp_path, _no_reload, reload):
    template = tmp_path / "template.xml"
    template.write_text(_TEMPLATE)
    rendered = tmp_path / "rendered.xml"

    ok = avahi_mod.render_and_install(
        peer_id="alice", room="kitchen", primary=True,
        template_path=str(template),
        rendered_path=str(rendered),
        reload_avahi=reload,
    )
    assert ok is True
    assert rendered.exists()
    assert _no_reload == ([1] if reload else [])
