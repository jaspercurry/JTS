// SH8601 AMOLED bring-up + status indicator. Phase 1.2 scope: just
// "draw a colored circle and a label so the user can see what state
// the satellite is in." Direct Arduino_GFX draws — no LVGL yet.
//
// Hardware: Waveshare ESP32-S3-Touch-AMOLED-1.8 (368×448 SH8601 over
// QSPI). The SH8601 reset line is NOT a direct GPIO — it sits behind
// a TCA9554 I²C expander on P0. Arduino_GFX's Arduino_SH8601 expects
// to drive reset itself; we hand it GFX_NOT_DEFINED and toggle reset
// over I²C externally before calling begin().
//
// Reference project: vthinkxie/claude-desktop-buddy-esp32-s3-touch-
// amoled-1.8 — same board, same TCA9554+SH8601 pattern. Diverges only
// in that they vendor Adafruit_XCA9554; we inline the few register
// pokes we need (~25 lines) to avoid the dependency.

#include "display.h"

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <Wire.h>

#include "config.h"

// --- TCA9554 I/O expander helper ---
//
// Register map (datasheet TI TCA9554 / NXP PCA9554):
//   0x00  input port    (read-only — current pin levels)
//   0x01  output port   (write here to set output values)
//   0x02  polarity inv  (we leave at 0 = no inversion)
//   0x03  configuration (0=output, 1=input; default 0xFF = all input)
//
// We cache the output and config registers in RAM so multi-pin updates
// don't require a read-modify-write round-trip on each call. Reset
// values (0xFF for cfg, 0x00 for out) match the chip's POR state.
static uint8_t s_tcaCfg = 0xFF;
static uint8_t s_tcaOut = 0x00;

static bool tcaWrite(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(TCA9554_I2C_ADDR);
    Wire.write(reg);
    Wire.write(val);
    return Wire.endTransmission() == 0;
}

// Drive expander pin `pin` (0..7) to `level`. Reconfigures from input
// to output on the first call for that pin. Returns false on bus error.
static bool tcaSetPin(uint8_t pin, bool level) {
    uint8_t mask = 1 << pin;
    if (s_tcaCfg & mask) {
        s_tcaCfg &= ~mask;
        if (!tcaWrite(0x03, s_tcaCfg)) return false;
    }
    if (level) s_tcaOut |=  mask;
    else       s_tcaOut &= ~mask;
    return tcaWrite(0x01, s_tcaOut);
}

// Pull SH8601 RESN low for 20 ms then release high for 20 ms.
// SH8601 datasheet asks for ≥10 µs reset pulse and ≥120 ms after
// release before sending commands; the 20+20 here is comfortably
// above the pulse min and the gfx->begin() init sequence eats the
// post-release settling delay.
static bool releaseLcdFromReset() {
    if (!tcaSetPin(TCA9554_EXIO_DSI_PWR_EN, true))   return false;  // power rail up first
    delay(5);
    if (!tcaSetPin(TCA9554_EXIO_LCD_RESET, false))   return false;  // assert reset
    delay(20);
    if (!tcaSetPin(TCA9554_EXIO_LCD_RESET, true))    return false;  // release reset
    delay(20);
    return true;
}

// --- Arduino_GFX panel + bus ---
//
// `s_gfx` is typed as Arduino_SH8601* (not the Arduino_GFX base) because
// setBrightness() lives on Arduino_OLED — the AMOLED-specific subclass —
// not the GFX core. Drawing primitives (fillScreen / fillCircle / print)
// are inherited from Arduino_GFX so we don't lose anything by typing the
// pointer this way.

static Arduino_DataBus *s_bus  = nullptr;
static Arduino_SH8601  *s_gfx  = nullptr;

// Arduino_GFX (current versions) doesn't define BLACK/WHITE convenience
// macros; we use raw RGB565 hex everywhere else, but the two greys come
// up often enough to deserve names.
static constexpr uint16_t COLOR_BLACK = 0x0000;
static constexpr uint16_t COLOR_WHITE = 0xFFFF;

