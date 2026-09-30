#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    int64_t tokens_used;     // tokens used this billing period, from local transcripts (Enterprise); <0 = daemon didn't send "tok".
                              // int64_t, not long: cache-read tokens make heavy-use monthly totals exceed 2^31 (observed ~1.7B in testing).
    float cost_usd;          // $ spent this billing period, as reported by Anthropic's OAuth usage endpoint (Enterprise); <0 = daemon didn't send "cost"
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    bool has_usage;          // payload carried usage fields ("s" present); false for event-only payloads
    char event[8];           // Claude Code event from the daemon's hooks: "done" | "needs" | "clear" | "" (none)
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};
