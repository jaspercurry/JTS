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

// --- Display (GC9A01 driver, 240x240 round IPS) ---
// Pins from the CrowPanel HMI factory firmware source. SPI bus is
// dedicated to the display (no other SPI peripherals).
#define TFT_SCLK 10
#define TFT_MOSI 11
#define TFT_DC    3
#define TFT_CS    9
#define TFT_RST  14
#define TFT_BACKLIGHT 46

// --- Touch (CST816D capacitive controller) ---
#define TP_I2C_SDA 6
#define TP_I2C_SCL 7
#define TP_RST    13
#define TP_INT     5

// --- Default I2C bus (factory uses for the onboard SSD1306 OLED) ---
// We don't drive the OLED, but Wire.begin() on these pins matches
// factory boot order so any board-level magic that depends on the
// bus being live still works.
#define I2C_SDA   38
#define I2C_SCL   39

// --- Misc onboard ---
// Power LED (red glow under the bezel) — ACTIVE LOW. Factory firmware
// drives it LOW to enable. Don't set HIGH.
#define POWER_LED_PIN 40

// CrowPanel HMI v1 power-enable pins. The panel's backlight + display
// power supply is gated by GPIO1 and GPIO2 — without driving these
// HIGH the screen has no power and stays dark, regardless of SPI /
// LVGL state. Source: factory firmware setup() in
//   factory_soucecode/RotaryScreen_1_28/RotaryScreen_1_28.ino
#define PANEL_POWER_PIN_1 1
#define PANEL_POWER_PIN_2 2

// Backlight PWM channel/freq/resolution.
#define BACKLIGHT_PWM_CHANNEL    0
#define BACKLIGHT_PWM_FREQ_HZ 5000
#define BACKLIGHT_PWM_RES_BITS   8

// LVGL framebuffer: full 240×240 in PSRAM, matching factory firmware.
// pushImageDMA (not pushPixels) handles PSRAM-backed buffers
// correctly — the GDMA controller's PSRAM-cache path used by
// pushImageDMA differs from the direct pushPixels DMA path. 240*240*2
// = 115 KB per buffer, double-buffered = 230 KB in PSRAM (we have
// 8 MB).
#define LVGL_BUF_LINES 240

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
