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
    python3 python3-venv python3-pip sqlite3 git rsync avahi-daemon

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
mkdir -p "$DATA_DIR/publish" "$DATA_DIR/backups"
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

if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    HOSTNAME_LOCAL="$(hostname).local"
    cat <<EOF

  Stockroom is running.

    Staff UI     http://${HOSTNAME_LOCAL}:8000/
    Public page  http://${HOSTNAME_LOCAL}:8000/public/
    Health       http://${HOSTNAME_LOCAL}:8000/health

  Next steps
    1. Open the staff UI and enter your name when prompted.
    2. Import existing stock:
         sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/stockroom import stock.csv
         (add --commit once the dry run looks right)
    3. Print barcode labels from  http://${HOSTNAME_LOCAL}:8000/labels

  Logs        journalctl -u stockroom -f
  Restart     sudo systemctl restart stockroom
  Backups     nightly at 02:30 into ${DATA_DIR}/backups

EOF
else
    echo "The service did not come up. Check: journalctl -u stockroom -n 50" >&2
    exit 1
fi
