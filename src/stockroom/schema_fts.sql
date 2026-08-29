-- ============================================================================
-- Full-text search index over items.
--
-- Applied only when the running SQLite build has the FTS5 extension (see
-- db.has_fts5). Debian/Raspberry Pi OS python3 ships with it, but the code
-- degrades to LIKE matching rather than failing if it is ever missing --
-- search.py picks the strategy at query time.
--
-- This is an "external content" table (content=item): the index stores no
-- copy of the data, only the inverted index, and the triggers below keep it
-- in step with the item table.
-- ============================================================================

CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
    name,
    description,
    barcode,
    location,           -- unit / shelf / sub_location flattened
    content='',         -- contentless: we store the text in the index itself
    tokenize='unicode61'
);

-- Contentless FTS5 tables cannot be UPDATEd in place, so the triggers delete
-- the old row (using the special 'delete' command) and re-insert.
DROP TRIGGER IF EXISTS item_fts_ai;
CREATE TRIGGER item_fts_ai AFTER INSERT ON item BEGIN
    INSERT INTO item_fts (rowid, name, description, barcode, location)
    VALUES (new.id, new.name, new.description, COALESCE(new.barcode, ''),
            new.unit || ' ' || new.shelf || ' ' || COALESCE(new.sub_location, ''));
END;

DROP TRIGGER IF EXISTS item_fts_ad;
CREATE TRIGGER item_fts_ad AFTER DELETE ON item BEGIN
    INSERT INTO item_fts (item_fts, rowid, name, description, barcode, location)
    VALUES ('delete', old.id, old.name, old.description, COALESCE(old.barcode, ''),
            old.unit || ' ' || old.shelf || ' ' || COALESCE(old.sub_location, ''));
END;

DROP TRIGGER IF EXISTS item_fts_au;
CREATE TRIGGER item_fts_au AFTER UPDATE ON item BEGIN
    INSERT INTO item_fts (item_fts, rowid, name, description, barcode, location)
    VALUES ('delete', old.id, old.name, old.description, COALESCE(old.barcode, ''),
            old.unit || ' ' || old.shelf || ' ' || COALESCE(old.sub_location, ''));
    INSERT INTO item_fts (rowid, name, description, barcode, location)
    VALUES (new.id, new.name, new.description, COALESCE(new.barcode, ''),
            new.unit || ' ' || new.shelf || ' ' || COALESCE(new.sub_location, ''));
END;
