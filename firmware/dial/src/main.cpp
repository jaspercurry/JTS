// Jasper Dial — Phase 1 firmware
//
// Encoder rotation → POST /volume/adjust to jasper-control on the Pi.
// WiFi provisioning over Improv-over-Serial (no captive portal, no
// hardcoded SSID). Status colour on LED 0 of the WS2812 chain.
//
// Phase 2 (button click → /transport/toggle) and phase 3 (button hold
// → /session/start, release → /session/end) layer on top of this
// without restructuring.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <FastLED.h>
#include <HTTPClient.h>
#include <ImprovWiFiLibrary.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <lvgl.h>
#include <stdarg.h>
#include <time.h>

#include "config.h"
#include "display.h"
#include "scenes.h"

// Local time zone for the SNTP-driven clock face. Hardcoded to US
// Eastern with DST rules; phase 6 could read this from /etc/jasper/
// jasper.env on the Pi via /now-playing if you ship dials to other
// time zones.
#define DIAL_TZ "EST5EDT,M3.2.0,M11.1.0"

// --- LED status ---
//
// Six visible states. Colours chosen so they're legible on cheap
// USB-C extension cables in dim rooms — solid colours, not subtle
// blends.
//
//   BOOT        magenta solid   — power on, before any work
//   PROVISION   yellow blink    — no creds, awaiting Improv push
//   CONNECTING  yellow solid    — joining WiFi with stored creds
//   ONLINE      green dim       — WiFi up, Pi reachable
//   HTTP_ERROR  red blink       — WiFi up but jasper-control failed
//   OFFLINE     red solid       — WiFi dropped, retrying

enum class Status { BOOT, PROVISION, CONNECTING, ONLINE, HTTP_ERROR, OFFLINE };

static CRGB g_leds[NUM_LEDS];
static volatile Status g_status = Status::BOOT;

static void renderStatus() {
    static unsigned long lastBlink = 0;
    static bool blinkOn = true;
    if (millis() - lastBlink > 500) {
        lastBlink = millis();
        blinkOn = !blinkOn;
    }
    CRGB c = CRGB::Black;
    switch (g_status) {
        case Status::BOOT:       c = CRGB::Magenta; break;
        case Status::PROVISION:  c = blinkOn ? CRGB::Yellow : CRGB::Black; break;
        case Status::CONNECTING: c = CRGB::Yellow; break;
        case Status::ONLINE:     c = CRGB(0, 60, 0); break;  // dim green
        case Status::HTTP_ERROR: c = blinkOn ? CRGB::Red : CRGB::Black; break;
        case Status::OFFLINE:    c = CRGB::Red; break;
    }
    g_leds[0] = c;
    FastLED.show();
}

// --- Encoder ---
//
// Quadrature decoder using rolling state transitions. Each tick of
// the knob produces ENCODER_PULSES_PER_DETENT raw transitions; we
// accumulate raw and convert to detents in loop().

static volatile int32_t g_encoderRaw = 0;
static volatile uint8_t g_encoderLastState = 0;

static void IRAM_ATTR onEncoderChange() {
    uint8_t a = digitalRead(ENCODER_A_PIN);
    uint8_t b = digitalRead(ENCODER_B_PIN);
    uint8_t state = (a << 1) | b;
    // 4-bit transition: prev[1:0] | curr[1:0]. Cases swapped vs.
    // textbook quadrature so clockwise = volume up on the CrowPanel
    // HMI dial — the physical knob's CW direction matches the
    // *second* row of cases in this decoder, not the first. To flip
    // direction (e.g. for a different encoder wiring), swap ++/--.
    uint8_t transition = (g_encoderLastState << 2) | state;
    switch (transition) {
        case 0b0001: case 0b0111: case 0b1110: case 0b1000:
            g_encoderRaw--;
            break;
        case 0b0010: case 0b1011: case 0b1101: case 0b0100:
            g_encoderRaw++;
            break;
        // 0b0000 / 0b0101 / 0b1010 / 0b1111 = no change
        // 0b0011 / 0b1100 / 0b0110 / 0b1001 = double-step (noise);
        // ignore so a glitch on one line doesn't fake a tick.
    }
    g_encoderLastState = state;
}

// --- WiFi & HTTP ---

static Preferences g_prefs;
static ImprovWiFi g_improv(&Serial);
static WiFiUDP g_logUdp;
static IPAddress g_logTarget;  // (0,0,0,0) means "not yet resolved"

