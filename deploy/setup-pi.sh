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
#
# THE LEADING SLASHES ARE LOAD-BEARING. An rsync pattern without one matches
# at every level of the tree, not just the top -- so a bare `--exclude publish`
# also deletes src/stockroom/publish/, the Python subpackage that renders the
# public page, and the install then dies with:
#
#     ModuleNotFoundError: No module named 'stockroom.publish'
#
# Anchored patterns match only the transfer root, which is what was meant:
# the generated ./publish output directory and the local ./data database.
# __pycache__ stays unanchored on purpose -- that one really should go at
# every level. Covered by tests/test_deploy.py::test_the_installer_rsync_
# keeps_every_python_package.
rsync -a --delete \
    --exclude '/.venv' --exclude '__pycache__' --exclude '/.git' \
    --exclude '/data' --exclude '/publish' --exclude '/.pytest_cache' \
    "$REPO_DIR/" "$APP_DIR/"

say "Building the virtual environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

say "Installing the ${SERVICE_USER} command"
# So that the `stockroom doctor` written in README.md and docs/security.md is
# a command that exists on this machine, rather than one that needs the venv
# path and `sudo -u` typed in front of it. The wrapper supplies both.
install -m 0755 "$REPO_DIR/deploy/stockroom-wrapper.sh" /usr/local/bin/stockroom

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

