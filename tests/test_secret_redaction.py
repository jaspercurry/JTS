# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One case table for both redactors.

`jasper/secret_redaction.py` is the Python one; `redact_jasper_diagnostics`
in `scripts/_diagnostic_redaction.sh` is the bash one that guards the support
bundle when the venv is the broken thing. Rows flagged `bash=True` sit inside
the bash redactor's mandate and must come out identical from both.
Non-negotiable: no live credential may survive into a log, `/state`, doctor
output or a bundle.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from jasper.secret_redaction import (
    SECRET_ENV_NAME_RE,
    SECRET_ENV_SUFFIX_RE,
    redact_secrets,
)

ROOT = Path(__file__).resolve().parents[1]
_BASH_REDACTOR = ROOT / "scripts" / "_diagnostic_redaction.sh"

# Every secret-bearing env name the project defines or reads.
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_ROUTES_API_KEY",
    "JASPER_HA_TOKEN",
    "JASPER_MTA_BUSTIME_KEY",
    "JASPER_WIFI_PSK",
    "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN",
)

# Public identifiers that ride the same env files and must survive both.
PUBLIC_ENV_NAMES = ("GOOGLE_CLIENT_ID", "SPOTIFY_CLIENT_ID", "JASPER_WIFI_KEY_MGMT")

_HTTPX_REPR = (
    "HTTPStatusError(\"Server error '500' for url "
    "'https://bt.mta.info/api/?key=%s&x=y'\")"
)

