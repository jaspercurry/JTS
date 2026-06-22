/*
 * SPDX-FileCopyrightText: 2026 Jasper Curry
 *
 * SPDX-License-Identifier: Apache-2.0
 */

// mDNS service discovery for jasper-control. Replaces the
// hardcoded JASPER_HOST/JASPER_PORT pair so the dial finds whichever
// Pi answers `_jasper-control._tcp` rather than depending on a
// specific hostname. Pi side: deploy/avahi/jasper-control.service
// (installed by deploy/install.sh).
//
// Falls back to compile-time JASPER_HOST/PORT when no service is
// advertised — keeps the dial working on networks where avahi isn't
// running, or against a Pi whose install.sh predates the avahi file.
#pragma once

#include <Arduino.h>
#include <IPAddress.h>

struct ControlEndpoint {
    String   hostOrIp;   // string suitable for HTTPClient URL
    IPAddress ip;        // for UDP log datagrams (zero on resolution fail)
    uint16_t port;       // 8780 in practice
    bool     fromMdns;   // true: discovered via mDNS-SD; false: fallback
};

// Resolve where jasper-control lives. Tries `_jasper-control._tcp`
// service browse first; falls back to `MDNS.queryHost(JASPER_HOST)`
// + DNS. Cheap to call — implicit ~3 s timeout in the underlying
// MDNS API. Caller should cache the result for the WiFi session
// and re-call on disconnect/reconnect.
ControlEndpoint discoverControlEndpoint();
