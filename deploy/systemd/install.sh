#!/usr/bin/env bash
# Bare-metal install for Debian/Ubuntu with systemd (no containers).
#   sudo bash deploy/systemd/install.sh example.com
set -euo pipefail
DOMAIN="${1:?usage: install.sh <domain>}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
apt-get update
apt-get install -y python3 python3-venv postgresql caddy
id redblue >/dev/null 2>&1 || useradd --system --create-home --home-dir /srv/redblue redblue
install -d -o redblue -g redblue /srv/redblue/site /srv/redblue/site/media /etc/redblue
python3 -m venv /srv/redblue/venv
/srv/redblue/venv/bin/pip install --upgrade pip
/srv/redblue/venv/bin/pip install "$SRC/packages/core[postgres,ai]" "$SRC/packages/cms" \
  "$SRC/packages/templates" "$SRC/packages/growth" "$SRC/packages/observe" "$SRC/packages/gate" \
  "$SRC/packages/builder" "$SRC/admin" "$SRC/packages/platform" "$SRC/packages/cli"
if ! sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='redblue'" | grep -q 1; then
  DBPASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sudo -u postgres psql -c "CREATE ROLE redblue LOGIN PASSWORD '$DBPASS'"
  sudo -u postgres psql -c "CREATE DATABASE redblue OWNER redblue"
  cat > /etc/redblue/env <<ENV
RB_ENV=prod
RB_BASE_URL=https://$DOMAIN
RB_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
RB_DATABASE_URL=postgresql+psycopg://redblue:$DBPASS@127.0.0.1:5432/redblue
RB_STORAGE_DIR=/srv/redblue/site/media
ENV
  chmod 600 /etc/redblue/env
  chown redblue:redblue /etc/redblue/env
fi
install -m 644 "$SRC/deploy/systemd/redblue.service" /etc/systemd/system/redblue.service
install -m 644 "$SRC/deploy/systemd/redblue-worker.service" /etc/systemd/system/redblue-worker.service
printf '%s {\n\tencode zstd gzip\n\treverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now redblue redblue-worker
systemctl reload caddy
echo "Installed. Create an admin with:"
echo "  sudo -u redblue env \$(cat /etc/redblue/env | xargs) /srv/redblue/venv/bin/redblue createadmin you@example.com"
echo "Then run: sudo -u redblue env \$(cat /etc/redblue/env | xargs) /srv/redblue/venv/bin/redblue doctor"
