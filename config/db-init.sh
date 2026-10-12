#!/usr/bin/env bash
# Least-privilege Postgres roles. Idempotent.
#  - Fresh volume: runs automatically (mounted into /docker-entrypoint-initdb.d).
#  - Existing volume: run once by hand:  docker compose exec -T db bash < config/db-init.sh
# Role passwords come from the db service environment (see docker-compose.yml).
set -euo pipefail
: "${LITELLM_DB_PASSWORD:?}" "${GATEWAY_OWNER_PASSWORD:?}" "${GATEWAY_APP_PASSWORD:?}"
PSQL=(psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER")

"${PSQL[@]}" -d postgres \
  -v litellm_pw="$LITELLM_DB_PASSWORD" -v owner_pw="$GATEWAY_OWNER_PASSWORD" -v app_pw="$GATEWAY_APP_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', r) FROM unnest(ARRAY['litellm_app','gateway_owner','gateway_app']) r
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) \gexec
ALTER ROLE litellm_app   LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'litellm_pw';
ALTER ROLE gateway_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'owner_pw';
ALTER ROLE gateway_app   LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';

SELECT 'CREATE DATABASE litellm' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm') \gexec
SELECT 'CREATE DATABASE gateway' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'gateway') \gexec
ALTER DATABASE litellm OWNER TO litellm_app;
ALTER DATABASE gateway OWNER TO gateway_owner;

-- Postgres lets every role connect to every database by default; undo that.
REVOKE CONNECT ON DATABASE litellm, gateway FROM PUBLIC;
GRANT CONNECT ON DATABASE litellm TO litellm_app;
GRANT CONNECT ON DATABASE gateway TO gateway_owner, gateway_app;
SQL

# Move existing LiteLLM objects (created earlier by the bootstrap superuser) to litellm_app.
# No-op on a fresh volume. Sequences owned by a table column move with their table.
"${PSQL[@]}" -d litellm <<'SQL'
DO $$
DECLARE o record;
BEGIN
  FOR o IN SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m') ORDER BY c.relkind LOOP
    EXECUTE format('ALTER %s public.%I OWNER TO litellm_app',
                   CASE o.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, o.relname);
  END LOOP;
  FOR o IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'S' AND pg_get_userbyid(c.relowner) <> 'litellm_app' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO litellm_app', o.relname);
  END LOOP;
  FOR o IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public' AND t.typtype IN ('e','d') LOOP
    EXECUTE format('ALTER TYPE public.%I OWNER TO litellm_app', o.typname);
  END LOOP;
END $$;
SQL
echo "db-init: roles litellm_app, gateway_owner, gateway_app ready"
