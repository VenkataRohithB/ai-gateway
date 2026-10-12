#!/usr/bin/env bash
# Create .env with fresh random secrets, or (--add-missing) append only the variables
# an existing .env lacks. Never changes existing values (LITELLM_SALT_KEY must stay stable).
# Never prints secrets.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

hex() { openssl rand -hex "$1"; }
gen() {
  case "$1" in
    POSTGRES_USER)          echo gateway ;;
    POSTGRES_PASSWORD|LITELLM_DB_PASSWORD|GATEWAY_OWNER_PASSWORD|GATEWAY_APP_PASSWORD) hex 24 ;;
    LITELLM_MASTER_KEY|LITELLM_SALT_KEY) echo "sk-$(hex 32)" ;;
    LITELLM_GATEWAY_KEY)    echo "sk-gw-$(hex 32)" ;;
    ADMIN_TOKEN)            echo "adm_$(hex 32)" ;;
    GEMINI_API_KEY)         echo "" ;;
  esac
}
VARS=(POSTGRES_USER POSTGRES_PASSWORD LITELLM_MASTER_KEY LITELLM_SALT_KEY
      LITELLM_DB_PASSWORD GATEWAY_OWNER_PASSWORD GATEWAY_APP_PASSWORD
      LITELLM_GATEWAY_KEY ADMIN_TOKEN GEMINI_API_KEY)

if [ "${1:-}" = "--add-missing" ]; then
  [ -e .env ] || { echo ".env missing; run without --add-missing first" >&2; exit 1; }
  added=()
  for v in "${VARS[@]}"; do
    grep -q "^$v=" .env || { echo "$v=$(gen "$v")" >> .env; added+=("$v"); }
  done
  echo "added: ${added[*]:-nothing}"
else
  [ -e .env ] && { echo ".env exists; not overwriting (use --add-missing)" >&2; exit 1; }
  for v in "${VARS[@]}"; do echo "$v=$(gen "$v")"; done > .env
  echo "wrote .env (secrets not printed). Add GEMINI_API_KEY yourself."
fi
