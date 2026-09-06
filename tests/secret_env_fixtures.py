# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared secret/public env-name vocabulary for the redactor and doctor
env-secrets suites — one case table, not two."""

from __future__ import annotations

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
