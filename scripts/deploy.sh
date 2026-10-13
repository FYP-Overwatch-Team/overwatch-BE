#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <image-tag> <health-url>" >&2
  exit 2
fi

IMAGE_TAG=$1
HEALTH_URL=$2
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RELEASE_ENV="$APP_DIR/.release.env"
PREVIOUS_TAG=""

if [ -f "$RELEASE_ENV" ]; then
  PREVIOUS_TAG=$(sed -n 's/^IMAGE_TAG=//p' "$RELEASE_ENV" | head -n 1)
fi

write_release() {
  printf 'IMAGE_TAG=%s\n' "$1" > "$RELEASE_ENV"
  chmod 600 "$RELEASE_ENV"
}

compose() {
  docker compose \
    --env-file "$APP_DIR/.env" \
    --env-file "$RELEASE_ENV" \
    -f "$APP_DIR/compose.prod.yml" "$@"
}

rollback() {
  echo "deploy: health check failed; rolling back" >&2
  if [ -n "$PREVIOUS_TAG" ]; then
    write_release "$PREVIOUS_TAG"
    compose pull api || true
    compose up -d --remove-orphans || true
  else
    compose stop api || true
  fi
}

write_release "$IMAGE_TAG"
if ! compose pull; then
  rollback
  exit 1
fi

if ! compose up -d --remove-orphans; then
  rollback
  exit 1
fi

attempt=1
while [ "$attempt" -le 24 ]; do
  if curl --fail --silent --show-error "$HEALTH_URL" >/dev/null; then
    echo "deploy: $IMAGE_TAG is healthy"
    docker image prune -f >/dev/null
    exit 0
  fi
  echo "deploy: waiting for health check ($attempt/24)"
  attempt=$((attempt + 1))
  sleep 5
done

rollback
exit 1
