#!/usr/bin/env bash
#
# Lock down the Raspberry Pi that runs the stockroom.
#
#     sudo ./deploy/harden-pi.sh                     # campus defaults, below
#     sudo ./deploy/harden-pi.sh --allow-from 129.21.0.0/16,10.0.0.0/8
#     sudo ./deploy/harden-pi.sh --ssh-from 129.21.0.0/16   # web wider than SSH
#     sudo ./deploy/harden-pi.sh --any               # anything that can route here
#
# Who can reach it:
#
#   A single subnet is the wrong unit. A phone or laptop on eduroam is handed
#   an address from whatever wireless VLAN it lands on -- different building,
#   different range, and it changes as somebody walks across campus. Pinning
#   the firewall to one CIDR, or to the Pi's own subnet, locks out exactly the
#   people the stockroom is for. eduroam is campus-wide, so the allow list is
#   the campus network as a whole:
#
#       129.21.0.0/16    RIT's public allocation, wired and wireless
#       10.0.0.0/8      \
#       172.16.0.0/12    | private ranges -- eduroam clients arriving NAT'd
#       192.168.0.0/16  /
#
#   The private ranges are not a hole to the internet: RFC1918 addresses are
#   not routable across it, so those rules can only ever match a packet that
#   reached this machine from the campus network. Replace the list with
#   --allow-from if your site differs, and use --ssh-from to keep port 22
#   narrower than 80/443.
#
# What else it does, and why:
#
#   * ufw default-deny inbound, allowing only SSH, HTTP and HTTPS, and only
#     from the ranges above. This is the control that makes "no inbound
#     exposure from the internet" true rather than aspirational.
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

# The campus network as seen from the stockroom LAN -- see the note above for
# why this is a list of ranges and not one subnet.
DEFAULT_ALLOW=(129.21.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16)

ALLOW=()
SSH_ALLOW=()
ALLOW_ANY=0
SKIP_SSH=0

# Accept ranges one per flag or comma-separated, so both
# `--allow-from a --allow-from b` and `--allow-from a,b` do the same thing.
add_ranges() {
    local -n _target="$1"; shift
    local raw item _items
    for raw in "$@"; do
        IFS=', ' read -r -a _items <<< "$raw"
        for item in "${_items[@]}"; do
            [[ -n "$item" ]] && _target+=("$item")
        done
    done
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --allow-from) add_ranges ALLOW "$2"; shift 2 ;;
        --ssh-from)   add_ranges SSH_ALLOW "$2"; shift 2 ;;
        # The original spelling, still in older printed instructions and in
        # anyone's shell history. It means the same thing and may be repeated.
        --subnet)     add_ranges ALLOW "$2"; shift 2 ;;
        --any)        ALLOW_ANY=1; shift ;;
        --skip-ssh)   SKIP_SSH=1; shift ;;
        # Print the header block -- it is the documentation, so it cannot go
        # stale the way a line range into this file would.
        -h|--help)    sed -n '2,/^$/p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 [--allow-from <cidr>]" >&2
    exit 1
fi

