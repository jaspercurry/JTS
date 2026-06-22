/*
 * SPDX-FileCopyrightText: 2026 Jasper Curry
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdint.h>

// Connection-state model for the satellite. Mirrors firmware/dial/'s
// six-state convention (see docs/satellites.md "Status / UI conventions")
// so satellites give the user the same visual vocabulary regardless of
// which device they're looking at.
//
//   BOOT         power on, before anything has run
//   PROVISION    no WiFi creds in NVS — awaiting Improv push
//   CONNECTING   joining WiFi with stored creds
//   ONLINE       WiFi up + jasper-control endpoint resolved
//   HTTP_ERROR   WiFi up but a recent jasper-control POST failed
//   OFFLINE      WiFi dropped after a successful connect; reconnecting
//
// Underlying type is fixed so a sentinel like 0xFF (a "never drawn yet"
// marker) can coexist without colliding with a real value.
enum class Status : uint8_t {
    BOOT       = 0,
    PROVISION  = 1,
    CONNECTING = 2,
    ONLINE     = 3,
    HTTP_ERROR = 4,
    OFFLINE    = 5,
};
