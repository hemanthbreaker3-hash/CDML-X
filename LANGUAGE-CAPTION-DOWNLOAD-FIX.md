Caption language and download notes
===================================
- `{languages}` resolves detected audio languages to display names.
- `{languagesr}` resolves detected audio languages to native/autonym names.
- ISO-639-2/3 tags from FFprobe now get a pycountry fallback before language formatting.
- Empty values fall back to the other resolved language value, avoiding blank placeholders when detection yields only one representation.
- Telegram FloodWait is still honored; skipping server-mandated waits would cause more failures. Download throughput remains dependent on Telegram/DC limits, source, and network.
