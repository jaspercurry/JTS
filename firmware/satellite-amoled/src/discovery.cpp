#include "discovery.h"

#include <ESPmDNS.h>
#include <WiFi.h>

#include "config.h"

ControlEndpoint discoverControlEndpoint() {
    ControlEndpoint ep;
    ep.port = JASPER_PORT;
    ep.fromMdns = false;

    // Prefer mDNS-SD: ask whoever advertises `_jasper-control._tcp`
    // who they are. The Pi's avahi service file (deploy/avahi/
    // jasper-control.service) makes us discoverable regardless of
    // hostname, so swapping Pi hardware doesn't require re-flashing.
    int n = MDNS.queryService("jasper-control", "tcp");
    if (n > 0) {
        IPAddress mip = MDNS.address(0);
        if ((uint32_t)mip != 0) {
            ep.ip = mip;
            ep.hostOrIp = mip.toString();   // IP is fine for HTTPClient
            ep.port = MDNS.port(0);
            if (ep.port == 0) ep.port = JASPER_PORT;  // defensive
            ep.fromMdns = true;
            return ep;
        }
    }

    // Fallback: compile-time JASPER_HOST. Resolve via mDNS hostname
    // first (works for *.local), then standard DNS.
    IPAddress fallback_ip;
    if (MDNS.queryHost(JASPER_HOST, fallback_ip) ||
        WiFi.hostByName(JASPER_HOST, fallback_ip)) {
        ep.ip = fallback_ip;
        ep.hostOrIp = JASPER_HOST;
    } else {
        // Last resort: use the hostname literally. HTTPClient may
        // succeed if the OS-level resolver later catches up.
        ep.hostOrIp = JASPER_HOST;
    }
    return ep;
}
