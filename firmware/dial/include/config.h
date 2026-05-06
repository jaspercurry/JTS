#pragma once

// Pin definitions for the CrowPanel 1.28" HMI Rotary Display.
// Source: https://github.com/Elecrow-RD/CrowPanel-1.28inch-HMI-ESP32-Rotary-Display-240-240-IPS-Round-Touch-Knob-Screen
//   /readme.md "Pin definition"
#define ENCODER_A_PIN  45
#define ENCODER_B_PIN  42
#define SWITCH_PIN     41

// Onboard WS2812B chain (5 LEDs in series). LED 0 is the status
// indicator in this firmware; the others are used by phase 5's
// listening-ring UI.
#define RGB_LED_PIN    48
#define NUM_LEDS       5

// jasper-control is reachable at jasper.local:8780 over HTTP. mDNS
// resolves the hostname; if your network blocks mDNS, override here.
// Match JASPER_CONTROL_PORT in /etc/jasper/jasper.env on the Pi.
#define JASPER_HOST    "jasper.local"
#define JASPER_PORT    8780

// UDP log target — see jasper-control's dial-log listener. Diagnostic
// output from the dial is fire-and-forget UDP to this port so we can
// debug without keeping the dial USB-tethered to the Pi.
#define JASPER_LOG_PORT 5514

// Volume per detent. Two detents per dB step is the agreed default;
// retune in firmware if it feels wrong (no Pi-side change needed).
#define VOLUME_STEP_DB 2.0f

// Press duration threshold separating short-press (play/pause) from
// long-press (hold-to-talk in phase 3). 500 ms feels natural —
// shorter than a typical human hold but longer than a quick click.
#define LONG_PRESS_MS 500

// Quadrature transitions per detent. 4 is standard for mechanical
// encoders. The CrowPanel HMI factory firmware uses a different
// debounce strategy ("50 pulse counts for direction") — if rotation
// feels too fast / too slow on real hardware, this is the dial.
#define ENCODER_PULSES_PER_DETENT 4

// LED brightness (0-255). The 5×WS2812 ring is bright enough that
// 20 is plenty for an indicator. Phase 5 will dim/brighten dynamically.
#define LED_BRIGHTNESS 20

// mDNS hostname the dial registers as. Mostly cosmetic — the Pi
// pings it after onboarding to confirm WiFi success.
#define MDNS_HOSTNAME  "jasper-dial"

// Used by Improv WiFi to identify the device in provisioning UIs.
#define DEVICE_NAME    "Jasper Dial"
#define DEVICE_MFG     "JTS"
