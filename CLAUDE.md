# CIS Stockroom Inventory System — working notes

Inventory and checkout tracking for the RIT Carlson Center for Imaging
Science stockroom. Runs on a Raspberry Pi on the stockroom LAN.

## The rule

> **Every mutation goes through a service module — `service.py` (inventory),
> `accounts.py` (people and sessions), `requests_service.py` (requests),
> `kits.py` (bundles) or `stocktake.py` (physical counts) — which writes the
> change and its `event` row in the same transaction. Nothing else writes to
> the tables.**

This is the point of the system — the stockroom needs to know who had what and
when, and that guarantee is worth more than any feature. If you add a mutating
function:

1. Put it in the matching service module.
2. Wrap it in `db.transaction()`.
3. Call `log_event()` inside that transaction.
4. Add it to `MUTATIONS` and the call table in `tests/test_audit.py`.

`test_no_mutating_function_is_missing_from_the_table` scans all five service
modules and fails if a function takes `actor: Actor` and is not audit-tested.
That is intentional — do not weaken it to make it pass; add the test.

There are two documented exceptions:

- the **session heartbeat** (`last_seen_at` and the sliding idle expiry): a
  page view is not a domain change, and logging it would bury the inventory
  history;
- **`stocktake.record_scan`**: a count of a thousand items would write a
  thousand rows into the log for the same reason. The scans live in
  `stocktake_scan`, and the session is audited at start and finish.

## Availability

> **`available = quantity − units on loan − units held out of service`**, and
> it lives in the `item_status` view. Nothing stores an availability number.

`quantity` is what the stockroom bought and never moves on its own. A broken,
missing or written-off unit is an open row in `item_hold`, so the shelf can
say "we own ten, two are unaccounted for" rather than quietly becoming eight.
Every availability figure in the app, the CLI, the kits, the stocktake and the
public page comes through that one view.

## Security invariants

Two tests in `tests/test_authz.py` enumerate the application's real route table:

- **every route** is either on `deps.PUBLIC_PATHS` or rejects anonymous callers;
- **every POST** rejects a request with no CSRF token.

Both will fail on a newly added route that skips a guard. That is the point —
fix the route, not the test.

Other things that are load-bearing:

- **CSRF is checked in middleware**, reading the body with `request.body()`.
  Do **not** switch it to `request.form()`: Starlette only replays a body it
  saw read via `body()`, and `form()` leaves every downstream route seeing an
  empty form. Covered by `test_csrf_middleware_does_not_eat_the_request_body`.
- **No inline JavaScript or `style=` attributes**, anywhere. The CSP has no
  `unsafe-inline`, so an inline handler fails silently in the browser. Barcode
  SVGs use `fill=` attributes for the same reason.
- **`deps.safe_path()` guards every caller-supplied redirect** (`next`,
  `Referer`). Skipping it reintroduces an open redirect.
- **Do not trust `X-Shib-*` or `X-Remote-User` headers.** That support was
  removed deliberately; nginx passes client headers through, so trusting them
  is impersonation-as-a-service. See `docs/sso-integration.md`.

## Conventions

- **Python 3.11**, stdlib `sqlite3`, hand-written SQL. No ORM: the dependency
  footprint stays small on a Pi and the audit discipline stays reviewable.
- **Timestamps** are ISO-8601 UTC strings via `db.utcnow()`. They sort
  lexicographically, which the overdue query relies on.
- **Reads** go through the `item_status` view, never the `item` table
  directly — availability is derived and must not be duplicated.
- **Errors**: raise `ValidationError` (bad input), `ConflictError` (violates
  an invariant), `NotFound`. The web layer renders these as flash messages,
  not 500s. Anything else is a real bug.
- **Nothing is deleted.** Archive items, close loans, append events.
- **Comments** explain *why*, especially where the code looks odd — the
  `BEGIN IMMEDIATE`, the partial-return split, the `<script>` JSON escaping.
  These all encode a bug that was actually hit.

## Things that will bite you

- **`db.transaction()` uses `BEGIN IMMEDIATE`.** Do not "optimise" this to a
  deferred transaction; it is what stops two people checking out the same last
  unit. Covered by `test_concurrent_checkouts_cannot_oversubscribe`.
