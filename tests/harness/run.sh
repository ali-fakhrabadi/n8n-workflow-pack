#!/usr/bin/env bash
# Replay the recorded events against a fresh, disposable n8n and remove it afterwards.
#
#   tests/harness/run.sh                 # report: tests/harness/out/harness-report.json
#   HARNESS_ONLY=social tests/harness/run.sh
#   HARNESS_KEEP=1 HARNESS_EDITOR=1 tests/harness/run.sh   # keep it running, editor on 127.0.0.1
#
# Every run starts from empty volumes with throw-away secrets (never a deployment's). The
# images are pulled first, so the measured import-to-first-run time does not include downloads.
#
# Timing (all on the host clock, written to out/timing.env):
#   import start   n8n-setup starts against an empty n8n database (migrations, credentials,
#                  workflow import, publish)
#   setup done     n8n-setup exited
#   ready          n8n reports /healthz/readiness
#   first success  the first replayed event's run finished successfully (from the harness)
set -euo pipefail
cd "$(dirname "$0")/../.."

# Short waits so the approval-timeout and follow-up paths run in minutes, not days.
export PACK_APPROVAL_TIMEOUT_S="${PACK_APPROVAL_TIMEOUT_S:-25}"
export PACK_FOLLOW_UP_DELAY_S="${PACK_FOLLOW_UP_DELAY_S:-12}"
export PACK_MAX_ATTEMPTS="${PACK_MAX_ATTEMPTS:-4}"
export PACK_RETRY_BASE_S="${PACK_RETRY_BASE_S:-1}"
export PACK_CLAIM_STALE_AFTER_S="${PACK_CLAIM_STALE_AFTER_S:-900}"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-n8n-pack-harness}"
export PACK_SECRETS HARNESS_UID HARNESS_GID HARNESS_OUT_DIR="$PWD/tests/harness/out"
PACK_SECRETS=$(mktemp -d)
HARNESS_UID=$(id -u)
HARNESS_GID=$(id -g)
files=(-f compose.yaml)
if [ -n "${HARNESS_EDITOR:-}" ]; then files+=(-f compose.editor.yaml); fi
dc() { docker compose "${files[@]}" --profile harness "$@"; }

cleanup() {
  if [ -n "${HARNESS_KEEP:-}" ]; then
    echo "kept the stack (secrets in $PACK_SECRETS); remove it with:" >&2
    echo "  PACK_SECRETS=$PACK_SECRETS COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME docker compose ${files[*]} --profile harness down -v" >&2
    return
  fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$PACK_SECRETS"
}
trap cleanup EXIT

for name in postgres_password n8n_db_password ledger_db_password n8n_encryption_key \
            n8n_runners_auth_token n8n_owner_password webhook_token downstream_token; do
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$PACK_SECRETS/$name"
done
chmod 711 "$PACK_SECRETS"
chmod 644 "$PACK_SECRETS"/*
rm -rf "$HARNESS_OUT_DIR"
mkdir -p "$HARNESS_OUT_DIR"

now_ms() { date +%s%3N; }

dc down -v --remove-orphans >/dev/null 2>&1 || true
if [ "${HARNESS_PULL:-1}" = 1 ]; then dc pull --quiet; fi
dc up -d --wait postgres mock-sinks mailpit

export HARNESS_IMPORT_START_MS HARNESS_READY_MS HARNESS_SETUP_DONE
HARNESS_IMPORT_START_MS=$(now_ms)
dc up -d --wait n8n
HARNESS_READY_MS=$(now_ms)
HARNESS_SETUP_DONE=$(docker inspect --format '{{.State.FinishedAt}}' "$(dc ps -a -q n8n-setup)")
dc up -d n8n-runners
{
  echo "import_start_ms=$HARNESS_IMPORT_START_MS"
  echo "setup_finished_at=$HARNESS_SETUP_DONE"
  echo "ready_ms=$HARNESS_READY_MS"
} > "$HARNESS_OUT_DIR/timing.env"
dc logs --no-color n8n-setup > "$HARNESS_OUT_DIR/n8n-setup.log" 2>&1 || true

status=0
dc run --rm harness || status=$?

# The ledger as PostgreSQL holds it, for the final checks of each event's end state.
dc exec -T postgres psql -U postgres -d automation -At -c \
  "SELECT coalesce(json_agg(t ORDER BY created_at), '[]') FROM idempotency_keys t" \
  > "$HARNESS_OUT_DIR/ledger.json" || status=1
dc run --rm harness python -u /harness/replay.py --verify-ledger || status=$?

if [ "$status" -ne 0 ]; then
  dc logs --no-color --tail 200 n8n > "$HARNESS_OUT_DIR/n8n.log" 2>&1 || true
fi
exit "$status"
