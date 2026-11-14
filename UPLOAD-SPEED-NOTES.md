# Telegram upload performance notes

- Linux requirements use `wzgram[fast]>=3.1.5`; this is the latest version found during this update and requests the upstream fast extra (including uvloop where supported).
- Windows requirements retain `wzgram>=3.1.5` to avoid installing a Linux-only speed extra.
- The project already routes uploads through its HyperUP/MtprotoPool uploader and balances across configured helper clients. Configure multiple valid helper bot tokens for higher aggregate throughput.
- `wzgram` and the older `wzxpyro` fork are not installed together: both provide the Pyrogram-compatible import namespace, and mixing them can cause module conflicts. This project currently imports `wzgram.Client`, so it remains on wzgram.
- Actual throughput depends on VPS network, Telegram DC route, helper-account limits, file size, disk I/O, and FloodWaits. No library can guarantee a specific speed.


WzGram dependency policy: requirements allow releases from 3.1.5 onward, and deployment scripts run `pip install --upgrade wzgram[fast]` so each deployment installs the newest release available. The PyPI release listing checked on 2026-10-29 showed 3.1.5 as the newest published version; requiring >=3.1.6 today would make installation fail until that version is published.
