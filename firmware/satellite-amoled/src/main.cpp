// Jasper AMOLED Satellite — Phase 1.2 firmware.
//
// Phase 0 (mic capture) + Phase 1.1 (WiFi + Improv) + on-screen status
// indicator. Audio path is unchanged from v0.1.0 / v0.2.0 — same
// register sequence, same I²S config. WiFi/Improv path unchanged from
// v0.2.0. New in this build:
//
//   - Toolchain bumped to Arduino-ESP32 v3.x via pioarduino. The legacy
//     <driver/i2s.h> we use here is preserved as a deprecated
//     compatibility shim and continues to compile + run unchanged.
//   - SH8601 AMOLED comes up early in setup() (right after I²C, before
//     the potentially-15-second WiFi join) and renders a colored circle
//     + label reflecting the Status enum — see include/status.h and
//     src/display.cpp. Redraws only on state transitions.
//
// Phase 1.1 features carried forward (unchanged):
//   - WiFi connects from creds in NVS at boot (15 s timeout); if no
//     creds, sits in PROVISION and accepts Improv pushes over USB-CDC.
//   - mDNS-SD discovery resolves jasper-control on the LAN.
//   - dlog() emits diagnostic lines over USB-CDC + UDP :5514.
//
// Improv coexistence with the binary PCM stream is intentional. The
// host's Improv parser scans for the `IMPROV\\x01` magic prefix in the
// incoming byte stream — PCM bytes that don't match the prefix are
// ignored. Improv onboarding (rare) does cause a tiny audible click
// in any concurrent capture; not a problem in practice since onboarding
// happens once per device.
//
// Hardware path is unchanged from Phase 0 (ESP32 = I²S master, codec
// is slave). Audio init / capture comments live with their respective
// functions below.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ImprovWiFiLibrary.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <esp_chip_info.h>
#include <esp_heap_caps.h>
#include <stdarg.h>

#include "config.h"
#include "discovery.h"
#include "display.h"
#include "status.h"

static const i2s_port_t I2S_PORT       = I2S_NUM_0;
static constexpr int     SAMPLE_RATE_HZ = 16000;
static constexpr int     MCLK_HZ        = SAMPLE_RATE_HZ * 256;  // = 4.096 MHz

// Connection-state model — six values defined in include/status.h so
// display.cpp can render them. Default to BOOT so the first loop()
// pass either redraws (if displayInit succeeded mid-setup) or no-ops.
static volatile Status g_status = Status::BOOT;

static Preferences     g_prefs;
static ImprovWiFi      g_improv(&Serial);
static WiFiUDP         g_logUdp;
static IPAddress       g_logTarget;     // (0,0,0,0) = not yet resolved
static ControlEndpoint g_control;        // resolved after every WiFi-up

// Update g_status AND redraw the panel synchronously. Use this instead
// of `g_status = X` everywhere so transitions show up immediately —
// the loop's dedupe-redraw is fine when control reaches it, but the
// loop is blocked inside `i2s_read(..., portMAX_DELAY)` between
// frames AND inside `g_improv.handleSerial()` for the duration of an
// Improv-driven WiFi join (~2–5 s while we resolve mDNS, write NVS,
// etc., before the callback returns). A status flip from inside
// `onImprovConnected` would otherwise stay invisible until the next
// I²S frame *and* the loop body actually reaches the dedupe block.
// Drawing inline removes that dependency on loop scheduling. All
// callers run on the Arduino loop task — no concurrency on the
// SH8601 QSPI bus.
static void setStatus(Status s) {
    if (g_status == s) return;
    g_status = s;
    displayShowStatus(s);
}

// --- WiFi / Improv / discovery helpers (mirrored from firmware/dial/) ---

