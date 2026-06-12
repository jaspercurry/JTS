// Scene graph + LVGL UI for the dial. See scenes.h for the state
// model. Aesthetic: modern hi-fi — midnight-blue background, warm
// white type, accent color per state.

#include <Arduino.h>
#include <lvgl.h>
#include <time.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "scenes.h"

// --- Palette ---------------------------------------------------------------
// Background and text colors stay constant; only the accent shifts
// with state (idle → warm-white, listening → cyan, etc.).

static const lv_color_t COL_BG       = LV_COLOR_MAKE(0x05, 0x09, 0x14);  // midnight blue
static const lv_color_t COL_BG_SOFT  = LV_COLOR_MAKE(0x10, 0x18, 0x28);
static const lv_color_t COL_TEXT     = LV_COLOR_MAKE(0xF2, 0xE9, 0xD8);  // warm white
static const lv_color_t COL_TEXT_DIM = LV_COLOR_MAKE(0x90, 0x88, 0x78);
static const lv_color_t COL_ACCENT   = LV_COLOR_MAKE(0xC9, 0xA1, 0x4F);  // brushed brass
static const lv_color_t COL_LISTEN   = LV_COLOR_MAKE(0x4E, 0xC4, 0xD8);  // cyan

// --- Top-level layout ------------------------------------------------------
// One root screen, with overlapping containers we show/hide based on
// state. LVGL's "screen" abstraction would let us swap entire trees,
// but for a 240x240 display with tightly-coupled overlays (volume
// overlay layered on top of the clock or now-playing) it's simpler
// to keep one tree and toggle visibility.

static lv_obj_t *root        = nullptr;
static lv_obj_t *splash      = nullptr;
static lv_obj_t *splash_lbl  = nullptr;
static lv_obj_t *status_lbl  = nullptr;

// Clock face
static lv_obj_t *clock_face  = nullptr;
static lv_obj_t *clock_time  = nullptr;

// Volume overlay
static lv_obj_t *vol_overlay = nullptr;
static lv_obj_t *vol_arc     = nullptr;
static lv_obj_t *vol_label   = nullptr;
static uint32_t  vol_until_ms = 0;

// Listening orb
static lv_obj_t *orb_overlay = nullptr;
static lv_obj_t *orb         = nullptr;
static bool      orb_active  = false;

// Now-playing card
static lv_obj_t *np_card     = nullptr;
static lv_obj_t *np_art      = nullptr;
static lv_obj_t *np_title    = nullptr;
static lv_obj_t *np_artist   = nullptr;
static lv_img_dsc_t np_art_dsc = {};
static bool      np_active   = false;

// --- Helpers ---------------------------------------------------------------

