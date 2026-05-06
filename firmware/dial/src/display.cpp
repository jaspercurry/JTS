// LovyanGFX + LVGL bring-up for the GC9A01 round display + CST816D
// touch. Pin assignments come from include/config.h, which mirrors
// the CrowPanel HMI factory firmware.

#define LGFX_USE_V1
#include <Arduino.h>
#include <Wire.h>
#include <LovyanGFX.hpp>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>

#include "display.h"
#include "config.h"
#include "scenes.h"
#include "CST816D.h"

// LovyanGFX wrapper for the GC9A01. Mirrors the factory firmware
// configuration (SPI 80 MHz, 240x240, color-inverted, RGB order
// false, no MISO/busy lines).
class LGFX : public lgfx::LGFX_Device {
    lgfx::Panel_GC9A01 _panel;
    lgfx::Bus_SPI _bus;

public:
    LGFX() {
        {
            auto cfg = _bus.config();
            cfg.spi_host = SPI2_HOST;
            cfg.spi_mode = 0;
            cfg.freq_write = 80000000;
            cfg.freq_read = 20000000;
            cfg.spi_3wire = true;
            cfg.use_lock = true;
            cfg.dma_channel = SPI_DMA_CH_AUTO;
            cfg.pin_sclk = TFT_SCLK;
            cfg.pin_mosi = TFT_MOSI;
            cfg.pin_miso = -1;
            cfg.pin_dc = TFT_DC;
            _bus.config(cfg);
            _panel.setBus(&_bus);
        }
        {
            auto cfg = _panel.config();
            cfg.pin_cs = TFT_CS;
            cfg.pin_rst = TFT_RST;
            cfg.pin_busy = -1;
            cfg.memory_width = 240;
            cfg.memory_height = 240;
            cfg.panel_width = 240;
            cfg.panel_height = 240;
            cfg.offset_x = 0;
            cfg.offset_y = 0;
            cfg.offset_rotation = 0;
            cfg.dummy_read_pixel = 8;
            cfg.dummy_read_bits = 1;
            cfg.readable = false;
            cfg.invert = true;     // GC9A01 reads "normal" panels
                                   // inverted; matches factory.
            cfg.rgb_order = false;
            cfg.dlen_16bit = false;
            cfg.bus_shared = false;
            _panel.config(cfg);
        }
        setPanel(&_panel);
    }
};

static LGFX gfx;
static CST816D touch(TP_I2C_SDA, TP_I2C_SCL, TP_RST, TP_INT);

// LVGL draw buffer. Half-screen height in PSRAM; double-buffered
// so DMA + render overlap. 240 * 120 * 2 = 57.6 KB per buffer.
static lv_disp_draw_buf_t lvgl_draw_buf;
static lv_color_t *lvgl_buf1 = nullptr;
static lv_color_t *lvgl_buf2 = nullptr;
static lv_disp_drv_t lvgl_disp_drv;
static lv_indev_drv_t lvgl_indev_drv;

// LVGL → display: hand the rendered buffer to LovyanGFX's DMA path.
// Using pushImageDMA (not pushPixels) so PSRAM-backed buffers work —
// the cache-aware DMA descriptor that pushImageDMA sets up handles
// PSRAM reads, where the lower-level pushPixels DMA path doesn't.
// startWrite/endWrite are bracketed across calls (factory pattern):
// gfx.startWrite() is called once at boot, and we just check
// getStartCount() here so back-to-back flushes don't double-bracket.
static void disp_flush(lv_disp_drv_t *drv, const lv_area_t *area,
                       lv_color_t *color_p) {
    uint32_t w = area->x2 - area->x1 + 1;
    uint32_t h = area->y2 - area->y1 + 1;
    if (gfx.getStartCount() > 0) gfx.endWrite();
    gfx.pushImageDMA(area->x1, area->y1, w, h,
                     (lgfx::rgb565_t *)&color_p->full);
    lv_disp_flush_ready(drv);
}

// CST816D → LVGL: report finger pressure as pointer events. Gestures
// (swipe, double-tap) are read but currently unused — LVGL does its
// own gesture detection from the pointer stream when needed.
static void touch_read(lv_indev_drv_t *drv, lv_indev_data_t *data) {
    uint16_t x = 0, y = 0;
    uint8_t gesture = 0;
    if (touch.getTouch(&x, &y, &gesture)) {
        data->state = LV_INDEV_STATE_PR;
        data->point.x = x;
        data->point.y = y;
    } else {
        data->state = LV_INDEV_STATE_REL;
    }
}

void display_set_backlight(uint8_t level) {
    ledcWrite(BACKLIGHT_PWM_CHANNEL, level);
}

// LVGL pump task. Pinned to core 0 so it doesn't compete with the
// Arduino loop (core 1) for CPU. Any LVGL state mutation from main
// (scenes_set_*) must take the same mutex this task holds while
// rendering, otherwise the renderer can dereference half-built
// objects. The mutex is owned by scenes.cpp; we acquire it via
// scenes_lock/unlock.
static TaskHandle_t lvgl_task_handle = nullptr;

static void lvgl_task_loop(void *) {
    while (true) {
        scenes_lock();
        lv_timer_handler();
        scenes_tick();
        scenes_unlock();
        // ~30 fps target. Lower bound by panel SPI throughput, not
        // CPU — disp_flush at 80 MHz pushes 240×120 in ~6 ms.
        vTaskDelay(pdMS_TO_TICKS(30));
    }
}