# (id, text, expected, also_in_the_bash_redactor's_mandate)
CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("env_double_quoted", 'OPENAI_API_KEY="sk-proj-QuOtEd0123456789"',
     "OPENAI_API_KEY=<redacted>", True),
    ("env_single_quoted", "JASPER_HA_TOKEN='eyJhbG.SiNgLe.QuOtEd99'",
     "JASPER_HA_TOKEN=<redacted>", True),
    ("env_colon_separator", "JASPER_HA_TOKEN: eyJhbGciCoLoN9876",
     "JASPER_HA_TOKEN: <redacted>", True),
    ("env_value_with_spaces", "JASPER_WIFI_PSK=my long wifi passphrase",
     "JASPER_WIFI_PSK=<redacted>", True),
    # The `_PASSWORD`/`_PASSPHRASE` halves of the naming convention, which
    # no key the project defines today exercises.
    ("env_password_suffix", "SMB_PASSWORD=hunter2xylophone",
     "SMB_PASSWORD=<redacted>", True),
    ("env_passphrase_suffix", "BACKUP_PASSPHRASE=correct horse battery staple",
     "BACKUP_PASSPHRASE=<redacted>", True),
    ("env_inline_in_log_line",
     "2026-09-05 voice[1]: rejected OPENAI_API_KEY=sk-proj-LoGgEd01234567 invalid",
     "2026-09-05 voice[1]: rejected OPENAI_API_KEY=<redacted> invalid", True),
    ("env_inline_quoted_values",
     "started SPOTIFY_CLIENT_SECRET='two words' JASPER_WIFI_PSK=\"wifi words\" ok",
     "started SPOTIFY_CLIENT_SECRET=<redacted> JASPER_WIFI_PSK=<redacted> ok", True),
    # A value holding the *other* quote character: a run scoped to either
    # quote stops early and leaves the rest of the credential in place.
    ("env_inline_double_quoted_apostrophe",
     "env JASPER_WIFI_PSK=\"don't tell anyone\" python",
     "env JASPER_WIFI_PSK=<redacted> python", True),
    ("systemd_environment_bare", "Environment=OPENAI_API_KEY=sk-live1234",
     "Environment=OPENAI_API_KEY=<redacted>", True),
    ("systemd_environment_double_quoted", 'Environment="JASPER_WIFI_PSK=two words"',
     'Environment="JASPER_WIFI_PSK=<redacted>"', True),
    ("systemd_environment_single_quoted",
     "Environment='SPOTIFY_CLIENT_SECRET=quoted secret'",
     "Environment='SPOTIFY_CLIENT_SECRET=<redacted>'", True),
    ("systemd_environment_apostrophe_in_value",
     "Environment=\"JASPER_WIFI_PSK=it's mine\"",
     'Environment="JASPER_WIFI_PSK=<redacted>"', True),
    # systemd.exec(5) allows several assignments per Environment= line, and
    # the quoted ones are shell words: everything after the first must
    # redact too.
    ("systemd_environment_multi_assignment",
     'Environment="VAR1=word1 word2" JASPER_WIFI_PSK=word3 "OPENAI_API_KEY=$word 5 6"',
     'Environment="VAR1=word1 word2" JASPER_WIFI_PSK=<redacted> '
     '"OPENAI_API_KEY=<redacted>"', True),
    ("execstart_env_quoted_assignment",
     "ExecStart=/usr/bin/env 'JASPER_HA_TOKEN=eyJ abc' /opt/x",
     "ExecStart=/usr/bin/env 'JASPER_HA_TOKEN=<redacted>' /opt/x", True),
    # Quoted value, no whitespace before the name: the bare-run expression
    # would cut this at the first space.
    ("systemd_environment_quoted_value",
     'Environment=JASPER_WIFI_PSK="two words here"',
     "Environment=JASPER_WIFI_PSK=<redacted>", True),
    ("url_key_param", "GET https://bt.mta.info/api?key=0d4e6c2a9f1b&lat=1",
     "GET https://bt.mta.info/api?key=<redacted>&lat=1", True),
    ("url_key_param_not_first", "https://example.com/?lat=40.65&key=ABC123XYZ&lon=-73.9",
     "https://example.com/?lat=40.65&key=<redacted>&lon=-73.9", True),
    ("httpx_error_repr_with_url", _HTTPX_REPR % "SECRET_VAL",
     _HTTPX_REPR % "<redacted>", True),
    # The value run stops at `&` and `)`: three call sites feed `repr(e)`.
    ("url_access_token_keeps_query_tail",
     "GET /api?access_token=abc123def&lat=40.6&lon=-73.9 200",
     "GET /api?access_token=<redacted>&lat=40.6&lon=-73.9 200", False),
    ("repr_keeps_closing_paren", "RuntimeError(api_key=sk-abcd1234efgh)",
     "RuntimeError(api_key=<redacted>)", False),
    ("json_refresh_token", '{"refresh_token": "1//0gABCDEFGHijk"}',
     '{"refresh_token": <redacted>}', False),
    ("json_api_key", '{"api_key": "sk-jsonAbCd12345678"}',
     '{"api_key": <redacted>}', False),
    ("json_api_key_with_apostrophe", '{"api_key":"ab\'cd1234efgh"}',
     '{"api_key":<redacted>}', False),
    ("json_spotify_access_token", '{"access_token": "BQAbCdEf1234567890"}',
     '{"access_token": <redacted>}', False),
    # `_supervisor` clips a provider body to `_SCAN_LIMIT` before redacting,
    # so the opening quote can arrive without its closing one.
    ("json_truncated_access_token", '{"access_token": "BQAbCdEf1234567890',
     '{"access_token": <redacted>', False),
    ("truncated_quoted_token", 'token="abcdefgh12345', "token=<redacted>", False),
    ("bearer_token", "Bearer eyJhbGciOiJIUzI1NiJ9xyz", "Bearer <redacted>", False),
    ("authorization_bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiAA",
     "Authorization: Bearer <redacted>", False),
    ("bare_openai_prefix", "Incorrect API key provided: sk-abcd1234efgh.",
     "Incorrect API key provided: <redacted>.", False),
    ("bare_google_prefix", "request denied for AIzaSyBareKey123456",
     "request denied for <redacted>", False),
    ("bare_xai_prefix", "auth failed key xai-BareKey12345678",
     "auth failed key <redacted>", False),
    ("bare_google_oauth_secret_prefix", "client rejected GOCSPX-BareCli3ntS3cr3t",
     "client rejected <redacted>", False),
    ("google_api_key_header", "x-goog-api-key: AIzaSyHeAdEr12345678",
     "x-goog-api-key: <redacted>", False),
    ("jts_household_header", "X-JTS-Household: kR3n9QpZ7sT2vX8b",
     "X-JTS-Household: <redacted>", False),
    ("jts_household_header_json", '{"X-JTS-Household": "kR3n9QpZ7sT2vX8b"}',
     '{"X-JTS-Household": <redacted>}', False),
    ("jts_household_header_repr", "{'X-JTS-Household': 'kR3n9QpZ7sT2vX8b'}",
     "{'X-JTS-Household': <redacted>}", False),
    ("jts_token_header", "X-JTS-Token: t0k3nV4lu3ForTheLan",
     "X-JTS-Token: <redacted>", False),
    ("nmcli_argv_psk", "nmcli dev wifi connect Home password hunter2xylophone",
     "nmcli dev wifi connect Home password <redacted>", False),
    ("nmcli_argv_quoted_psk", "nmcli device wifi connect 'My Net' password 'my long psk'",
     "nmcli device wifi connect 'My Net' password <redacted>", False),
    # NetworkManager echoes the submitted PSK back on the property whose
    # value it rejected, spaced and terse. Python-only: bash knows env-name
    # shapes, not keywords, and leaves all three of these alone.
    ("nm_psk_echo_spaced", "802-11-wireless-security.psk: hunter2xylophone",
     "802-11-wireless-security.psk: <redacted>", False),
    ("nm_psk_echo_terse", "802-11-wireless-security.psk:hunter2xylophone",
     "802-11-wireless-security.psk:<redacted>", False),
    # Shell escaping puts the quote characters *inside* the token
    # (`don't` joins as `'don'\''t'`), so a run scoped to one quote stops
    # mid-credential.
    ("password_shell_escaped_quote", "nmcli: password 'don'\\''t' rejected",
     "nmcli: password <redacted> rejected", False),
    # 7 characters: under the bare run's WPA floor, which quoting replaces.
    ("password_short_quoted", "nmcli: password 'sw0rd' rejected",
     "nmcli: password <redacted> rejected", False),
    # A colon-introduced passphrase is words, not one token, so the value
    # runs to end of line. Python-only for the same reason as the NM rows.
    ("passphrase_colon_words", "Passphrase: correct horse battery",
     "Passphrase: <redacted>", False),
    ("password_colon_words", "password: two words", "password: <redacted>", False),
    ("psk_colon_words", "psk: three word psk", "psk: <redacted>", False),
    # Over-redaction, kept deliberately: narrowing the `password <arg>` rule
    # enough to spare this prose also spares an all-lowercase PSK echoed
    # back by nmcli, which the wizard holds no literal for.
    ("password_prose_over_redacted", "wifi password validation failed",
     "wifi password <redacted> failed", False),
    # Negatives: ordinary text neither redactor may touch.
    ("negative_prose_key", "the key is under the mat", "the key is under the mat", True),
    ("negative_tokenizer", "tokenizer=whisper rate=48000",
     "tokenizer=whisper rate=48000", True),
    ("negative_passwordless", "wifi profile is passwordless",
     "wifi profile is passwordless", True),
    ("negative_password_prose", "password reset link sent",
     "password reset link sent", True),
    ("negative_masked_display", "saved key sk-p…6789", "saved key sk-p…6789", True),
    ("negative_plain_kv", "backend=alsa sink=hw:0,0", "backend=alsa sink=hw:0,0", True),
    ("negative_wpa_psk_key_mgmt", "key-mgmt=wpa-psk connection activated",
     "key-mgmt=wpa-psk connection activated", True),
    # NetworkManager's own message for a mistyped PSK, which the wizard
    # shows in its banner: `.psk:` names a property, not a credential.
    ("negative_nm_psk_property_error",
     "Error: 802-11-wireless-security.psk: property is invalid",
     "Error: 802-11-wireless-security.psk: property is invalid", True),
    ("negative_basic_auth_prose", "Basic authentication failed for user",
     "Basic authentication failed for user", True),
    # A key at end of line must not reach across into the next one.
    ("negative_password_colon_block", "password:\n  reset_count: 3",
     "password:\n  reset_count: 3", False),
) + tuple(
    (f"name_{name.lower()}", f"{name}=S3CR3TV4LUE", f"{name}=<redacted>", True)
    for name in SECRET_ENV_NAMES
) + tuple(
    (f"public_{name.lower()}", f"{name}=pub1ic-1dent1f1er",
     f"{name}=pub1ic-1dent1f1er", True)
    for name in PUBLIC_ENV_NAMES
) + tuple(
    # WPA passphrases are 8-63 printable ASCII: every one of these is legal
    # in a PSK and ends the keyword rule's value class, so the assignment
    # rule has to take the run to the next whitespace like bash does.
    (f"env_inline_psk_with_{name}", f"started JASPER_WIFI_PSK=ab{ch}cd1234 ok",
     "started JASPER_WIFI_PSK=<redacted> ok", True)
    for name, ch in (
        ("ampersand", "&"), ("paren", ")"), ("comma", ","),
        ("semicolon", ";"), ("brace", "}"),
    )
)

