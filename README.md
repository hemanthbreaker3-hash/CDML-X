# CDML-X

CDML-X is a Telegram mirror/leech bot for Linux VPS deployments. It supports download, media processing, Telegram uploads, user settings, verification, and video/audio track tools.

> **Deployment note:** A successful deployment depends on valid Telegram credentials, a reachable database, supported system packages, and a correctly configured VPS. Review the configuration and deployment logs rather than assuming that a build is guaranteed to succeed on every host.

## Features

- Telegram mirror and leech workflows with task status and upload handling.
- Video processing through `-vt`, including video merge, audio split, subtitle sync, compression, conversion, watermarking, trimming, extraction, and binary media splitting.
- Audio/subtitle track management through `-tm`.
- Per-user settings and persistent configuration when a database is configured.
- Verification controls and configurable verification shorteners.
- Command suffix support through `CMD_SUFFIX`.

## Audio Split

Audio Split is opt-in. When it is disabled, incoming tasks keep their original audio streams and are processed normally.

Enable **Audio Split on Incoming Tasks** in User Settings → Video Processing Tools to apply it after downloads complete. All supported download sources converge on the same `TaskListener.on_download_complete` pipeline, including Aria2/direct HTTP, qBittorrent, NZB, JDownloader, Mega, Google Drive, Rclone, yt-dlp, and Telegram downloads. Folder and archive inputs are prepared before splitting. Choose the mode there:

- **Auto:** detects every audio stream and creates one output video for each stream. Each output uses the original video stream and exactly one audio stream.
- **Manual:** selecting Manual reveals the **Set Languages** button. Enter your own comma-separated language codes or names (for example `TEL,TAM,HIN`). No language list is preconfigured. The saved list is retained when switching to Auto and restored when switching back to Manual. Each matching track gets its own output video. All non-matching audio tracks are preserved together in one additional output video. Send `clear` to remove the saved list; if no languages are configured, the original input is kept unchanged.
- **Auto UI:** selecting Auto hides the language-configuration button and splits every detected audio track. No language configuration is requested or used in Auto mode.
- If none of the configured languages is found, the original input is kept unchanged.
- Missing configured languages are skipped without failing the task.
- Stream copying (`-c copy`) is used; video and audio are not re-encoded, preserving source quality and synchronization.

The same Audio Split processing engine and saved Auto/Manual configuration are used by the `-vt` Video Tools workflow. An explicit `-vt` operation takes precedence over the automatic incoming-task setting; select Audio Split in `-vt` to run it directly. For manual matching, language metadata should be present on the audio streams; untagged streams cannot be reliably matched to a language. Common ISO codes, three-letter codes, and language names are normalized (including regional tags such as `eng-US`). During processing, the task status displays **🔊 Audio splitting...**. If splitting cannot find a usable video/audio stream, the original input is retained rather than producing an empty result.

## Automatic Media Thumbnails

Automatic thumbnails extract the series/movie title from the media filename before searching artwork catalogues. Episode markers (`S01E03`, `S1 E3`, `Episode 3`), empty or populated bracket groups, audio-language lists (for example `Tam + Tel + Hin + Eng + Jap`), resolution, HDR, codecs, release tags, and subtitle labels are excluded from the catalogue search title. Unicode titles are preserved, and IMDb lookup retries a matching locale for common non-Latin scripts.

- **Video/media upload:** prefers an IMDb landscape gallery image/backdrop. It does not substitute a portrait poster as a video backdrop; if no suitable backdrop is available, the uploader can use its local video-frame fallback.
- **Document upload:** prefers portrait IMDb cover/poster artwork.
- **Matching safety:** IMDb, TMDb, Jikan, AniList, and TVmaze results are checked against the extracted title to avoid attaching artwork for an unrelated title.
- **Language-independent title matching:** audio and subtitle language tags are treated as release metadata, not part of the title.

Example filename:

```text
[] The Laid-Off Cheat-Granting Mage Enjoys a Second Lease on Life - S01E03 - [Tam + Tel + Hin + Eng + Jap] - 480p HDRip x265 - Multi-Subs.mkv
```

