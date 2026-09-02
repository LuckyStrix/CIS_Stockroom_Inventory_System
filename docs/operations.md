# Operations

Day-to-day running of the stockroom system.

## Accounts

New people sign up at `/register` and wait for approval — the **Accounts** tab
shows a badge when someone is queued. Full workflow, including what to tell
people when you approve them, is in
[accounts-and-requests.md](accounts-and-requests.md).

The first administrator must be created from a shell on the Pi:

```bash
stockroom user create \
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

### Save a kit

**Kits.** A kit is a named list of items that habitually go out together — a
camera, its charger, two batteries and a card. Build one once, and the counter
drops the whole list into the basket with one click.

Nothing is ever lent "as a kit". The lines are expanded into ordinary basket
rows and the kit is then forgotten, so a borrower who does not need the tripod
can have it removed before checkout, and each line returns on its own. That
also means a kit naming something out of stock is not an error — the basket
just shows what could not be added.

Kits are archived rather than deleted, like items.

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

### Add a photo

On the item page, **Add a photo**. "Is this the right cable?" is a photo
question, and a picture of the connector answers it better than any
description will.

Uploads are re-encoded on the way in: decoded, downscaled to 1600px on the
longest edge, stripped of EXIF and written out as a fresh JPEG
(`STOCKROOM_PHOTO_MAX_PIXELS` if that is the wrong size). A phone photo
arrives at 3-5 MB and lands around 200 KB, which matters on an SD card — and
re-encoding is also what removes the GPS coordinates a phone attaches.

The first photo becomes the main one; **Make main** changes that. Photos are
internal only — they are never copied onto the public page, which stays a
single self-contained file with no asset directory.

Photo files live in `PHOTO_DIR` (`/var/lib/stockroom/photos`), *not* in the
database, so they are not in the nightly snapshot. Back them up separately if
they matter to you.

### Track individual units

Most of the stockroom is countable and needs none of this. But where it
matters *which* body came back with a bent mount, mark the item tracked and
**Register a unit** for each physical object, giving each one its own asset
tag — a scannable code separate from the item barcode. Scanning the item says
*what*; scanning the tag says *which one*.

Checkout can then name the unit, and its faults, repairs and history follow
that object rather than the pile. **Retire** a unit that has left the building
for good.

A unit loan is quantity 1 by definition, so it can never be partially
returned, and one unit cannot be lent to two people even where the arithmetic
would allow it.

### See what the year adds up to

**Reports.** Most borrowed, never borrowed, typical time out, busiest weeks,
busiest borrowers, returned late most often, unaccounted for, and what is
waiting on a repair. The window is a year by default — `?days=` on the page,
`--days` on the CLI.

This is the material for "what should we buy next?" — and the
never-borrowed list is the one that argues for shelf space back. The charts
are server-rendered SVG rather than a charting library, because the CSP
forbids loading one.

### Retire an item

**Archive** it. Archived items disappear from the lists and the public page but
keep their full history, and can be restored. Nothing is ever deleted. An item
with units still out cannot be archived — get them back first.

## The command line

`setup-pi.sh` installs `/usr/local/bin/stockroom`, so the command is just
`stockroom` on the Pi. It is a wrapper: the real CLI is in the venv at
`/opt/stockroom/.venv/bin/stockroom`, which is on nobody's `PATH`, and it has
to run as the `stockroom` service user — running it as root leaves a
root-owned WAL beside the database and the service then fails every write with
`attempt to write a readonly database`. The wrapper does the `sudo -u` for
you, so it will ask for *your* password.

Aliases do not expand in non-interactive shells, and neither `/usr/local/bin`
nor the wrapper's `sudo` is a safe assumption inside a systemd unit: write the
full `/opt/stockroom/.venv/bin/stockroom` path there, as
`deploy/stockroom-backup.service` does.

```bash
stockroom status                      # headline numbers
stockroom items --low                 # what needs reordering
stockroom items --out                 # what is lent out
stockroom loans --overdue             # chase list
stockroom history --item 12           # one item's full history
stockroom checkout CIS-000142 alice@rit.edu --qty 2
stockroom return 45 --qty 1           # partial return of loan 45
stockroom export > /tmp/stock.csv     # CSV snapshot
stockroom publish                     # rebuild the public page now
stockroom backup                      # snapshot the database now
stockroom prune                       # drop expired sessions and old login attempts
stockroom doctor                      # health checks; non-zero if something is wrong
stockroom report --days 365           # usage figures, same numbers as /reports