static void style_root() {
    lv_obj_set_style_bg_color(root, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(root, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_clear_flag(root, LV_OBJ_FLAG_SCROLLABLE);
}

static lv_obj_t *make_overlay() {
    // Full-screen container above the base scene. We toggle its
    // hidden flag rather than recreating it — LVGL is happy to render
    // hidden objects cheaply.
    lv_obj_t *o = lv_obj_create(root);
    lv_obj_set_size(o, 240, 240);
    lv_obj_align(o, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_color(o, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(o, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_width(o, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(o, 0, LV_PART_MAIN);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
    return o;
}

// --- Splash ----------------------------------------------------------------

static void build_splash() {
    splash = lv_obj_create(root);
    lv_obj_set_size(splash, 240, 240);
    lv_obj_align(splash, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_color(splash, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(splash, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(splash, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(splash, 0, LV_PART_MAIN);
    lv_obj_clear_flag(splash, LV_OBJ_FLAG_SCROLLABLE);

    splash_lbl = lv_label_create(splash);
    lv_label_set_text(splash_lbl, "JASPER");
    lv_obj_set_style_text_color(splash_lbl, COL_ACCENT, 0);
    lv_obj_set_style_text_font(splash_lbl, &lv_font_montserrat_36, 0);
    lv_obj_align(splash_lbl, LV_ALIGN_CENTER, 0, -10);

    status_lbl = lv_label_create(splash);
    lv_label_set_text(status_lbl, "");
    lv_obj_set_style_text_color(status_lbl, COL_TEXT_DIM, 0);
    lv_obj_set_style_text_font(status_lbl, &lv_font_montserrat_14, 0);
    lv_obj_align(status_lbl, LV_ALIGN_CENTER, 0, 35);
}

// --- Clock face ------------------------------------------------------------

static void build_clock() {
    clock_face = lv_obj_create(root);
    lv_obj_set_size(clock_face, 240, 240);
    lv_obj_align(clock_face, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_color(clock_face, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(clock_face, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(clock_face, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(clock_face, 0, LV_PART_MAIN);
    lv_obj_clear_flag(clock_face, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(clock_face, LV_OBJ_FLAG_HIDDEN);

    clock_time = lv_label_create(clock_face);
    lv_label_set_text(clock_time, "--:--");
    lv_obj_set_style_text_color(clock_time, COL_TEXT, 0);
    lv_obj_set_style_text_font(clock_time, &lv_font_montserrat_48, 0);
    lv_obj_align(clock_time, LV_ALIGN_CENTER, 0, 0);
}

static void update_clock() {
    time_t now = time(nullptr);
    if (now < 8 * 3600 * 365) {
        // Time hasn't synced via SNTP yet (epoch < 1970+early).
        return;
    }
    struct tm lt;
    localtime_r(&now, &lt);
    char buf[8];
    strftime(buf, sizeof(buf), "%H:%M", &lt);
    lv_label_set_text(clock_time, buf);
}

// --- Volume overlay --------------------------------------------------------
//
// JTS-owned procedural rendering: an LVGL arc, labels, and simple
// geometric rings. No third-party image assets are needed for the
// transient volume scene.

static void build_volume() {
    vol_overlay = make_overlay();
    lv_obj_set_style_bg_opa(vol_overlay, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(vol_overlay, COL_BG, LV_PART_MAIN);

    lv_obj_t *outer = lv_obj_create(vol_overlay);
    lv_obj_set_size(outer, 214, 214);
    lv_obj_align(outer, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_radius(outer, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(outer, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_width(outer, 2, LV_PART_MAIN);
    lv_obj_set_style_border_color(outer, COL_BG_SOFT, LV_PART_MAIN);
    lv_obj_set_style_border_opa(outer, LV_OPA_80, LV_PART_MAIN);
    lv_obj_clear_flag(outer, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *inner = lv_obj_create(vol_overlay);
    lv_obj_set_size(inner, 142, 142);
    lv_obj_align(inner, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_radius(inner, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_bg_color(inner, COL_BG_SOFT, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(inner, LV_OPA_40, LV_PART_MAIN);
    lv_obj_set_style_border_width(inner, 0, LV_PART_MAIN);
    lv_obj_clear_flag(inner, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *label_volume = lv_label_create(vol_overlay);
    lv_label_set_text(label_volume, "VOLUME");
    lv_obj_set_style_text_color(label_volume, COL_TEXT_DIM, 0);
    lv_obj_set_style_text_font(label_volume, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_letter_space(label_volume, 2, 0);
    lv_obj_align(label_volume, LV_ALIGN_CENTER, 0, 48);

    lv_obj_t *min_label = lv_label_create(vol_overlay);
    lv_label_set_text(min_label, "0");
    lv_obj_set_style_text_color(min_label, COL_TEXT_DIM, 0);
    lv_obj_set_style_text_font(min_label, &lv_font_montserrat_14, 0);
    lv_obj_align(min_label, LV_ALIGN_CENTER, -64, 78);

    lv_obj_t *max_label = lv_label_create(vol_overlay);
    lv_label_set_text(max_label, "100");
    lv_obj_set_style_text_color(max_label, COL_TEXT_DIM, 0);
    lv_obj_set_style_text_font(max_label, &lv_font_montserrat_14, 0);
    lv_obj_align(max_label, LV_ALIGN_CENTER, 63, 78);

    vol_label = lv_label_create(vol_overlay);
    lv_label_set_text(vol_label, "50%");
    lv_obj_set_style_text_color(vol_label, COL_LISTEN, 0);
    lv_obj_set_style_text_font(vol_label, &lv_font_montserrat_40, 0);
    lv_obj_set_width(vol_label, 132);
    lv_obj_set_style_text_align(vol_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(vol_label, LV_ALIGN_CENTER, 0, -4);

    vol_arc = lv_arc_create(vol_overlay);
    lv_obj_set_size(vol_arc, 188, 188);
    lv_obj_align(vol_arc, LV_ALIGN_CENTER, 0, 0);
    lv_arc_set_rotation(vol_arc, 135);
    lv_arc_set_bg_angles(vol_arc, 0, 270);
    lv_arc_set_range(vol_arc, 0, 100);
    lv_arc_set_value(vol_arc, 50);
    lv_obj_remove_style(vol_arc, NULL, LV_PART_KNOB);
    lv_obj_clear_flag(vol_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_width(vol_arc, 18, LV_PART_MAIN);
    lv_obj_set_style_arc_color(vol_arc, COL_BG_SOFT, LV_PART_MAIN);
    lv_obj_set_style_arc_opa(vol_arc, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(vol_arc, true, LV_PART_MAIN);
    lv_obj_set_style_arc_width(vol_arc, 18, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(vol_arc, COL_LISTEN, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(vol_arc, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(vol_arc, true, LV_PART_INDICATOR);
}

// --- Listening orb ---------------------------------------------------------

static void build_orb() {
    orb_overlay = make_overlay();
    lv_obj_set_style_bg_color(orb_overlay, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(orb_overlay, LV_OPA_70, LV_PART_MAIN);

    orb = lv_obj_create(orb_overlay);
    lv_obj_set_size(orb, 80, 80);
    lv_obj_align(orb, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_radius(orb, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_bg_color(orb, COL_LISTEN, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(orb, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(orb, 0, LV_PART_MAIN);
    lv_obj_clear_flag(orb, LV_OBJ_FLAG_SCROLLABLE);
}

static void animate_orb() {
    // Sine-wave breathing: 80px → 110px and back, ~1.2s period.
    if (!orb_active) return;
    uint32_t t = lv_tick_get();
    float phase = (float)(t % 1200) / 1200.0f * 6.2831f;
    int size = 80 + (int)(15.0f + 15.0f * sinf(phase));
    lv_obj_set_size(orb, size, size);
    lv_obj_align(orb, LV_ALIGN_CENTER, 0, 0);
}

// --- Now-playing card ------------------------------------------------------

static void build_now_playing() {
    np_card = lv_obj_create(root);
    lv_obj_set_size(np_card, 240, 240);
    lv_obj_align(np_card, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_color(np_card, COL_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(np_card, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(np_card, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(np_card, 0, LV_PART_MAIN);
    lv_obj_clear_flag(np_card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(np_card, LV_OBJ_FLAG_HIDDEN);

    np_art = lv_img_create(np_card);
    lv_obj_align(np_art, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_size(np_art, 240, 120);
    // Image source set in scenes_set_now_playing.

    np_title = lv_label_create(np_card);
    lv_label_set_text(np_title, "");
    lv_obj_set_style_text_color(np_title, COL_TEXT, 0);
    lv_obj_set_style_text_font(np_title, &lv_font_montserrat_18, 0);
    lv_label_set_long_mode(np_title, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_width(np_title, 220);
    lv_obj_set_style_text_align(np_title, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(np_title, LV_ALIGN_TOP_MID, 0, 135);

    np_artist = lv_label_create(np_card);
    lv_label_set_text(np_artist, "");
    lv_obj_set_style_text_color(np_artist, COL_TEXT_DIM, 0);
    lv_obj_set_style_text_font(np_artist, &lv_font_montserrat_14, 0);
    lv_label_set_long_mode(np_artist, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_width(np_artist, 220);
    lv_obj_set_style_text_align(np_artist, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(np_artist, LV_ALIGN_TOP_MID, 0, 165);
}

// --- Public API ------------------------------------------------------------

static SemaphoreHandle_t lvgl_mutex = nullptr;

// Indefinite lock — used only by the LVGL pump task in display.cpp,
// which is the canonical owner of the mutex. If pumping deadlocks,
// the pump dies, not main.
void scenes_lock() {
    if (lvgl_mutex) xSemaphoreTake(lvgl_mutex, portMAX_DELAY);
}

void scenes_unlock() {
    if (lvgl_mutex) xSemaphoreGive(lvgl_mutex);
}

// Best-effort lock for main-thread mutators. Bounded wait so a
// wedged LVGL task can't stall the encoder/button handlers — UI
// updates may be silently dropped, but the dial stays responsive.
namespace { struct LvglTryGuard {
    bool held;
    LvglTryGuard() : held(false) {
        if (lvgl_mutex) {
            held = (xSemaphoreTake(lvgl_mutex, pdMS_TO_TICKS(50)) == pdTRUE);
        }
    }
    ~LvglTryGuard() {
        if (held) xSemaphoreGive(lvgl_mutex);
    }
}; }

void scenes_init() {
    // Mutex created here, BEFORE the LVGL pump task is spawned.
    // We don't take it: nothing else has a reference yet, so building
    // the scene graph is race-free. (Using LvglTryGuard with its
    // 50 ms timeout would risk silently aborting the build if some
    // future change calls scenes_init when contended.)
    lvgl_mutex = xSemaphoreCreateMutex();
    root = lv_scr_act();
    style_root();
    build_splash();
    build_clock();
    build_now_playing();
    build_volume();
    build_orb();
}

void scenes_show_volume(int percent) {
    LvglTryGuard g; if (!g.held) return;
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    lv_arc_set_value(vol_arc, percent);
    char buf[8];
    snprintf(buf, sizeof(buf), "%d%%", percent);
    lv_label_set_text(vol_label, buf);
    lv_obj_clear_flag(vol_overlay, LV_OBJ_FLAG_HIDDEN);
    vol_until_ms = lv_tick_get() + 1800;
}

void scenes_set_listening(bool listening) {
    LvglTryGuard g; if (!g.held) return;
    orb_active = listening;
    if (listening) {
        lv_obj_clear_flag(orb_overlay, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(orb_overlay, LV_OBJ_FLAG_HIDDEN);
    }
}

void scenes_set_now_playing(const char *title, const char *artist,
                            const uint16_t *art_buf) {
    LvglTryGuard g; if (!g.held) return;
    bool has_track = (title && *title) || (artist && *artist);
    if (!has_track) {
        np_active = false;
        lv_obj_add_flag(np_card, LV_OBJ_FLAG_HIDDEN);
        // Reveal the clock if nothing else is showing.
        lv_obj_clear_flag(clock_face, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(splash, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    np_active = true;
    lv_label_set_text(np_title, title ? title : "");
    lv_label_set_text(np_artist, artist ? artist : "");
    if (art_buf) {
        np_art_dsc.header.always_zero = 0;
        np_art_dsc.header.cf = LV_IMG_CF_TRUE_COLOR;
        np_art_dsc.header.w = 240;
        np_art_dsc.header.h = 120;
        np_art_dsc.data_size = 240 * 120 * sizeof(uint16_t);
        np_art_dsc.data = (const uint8_t *)art_buf;
        lv_img_set_src(np_art, &np_art_dsc);
    }
    lv_obj_clear_flag(np_card, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(clock_face, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(splash, LV_OBJ_FLAG_HIDDEN);
}

void scenes_set_status(const char *msg) {
    LvglTryGuard g; if (!g.held) return;
    if (status_lbl) lv_label_set_text(status_lbl, msg ? msg : "");
}

void scenes_tick() {
    // Hide volume overlay after the auto-revert deadline.
    if (vol_until_ms != 0 && lv_tick_get() >= vol_until_ms) {
        lv_obj_add_flag(vol_overlay, LV_OBJ_FLAG_HIDDEN);
        vol_until_ms = 0;
    }
    // Update clock (only visible if neither now-playing nor any
    // overlay is on top).
    static uint32_t last_clock_tick = 0;
    uint32_t now = lv_tick_get();
    if (now - last_clock_tick > 500) {
        last_clock_tick = now;
        update_clock();
        // Auto-promote splash → clock once we have a synced time.
        time_t epoch = time(nullptr);
        if (epoch > 8 * 3600 * 365 && !np_active) {
            lv_obj_add_flag(splash, LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(clock_face, LV_OBJ_FLAG_HIDDEN);
        }
    }
    // Animate the orb.
    if (orb_active) animate_orb();
}
