# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /speaker/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css,
   carries the shared .app-header, embeds the CSRF meta tag) and delivers its
   behaviour as an ES module -- no inline <script>.
2. The migration was presentation-only: the server-rendered POST /save flow
   (validate -> duplicate-check -> write -> restart) and the public module
   surface (render fn, make_server, main) are unchanged.
3. The rename transaction rewrites BlueZ configuration atomically and keeps
   the independent Bluetooth, Avahi, USB-gadget, and renderer surfaces moving
   when one best-effort surface fails.
"""

from __future__ import annotations

import http
import logging
import types

import pytest

from jasper.speaker_name import DEFAULT_SPEAKER_NAME, SpeakerNameError
from jasper.web import speaker_setup

from ._web_test_helpers import FakeHandler


def _render(
    current_name: str = "Kitchen",
    current_room: str = "",
    flash: str = "",
    hostname: str = "jts.local",
) -> str:
    return speaker_setup._index_html(
        current_name=current_name,
        current_room=current_room,
        hostname=hostname,
        csrf_token="tok-abcdefghijklmnopqrstuvwx",
        status_msg=flash,
    ).decode()


def test_speaker_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    assert "max-width: 620px" not in out


def test_speaker_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Speaker name</h1>' in out
    assert '<use href="#icon-back">' in out


def test_speaker_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_speaker_form_preserves_csrf_field_and_save_action():
    out = _render()
    assert 'action="./save"' in out
    assert 'method="post"' in out
    assert 'type="hidden"' in out


def test_speaker_form_uses_canonical_field_vocabulary():
    out = _render()
    assert 'class="field"' in out
    assert 'type="text" name="name"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out


def test_speaker_page_loads_es_module_not_inline_script():
    out = _render()
    assert '<script type="module" src="/assets/speaker/js/main.js">' in out
    before_module = out.split('<script type="module"')[0]
    assert "jtsConfirm(" not in before_module
    assert "addEventListener" not in before_module


def test_speaker_page_passes_default_via_data_attr_not_inline_js():
    out = _render()
    assert 'data-default="' + DEFAULT_SPEAKER_NAME + '"' in out


def test_speaker_current_name_is_escaped_into_value():
    out = _render(current_name='Brittany "B"')
    assert "Brittany" in out
    assert "&quot;B&quot;" in out


def test_speaker_hint_shows_configured_hostname_not_hardcoded():
    # Regression: the "The address stays X" hint must reflect this
    # speaker's actual JASPER_HOSTNAME, not a baked-in "jts.local".
    out = _render(hostname="jts2.local")
    assert "The address stays <code>jts2.local</code>" in out
    assert "<code>jts.local</code>" not in out


def test_speaker_hint_escapes_hostname():
    out = _render(hostname='evil"<script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_speaker_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(flash="")


def test_speaker_saved_flash_renders_ok_banner():
    out = _render(flash='Saved. Speaker renamed to "Kitchen". Services restarting.')
    assert "banner--ok" in out


def _handler_cls():
    return speaker_setup._make_handler({"state_path": "/tmp/does-not-matter.env"})


def test_public_surface_is_stable():
    assert callable(speaker_setup.make_server)
    assert callable(speaker_setup.main)
    assert callable(speaker_setup._index_html)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "[General]\nName = Old name\nClass = 0x200414\n",
            "[General]\nName = Kitchen\nClass = 0x200414\n",
        ),
        (
            "[General]\n# Name = BlueZ\nClass = 0x200414\n",
            "[General]\nName = Kitchen\nClass = 0x200414\n",
        ),
        (
            "[General]\nClass = 0x200414\n",
            "[General]\nClass = 0x200414\nName = Kitchen\n",
        ),
    ],
    ids=("active", "commented", "absent"),
)
def test_bluez_main_conf_name_rewrites_supported_shapes(
    tmp_path,
    original,
    expected,
):
    conf = tmp_path / "main.conf"
    conf.write_text(original, encoding="utf-8")

    speaker_setup._write_bluez_main_conf_name("Kitchen", str(conf))

    assert conf.read_text(encoding="utf-8") == expected
    assert conf.stat().st_mode & 0o777 == 0o644


def test_bluez_main_conf_missing_is_fail_soft(tmp_path, caplog):
    conf = tmp_path / "main.conf"

    with caplog.at_level(logging.WARNING, logger=speaker_setup.__name__):
        speaker_setup._write_bluez_main_conf_name("Kitchen", str(conf))

    assert not conf.exists()
    assert "event=speaker_name.bluez_conf_missing" in caplog.text
    assert f"path={conf}" in caplog.text


def test_bluez_main_conf_identical_name_does_not_rewrite(tmp_path, monkeypatch):
    conf = tmp_path / "main.conf"
    original = "[General]\nName = Kitchen\nClass = 0x200414\n"
    conf.write_text(original, encoding="utf-8")

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("identical BlueZ config must not be rewritten")

    monkeypatch.setattr(speaker_setup, "atomic_write_text", unexpected_write)

    speaker_setup._write_bluez_main_conf_name("Kitchen", str(conf))

    assert conf.read_text(encoding="utf-8") == original


def test_bluez_main_conf_uses_canonical_atomic_writer(tmp_path, monkeypatch):
    conf = tmp_path / "main.conf"
    conf.write_text("[General]\nName = Old\n", encoding="utf-8")
    calls = []

    def record_write(path, text, **kwargs):
        calls.append((path, text, kwargs))

    monkeypatch.setattr(speaker_setup, "atomic_write_text", record_write)

    speaker_setup._write_bluez_main_conf_name("Kitchen", str(conf))

    assert calls == [
        (
            conf,
            "[General]\nName = Kitchen\n",
            {"mode": 0o644, "group_from_parent": True},
        ),
    ]


def test_bluez_main_conf_invalid_utf8_is_fail_soft(tmp_path, caplog):
    conf = tmp_path / "main.conf"
    conf.write_bytes(b"[General]\nName = \xff\n")

    with caplog.at_level(logging.WARNING, logger=speaker_setup.__name__):
        speaker_setup._write_bluez_main_conf_name("Kitchen", str(conf))

    assert conf.read_bytes() == b"[General]\nName = \xff\n"
    assert "event=speaker_name.bluez_conf" in caplog.text
    assert "result=failed" in caplog.text
    assert "operation=read" in caplog.text
    bluez_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=speaker_name.bluez_conf ")
    ]
    assert len(bluez_records) == 1


@pytest.mark.parametrize("gadget_active", [False, True])
def test_apply_name_orders_surfaces_and_composes_restart_list(
    monkeypatch,
    gadget_active,
):
    events = []
    original_units = list(speaker_setup.RESTART_UNITS)

    def unit_active(unit):
        events.append(("probe", unit))
        return gadget_active

    async def set_alias(name):
        events.append(("alias", name))

    def render_advert(name):
        events.append(("advert", name))
        return True

    monkeypatch.setattr(speaker_setup, "_unit_active", unit_active)
    monkeypatch.setattr(
        speaker_setup,
        "_write_bluez_main_conf_name",
        lambda name: events.append(("bluez_conf", name)),
    )
    monkeypatch.setattr("jasper.bluetooth.adapter.set_alias", set_alias)
    monkeypatch.setattr(
        "jasper.control_advert.render_control_advert",
        render_advert,
    )
    monkeypatch.setattr(
        speaker_setup,
        "_restart_units",
        lambda units, *, verb="restart", no_block=True, timeout=5.0: events.append(
            (verb, tuple(units), no_block, timeout)
        ),
    )
    monkeypatch.setattr(
        speaker_setup,
        "kick_source_reconcile",
        lambda **_kwargs: events.append(("source-reconcile",)) or {"ok": True},
    )

    assert speaker_setup._apply_name("Kitchen") is True

    expected_units = tuple(
        [
            *original_units,
            *(["jasper-usbgadget.service"] if gadget_active else []),
        ],
    )
    assert events == [
        ("probe", "jasper-usbgadget.service"),
        ("bluez_conf", "Kitchen"),
        ("alias", "Kitchen"),
        ("advert", "Kitchen"),
        (
            "try-restart",
            tuple(speaker_setup.SOURCE_TRY_RESTART_UNITS),
            False,
            60.0,
        ),
        ("source-reconcile",),
        ("source-reconcile",),
        ("restart", expected_units, True, 5.0),
    ]
    assert speaker_setup.RESTART_UNITS == original_units


def test_apply_name_continues_after_bluez_alias_and_advert_failures(
    tmp_path,
    monkeypatch,
    caplog,
):
    conf = tmp_path / "main.conf"
    conf.write_text("[General]\nName = Old\n", encoding="utf-8")
    events = []
    real_write_bluez = speaker_setup._write_bluez_main_conf_name

    def fail_atomic_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    def write_bluez(name):
        real_write_bluez(name, str(conf))
        events.append(("bluez_returned", name))

    async def fail_alias(name):
        events.append(("alias", name))
        raise RuntimeError("BlueZ unavailable")

    def fail_advert(name):
        events.append(("advert", name))
        raise RuntimeError("Avahi unavailable")

    monkeypatch.setattr(speaker_setup, "_unit_active", lambda _unit: False)
    monkeypatch.setattr(speaker_setup, "atomic_write_text", fail_atomic_write)
    monkeypatch.setattr(
        speaker_setup,
        "_write_bluez_main_conf_name",
        write_bluez,
    )
    monkeypatch.setattr("jasper.bluetooth.adapter.set_alias", fail_alias)
    monkeypatch.setattr(
        "jasper.control_advert.render_control_advert",
        fail_advert,
    )
    monkeypatch.setattr(
        speaker_setup,
        "_restart_units",
        lambda units, *, verb="restart", no_block=True, timeout=5.0: events.append(
            (verb, tuple(units), no_block, timeout)
        ),
    )
    monkeypatch.setattr(
        speaker_setup,
        "kick_source_reconcile",
        lambda **_kwargs: events.append(("source-reconcile",)) or {"ok": True},
    )

    with caplog.at_level(logging.WARNING, logger=speaker_setup.__name__):
        assert speaker_setup._apply_name("Kitchen") is True

    assert events == [
        ("bluez_returned", "Kitchen"),
        ("alias", "Kitchen"),
        ("advert", "Kitchen"),
        ("restart", ("bluetooth.service",), False, 60.0),
        (
            "try-restart",
            tuple(speaker_setup.SOURCE_TRY_RESTART_UNITS),
            False,
            60.0,
        ),
        ("source-reconcile",),
        ("source-reconcile",),
        ("restart", tuple(speaker_setup.RESTART_UNITS), True, 5.0),
    ]
    assert conf.read_text(encoding="utf-8") == "[General]\nName = Old\n"
    assert "event=speaker_name.bluez_conf" in caplog.text
    assert "operation=write" in caplog.text
    assert "event=speaker_name.bluetooth_alias" in caplog.text
    assert "event=speaker_name.avahi" in caplog.text
    bluez_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=speaker_name.bluez_conf ")
    ]
    assert len(bluez_records) == 1


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(
        speaker_setup,
        "read_state",
        lambda path: types.SimpleNamespace(name="Kitchen", room=""),
    )
    handler = _handler_cls()
    h = FakeHandler("/")
    handler.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out


def test_post_unknown_route_404s():
    handler = _handler_cls()
    h = FakeHandler("/nope", body=b"")
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_save_validation_error_redirects_with_flash(monkeypatch):
    token = "z" * 64

    def boom(_name):
        raise SpeakerNameError("Name too long")

    monkeypatch.setattr(speaker_setup, "validate_name", boom)
    handler = _handler_cls()
    # csrf_token is the form field (_common.CSRF_FORM_FIELD); jts_csrf is the
    # double-submit cookie. They must carry the same token to pass guard_mutating_request.
    body = ("csrf_token=" + token + "&name=waytoolong").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["./"]


def test_post_save_applies_rename_and_restarts(monkeypatch):
    token = "y" * 64
    calls = {"apply": [], "write": []}

    monkeypatch.setattr(speaker_setup, "validate_name", lambda n: n.strip())
    monkeypatch.setattr(speaker_setup, "validate_room", lambda r: r.strip())
    monkeypatch.setattr(
        speaker_setup,
        "read_state",
        lambda path: types.SimpleNamespace(name="OldName", room=""),
    )
    monkeypatch.setattr(speaker_setup, "_find_conflicts", lambda name: [])
    monkeypatch.setattr(
        speaker_setup,
        "write_state",
        lambda name, room, path, mode=0o644: (
            calls["write"].append((name, room)) or name
        ),
    )
    monkeypatch.setattr(
        speaker_setup,
        "_apply_name",
        lambda name: calls["apply"].append(name) or True,
    )

    handler = _handler_cls()
    # csrf_token = form field (CSRF_FORM_FIELD); jts_csrf = double-submit cookie.
    body = ("csrf_token=" + token + "&name=NewName&room=Kitchen").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert calls["write"] == [("NewName", "Kitchen")]
    assert calls["apply"] == ["NewName"]


def test_post_save_surfaces_source_reconcile_failure(monkeypatch):
    token = "y" * 64
    monkeypatch.setattr(speaker_setup, "validate_name", lambda n: n.strip())
    monkeypatch.setattr(speaker_setup, "validate_room", lambda r: r.strip())
    monkeypatch.setattr(
        speaker_setup,
        "read_state",
        lambda path: types.SimpleNamespace(name="OldName", room=""),
    )
    monkeypatch.setattr(speaker_setup, "_find_conflicts", lambda name: [])
    monkeypatch.setattr(
        speaker_setup,
        "write_state",
        lambda name, room, path, mode=0o644: name,
    )
    monkeypatch.setattr(speaker_setup, "_apply_name", lambda name: False)

    handler = _handler_cls()
    body = ("csrf_token=" + token + "&name=NewName&room=Kitchen").encode()
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    flash_cookie = "\n".join(h.header_values("Set-Cookie"))
    assert "some%20audio%20sources%20could%20not%20restart" in flash_cookie


def test_post_save_rejects_bad_csrf(monkeypatch):
    monkeypatch.setattr(
        speaker_setup,
        "read_state",
        lambda path: types.SimpleNamespace(name="OldName"),
    )
    handler = _handler_cls()
    # Form-field token (csrf_token) deliberately differs from the cookie token,
    # so the double-submit compare fails -> 403, no rename.
    body = b"csrf_token=" + b"a" * 64 + b"&name=NewName"
    h = FakeHandler("/save", body=body, cookies="jts_csrf=" + "b" * 64)
    handler.do_POST(h)
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
