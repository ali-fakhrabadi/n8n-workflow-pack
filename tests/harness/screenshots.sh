#!/usr/bin/env bash
# Take the README screenshots from a stack kept by a harness run:
#
#   HARNESS_KEEP=1 tests/harness/run.sh       # prints PACK_SECRETS=... at the end
#   PACK_SECRETS=/tmp/tmp.xxxx tests/harness/screenshots.sh
#
# Writes assets/screenshots/*.png. The browser runs on the stack's internal network only.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${PACK_SECRETS:?set PACK_SECRETS to the secrets directory of the kept run}"
project="${COMPOSE_PROJECT_NAME:-n8n-pack-harness}"
image=n8n-workflow-pack-screenshots:local
docker build -q -t "$image" -f tests/harness/Dockerfile.screenshots tests/harness >/dev/null
mkdir -p assets/screenshots
docker run --rm --network "${project}_pack" --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -v "$PWD/tests/harness/screenshots.py:/shots.py:ro" \
  -v "$PWD/tests/harness/out:/out:ro" -v "$PWD/assets/screenshots:/shots" \
  -v "$PACK_SECRETS/n8n_owner_password:/run/secrets/n8n_owner_password:ro" \
  "$image" python /shots.py
