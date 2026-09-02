# Raspberry Pi setup, from a blank SD card

The stockroom Pi is not set up yet, so this starts at the beginning. Budget
about 45 minutes.

## What you need

- Raspberry Pi 4 or 5 (a Pi 3 works; a Zero 2 W is enough but slow to build)
- 16 GB+ SD card — or, much better, a USB SSD
- Ethernet if you can. Fewer things go wrong than with Wi-Fi
- A USB barcode scanner (any "HID keyboard wedge" model — no driver needed)
- Optionally a label printer, or just print label sheets on the office printer

> **On SD cards:** they wear out, and the failure mode is silent corruption.
> The nightly backup (below) exists because of this. For anything long-lived,
> boot from a USB SSD instead.

## 1. Flash the OS

Use Raspberry Pi Imager. Choose **Raspberry Pi OS Lite (64-bit)** — there is
no need for a desktop.

Before writing, open the settings (the gear icon) and set:

- **Hostname:** `cis-stockroom` — this becomes `cis-stockroom.local`
- **Enable SSH**, with a password or your public key
- **Username:** e.g. `stockroom-admin` (this is your login, not the service account)
- **Wi-Fi**, only if you cannot use Ethernet
- **Locale and timezone** — set the timezone correctly, or the audit log's
  local-time display will be wrong

## 2. First boot

```bash
ssh stockroom-admin@cis-stockroom.local
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

If `cis-stockroom.local` does not resolve, find the Pi's IP on your router and
use that — for SSH now, and for the site later; the app accepts a bare address
as readily as a name. mDNS is installed in the next step.

## 3. Give it a fixed address

The Pi needs to be findable. Either is fine:

- **DHCP reservation (preferred).** Ask RIT ITS, or your router, to always
  hand this MAC address the same IP. Nothing to configure on the Pi.
- **Static IP** in `/etc/dhcpcd.conf`:
  ```
  interface eth0
  static ip_address=129.21.x.y/24
  static routers=129.21.x.1
  static domain_name_servers=129.21.3.17 129.21.4.18
  ```

`avahi-daemon` (installed by the setup script) makes `cis-stockroom.local`
work on the LAN regardless.

**But mDNS does not cross a VLAN.** A phone on eduroam is on a different
wireless segment from the stockroom's wired one, so `cis-stockroom.local` will
not resolve for it however well it works from the bench next to the Pi. Those
devices need either the Pi's IP address or a real DNS name from ITS — which is
the reason to pin the address here rather than let DHCP move it.

## 4. Install the application

Copy this repository to the Pi and run the installer:

```bash
git clone <your-repo-url> stockroom     # or: rsync -a ./ pi:stockroom/
cd stockroom
sudo ./deploy/setup-pi.sh
```

It installs packages, creates the `stockroom` service account, builds a
virtualenv in `/opt/stockroom`, initialises the database in
`/var/lib/stockroom`, and enables the service plus the nightly backup timer.
It is safe to re-run to upgrade.

When it finishes:

| | |
|---|---|
| Staff UI | `https://cis-stockroom.local/` |
| Public page | `https://cis-stockroom.local/public/` (also plain HTTP) |
| Health check | `https://cis-stockroom.local/health` |

The installer prints the Pi's IP address alongside these. Both work: the app
answers to its own hostname, that name with `.local`, and any bare IP address.
Phones and Chromebooks often cannot resolve `.local` at all, so the address is
what you give people — which is why step 3 matters.

If a browser gets **`Invalid host header`**, it reached the Pi under a name the
app does not know (a DNS alias, say). The message names the header it refused
and the hosts it accepts; add yours to `STOCKROOM_ALLOWED_HOSTS` in
`/etc/stockroom.env` and `sudo systemctl restart stockroom`.

Your browser will warn about the certificate — it is self-signed. That is
expected; [security.md](security.md) covers trusting it, and why an
ITS-issued certificate is the better answer.

