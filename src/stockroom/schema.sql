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


-- ===========================================================================
-- Phase 2: accounts, sessions and requests.
--
-- Everything below is additive -- schema version 2 introduces new tables and
-- alters nothing that existed in version 1, so an existing database picks it
-- up with no migration beyond running this file again.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- account: someone who can log in.
--
-- Deliberately separate from `person`. A `person` is anyone who can hold
-- equipment -- including visiting researchers and collaborators who will never
-- have a login. An `account` is a credential. When an account is approved it
-- is linked to (or creates) the matching `person`, joined by email.
--
-- password_hash format: "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>"
-- The parameters live inside the string so they can be raised later and old
-- hashes upgraded transparently on the owner's next login.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account (
    id                  INTEGER PRIMARY KEY,
    first_name          TEXT    NOT NULL,
    last_name           TEXT    NOT NULL,
    email               TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT    NOT NULL,
    role                TEXT    NOT NULL DEFAULT 'requester'
                        CHECK (role IN ('requester', 'staff', 'admin')),
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'active', 'disabled')),
    person_id           INTEGER REFERENCES person(id),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    approved_at         TEXT,
    approved_by_id      INTEGER REFERENCES account(id),
    last_login_at       TEXT,
    password_changed_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_account_status ON account (status);
CREATE INDEX IF NOT EXISTS idx_account_person ON account (person_id);


-- ---------------------------------------------------------------------------
-- session: a logged-in browser.
--
-- Server-side on purpose. A signed cookie carrying the identity would be
-- smaller, but it cannot be revoked -- and being able to kill a session (a
-- shared machine, a lost laptop, a departing student) is worth far more here
-- than saving a row lookup.
--
-- The cookie holds a 256-bit random token; only its SHA-256 is stored, so a
-- database leak does not hand over live sessions.
--
--   expires_at          idle timeout, pushed forward on use
--   absolute_expires_at hard cap, never extended
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES account(id),
    token_hash          TEXT    NOT NULL UNIQUE,
    csrf_token          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL,
    expires_at          TEXT    NOT NULL,
    absolute_expires_at TEXT    NOT NULL,
    ip                  TEXT    NOT NULL DEFAULT '',
    user_agent          TEXT    NOT NULL DEFAULT '',
    revoked_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_account ON session (account_id);
CREATE INDEX IF NOT EXISTS idx_session_expiry  ON session (expires_at);


-- ---------------------------------------------------------------------------
-- auth_attempt: login attempts, for lockout and for the security record.
--
-- Kept in the database rather than in process memory so that lockout survives
-- a service restart -- otherwise `systemctl restart` is a trivial way to clear
-- a brute-force throttle.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_attempt (
    id      INTEGER PRIMARY KEY,
    email   TEXT    NOT NULL COLLATE NOCASE,
    ip      TEXT    NOT NULL DEFAULT '',
    at      TEXT    NOT NULL,
    success INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_attempt_email ON auth_attempt (email, at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_attempt_ip    ON auth_attempt (ip, at DESC);


-- ---------------------------------------------------------------------------
-- request: the three request forms, in one workflow.
--
-- One table rather than three: the three kinds differ only in a handful of
-- fields, but share an identical lifecycle (pending -> approved/declined,
-- approved -> fulfilled), one staff inbox and one audit path. The CHECK
-- constraint at the bottom enforces that each kind carries the fields it
-- actually needs, so the flexibility does not become sloppiness.
--
-- Approving a borrow request does NOT create a loan. Equipment moves when a
-- human hands it over, so fulfilment is a separate, deliberate step that then
-- links `loan_id`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request (
    id                   INTEGER PRIMARY KEY,
    kind                 TEXT    NOT NULL
                         CHECK (kind IN ('borrow', 'new_item', 'open_hours')),
    requester_id         INTEGER NOT NULL REFERENCES account(id),
    status               TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'declined',
                                           'cancelled', 'fulfilled')),
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    requester_note       TEXT    NOT NULL DEFAULT '',

    decided_at           TEXT,
    decided_by_id        INTEGER REFERENCES account(id),
    decision_note        TEXT    NOT NULL DEFAULT '',

    -- kind = 'borrow'
    item_id              INTEGER REFERENCES item(id),
    quantity             INTEGER CHECK (quantity IS NULL OR quantity > 0),
    needed_from          TEXT,
    needed_until         TEXT,

    -- kind = 'new_item'
    proposed_name        TEXT,
    proposed_description TEXT,
    proposed_url         TEXT,
    proposed_quantity    INTEGER CHECK (proposed_quantity IS NULL OR proposed_quantity > 0),
    proposed_vendor      TEXT,

    -- kind = 'open_hours'
    window_start         TEXT,
    window_end           TEXT,
    purpose              TEXT CHECK (purpose IS NULL OR
                                     purpose IN ('borrow', 'return', 'both')),

    -- what fulfilling the request produced
    loan_id              INTEGER REFERENCES loan(id),
    created_item_id      INTEGER REFERENCES item(id),

    CHECK (
        (kind = 'borrow'     AND item_id IS NOT NULL AND quantity IS NOT NULL)
     OR (kind = 'new_item'   AND proposed_name IS NOT NULL)
     OR (kind = 'open_hours' AND window_start IS NOT NULL
                             AND window_end IS NOT NULL
                             AND purpose IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_request_status    ON request (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_requester ON request (requester_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_request_kind      ON request (kind, status);


-- ---------------------------------------------------------------------------
-- open_hours: a confirmed window when the stockroom is staffed.
--
-- Created when staff approve an open_hours request, or added directly. These
-- are published on the public page, so "when can I come and collect this?" is
-- answerable without logging in.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS open_hours (
    id           INTEGER PRIMARY KEY,
    window_start TEXT    NOT NULL,
    window_end   TEXT    NOT NULL,
    note         TEXT    NOT NULL DEFAULT '',
    published    INTEGER NOT NULL DEFAULT 1,
    request_id   INTEGER REFERENCES request(id),
    created_at   TEXT    NOT NULL,
    created_by   TEXT    NOT NULL,
    cancelled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_open_hours_start ON open_hours (window_start);


-- ---------------------------------------------------------------------------
-- request_detail: requests joined to the names they reference.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS request_detail;
CREATE VIEW request_detail AS
SELECT
    r.*,
    a.first_name || ' ' || a.last_name AS requester_name,
    a.email                            AS requester_email,
    d.first_name || ' ' || d.last_name AS decided_by_name,
    i.name                             AS item_name,
    i.barcode                          AS item_barcode
FROM request r
JOIN account a ON a.id = r.requester_id
LEFT JOIN account d ON d.id = r.decided_by_id
LEFT JOIN item i ON i.id = r.item_id;