// Fire-and-forget log helper. Always prints to USB-CDC; if WiFi is up
// and we've resolved the Pi's IP, also sends one UDP datagram per call
// to the jasper-control dial-log listener. UDP loss is acceptable —
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

// SNTP setup: fire on every WiFi-up. Sets system time + TZ so the
// LVGL clock face has something to render. Idempotent; configTzTime
// is cheap and just (re)programs the SNTP daemon.
static void startSntp() {
    configTzTime(DIAL_TZ, "pool.ntp.org", "time.cloudflare.com", "time.google.com");
}

// Resolve the Pi's IP for UDP logging. Called whenever WiFi comes up.
// Cached in g_logTarget; cleared on disconnect so the next reconnect
// re-resolves (the Pi may have a new DHCP lease).
static void resolveLogTarget() {
    g_logTarget = MDNS.queryHost(JASPER_HOST);
    if ((uint32_t)g_logTarget != 0) {
        Serial.printf("[log] resolved %s.local → %s\n",
                      JASPER_HOST, g_logTarget.toString().c_str());
    } else {
        // Fall back to standard DNS resolution (router DNS often answers
        // for .local hostnames if mDNS is blocked).
        if (WiFi.hostByName(JASPER_HOST, g_logTarget)) {
            Serial.printf("[log] DNS-resolved %s → %s\n",
                          JASPER_HOST, g_logTarget.toString().c_str());
        } else {
            Serial.printf("[log] could not resolve %s — UDP logs disabled\n",
                          JASPER_HOST);
        }
    }
}

// Status of the most recent jasper-control POST. true = 2xx, false =
// transport/HTTP error. Used to flip the LED into HTTP_ERROR state
// without spamming console-only.
static volatile bool g_lastPostOk = true;

static bool postJson(const char *path, const char *body) {
    if (WiFi.status() != WL_CONNECTED) {
        g_lastPostOk = false;
        return false;
    }
    HTTPClient http;
    String url = String("http://") + JASPER_HOST + ":" + String(JASPER_PORT) + path;
    if (!http.begin(url)) {
        g_lastPostOk = false;
        return false;
    }
    http.setConnectTimeout(2000);
    http.setTimeout(3000);  // toggle path may query Spotify before responding
    http.addHeader("Content-Type", "application/json");
    int code = http.POST((uint8_t *)body, strlen(body));
    http.end();
    bool ok = (code >= 200 && code < 300);
    g_lastPostOk = ok;
    return ok;
}

// Tracks the latest volume percent reported by the Pi. Used to seed
// the volume gauge — without this, a dial-driven turn would only
// know the *delta* it sent, not where the gauge should land.
static int g_lastVolumePercent = -1;

static bool postVolumeAdjust(float deltaDb) {
    if (WiFi.status() != WL_CONNECTED) {
        g_lastPostOk = false;
        return false;
    }
    HTTPClient http;
    String url = String("http://") + JASPER_HOST + ":" + String(JASPER_PORT) + "/volume/adjust";
    if (!http.begin(url)) {
        g_lastPostOk = false;
        return false;
    }
    http.setConnectTimeout(2000);
    http.setTimeout(2000);
    http.addHeader("Content-Type", "application/json");

    char body[48];
    snprintf(body, sizeof(body), "{\"delta_db\":%.2f}", deltaDb);

    int code = http.POST((uint8_t *)body, strlen(body));
    bool ok = (code >= 200 && code < 300);
    if (ok) {
        // Parse {"db":..,"percent":..} so the gauge can show the new
        // level rather than guessing from sent delta.
        StaticJsonDocument<128> doc;
        if (!deserializeJson(doc, http.getString())) {
            int p = doc["percent"] | -1;
            if (p >= 0) g_lastVolumePercent = p;
        }
    }
    http.end();
    g_lastPostOk = ok;
    return ok;
}

static bool postTransportToggle() {
    return postJson("/transport/toggle", "{}");
}

static bool postSessionStart() {
    return postJson("/session/start", "{}");
}

static bool postSessionEnd() {
    return postJson("/session/end", "{}");
}

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
        renderStatus();
        delay(100);
    }
    return WiFi.status() == WL_CONNECTED;
}

