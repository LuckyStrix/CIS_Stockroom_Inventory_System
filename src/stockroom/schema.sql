-- ============================================================================
-- CIS Stockroom Inventory System -- database schema
--
-- Conventions used throughout:
--   * All timestamps are ISO-8601 UTC strings ("2026-08-28T14:03:11Z").
--     SQLite has no date type; text sorts correctly in this format.
--   * Nothing is ever hard-deleted. Items are archived (`archived_at` set).
--     Loans are closed (`returned_at` set). The `event` table is append-only.
--   * Availability is never stored -- it is always derived from open loans
--     by the `item_status` view, so it cannot drift out of sync.
--
-- See docs/data-model.md for the narrative version of this file.
-- ============================================================================

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- meta: schema version and the barcode counter.
-- Single-row-per-key store. Kept in the database (not a file) so that a copy
-- of the .db file is a complete, self-contained backup.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- person: someone who can borrow things.
--
-- For now a person is just a name and an email -- there is no login and no
-- password. When RIT SSO lands (docs/sso-integration.md) `email` becomes the
-- join key against the Shibboleth `mail` attribute, which is why it is
-- case-insensitively unique.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    email      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    active     INTEGER NOT NULL DEFAULT 1,   -- 0 hides them from pickers
    notes      TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);


-- ---------------------------------------------------------------------------
-- item: a kind of thing in the stockroom, with a total quantity.
--
-- `quantity` is the total number of units OWNED, not the number available.
-- Ten SD cards with two lent out is one row with quantity = 10; the view
-- below reports available = 8. A unique piece of gear is simply quantity = 1.
--
-- Location is three fields: unit (the storage unit / cabinet), shelf, and an
-- optional deeper descriptor (bin, drawer, pelican case, ...).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS item (
    id           INTEGER PRIMARY KEY,
    barcode      TEXT    UNIQUE,             -- NULL allowed; assigned on demand
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    product_url  TEXT,                       -- optional link to vendor page
    quantity     INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    unit         TEXT    NOT NULL DEFAULT '',
    shelf        TEXT    NOT NULL DEFAULT '',
    sub_location TEXT,                       -- optional 3rd descriptor
    min_quantity INTEGER CHECK (min_quantity IS NULL OR min_quantity >= 0),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    archived_at  TEXT                        -- non-NULL => archived
);

CREATE INDEX IF NOT EXISTS idx_item_archived ON item (archived_at);
CREATE INDEX IF NOT EXISTS idx_item_location ON item (unit, shelf, sub_location);
CREATE INDEX IF NOT EXISTS idx_item_name     ON item (name COLLATE NOCASE);


-- ---------------------------------------------------------------------------
-- loan: N units of an item held by a person.
--
-- An open loan has returned_at IS NULL. A partial return closes the original
-- loan and opens a residual one for the remainder (see service.return_loan),
-- so a row is never rewritten to a smaller quantity and the trail stays
-- append-only. `split_from_loan_id` links a residual back to its parent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loan (
    id                 INTEGER PRIMARY KEY,
    item_id            INTEGER NOT NULL REFERENCES item(id),
    person_id          INTEGER NOT NULL REFERENCES person(id),
    quantity           INTEGER NOT NULL CHECK (quantity > 0),
    checked_out_at     TEXT    NOT NULL,
    due_at             TEXT,                 -- optional; drives the overdue list
    returned_at        TEXT,                 -- NULL => still out
    checkout_note      TEXT    NOT NULL DEFAULT '',
    return_note        TEXT    NOT NULL DEFAULT '',
    checked_out_by     TEXT    NOT NULL,     -- actor (operator), not borrower
    returned_by        TEXT,
    split_from_loan_id INTEGER REFERENCES loan(id)
);

-- Partial index: the hot query is "all open loans", and it is the one the
-- item_status view runs for every row.
CREATE INDEX IF NOT EXISTS idx_loan_open      ON loan (item_id) WHERE returned_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_loan_person    ON loan (person_id);
CREATE INDEX IF NOT EXISTS idx_loan_due       ON loan (due_at) WHERE returned_at IS NULL;


-- ---------------------------------------------------------------------------
-- event: the append-only audit log. THE point of this system.
--
-- Every mutation writes exactly one row here, in the same transaction as the
-- change itself (see the module docstring of service.py). Rows are never
-- updated or deleted.
--
--   changes_json -- field-level diff, {"field": {"from": x, "to": y}}
--   item_id / person_id -- denormalized so per-item and per-person history
--                          are single indexed lookups rather than joins
--                          through entity_type/entity_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY,
    at           TEXT    NOT NULL,
    actor        TEXT    NOT NULL,   -- who did it (SSO principal, eventually)
    action       TEXT    NOT NULL,   -- "item.create", "loan.checkout", ...
    entity_type  TEXT    NOT NULL,   -- "item" | "person" | "loan" | "system"
    entity_id    INTEGER,
    item_id      INTEGER REFERENCES item(id),
    person_id    INTEGER REFERENCES person(id),
    summary      TEXT    NOT NULL,   -- human-readable one-liner
    changes_json TEXT                -- JSON object or NULL
);

CREATE INDEX IF NOT EXISTS idx_event_at     ON event (at DESC);
CREATE INDEX IF NOT EXISTS idx_event_item   ON event (item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_event_person ON event (person_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_event_action ON event (action);


-- ---------------------------------------------------------------------------
-- item_status: item + derived availability. The single source of truth for
-- "how many can I take right now".
--
-- available = quantity - (sum of open loan quantities)
--
-- Because this is a view rather than a stored column, it is impossible for
-- availability to disagree with the loan table.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS item_status;
CREATE VIEW item_status AS
SELECT
    i.*,
    COALESCE(o.out_qty, 0)              AS out_qty,
    i.quantity - COALESCE(o.out_qty, 0) AS available,
    COALESCE(o.loan_count, 0)           AS open_loan_count
FROM item i
LEFT JOIN (
    SELECT item_id,
           SUM(quantity) AS out_qty,
           COUNT(*)      AS loan_count
    FROM loan
    WHERE returned_at IS NULL
    GROUP BY item_id
) o ON o.item_id = i.id;


-- ---------------------------------------------------------------------------
-- loan_detail: open and closed loans joined to their item and person.
-- Used by the dashboard, the person page and the item page.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS loan_detail;
CREATE VIEW loan_detail AS
SELECT
    l.*,
    i.name    AS item_name,
    i.barcode AS item_barcode,
    i.unit    AS item_unit,
    i.shelf   AS item_shelf,
    p.name    AS person_name,
    p.email   AS person_email
FROM loan l
JOIN item   i ON i.id = l.item_id
JOIN person p ON p.id = l.person_id;
