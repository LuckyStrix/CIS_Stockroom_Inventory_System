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
    updated_at TEXT    NOT NULL,
    -- Set when this record was merged into another (service.merge_people).
    -- The row is kept so its old loans still resolve to a name; reads follow
    -- the pointer to the survivor. Nothing is ever deleted.
    merged_into_id INTEGER REFERENCES person(id)
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
    -- 1 => this item's individual units are tracked in the `unit` table, so
    -- "which camera body was it" is answerable. Off for anything countable
    -- (SD cards, batteries), where a row per unit would be absurd.
    tracked      INTEGER NOT NULL DEFAULT 0,
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
--
-- `unit_id` names the individual object that went out, for items with
-- `tracked = 1`. Without it the stockroom knew that one of four camera bodies
-- was on loan but not which one, so "who had the body that came back with a
-- bent mount" was unanswerable -- which is the entire reason the `unit` table
-- exists. NULL for anything countable, which is most of the stockroom.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loan (
    id                 INTEGER PRIMARY KEY,
    item_id            INTEGER NOT NULL REFERENCES item(id),
    person_id          INTEGER NOT NULL REFERENCES person(id),
    unit_id            INTEGER REFERENCES unit(id),  -- NULL when countable
    quantity           INTEGER NOT NULL CHECK (quantity > 0),
    checked_out_at     TEXT    NOT NULL,
    due_at             TEXT,                 -- optional; drives the overdue list
    returned_at        TEXT,                 -- NULL => still out
    checkout_note      TEXT    NOT NULL DEFAULT '',
    return_note        TEXT    NOT NULL DEFAULT '',
    checked_out_by     TEXT    NOT NULL,     -- actor (operator), not borrower
    returned_by        TEXT,
    split_from_loan_id INTEGER REFERENCES loan(id)

    -- NOTE: item_hold carries `CHECK (unit_id IS NULL OR quantity = 1)` and
    -- this table deliberately does not, even though the same rule applies.
    -- SQLite's ALTER TABLE ADD COLUMN cannot add a table-level CHECK, so a
    -- database that upgraded in place would silently lack it while a fresh
    -- one had it -- and test_a_migrated_database_matches_a_fresh_one compares
    -- column names and types only, so nothing would catch the divergence.
    -- The rule is enforced in service._checkout_locked instead. Indexes are
    -- fine: they are CREATE ... IF NOT EXISTS and apply to both paths.
);

-- Partial index: the hot query is "all open loans", and it is the one the
-- item_status view runs for every row.
CREATE INDEX IF NOT EXISTS idx_loan_open      ON loan (item_id) WHERE returned_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_loan_person    ON loan (person_id);
CREATE INDEX IF NOT EXISTS idx_loan_due       ON loan (due_at) WHERE returned_at IS NULL;

-- One open loan per unit, exactly as idx_hold_one_open_per_unit does for
-- holds. This is what stops the same camera body being lent to two people --
-- the availability check counts quantities and would happily allow it -- and
-- it is what keeps the unit_status join below single-valued.
CREATE UNIQUE INDEX IF NOT EXISTS idx_loan_one_open_per_unit
    ON loan (unit_id) WHERE unit_id IS NOT NULL AND returned_at IS NULL;


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
--
-- The log is also a hash chain: each row's `hash` covers its own fields plus
-- the previous row's hash, so editing or removing any historical row breaks
-- every hash after it. See service.log_event and service.verify_audit_chain
-- for what that does and does not buy you.
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
    changes_json TEXT,               -- JSON object or NULL
    prev_hash    TEXT,               -- hash of the preceding row ('' for the first)
    hash         TEXT                -- this row's digest; see service.log_event
);

