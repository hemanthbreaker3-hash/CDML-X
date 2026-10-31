\
#!/usr/bin/env bash
# Codespaces/container launcher: deliberately does not use systemd or apt.
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
fail(){ echo "ERROR: $*" >&2; exit 1; }

for key in BOT_TOKEN OWNER_ID TELEGRAM_API TELEGRAM_HASH DATABASE_URL CMD_SUFFIX; do
  if [[ -v "$key" ]]; then export "$key"; fi
done
[[ -n "${BOT_TOKEN:-}" ]] || fail "Export BOT_TOKEN first."
[[ "$BOT_TOKEN" == *:* ]] || fail "BOT_TOKEN format is invalid."
for key in OWNER_ID TELEGRAM_API TELEGRAM_HASH; do
  [[ -n "${!key:-}" ]] || fail "Export $key first."
done

command -v python3 >/dev/null 2>&1 || fail "Python 3 is required in this Codespace."
command -v deno >/dev/null 2>&1 || fail "Deno is required. Install Deno 2.x in the Codespace, then retry."
mkdir -p logs downloads thumbnails tokens rclone cookies Images configs/sabnzbd
[[ -f requirements.txt ]] || fail "requirements.txt is missing."
[[ -x venv/bin/python ]] || python3 -m venv venv
venv/bin/python -m pip install --upgrade pip setuptools wheel
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip install --upgrade "wzgram[fast]" imdbinfo pycountry aiofiles pyyaml
venv/bin/python -m pip install --upgrade --pre 'yt-dlp[default,curl-cffi]' yt-dlp-ejs
venv/bin/python -m py_compile config.py

POT_DIR="${POT_DIR:-$APP_DIR/.cache/bgutil-ytdlp-pot-provider}"
if [[ ! -d "$POT_DIR/.git" ]]; then
  git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_DIR"
fi
(cd "$POT_DIR/server" && deno install --allow-scripts=npm:canvas --frozen)
venv/bin/python -m pip install --upgrade 'bgutil-ytdlp-pot-provider==2.0.2'

# Codespaces is not a systemd host. Run the local provider in the background.
if ! ss -ltnH 'sport = :4416' 2>/dev/null | grep -q .; then
  (cd "$POT_DIR/server/node_modules" && nohup deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts >>"$APP_DIR/logs/po-token-provider.log" 2>&1 </dev/null &)
fi
ready=0
for _ in $(seq 1 30); do
  if ss -ltnH 'sport = :4416' 2>/dev/null | grep -q .; then ready=1; break; fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  tail -80 "$APP_DIR/logs/po-token-provider.log" 2>/dev/null || true
  fail "PO-token provider did not listen on localhost:4416; see logs/po-token-provider.log"
fi

# Persist supplied environment locally with restrictive permissions for start.sh.
python3 - <<'PY'
import os
from pathlib import Path
keys=("BOT_TOKEN","OWNER_ID","TELEGRAM_API","TELEGRAM_HASH","DATABASE_URL","CMD_SUFFIX")
def q(v): return "'" + v.replace("'", "'\\''").replace("\n","") + "'"
Path(".codespaces.env").write_text("\n".join(f"export {k}={q(os.environ.get(k,''))}" for k in keys)+"\n")
os.chmod(".codespaces.env",0o600)
PY
pkill -f "$APP_DIR/venv/bin/python.*-m bot" 2>/dev/null || true
( set -a; source .codespaces.env; set +a; nohup ./start.sh >>"$APP_DIR/logs/bot.log" 2>&1 </dev/null & )
sleep 8
if ! pgrep -af "$APP_DIR/venv/bin/python.*-m bot" >/dev/null 2>&1; then
  tail -100 "$APP_DIR/logs/bot.log" 2>/dev/null || true
  fail "Bot did not stay running; inspect logs/bot.log."
fi
echo "Codespaces launch complete. Provider: logs/po-token-provider.log; bot: logs/bot.log"
echo "Codespaces is not 24/7 hosting; processes can stop when the Codespace sleeps/stops."
