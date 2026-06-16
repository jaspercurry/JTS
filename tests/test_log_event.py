"""Tests for jasper.log_event — the canonical structured-log emitter.

Pins the logfmt rendering (so the on-the-wire `event=` shape stays
grep-stable), the quoting/escaping that fixes the broken-parse bug for
values with whitespace/`=`/quotes (the SSID-with-a-space case), the
predictable scalar rendering, the opt-in JASPER_LOG_JSON sink, and the
fact that `log_event` routes through the caller's own logger at the
requested level. All stdlib, no hardware.
"""
from __future__ import annotations

import json
import logging

import pytest

from jasper.log_event import (
    json_mode_enabled,
    log_event,
    render_json,
    render_logfmt,
)


# --------------------------------------------------------------- logfmt render


def test_logfmt_clean_values_are_byte_identical_to_hand_written():
    # The common case must stay exactly what the old f-strings emitted,
    # so existing journal greps and parsers don't break.
    line = render_logfmt(
        "knob.action",
        {"device": "VK-01", "key": "KEY_MUTE", "path": "/mic/mute", "status": 200},
    )
    assert line == "event=knob.action device=VK-01 key=KEY_MUTE path=/mic/mute status=200"


def test_logfmt_preserves_field_order():
    line = render_logfmt("d.a", {"b": 1, "a": 2, "c": 3})
    assert line == "event=d.a b=1 a=2 c=3"


def test_logfmt_event_name_is_not_quoted():
    # Names are domain.action vocabulary, never untrusted; quoting them
    # would churn every grep.
    assert render_logfmt("wifi_scan_repair.attempt", {}) == "event=wifi_scan_repair.attempt"


# ------------------------------------------------- the bug this module fixes


def test_logfmt_quotes_value_with_whitespace_ssid_case():
    # An SSID with a space silently corrupted the key=val parse before
    # this helper existed. It must be quoted so `ssid` is one field.
    line = render_logfmt("wifi_guardian.recover", {"ssid": "My Home Wifi", "ok": True})
    assert line == 'event=wifi_guardian.recover ssid="My Home Wifi" ok=true'


def test_logfmt_quotes_and_escapes_embedded_quotes():
    line = render_logfmt("x.y", {"label": 'a "quoted" name'})
    assert line == r'event=x.y label="a \"quoted\" name"'


def test_logfmt_escapes_backslash_before_quote():
    # Backslashes are escaped first so a trailing one can't escape the
    # closing quote.
    line = render_logfmt("x.y", {"path": r"C:\dir\x"})
    assert line == r'event=x.y path="C:\\dir\\x"'


def test_logfmt_quotes_value_with_equals_sign():
    line = render_logfmt("x.y", {"kv": "a=b"})
    assert line == 'event=x.y kv="a=b"'


def test_logfmt_quotes_empty_string():
    # An empty value would otherwise render `key=` and read like a
    # missing value; quote it so the field is unambiguous.
    assert render_logfmt("x.y", {"reason": ""}) == 'event=x.y reason=""'


# ------------------------------------------------------ scalar rendering rules


def test_logfmt_none_renders_null():
    assert render_logfmt("x.y", {"driver": None}) == "event=x.y driver=null"


def test_logfmt_bool_renders_lowercase_words():
    assert render_logfmt("x.y", {"ack": True, "stale": False}) == "event=x.y ack=true stale=false"


def test_logfmt_float_keeps_decimal_point():
    # repr-based so 1.0 stays 1.0 (a plain str(1.0) is "1.0" but int-ish
    # floats from arithmetic shouldn't collapse to an int token).
    assert render_logfmt("x.y", {"remaining": 1.0}) == "event=x.y remaining=1.0"
    assert render_logfmt("x.y", {"remaining": 12.5}) == "event=x.y remaining=12.5"


def test_logfmt_bool_not_treated_as_int():
    # bool is an int subclass; the bool branch must win so True != 1.
    assert "ack=true" in render_logfmt("x.y", {"ack": True})


# --------------------------------------------------------------- JSON mode


def test_json_mode_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)
    assert json_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enabled"])
def test_json_mode_truthy_values(monkeypatch, value):
    monkeypatch.setenv("JASPER_LOG_JSON", value)
    assert json_mode_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "  "])
def test_json_mode_falsy_values(monkeypatch, value):
    monkeypatch.setenv("JASPER_LOG_JSON", value)
    assert json_mode_enabled() is False


def test_render_json_event_is_first_key_and_carries_fields():
    line = render_json("knob.adjust", {"device": "VK-01", "delta": "+5", "status": 200})
    obj = json.loads(line)
    assert obj == {"event": "knob.adjust", "device": "VK-01", "delta": "+5", "status": 200}
    assert list(obj.keys())[0] == "event"