_BASH_CASES = tuple((cid, text, expected) for cid, text, expected, m in CASES if m)


@pytest.fixture(scope="module")
def bash_redacted() -> dict[str, str]:
    """The whole bash mandate through one `bash`, keyed by case id.

    The redactor is line-oriented, so a joined corpus redacts row for row —
    and the table is the growth path for every daemon's shapes, so one
    subprocess per row does not scale.
    """
    assert not any("\n" in text for _, text, _ in _BASH_CASES)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; . {_BASH_REDACTOR}; redact_jasper_diagnostics",
        ],
        input="".join(f"{text}\n" for _, text, _ in _BASH_CASES),
        text=True,
        capture_output=True,
        check=True,
    )
    lines = proc.stdout.split("\n")[:-1]
    assert len(lines) == len(_BASH_CASES)
    return {cid: line for (cid, _, _), line in zip(_BASH_CASES, lines)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [pytest.param(text, expected, id=cid) for cid, text, expected, _ in CASES],
)
def test_python_redactor(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


@pytest.mark.parametrize(
    ("cid", "expected"),
    [pytest.param(cid, expected, id=cid) for cid, _, expected in _BASH_CASES],
)
def test_bash_redactor(cid: str, expected: str, bash_redacted: dict[str, str]) -> None:
    assert bash_redacted[cid] == expected


@pytest.mark.parametrize("name", SECRET_ENV_NAMES)
def test_secret_env_name_pattern_covers_every_real_key(name: str) -> None:
    assert re.fullmatch(SECRET_ENV_NAME_RE, name) is not None


@pytest.mark.parametrize("name", PUBLIC_ENV_NAMES)
def test_secret_env_name_pattern_leaves_public_names_alone(name: str) -> None:
    assert re.fullmatch(SECRET_ENV_NAME_RE, name) is None


@pytest.mark.parametrize("literal", ["k(e)y+1*2", "two word secret"])
def test_a_caller_held_literal_is_replaced_whatever_shape_it_has(literal: str) -> None:
    """A pasted key in no recognised shape is removable only by its value,
    and `str.replace` is literal — regex metacharacters and spaces alike."""
    assert (
        redact_secrets(f"rejected {literal} upstream", [literal])
        == "rejected <redacted> upstream"
    )


@pytest.mark.parametrize("name", SECRET_ENV_NAMES)
def test_the_suffix_rule_is_the_convention_without_the_key_predating_it(
    name: str,
) -> None:
    """Both redactors scrub `JASPER_MTA_BUSTIME_KEY`, but it is also a
    documented `/etc/jasper/jasper.env` key, so a consumer of the convention
    alone — "this name may live only in a compartment" — must not see it."""
    composed = rf"[A-Za-z_][A-Za-z0-9_]*{SECRET_ENV_SUFFIX_RE}"

    matched = re.fullmatch(composed, name) is not None

    assert matched is (name != "JASPER_MTA_BUSTIME_KEY")
