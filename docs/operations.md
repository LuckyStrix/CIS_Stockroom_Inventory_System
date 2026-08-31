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

### Work a queue at the counter

**Counter.** Scan everything into the basket, say who it is for, check out.
The whole basket is one transaction: if any line cannot go out, none of it
does and the basket is still on screen to be corrected. A saved **kit** drops
its whole contents in with one click.

**Counter → Returns** does the same in reverse: pick the person, untick
anything they are keeping, and hand back the rest in one go.

Something that comes back damaged is better returned from the item page or
with the condition dropdown on the return row, so the fault is recorded
against the right loan — that is what makes "who had it when it broke"
answerable later.

### Say that something is broken

On the item page, **Record a problem**: broken, in repair, missing, or written
off. Those units stop being lendable and the public page stops advertising
them, but the quantity owned does not change — the stockroom still bought ten
of them, and "we own ten, two are unaccounted for" is the sentence that gets a
replacement budgeted.

Put it back with **Back in service** when it returns from repair.

### Count the shelves

**Stocktake.** Pick a storage unit (a whole room at once is rarely realistic),
then walk it with a scanner. When you finish, you get a list of everything the
shelves and the database disagree about.

Nothing is applied automatically. The most common cause of a missing scan is a
missed scan, so recording something as missing is a deliberate click — and it
opens a hold you can close again the moment the thing turns up.

### Check the system is still alive

**Health** in the navigation, or `stockroom doctor` on the Pi. It runs
nightly as part of the backup job, so a failure shows up in
`systemctl status stockroom-backup`. It checks the database, the audit chain,
the search index, disk space, and whether backups are actually happening —
including whether any of them are leaving the machine.

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

## Configuration

Settings live in `/etc/stockroom.env`, installed from
`deploy/stockroom.env.example`. **Quote every value.** The file is read by two
parsers that do not agree: systemd takes everything after the first `=`
literally, so an unquoted value containing spaces works in the running service
but breaks any shell that reads the same file. Double quotes are right for
both — systemd strips them.

```bash
STOCKROOM_ORG="Carlson Center for Imaging Science — RIT"   # correct
STOCKROOM_ORG=Carlson Center for Imaging Science — RIT     # breaks the installer
```

`sudo systemctl restart stockroom` after editing it.

## Backups

A snapshot is taken nightly at 02:30 into `/var/lib/stockroom/backups`, and
the most recent 30 are kept (`STOCKROOM_BACKUP_KEEP`). It uses SQLite's online
backup API, so it is safe while the service is running — unlike copying the
`.db` file, which can catch a half-written WAL.

Every snapshot is **verified** before it counts: the new file is reopened and
run through `PRAGMA integrity_check`, and a snapshot that fails is deleted
rather than kept. A database corrupted by a failing SD card copies without
complaint, so an unverified nightly job produces a month of unusable files and
reports success every single night.

```bash
systemctl list-timers stockroom-backup.timer   # when it next runs
sudo systemctl start stockroom-backup.service  # run one now
ls -lh /var/lib/stockroom/backups
```

### Getting a copy off the SD card

Backups on the same card as the database do not protect against the card
failing, which is the most likely way this system dies. Set either or both of
these in `/etc/stockroom.env`; `stockroom doctor` warns while neither is
configured.

**A USB stick left in the Pi:**

```bash
STOCKROOM_BACKUP_COPY_DIR=/mnt/stockroom-usb
```

**Google Drive, or anything else rclone speaks.** rclone is a single binary
with its own OAuth flow; the application never handles a token. Set it up once
as the service user, because that is where the app expects the config:

```bash
sudo apt install rclone
sudo -u stockroom rclone config          # create the remote, do the OAuth
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom backup
```

```bash
STOCKROOM_BACKUP_REMOTE=gdrive:stockroom-backups
STOCKROOM_BACKUP_REMOTE_KEEP=30
```

> **What ends up in that Drive folder is a readable copy of the whole
> database** — every email address and the entire audit log. Keep it private
> to the account that owns it, and do not share the link.

An upload failure never invalidates the local snapshot, but it does make the
command exit non-zero, so a Drive upload that has quietly stopped working
shows up as a failed unit rather than as silence.

Pulling from another machine still works too, and needs nothing on the Pi:

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