// Fire-and-forget log line. Always prints to USB-CDC; if WiFi is up
// and we've resolved the Pi's IP, also sends one UDP datagram per
// call to jasper-control's :5514 listener. UDP loss is acceptable —
// these are diagnostic lines, not protocol.
static void dlog(const char *fmt, ...) {
    char buf[256];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    if (n < 0) return;
    if (n >= (int)sizeof(buf)) n = sizeof(buf) - 1;
    Serial.write((const uint8_t *)buf, n);
    if (n > 0 && buf[n - 1] != '\n') Serial.write('\n');

    if (WiFi.status() == WL_CONNECTED && (uint32_t)g_logTarget != 0) {
        if (g_logUdp.beginPacket(g_logTarget, JASPER_LOG_PORT)) {
            g_logUdp.write((const uint8_t *)buf, n);
            g_logUdp.endPacket();
        }
    }
}

// Resolve where jasper-control lives on the LAN. Cached into g_control
// + g_logTarget so the per-loop dlog() doesn't re-discover. Re-call on
// every WiFi-up.
static void resolveControlEndpoint() {
    g_control = discoverControlEndpoint();
    g_logTarget = g_control.ip;
    Serial.printf(
        "[discovery] jasper-control %s at %s:%u\n",
        g_control.fromMdns ? "via mDNS-SD" : "via fallback hostname",
        g_control.hostOrIp.c_str(), (unsigned)g_control.port
    );
    if ((uint32_t)g_logTarget == 0) {
        Serial.println(
            "[discovery] no IP resolved — UDP logs disabled until reconnect"
        );
    }
}

// Try to join WiFi using credentials stored in NVS. Blocks up to 15 s.
// Returns true on success. Caller drives g_status before/after.
static bool tryConnectStored() {
    String ssid = g_prefs.getString("ssid", "");
    String pass = g_prefs.getString("pass", "");
    if (ssid.length() == 0) return false;

    g_status = Status::CONNECTING;
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(MDNS_HOSTNAME);
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
        delay(100);
    }
    return WiFi.status() == WL_CONNECTED;
}

// Improv callback. The library invokes this AFTER it has independently
// verified that the credentials work (it called improvConnect below
// and got WL_CONNECTED). Persist creds, set up mDNS, mark online.
static void onImprovConnected(const char *ssid, const char *password) {
    g_prefs.putString("ssid", ssid);
    g_prefs.putString("pass", password);
    MDNS.begin(MDNS_HOSTNAME);
    resolveControlEndpoint();
    setStatus(Status::ONLINE);
    dlog("[improv] connected, ip=%s, hostname=%s.local",
         WiFi.localIP().toString().c_str(), MDNS_HOSTNAME);
}

// Improv "custom connect" callback. Called by the library when the
// host pushes credentials; we drive the WiFi attempt and report
// success/failure back. 30 s timeout because the first DHCP after a
// fresh router can be slow.
static bool improvConnect(const char *ssid, const char *password) {
    Serial.printf("[improv] connect attempt: ssid=%s, pass=<%d chars>\n",
                  ssid, (int)strlen(password));
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(MDNS_HOSTNAME);
    WiFi.begin(ssid, password);
    unsigned long t0 = millis();
    wl_status_t prev = (wl_status_t)255;
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
        wl_status_t s = WiFi.status();
        if (s != prev) {
            Serial.printf("[improv] wifi status: %d (elapsed=%lums)\n",
                          (int)s, millis() - t0);
            prev = s;
        }
        delay(100);
    }
    bool ok = (WiFi.status() == WL_CONNECTED);
    Serial.printf("[improv] connect %s after %lums (final status=%d)\n",
                  ok ? "OK" : "FAIL", millis() - t0, (int)WiFi.status());
    return ok;
}

// --- Heap reporting ---

// One-line summary of the heap state in a single capability domain
// (internal SRAM or PSRAM). `caps` is a MALLOC_CAP_* bitmask.
static void print_heap(const char *label, uint32_t caps) {
    size_t total = heap_caps_get_total_size(caps);
    size_t freeb = heap_caps_get_free_size(caps);
    size_t largest = heap_caps_get_largest_free_block(caps);
    Serial.printf("[boot] %s: total=%u free=%u largest=%u\n",
                  label, (unsigned)total, (unsigned)freeb, (unsigned)largest);
}

