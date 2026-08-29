# Data model

SQLite, WAL mode. The authoritative definition is
[`src/stockroom/schema.sql`](../src/stockroom/schema.sql); this is the
narrative version.

All timestamps are ISO-8601 UTC text (`2026-08-28T14:03:11Z`). SQLite has no
date type, and this format sorts lexicographically — which is why the overdue
query can compare dates with a plain string comparison.

## Tables

### `item` — a kind of thing, with a total quantity

| Column | Notes |
|---|---|
| `barcode` | Unique, nullable. `CIS-000142` generated, or a manufacturer code |
| `name` | Required |
| `description` | Required by the UI, defaults to `''` |
| `product_url` | Optional vendor link |
| `quantity` | **Total owned**, not available. `CHECK (quantity >= 0)` |
| `unit`, `shelf`, `sub_location` | Location; the third level is optional |
| `min_quantity` | Optional low-stock threshold |
| `archived_at` | Non-NULL means archived. Rows are never deleted |

Ten SD cards with two lent out is **one row** with `quantity = 10`. A unique
camera body is the same shape with `quantity = 1`.

### `person` — someone who can borrow

Name plus a case-insensitively unique email. No login, no password. When SSO
lands, `email` is the join key against the Shibboleth `mail` attribute.

### `loan` — N units of an item held by a person

`returned_at IS NULL` means still out. `due_at` is optional and drives the
overdue list. `checked_out_by` / `returned_by` record the **operator** who
handled the transaction, which is not the same person as the borrower.

`split_from_loan_id` links a residual loan back to the loan it was split from
by a partial return.

### `event` — the append-only audit log

One row per change, written in the same transaction as the change.

| Column | Notes |
|---|---|
| `at`, `actor` | When, and who did it |
| `action` | `item.create`, `loan.checkout`, … (table below) |
| `entity_type`, `entity_id` | What was changed |
| `item_id`, `person_id` | Denormalized, so per-item history is one indexed lookup |
| `summary` | Human-readable one-liner shown in the UI |
| `changes_json` | `{"shelf": {"from": "3", "to": "1"}}` |

Actions: `item.create`, `item.update`, `item.relocate`, `item.quantity_adjust`,
`item.archive`, `item.restore`, `person.create`, `person.update`,
`loan.checkout`, `loan.return`, `loan.partial_return`, `import.run`.

`update_item` picks the most specific action that fits, so a pure location
change reads as `item.relocate` and the history can be filtered meaningfully.

## Views

### `item_status` — item plus derived availability

```sql
available = quantity - COALESCE(SUM(open loan quantities), 0)
```

Every read path uses this rather than the `item` table. Availability is
computed, never a stored column — so it cannot disagree with the loans.

### `loan_detail`

Loans joined to their item and person, for the dashboard and detail pages.

## Search

`item_fts` is a contentless FTS5 index over name, description, barcode and
flattened location, kept in sync by triggers on `item`. If the SQLite build
lacks FTS5, `search.py` falls back to `LIKE` — slower, but correct, and at
stockroom scale still instant.

## Worked example

`SanDisk 64GB SD Card`, 10 owned. Alice takes 2, Bob takes 1, Alice returns 1.

```
item   id=2  quantity=10

loan   id=1  alice  qty=2  returned_at=2026-08-28T14:22:00Z   <- closed by
loan   id=2  bob    qty=1  returned_at=NULL                      the partial
loan   id=3  alice  qty=1  returned_at=NULL  split_from=1        return

item_status:  quantity=10  out_qty=2  available=8
```

Loan 1 is closed and keeps `quantity = 2` forever. Loan 3 is the residual,
carrying Alice's original checkout timestamp. The event log holds
`loan.checkout` ×2 and `loan.partial_return` ×1, each with its actor.