bool displayInit() {
    if (!releaseLcdFromReset()) {
        Serial.println("[disp] TCA9554 init / reset sequence failed — "
                       "is the expander on the I²C bus at 0x20?");
        return false;
    }

    s_bus = new Arduino_ESP32QSPI(
        PIN_LCD_QSPI_CS, PIN_LCD_QSPI_SCLK,
        PIN_LCD_QSPI_D0, PIN_LCD_QSPI_D1,
        PIN_LCD_QSPI_D2, PIN_LCD_QSPI_D3
    );
    s_gfx = new Arduino_SH8601(
        s_bus,
        GFX_NOT_DEFINED,             // reset handled externally above
        LCD_ROTATION,
        LCD_PANEL_WIDTH, LCD_PANEL_HEIGHT
    );

    if (!s_gfx->begin()) {
        Serial.println("[disp] Arduino_SH8601 begin() returned false");
        return false;
    }
    s_gfx->fillScreen(COLOR_BLACK);
    s_gfx->setBrightness(150);       // ~59 % of max — bright enough to
                                     // read across a room without
                                     // burning AMOLED pixels at idle.
    Serial.printf("[disp] SH8601 up: %dx%d\n",
                  LCD_PANEL_WIDTH, LCD_PANEL_HEIGHT);
    return true;
}

// Color + label for each connection state. Color choices mirror the
// dial's WS2812 convention from docs/satellites.md so the two
// satellites read the same at a glance:
//   BOOT       magenta
//   PROVISION  yellow
//   CONNECTING orange   (distinct from PROVISION since both are
//                        "still working on it" but mean different
//                        things — colour helps disambiguate)
//   ONLINE     green
//   HTTP_ERROR bright red
//   OFFLINE    dim red  (same hue as HTTP_ERROR — desaturated to
//                        differentiate "WiFi gone" from "WiFi up but
//                        a POST failed")
struct StatusVisual {
    uint16_t color;
    const char *label;
};

static StatusVisual visualFor(Status s) {
    switch (s) {
        case Status::BOOT:        return { 0xF81F, "Boot"          };
        case Status::PROVISION:   return { 0xFFE0, "Awaiting WiFi" };
        case Status::CONNECTING:  return { 0xFD20, "Connecting"    };
        case Status::ONLINE:      return { 0x07E0, "Online"        };
        case Status::HTTP_ERROR:  return { 0xF800, "HTTP error"    };
        case Status::OFFLINE:     return { 0x6000, "Offline"       };
    }
    return { 0xFFFF, "?" };  // unreachable; satisfies the compiler
}

void displayShowStatus(Status s) {
    if (!s_gfx) return;  // displayInit() never ran or failed
    StatusVisual v = visualFor(s);

    // Centred circle, label centred below. Adafruit_GFX's default 5×7
    // font becomes 6N × 8N including inter-glyph spacing at textSize=N.
    // Use s_gfx->width()/height() (post-rotation) so coords stay right
    // when LCD_ROTATION changes — the constants in config.h are the
    // *native* panel dims, before the rotation swap.
    constexpr int textSize    = 3;
    constexpr int charW       = 6 * textSize;
    constexpr int charH       = 8 * textSize;
    const int     screenW     = s_gfx->width();
    const int     screenH     = s_gfx->height();
    const int     circleX     = screenW / 2;
    const int     circleY     = screenH / 2 - 30;
    constexpr int circleR     = 60;
    constexpr int labelGap    = 30;
    int textW = (int)strlen(v.label) * charW;
    int textX = (screenW - textW) / 2;
    int textY = circleY + circleR + labelGap;

    s_gfx->fillScreen(COLOR_BLACK);
    s_gfx->fillCircle(circleX, circleY, circleR, v.color);
    s_gfx->setTextColor(COLOR_WHITE);
    s_gfx->setTextSize(textSize);
    s_gfx->setCursor(textX, textY);
    s_gfx->print(v.label);
    (void)charH;  // reserved for a future second-line glyph
}
