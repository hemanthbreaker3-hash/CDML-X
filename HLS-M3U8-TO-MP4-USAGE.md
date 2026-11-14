# HLS / M3U8 to MP4

Use `-hls` with `/leech`, `/mirror`, or `/uphoster`, replying to either an HLS/M3U8 URL or a supported player webpage URL.

The HLS path now supplies browser-like User-Agent, Referer, and Origin headers; retries playlist/fragment requests; downloads multiple HLS fragments concurrently through yt-dlp/FFmpeg; exposes available quality choices; and remuxes the selected stream to MP4 before continuing through the command's normal upload pipeline.

Example:

1. Send the player webpage or M3U8 URL.
2. Reply to it with `/leech -hls` (or `/mirror -hls` / `/uphoster -hls`).
3. Choose a quality when the extractor exposes multiple variants.

A page that only plays in a mobile browser may still refuse VPS downloads when its signed stream URL is expired, bound to the phone's IP, or protected by login/session cookies. In that case, use a fresh player URL and configure authorized cookies in the bot; do not share account passwords or session strings in chat. The code cannot bypass IP binding or access controls.