// Improv: callback fired when the host (improv-wifi.com or
// jasper-dial-onboard.py) successfully provisions credentials.
// The library has already proven the SSID/password works before
// calling this, so we persist immediately and announce on mDNS so
// the Pi can find us by hostname.
static void onImprovConnected(const char *ssid, const char *password) {
    g_prefs.putString("ssid", ssid);
    g_prefs.putString("pass", password);
    MDNS.begin(MDNS_HOSTNAME);
    resolveLogTarget();
    startSntp();
    scenes_set_status("provisioned");
    g_status = Status::ONLINE;
    dlog("[improv] connected, ip=%s, hostname=%s.local",
         WiFi.localIP().toString().c_str(), MDNS_HOSTNAME);
}

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

// --- setup / loop ---

void setup() {
    Serial.begin(115200);
    // Give USB-CDC ~1 s to enumerate so the host sees our boot prints.
    // Without this, the magenta-LED-set-then-silence symptom looks
    // identical to a setup() hang to anyone watching the serial port.
    delay(1000);
    Serial.println("[boot] jasper-dial firmware v" JASPER_DIAL_FIRMWARE_VERSION);


    // Hardware peripherals (encoder, RMT for WS2812 ring, NVS) FIRST.
    // Order matters: when this block ran AFTER display_init +
    // display_start_lvgl_task, FastLED.addLeds() hung — apparently
    // contending with LovyanGFX/LVGL for an SPI-DMA or RMT channel
    // allocation. Initializing the simple peripherals before the
    // display stack avoids the contention entirely.
    pinMode(ENCODER_A_PIN, INPUT_PULLUP);
    pinMode(ENCODER_B_PIN, INPUT_PULLUP);
    pinMode(SWITCH_PIN, INPUT_PULLUP);
    g_encoderLastState = (digitalRead(ENCODER_A_PIN) << 1) | digitalRead(ENCODER_B_PIN);
    attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), onEncoderChange, CHANGE);
    attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), onEncoderChange, CHANGE);
    Serial.println("[boot] encoder pins armed");

    FastLED.addLeds<WS2812, RGB_LED_PIN, GRB>(g_leds, NUM_LEDS);
    FastLED.setBrightness(LED_BRIGHTNESS);
    g_status = Status::BOOT;
    renderStatus();
    Serial.println("[boot] LED ring init");

    g_prefs.begin("jasper", false);
    Serial.println("[boot] prefs (NVS) opened");

    // Display + LVGL come up AFTER FastLED so peripheral allocation
    // doesn't contend. LVGL task isn't started yet — we spawn it at
    // the very end of setup, after WiFi is up, so disp_flush can't
    // hang the main thread before we've finished provisioning.
    display_init();
    scenes_init();
    scenes_set_status("booting");
    display_set_backlight(180);
    Serial.println("[boot] display + LVGL up");

    // Use the 4-arg overload — passing nullptr to the 5-arg deviceUrl
    // form crashes some library versions (the library does
    // String(deviceUrl) without a null check, and on ESP32 String's
    // ctor does strlen on null).
    g_improv.setDeviceInfo(
        ImprovTypes::ChipFamily::CF_ESP32_S3,
        DEVICE_NAME,
        JASPER_DIAL_FIRMWARE_VERSION,
        DEVICE_MFG
    );
    g_improv.onImprovConnected(onImprovConnected);
    g_improv.setCustomConnectWiFi(improvConnect);
    Serial.println("[boot] improv configured");

    if (tryConnectStored()) {
        g_status = Status::ONLINE;
        MDNS.begin(MDNS_HOSTNAME);
        resolveLogTarget();
        startSntp();
        scenes_set_status("online");
        dlog("[boot] WiFi connected from stored creds, ip=%s",
             WiFi.localIP().toString().c_str());
    } else {
        // No creds (or bad creds) — sit in PROVISION until the host
        // pushes credentials over Improv. The jasper-dial-onboard
        // script on the Pi does this; for laptop dev,
        // https://www.improv-wifi.com/ does the same.
        g_status = Status::PROVISION;
        Serial.println("[boot] no stored creds; awaiting Improv");
    }

    // Now safe to spawn the LVGL task. WiFi is up (or we're in
    // PROVISION mode and no race against WiFi can happen). If
    // disp_flush still hangs at this point, only the screen breaks —
    // encoder, button, watchdog, and Improv all keep running.
    display_start_lvgl_task();
    Serial.println("[boot] LVGL task spawned");
}

