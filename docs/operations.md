# Operations

Day-to-day running of the stockroom system.

## Accounts

New people sign up at `/register` and wait for approval — the **Accounts** tab
shows a badge when someone is queued. Full workflow, including what to tell
people when you approve them, is in
[accounts-and-requests.md](accounts-and-requests.md).

The first administrator must be created from a shell on the Pi:

```bash
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom user create \
    --first-name Your --last-name Name --email you@rit.edu --admin
```

There is deliberately no way to do that over the network.

## Everyday tasks

### Check something out

Scan the barcode (or search) on the dashboard, then use the **Check out** box.
Type the borrower's email — known people autocomplete, and anyone new is
created on the spot. A due date is optional; set one and the loan appears in
the overdue list once it passes.

### Take something back

Every open loan has a **Return** button — on the item page, the person's page,
and the "Checked out" list. For a multi-unit loan, change the number before
clicking to record a partial return.

### Add an item

**Items → Add item.** A barcode is assigned automatically unless you type one
(scan the manufacturer's barcode into the field to use that instead). Then
print its label from the item page.

### Retire an item

**Archive** it. Archived items disappear from the lists and the public page but
keep their full history, and can be restored. Nothing is ever deleted. An item
with units still out cannot be archived — get them back first.

## The command line

Run as the service user so file ownership stays correct:

```bash
S="sudo -u stockroom /opt/stockroom/.venv/bin/stockroom"

$S status                      # headline numbers
$S items --low                 # what needs reordering
$S items --out                 # what is lent out
$S loans --overdue             # chase list
$S history --item 12           # one item's full history
$S checkout CIS-000142 alice@rit.edu --qty 2
$S return 45 --qty 1           # partial return of loan 45
$S export > /tmp/stock.csv     # CSV snapshot
$S publish                     # rebuild the public page now
$S backup                      # snapshot the database now
$S prune                       # drop expired sessions and old login attempts

$S user list                   # accounts, pending first
$S user approve an1234@rit.edu
$S user role an1234@rit.edu staff
$S user disable an1234@rit.edu # --enable to reverse
$S user passwd an1234@rit.edu  # reset a password; revokes their sessions
$S sessions revoke an1234@rit.edu
```

Every mutating command takes `--actor "Name <email>"`, which is what lands in
the audit log. Without it, changes are attributed to `cli:<unix user>`.

## Backups

A snapshot is taken nightly at 02:30 into `/var/lib/stockroom/backups`, and
the most recent 30 are kept (`STOCKROOM_BACKUP_KEEP`). It uses SQLite's online
backup API, so it is safe while the service is running — unlike copying the
`.db` file, which can catch a half-written WAL.

```bash
systemctl list-timers stockroom-backup.timer   # when it next runs
sudo systemctl start stockroom-backup.service  # run one now
ls -lh /var/lib/stockroom/backups
```

**Copy backups off the Pi.** They are on the same SD card as the database, so
they do not protect against the card failing — which is the most likely way
this system dies. A cron job on another machine is enough:

```bash
rsync -az stockroom-admin@cis-stockroom.local:/var/lib/stockroom/backups/ ~/stockroom-backups/
```

**Test a restore before you need one.** A backup nobody has restored is a
hypothesis. Once a term, copy a snapshot to a laptop and open it:

```bash
STOCKROOM_DB=~/stockroom-backups/stockroom-20260901T023000Z.db \
    .venv/bin/stockroom status
```

If that prints sensible numbers, the backup is real.

### Restore

```bash
sudo systemctl stop stockroom
sudo -u stockroom cp /var/lib/stockroom/backups/stockroom-<timestamp>.db \
                     /var/lib/stockroom/stockroom.db
sudo systemctl start stockroom
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom publish
```

The CSV export is a second, format-independent safety net — it is readable by
anything, but it holds only current stock, not the history.

## Upgrading

```bash
cd ~/stockroom && git pull
sudo ./deploy/setup-pi.sh      # safe to re-run; never touches the database
```

The schema is created and migrated idempotently at every start.

## Troubleshooting

**The site is unreachable.**
```bash
systemctl status stockroom
journalctl -u stockroom -n 50 --no-pager
curl -s localhost:8000/health
```
If `/health` answers on the Pi but not from your laptop, it is the network or
mDNS — try the IP address directly.

**The public page is stale or missing.** Regenerate it: `$S publish`. If that
works but automatic updates do not, look for `publish failed` in
`journalctl -u stockroom`. The most common cause is the optional GitHub Pages
publisher failing to push; the local page still updates, because publishing
never blocks a change.

**"Database is locked".** Two writers collided and one waited past five
seconds. This should not happen at stockroom scale — check for a second
`stockroom` process (`systemctl status stockroom`; there should be one
uvicorn worker) or a long-running manual `sqlite3` session holding a
transaction open.

**Somebody cannot sign in.** Check, in order: is their account `active`
(`stockroom user list`), and are they locked out after five bad attempts (wait
15 minutes, or check `stockroom history --action auth.login_failed`)? The login
page deliberately gives the same message for every failure, so the history is
where the actual reason lives.

**"That form has expired."** The CSRF token is tied to their session and the
session timed out. Reloading the page and resubmitting fixes it. If it happens
constantly, the clock on the Pi is probably wrong — check `timedatectl`.

**The browser warns about the certificate.** Expected with the self-signed
certificate `setup-pi.sh` generates. See
[security.md](security.md) for trusting it on stockroom machines, or replacing
it with one from ITS.

**The scanner types into the wrong place.** The dashboard's search box is
focused on load; click it and scan again. If the scanner adds stray
characters, it is configured for the wrong keyboard layout — scan the
"US keyboard" configuration barcode in its manual.

**Barcodes will not scan off a printed sheet.** Almost always print scaling.
Reprint at exactly 100% with page scaling off, and check the printer is not
in a draft/toner-saving mode.

**Availability looks wrong.** It is derived, not stored, so it cannot drift:
`available = quantity − sum(open loans)`. If it reads low, something is still
checked out — the item page lists exactly who has what. If someone lost an
item, return the loan with a note and then reduce the total quantity; both
steps are recorded.

## Inspecting the database by hand

```bash
sudo -u stockroom sqlite3 /var/lib/stockroom/stockroom.db
sqlite> .mode box
sqlite> SELECT name, quantity, out_qty, available FROM item_status;
sqlite> SELECT at, actor, action, summary FROM event ORDER BY id DESC LIMIT 20;
```

Read freely. **Do not write** — a direct `UPDATE` bypasses the audit log, which
is the one guarantee this system exists to provide. Use the UI or the CLI.

## Security checks worth doing

```bash
# From ANOTHER machine: only 22, 80 and 443 should answer, and only from
# inside the allowed subnet.
nmap -Pn cis-stockroom.local

# On the Pi: uvicorn must be on 127.0.0.1, never 0.0.0.0.
sudo ss -ltnp | grep 8000

# Who has been signing in, and failing?
$S history --action auth.login
$S history --action auth.login_failed

# Are security updates actually being applied?
sudo unattended-upgrade --dry-run --debug 2>&1 | tail -5
cat /var/run/reboot-required 2>/dev/null && echo "-- a reboot is pending"
```

Reboots are **not** automatic, deliberately: this is a shared tool and it
should not disappear mid-checkout. Someone has to reboot it when a kernel
update lands. That someone should be named in the stockroom's own records —
see the residual risks in [security.md](security.md).
