# CDML-X update — 2026-10-30

## Changes
- `CMD_SUFFIX` is now enforced as a strict command namespace: command handlers and Telegram command registration use suffixed command names only. The only unsuffixed command names are `/start`, `/cancelall`, and `/statusall`. Other aliases are suffixed too.
- The `/start verify` already-verified response now calculates the remaining verification time from the stored verification timestamp and configured validity. Unlimited access is reported as unlimited.
- Dynamic task cancel/select command patterns now require the configured suffix instead of accepting both suffixed and unsuffixed forms.

## Telegram process engines
This source tree currently imports `wzgram.Client` directly in `bot/core/tg_client.py` and relies throughout on the Pyrogram-compatible API. Gogogram and TDLib do not have drop-in adapters in this codebase. A `/lib` switch that merely saves a button choice would not change the actual transport and could make the bot fail on restart, so no fake engine switch is included. Real WzGram/Pyrofork/Gogogram/TDLib selection needs separate client adapters, dependency/version validation, and lifecycle tests before it can safely be exposed.

## Restart
The existing restart flow was retained rather than replaced with an untested process launcher. The project compiles successfully after these changes; deployment-specific restart behavior still depends on the process supervisor and environment.
