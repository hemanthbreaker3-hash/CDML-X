#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${1:-}"
if [[ -z "$APP_NAME" ]]; then
  echo "Usage: bash deploy-heroku.sh YOUR_HEROKU_APP_NAME" >&2
  exit 2
fi

fail() { echo "ERROR: $*" >&2; exit 1; }
command -v heroku >/dev/null 2>&1 || fail "Heroku CLI is not installed. Install it and run 'heroku login'."
command -v docker >/dev/null 2>&1 || fail "Docker is required for this container deployment. Start Docker Desktop/Engine and retry."
heroku auth:whoami >/dev/null 2>&1 || fail "Not logged in to Heroku. Run 'heroku login'."
heroku apps:info -a "$APP_NAME" >/dev/null 2>&1 || fail "App '$APP_NAME' does not exist or your account cannot access it. Create/select the app first."
docker info >/dev/null 2>&1 || fail "Docker engine is not running. Start Docker and retry."

# Never print config-var values. They must be set in Dashboard > Settings > Config Vars.
missing=()
for key in BOT_TOKEN OWNER_ID TELEGRAM_API TELEGRAM_HASH; do
  value="$(heroku config:get "$key" -a "$APP_NAME" 2>/dev/null || true)"
  if [[ -z "$value" ]]; then missing+=("$key"); fi
done
if (( ${#missing[@]} )); then
  printf 'ERROR: Set these required Heroku Config Vars before deploying: %s\n' "${missing[*]}" >&2
  echo "Open Heroku Dashboard > your app > Settings > Config Vars. Do not paste secrets into public logs or source code." >&2
  exit 2
fi

if [[ -z "$(heroku config:get DATABASE_URL -a "$APP_NAME" 2>/dev/null || true)" ]]; then
  echo "WARNING: DATABASE_URL is not set. The bot may start, but persistent settings/history features will be limited." >&2
fi

# Heroku Container Registry only accepts x86_64 images.
export DOCKER_DEFAULT_PLATFORM=linux/amd64

echo "[1/6] Setting container stack..."
heroku stack:set container -a "$APP_NAME"
echo "[2/6] Logging into Heroku Container Registry..."
heroku container:login
echo "[3/6] Building and pushing worker image (linux/amd64)..."
heroku container:push worker -a "$APP_NAME"
echo "[4/6] Releasing the worker image..."
heroku container:release worker -a "$APP_NAME"
echo "[5/6] Selecting a non-sleeping Basic worker dyno..."
# Eco dynos can be forced to sleep when account-level Eco hours are exhausted.
# A Basic worker stays running when idle, but is paid. This cannot override
# account suspension, quota/billing enforcement, or Heroku's routine restarts.
heroku ps:type worker=basic -a "$APP_NAME"
# This is a worker-only Telegram bot. If an old deployment has a web dyno,
# remove it so the app does not accidentally retain a sleeping Eco web process.
if heroku ps -a "$APP_NAME" 2>/dev/null | grep -Eq '^=== web([[:space:]]|$)'; then
  echo "Scaling down the unused web process..."
  heroku ps:scale web=0 -a "$APP_NAME"
fi
echo "[6/6] Ensuring exactly one worker is running..."
heroku ps:scale worker=1 -a "$APP_NAME"
heroku ps -a "$APP_NAME"
echo "Deployment commands completed. Check boot/runtime logs next: heroku logs --tail -a $APP_NAME"
if [[ "${FOLLOW_LOGS:-0}" == "1" ]]; then
  heroku logs --tail -a "$APP_NAME"
fi
