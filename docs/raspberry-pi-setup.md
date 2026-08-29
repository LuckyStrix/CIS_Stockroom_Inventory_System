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
use that. mDNS is installed in the next step.

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
| Staff UI | `http://cis-stockroom.local:8000/` |
| Public page | `http://cis-stockroom.local:8000/public/` |
| Health check | `http://cis-stockroom.local:8000/health` |

## 5. Load your stock

```bash
# Copy your spreadsheet over as CSV first, then:
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom import stock.csv
# Nothing is written yet. Read the report, then:
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom import stock.csv --commit
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

Print labels for everything from `http://cis-stockroom.local:8000/labels`.
The sheet is laid out for Avery 5160 (3 × 10, 2.625" × 1"). **Print at 100%
scale with page scaling off** — a scaled barcode may not scan.

## 7. Optional: put the public page on port 80

So people can use `http://cis-stockroom.local` with no port number:

```bash
sudo apt install nginx
sudo tee /etc/nginx/sites-available/stockroom >/dev/null <<'EOF'
server {
    listen 80 default_server;
    root /var/lib/stockroom/publish;
    index index.html;

    # The public page is served straight from disk.
    location / { try_files $uri $uri/ =404; }

    # The staff UI lives under /app.
    location /app/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/stockroom /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Optional: mirror to GitHub Pages

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
systemctl status stockroom          # is it running
journalctl -u stockroom -f          # live logs
curl -s localhost:8000/health       # JSON summary
systemctl list-timers stockroom\*   # nightly backup scheduled
```

Day-to-day operations, backup and restore: [operations.md](operations.md).
Real logins: [sso-integration.md](sso-integration.md).
