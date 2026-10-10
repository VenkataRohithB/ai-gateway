#!/usr/bin/env bash
# Create .env with fresh random secrets. Refuses to overwrite (salt key must stay stable).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -e .env ] && { echo ".env exists; not overwriting" >&2; exit 1; }
umask 077
cat > .env <<EOT
POSTGRES_USER=gateway
POSTGRES_PASSWORD=$(openssl rand -hex 24)
LITELLM_MASTER_KEY=sk-$(openssl rand -hex 32)
LITELLM_SALT_KEY=sk-$(openssl rand -hex 32)
OPENAI_API_KEY=
EOT
echo "wrote .env (secrets not printed). Add OPENAI_API_KEY yourself."
