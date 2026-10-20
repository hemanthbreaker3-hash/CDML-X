# CDML-X — VPS Deployment

> A Telegram mirror/leech bot intended for a Linux VPS. The one-click installer configures system packages, a Python virtual environment, the bot service, and its local helper services.

## One-click deployment (VPS)

Supported on Ubuntu/Debian Linux. The installer supports two modes:

- **Root/sudo mode:** installs OS dependencies, configures the PO-token helper, and creates systemd services.
- **User-local mode:** when sudo is denied, installs Python packages into `./venv`, saves deployment settings in a private `.cdmlx-user.env`, and starts the bot as your current user. It cannot install OS packages, bind privileged ports, or create systemd services. Some bot features require administrator-installed tools such as FFmpeg, aria2, 7z, rclone, and MediaInfo.

### 1. Get the project

```bash
cd ~/
git clone https://github.com/hemanthbreaker3-hash/CDML-X.git CDML-X
cd CDML-X
```

If you already uploaded/extracted the ZIP, enter that project directory instead.

### 2. Configure credentials

Set these in your shell before running the installer, or configure them in `config.py`:

```bash
export BOT_TOKEN='REPLACE_WITH_BOT_TOKEN'
export TELEGRAM_API='REPLACE_WITH_TELEGRAM_API_ID'
export TELEGRAM_HASH='REPLACE_WITH_TELEGRAM_API_HASH'
export OWNER_ID='YOUR_TELEGRAM_USER_ID'
export DATABASE_URL='REPLACE_WITH_MONGODB_URI'
export CMD_SUFFIX='a'
```

Optional start-menu buttons:

```bash
export START_REPO_URL='https://t.me/your_repository_channel'
export START_REPO_BUTTON='Repository'
export START_UPDATE_URL='https://t.me/your_updates_channel'
export START_UPDATE_BUTTON='Updates'
export START_DEV_URL='https://t.me/your_developer_channel'
export START_DEV_BUTTON='Developer'
```

Environment variables are saved in `.cdmlx-user.env` in user-local mode with owner-only permissions. Do not share this file or commit it. If a bot token has been exposed, revoke and regenerate it.

### 3. Run deployment

```bash
chmod +x one-click-deploy.sh deploy.vps deploy-user.vps start.sh
./one-click-deploy.sh
```

If you have root access, run the installer from a root shell to enable system packages and systemd. If sudo is allowed, the script requests it. If sudo is denied (for example, `sudo: Sorry, user hemanth may not run sudo`), the script automatically attempts **user-local mode** instead; this does not bypass sudo restrictions.

### User-local process controls

```bash
tail -f logs/user-bot.log          # view logs
kill "$(cat .cdmlx-user.pid)"      # stop the current user-mode process
./run-user.sh                      # run in foreground for troubleshooting
```

User-local mode is not a systemd service and may stop after a VPS reboot or provider cleanup. To enable automatic boot startup or install missing system packages, ask the VPS administrator to configure those features.

### Root/sudo service controls

```bash
systemctl status cantarellabots_bot --no-pager -l
journalctl -u cantarellabots_bot -f
systemctl status bgutil-ytdlp-pot-provider --no-pager -l
journalctl -u bgutil-ytdlp-pot-provider -f
```

## Upload speed notes

> Telegram upload speed depends on VPS network throughput, route/peering, CPU, storage read speed, Telegram limits, and account/session restrictions. A 100 Mbps VPS link has a theoretical ceiling of about **12.5 MB/s** before overhead; this installer cannot guarantee that sustained rate. The client enables concurrent transmissions, and the uploader avoids an unnecessary fixed delay before flushing grouped media. Do not raise concurrency blindly: it can increase RAM usage and trigger Telegram flood limits.

## Troubleshooting

- Check `journalctl -u cantarellabots_bot -n 200 --no-pager` for startup errors.
- Confirm the VPS can resolve and reach GitHub, Telegram, and your database host.
- Ensure `BOT_TOKEN`, `TELEGRAM_API`, `TELEGRAM_HASH`, `OWNER_ID`, and `DATABASE_URL` are valid.
- If a port is already in use, identify the process before stopping anything.
- Re-run `./one-click-deploy.sh` from the project directory after correcting configuration.


### Command and Video Tools notes

- `/restart` opens a confirmation for a full bot-process restart. `/restart -s` requests a soft module reload. Both remain restricted to the owner/sudo list.
- `/taskuser` is available to regular users in private chats and groups to inspect their own queue and adjust their own task limit within the configured admin cap. Custom input must be sent in the same chat where the menu was opened.
- `/mirror -vt URL` (or the equivalent leech command with `-vt`) opens the interactive Video Tools selector. Archive inputs are extracted before applicable video/audio/subtitle processing. Keep FFmpeg and ffprobe installed and reachable in `PATH`; audio-to-subtitle additionally needs the declared Whisper/translation dependencies.
