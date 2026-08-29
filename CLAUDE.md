# CIS Stockroom Inventory System — working notes

Inventory and checkout tracking for the RIT Carlson Center for Imaging
Science stockroom. Runs on a Raspberry Pi on the stockroom LAN.

## The rule

> **Every mutation goes through `src/stockroom/service.py`, which writes the
> change and its `event` row in the same transaction. Nothing else writes to
> `item`, `loan` or `person`.**

This is the point of the system — the stockroom needs to know who had what and
when, and that guarantee is worth more than any feature. If you add a mutating
function:

1. Put it in `service.py`.
2. Wrap it in `db.transaction()`.
3. Call `log_event()` inside that transaction.
4. Add it to `MUTATIONS` and the call table in `tests/test_audit.py`.

`test_no_mutating_function_is_missing_from_the_table` fails if a new function
takes `actor: Actor` and is not audit-tested. That is intentional — do not
weaken it to make it pass; add the test.

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
  service.py          <- all mutations + the audit log
  search.py  barcodes.py  csvio.py
  publish/  render.py  publishers.py  worker.py
  web/      app.py  deps.py  routes_*.py
  templates/  static/  cli.py
tests/    deploy/    docs/
```

## Testing

```bash
.venv/bin/pytest              # 128 tests, ~8s
```

`tests/test_web.py` drives real HTTP requests through the actual routes and
templates, so a template referencing a variable a route does not pass will
fail there rather than in production.

## Not goals (for now)

No authentication, CSRF tokens or rate limiting — this is a trusted-LAN tool
by explicit choice. The next major piece of work is RIT Shibboleth SSO; all
identity logic is already isolated in `web/deps.py::current_actor()`. See
`docs/sso-integration.md` before touching anything identity-related.
