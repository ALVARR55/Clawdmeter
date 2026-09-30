#pragma once
#include <stdint.h>

enum ble_state_t {
    BLE_STATE_INIT,
    BLE_STATE_ADVERTISING,
    BLE_STATE_CONNECTED,
    BLE_STATE_DISCONNECTED,
};

void ble_init(void);
void ble_tick(void);
ble_state_t ble_get_state(void);
const char* ble_get_device_name(void);
// Owner-chosen suffix stored in NVS: the board advertises as
// "Clawdmeter-<suffix>" (letters, digits, '-' and '_', at most
// BLE_NAME_SUFFIX_MAX chars so the name still fits the 31-byte advertising
// packet next to the HID service UUID macOS needs to list the device). An
// empty suffix restores the plain "Clawdmeter". Returns false if the suffix
// was rejected. Takes effect immediately (advertising restarts); the bonded
// Mac may keep its cached name until it re-pairs — it can rename locally anyway.
#define BLE_NAME_SUFFIX_MAX 7
bool ble_set_name_suffix(const char* suffix);
const char* ble_get_mac_address(void);
void ble_clear_bonds(void);
bool ble_has_bonds(void);
bool ble_has_data(void);
const char* ble_get_data(void);
void ble_send_ack(void);
void ble_send_nack(void);
void ble_request_refresh(void);

void ble_set_battery_level(int pct);

// BLE HID keyboard
void ble_keyboard_press(uint8_t key, uint8_t modifier);
void ble_keyboard_release(void);