stockroom user list                   # accounts, pending first
stockroom user approve an1234@rit.edu
stockroom user role an1234@rit.edu staff
stockroom user disable an1234@rit.edu # --enable to reverse
stockroom user passwd an1234@rit.edu  # reset a password; revokes their sessions
stockroom sessions revoke an1234@rit.edu
```

Every mutating command takes `--actor "Name <email>"`, which is what lands in
the audit log. **Pass it.** Without it the change is attributed to
`cli:<unix user>`, and since the command runs as the service user either way,
that is `cli:stockroom` for everyone who has a shell on the Pi — which
identifies nobody.

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

`STOCKROOM_ALLOWED_HOSTS` is the one worth knowing about before it bites: the
app refuses a request whose `Host` it does not recognise, which is what an
`Invalid host header` page means. It defaults to this machine's hostname, that
name with `.local`, loopback and any bare IP address, so it is normally not
something you set — a DNS alias or a CNAME is the case that needs it. The
refusal names the host it turned away and the ones it would accept.

The `stockroom` CLI reads the same file, so a command run by hand uses the same
database and directories as the service. It has to: nothing hands a `sudo -u
stockroom stockroom ...` invocation an environment, and before it did, the CLI
fell back to its development defaults under `/opt/stockroom/data` and failed
with `PermissionError: [Errno 13] Permission denied: '/opt/stockroom/data'`.
Point it somewhere else for a one-off with `STOCKROOM_ENV_FILE=`, or by setting
the variable itself — a real environment variable always beats the file.

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

> **Re-run `deploy/setup-pi.sh` after setting this.** The backup unit runs
> under `ProtectSystem=strict`, which makes everything outside
> `/var/lib/stockroom` read-only to it — including the stick. The installer
> reads this variable and writes
> `/etc/systemd/system/stockroom-backup.service.d/backup-copy-dir.conf`
> granting the path. Without that drop-in the nightly copy fails with a
> read-only-filesystem error while the same command run by hand from an SSH
> session works perfectly, which is a miserable thing to debug.
> `stockroom doctor` names this specific failure if it happens.

The copy is written under a `.part` name, checked with SQLite's integrity
check, and only then renamed into place — so a stick pulled out mid-write
leaves no truncated file wearing a backup's name.

**Google Drive, or anything else rclone speaks.** rclone is a single binary
with its own OAuth flow; the application never handles a token. Set it up once
as the service user, because that is where the app expects the config:

```bash
sudo apt install rclone
sudo -u stockroom rclone config          # create the remote, do the OAuth
stockroom backup
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

The nightly unit runs `backup`, then `prune`, then `doctor`, and the first two
are prefixed with `-` so systemd carries on past a failure. That is
deliberate: `Type=oneshot` otherwise stops at the first failing step, and an
unplugged USB stick used to take the health check down with it — so the one
night the audit chain or the database integrity most needed looking at was the
night nothing looked. `doctor` runs last and unprefixed, so its exit code is
still what marks the unit failed.

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
stockroom publish
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

**The public page is stale or missing.** Regenerate it: `stockroom publish`. If that
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
`available = quantity − units on loan − units held out of service`. If it
reads low, either something is still checked out — the item page lists exactly
who has what — or a unit is on hold as broken, in repair, missing or written
off, which the item page also shows. Note that a hold deliberately does *not*
reduce `quantity`: "we own ten, two are unaccounted for" is the sentence that
gets a replacement funded.

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
# inside the allowed ranges (harden-pi.sh --allow-from; campus-wide by default).
nmap -Pn cis-stockroom.local

# On the Pi: uvicorn must be on 127.0.0.1, never 0.0.0.0.
sudo ss -ltnp | grep 8000

# Who has been signing in, and failing?
stockroom history --action auth.login
stockroom history --action auth.login_failed

# Are security updates actually being applied?
sudo unattended-upgrade --dry-run --debug 2>&1 | tail -5
cat /var/run/reboot-required 2>/dev/null && echo "-- a reboot is pending"
```

Reboots are **not** automatic, deliberately: this is a shared tool and it
should not disappear mid-checkout. Someone has to reboot it when a kernel
update lands. That someone should be named in the stockroom's own records —
see the residual risks in [security.md](security.md).
