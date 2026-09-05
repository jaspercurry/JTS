#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-env-file.sh).

# Canonical rosters for a core-graph (CamillaDSP / outputd / fan-in) restart:
# the audio clients to STOP so DAC and Camilla ALSA ownership can be
# reclaimed, and — below — how each of them comes back.
#
# Why this exists: these are the units that can hold fan-in, Camilla,
# outputd, or renderer ALSA endpoints during deploy/runtime churn. If the
# core graph restarts while one of them still owns /dev/snd, CamillaDSP or
# outputd start fails with "Device or resource busy" (EBUSY) — the exact
# failure class the camilla EBUSY recovery handler exists to fix
# (the 2026-06-25 JTS5 incident).
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

# The RESTORE side: the source clients the recovery ladder starts again
# itself, in the order it starts them (shairport-sync carries Requires= and
# After= on nqptp, so the clock leads). Started `is-enabled`-gated, so a unit
# a profile or reconciler parked on purpose stays parked.
#
# bt-agent.service is start-only, and deliberately absent from the park list:
# the Bluetooth ALSA endpoint is bluealsa-aplay's, not the pairing agent's, so
# stopping it reclaims no device and drops an in-flight pairing — but the
# multiroom-follower park set does stop it, so the ladder puts it back with the
# rest of the source clients.
#
# Consumed by jasper-camilla-recover only. The installer parks with the list
# above and restores through its own ordered restart/reconcile steps instead.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_RESTORE_UNITS=(
    nqptp.service
    shairport-sync.service
    librespot.service
    bt-agent.service
    jasper-mux.service
    bluealsa-aplay.service
)

# Park-list units the ladder deliberately does NOT start directly, each paired
# with the reconciler that re-arms it (`unit=owner`). Every owner must also be
# in JASPER_CORE_GRAPH_RESTORE_RECONCILERS below, which the ladder kicks. Each
# is gated on state only its owner holds, so a blind start would either
# condition-fail or overrule a park the owner decided.
#
# jasper-outputd.service is absent because the ladder restarts it itself, as a
# core-graph step ahead of these clients.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_RECONCILER_OWNED_UNITS=(
    # ConditionPathExists=!/var/lib/jasper/voice-input-absent and the unit's
    # enable state are both written by jasper-aec-reconcile, which restarts
    # voice on every mic-bearing branch once it sees the unit inactive.
    jasper-voice.service=jasper-aec-reconcile.service
    # ConditionPathExists=/run/jasper-aec-reconcile/aec-bridge-ready — a marker
    # only that reconciler publishes or revokes.
    jasper-aec-bridge.service=jasper-aec-reconcile.service
    # Armed only for an ACTIVE leader, and only after the crossover statefile
    # is re-seeded with the re-proven driver-domain graph: a cold start off a
    # stale statefile is the full-range-to-a-tweeter hazard the unit's
    # ExecStartPre guard is documented NOT to convert.
    jasper-camilla-crossover.service=jasper-grouping-reconcile.service
    # Which of the two runs is the bond ROLE — leader both, follower client
    # only, solo neither — so a start would put a snapclient on an unbonded box.
    jasper-snapclient.service=jasper-grouping-reconcile.service
    jasper-snapserver.service=jasper-grouping-reconcile.service
)

# The reconcilers the ladder kicks so the owned units above come back.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_RESTORE_RECONCILERS=(
    jasper-aec-reconcile.service
    jasper-grouping-reconcile.service
)
