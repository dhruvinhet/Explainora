#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
: "${IMAGE:?IMAGE must be an immutable GHCR SHA tag}"
: "${APP_VERSION:?APP_VERSION must be the commit SHA}"
: "${APP_DOMAIN:?APP_DOMAIN must be the public hostname}"

env_file="env/production.env"
test -s "${env_file}" || { echo "Missing ${env_file}" >&2; exit 1; }

previous_image=""
if [[ -f .last-image ]]; then
  previous_image="$(<.last-image)"
fi
if [[ -f .deployment.env ]]; then
  cp .deployment.env .previous-deployment.env
  chmod 600 .previous-deployment.env
fi

printf 'IMAGE=%q\nAPP_VERSION=%q\nAPP_DOMAIN=%q\n' \
  "${IMAGE}" "${APP_VERSION}" "${APP_DOMAIN}" > .deployment.env
chmod 600 .deployment.env

export IMAGE APP_VERSION APP_DOMAIN
docker compose --env-file "${env_file}" -f compose.yaml pull
docker compose --env-file "${env_file}" -f compose.yaml up -d --remove-orphans

web_id="$(docker compose --env-file "${env_file}" -f compose.yaml ps -q web)"
for attempt in $(seq 1 60); do
  if docker exec "${web_id}" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" >/dev/null 2>&1; then
    break
  fi
  if [[ "${attempt}" -eq 60 ]]; then
    docker compose --env-file "${env_file}" -f compose.yaml logs --tail 150 web
    if [[ -n "${previous_image}" ]]; then
      echo "New web image failed liveness; rolling back to ${previous_image}." >&2
      IMAGE="${previous_image}" docker compose --env-file "${env_file}" -f compose.yaml up -d --no-deps web worker
      if [[ -f .previous-deployment.env ]]; then
        cp .previous-deployment.env .deployment.env
        chmod 600 .deployment.env
      fi
    fi
    exit 1
  fi
  sleep 2
done

# A worker is not operational merely because its Python process is alive. Its
# health file is now refreshed only after a successful durable-queue heartbeat.
worker_id="$(docker compose --env-file "${env_file}" -f compose.yaml ps -q worker)"
for attempt in $(seq 1 60); do
  worker_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${worker_id}" 2>/dev/null || true)"
  if [[ "${worker_health}" == "healthy" ]]; then
    break
  fi
  if [[ "${attempt}" -eq 60 ]]; then
    docker compose --env-file "${env_file}" -f compose.yaml logs --tail 150 web worker
    if [[ -n "${previous_image}" ]]; then
      echo "New worker failed queue readiness; rolling back to ${previous_image}." >&2
      IMAGE="${previous_image}" docker compose --env-file "${env_file}" -f compose.yaml up -d --no-deps web worker
      if [[ -f .previous-deployment.env ]]; then
        cp .previous-deployment.env .deployment.env
        chmod 600 .deployment.env
      fi
    fi
    exit 1
  fi
  sleep 2
done

./maintenance.sh off
web_id="$(docker compose --env-file "${env_file}" -f compose.yaml ps -q web)"
for attempt in $(seq 1 60); do
  if docker exec "${web_id}" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/readyz', timeout=5)" >/dev/null 2>&1; then
    printf '%s' "${IMAGE}" > .last-image
    docker image prune -f --filter 'until=168h' >/dev/null
    echo "Deployment ${APP_VERSION} is ready."
    exit 0
  fi
  sleep 2
done

docker compose --env-file "${env_file}" -f compose.yaml logs --tail 150 web worker
./maintenance.sh on
echo "Deployment failed readiness and was left in maintenance mode." >&2
exit 1
