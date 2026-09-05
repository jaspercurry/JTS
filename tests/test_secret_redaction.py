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

from jasper.secret_redaction import SECRET_ENV_NAME_RE, redact_secrets

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
    ("env_openai", "OPENAI_API_KEY=sk-proj-AbCdEf0123456789",
     "OPENAI_API_KEY=<redacted>", True),
    ("env_gemini", "GEMINI_API_KEY=AIzaSyD1e2f3G4h5I6j7K8",
     "GEMINI_API_KEY=<redacted>", True),
    ("env_xai", "XAI_API_KEY=xai-Ab12Cd34Ef56Gh78", "XAI_API_KEY=<redacted>", True),
    ("env_google_client_secret", "GOOGLE_CLIENT_SECRET=GOCSPX-Ab12Cd34Ef56",
     "GOOGLE_CLIENT_SECRET=<redacted>", True),
    ("env_google_routes", "GOOGLE_ROUTES_API_KEY=AIzaSyRoUtEs123456",
     "GOOGLE_ROUTES_API_KEY=<redacted>", True),
    ("env_ha_token", "JASPER_HA_TOKEN=eyJhbGciOi.eyJpc3Mi.sIgXyZ",
     "JASPER_HA_TOKEN=<redacted>", True),
    ("env_wifi_psk", "JASPER_WIFI_PSK=hunter2xylophone",
     "JASPER_WIFI_PSK=<redacted>", True),
    ("env_bustime_key", "JASPER_MTA_BUSTIME_KEY=0d4e6c2a-9f1b-4c3d",
     "JASPER_MTA_BUSTIME_KEY=<redacted>", True),
    ("env_relay_token", "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=rl_9f8e7d6c5b4a",
     "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=<redacted>", True),
    ("env_double_quoted", 'OPENAI_API_KEY="sk-proj-QuOtEd0123456789"',
     "OPENAI_API_KEY=<redacted>", True),
    ("env_single_quoted", "JASPER_HA_TOKEN='eyJhbG.SiNgLe.QuOtEd99'",
     "JASPER_HA_TOKEN=<redacted>", True),
    ("env_colon_separator", "JASPER_HA_TOKEN: eyJhbGciCoLoN9876",
     "JASPER_HA_TOKEN: <redacted>", True),
    ("env_value_with_spaces", "JASPER_WIFI_PSK=my long wifi passphrase",
     "JASPER_WIFI_PSK=<redacted>", True),
    ("env_inline_in_log_line",
     "2026-09-05 voice[1]: rejected OPENAI_API_KEY=sk-proj-LoGgEd01234567 invalid",
     "2026-09-05 voice[1]: rejected OPENAI_API_KEY=<redacted> invalid", True),
    ("env_inline_quoted_values",
     "started SPOTIFY_CLIENT_SECRET='two words' JASPER_WIFI_PSK=\"wifi words\" ok",
     "started SPOTIFY_CLIENT_SECRET=<redacted> JASPER_WIFI_PSK=<redacted> ok", True),
    ("systemd_environment_bare", "Environment=OPENAI_API_KEY=sk-live1234",
     "Environment=OPENAI_API_KEY=<redacted>", True),
    ("systemd_environment_double_quoted", 'Environment="JASPER_WIFI_PSK=two words"',
     'Environment="JASPER_WIFI_PSK=<redacted>"', True),
    ("systemd_environment_single_quoted",
     "Environment='SPOTIFY_CLIENT_SECRET=quoted secret'",
     "Environment='SPOTIFY_CLIENT_SECRET=<redacted>'", True),
    ("url_key_param", "GET https://bt.mta.info/api?key=0d4e6c2a9f1b&lat=1",
     "GET https://bt.mta.info/api?key=<redacted>&lat=1", True),
    ("url_key_param_not_first", "https://example.com/?lat=40.65&key=ABC123XYZ&lon=-73.9",
     "https://example.com/?lat=40.65&key=<redacted>&lon=-73.9", True),
    ("httpx_error_repr_with_url", _HTTPX_REPR % "SECRET_VAL",
     _HTTPX_REPR % "<redacted>", True),
    ("json_refresh_token", '{"refresh_token": "1//0gABCDEFGHijk"}',
     '{"refresh_token": <redacted>}', False),
    ("json_api_key", '{"api_key": "sk-jsonAbCd12345678"}',
     '{"api_key": <redacted>}', False),
    ("json_spotify_access_token", '{"access_token": "BQAbCdEf1234567890"}',
     '{"access_token": <redacted>}', False),
    ("bearer_token", "Bearer eyJhbGciOiJIUzI1NiJ9xyz", "Bearer <redacted>", False),
    ("authorization_bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiAA",
     "Authorization: Bearer <redacted>", False),
    ("basic_auth", "Authorization: Basic am9lOnMzY3IzdA==",
     "Authorization: Basic <redacted>", False),
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
    ("jts_token_header", "X-JTS-Token: t0k3nV4lu3ForTheLan",
     "X-JTS-Token: <redacted>", False),
    ("nmcli_argv_psk", "nmcli dev wifi connect Home password hunter2xylophone",
     "nmcli dev wifi connect Home password <redacted>", False),
    ("nmcli_argv_quoted_psk", "nmcli device wifi connect 'My Net' password 'my long psk'",
     "nmcli device wifi connect 'My Net' password <redacted>", False),
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
    ("negative_google_client_id", "GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com",
     "GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com", True),
    ("negative_wifi_key_mgmt", "JASPER_WIFI_KEY_MGMT=wpa-psk",
     "JASPER_WIFI_KEY_MGMT=wpa-psk", True),
) + tuple(
    (f"name_{name.lower()}", f"{name}=S3CR3TV4LUE", f"{name}=<redacted>", True)
    for name in SECRET_ENV_NAMES
)


def _bash_redact(text: str) -> str:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; . {_BASH_REDACTOR}; redact_jasper_diagnostics",
        ],
        input=f"{text}\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.removesuffix("\n")


@pytest.mark.parametrize(
    ("text", "expected"),
    [pytest.param(text, expected, id=cid) for cid, text, expected, _ in CASES],
)
def test_python_redactor(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [pytest.param(t, e, id=cid) for cid, t, e, in_bash_mandate in CASES if in_bash_mandate],
)
def test_bash_redactor(text: str, expected: str) -> None:
    assert _bash_redact(text) == expected


@pytest.mark.parametrize("name", SECRET_ENV_NAMES)
def test_secret_env_name_pattern_covers_every_real_key(name: str) -> None:
    assert re.fullmatch(SECRET_ENV_NAME_RE, name) is not None


@pytest.mark.parametrize("name", PUBLIC_ENV_NAMES)
def test_secret_env_name_pattern_leaves_public_names_alone(name: str) -> None:
    assert re.fullmatch(SECRET_ENV_NAME_RE, name) is None
