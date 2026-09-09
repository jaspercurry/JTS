#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-env-file.sh).

# The audio clients to STOP before a core-graph (CamillaDSP / outputd /
# fan-in) restart: the units that can hold a fan-in, Camilla, outputd, or
# renderer ALSA endpoint during deploy churn, which would fail the graph start
# with "Device or resource busy".
#
# Scope: this is the DEPLOY park set (full speaker hardware ownership
# reclaim). It is deliberately NOT
# jasper.local_sources.registry.local_source_park_units() (the multiroom-
# follower set, which parks bluealsa/bt-agent/usbsink and omits the core
# daemons). Keep them separate.
#
# Missing units are harmless on streambox or partial installs: the consumer
# stops best-effort and ignores not-found.

# SC2034 (appears unused) — consumed by deploy/lib/install/systemd-units.sh.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_PARK_UNITS=(
    jasper-voice.service
    jasper-aec-bridge.service
    jasper-outputd.service
    jasper-camilla-crossover.service
    jasper-snapclient.service
    jasper-snapserver.service
    shairport-sync.service
    nqptp.service
    librespot.service
    bluealsa-aplay.service
    jasper-mux.service
)
