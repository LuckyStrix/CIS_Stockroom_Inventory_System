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

A loan may also name **which** physical object went out (`loan.unit_id`), for
items with `tracked = 1`. That does not change the arithmetic above — a unit
loan is quantity 1 like any other — but it is what makes "who had the body
that came back with a bent mount" answerable. Optional everywhere: most of the
stockroom is countable and has no `unit` rows at all.

## Security invariants

Five tests in `tests/test_authz.py` enumerate the application's real route table:

- **every route** is either on `deps.PUBLIC_PATHS` or rejects anonymous callers;
- **every POST** rejects a request with no CSRF token;
- **every staff-only route refuses a requester** — found by reading which
  `require_*` each handler calls, not from a list kept by hand;
- **no handler acts before its permission check** — the guards are called in
  the body rather than declared as dependencies, so "guarded" and "guarded
  first" are separate questions and only the first is visible in a listing;
- **no page a requester can reach links to one they cannot** — it follows
  every link and form action on those pages.

They will fail on a newly added route that skips a guard. That is the point —
fix the route, not the test.

`_walk_routes` is what makes the first two mean anything, and it has been
wrong before. FastAPI wraps each `include_router` in a `_IncludedRouter` that
has no `.path` **and no `.routes`** — the real ones hang off
`.original_router`. Reading `.routes` with a `[]` default walked past all
seventy-eight router routes and left both tests asserting over the dozen
declared on `app` itself, which is to say passing vacuously.
`test_the_route_walk_actually_finds_the_routes` now fails if the walk stops
seeing the application. **A test that enumerates is only as good as its
enumeration**; if you touch the traversal, check the count.

### Roles reach into the templates

`require_staff` on the route is half of it. A page a requester can open must
not *render* what it will then refuse:

- **The route withholds the data, the template hides the control.** Both, not
  either. `routes_items.item_detail` sends `people`, `open_loans`,
  `past_loans`, `units` and the hold machinery only to staff — it once sent
  them to everyone, so `item_detail.html` handed every signed-in student the
  borrower datalist, which is every email address the stockroom holds.
  `routes_requests.request_detail` withholds `overlaps` the same way.
- **Borrower identity is staff-only.** The public page omits it unless
  `config.PUBLIC_SHOW_BORROWERS` says otherwise; being signed in as a
  requester is not a reason to be laxer than the page on the corridor wall.
- **A control that 403s is a bug, not a defence.** The route refusing it is
  correct and the button being there is still wrong.

Other things that are load-bearing:

- **CSRF is checked in middleware**, reading the body with `request.body()`.
  Do **not** switch it to `request.form()`: Starlette only replays a body it
  saw read via `body()`, and `form()` leaves every downstream route seeing an
  empty form. Covered by `test_csrf_middleware_does_not_eat_the_request_body`.
- **No inline JavaScript or `style=` attributes**, anywhere. The CSP has no
  `unsafe-inline`, so an inline handler fails silently in the browser. Barcode
  SVGs use `fill=` attributes for the same reason.
- **The `Host` check derives its allow list from the machine's own hostname**
  (`config._default_allowed_hosts`) and accepts bare IP addresses. Hardcoding
  `cis-stockroom` is what made a differently-named Pi answer every device with
  a bare 400, and refusing IPs broke every phone without an mDNS resolver. The
  app builds no absolute URL from `Host`, so there is nothing to poison; keep
  it that way, or set `STOCKROOM_ALLOW_IP_HOSTS="0"`.
- **`deps.safe_path()` guards every caller-supplied redirect** (`next`,
  `Referer`). Skipping it reintroduces an open redirect.
- **Do not trust `X-Shib-*` or `X-Remote-User` headers.** That support was
  removed deliberately; nginx passes client headers through, so trusting them
  is impersonation-as-a-service. See `docs/sso-integration.md`.

## Conventions

- **Python 3.11**, stdlib `sqlite3`, hand-written SQL. No ORM: the dependency
  footprint stays small on a Pi and the audit discipline stays reviewable.
- **Timestamps** are ISO-8601 UTC strings via `db.utcnow()`. They sort
  lexicographically, which the overdue query relies on. UTC is the *storage*
  format and every comparison stays in it; conversion happens only at the two
  edges where a human is involved — `deps.local_to_utc()` reads a date typed
  into a form, `deps.utc_to_local()` (via the `|date` and `|datetime` filters)
  prints one back. `config.TIMEZONE` is the stockroom's wall clock. Neither
  edge used to convert at all, so a 2pm checkout displayed as 18:00 and a loan
  due "Friday" went overdue at 19:59 on Friday afternoon.
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
  never rewritten to a smaller quantity. A *unit* loan is quantity 1, so it
  can never split — and must not, since a residual row would have to decide
  whether to carry the unit forward.
- **The generated public page carries its own CSP and must keep it.**
  `render._csp_hashes` builds a `<meta>` policy from SHA-256 hashes of the
  page's own inline `<style>` and `<script>`, because a file opened from a USB
  stick or GitHub Pages has no server to mint a nonce. `_apply_security_headers`
  therefore skips `/public` — a browser enforces every policy it is handed and
  takes the intersection, so adding the app's nonce policy allowed *neither*
  block and the page rendered unstyled and empty. Covered by
  `test_the_app_does_not_override_the_public_pages_own_policy`.
- **Middleware order is registration order reversed.** `add_middleware`
  inserts at the front, so the last registration is the outermost layer. They
  are registered in one explicit block at the bottom of `web/app.py`; read the
  comment there before adding one. Getting it wrong is silent: the auth gate
  once sat outside the headers middleware, so every refusal it produced went
  out with no CSP and no `nosniff`.
- **Every response that leaves `security_middleware` must go through
  `finish()`**, including the early CSRF and oversize refusals. Returning one
  directly skips the security headers.

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
.venv/bin/pytest              # 753 tests, ~5min
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

## Layout additions (phase 4)

Nothing new here is a module; phase 4 is repairs plus one feature.

```
loan.unit_id           <- which physical object went out (schema v4)
stocktake_result       <- what a finished count found, frozen
search.resolve()       <- barcode OR asset tag -> Scan(item, unit)
```

`SCHEMA_VERSION` is **4**. `search.resolve_scan()` still exists and still
returns an `Item`; call `resolve()` when knowing *which* one matters.

### Things phase 3 added that are easy to get wrong

- **`db._ADDED_COLUMNS` and `schema.sql` must agree.** A new column has to be
  written twice — in `schema.sql` for fresh databases and in `_ADDED_COLUMNS`
  for existing ones. `test_a_migrated_database_matches_a_fresh_one` compares
  them; without it, a Pi upgrading in place quietly lacks the column.
- **`ALTER TABLE` cannot add a table-level `CHECK`.** So a constraint written
  into `schema.sql` reaches a fresh database and silently misses an upgrading
  one — and `test_a_migrated_database_matches_a_fresh_one` compares column
  names and types only, so it will not catch the drift. `loan` needs
  `unit_id IS NULL OR quantity = 1` and enforces it in
  `service._checkout_locked` for exactly this reason. Indexes are fine; they
  are `CREATE ... IF NOT EXISTS` and run after `_ensure_columns`.
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
  `tests/test_source_hygiene.py`. The same trap is in the views: `loan_detail`
  has both `item_unit` (the cabinet) and `asset_tag`/`serial` (the object).
- **`Unit.is_available` and `Unit.is_lendable` are different questions.**
  `is_available` means sound and still owned — what the condition machinery
  asks before opening a hold. `is_lendable` adds "and not already in somebody
  else's bag". Offering `is_available` units in a checkout picker hands out
  cameras that are already out.
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
