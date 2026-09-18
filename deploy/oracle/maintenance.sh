#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:-}"
case "${mode}" in
  on) value=1 ;;
  off) value=0 ;;
  *) echo "Usage: $0 on|off" >&2; exit 2 ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")"
env_file="env/production.env"
metadata_file=".deployment.env"
if [[ ! -f "${env_file}" ]]; then
  echo "Missing ${env_file}" >&2
  exit 1
fi
if [[ ! -f "${metadata_file}" ]]; then
  echo "No deployed release metadata exists yet; nothing to restart."
  exit 0
fi
# deploy.sh shell-escapes these values before writing the local metadata file.
# shellcheck disable=SC1090
source "${metadata_file}"
export IMAGE APP_VERSION APP_DOMAIN

temporary="$(mktemp)"
awk -v value="${value}" '
  BEGIN { found = 0 }
  /^MAINTENANCE_MODE=/ { print "MAINTENANCE_MODE=" value; found = 1; next }
  { print }
  END { if (!found) print "MAINTENANCE_MODE=" value }
' "${env_file}" > "${temporary}"
chmod 600 "${temporary}"
mv "${temporary}" "${env_file}"

if docker compose --env-file "${env_file}" -f compose.yaml ps --services --status running 2>/dev/null | grep -qx web; then
  docker compose --env-file "${env_file}" -f compose.yaml up -d --no-deps --force-recreate web
fi
echo "Maintenance mode ${mode}."
