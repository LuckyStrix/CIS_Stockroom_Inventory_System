#!/usr/bin/env bash
#
# Replace the self-signed certificate with a Let's Encrypt one that renews
# itself.
#
#     sudo ./deploy/enable-letsencrypt.sh --dry-run    # try it; changes nothing
#     sudo ./deploy/enable-letsencrypt.sh              # for $(hostname -f)
#     sudo ./deploy/enable-letsencrypt.sh --domain stockroom.cis.rit.edu
#     sudo ./deploy/enable-letsencrypt.sh --email you@rit.edu
#
# What it needs, and what it does not:
#
#   * A public DNS name for this Pi. A .local name or a bare IP cannot have a
#     publicly trusted certificate.
#   * Port 80 routable from the internet -- RIT's border does not filter it
#     for this host. ufw on the Pi DOES, by design (harden-pi.sh), and that
#     stays true: the certbot hooks open port 80 to everyone only for the
#     seconds a renewal takes and close it again whether it worked or not.
#     See deploy/stockroom-acme-hook.sh.
#   * Nothing from ITS, no DNS changes, and no change to SSO. The SAML keys
#     are a separate pair (/etc/stockroom/sp.crt) and are not in nginx at all.
#
# The certificate lands at /etc/ssl/stockroom/stockroom.{crt,key}, the paths
# nginx has always read. The self-signed one is kept beside it as
# *.self-signed. To go back:
#
#     sudo certbot delete --cert-name <domain>
#     sudo cp /etc/ssl/stockroom/stockroom.crt.self-signed /etc/ssl/stockroom/stockroom.crt
#     sudo cp /etc/ssl/stockroom/stockroom.key.self-signed /etc/ssl/stockroom/stockroom.key
#     sudo systemctl reload nginx
#
# Idempotent: re-running it installs the current hook and re-issues nothing
# that is not due. `stockroom doctor` warns if renewal stops working.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK=/usr/local/lib/stockroom/acme-hook.sh
WEBROOT=/var/www/acme
NGINX_SITE=/etc/nginx/sites-available/stockroom

DOMAIN=""
EMAIL=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)  DOMAIN="$2"; shift 2 ;;
        --email)   EMAIL="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 [--dry-run]" >&2
    exit 1
fi

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }

# `hostname -f`, not `hostname`, for the same reason as setup-pi.sh: Debian
# usually prints the short name, and nobody can issue a certificate for that.
if [[ -z "$DOMAIN" ]]; then
    DOMAIN="$(hostname -f 2>/dev/null || hostname)"
fi
DOMAIN="${DOMAIN,,}"
if [[ "$DOMAIN" != *.* || "$DOMAIN" == *.local ]]; then
    echo "\"$DOMAIN\" is not a public DNS name. Pass --domain." >&2
    exit 1
fi

# The challenge is served by nginx's port-80 block. A Pi whose nginx config
# predates that location would answer every challenge with a redirect to
# HTTPS -- which ufw refuses from outside -- and certbot would report only a
# timeout from Let's Encrypt's side.
if ! grep -q 'acme-challenge' "$NGINX_SITE" 2>/dev/null; then
    echo "$NGINX_SITE does not serve /.well-known/acme-challenge/." >&2
    echo "Update it first:  sudo $REPO_DIR/deploy/setup-pi.sh" >&2
    exit 1
fi

say "Installing certbot"
apt-get install -y -qq certbot >/dev/null

say "Installing the renewal hooks"
install -d -m 0755 "$(dirname "$HOOK")"
install -m 0755 "$REPO_DIR/deploy/stockroom-acme-hook.sh" "$HOOK"
install -d -m 0755 "$WEBROOT"

if [[ -n "$EMAIL" ]]; then
    ACCOUNT=(--email "$EMAIL")
else
    # Let's Encrypt stopped sending expiry mail in 2025, so an address buys
    # little; `stockroom doctor` is what notices a renewal that stopped.
    ACCOUNT=(--register-unsafely-without-email)
fi

CERTBOT=(certbot certonly --non-interactive --agree-tos "${ACCOUNT[@]}"
         --webroot -w "$WEBROOT" -d "$DOMAIN"
         --pre-hook "$HOOK open"
         --post-hook "$HOOK close"
         --deploy-hook "$HOOK install")

if [[ $DRY_RUN -eq 1 ]]; then
    # Against the staging server, saving nothing and running no deploy hook --
    # but it does run the pre and post hooks, so it proves the firewall
    # really opens and really closes.
    say "Dry run against Let's Encrypt staging for $DOMAIN"
    "${CERTBOT[@]}" --dry-run
    echo
    echo "Dry run passed. Run again without --dry-run to issue the certificate."
    exit 0
fi

say "Requesting a certificate for $DOMAIN"
"${CERTBOT[@]}" --keep-until-expiring

# Debian's package ships the timer; a Pi that had it disabled would issue
# once and quietly expire ninety days later.
systemctl enable --now certbot.timer >/dev/null

say "Done"
openssl x509 -in /etc/ssl/stockroom/stockroom.crt -noout -subject -issuer -enddate
echo
echo "Renewal is automatic (systemctl list-timers certbot.timer)."
echo "Check it end to end with:  sudo certbot renew --dry-run"