// Pretty-print known chip names alongside the raw I²C address.
// Returns nullptr for unrecognized addresses so the scanner can label
// them as "(unknown)" rather than guessing wrong.
static const char *i2c_addr_label(uint8_t addr) {
    switch (addr) {
        case 0x18: return "ES8311 codec";
        case 0x20: return "TCA9554 expander (A0=A1=A2=0)";
        case 0x21: return "TCA9554 expander (A0=1)";
        case 0x22: return "TCA9554 expander (A1=1)";
        case 0x23: return "TCA9554 expander (A0=A1=1)";
        case 0x34: return "AXP2101 PMIC";
        case 0x38: return "FT3168 touch";
        case 0x51: return "PCF85063 RTC";
        case 0x6A: return "QMI8658 IMU";
        case 0x6B: return "QMI8658 IMU (alt)";
        default:   return nullptr;
    }
}

// Single-byte register write to the ES8311 over the shared I²C bus.
// Returns true on ACK, false on NACK / bus error.
static bool es8311_write(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(ES8311_I2C_ADDR);
    Wire.write(reg);
    Wire.write(val);
    return Wire.endTransmission() == 0;
}

// Single-byte register read. Returns 0xFF on bus error (also a valid
// register value for some regs — check transmission status separately
// if the disambiguation matters).
static uint8_t es8311_read(uint8_t reg) {
    Wire.beginTransmission(ES8311_I2C_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFF;  // repeated start
    if (Wire.requestFrom((int)ES8311_I2C_ADDR, 1) != 1) return 0xFF;
    return Wire.read();
}

// Dump every register the ES8311 cares about for cold-boot ADC capture
// debugging. Call this AFTER init to verify writes stuck.
static void es8311_dump_regs() {
    static const uint8_t regs[] = {
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
        0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
        0x18, 0x19, 0x1A, 0x1B, 0x1C,
        0x44,
        0xFD, 0xFE, 0xFF,  // chip ID + version (read-only)
    };
    Serial.println("[es8311] register dump (after init):");
    for (size_t i = 0; i < sizeof(regs); i++) {
        uint8_t v = es8311_read(regs[i]);
        Serial.printf("[es8311]   0x%02X = 0x%02X\n", regs[i], v);
    }
}

// Bring up the I²S peripheral as MASTER (ESP32 generates BCLK + LRCK
// + MCLK). RX-only — we don't drive the speaker DAC in Phase 0.
// Uses the legacy `driver/i2s.h` API per the comment at the top of
// this file (PSRAM/GDMA gotcha with the new i2s_std API).
//
// **Stereo mode is intentional even though the codec is mono.** When
// configured with I2S_CHANNEL_FMT_ONLY_LEFT, the legacy driver's BCLK
// timing math doesn't produce an integer number of BCLK ticks per
// half-LRCK frame for our 4.096 MHz MCLK / 16 kHz LRCK / 16-bit
// configuration — the result is samples that are bit-quantized-sounding
// even though intelligible, because ESP32 samples on misaligned BCLK
// edges. Espressif's official i2s_es8311 example uses STEREO + discard
// the unused channel in software for exactly this reason. See:
//   github.com/espressif/esp-idf/blob/master/examples/peripherals/i2s/
//     i2s_codec/i2s_es8311/
//   github.com/espressif/esp-idf/issues/10630
static bool i2s_rx_init() {
    i2s_config_t cfg = {};
    cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
    cfg.sample_rate          = SAMPLE_RATE_HZ;
    cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
    // RIGHT_LEFT = standard stereo. ES8311 puts its mono ADC sample
    // in the left slot when WS=0; the right slot is either zero or a
    // duplicate of left (depends on REG14 settings). Either way the
    // host code below keeps the LEFT channel and discards the right.
    cfg.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count        = 4;
    cfg.dma_buf_len          = 256;     // 256 frames × 4 bufs ≈ 64 ms latency
    cfg.use_apll             = false;   // PLL_AUDIO unnecessary at 16 kHz
    cfg.fixed_mclk           = MCLK_HZ;
    cfg.mclk_multiple        = I2S_MCLK_MULTIPLE_256;

    if (i2s_driver_install(I2S_PORT, &cfg, 0, nullptr) != ESP_OK) return false;

    i2s_pin_config_t pins = {};
    pins.mck_io_num   = PIN_I2S_MCLK;
    pins.bck_io_num   = PIN_I2S_BCLK;
    pins.ws_io_num    = PIN_I2S_LRCK;
    pins.data_out_num = I2S_PIN_NO_CHANGE;  // RX only
    pins.data_in_num  = PIN_I2S_DIN;
    return i2s_set_pin(I2S_PORT, &pins) == ESP_OK;
}

// Minimum-viable ES8311 init for 16 kHz mono ADC capture, ESP32 as
// I²S master driving MCLK at 4.096 MHz.
//
// Translated from espressif/esp-adf release/v2.x
// components/audio_hal/driver/es8311/es8311.c — specifically:
//   - es8311_codec_init()           : the boilerplate boot sequence
//   - es8311_config_sample(16k)     : applies coeff_div row for 4.096/16k
//   - es8311_set_bits_per_sample(16): sets REG09/REG0A to 16-bit
//   - es8311_mic_select(false)      : analog mic (REG14)
//   - es8311_set_mic_gain(...)      : ADC PGA in dB-stepping enum
//
// Coefficient row used (from coeff_div[]):
//   {4096000, 16000, pre_div=1, pre_multi=1, adc_div=1, dac_div=1,
//    fs_mode=0, lrck_h=0, lrck_l=0xff, bclk_div=4, adc_osr=0x10,
//    dac_osr=0x20}
static bool es8311_init_for_capture() {
    bool ok = true;

    // Twice — Espressif's source explicitly does this for I²C noise
    // immunity during cold init.
    ok &= es8311_write(0x44, 0x08);
    ok &= es8311_write(0x44, 0x08);

    // CLK_MANAGER REG01. Diagnostic: trying 0xBF (derive MCLK from
    // SCLK/BCLK) instead of 0x30 (use external MCLK pin). If the
    // board's MCLK wire from ESP32 GPIO16 to ES8311 isn't present
    // (or has a routing issue), the codec needs SCLK-derived mode
    // or its ADC clock won't run. ESPHome's reference init uses
    // 0xBF unconditionally, which is robust to either wiring.
    ok &= es8311_write(0x01, 0xBF);

    // Coefficient writes for 4.096 MHz / 16 kHz.
    //
    // **Critical: pre_multi must be 3 (8× multiplier) when MCLK is
    // SCLK-derived** (REG01=0xBF). Espressif's es8311_config_sample()
    // overrides datmp to 3 in that mode so DIG_MCLK = BCLK × 8 still
    // equals the expected 4.096 MHz internally. Without this, the
    // ADC samples at 1/8 the expected rate (~2 kHz instead of 16 kHz)
    // and outputs each sample repeated ~8× into the I²S frame —
    // exactly the "bitcrushed / pixel-y / sample-held" sound the
    // earlier captures had despite levels and bit-depth looking
    // healthy on stats.
    //
    // Bit layout: REG02 = ((pre_div - 1) << 5) | (datmp << 3) | low3
    //   pre_div = 1 → 0x00 in bits 7:5
    //   datmp   = 3 → 0x18 in bits 4:3
    //   → REG02 = 0x18
    ok &= es8311_write(0x02, 0x18);
    ok &= es8311_write(0x05, 0x00);  // (adc_div-1)<<4 | (dac_div-1)<<0
    ok &= es8311_write(0x03, 0x10);  // fs_mode=0<<6 | adc_osr=0x10
    ok &= es8311_write(0x04, 0x20);  // dac_osr=0x20
    ok &= es8311_write(0x07, 0x00);  // lrck_h=0
    ok &= es8311_write(0x08, 0xFF);  // lrck_l=0xff
    ok &= es8311_write(0x06, 0x03);  // bclk_div=4 → register=4-1=3 (since <19)

    // System power-up. Order matters per Espressif's source.
    ok &= es8311_write(0x0B, 0x00);
    ok &= es8311_write(0x0C, 0x00);
    ok &= es8311_write(0x10, 0x1F);  // power up system
    ok &= es8311_write(0x11, 0x7F);  // ADC + reference power on

    // SDP (serial data port) — 16-bit on both DAC and ADC paths.
    // (We only care about ADC/REG0A for capture, but Espressif's
    // helper writes both; harmless.)
    ok &= es8311_write(0x09, 0x0C);  // SDPIN  REG09 = 16-bit
    ok &= es8311_write(0x0A, 0x0C);  // SDPOUT REG0A = 16-bit

    // Analog PGA gain (REG16). Empirically calibrated:
    //   0x24 → peaks ~-20 dBFS on speech (Espressif's conservative default)
    //   0x37 → peaks  -1 dBFS on speech, 0 dBFS (clipping) on music
    //   0x3F → mutes (probably a control bit, not gain)
    // Each register step ≈ 0.95 dB of gain. 0x32 keeps peaks ~-5 dBFS
    // on music (no clipping) while keeping speech well above noise
    // floor at typical room distance.
    ok &= es8311_write(0x16, 0x32);

    // Digital ADC volume (REG17). 0xBF ≈ 0 dB digital gain. Leave at
    // a near-unity default; we'll tune the analog stage first.
    ok &= es8311_write(0x17, 0xBF);

    // --- es8311_start(ES_MODULE_ADC) ---
    //
    // The boilerplate above gets the chip clocked and the data path
    // configured, but the **analog signal chain is still off** until
    // these writes — without them the I²S RX produces a constant bias
    // value (we observed ~`-7` repeating). Translated from
    // espressif/esp-adf es8311.c es8311_start() with mode=ES_MODULE_ADC.
    ok &= es8311_write(0x0E, 0x02);  // enable analog PGA + ADC modulator
    ok &= es8311_write(0x12, 0x00);  // DAC enable (harmless for ADC-only)
    ok &= es8311_write(0x13, 0x00);  // **clear ADC↔DAC loopback / mute**
                                     // — chip default is 0x10 (bit 4 set)
                                     // which on this part appears to
                                     // gate the ADC input. ESPHome's
                                     // init explicitly writes 0 here.
                                     // Without this, samples freeze at
                                     // ADC noise floor (-7 LSB).
    ok &= es8311_write(0x14, 0x1A);  // analog mic select + full PGA path
    ok &= es8311_write(0x0D, 0x01);  // power up analog circuitry
    ok &= es8311_write(0x15, 0x40);  // ADC ramp rate / dmic sense
    ok &= es8311_write(0x1B, 0x0A);  // ADC HPF stage 1
    ok &= es8311_write(0x1C, 0x6A);  // ADC HPF stage 2 + DC offset cancel

    // Final on. Espressif's code writes 0x80 to REG00 last to bring
    // the chip out of reset with all the above settings latched.
    ok &= es8311_write(0x00, 0x80);

    return ok;
}

// Walk the I²C bus once and report which 7-bit addresses ACK an
// empty start+address+stop transaction. Standard probe pattern;
// won't disturb chips that are already configured.
//
// Reserved address ranges skipped per the I²C spec:
//   0x00–0x07  reserved (general call, CBUS, etc.)
//   0x78–0x7F  reserved (10-bit addressing, future)
static void scan_i2c_bus() {
    int found = 0;
    Serial.println("[i2c] scanning bus...");
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        Wire.beginTransmission(addr);
        uint8_t err = Wire.endTransmission();
        if (err == 0) {
            const char *label = i2c_addr_label(addr);
            Serial.printf("[i2c]   0x%02X  %s\n",
                          addr, label ? label : "(unknown)");
            found++;
        }
        // err==2 (NACK on address) is the "nothing here" case — silent.
        // err==4 (other error) might signal a stuck bus; report it.
        else if (err != 2) {
            Serial.printf("[i2c]   0x%02X  bus error %u\n",
                          addr, err);
        }
    }
    Serial.printf("[i2c] scan done — %d device(s) found.\n", found);
}

