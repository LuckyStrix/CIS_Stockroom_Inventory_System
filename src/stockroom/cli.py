"""The ``stockroom`` command line tool.

For bulk and operational work that is awkward through a browser: importing a
spreadsheet, taking a backup, rebuilding the public page from cron, and
checking what the database thinks is going on.

    stockroom init                       create/upgrade the database
    stockroom import stock.csv           dry run -- shows what it would do
    stockroom import stock.csv --commit  actually apply it
    stockroom export > backup.csv        dump the inventory as CSV
    stockroom items --out                what is currently lent out
    stockroom history --item 12          one item's full history
    stockroom checkout 12 alice@rit.edu --qty 2
    stockroom return 45
    stockroom publish                    rebuild the public page now
    stockroom backup                     snapshot the database
    stockroom status                     headline numbers

Every mutating command takes ``--actor``, which is what lands in the audit
log. It defaults to ``cli:<unix user>`` so an unattended cron job is still
attributable.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import __version__, config, csvio, db, service
from .publish import worker as publish_worker
from .service import Actor, StockroomError


def _publish(quiet: bool = True) -> None:
    """Rebuild the public page after a change made from the CLI.

    A publishing failure is reported but never changes the exit code: the
    database change already committed, and the page can be rebuilt later with
    `stockroom publish`.
    """
    worker = publish_worker.PublishWorker(db_path=config.DB_PATH)
    worker.publish()
    if worker.last_error is not None and not quiet:
        print(f"Warning: the public page could not be updated: {worker.last_error}",
              file=sys.stderr)


def _actor(args: argparse.Namespace) -> Actor:
    if args.actor:
        name, _, email = args.actor.partition("<")
        return Actor(name=name.strip(), email=email.rstrip(">").strip())
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return Actor(name=f"cli:{user}")


def _conn():
    return db.init_db()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    conn = _conn()
    print(f"Database ready at {config.DB_PATH}")
    print(f"  schema version {db.get_meta(conn, 'schema_version')}")
    print(f"  full-text search: {'on' if db.fts_enabled(conn) else 'off (LIKE fallback)'}")
    return 0


def cmd_import(args) -> int:
    conn = _conn()
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 1
    try:
        result = csvio.import_csv(
            conn, csvio.read_text(path), actor=_actor(args),
            commit=args.commit, source=path.name,
        )
    except StockroomError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    print(result.report())
    if result.committed:
        _publish(quiet=False)
    return 0 if result.ok else 1


def cmd_export(args) -> int:
    sys.stdout.write(csvio.export_csv(_conn(), include_archived=args.all))
    return 0


def cmd_items(args) -> int:
    conn = _conn()
    items = service.list_items(
        conn,
        include_archived=args.all,
        only_out=args.out,
        only_low_stock=args.low,
        unit=args.unit,
    )
    if not items:
        print("No matching items.")
        return 0
    width = min(max(len(i.name) for i in items), 42)
    print(f"{'BARCODE':<13} {'NAME':<{width}} {'AVAIL':>7} {'TOTAL':>6}  LOCATION")
    for item in items:
        print(
            f"{item.barcode or '-':<13} {item.name[:width]:<{width}} "
            f"{item.available:>7} {item.quantity:>6}  {item.location}"
        )
    print(f"\n{len(items)} item(s).")
    return 0


def cmd_checkout(args) -> int:
    conn = _conn()
    try:
        item = _resolve_item(conn, args.item)
        person = service.find_person_by_email(conn, args.email)
        loan = service.checkout(
            conn, actor=_actor(args), item_id=item.id,
            person_name=(person.name if person else args.name or args.email.split("@")[0]),
            person_email=args.email, quantity=args.qty, note=args.note or "",
        )
    except StockroomError as exc:
        print(f"Cannot check out: {exc}", file=sys.stderr)
        return 1
    print(f"Loan #{loan.id}: {loan.quantity} x {loan.item_name} -> {loan.person_name}")
    _publish(quiet=False)
    return 0


def cmd_return(args) -> int:
    conn = _conn()
    try:
        loan = service.return_loan(
            conn, actor=_actor(args), loan_id=args.loan,
            quantity=args.qty, note=args.note or "",
        )
    except StockroomError as exc:
        print(f"Cannot return: {exc}", file=sys.stderr)
        return 1
    print(f"Returned {args.qty or loan.quantity} x {loan.item_name} from {loan.person_name}")
    _publish(quiet=False)
    return 0


def cmd_loans(args) -> int:
    conn = _conn()
    loans = service.list_loans(conn, open_only=True, overdue_only=args.overdue)
    if not loans:
        print("Nothing is checked out.")
        return 0
    now = db.utcnow()
    for loan in loans:
        flag = " OVERDUE" if loan.is_overdue(now) else ""
        print(
            f"#{loan.id:<5} {loan.quantity:>3} x {loan.item_name[:34]:<34} "
            f"{loan.person_email:<28} since {loan.checked_out_at[:10]}{flag}"
        )
    print(f"\n{len(loans)} open loan(s).")
    return 0


def cmd_history(args) -> int:
    conn = _conn()
    events = service.list_events(
        conn, item_id=args.item, action=args.action, limit=args.limit
    )
    for event in reversed(events):
        print(f"{event.at}  {event.actor:<28} {event.action:<22} {event.summary}")
    print(f"\n{len(events)} event(s).")
    return 0


def cmd_publish(args) -> int:
    _conn()
    files = publish_worker.publish_now(config.DB_PATH)
    print(f"Published {', '.join(sorted(files))} to {config.PUBLISH_DIR}")
    if config.GITHUB_PAGES_DIR:
        print(f"  mirrored to {config.GITHUB_PAGES_DIR} ({config.GITHUB_PAGES_BRANCH})")
    return 0


def cmd_backup(args) -> int:
    """Snapshot the database, then prune old snapshots.

    Uses SQLite's online backup API, so this is safe to run from cron while
    the web service is live and serving checkouts.
    """
    _conn()
    target = Path(args.output) if args.output else (
        config.BACKUP_DIR / f"stockroom-{db.utcnow().replace(':', '')}.db"
    )
    db.backup(target)
    print(f"Wrote {target} ({target.stat().st_size:,} bytes)")

    if args.output is None and config.BACKUP_KEEP > 0:
        snapshots = sorted(config.BACKUP_DIR.glob("stockroom-*.db"))
        for stale in snapshots[: max(0, len(snapshots) - config.BACKUP_KEEP)]:
            stale.unlink()
            print(f"Pruned {stale.name}")
    return 0


def cmd_status(args) -> int:
    conn = _conn()
    info = service.summary(conn)
    print(f"CIS Stockroom {__version__}")
    print(f"  database   {config.DB_PATH}")
    print(f"  publish to {config.PUBLISH_DIR}")
    print(f"  search     {'FTS5' if db.fts_enabled(conn) else 'LIKE fallback'}")
    print()
    for label, key in [
        ("Item types", "item_count"), ("Total units", "total_units"),
        ("Available", "units_available"), ("Checked out", "units_out"),
        ("Open loans", "open_loan_count"), ("Overdue", "overdue_count"),
        ("Low stock", "low_stock_count"), ("People", "person_count"),
        ("Logged changes", "event_count"),
    ]:
        print(f"  {label:<16}{info[key]:>8}")
    return 0


def _resolve_item(conn, token: str):
    """Accept an item id or a barcode, so the CLI works with a scanner too."""
    from .search import resolve_scan

    if token.isdigit():
        return service.get_item(conn, int(token))
    item = resolve_scan(conn, token)
    if item is None:
        raise StockroomError(f"No item matches {token!r}.")
    return item


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockroom",
        description="CIS Stockroom Inventory System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Every mutating")[0].split("    stockroom init")[0].strip(),
    )
    parser.add_argument("--version", action="version", version=f"stockroom {__version__}")
    parser.add_argument(
        "--actor", help="who to record in the audit log, e.g. \"Carter <c@rit.edu>\""
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or upgrade the database").set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="import items from a CSV file")
    p.add_argument("path")
    p.add_argument("--commit", action="store_true",
                   help="actually write (default is a dry run)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="write the inventory to stdout as CSV")
    p.add_argument("--all", action="store_true", help="include archived items")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("items", help="list items")
    p.add_argument("--all", action="store_true", help="include archived")
    p.add_argument("--out", action="store_true", help="only items with units out")
    p.add_argument("--low", action="store_true", help="only low-stock items")
    p.add_argument("--unit", help="only this storage unit")
    p.set_defaults(func=cmd_items)

    p = sub.add_parser("checkout", help="check an item out to someone")
    p.add_argument("item", help="item id or barcode")
    p.add_argument("email")
    p.add_argument("--name", help="borrower's name, if they are new")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--note")
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("return", help="return a loan")
    p.add_argument("loan", type=int, help="loan id (see `stockroom loans`)")
    p.add_argument("--qty", type=int, help="partial return quantity")
    p.add_argument("--note")
    p.set_defaults(func=cmd_return)

    p = sub.add_parser("loans", help="list open loans")
    p.add_argument("--overdue", action="store_true")
    p.set_defaults(func=cmd_loans)

    p = sub.add_parser("history", help="read the audit log")
    p.add_argument("--item", type=int, help="restrict to one item id")
    p.add_argument("--action", help="restrict to one action, e.g. loan.checkout")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    sub.add_parser("publish", help="rebuild the public page now").set_defaults(func=cmd_publish)

    p = sub.add_parser("backup", help="snapshot the database")
    p.add_argument("--output", help="write here instead of the rotating backup dir")
    p.set_defaults(func=cmd_backup)

    sub.add_parser("status", help="headline numbers").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Terse logging: the CLI reports problems itself, in its own words.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except StockroomError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
