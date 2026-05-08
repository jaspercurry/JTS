#pragma once

// Pin definitions and constants for the Waveshare ESP32-S3-Touch-AMOLED-1.8.
//
// **Pin map is intentionally minimal at milestone 1.** The vendor's
// reference code (Waveshare wiki + waveshareteam/Waveshare-ESP32-components
// on GitHub) defines the actual GPIO assignments for the ES8311 codec,
// SH8601 display, FT3168 touch, TCA9554 I/O expander, AXP2101 PMIC,
// and QMI8658 IMU. We'll lift exact pin numbers from there as each
// peripheral comes online — committing them now would be guessing.
//
// Sources to pull from when we add each subsystem:
//   - https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8
//   - https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8/Development-Environment-Setup-Arduino
//   - https://github.com/waveshareteam/Waveshare-ESP32-components
//   - https://github.com/Maucke/esp_lcd_sh8601 (third-party SH8601 driver)

// --- Identity ---

// mDNS hostname this satellite registers as. Mirrors firmware/dial/
// pattern (`jasper-dial`). Discovery happens server-side via
// _jasper-control._tcp; the satellite's own hostname is mostly cosmetic.
#define MDNS_HOSTNAME  "jasper-satellite-amoled"

// Used by Improv WiFi to identify the device in provisioning UIs
// (jasper-dial-onboard / improv-wifi.com). Wired up at Phase 1.
#define DEVICE_NAME    "Jasper AMOLED Satellite"
#define DEVICE_MFG     "JTS"

// --- jasper-control endpoint (compile-time fallback) ---
//
// Phase 1 switches the satellite to mDNS-SD discovery of
// _jasper-control._tcp (matches firmware/dial/src/discovery.cpp).
// Until then, hardcoded fallback for any HTTP we add. Match
// JASPER_CONTROL_PORT in /etc/jasper/jasper.env on the Pi.
#define JASPER_HOST    "jts.local"
#define JASPER_PORT    8780

// UDP log target — see jasper-control's dial-log listener at port 5514.
// Reused by satellites for fire-and-forget diagnostics. Wired up at
// Phase 1.
#define JASPER_LOG_PORT 5514

// --- Pin map ---
//
// Cross-verified between two independent sources for this exact board:
//   - vthinkxie/claude-desktop-buddy-esp32 board header
//     (src/boards/board_waveshare_esp32s3_touch_amoled_1_8.h)
//   - HA community ESPHome YAML (argsnd, Dec 2025)
//   - https://community.home-assistant.io/t/esp32-s3-1-8inch-amoled-touch/956270

// I²C bus — shared between TCA9554 (expander), AXP2101 (PMIC),
// FT3168 (touch), ES8311 (codec), QMI8658 (IMU), PCF85063 (RTC).
// Single bus with multiple addresses.
#define PIN_I2C_SDA          15
#define PIN_I2C_SCL          14
#define I2C_FREQ_HZ          200000

// Known I²C addresses on this board (see I²C scan output for verification):
//   0x18  ES8311 audio codec
//   0x20  TCA9554 GPIO expander  (sometimes 0x21 depending on strap pins)
//   0x34  AXP2101 PMIC
//   0x38  FT3168 capacitive touch
//   0x51  PCF85063 RTC
//   0x6A  QMI8658 IMU             (or 0x6B)
#define ES8311_I2C_ADDR      0x18

// I²S audio — ESP32 is master (generates MCLK/BCLK/LRCK), ES8311 is slave.
// Mic ADC output goes to ESP DI (RX); speaker DAC input comes from ESP DO (TX).
#define PIN_I2S_MCLK         16
#define PIN_I2S_BCLK         9
#define PIN_I2S_LRCK         45      // aka WS
#define PIN_I2S_DIN          10      // ESP RX from ES8311 ADC
#define PIN_I2S_DOUT         8       // ESP TX to ES8311 DAC (speaker)

// Power amplifier enable for the onboard speaker. Active high.
// Not used in Phase 0 (mic-only); leave high-impedance / off.
#define PIN_PA_CTRL          46

// User input — BOOT button. Active-low. Useful for pause/resume of
// streaming, "press to record", etc. without needing touch yet.
#define PIN_BTN_BOOT         0

// --- Display + touch (TBD — fill in at Phase 1/4) ---
//
// SH8601 AMOLED on QSPI: SCLK=GPIO11, D0..D3=GPIO4..7, CS=GPIO12.
// Reset is routed through TCA9554 P0 (not a direct GPIO).
// FT3168 touch: I²C addr 0x38 on the shared bus, INT=GPIO21,
// reset via TCA9554 P1.
// Filling in the #defines once we get to that phase.