void setup() {
    Serial.begin(115200);
    // Give USB-CDC ~1 s to enumerate so the host sees our boot prints.
    // Without this delay, the early lines race the ttyACM0 setup on
    // the Pi and the user just sees memory ticks with no banner.
    delay(1000);

    Serial.println();
    Serial.println("[boot] jasper-satellite-amoled firmware v"
                   JASPER_SATELLITE_AMOLED_FIRMWARE_VERSION);

    esp_chip_info_t info = {};
    esp_chip_info(&info);
    Serial.printf("[boot] chip: ESP32-S3 rev %d, %d cores, %u MHz\n",
                  info.revision, info.cores,
                  (unsigned)(getCpuFrequencyMhz()));
    Serial.printf("[boot] flash: %u MB\n",
                  (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)));

    print_heap("PSRAM", MALLOC_CAP_SPIRAM);
    print_heap("SRAM",  MALLOC_CAP_INTERNAL);

    // I²C bus + display come up FIRST so the user sees a status
    // indicator within ~100 ms of power-on, instead of staring at a
    // black panel through a potentially-15-second WiFi join. The full
    // I²C device scan stays after WiFi has had a chance to come up so
    // the scan output dlogs over UDP if we connected.
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setClock(I2C_FREQ_HZ);
    Serial.printf("[boot] I²C up: SDA=%d SCL=%d @ %u Hz\n",
                  PIN_I2C_SDA, PIN_I2C_SCL, (unsigned)I2C_FREQ_HZ);
    if (displayInit()) {
        displayShowStatus(Status::BOOT);
    }

    // --- WiFi + Improv ---
    //
    // Configure Improv first so handleSerial() in loop() can respond to
    // a host-driven onboarding even if the WiFi join below fails. Then
    // try stored creds. If the device has been provisioned before, we
    // arrive at ONLINE; if not, we sit in PROVISION until the host
    // pushes credentials over Improv.
    g_prefs.begin("jasper", false);
    Serial.println("[boot] prefs (NVS) opened");

    g_improv.setDeviceInfo(
        ImprovTypes::ChipFamily::CF_ESP32_S3,
        DEVICE_NAME,
        JASPER_SATELLITE_AMOLED_FIRMWARE_VERSION,
        DEVICE_MFG
    );
    g_improv.onImprovConnected(onImprovConnected);
    g_improv.setCustomConnectWiFi(improvConnect);
    Serial.println("[boot] improv configured");

    if (tryConnectStored()) {
        setStatus(Status::ONLINE);
        MDNS.begin(MDNS_HOSTNAME);
        resolveControlEndpoint();
        dlog("[boot] WiFi connected from stored creds, ip=%s, hostname=%s.local",
             WiFi.localIP().toString().c_str(), MDNS_HOSTNAME);
    } else {
        setStatus(Status::PROVISION);
        Serial.println("[boot] no stored WiFi creds; awaiting Improv push");
    }

    scan_i2c_bus();

    // I²S BEFORE codec config. This starts the master clock generator,
    // which the ES8311 needs running to sync its internal PLL. If we
    // tried to write codec registers without MCLK live, the chip's
    // clock-domain logic could lock up.
    if (i2s_rx_init()) {
        Serial.printf("[i2s] master init OK — MCLK=%d Hz on GPIO%d, "
                      "BCLK=GPIO%d, LRCK=GPIO%d, DIN=GPIO%d\n",
                      MCLK_HZ, PIN_I2S_MCLK, PIN_I2S_BCLK,
                      PIN_I2S_LRCK, PIN_I2S_DIN);
    } else {
        Serial.println("[i2s] init FAILED — driver_install or set_pin error");
    }

    delay(20);  // let MCLK stabilize before clocking out I²C writes

    if (es8311_init_for_capture()) {
        Serial.println("[es8311] codec init OK (16 kHz / 16-bit / mono / "
                       "analog mic / +24 dB PGA)");
    } else {
        Serial.println("[es8311] codec init FAILED — at least one register "
                       "write did not ACK. Check power and bus pull-ups.");
    }

    // Verify the writes stuck by reading them back. This is invaluable
    // when a register write silently fails (some ES8311 quirks: certain
    // registers don't accept writes until the chip is fully powered;
    // others read back masked).
    es8311_dump_regs();

    // ADC needs ~tens of ms to settle after analog power-up before it
    // produces stable PCM. Without this delay the first ~1k samples
    // are stuck at the chip's quiescent value (we observed `-7`
    // forever before the loop "wakes up"). 100 ms is conservative.
    delay(100);

    // Drain any junk samples the I²S DMA captured during codec
    // settling so the host gets clean audio from the very first byte.
    int16_t drain[256];
    size_t drained = 0;
    for (int i = 0; i < 8; i++) {
        i2s_read(I2S_PORT, drain, sizeof(drain), &drained, 10 / portTICK_PERIOD_MS);
    }

    // Sentinel for the host capture script. After this line, every
    // byte out USB-CDC is raw int16 little-endian PCM at 16 kHz mono.
    // No more text. The host splits on this marker.
    Serial.println("[stream-start]");
    Serial.flush();
}

