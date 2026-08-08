#!/usr/bin/env bash
# Sync Apple Health data to Fly.io: preprocess.py -> upload Parquet via HTTPS -> reload cache.
# SSH/SFTP tunnels to Fly are blocked on this network, so /admin/upload and
# /admin/reload (both gated by API_KEY, see wrapper.py) are used instead.
set -euo pipefail

APP="fjcabello-apple-health-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "${SCRIPT_DIR}/.fly_secret_local" ]]; then
  source "${SCRIPT_DIR}/.fly_secret_local"
fi
if [[ -z "${API_KEY:-}" ]]; then
  echo "Error: API_KEY not set (expected in .fly_secret_local or the environment)" >&2
  exit 1
fi

BASE_URL="https://${APP}.fly.dev"

echo "==> Preprocessing latest Apple Health export"
(cd "${SCRIPT_DIR}" && python3 preprocess.py)

echo ""
echo "==> Uploading Parquet files to ${BASE_URL}"
cd "${SCRIPT_DIR}/data"
for f in *.parquet; do
  echo -n "  ${f}... "
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 60 -X PUT --data-binary "@${f}" "${BASE_URL}/admin/upload/${f}?api_key=${API_KEY}")
  if [[ "$code" != "200" ]]; then
    echo "FAILED (HTTP ${code})"
    exit 1
  fi
  echo "OK"
done

echo ""
echo "==> Reloading server cache"
curl -s -X POST "${BASE_URL}/admin/reload?api_key=${API_KEY}" && echo ""

echo ""
echo "Done. Data synced to ${BASE_URL}"