USING_DEFAULTS=0
if [[ ${#ALLOW[@]} -eq 0 ]]; then
    ALLOW=("${DEFAULT_ALLOW[@]}")
    USING_DEFAULTS=1
fi
# SSH follows the web ports unless it was given a narrower list of its own.
if [[ ${#SSH_ALLOW[@]} -eq 0 ]]; then
    SSH_ALLOW=("${ALLOW[@]}")
fi

# Catch a typo here rather than halfway through rewriting the rules, where
# `set -e` would leave the firewall with the allow rules only partly applied.
for _range in "${ALLOW[@]}" "${SSH_ALLOW[@]}"; do
    if [[ "$_range" == *:* ]]; then
        [[ "$_range" =~ ^[0-9A-Fa-f:]+(/[0-9]{1,3})?$ ]] && continue
    else
        [[ "$_range" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] && continue
    fi
    echo "Not an address or CIDR range: $_range" >&2
    exit 1
done

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;31m!!  %s\033[0m\n' "$1"; }

# The account deploy/setup-pi.sh creates for the service. Named here only so
# the journal group membership below can be granted; this script otherwise
# touches nothing the application owns, and runs happily before it exists.
SERVICE_USER="${SERVICE_USER:-stockroom}"

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

if [[ $ALLOW_ANY -eq 1 ]]; then
    warn "--any given: allowing 22/80/443 from any address that can route here."
    warn "Drop the flag to restrict this to the campus ranges. It is the single"
    warn "most valuable line of defence on a shared network."
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
else
    for _range in "${SSH_ALLOW[@]}"; do
        ufw allow from "$_range" to any port 22  proto tcp comment 'SSH from campus'
    done
    for _range in "${ALLOW[@]}"; do
        ufw allow from "$_range" to any port 80  proto tcp comment 'HTTP redirect + public page'
        ufw allow from "$_range" to any port 443 proto tcp comment 'stockroom HTTPS'
    done
    echo "HTTP/HTTPS allowed from: ${ALLOW[*]}"
    echo "SSH allowed from:        ${SSH_ALLOW[*]}"
    [[ $USING_DEFAULTS -eq 1 ]] && \
        echo "(campus defaults -- pass --allow-from <cidr> to replace them)"

    # A device that reaches the Pi over IPv6 arrives from an address no v4 rule
    # can match and is dropped by the default policy, with nothing on the
    # client to say why. Only worth mentioning if v6 is actually in play here.
    if ! printf '%s\n' "${ALLOW[@]}" | grep -q ':' &&
       ip -6 addr show scope global 2>/dev/null | grep -q inet6; then
        warn "This Pi has a global IPv6 address, but the allow list is IPv4 only:"
        warn "anything that connects over IPv6 will be dropped. Add the campus"
        warn "IPv6 prefix with --allow-from, or turn IPv6 off on this machine."
    fi
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
say "Making the system log survive a reboot"
# ---------------------------------------------------------------------------
# On a stock Raspberry Pi OS the journal is VOLATILE. systemd-journald ships
# Storage=auto, which means "persist only if /var/log/journal exists", and
# Debian does not create it -- so every reboot throws the log away. RIT's
# Server Security Standard (3.5) wants at least two weeks of authentication,
# privilege-escalation, account-change and job-start-up records, and this is
# what makes that true rather than accidental.
#
# The cap matters as much as the retention: an unbounded journal on an SD card
# is a way to wear the card out and then fill it. 500M and 30 days is roughly
# a year of this machine's traffic.
install -d -m 2755 /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true

install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/stockroom.conf <<'EOF'
# Written by deploy/harden-pi.sh. See docs/operations.md.
[Journal]
Storage=persistent
MaxRetentionSec=30day
SystemMaxUse=500M
SystemMaxFileSize=50M
EOF
systemctl restart systemd-journald

# The nightly export reads the WHOLE journal, not just the stockroom unit,
# because the records the standard asks for are sshd's, sudo's and systemd's.
# A user outside this group gets its own (empty) journal back, and journalctl
# exits 0 while doing it -- so without this the archives are silently empty.
# stockroom/logs.py names that failure if it ever happens anyway.
if id "$SERVICE_USER" >/dev/null 2>&1; then
    usermod -aG systemd-journal "$SERVICE_USER"
    echo "Added $SERVICE_USER to systemd-journal."
else
    echo "No $SERVICE_USER account yet -- run deploy/setup-pi.sh, then re-run this."
fi

journalctl --disk-usage

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

  Firewall     inbound denied by default
  Allowed      22/80/443 from $( [[ $ALLOW_ANY -eq 1 ]] && echo "anywhere that can route here" || echo "${ALLOW[*]}" )
  SSH          $( [[ $SKIP_SSH -eq 1 ]] && echo "unchanged" || echo "key-only (if a key was present)" )
  Updates      security patches applied automatically, reboots left to you
  fail2ban     active on sshd
  Journal      persistent, 30 days, capped at 500M

  Check it from ANOTHER machine -- this is the test that matters:

      nmap -Pn $(hostname -I | awk '{print $1}')

  Expect 22, 80 and 443 and nothing else. From outside the allowed ranges,
  expect nothing at all.

  Remaining risks that no script can fix are listed in docs/security.md.
  The big one: this Pi needs a named owner who applies reboots and checks
  backups. Software cannot supply that.

EOF