def test_render_json_handles_whitespace_value_without_escaping():
    # In JSON mode the SSID-with-a-space just round-trips as a string.
    obj = json.loads(render_json("wifi_guardian.recover", {"ssid": "My Home Wifi"}))
    assert obj["ssid"] == "My Home Wifi"


def test_render_json_falls_back_to_str_for_nonjson_values():
    obj = json.loads(render_json("x.y", {"err": ValueError("boom")}))
    assert obj["err"] == "boom"


# ----------------------------------------------- log_event end-to-end emission


def test_log_event_emits_logfmt_through_logger(caplog):
    logger = logging.getLogger("jasper.test.log_event.logfmt")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "knob.action", device="VK-01", status=200)
    assert caplog.records[-1].getMessage() == "event=knob.action device=VK-01 status=200"
    assert caplog.records[-1].levelno == logging.INFO


def test_log_event_respects_level_arg(caplog):
    logger = logging.getLogger("jasper.test.log_event.level")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log_event(logger, "knob.action.failed", level=logging.WARNING, err="nope")
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].getMessage() == "event=knob.action.failed err=nope"


def test_log_event_emits_json_when_enabled(monkeypatch, caplog):
    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    logger = logging.getLogger("jasper.test.log_event.json")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "knob.action", device="VK-01", status=200)
    obj = json.loads(caplog.records[-1].getMessage())
    assert obj == {"event": "knob.action", "device": "VK-01", "status": 200}


def test_log_event_percent_in_value_is_safe(caplog):
    # The line is fully rendered before reaching logger.log, so a literal
    # `%` in a value can't trip %-formatting (no lazy args are passed).
    logger = logging.getLogger("jasper.test.log_event.percent")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "x.y", note="100% done")
    assert caplog.records[-1].getMessage() == 'event=x.y note="100% done"'


def test_log_event_fields_can_shadow_logger_and_name(caplog):
    # logger/name are positional-only, so a field literally named
    # "name" or "logger" is fine.
    logger = logging.getLogger("jasper.test.log_event.shadow")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "x.y", name="bob", logger="other")
    assert caplog.records[-1].getMessage() == "event=x.y name=bob logger=other"


def test_log_event_default_carries_no_traceback(caplog):
    # The common path must stay byte-identical to a plain logger.info:
    # no exc_info attached even when called from inside an except block.
    logger = logging.getLogger("jasper.test.log_event.no_exc")
    with caplog.at_level(logging.INFO, logger=logger.name):
        try:
            raise ValueError("boom")
        except ValueError:
            log_event(logger, "x.y", k="v")
    assert caplog.records[-1].exc_info is None


def test_log_event_exc_info_attaches_traceback(caplog):
    # exc_info=True from an except block attaches the active exception —
    # the logger.exception("event=...") equivalent the migration relies on.
    logger = logging.getLogger("jasper.test.log_event.exc")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise ValueError("boom")
        except ValueError:
            log_event(logger, "x.crash", level=logging.ERROR, exc_info=True)
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "event=x.crash"
    assert record.exc_info is not None
    assert record.exc_info[0] is ValueError


def test_log_event_fields_dict_carries_reserved_level_key(caplog):
    # A field literally named "level" can't be a keyword (it binds the
    # logging level), so it rides the explicit fields= mapping. The
    # logging level stays separate.
    logger = logging.getLogger("jasper.test.log_event.level_field")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log_event(
            logger,
            "volume.reconciled",
            level=logging.WARNING,
            fields={"source": "spotify", "level": 42},
        )
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].getMessage() == "event=volume.reconciled source=spotify level=42"


def test_log_event_fields_dict_orders_before_kwfields(caplog):
    # fields= entries render first (in dict order), then **kwfields.
    logger = logging.getLogger("jasper.test.log_event.fields_order")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "x.y", fields={"a": 1, "level": 2}, b=3)
    assert caplog.records[-1].getMessage() == "event=x.y a=1 level=2 b=3"


def test_log_event_fields_dict_in_json_mode(monkeypatch, caplog):
    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    logger = logging.getLogger("jasper.test.log_event.fields_json")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "volume.set", fields={"level": 7})
    obj = json.loads(caplog.records[-1].getMessage())
    assert obj == {"event": "volume.set", "level": 7}


# ---------------------------------------------------- cheap shape contract


@pytest.mark.parametrize(
    "name",
    ["knob.action", "knob.adjust.failed", "wifi_scan_repair.attempt_failed"],
)
def test_emits_domain_action_shape(name):
    # The rendered line always starts with event=<domain>.<action>.
    line = render_logfmt(name, {"k": "v"})
    assert line.startswith(f"event={name} ")
    event_token = line.split(" ", 1)[0]
    assert event_token.startswith("event=")
    assert "." in event_token.split("=", 1)[1]
