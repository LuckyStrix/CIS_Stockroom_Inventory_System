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

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .

.venv/bin/stockroom init
.venv/bin/stockroom import examples/sample-inventory.csv --commit
.venv/bin/uvicorn stockroom.web.app:app --reload
```

Then open <http://127.0.0.1:8000/>. The public page is at `/public/`.

Run the tests with `.venv/bin/pytest`.

## Installing on the Pi

```bash
sudo ./deploy/setup-pi.sh
```

Full walkthrough from a blank SD card, including the scanner and the label
printer: **[docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md)**.

## The command line

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
stockroom backup                 # snapshot the database
```

## How it is put together

| Path | What |
|---|---|
| `src/stockroom/service.py` | **All mutations.** Invariants and the audit log |
| `src/stockroom/schema.sql` | Tables and views |
| `src/stockroom/web/` | FastAPI routes and templates |
| `src/stockroom/publish/` | Public page rendering and delivery |
| `src/stockroom/cli.py` | The `stockroom` command |
| `deploy/` | Setup script and systemd units |
| `docs/` | Architecture, data model, operations, Pi setup, SSO plan |

Python 3.11, FastAPI, Jinja2, SQLite. Five runtime dependencies, no ORM, no
build step, no JavaScript framework.

The rule everything else follows:

> Every mutation goes through `service.py`, which writes the change and its
> audit-log row in the same transaction.

More in **[docs/architecture.md](docs/architecture.md)**.

## Security, and what is coming

**There is no authentication yet, deliberately.** Operators type their name
once and it is stored in a cookie; that name is recorded against every change.
This is an accountability mechanism, not an access control — anyone on the
network can claim to be anyone.

That is an appropriate trade for a trusted stockroom LAN, and it is why the
service must not be exposed to the open internet as it stands.

**Next step: RIT single sign-on** via the Shibboleth SAML IdP. All identity
logic already lives in one function (`web/deps.py::current_actor()`), which
already prefers Shibboleth attribute headers when present — so the integration
is mostly configuration plus adding roles. The plan, including what to request
from ITS: **[docs/sso-integration.md](docs/sso-integration.md)**.

## License

Internal tool for the Carlson Center for Imaging Science.
