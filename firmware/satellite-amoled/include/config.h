/*
 * SPDX-FileCopyrightText: 2026 Jasper Curry
 *
 * SPDX-License-Identifier: Apache-2.0
 */

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

// --- AMOLED display: SH8601 over QSPI ---
//
// 368×448 SH8601 controller on a 4-bit Quad-SPI bus. Distinct from
// standard 4-line SPI: command + 24-bit address are clocked on a
// single line (D0), then payload uses all four data lines (D0..D3).
// Arduino_GFX's `Arduino_ESP32QSPI` databus implements the framing.
//
// Reset is NOT on a direct GPIO — it's behind the TCA9554 expander
// (see below). Pass GFX_NOT_DEFINED to Arduino_SH8601's reset arg
// and toggle it externally before calling gfx->begin().
#define PIN_LCD_QSPI_CS    12
#define PIN_LCD_QSPI_SCLK  11
#define PIN_LCD_QSPI_D0     4
#define PIN_LCD_QSPI_D1     5
#define PIN_LCD_QSPI_D2     6
#define PIN_LCD_QSPI_D3     7

// Native panel dimensions, before LCD_ROTATION is applied. Use
// s_gfx->width() / height() in render code if you need the
// post-rotation values.
#define LCD_PANEL_WIDTH   368
#define LCD_PANEL_HEIGHT  448

// Output rotation. **Stuck at 0 or 2 (= 180°) until we add software
// rotation support.** Per Arduino_GFX's Arduino_SH8601.cpp ("SH8601
// does not support rotation"), the chip's MADCTL exposes only X/Y
// axis flips — there's no MV (row/column exchange) bit, so the
// hardware can't do 90°/270°. Setting LCD_ROTATION=1 or 3 makes the
// library report a swapped 448×368 logical canvas while the chip's
// column window is still 368, and writes spill over → garbage VRAM.
//
//   0 = native (USB-C ends up on the LEFT side of the screen content)
//   2 = 180° flip (USB-C on the RIGHT side)
//
// Software rotation (render to a PSRAM framebuffer, transpose on
// push) is a Phase 1.2.x follow-up if a portrait hold matters.
#define LCD_ROTATION  0

// --- TCA9554 GPIO expander ---
//
// Three peripherals hide behind this expander on the I²C bus
// (0x20 by default; A0=A1=A2=0). Holding any of these LOW keeps
// the corresponding peripheral in reset / unpowered.
#define TCA9554_I2C_ADDR        0x20
#define TCA9554_EXIO_LCD_RESET     0   // P0 → SH8601 RESN (active low)
#define TCA9554_EXIO_TP_RESET      1   // P1 → FT3168 RST  (active low)
#define TCA9554_EXIO_DSI_PWR_EN    2   // P2 → display power rail enable

// --- Capacitive touch (Phase 1.4) ---
//
// FT3168 on the shared I²C bus at 0x38. INT line is a direct GPIO;
// reset is via TCA9554 P1.
#define PIN_TP_INT             21
#define FT3168_I2C_ADDR      0x38