void loop() {
    // Improv first so onboarding via USB-CDC works even when the audio
    // stream is filling Serial. The library scans incoming bytes for
    // the `IMPROV\\x01` magic prefix; PCM bytes that don't match are
    // ignored. handleSerial() is cheap when no Improv frame is in
    // progress (a quick read + compare).
    g_improv.handleSerial();

    // WiFi watchdog. If we drop offline, retry stored creds every 5 s
    // and re-resolve jasper-control on reconnect. State transitions
    // are logged so we can spot churny networks from the journal.
    static unsigned long lastWatchdog = 0;
    if (millis() - lastWatchdog > 5000) {
        lastWatchdog = millis();
        if (WiFi.status() != WL_CONNECTED) {
            String ssid = g_prefs.getString("ssid", "");
            if (ssid.length() > 0) {
                if (g_status != Status::OFFLINE) {
                    setStatus(Status::OFFLINE);
                    dlog("[wifi] disconnected; reconnecting…");
                }
                g_logTarget = IPAddress();  // re-resolve on reconnect
                WiFi.reconnect();
            } else if (g_status != Status::PROVISION) {
                setStatus(Status::PROVISION);
                Serial.println("[wifi] no creds; staying in PROVISION");
            }
        } else if (g_status != Status::ONLINE) {
            // Late-resolved discovery: tryConnectStored failed but a
            // later WiFi.reconnect() succeeded, so we hadn't run
            // resolveControlEndpoint yet.
            if ((uint32_t)g_logTarget == 0) resolveControlEndpoint();
            setStatus(Status::ONLINE);
            dlog("[wifi] online, ip=%s",
                 WiFi.localIP().toString().c_str());
        }
    }

    // Status indicator. Cheap dedupe so the panel only redraws on a
    // state transition — the SH8601 fillScreen+fillCircle+text takes
    // ~30 ms and would otherwise compete with the I²S read below for
    // wall time on every loop pass.
    static Status lastDrawn = (Status)0xFF;  // sentinel: never drawn
    Status now = g_status;
    if (now != lastDrawn) {
        displayShowStatus(now);
        lastDrawn = now;
    }

    // Continuous I²S read → USB-CDC raw write. After [stream-start]
    // is printed in setup(), this loop produces nothing but binary
    // PCM until the device is reset. The host's capture script splits
    // text-vs-binary on the sentinel.
    //
    // I²S is configured for stereo (see i2s_rx_init() comment about
    // why) so each LRCK period gives us 2 samples — left then right.
    // The codec only fills one slot meaningfully; we keep LEFT, drop
    // RIGHT. Read 512 stereo frames (= 256 mono samples = 16 ms at
    // 16 kHz), demux, write 512 bytes of mono PCM.
    static int16_t stereo_buf[512];   // L,R,L,R,...
    static int16_t mono_buf[256];     // L only
    size_t bytes_read = 0;
    if (i2s_read(I2S_PORT, stereo_buf, sizeof(stereo_buf),
                 &bytes_read, portMAX_DELAY) == ESP_OK
        && bytes_read >= 4) {
        size_t stereo_samples = bytes_read / sizeof(int16_t);  // L+R count
        size_t mono_samples = stereo_samples / 2;
        for (size_t i = 0; i < mono_samples; i++) {
            mono_buf[i] = stereo_buf[i * 2];  // even index = LEFT
        }
        Serial.write((const uint8_t *)mono_buf,
                     mono_samples * sizeof(int16_t));
    }
    // No delay() — i2s_read blocks until samples are ready, which is
    // the rate-limiter. Adding delay() would back-pressure DMA.
}
