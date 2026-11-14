#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy one isolated CDML-X instance using the credentials already in config.py.
# Usage: ./deploy-docker.sh [container-name]
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${1:-cdmlx-bot1}"
IMAGE="${CONTAINER}:local"

if [[ ! "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "ERROR: Invalid container name: $CONTAINER" >&2
  exit 2
fi

if [[ ! -f "$APP_DIR/config.py" ]]; then
  echo "ERROR: config.py is missing from $APP_DIR" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/Dockerfile" || ! -f "$APP_DIR/start.sh" ]]; then
  echo "ERROR: Dockerfile or start.sh is missing." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine and Docker Compose plugin, then rerun." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running or this user cannot access it." >&2
  exit 1
fi

# Keep the supplied config out of the image; mount it read-only at runtime.
chmod 600 "$APP_DIR/config.py"
mkdir -p "$APP_DIR/accounts" "$APP_DIR/downloads"

# Build a distinct image tag for this instance.
echo "Building $IMAGE ..."
docker build --pull -t "$IMAGE" "$APP_DIR"

# Replace only this named container. Other bots/containers are untouched.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "Starting $CONTAINER ..."
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --init \
  --label "app=cdml-x" \
  --mount "type=bind,src=$APP_DIR/config.py,dst=/usr/src/app/config.py,readonly" \
  --mount "type=bind,src=$APP_DIR/accounts,dst=/usr/src/app/accounts" \
  --mount "type=bind,src=$APP_DIR/downloads,dst=/usr/src/app/downloads" \
  "$IMAGE"

echo
echo "Deployment started: $CONTAINER"
echo "Logs:    docker logs -f $CONTAINER"
echo "Restart: docker restart $CONTAINER"
echo "Stop:    docker stop $CONTAINER"
