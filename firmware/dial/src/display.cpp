// LovyanGFX + LVGL bring-up for the GC9A01 round display + CST816D
// touch. Pin assignments come from include/config.h, which mirrors
// the CrowPanel HMI factory firmware.

#define LGFX_USE_V1
#include <Arduino.h>
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

// LVGL → display: copy LVGL's rendered buffer to the GC9A01.
static void disp_flush(lv_disp_drv_t *drv, const lv_area_t *area,
                       lv_color_t *color_p) {
    uint32_t w = area->x2 - area->x1 + 1;
    uint32_t h = area->y2 - area->y1 + 1;
    gfx.startWrite();
    gfx.setAddrWindow(area->x1, area->y1, w, h);
    gfx.pushPixels((uint16_t *)color_p, w * h, true);
    gfx.endWrite();
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
    // Power LED on as a "we have power" indicator before the
    // backlight comes up. Useful when debugging a black screen.
    pinMode(POWER_LED_PIN, OUTPUT);
    digitalWrite(POWER_LED_PIN, HIGH);

    // Backlight PWM — start at 0 (display dark), bring it up after
    // we've drawn the splash so the user doesn't see a flash of
    // garbage from the framebuffer's power-on state.
    ledcSetup(BACKLIGHT_PWM_CHANNEL, BACKLIGHT_PWM_FREQ_HZ, BACKLIGHT_PWM_RES_BITS);
    ledcAttachPin(TFT_BACKLIGHT, BACKLIGHT_PWM_CHANNEL);
    display_set_backlight(0);

    gfx.init();
    gfx.setRotation(0);
    gfx.fillScreen(0x0000);  // black

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

    // Allocate the LVGL flush buffers in DMA-capable internal RAM.
    // PSRAM-backed buffers hang the SPI controller mid-transfer
    // because the GDMA peripheral doesn't safely service PSRAM reads
    // at our 80 MHz SPI clock — DMA fetches stall and disp_flush
    // never returns. Internal RAM keeps DMA happy at the cost of
    // ~58 KB out of the chip's 320 KB DRAM (we have plenty).
    size_t buf_bytes = sizeof(lv_color_t) * 240 * LVGL_BUF_LINES;
    lvgl_buf1 = (lv_color_t *)heap_caps_malloc(
        buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    lvgl_buf2 = (lv_color_t *)heap_caps_malloc(
        buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
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
