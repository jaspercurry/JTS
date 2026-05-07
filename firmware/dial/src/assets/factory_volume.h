// Vendored factory volume-gauge assets — pulled from
//   github.com/Elecrow-RD/CrowPanel-1.28inch-HMI-ESP32-Rotary-Display-…
//   /example/libraries/UI/ui_img_*.c
// SquareLine-Studio-generated `lv_img_dsc_t` blobs. The four images
// together compose the factory's volume-screen look:
//
//   bj_volume_100  — 240×240 dark background with concentric arcs
//   icon_volume_40 — 20×20 speaker icon shown above the percent
//   bar_light_01   — gradient texture used as the arc's "track"
//   bar_bule_02    — cyan glossy texture used as the arc's "fill"
//
// The .c files include `<lvgl.h>` (we substitute that for the
// SquareLine-relative `"ui.h"` at vendor time) and define the
// LV_IMG_DECLARE'd symbols below.
#pragma once
#include <lvgl.h>

#ifdef __cplusplus
extern "C" {
#endif

LV_IMG_DECLARE(ui_img_v2_bj_volume_100_png);
LV_IMG_DECLARE(ui_img_icon_volume_40_png);
LV_IMG_DECLARE(ui_img_bar_light_01_png);
LV_IMG_DECLARE(ui_img_bar_bule_02_png);

#ifdef __cplusplus
}
#endif