# Read the env file the way systemd's EnvironmentFile= does, NOT the way bash
# `source` does. They are not the same language: systemd takes everything after
# the first `=` as a literal value, so an operator writing
#
#     STOCKROOM_ORG=Carlson Center for Imaging Science
#
# is perfectly valid to systemd and a syntax error to bash, which reads
# `Center` as a command and dies with "Center: command not found" -- part-way
# through the install, having already created the service account and the
# database directory.
#
# Parsing also means this script never executes the contents of a file in
# /etc as root, which sourcing did.
load_env_file() {
    local file="$1" line key value
    [[ -r "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*($|#|\;) ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        value="${line#*=}"
        key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Strip one layer of matching quotes, as systemd does.
        if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then
            value="${value:1:${#value}-2}"
        fi
        export "$key=$value"
    done < "$file"
}

say "Initialising the database"
( load_env_file "$ENV_FILE"
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
#
# Drop a real certificate in as stockroom.crt/stockroom.key and this block
# leaves it alone; nginx reads these paths either way.
CERT_DIR=/etc/ssl/stockroom
install -d -m 0755 "$CERT_DIR"

# Every name this Pi can be reached by has to be a SAN, or the browser reports
# a name mismatch and trusting the certificate does not help -- the name simply
# is not in it.
#
# `hostname` alone is not enough for either half of this. On Debian it usually
# prints the SHORT name while the qualified one lives in /etc/hosts, so a Pi
# registered as cisstockroom.device.rit.edu got a certificate naming
# `cisstockroom` and `cisstockroom.local` and nothing for the name anybody
# types. And where `hostname` DOES print the FQDN, the old code appended
# `.local` to it and generated a SAN for cisstockroom.device.rit.edu.local.
# Hence: ask for both forms and derive the short one by truncation.
HOST_FULL="$(hostname -f 2>/dev/null || hostname)"
HOST_FULL="${HOST_FULL,,}"
HOST_SHORT="${HOST_FULL%%.*}"
HOST_IP="$(hostname -I | awk '{print $1}')"

CERT_SANS="DNS:${HOST_SHORT},DNS:${HOST_SHORT}.local"
CERT_CN="${HOST_SHORT}.local"
if [[ "$HOST_FULL" != "$HOST_SHORT" && "$HOST_FULL" == *.* ]]; then
    CERT_SANS="DNS:${HOST_FULL},${CERT_SANS}"
    CERT_CN="$HOST_FULL"
fi
[[ -n "$HOST_IP" ]] && CERT_SANS="${CERT_SANS},IP:${HOST_IP}"

if [[ ! -f "$CERT_DIR/stockroom.crt" ]]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 1825 \
        -keyout "$CERT_DIR/stockroom.key" \
        -out "$CERT_DIR/stockroom.crt" \
        -subj "/CN=${CERT_CN}/O=CIS Stockroom" \
        -addext "subjectAltName=${CERT_SANS}" \
        2>/dev/null
    chmod 0640 "$CERT_DIR/stockroom.key"
    chgrp www-data "$CERT_DIR/stockroom.key"
    echo "Generated a self-signed certificate for ${CERT_SANS}."
else
    # Existing certificate: say whether it actually covers this machine's
    # names. A Pi set up under one name and later given a DNS record keeps the
    # certificate it was imaged with, and the resulting name mismatch looks
    # exactly like the untrusted-issuer warning that is expected here -- so it
    # gets ignored, and the only fix is a regeneration nothing prompts for.
    echo "Keeping the existing certificate in $CERT_DIR."
    # `-checkhost` exits 0 whether or not it matched -- the answer is the text
    # it prints, "does match certificate" or "does NOT match certificate". A
    # bare `if ! openssl ...` here silently never fires.
    if ! openssl x509 -in "$CERT_DIR/stockroom.crt" -noout \
            -checkhost "$HOST_FULL" 2>/dev/null | grep -q "does match certificate"; then
        echo "  WARNING: it does not cover ${HOST_FULL}, so browsers will report"
        echo "  a name mismatch. To replace it with one that does:"
        echo "      sudo rm ${CERT_DIR}/stockroom.crt ${CERT_DIR}/stockroom.key"
        echo "      sudo ${BASH_SOURCE[0]}"
    fi
fi

say "Configuring nginx"
install -m 0644 "$REPO_DIR/deploy/nginx-stockroom.conf" \
    /etc/nginx/sites-available/stockroom
ln -sf /etc/nginx/sites-available/stockroom /etc/nginx/sites-enabled/stockroom
rm -f /etc/nginx/sites-enabled/default
# The public page is served straight from disk by nginx, so www-data has to
# reach $DATA_DIR/publish. It does NOT have to read anything else in there.
#
# 0755 on $DATA_DIR was how it used to get through, and it also let every
# local account list and read the directory holding stockroom.db -- every
# password hash, every session token hash, every email address and the whole
# audit log. 0751 grants the traverse (--x) that nginx actually needs and
# nothing else: others can pass through to publish/ but cannot list what is
# beside it.
chmod 0751 "$DATA_DIR"
chmod 0755 "$DATA_DIR/publish"
# Backups are whole copies of the database, so they are exactly as sensitive.
chmod 0750 "$DATA_DIR/backups" "$DATA_DIR/photos"
# An `[[ ... ]] && chmod` one-liner would abort the whole installer here under
# `set -e` on a fresh Pi, where there is no database yet.
if [[ -f "$DATA_DIR/stockroom.db" ]]; then
    chmod 0640 "$DATA_DIR"/stockroom.db*
fi
if nginx -t; then
    systemctl enable --now nginx
    systemctl reload nginx
else
    echo "nginx rejected the configuration; not reloading." >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Single sign-on, if and only if it has been asked for.
#
# Everything here is skipped when STOCKROOM_AUTH_MODE is unset or "password",
# which is the default and the state of the Pi until RIT ITS have registered
# the service provider. That keeps a normal install exactly as fast and as
# small as it has always been -- python3-saml pulls in lxml and xmlsec, which
# are native extensions, and nobody should pay for them to run a stockroom on
# passwords.
# --------------------------------------------------------------------------
( load_env_file "$ENV_FILE"
  if [[ "${STOCKROOM_AUTH_MODE:-password}" != "password" ]]; then
      say "Setting up RIT single sign-on"

      # Build dependencies for xmlsec. A wheel usually exists for this
      # architecture and none of this is needed; when one does not, the
      # compile fails with a header error that reads like a Python problem.
      apt-get install -y --no-install-recommends \
          libxmlsec1-dev libxml2-dev pkg-config python3-dev

      "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[sso]"

      install -d -m 0755 /etc/stockroom
      # Generates the SAML keypair and caches RIT's metadata. Never
      # overwrites an existing key: doing so would silently invalidate a
      # registration ITS have already accepted.
      sudo -u "$SERVICE_USER" --preserve-env=STOCKROOM_AUTH_MODE,STOCKROOM_SSO_BASE_URL,STOCKROOM_SSO_ENTITY_ID,STOCKROOM_SSO_SP_CERT,STOCKROOM_SSO_SP_KEY,STOCKROOM_SSO_IDP_METADATA \
          "$APP_DIR/.venv/bin/stockroom" sso init || {
          echo "  Single sign-on is not ready yet. The service will still" >&2
          echo "  start; see docs/its-registration.md." >&2
      }
      # The private key is read by the service, not by everyone on the Pi.
      chgrp "$SERVICE_USER" /etc/stockroom/sp.key 2>/dev/null || true
      chmod 0640 /etc/stockroom/sp.key 2>/dev/null || true
  fi
)

say "Installing systemd units"
install -m 0644 "$REPO_DIR/deploy/stockroom.service"        /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/stockroom-backup.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/stockroom-backup.timer"   /etc/systemd/system/

# The backup unit runs under ProtectSystem=strict, which makes everything
# read-only except the paths it names. STOCKROOM_BACKUP_COPY_DIR points
# somewhere else by definition -- a USB stick under /mnt, usually -- so
# without this the documented off-box backup fails every night with a
# read-only-filesystem error, while the identical command run by hand in an
# SSH session works perfectly.
#
# The "-" prefix on ReadWritePaths means "tolerate this path not existing",
# so an unplugged stick is a backup failure rather than a unit that refuses
# to start at all.
BACKUP_DROPIN=/etc/systemd/system/stockroom-backup.service.d
( load_env_file "$ENV_FILE"
  if [[ -n "${STOCKROOM_BACKUP_COPY_DIR:-}" ]]; then
      say "Granting the backup job write access to ${STOCKROOM_BACKUP_COPY_DIR}"
      install -d -m 0755 "$BACKUP_DROPIN"
      cat > "$BACKUP_DROPIN/backup-copy-dir.conf" <<EOF
# Written by deploy/setup-pi.sh from STOCKROOM_BACKUP_COPY_DIR in
# ${ENV_FILE}. Change the variable there and re-run the installer rather
# than editing this file.
[Service]
ReadWritePaths=-${STOCKROOM_BACKUP_COPY_DIR}
EOF
  else
      rm -f "$BACKUP_DROPIN/backup-copy-dir.conf"
  fi )

systemctl daemon-reload
systemctl enable stockroom.service stockroom-backup.timer

# RESTART, not `enable --now`. On an already-running install `--now` sees an
# active unit and does nothing, so re-running this script -- which the header
# above promises "upgrades an existing install in place" -- copied new code to
# /opt/stockroom and then left the old process serving it. Every fix looked
# like it had not worked.
systemctl restart stockroom.service
systemctl start stockroom-backup.timer

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

# The name to print. Prefer the qualified one when the Pi has it: that is what
# people type and what the certificate now names. `$(hostname).local` was both
# wrong on a Pi whose hostname is already qualified -- yielding
# cisstockroom.device.rit.edu.local -- and, where it was right, still pointed
# everyone at the mDNS name rather than the DNS record. HOST_FULL and
# HOST_SHORT come from the TLS section above.
if [[ "$HOST_FULL" != "$HOST_SHORT" && "$HOST_FULL" == *.* ]]; then
    SITE_HOST="$HOST_FULL"
else
    SITE_HOST="${HOST_SHORT}.local"
fi
# Print the IP as well as the name. `.local` needs an mDNS resolver, which
# Android and some Chromebooks do not have -- on those the address is the only
# way in, and someone standing at the counter with a phone should not have to
# work that out. The app accepts a bare IP as the Host for the same reason.
LAN_IP="$(hostname -I | awk '{print $1}')"
ADMIN_EXISTS=$(sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/stockroom" user list --status active 2>/dev/null | grep -c admin || true)

cat <<EOF

  Stockroom is running.

    Staff UI     https://${SITE_HOST}/       or  https://${LAN_IP}/
    Public page  https://${SITE_HOST}/public/   (also on http://)
    Health       https://${SITE_HOST}/health

  If a phone cannot open the .local name, use the address. Give the Pi a
  DHCP reservation on the router so that address does not move.

  Your browser will warn about the certificate until you trust it. That is
  expected with a self-signed certificate -- docs/security.md explains how to
  install it on the stockroom machines, and how to replace it with a real one.

EOF

if [[ "$ADMIN_EXISTS" -eq 0 ]]; then
cat <<EOF
  NEXT, AND REQUIRED -- create the first administrator. There is deliberately
  no way to do this over the network:

    stockroom user create \\
        --first-name Your --last-name Name --email you@rit.edu --admin

EOF
fi

cat <<EOF
  Then
    1. Sign in and import existing stock:
         stockroom import stock.csv
         (add --commit once the dry run looks right)
    2. Print barcode labels from  https://${SITE_HOST}/labels
    3. LOCK THE PI DOWN -- this script has not done it:
         sudo ./deploy/harden-pi.sh
         (allows 22/80/443 from the campus network, eduroam included;
          --allow-from <cidr> to set the ranges yourself)

  Commands    stockroom status | doctor | loans --overdue
              (runs as the ${SERVICE_USER} account; --actor names you in the log)
  Logs        journalctl -u stockroom -f
  Restart     sudo systemctl restart stockroom
  Backups     nightly at 02:30 into ${DATA_DIR}/backups
              (copy them off this machine -- see docs/operations.md)

EOF
