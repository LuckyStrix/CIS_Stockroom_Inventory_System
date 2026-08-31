#!/usr/bin/env bash
#
# Install the CIS Stockroom Inventory System on a Raspberry Pi.
#
# Assumes a fresh Raspberry Pi OS (Bookworm) Lite install with network and
# SSH already working -- see docs/raspberry-pi-setup.md for getting to that
# point from a blank SD card.
#
# Run from a checkout of this repository:
#
#     sudo ./deploy/setup-pi.sh
#
# Safe to re-run: it upgrades an existing install in place and never touches
# the database.

set -euo pipefail

APP_DIR=/opt/stockroom
DATA_DIR=/var/lib/stockroom
ENV_FILE=/etc/stockroom.env
SERVICE_USER=stockroom
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }

say "Installing system packages"
apt-get update -qq
# sqlite3 is only for poking at the database by hand; the app uses Python's
# built-in module. git is needed only for the optional Pages publisher.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip sqlite3 git rsync avahi-daemon nginx openssl rclone

say "Creating the ${SERVICE_USER} service account"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "created"
else
    echo "already exists"
fi

say "Installing the application to ${APP_DIR}"
mkdir -p "$APP_DIR"
# Copy the source, but never the local database, venv or generated page.
rsync -a --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
    --exclude 'data' --exclude 'publish' --exclude '.pytest_cache' \
    "$REPO_DIR/" "$APP_DIR/"

say "Building the virtual environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

say "Preparing ${DATA_DIR}"
mkdir -p "$DATA_DIR/publish" "$DATA_DIR/backups" "$DATA_DIR/photos"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chown -R root:root "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
    say "Installing ${ENV_FILE}"
    install -m 0644 "$REPO_DIR/deploy/stockroom.env.example" "$ENV_FILE"
    echo "Edit $ENV_FILE to change the organisation name or enable GitHub Pages."
else
    say "Keeping the existing ${ENV_FILE}"
fi

say "Initialising the database"
# Source rather than word-split the env file: STOCKROOM_ORG contains spaces.
( set -a; . "$ENV_FILE"; set +a
  sudo -u "$SERVICE_USER" --preserve-env=STOCKROOM_DATA_DIR,STOCKROOM_PUBLISH_DIR,STOCKROOM_ORG \
      "$APP_DIR/.venv/bin/stockroom" init )

# ---------------------------------------------------------------------------
say "Setting up TLS"
# ---------------------------------------------------------------------------
# There are passwords in this system now, so plain HTTP is not an option: a
# session cookie or a password crossing a shared university LAN in the clear
# is the whole game.
#
# A self-signed certificate encrypts that traffic, which is the important
# part. It does NOT authenticate the server, so browsers will warn until it
# is trusted on the machines that use it -- see docs/security.md, which also
# explains why an ITS-issued certificate is the real fix.
CERT_DIR=/etc/ssl/stockroom
install -d -m 0755 "$CERT_DIR"

if [[ ! -f "$CERT_DIR/stockroom.crt" ]]; then
    HOST_SHORT="$(hostname)"
    HOST_IP="$(hostname -I | awk '{print $1}')"
    openssl req -x509 -nodes -newkey rsa:2048 -days 1825 \
        -keyout "$CERT_DIR/stockroom.key" \
        -out "$CERT_DIR/stockroom.crt" \
        -subj "/CN=${HOST_SHORT}.local/O=CIS Stockroom" \
        -addext "subjectAltName=DNS:${HOST_SHORT},DNS:${HOST_SHORT}.local,IP:${HOST_IP}" \
        2>/dev/null
    chmod 0640 "$CERT_DIR/stockroom.key"
    chgrp www-data "$CERT_DIR/stockroom.key"
    echo "Generated a self-signed certificate for ${HOST_SHORT}.local (${HOST_IP})."
else
    echo "Keeping the existing certificate in $CERT_DIR."
fi

say "Configuring nginx"
install -m 0644 "$REPO_DIR/deploy/nginx-stockroom.conf" \
    /etc/nginx/sites-available/stockroom
ln -sf /etc/nginx/sites-available/stockroom /etc/nginx/sites-enabled/stockroom
rm -f /etc/nginx/sites-enabled/default
# The public page is served straight from disk by nginx, so it needs to be
# able to traverse into the publish directory.
chmod 0755 "$DATA_DIR" "$DATA_DIR/publish"
if nginx -t; then
    systemctl enable --now nginx
    systemctl reload nginx
else
    echo "nginx rejected the configuration; not reloading." >&2
    exit 1
fi

say "Installing systemd units"
install -m 0644 "$REPO_DIR/deploy/stockroom.service"        /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/stockroom-backup.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/stockroom-backup.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stockroom.service
systemctl enable --now stockroom-backup.timer

say "Waiting for the service to answer"
for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "The service did not come up. Check: journalctl -u stockroom -n 50" >&2
    exit 1
fi

HOSTNAME_LOCAL="$(hostname).local"
ADMIN_EXISTS=$(sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/stockroom" user list --status active 2>/dev/null | grep -c admin || true)

cat <<EOF

  Stockroom is running.

    Staff UI     https://${HOSTNAME_LOCAL}/
    Public page  https://${HOSTNAME_LOCAL}/public/   (also on http://)
    Health       https://${HOSTNAME_LOCAL}/health

  Your browser will warn about the certificate until you trust it. That is
  expected with a self-signed certificate -- docs/security.md explains how to
  install it on the stockroom machines, and how to replace it with a real one.

EOF

if [[ "$ADMIN_EXISTS" -eq 0 ]]; then
cat <<EOF
  NEXT, AND REQUIRED -- create the first administrator. There is deliberately
  no way to do this over the network:

    sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/stockroom user create \\
        --first-name Your --last-name Name --email you@rit.edu --admin

EOF
fi

cat <<EOF
  Then
    1. Sign in and import existing stock:
         sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/stockroom import stock.csv
         (add --commit once the dry run looks right)
    2. Print barcode labels from  https://${HOSTNAME_LOCAL}/labels
    3. LOCK THE PI DOWN -- this script has not done it:
         sudo ./deploy/harden-pi.sh --subnet <your campus CIDR>

  Logs        journalctl -u stockroom -f
  Restart     sudo systemctl restart stockroom
  Backups     nightly at 02:30 into ${DATA_DIR}/backups
              (copy them off this machine -- see docs/operations.md)

EOF