void display_init() {
    // CRITICAL: power-enable pins for the panel + backlight rail.
    // Without driving GPIO1 and GPIO2 HIGH, the panel has no power
    // and stays completely dark regardless of SPI / init state.
    // This is the CrowPanel HMI's onboard power-gating — see the
    // factory firmware setup() for the canonical reference.
    pinMode(PANEL_POWER_PIN_1, OUTPUT);
    digitalWrite(PANEL_POWER_PIN_1, HIGH);
    pinMode(PANEL_POWER_PIN_2, OUTPUT);
    digitalWrite(PANEL_POWER_PIN_2, HIGH);

    // Power LED is ACTIVE LOW on this board (factory drives it LOW
    // to enable). Setting HIGH keeps it off — that's fine, our
    // WS2812 ring is the user-visible status indicator anyway.
    pinMode(POWER_LED_PIN, OUTPUT);
    digitalWrite(POWER_LED_PIN, LOW);

    // Default I2C bus (pins 38/39) — factory inits this for the
    // onboard SSD1306 OLED. We don't use the OLED but bring the bus
    // up to match factory behavior in case any board-init magic
    // depends on it.
    Wire.begin(I2C_SDA, I2C_SCL);

    bool gfx_ok = gfx.init();
    gfx.initDMA();   // factory: required after init() for pushImageDMA
    Serial.printf("[disp] gfx.init() = %d\n", (int)gfx_ok);
    gfx.setRotation(0);

    // Diagnostic test pattern: prove panel + SPI work *before* we
    // touch LVGL. If the user sees RED→GREEN→BLUE→BLACK during boot,
    // the panel responds; we know any subsequent dark-screen problem
    // is in LVGL's flush_cb. If they see nothing, the panel itself
    // isn't being driven (init/pin/clock issue).
    //
    // Backlight is brought up here at full so the test colors are
    // unambiguous. The main caller resets it to its preferred level
    // after display_init returns.
    ledcSetup(BACKLIGHT_PWM_CHANNEL, BACKLIGHT_PWM_FREQ_HZ, BACKLIGHT_PWM_RES_BITS);
    ledcAttachPin(TFT_BACKLIGHT, BACKLIGHT_PWM_CHANNEL);
    ledcWrite(BACKLIGHT_PWM_CHANNEL, 255);
    Serial.println("[disp] panel test: red");
    gfx.fillScreen(0xF800);
    delay(1200);
    Serial.println("[disp] panel test: green");
    gfx.fillScreen(0x07E0);
    delay(1200);
    Serial.println("[disp] panel test: blue");
    gfx.fillScreen(0x001F);
    delay(1200);
    Serial.println("[disp] panel test: black");
    gfx.fillScreen(0x0000);

    // Touch is initialized but NOT registered as an LVGL input
    // device. The vendored CST816D driver's i2c_read() spins forever
    // if the chip doesn't ACK (do-while loop on rdDataCount), which
    // hangs the LVGL pump task every time it polls for input — that
    // in turn holds the scene mutex and starves anything in main
    // that tries to update UI. Touch interactions are a phase-6
    // follow-up; we'll re-enable when we either trust the chip or
    // wrap getTouch() with a timeout.
    // touch.begin();
    (void)touch;

    lv_init();

    // Full-screen double buffer in PSRAM (matches factory). The
    // PSRAM path works *only* because we use pushImageDMA in the
    // flush callback — pushPixels' direct-DMA path can't safely
    // read PSRAM. 240×240×2 = 115 KB each, 230 KB total in 8 MB
    // PSRAM. Falls back to internal RAM if PSRAM isn't available.
    size_t buf_bytes = sizeof(lv_color_t) * 240 * LVGL_BUF_LINES;
    lvgl_buf1 = (lv_color_t *)heap_caps_malloc(buf_bytes, MALLOC_CAP_SPIRAM);
    lvgl_buf2 = (lv_color_t *)heap_caps_malloc(buf_bytes, MALLOC_CAP_SPIRAM);
    if (!lvgl_buf1) lvgl_buf1 = (lv_color_t *)malloc(buf_bytes);
    if (!lvgl_buf2) lvgl_buf2 = (lv_color_t *)malloc(buf_bytes);
    Serial.printf("[disp] flush bufs: %p / %p (%u bytes ea)\n",
                  lvgl_buf1, lvgl_buf2, (unsigned)buf_bytes);
    lv_disp_draw_buf_init(&lvgl_draw_buf, lvgl_buf1, lvgl_buf2,
                          240 * LVGL_BUF_LINES);

    lv_disp_drv_init(&lvgl_disp_drv);
    lvgl_disp_drv.hor_res = 240;
    lvgl_disp_drv.ver_res = 240;
    lvgl_disp_drv.flush_cb = disp_flush;
    lvgl_disp_drv.draw_buf = &lvgl_draw_buf;
    lv_disp_drv_register(&lvgl_disp_drv);

    // Touch input device deliberately not registered — see comment
    // above touch.begin().
    (void)lvgl_indev_drv;
    (void)touch_read;
}

void display_start_lvgl_task() {
    // Created AFTER scenes_init so the first lv_timer_handler tick
    // sees a built scene graph, not a half-initialized one.
    xTaskCreatePinnedToCore(
        lvgl_task_loop, "lvgl", 8192, nullptr, 5, &lvgl_task_handle, 0);
}
