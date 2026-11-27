#!/bin/sh
# PostgreSQL first-start script (mounted into /docker-entrypoint-initdb.d): one database for
# n8n itself, one for the idempotency ledger, each with its own login role. The ledger role
# may only read and write the ledger table. Passwords come from Docker secrets.
set -eu

n8n_pw=$(cat /run/secrets/n8n_db_password)
ledger_pw=$(cat /run/secrets/ledger_db_password)

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v n8n_pw="$n8n_pw" -v ledger_pw="$ledger_pw" <<'SQL'
CREATE ROLE n8n LOGIN PASSWORD :'n8n_pw';
CREATE DATABASE n8n OWNER n8n;
CREATE ROLE automation LOGIN PASSWORD :'ledger_pw';
CREATE DATABASE automation;
REVOKE ALL ON DATABASE automation FROM PUBLIC;
GRANT CONNECT ON DATABASE automation TO automation;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname automation <<'SQL'
\i /opt/pack/ledger.sql
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO automation;
GRANT SELECT, INSERT, UPDATE ON idempotency_keys TO automation;
SQL
