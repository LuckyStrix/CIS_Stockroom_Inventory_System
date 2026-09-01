# CIS Stockroom Inventory System

Inventory, checkout and audit tracking for the stockroom at the **Carlson
Center for Imaging Science, RIT**.

Runs on a Raspberry Pi in the stockroom. Staff use a web UI on the RIT
network to check equipment in and out; everyone else gets a read-only page
showing what is in stock and what is available, regenerated automatically on
every change.

```
   scan a barcode  ─────>  check out to a person  ─────>  public page updates
        │                          │                            │
        └──────────  every step recorded in the audit log  ──────┘
```

## What it does

- **Track items** — name, description, quantity, product link, barcode, and a
  three-level location (storage unit → shelf → optional bin/drawer/case).
- **Check items in and out** — to a person identified by name and email, with
  an optional due date. Ten SD cards is one item: three can be out to one
  person and two to another, with five still on the shelf.
- **Record everything** — every change, who made it, when, and what the values
  were before and after. Nothing is ever deleted; retired items are archived
  and keep their history.
- **Publish a public page** — a self-contained, searchable HTML page plus a
  JSON feed, rebuilt on every change. It shows availability counts, never
  who is holding what.
- **Print barcode labels** — Code128 labels laid out for Avery 5160 sheets.
  Any USB scanner works; they behave as keyboards.
- **Import your existing spreadsheet** — CSV import with a dry run first.
- **Track individual units** — for gear where it matters *which* one. Each
  camera body gets its own asset tag, so a fault is recorded against the right
  one rather than against "the cameras".
- **Record what is broken** — damaged, in repair, missing or written off, with
  a note and a link to the loan it came back on. Availability drops; the
  quantity owned does not, so the shelf can say "we bought ten and two are
  unaccounted for".
- **Work a counter queue** — scan a whole basket, or a saved kit, and check it
  all out to one person in a single transaction. Returns work the same way.
- **Photograph the confusing ones** — "is this the right cable?" is a photo
  question. Uploads are downscaled and stripped of EXIF on the way in; they
  stay internal and never reach the public page.
- **Count the shelves** — a stocktake walks the room with a scanner and reports
  where the shelves and the database disagree. It is the only thing that
  catches drift, because drift never went through the counter.
- **See what a year adds up to** — most borrowed, never borrowed, typical time
  out, what is unaccounted for. The material for "what should we buy next?".
- **Accounts and requests** — people sign up with their RIT email, staff
  approve them, and they can then ask to borrow equipment, suggest something
  the stockroom should own, or ask for the room to be open at a particular
  time. Confirmed open hours appear on the public page.

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .

.venv/bin/stockroom init
.venv/bin/stockroom user create --first-name Your --last-name Name \
    --email you@rit.edu --admin
.venv/bin/stockroom import examples/sample-inventory.csv --commit
.venv/bin/uvicorn stockroom.web.app:app --reload
```

Then open <http://127.0.0.1:8000/> and sign in. The public page is at
`/public/` and needs no account.

Run the tests with `.venv/bin/pytest`.

## Installing on the Pi

```bash
sudo ./deploy/setup-pi.sh
```

Full walkthrough from a blank SD card, including the scanner and the label
printer: **[docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md)**.

## The command line

On the Pi this is `/usr/local/bin/stockroom`, installed by `setup-pi.sh`: a
wrapper that runs the real CLI as the `stockroom` service account. In a
checkout it is `.venv/bin/stockroom`.

```bash
stockroom status                 # headline numbers
stockroom items --low            # what needs reordering
stockroom loans --overdue        # chase list
stockroom checkout CIS-000142 alice@rit.edu --qty 2
stockroom return 45 --qty 1      # partial return
stockroom history --item 12      # one item's full history
stockroom import stock.csv       # dry run; add --commit to apply
stockroom export > backup.csv
stockroom publish                # rebuild the public page
stockroom backup                 # snapshot it, verify it, send it off the Pi
stockroom doctor                 # is any of this still working?
stockroom report                 # usage, and what nobody borrows
```

## How it is put together

| Path | What |
|---|---|
| `src/stockroom/service.py` | Inventory mutations. Invariants and the audit log |
| `src/stockroom/kits.py` | Named bundles, expanded into a basket at the counter |
| `src/stockroom/stocktake.py` | Physical counts and reconciliation |
| `src/stockroom/reports.py` | Usage figures and server-rendered SVG charts |
| `src/stockroom/diagnostics.py` | The health checks behind `stockroom doctor` |
| `src/stockroom/backup_targets.py` | Getting a snapshot off the SD card |
| `src/stockroom/accounts.py` | Accounts, passwords, sessions, lockout |
| `src/stockroom/requests_service.py` | The three request workflows |
| `src/stockroom/security.py` | Hashing, tokens, CSRF, rate limiting |
| `src/stockroom/schema.sql` | Tables and views |
| `src/stockroom/web/` | FastAPI routes and templates |
| `src/stockroom/publish/` | Public page rendering and delivery |
| `src/stockroom/cli.py` | The `stockroom` command |
| `deploy/` | Setup and hardening scripts, nginx config, systemd units |
| `docs/` | Architecture, data model, security, operations, Pi setup, SSO plan |

Python 3.11, FastAPI, Jinja2, SQLite. Six runtime dependencies, no ORM, no
build step, no JavaScript framework. Password hashing, session tokens, CSRF
and rate limiting are all standard library — authentication added **zero** new
dependencies.

The strict CSP shapes more than the security story: with no inline script and
no external origin allowed, charts are server-rendered SVG rather than a
charting library, and the counter's basket lives in hidden form fields rather
than in JavaScript. Both turned out simpler than the thing they replaced.

The rule everything else follows:

> Every mutation goes through a service module, which writes the change and
> its audit-log row in the same transaction.

The audit log is also a hash chain: each entry covers the one before it, so an
edited or deleted row is detectable rather than silent. `stockroom doctor`
checks it, and the head hash is published in `inventory.json` and `/health`.

More in **[docs/architecture.md](docs/architecture.md)**.

## Security

The Pi has **no inbound exposure to the internet** — no port forwarding, no
public DNS, no public IP. It is reachable from the RIT network over HTTPS,
behind a default-deny firewall, with the app itself bound to loopback behind
nginx. Off-campus staff come in over the RIT VPN.

Everything except the public inventory page requires an account. Passwords are
hashed with scrypt; sessions are server-side and revocable; every unsafe
request carries a CSRF token; a strict CSP with per-request nonces means there
is no inline JavaScript anywhere. Two tests walk the real route table and fail
the build if any route is reachable anonymously or any `POST` accepts a
request without a CSRF token.

Read **[docs/security.md](docs/security.md)** before deploying it — including
the residual risks, which are stated plainly rather than glossed over.

**Still to come: RIT single sign-on** via the Shibboleth SAML IdP, which
removes the password liability entirely. Identity already lives in one function
(`web/deps.py::current_account()`) and roles already exist, so the work is
mostly configuration. See **[docs/sso-integration.md](docs/sso-integration.md)**.

## License

MIT — see [LICENSE](LICENSE).

Built for the stockroom at RIT's Carlson Center for Imaging Science, but
nothing in it is specific to that room: it is a general small-inventory system
with an audit trail, and the RIT-specific parts are the default organisation
name and the `@rit.edu` sign-up check, both of which are configuration.