void loop() {
    g_improv.handleSerial();

    // Encoder → volume. Snapshot the volatile counter, compute new
    // detents since last apply, send one POST per net detent change.
    static int32_t lastApplied = 0;
    int32_t snapshot = g_encoderRaw;
    int32_t deltaRaw = snapshot - lastApplied;
    int32_t detents = deltaRaw / ENCODER_PULSES_PER_DETENT;
    if (detents != 0) {
        lastApplied += detents * ENCODER_PULSES_PER_DETENT;
        if (WiFi.status() == WL_CONNECTED) {
            bool ok = postVolumeAdjust((float)detents * VOLUME_STEP_DB);
            if (ok && g_lastVolumePercent >= 0) {
                scenes_show_volume(g_lastVolumePercent);
            }
            dlog("[encoder] detent=%ld → POST %.2f dB %s (now %d%%)",
                 (long)detents, (float)detents * VOLUME_STEP_DB,
                 ok ? "OK" : "FAIL", g_lastVolumePercent);
        } else {
            dlog("[encoder] detent=%ld dropped (WiFi disconnected)",
                 (long)detents);
        }
    }

    // Button (encoder press) — polled with software debounce.
    //   Short press (release before LONG_PRESS_MS): /transport/toggle
    //   Long press: at LONG_PRESS_MS while still held, /session/start
    //               (low latency — fires DURING the hold, not after);
    //               on release, /session/end so Gemini can respond.
    // Dispatching the long-press start mid-hold lets the user begin
    // talking the instant they're past the threshold; the dial's LED
    // could indicate "listening" if we wanted, though phase 5 owns
    // visuals.
    static bool buttonPrev = HIGH;
    static unsigned long buttonChangedAt = 0;
    static unsigned long buttonPressedAt = 0;
    static bool sessionStarted = false;
    bool buttonNow = digitalRead(SWITCH_PIN);
    unsigned long now = millis();
    if (buttonNow != buttonPrev && now - buttonChangedAt > 30) {
        buttonChangedAt = now;
        buttonPrev = buttonNow;
        if (buttonNow == LOW) {
            // press
            buttonPressedAt = now;
            sessionStarted = false;
        } else {
            // release
            unsigned long held = now - buttonPressedAt;
            if (sessionStarted) {
                bool ok = postSessionEnd();
                scenes_set_listening(false);
                dlog("[button] long-press release (%lums) → session/end %s",
                     held, ok ? "OK" : "FAIL");
            } else if (held < LONG_PRESS_MS) {
                bool ok = postTransportToggle();
                dlog("[button] short-press (%lums) → toggle %s",
                     held, ok ? "OK" : "FAIL");
            } else {
                // Held past the threshold but session never started —
                // most likely WiFi was down at the moment. The user got
                // no listening feedback (no session opened) so don't
                // surprise them with end_input either.
                dlog("[button] long-press release (%lums) but session never started",
                     held);
            }
        }
    } else if (
        buttonNow == LOW
        && !sessionStarted
        && buttonPressedAt > 0
        && (now - buttonPressedAt) >= LONG_PRESS_MS
    ) {
        // Mid-press: still holding past the threshold, fire session/start.
        sessionStarted = true;
        bool ok = postSessionStart();
        scenes_set_listening(true);
        dlog("[button] long-press start at %lums → session/start %s",
             now - buttonPressedAt, ok ? "OK" : "FAIL");
    }

    // LVGL is pumped on its own FreeRTOS task pinned to core 0
    // (see display.cpp). Calling lv_timer_handler() from this loop
    // wedged the dial — disp_flush over SPI took long enough that
    // the next call landed before the previous returned, starving
    // encoder/button polling. Mutexed task = clean separation.

    // WiFi watchdog — every 5 s, if we're not connected and have
    // creds, try again. If we're connected but the last POST failed,
    // surface that on the LED so the user knows it's a Pi-side issue
    // not a WiFi issue.
    static unsigned long lastWatchdog = 0;
    if (millis() - lastWatchdog > 5000) {
        lastWatchdog = millis();
        if (WiFi.status() != WL_CONNECTED) {
            String ssid = g_prefs.getString("ssid", "");
            if (ssid.length() > 0) {
                g_status = Status::OFFLINE;
                g_logTarget = IPAddress();  // re-resolve on next connect
                WiFi.reconnect();
            } else {
                g_status = Status::PROVISION;
            }
        } else {
            // Late mDNS resolve: if WiFi came up via reconnect after
            // setup() picked the no-creds path, the log target won't
            // be set yet. Resolve once we see WL_CONNECTED.
            if ((uint32_t)g_logTarget == 0) resolveLogTarget();
            g_status = g_lastPostOk ? Status::ONLINE : Status::HTTP_ERROR;
        }
    }

    renderStatus();
    delay(2);
}