## 4b. Create the first administrator, and lock the Pi down

Two steps the installer deliberately does not do for you.

```bash
# There is no way to make an admin over the network. This is on purpose.
# `stockroom` here is /usr/local/bin/stockroom, which setup-pi.sh installed;
# it runs the CLI as the service account and will ask for your password.
stockroom user create \
    --first-name Your --last-name Name --email you@rit.edu --admin

# Firewall, key-only SSH, automatic security updates, fail2ban.
# With no arguments it allows 22/80/443 from the campus network -- RIT's
# 129.21.0.0/16 plus the private ranges eduroam clients are NAT'd behind.
sudo ./deploy/harden-pi.sh
```

Deliberately not a single subnet: eduroam hands a device an address from
whichever wireless VLAN it happens to be on, so a one-CIDR rule locks out the
phones and laptops this is for. Elsewhere, or to keep SSH tighter than the
web ports:

```bash
sudo ./deploy/harden-pi.sh --allow-from 10.0.0.0/8,172.16.0.0/12 \
                           --ssh-from 129.21.5.0/24
```

`./deploy/harden-pi.sh --help` lists the rest.

`harden-pi.sh` will refuse to disable password SSH unless you already have a
key installed, rather than locking you out of your own Pi. Run
`ssh-copy-id user@cis-stockroom.local` first if you have not.

Then verify from **another machine** — this is the check that matters:

```bash
nmap -Pn cis-stockroom.local     # expect 22, 80, 443 and nothing else
```

Everyone else signs up at `https://cis-stockroom.local/register` and waits for
you to approve them.

## 5. Load your stock

```bash
# Copy your spreadsheet over as CSV first, then:
stockroom import stock.csv
# Nothing is written yet. Read the report, then:
stockroom import stock.csv --commit
```

Columns: `name, description, quantity, unit, shelf, sub_location, barcode,
product_url, min_quantity`. Only `name` is required, and common spellings
("Qty", "Storage Unit", "Product Link") are understood. See
[`examples/sample-inventory.csv`](../examples/sample-inventory.csv).

## 6. The barcode scanner

Plug it in. There is nothing to configure — these scanners type the barcode
and press Enter, exactly like a keyboard.

Test it: open the dashboard, scan a label, and the page should jump to that
item. The search box is focused automatically for this reason, so scanning
works the moment the page loads.

Print labels for everything from `https://cis-stockroom.local/labels`.
The sheet is laid out for Avery 5160 (3 × 10, 2.625" × 1"). **Print at 100%
scale with page scaling off** — a scaled barcode may not scan.

## 7. Optional: mirror to GitHub Pages

To make the page readable from off campus, clone a Pages repo somewhere the
service user can write and give it push credentials (a deploy key is
simplest):

```bash
sudo -u stockroom git clone git@github.com:<org>/<repo>.git /var/lib/stockroom/pages
sudo sed -i 's|^# STOCKROOM_GITHUB_PAGES_DIR=.*|STOCKROOM_GITHUB_PAGES_DIR=/var/lib/stockroom/pages|' /etc/stockroom.env
sudo systemctl restart stockroom
```

Every change now also commits and pushes the regenerated page. If the push
fails the local page still updates and the error is logged — publishing never
blocks a checkout.

## Checks and next steps

```bash
systemctl status stockroom nginx    # both running?
journalctl -u stockroom -f          # live logs
curl -sk https://localhost/health   # JSON summary
systemctl list-timers stockroom\*   # nightly backup scheduled
sudo ss -ltnp | grep 8000           # uvicorn must be on 127.0.0.1 only
```

Day-to-day operations, backup and restore: [operations.md](operations.md).
Accounts and the request workflows:
[accounts-and-requests.md](accounts-and-requests.md).
Security posture and residual risks: [security.md](security.md).
RIT single sign-on, when you are ready: [sso-integration.md](sso-integration.md).
