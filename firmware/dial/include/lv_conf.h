// LVGL 8.3 configuration for the Jasper dial. Most options left at
// LVGL's defaults; only the few that matter for our build are set
// explicitly. See lv_conf_template.h in the upstream LVGL repo for
// the full menu.
#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

// --- Color depth / pixel format ---
// 16-bit RGB565, byte-swapped — LovyanGFX expects MSB-first when
// pushing pixel buffers to the GC9A01.
#define LV_COLOR_DEPTH 16
#define LV_COLOR_16_SWAP 1

// --- Memory ---
// Use libc malloc/free (Arduino exposes them). Avoids LVGL's own
// allocator which would carve a fixed pool out of internal RAM.
#define LV_MEM_CUSTOM 1
#define LV_MEM_SIZE (48U * 1024U)

// --- Tick source ---
// Use ESP-IDF's high-resolution timer for LVGL's tick instead of
// requiring us to call lv_tick_inc() from a periodic ISR.
#define LV_TICK_CUSTOM 1
#define LV_TICK_CUSTOM_INCLUDE "esp_timer.h"
#define LV_TICK_CUSTOM_SYS_TIME_EXPR (esp_timer_get_time() / 1000)

// --- Display refresh ---
#define LV_DISP_DEF_REFR_PERIOD 30   // ms between flushes (~33 Hz)
#define LV_INDEV_DEF_READ_PERIOD 30  // ms between input polls

// --- Logging ---
#define LV_USE_LOG 1
#define LV_LOG_LEVEL LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF 1

// --- Asserts ---
#define LV_USE_ASSERT_NULL 1
#define LV_USE_ASSERT_MALLOC 1

// --- Performance monitor (off in production) ---
#define LV_USE_PERF_MONITOR 0
#define LV_USE_MEM_MONITOR 0

// --- Themes ---
#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 1
#define LV_THEME_DEFAULT_GROW 1
#define LV_THEME_DEFAULT_TRANSITION_TIME 80

// --- Built-in fonts ---
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_MONTSERRAT_36 1
#define LV_FONT_MONTSERRAT_40 1
#define LV_FONT_MONTSERRAT_48 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

// --- Widgets we use ---
// LVGL widgets default to ON; explicitly leave them. Disable a couple
// of the heaviest unused ones to save flash.
#define LV_USE_ARC 1
#define LV_USE_LABEL 1
#define LV_USE_LINE 1
#define LV_USE_IMG 1
#define LV_USE_BTN 1
#define LV_USE_CANVAS 1
#define LV_USE_BAR 1
#define LV_USE_METER 0   // we use ARC for the volume gauge instead
#define LV_USE_TABLE 0
#define LV_USE_CHART 0
#define LV_USE_CALENDAR 0
#define LV_USE_KEYBOARD 0
#define LV_USE_TILEVIEW 0
#define LV_USE_TABVIEW 0
#define LV_USE_LIST 0
#define LV_USE_MENU 0

// --- File system / drawing extensions (off, we don't use) ---
#define LV_USE_FS_FATFS 0
#define LV_USE_FS_STDIO 0
#define LV_USE_FS_POSIX 0
#define LV_USE_FS_WIN32 0
#define LV_USE_PNG 0
#define LV_USE_BMP 0
#define LV_USE_SJPG 0
#define LV_USE_GIF 0
#define LV_USE_QRCODE 0
#define LV_USE_FREETYPE 0
#define LV_USE_RLOTTIE 0

#endif