CREATE INDEX IF NOT EXISTS idx_event_at     ON event (at DESC);
CREATE INDEX IF NOT EXISTS idx_event_item   ON event (item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_event_person ON event (person_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_event_action ON event (action);


-- ---------------------------------------------------------------------------
-- unit: one individual physical thing.
--
-- Only for items with `tracked = 1`. Ten SD cards do not want ten rows here;
-- four camera bodies do, because "which one came back with a bent mount" is
-- a question the stockroom actually has to answer.
--
-- `asset_tag` is the unit's own scannable code, separate from the item's
-- barcode: scanning the item tells you what kind of thing it is, scanning the
-- asset tag tells you which one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unit (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    asset_tag  TEXT    UNIQUE,           -- NULL allowed; assigned on demand
    serial     TEXT,                     -- manufacturer's serial, if known
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    retired_at TEXT                      -- non-NULL => no longer owned
);

CREATE INDEX IF NOT EXISTS idx_unit_item ON unit (item_id);


-- ---------------------------------------------------------------------------
-- item_hold: N units of an item are not lendable, and why.
--
-- This is how the stockroom says "that one is broken" without lying about how
-- many it owns. `item.quantity` stays at what was bought; a hold subtracts
-- from what can be lent. A write-off is a permanent hold in state 'gone', not
-- a quantity edit, so the shelf can still report "we bought ten, two are
-- unaccounted for" -- which is the number a stockroom manager actually needs.
--
--   broken   damaged, not lendable, no plan yet
--   repair   sent out or awaiting repair
--   missing  unaccounted for, may still turn up
--   gone     written off; not coming back
--
-- An open hold has closed_at IS NULL. Closing one returns those units to the
-- shelf. For a tracked item the hold names a `unit`; for a countable one it
-- carries a quantity and unit_id is NULL.
--
-- `loan_id` links a hold to the loan it came back on, which is what makes
-- "who broke it" answerable rather than merely "when did it break".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS item_hold (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    unit_id    INTEGER REFERENCES unit(id),   -- NULL when the item is countable
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    state      TEXT    NOT NULL
               CHECK (state IN ('broken', 'repair', 'missing', 'gone')),
    note       TEXT    NOT NULL DEFAULT '',
    loan_id    INTEGER REFERENCES loan(id),   -- came back like this, on this loan
    opened_at  TEXT    NOT NULL,
    opened_by  TEXT    NOT NULL,
    closed_at  TEXT,
    closed_by  TEXT,
    resolution TEXT,

    -- A named unit is one physical object, so it cannot be three of anything.
    CHECK (unit_id IS NULL OR quantity = 1)
);

CREATE INDEX IF NOT EXISTS idx_hold_open ON item_hold (item_id) WHERE closed_at IS NULL;

-- One open hold per unit. Without this a unit could be simultaneously broken
-- and missing, the unit_status join below would multiply rows, and the same
-- physical camera would be subtracted from availability twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hold_one_open_per_unit
    ON item_hold (unit_id) WHERE unit_id IS NOT NULL AND closed_at IS NULL;


-- ---------------------------------------------------------------------------
-- item_status: item + derived availability. The single source of truth for
-- "how many can I take right now".
--
--   available = quantity - (units on loan) - (units held out of service)
--
-- Because this is a view rather than a stored column, it is impossible for
-- availability to disagree with the loan and hold tables. Every availability
-- figure in the application, the CLI and the public page comes from here.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS item_status;
CREATE VIEW item_status AS
SELECT
    i.*,
    COALESCE(o.out_qty, 0)          AS out_qty,
    COALESCE(h.held_qty, 0)         AS held_qty,
    COALESCE(h.unaccounted_qty, 0)  AS unaccounted_qty,
    i.quantity - COALESCE(o.out_qty, 0) - COALESCE(h.held_qty, 0) AS available,
    COALESCE(o.loan_count, 0)       AS open_loan_count
FROM item i
LEFT JOIN (
    SELECT item_id,
           SUM(quantity) AS out_qty,
           COUNT(*)      AS loan_count
    FROM loan
    WHERE returned_at IS NULL
    GROUP BY item_id
) o ON o.item_id = i.id
LEFT JOIN (
    SELECT item_id,
           SUM(quantity) AS held_qty,
           -- Broken and in-repair units are accounted for: somebody knows
           -- where they are. Missing and written-off ones are not, and that
           -- is the number worth putting on a dashboard.
           SUM(CASE WHEN state IN ('missing', 'gone') THEN quantity ELSE 0 END)
               AS unaccounted_qty
    FROM item_hold
    WHERE closed_at IS NULL
    GROUP BY item_id
) h ON h.item_id = i.id;


-- ---------------------------------------------------------------------------
-- unit_status: each individual unit with its current condition, and who has it.
--
-- Both LEFT JOINs are single-valued, and each has a partial unique index
-- guaranteeing it: idx_hold_one_open_per_unit for the hold, and
-- idx_loan_one_open_per_unit for the loan. Without those a unit could join to
-- two open rows and every unit would silently appear twice.
--
-- `state` is the unit's condition and says nothing about whether it is lent
-- out; `loan_id` is the other half of the question. A unit is lendable only
-- when both are clear -- see models.Unit.is_lendable.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS unit_status;
CREATE VIEW unit_status AS
SELECT
    u.*,
    i.name                 AS item_name,
    i.barcode              AS item_barcode,
    COALESCE(h.state, 'ok') AS state,
    h.note                 AS state_note,
    h.id                   AS hold_id,
    l.id                   AS loan_id,
    p.name                 AS borrower_name,
    l.due_at               AS loan_due_at
FROM unit u
JOIN item i ON i.id = u.item_id
LEFT JOIN item_hold h ON h.unit_id = u.id AND h.closed_at IS NULL
LEFT JOIN loan l ON l.unit_id = u.id AND l.returned_at IS NULL
LEFT JOIN person p ON p.id = l.person_id;


-- ---------------------------------------------------------------------------
-- hold_detail: holds joined to the names they reference, for the item page
-- and the reports.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS hold_detail;
CREATE VIEW hold_detail AS
SELECT
    h.*,
    i.name    AS item_name,
    i.barcode AS item_barcode,
    u.asset_tag,
    u.serial,
    p.name    AS borrower_name,
    p.email   AS borrower_email
FROM item_hold h
JOIN item i ON i.id = h.item_id
LEFT JOIN unit u ON u.id = h.unit_id
LEFT JOIN loan l ON l.id = h.loan_id
LEFT JOIN person p ON p.id = l.person_id;


-- ---------------------------------------------------------------------------
-- loan_detail: open and closed loans joined to their item and person.
-- Used by the dashboard, the person page and the item page.
--
-- Careful with the word "unit" here: `item_unit` is the storage cabinet the
-- item lives in, and `asset_tag`/`serial` identify the individual object that
-- went out. Named as in hold_detail, which had to make the same distinction.
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
    p.email   AS person_email,
    u.asset_tag,
    u.serial
FROM loan l
JOIN item   i ON i.id = l.item_id
JOIN person p ON p.id = l.person_id
LEFT JOIN unit u ON u.id = l.unit_id;


-- ---------------------------------------------------------------------------
-- kit: a named bundle of items that go out together.
--
-- "Portrait kit 2" is a body, a lens, two batteries and a card. Staff think
-- in kits; the loan table thinks in items. A kit is expanded into ordinary
-- basket lines at the counter and then forgotten -- it is a shortcut for
-- typing, not a new kind of loan. Nothing is ever lent "as a kit", so there
-- is no kit state to get out of step with the loans it produced.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kit (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS kit_item (
    kit_id   INTEGER NOT NULL REFERENCES kit(id),
    item_id  INTEGER NOT NULL REFERENCES item(id),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (kit_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_kit_item_item ON kit_item (item_id);


-- ---------------------------------------------------------------------------
-- kit_contents: a kit's lines with the item detail needed to show whether
-- the whole kit can actually go out right now.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS kit_contents;
CREATE VIEW kit_contents AS
SELECT
    k.kit_id,
    k.item_id,
    k.quantity,
    s.name        AS item_name,
    s.barcode     AS item_barcode,
    s.unit        AS item_unit,
    s.shelf       AS item_shelf,
    s.available   AS item_available,
    s.archived_at AS item_archived_at
FROM kit_item k
JOIN item_status s ON s.id = k.item_id;


-- ---------------------------------------------------------------------------
-- stocktake: a physical count of the shelves, and what it found.
--
-- Every inventory system drifts. Something gets put back on the wrong shelf,
-- or walks out during an open lab, or was never logged in the first place --
-- and none of that shows up in the loan table, because none of it went
-- through the counter. A stocktake is the only thing that catches it: walk
-- the room, scan everything, compare against what the database expected.
--
-- Expected on the shelf is derived, never stored:
--
--     quantity - (units on loan) - (units held out of service)
--
-- which is exactly `item_status.available`, so a stocktake cannot disagree
-- with the rest of the system about what should have been there.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stocktake (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,
    started_by  TEXT    NOT NULL,
    scope_unit  TEXT,                       -- NULL => the whole stockroom
    note        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'finished', 'abandoned')),
    finished_at TEXT,
    finished_by TEXT
);

-- One open stocktake at a time. Two people counting the same shelves into
-- different sessions produces two half-counts and two sets of phantom
-- discrepancies, which is worse than not counting at all.
CREATE UNIQUE INDEX IF NOT EXISTS idx_stocktake_one_open
    ON stocktake ((1)) WHERE status = 'open';


CREATE TABLE IF NOT EXISTS stocktake_scan (
    id           INTEGER PRIMARY KEY,
    stocktake_id INTEGER NOT NULL REFERENCES stocktake(id),
    item_id      INTEGER NOT NULL REFERENCES item(id),
    unit_id      INTEGER REFERENCES unit(id),   -- when the asset tag was scanned
    quantity     INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    scanned_at   TEXT    NOT NULL,
    scanned_by   TEXT    NOT NULL,

    -- Deduplicates ASSET-TAG scans only, and that is deliberate rather than a
    -- shortfall. SQLite treats NULLs as distinct in a UNIQUE index, so a row
    -- with unit_id NULL never conflicts: scanning a countable item's barcode
    -- six times writes six rows of one, which is right, because six boxes of
    -- cards really are on the shelf. scan_counts sums them.
    --
    -- A unit scan does conflict, and must: an asset tag names one physical
    -- object, so scanning it twice is one camera seen twice. stocktake.
    -- record_scan's ON CONFLICT targets these three columns and resets the
    -- quantity to 1 for exactly that reason -- do not "fix" this constraint
    -- to cover the NULL case without reading that comment first, and note
    -- that ON CONFLICT raises at prepare time if its target stops matching a
    -- real constraint.
    UNIQUE (stocktake_id, item_id, unit_id)
);

CREATE INDEX IF NOT EXISTS idx_stocktake_scan_session
    ON stocktake_scan (stocktake_id);


-- ---------------------------------------------------------------------------
-- stocktake_result: what a finished count actually found, frozen.
--
-- The rest of this schema stores nothing derived, and for good reason. This
-- table is the exception, because a finished stocktake's findings are not a
-- derived quantity at all -- they are a historical observation, and the whole
-- point of walking the room was to record what was on the shelves on that day.
--
-- Recomputing them instead meant a report that changed every time stock moved:
-- open March's count in April and it lists discrepancies that are simply
-- April's loans. Written once by stocktake.finish_stocktake, inside the same
-- transaction that closes the session, and never updated.
--
-- `item_name`, `barcode` and `location` are copied rather than joined so the
-- report still reads as it did on the day after an item is renamed or moved.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stocktake_result (
    stocktake_id INTEGER NOT NULL REFERENCES stocktake(id),
    item_id      INTEGER NOT NULL REFERENCES item(id),
    item_name    TEXT    NOT NULL,
    barcode      TEXT,
    location     TEXT    NOT NULL DEFAULT '',
    expected     INTEGER NOT NULL,
    counted      INTEGER NOT NULL,
    -- Which list this row belonged to. 'unscanned' is deliberately distinct
    -- from 'short': the likeliest cause is a shelf nobody walked.
    kind         TEXT    NOT NULL
                 CHECK (kind IN ('matched', 'short', 'over', 'unscanned')),

    PRIMARY KEY (stocktake_id, item_id)
);


-- ---------------------------------------------------------------------------
-- item_photo: pictures of an item.
--
-- "Is this the right cable?" is a photo question, and no amount of
-- description answers it as well as a picture of the connector.
--
-- The file itself lives on disk under config.PHOTO_DIR, not in the database:
-- a stockroom's worth of photos would multiply the size of every nightly
-- snapshot for data that never changes once written. This table is the index,
-- and `filename` is a generated name, never anything the uploader chose.
--
-- Soft-deleted like everything else here: `deleted_at` hides the row and the
-- file is left alone, so a mis-click is recoverable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS item_photo (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    filename   TEXT    NOT NULL UNIQUE,
    caption    TEXT    NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    width      INTEGER,
    height     INTEGER,
    bytes      INTEGER,
    created_at TEXT    NOT NULL,
    created_by TEXT    NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_photo_item
    ON item_photo (item_id) WHERE deleted_at IS NULL;

-- At most one primary photo per item: it is the one shown on the item list
-- and at the counter, and two of them would make that choice arbitrary.
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_photo_one_primary
    ON item_photo (item_id) WHERE is_primary = 1 AND deleted_at IS NULL;


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