- **The public page's JSON must not be HTML-autoescaped.** HTML entities are
  not decoded inside a `<script>` element, so autoescaping the payload yields
  a page that silently renders an empty table. `_json_for_script()` escapes
  `<`, `>`, `&` as `\u00xx` and the template marks it safe. Covered by
  `test_the_embedded_data_is_parseable_json`.
- **Publishing must never fail a change.** It runs debounced on a background
  thread, after commit. Failures are logged and retried, never raised.
  Covered by `test_a_broken_publisher_cannot_block_a_checkout`.
- **`config.PUBLISH_DIR` is read per request**, not bound at import. `/public`
  is a route rather than a `StaticFiles` mount for exactly this reason.
- **A partial return splits the loan**, it does not shrink it. Loan rows are
  never rewritten to a smaller quantity.

## Layout

```
src/stockroom/
  config.py  db.py  schema.sql  schema_fts.sql  models.py
  service.py          <- inventory mutations + the audit log
  search.py  barcodes.py  csvio.py  photos.py
  publish/  render.py  publishers.py  worker.py
  web/      app.py  deps.py  routes_*.py
  templates/  static/  cli.py
tests/    deploy/    docs/
```

The phase 2 and phase 3 additions are listed below; the short version is that
`service.py` is no longer the only place mutations live.

## Testing

```bash
.venv/bin/pytest              # 601 tests, ~2min
```

`tests/test_web.py` drives real HTTP requests through the actual routes and
templates, so a template referencing a variable a route does not pass will
fail there rather than in production.

## Layout additions (phase 2)

```
accounts.py            <- accounts, passwords, sessions, lockout
requests_service.py    <- the three request workflows
security.py            <- hashing, tokens, CSRF, rate limiting
web/routes_auth.py  routes_accounts.py  routes_requests.py
deploy/harden-pi.sh  nginx-stockroom.conf
docs/security.md  accounts-and-requests.md
```

## Layout additions (phase 3)

```
kits.py                <- named bundles, expanded into a basket at the counter
stocktake.py           <- physical counts and reconciliation
reports.py             <- usage reads + server-rendered SVG charts
diagnostics.py         <- the checks behind `stockroom doctor` and /diagnostics
backup_targets.py      <- getting a snapshot off the SD card (USB, rclone)
photos.py              <- decode, downscale, strip EXIF, store
web/routes_counter.py  routes_kits.py  routes_stocktake.py  routes_admin.py
tests/fixtures/schema_v2.sql   <- what an upgrading Pi actually has
```

### Things phase 3 added that are easy to get wrong

- **`db._ADDED_COLUMNS` and `schema.sql` must agree.** A new column has to be
  written twice — in `schema.sql` for fresh databases and in `_ADDED_COLUMNS`
  for existing ones. `test_a_migrated_database_matches_a_fresh_one` compares
  them; without it, a Pi upgrading in place quietly lacks the column.
- **The audit log is a hash chain.** `log_event` inserts then hashes in the
  same transaction, which is race-free only because `BEGIN IMMEDIATE` already
  serialises writers. `rebuild_audit_chain` is called by the migration and
  nothing else: running it later would launder a tamper into a valid chain.
- **The counter basket is hidden form fields**, not a table and not a session.
  Accumulating a line writes nothing; only `checkout_many` does, and it is one
  transaction so a basket cannot half-happen.
- **`list_storage_units` vs `list_units`.** "Unit" means two things here: a
  storage cabinet (`item.unit`) and one physical object (the `unit` table).
  Two functions called `list_units` once shadowed each other silently — see
  `tests/test_source_hygiene.py`.
- **Multipart CSRF is bounded.** `_submitted_csrf` scans only the first 16 KB
  of a multipart body, so `_csrf` must be the **first field** in any form that
  can carry a file. `test_the_csrf_field_comes_first` enforces it.
- **`request.form()` returns Starlette's `UploadFile`, not FastAPI's.** An
  `isinstance` check against `fastapi.UploadFile` fails for every real upload
  and reports "no photo was chosen" for a photo that is right there.

## Not goals (for now)

No internet exposure, no email (so no self-service password reset and no
notifications), no full-disk encryption. Item photos are internal — the public
page is a single self-contained file with no asset directory. The next major piece of work is RIT
Shibboleth SSO, which deletes the password machinery entirely; identity is
isolated in `web/deps.py::current_account()`. Read
`docs/sso-integration.md` — especially the note about headers — before
touching anything identity-related.
