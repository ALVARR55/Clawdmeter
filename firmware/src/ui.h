#pragma once
#include "data.h"
#include "ble.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_update_battery(int percent, bool charging);

// Attention screens driven by Claude Code hooks (via the daemon's "ev" field):
// a held Clawd animation plus a banner, shown over whatever is on screen until
// the user taps the panel (or the daemon sends "clear" when a new prompt is
// submitted). A new event replaces the current one.
enum attention_t { ATTENTION_DONE, ATTENTION_NEEDS };
void ui_show_attention(attention_t kind);
void ui_clear_attention(void);
bool ui_attention_active(void);
