/*
 * SPDX-FileCopyrightText: 2026 Jasper Curry
 *
 * SPDX-License-Identifier: Apache-2.0
 */

// Screen / scene graph for the dial.
//
// State model (single source of truth lives in scenes.cpp):
//
//   IDLE         → analog clock, dim
//   NOW_PLAYING  → album art + title + artist, dim
//   VOLUME       → big arc + percent, transient (auto-revert ~2 s
//                  after the last detent)
//   LISTENING    → soft pulsing orb (during hold-to-talk session)
//   SPEAKING     → slow waveform circle (Gemini producing TTS)
//
// main.cpp drives the state via the setters below; scenes.cpp owns
// the lv_obj_t graph and decides what to render. Concretely, the
// transient volume overlay just shadows whatever was on screen and
// fades out — it doesn't change the underlying scene.
#pragma once

#include <stdint.h>

void scenes_init();

// Tick once per loop iteration (after lv_timer_handler). Called by
// the LVGL task in display.cpp; not for direct use.
void scenes_tick();

// Mutex around all LVGL state. Held by the LVGL task during
// lv_timer_handler + scenes_tick; main-thread mutators (the
// scenes_set_* / scenes_show_* calls) acquire it internally.
// Exposed so display.cpp's task loop can take it before pumping.
void scenes_lock();
void scenes_unlock();

// Called by main.cpp on encoder turns. percent is 0..100 inclusive,
// clamped by caller. Triggers the transient volume overlay.
void scenes_show_volume(int percent);

// Called by main.cpp when long-press triggers / releases a session.
void scenes_set_listening(bool listening);

// Now-playing metadata. Pass empty title/artist to clear (= IDLE).
// art_buf may be nullptr; if non-null it must point to a 240×120
// RGB565 buffer (LVGL keeps a pointer; do not free until the next
// scenes_set_now_playing call).
void scenes_set_now_playing(const char *title, const char *artist,
                            const uint16_t *art_buf);

// Status text displayed during boot (over the splash). Cleared once
// scenes_show_idle() / scenes_set_now_playing() is called.
void scenes_set_status(const char *msg);
