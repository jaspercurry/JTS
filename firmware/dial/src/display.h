// Display driver layer: LovyanGFX (GC9A01) + LVGL framework. Owns
// the global gfx object and the LVGL draw buffer; everything else
// (scene graph) lives in scenes.h/cpp.
#pragma once

#include <lvgl.h>

void display_init();
// Set backlight 0..255 via PWM. 0 = off, 200 = comfortable indoor.
void display_set_backlight(uint8_t level);
// Spawn the LVGL pump task on core 0. Call AFTER scenes_init so the
// task's first iteration sees a fully-built scene graph.
void display_start_lvgl_task();
