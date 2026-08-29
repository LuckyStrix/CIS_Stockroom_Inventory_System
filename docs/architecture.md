# Architecture

## The one rule

> **Every mutation goes through `service.py`, which writes the change and its
> audit-log row in the same transaction. Nothing else writes to `item`,
> `loan` or `person`.**

This is the design decision the rest of the system is arranged around. Because
the change and its `event` row commit together:

- a change cannot land without history, and
- a history entry cannot describe a change that rolled back.

`tests/test_audit.py` enforces it. `test_no_mutating_function_is_missing_from_the_table`
fails if a new mutating function is added to `service.py` without being
audit-tested, so the rule survives future work rather than depending on
someone remembering it.

## Layers

```
                    ┌──────────────┐   ┌──────────┐
      browser ─────>│  web/ (HTTP) │   │  cli.py  │<──── terminal, cron
                    └──────┬───────┘   └────┬─────┘
                           │                │
                           └───────┬────────┘
                                   v
                        ┌──────────────────────┐
                        │      service.py      │  invariants + audit log
                        └──────────┬───────────┘
                                   │
                    ┌──────────────┼───────────────┐
                    v              v               v
              ┌──────────┐  ┌────────────┐  ┌────────────┐
              │  db.py   │  │  search.py │  │  publish/  │
              │ SQLite   │  │   FTS5     │  │  worker    │
              └──────────┘  └────────────┘  └─────┬──────┘
                                                  v
                                     publish/index.html + inventory.json
```

| Module | Responsibility |
|---|---|
| `config.py` | Settings, all overridable by environment variable |
| `db.py` | Connections, WAL pragmas, schema init, `transaction()`, backups |
| `schema.sql` | Tables, views, indexes. `schema_fts.sql` adds the search index |
| `models.py` | Typed dataclasses over rows; no behaviour, no DB handle |
| `service.py` | **All mutations.** Invariants, validation, the audit log |
| `search.py` | FTS5 search with a LIKE fallback; barcode scan resolution |
| `barcodes.py` | Code128 SVG rendering for printable labels |
| `csvio.py` | Bulk import (dry-run by default) and export |
| `publish/` | Render the public page and deliver it |
| `web/` | FastAPI routes and Jinja templates |
| `cli.py` | The `stockroom` command |

## Decisions worth knowing

### Availability is derived, never stored

`item.quantity` is the total owned. What is *available* is computed by the
`item_status` view as `quantity - SUM(open loan quantities)`. There is no
`available` column, so it cannot drift out of step with the loan table. Every
read path uses the view.

### `BEGIN IMMEDIATE` on every write

`db.transaction()` opens an immediate transaction, taking the write lock
before reading. Checkout reads availability and then inserts a loan; with a
deferred transaction two people racing for the last unit could both read
"1 available" and both succeed. `test_concurrent_checkouts_cannot_oversubscribe`
runs four threads at one item with two units and asserts exactly two win.

### Partial returns split, they do not edit

Returning 2 units of a 5-unit loan closes the original loan and opens a new
one for the remaining 3, linked by `split_from_loan_id` and keeping the
original checkout timestamp. Loan rows are never rewritten to a smaller
quantity, so "how long have they had this?" stays answerable and the trail
stays append-only.

### Nothing is deleted

Items are archived (`archived_at` set); loans are closed (`returned_at` set);
events are never touched. An item with open loans cannot be archived.

### Publishing is decoupled from the request

After a change commits, `service._notify_change()` pings the publish worker,
which renders on a background thread after a short debounce. Consequences,
all intentional:

- a 200-row CSV import renders the page once, not 200 times;
- a checkout returns without waiting on disk or a `git push`;
- **a publisher failure can never fail a checkout** — it is logged and
  retried on the next change (`test_a_broken_publisher_cannot_block_a_checkout`).

### The public page is a flat file

`publish/index.html` inlines its CSS, its JavaScript and its data. No CDN, no
server, no build step. It works from `file://`, from the Pi, or from GitHub
Pages, and it keeps working if the Pi is off.

Its data is embedded as JSON inside a `<script>` block with `<`, `>` and `&`
escaped as `<`-style sequences. This matters more than it looks: HTML
entities are *not* decoded inside a script element, so letting Jinja autoescape
that payload silently produces a page that renders an empty table. See
`_json_for_script()` and `test_the_embedded_data_is_parseable_json`.

### Identity is one function

`web/deps.py::current_actor()` decides who is acting. It already prefers
Shibboleth headers over the cookie, so the SSO work is mostly configuration.
See [sso-integration.md](sso-integration.md).

## Deliberate non-goals

- **Security hardening.** No authentication, no CSRF tokens, no rate limits.
  This is a trusted-LAN tool, by explicit choice, until SSO lands.
- **An ORM.** Hand-written SQL in one module keeps the dependency footprint
  small on a Pi and makes the audit discipline reviewable.
- **Multi-process scale.** One uvicorn worker. SQLite takes one writer, and
  a stockroom sees a few requests a minute.
