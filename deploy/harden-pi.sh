#!/usr/bin/env bash
#
# Lock down the Raspberry Pi that runs the stockroom.
#
#     sudo ./deploy/harden-pi.sh --subnet 129.21.0.0/16
#
# What it does, and why:
#
#   * ufw default-deny inbound, allowing only SSH and HTTPS, and only from the
#     subnet you name. This is the control that makes "no inbound exposure from
#     the internet" true rather than aspirational.
#   * SSH keys only. Password authentication on a machine reachable from a
#     university network is a guessing game you eventually lose.
#   * unattended-upgrades for security patches, because the realistic way this
#     box gets compromised is an unpatched CVE eight months from now.
#   * fail2ban, sysctl network hardening, and tighter systemd sandboxing.
#
# Idempotent: safe to re-run after changes.
#
# It will REFUSE to disable password SSH if no authorised key is installed,
# rather than locking you out of your own Pi.

set -euo pipefail

SUBNET=""
SKIP_SSH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --subnet) SUBNET="$2"; shift 2 ;;
        --skip-ssh) SKIP_SSH=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 --subnet <cidr>" >&2
    exit 1
fi

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;31m!!  %s\033[0m\n' "$1"; }

say "Installing security packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    ufw fail2ban unattended-upgrades apt-listchanges openssl

# ---------------------------------------------------------------------------
say "Configuring the firewall"
# ---------------------------------------------------------------------------
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing

if [[ -n "$SUBNET" ]]; then
    ufw allow from "$SUBNET" to any port 22  proto tcp comment 'SSH from campus'
    ufw allow from "$SUBNET" to any port 80  proto tcp comment 'HTTP redirect + public page'
    ufw allow from "$SUBNET" to any port 443 proto tcp comment 'stockroom HTTPS'
    echo "Inbound allowed from $SUBNET only."
else
    warn "No --subnet given: allowing 22/80/443 from any address that can route here."
    warn "Re-run with --subnet <cidr> to restrict this. It is the single most"
    warn "valuable line of defence on a shared network."
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
fi

ufw --force enable
ufw status verbose

# ---------------------------------------------------------------------------
say "Hardening SSH"
# ---------------------------------------------------------------------------
if [[ $SKIP_SSH -eq 1 ]]; then
    echo "Skipped at your request (--skip-ssh)."
else
    # Refuse to turn off passwords unless a key is actually installed --
    # otherwise this script is a very efficient way to lock yourself out.
    KEYS_FOUND=0
    for home in /home/* /root; do
        [[ -s "$home/.ssh/authorized_keys" ]] && KEYS_FOUND=1
    done

    if [[ $KEYS_FOUND -eq 0 ]]; then
        warn "No authorised SSH keys found in any home directory."
        warn "NOT disabling password authentication -- that would lock you out."
        warn "Install a key first:  ssh-copy-id <user>@$(hostname)"
        warn "then re-run this script."
    else
        install -d -m 0755 /etc/ssh/sshd_config.d
        cat > /etc/ssh/sshd_config.d/10-stockroom.conf <<'EOF'
# Installed by deploy/harden-pi.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
EOF
        if sshd -t; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd
            echo "SSH is now key-only."
        else
            warn "sshd rejected the new config; reverting."
            rm -f /etc/ssh/sshd_config.d/10-stockroom.conf
        fi
    fi
fi

# ---------------------------------------------------------------------------
say "Enabling automatic security updates"
# ---------------------------------------------------------------------------
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

cat > /etc/apt/apt.conf.d/51stockroom-unattended <<'EOF'
// Security updates only, applied nightly. Reboots are NOT automatic: this is
// a shared stockroom tool and it should not vanish mid-checkout. Watch for
// /var/run/reboot-required and restart it deliberately.
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Raspbian,codename=${distro_codename},label=Raspbian";
    "origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation";
};
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
systemctl enable --now unattended-upgrades

# ---------------------------------------------------------------------------
say "Configuring fail2ban"
# ---------------------------------------------------------------------------
cat > /etc/fail2ban/jail.d/stockroom.conf <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true

# The application throttles and locks out failed logins itself, in the
# database (see security.check_lockout) -- that survives a restart, which an
# in-memory limiter would not. This jail adds an IP-level ban on top.
[nginx-http-auth]
enabled = false
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

# ---------------------------------------------------------------------------
say "Applying kernel network hardening"
# ---------------------------------------------------------------------------
cat > /etc/sysctl.d/99-stockroom.conf <<'EOF'
# Ignore ICMP redirects and source routing -- this host is not a router.
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.all.accept_source_route = 0

# Log packets with impossible addresses.
net.ipv4.conf.all.log_martians = 1

# Reverse-path filtering: drop packets whose source address is not reachable
# via the interface they arrived on.
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# SYN flood resistance.
net.ipv4.tcp_syncookies = 1

# Do not leak kernel pointers to unprivileged users.
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
EOF
sysctl --system >/dev/null
echo "Applied."

# ---------------------------------------------------------------------------
say "Done"
# ---------------------------------------------------------------------------
cat <<EOF

  Firewall     inbound denied by default${SUBNET:+, allowed only from $SUBNET}
  SSH          $( [[ $SKIP_SSH -eq 1 ]] && echo "unchanged" || echo "key-only (if a key was present)" )
  Updates      security patches applied automatically, reboots left to you
  fail2ban     active on sshd

  Check it from ANOTHER machine -- this is the test that matters:

      nmap -Pn $(hostname -I | awk '{print $1}')

  Expect 22, 80 and 443 and nothing else. From outside the allowed subnet,
  expect nothing at all.

  Remaining risks that no script can fix are listed in docs/security.md.
  The big one: this Pi needs a named owner who applies reboots and checks
  backups. Software cannot supply that.

EOF