The extracted search title is `The Laid-Off Cheat-Granting Mage Enjoys a Second Lease on Life`.

## Video Tools (`-vt`)

The Video Tools workflow supports the operations exposed by the interactive menu, including:

- Video + Video
- Audio Split
- SubSync
- Compress and Convert
- Watermark
- Extract and Trim
- Remove Ads & Names
- Audio Sync
- Media Split → Binary
- MULTI VT where supported

Some resource-intensive tools may be restricted to authorized or sudo users. The bot enforces its configured permission checks.

## Audio & Subtitle Track Manager (`-tm`)

Example:

```text
/mirror https://example.com/video.mkv -tm
```

The track manager supports selecting and removing audio/subtitle streams, reordering tracks, editing language/title metadata, setting default tracks, marking forced subtitles, and applying compatible choices across multiple files. Track changes are remuxed without re-encoding.

## Verification

The verification commands include:

```text
/verify
/verify on 7d
/verify off
/verify time 7d
/verify time never
/verify status
/verify user USER_ID 7d
/verify user USER_ID revoke
```

Use `/shorteners` as an authorized administrator to manage verification shorteners. Configure providers using their documented API formats.

## Configuration

Set the required values in `config.py` or through environment variables. Common deployment variables include:

```bash
export BOT_TOKEN='YOUR_TELEGRAM_BOT_TOKEN'
export TELEGRAM_API='YOUR_TELEGRAM_API_ID'
export TELEGRAM_HASH='YOUR_TELEGRAM_API_HASH'
export OWNER_ID='YOUR_TELEGRAM_USER_ID'
export DATABASE_URL='YOUR_MONGODB_URI'
export CMD_SUFFIX='a'
```

Do not commit real bot tokens, API hashes, database credentials, or other secrets to GitHub. Keep them in the deployment environment or a private `.env` file excluded from version control.

Optional start-menu links/buttons may be configured with `START_REPO_URL`, `START_REPO_BUTTON`, `START_UPDATE_URL`, `START_UPDATE_BUTTON`, `START_DEV_URL`, and `START_DEV_BUTTON`.

## Deploy on Ubuntu/Debian VPS

### 1. Get the source

```bash
git clone https://github.com/hemanthbreaker3-hash/CDML-X.git
cd CDML-X
```

If deploying from an archive, extract it and run the following commands from the project directory.

### 2. Configure credentials

Set the environment variables above, or configure the equivalent values in `config.py`. Confirm that the database URI is reachable from the VPS.

### 3. Install and start

```bash
chmod +x one-click-deploy.sh deploy.vps deploy-user.vps start.sh
./one-click-deploy.sh
```

Run as root for system package installation and systemd integration. User-local deployment may not be able to install system packages or create system services.

## Operations

### systemd

```bash
systemctl status cantarellabots_bot --no-pager -l
journalctl -u cantarellabots_bot -f
```

### User-local process

```bash
./run-user.sh
tail -f logs/user-bot.log
kill "$(cat .cdmlx-user.pid)"
```

## Troubleshooting

- **Bot does not start:** check `config.py`, required environment variables, and the first Python traceback in the service log.
- **Syntax error:** run `python3 -m compileall -q bot` from the project directory.
- **Database connection failure:** verify the database URI, network access, credentials, and database availability.
- **FFmpeg operation fails:** confirm `ffmpeg` and `ffprobe` are installed and available in `PATH`.
- **Audio Split does not match a language:** inspect the source with `ffprobe` and verify that each desired audio stream has a language tag. Common codes include `tel`, `tam`, `hin`, `eng`, `kan`, and `mal`.
- **Service repeatedly restarts:** inspect `journalctl -u cantarellabots_bot -n 200 --no-pager` and fix the earliest underlying error before restarting.

## Developer checks

Run from the repository root:

```bash
python3 -m compileall -q bot
```

Review the deployment script output and service logs after every update. Test each download source available in the deployment (Aria2/direct HTTP, qBittorrent, NZB, JDownloader, Mega, Google Drive, Rclone, yt-dlp, and Telegram), normal mirror/leech tasks, `-vt`, Audio Split Auto/Manual, archive/folder inputs, and `-tm` with representative media before production use.
