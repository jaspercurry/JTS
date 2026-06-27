#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-env-file.sh).

# Canonical list of audio clients to STOP before restarting the core DSP
# graph (CamillaDSP / outputd / fan-in) so DAC and Camilla ALSA ownership
# can be reclaimed.
#
# Why this exists: these are the units that can hold fan-in, Camilla,
# outputd, or renderer ALSA endpoints during deploy/runtime churn. If the
# core graph restarts while one of them still owns /dev/snd, CamillaDSP or
# outputd start fails with "Device or resource busy" (EBUSY) — the exact
# failure class the camilla EBUSY recovery handler exists to fix
# (docs/HANDOFF-resilience.md, the 2026-06-25 JTS5 incident).
#
# This list was duplicated byte-for-byte across the recovery handler
# (deploy/bin/jasper-camilla-recover) and the installer's pre-restart park
# step (deploy/lib/install/park_audio_clients_for_core_graph_restart),
# with no shared source and no test pinning them equal — so a future edit
# to one (e.g. a new renderer that holds the DAC) would drift the other
# and re-leak a holder. Both consumers now `source` this single definition
# and iterate the array; tests/test_core_graph_park_units_contract.py pins
# that no re-inlined copy survives in either file.
#
# Scope: this is the DEPLOY/RECOVERY park set (full speaker hardware
# ownership reclaim). It is intentionally NOT the same set as
# jasper.local_sources.registry.local_source_park_units() (the multiroom-
# follower park set, which parks bluealsa/bt-agent/usbsink and omits the
# core daemons) — those are different concerns. Keep them separate.
#
# Missing units are harmless on streambox or partial installs (both
# consumers stop best-effort and ignore not-found).

# SC2034 (appears unused) — consumed by the sourcing scripts
# (jasper-camilla-recover, deploy/lib/install/systemd-units.sh).
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
