#!/usr/bin/env bash
# certbot's pre, post and deploy hooks for the stockroom.
#
#     stockroom-acme-hook.sh open      (pre-hook)    port 80 open to everyone
#     stockroom-acme-hook.sh close     (post-hook)   ...and campus-only again
#     stockroom-acme-hook.sh install   (deploy-hook) new cert -> nginx
#
# Installed by deploy/enable-letsencrypt.sh, which names it in certbot's
# renewal config. Nothing else should call it.
#
# Why the firewall moves at all: harden-pi.sh only accepts port 80 from the
# campus ranges, and Let's Encrypt validates from servers outside RIT whose
# addresses it deliberately does not publish. So HTTP-01 can only succeed while
# port 80 is open to the world, and the stockroom's policy is that it is not.
# Opening it for the few seconds of a renewal, roughly every two months, keeps
# both. Port 443 and SSH are never touched.
#
# What the world can reach in that window is nginx's port-80 block: the
# challenge file, the redirect to HTTPS (which ufw still refuses from outside)
# and /public/, which is the page meant for the corridor wall anyway.
set -euo pipefail

# The marker is what makes `close` safe. harden-pi.sh run with no ranges adds
# its own unrestricted `allow 80/tcp`; a close that deleted that rule
# unconditionally would quietly shut the stockroom's own HTTP off campus-wide
# at the first renewal. So `close` removes the rule only if `open` added it.
MARKER="${STOCKROOM_ACME_MARKER:-/run/stockroom-acme-opened-port-80}"
CERT_DIR="${STOCKROOM_CERT_DIR:-/etc/ssl/stockroom}"

open_port() {
    if ufw status | grep -Eq '^80/tcp +ALLOW( IN)? +Anywhere'; then
        return 0    # already open to everyone, and not ours to close later
    fi
    # Every rule harden-pi.sh writes is an ALLOW, so position does not matter
    # -- and `insert 1` fails outright on the IPv6 table, which is empty.
    ufw allow 80/tcp comment 'ACME challenge (temporary)' >/dev/null
    touch "$MARKER"
}

close_port() {
    # A post-hook runs whether or not the renewal succeeded, which is the
    # point: a failed renewal must not leave port 80 open until the next one.
    [[ -f "$MARKER" ]] || return 0
    ufw delete allow 80/tcp >/dev/null
    rm -f "$MARKER"
}

install_cert() {
    # Copied rather than pointing nginx at /etc/letsencrypt/live, so that the
    # nginx config, setup-pi.sh and the self-signed fallback all keep reading
    # the one path they always have. Swapping back to self-signed is just
    # deleting these two files and re-running setup-pi.sh.
    : "${RENEWED_LINEAGE:?certbot sets this for a deploy hook}"
    install -d -m 0755 "$CERT_DIR"
    # Keep the self-signed certificate the first time, so there is something
    # to go back to that every stockroom machine may already trust.
    if [[ -f "$CERT_DIR/stockroom.crt" && ! -f "$CERT_DIR/stockroom.crt.self-signed" ]]; then
        local issuer subject
        issuer="$(openssl x509 -in "$CERT_DIR/stockroom.crt" -noout -issuer | sed 's/^issuer=//')"
        subject="$(openssl x509 -in "$CERT_DIR/stockroom.crt" -noout -subject | sed 's/^subject=//')"
        if [[ "$issuer" == "$subject" ]]; then
            cp -p "$CERT_DIR/stockroom.crt" "$CERT_DIR/stockroom.crt.self-signed"
            cp -p "$CERT_DIR/stockroom.key" "$CERT_DIR/stockroom.key.self-signed"
        fi
    fi
    # Write beside and rename, so nginx never reads a key that does not match
    # the certificate next to it -- a reload between the two copies would fail.
    install -m 0644 -o root -g root "$RENEWED_LINEAGE/fullchain.pem" "$CERT_DIR/.stockroom.crt.new"
    install -m 0640 -o root -g www-data "$RENEWED_LINEAGE/privkey.pem" "$CERT_DIR/.stockroom.key.new"
    mv -f "$CERT_DIR/.stockroom.key.new" "$CERT_DIR/stockroom.key"
    mv -f "$CERT_DIR/.stockroom.crt.new" "$CERT_DIR/stockroom.crt"
    if nginx -t 2>/dev/null; then
        systemctl reload nginx
    else
        echo "nginx rejected the configuration; the new certificate is in place" \
             "but nginx is still serving the old one." >&2
        exit 1
    fi
}

case "${1:-}" in
    open)    open_port ;;
    close)   close_port ;;
    install) install_cert ;;
    *) echo "usage: $0 open|close|install" >&2; exit 2 ;;
esac
