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
    stockroom doctor                     check the system is still healthy
    stockroom report                     usage, and what nobody borrows

    stockroom user create --admin        make the first administrator
    stockroom user list                  accounts, pending first
    stockroom user approve a@rit.edu     activate a pending signup
    stockroom user role a@rit.edu staff  change someone's role
    stockroom user disable a@rit.edu     switch an account off
    stockroom user passwd a@rit.edu      reset a password
    stockroom sessions revoke a@rit.edu  sign someone out everywhere
    stockroom benchmark-hash             tune password hashing for this machine

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

from . import (
    __version__,
    accounts,
    backup_targets,
    config,
    csvio,
    db,
    diagnostics,
    reports,
    security,
    service,
)
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
    """Snapshot the database, verify it, then send it off the machine.

    Uses SQLite's online backup API, so this is safe to run from cron while
    the web service is live and serving checkouts.

    The snapshot is verified before it counts: a database corrupted by a
    failing SD card copies without complaint, so an unchecked nightly job
    produces a month of unusable files and reports success every night.

    Off-box copies (a USB stick, an rclone remote) are attempted after the
    local snapshot succeeds. A failure there never invalidates the local copy,
    but it does set a non-zero exit code, because a backup that quietly stops
    leaving the building is only discovered on the day it was needed.
    """
    _conn()
    target = Path(args.output) if args.output else (
        config.BACKUP_DIR / f"stockroom-{db.utcnow().replace(':', '')}.db"
    )
    try:
        db.backup(target)
    except db.BackupCorrupt as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("The snapshot was discarded. Run `stockroom doctor` -- the "
              "live database may be damaged.", file=sys.stderr)
        return 1
    print(f"Wrote {target} ({target.stat().st_size:,} bytes, verified)")

    if args.output is None and config.BACKUP_KEEP > 0:
        snapshots = sorted(config.BACKUP_DIR.glob("stockroom-*.db"))
        for stale in snapshots[: max(0, len(snapshots) - config.BACKUP_KEEP)]:
            stale.unlink()
            print(f"Pruned {stale.name}")

    if args.no_upload:
        return 0

    targets = backup_targets.configured_targets()
    if not targets:
        return 0
    failures = backup_targets.copy_to_targets(target, targets)
    for target_obj in targets:
        if not any(f.target == target_obj.name for f in failures):
            print(f"Copied to {target_obj.name}")
    for failure in failures:
        print(f"Error: could not copy to {failure}", file=sys.stderr)
    return 1 if failures else 0


def cmd_report(args) -> int:
    """Usage figures, for the conversation about next year's budget."""
    conn = _conn()
    head = reports.headline(conn, days=args.days)
    print(f"CIS Stockroom — the last {head['window_days']} days")
    print(f"  {head['loans']} checkout(s), {head['units']} unit(s), "
          f"{head['borrowers']} borrower(s)")

    for title, rows, suffix in [
        ("Borrowed most often", reports.most_borrowed(conn, days=args.days), "loans"),
        ("Typical days out (median)", reports.loan_durations(conn, days=args.days), "days"),
        ("Busiest borrowers", reports.busiest_borrowers(conn, days=args.days), "loans"),
        ("Unaccounted for", reports.unaccounted(conn), "units"),
        ("Out of service", reports.out_of_service(conn), "units"),
    ]:
        print(f"\n{title}")
        if not rows:
            print("  (nothing)")
            continue
        for row in rows[: args.limit]:
            detail = f"  ({row.detail})" if row.detail else ""
            print(f"  {row.label[:38]:<38}{row.display:>7} {suffix}{detail}")

    stale = reports.never_borrowed(conn, days=args.days)
    print(f"\nNot borrowed in {args.days} days: {len(stale)} item(s)")
    for row in stale[: args.limit]:
        print(f"  {row.label[:38]:<38}{row.display:>7} owned  ({row.detail})")
    return 0


def cmd_doctor(args) -> int:
    """Check that the system is still healthy, and say so out loud.

    Written for a machine nobody watches. Every check is a read, so this is
    safe to run at any time, and it exits non-zero on failure so the nightly
    timer records a problem rather than a green tick.
    """
    conn = _conn()
    report = diagnostics.run_all(conn, skip_remote=args.skip_remote)

    for check in report.checks:
        print(f"  {check.mark} {check.name:<24}{check.detail}")

    print()
    if report.ok:
        print(f"All {len(report.checks)} checks passed.")
        return 0
    print(f"{len(report.failures)} of {len(report.checks)} checks FAILED:")
    for check in report.failures:
        print(f"  - {check.name}: {check.detail}")
    return 1


def cmd_prune(args) -> int:
    """Nightly housekeeping: expired sessions and old login attempts.

    Neither is needed once it is stale, and the auth_attempt table in
    particular grows with every failed login. Run from the backup timer.
    """
    from . import accounts as accounts_module

    conn = _conn()
    sessions = accounts_module.prune_sessions(conn)
    with db.transaction(conn):
        attempts = security.prune_auth_attempts(conn, keep_days=args.keep_days)
    print(f"Pruned {sessions} expired session(s) and {attempts} old login attempt(s).")
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
        ("Out of service", "units_held"), ("Unaccounted", "units_unaccounted"),
        ("Low stock", "low_stock_count"), ("People", "person_count"),
        ("Logged changes", "event_count"),
    ]:
        print(f"  {label:<16}{info[key]:>8}")
    return 0


# ---------------------------------------------------------------------------
# accounts
#
# Creating an administrator is CLI-only on purpose: there must never be an
# unauthenticated route to privilege over the network, so the first admin is
# made by someone who already has shell access to the Pi.
# ---------------------------------------------------------------------------
def _prompt_password(confirm: bool = True) -> str:
    password = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Confirm password: ") != password:
        raise StockroomError("Passwords did not match.")
    return password


def cmd_user_create(args) -> int:
    conn = _conn()
    password = args.password or _prompt_password()
    try:
        account = accounts.register(
            conn,
            first_name=args.first_name,
            last_name=args.last_name,
            email=args.email,
            password=password,
            role="admin" if args.admin else args.role,
            # A CLI-created account is made by someone with shell access, so
            # it is active immediately -- there is nobody else to approve it.
            status="active",
            actor=_actor(args),
        )
    except (StockroomError, security.PasswordError) as exc:
        print(f"Could not create the account: {exc}", file=sys.stderr)
        return 1
    print(f"Created {account.name} <{account.email}> as {account.role} ({account.status}).")
    return 0


def cmd_user_list(args) -> int:
    conn = _conn()
    rows = accounts.list_accounts(conn, status=args.status)
    if not rows:
        print("No accounts.")
        return 0
    print(f"{'ID':<5}{'NAME':<26}{'EMAIL':<30}{'ROLE':<11}STATUS")
    for a in rows:
        print(f"{a.id:<5}{a.name[:25]:<26}{a.email[:29]:<30}{a.role:<11}{a.status}")
    print(f"\n{len(rows)} account(s).")
    return 0


def _find_account(conn, email: str):
    account = accounts.find_by_email(conn, email)
    if account is None:
        raise StockroomError(f"No account for {email}.")
    return account


def cmd_user_approve(args) -> int:
    conn = _conn()
    account = _find_account(conn, args.email)
    approver = accounts.list_accounts(conn, role="admin")
    accounts.approve(
        conn, actor=_actor(args), account_id=account.id,
        approved_by=approver[0] if approver else account,
    )
    print(f"Approved {account.name} <{account.email}>.")
    return 0


def cmd_user_role(args) -> int:
    conn = _conn()
    account = _find_account(conn, args.email)
    updated = accounts.set_role(
        conn, actor=_actor(args), account_id=account.id, role=args.role
    )
    print(f"{updated.name} is now {updated.role}.")
    return 0


def cmd_user_disable(args) -> int:
    conn = _conn()
    account = _find_account(conn, args.email)
    accounts.set_status(
        conn, actor=_actor(args), account_id=account.id,
        status="active" if args.enable else "disabled",
    )
    print(f"{account.name} is now {'active' if args.enable else 'disabled'}.")
    return 0


def cmd_user_passwd(args) -> int:
    conn = _conn()
    account = _find_account(conn, args.email)
    try:
        accounts.change_password(
            conn, actor=_actor(args), account_id=account.id,
            new_password=_prompt_password(),
        )
    except (StockroomError, security.PasswordError) as exc:
        print(f"Could not change the password: {exc}", file=sys.stderr)
        return 1
    print(f"Password changed for {account.name}. All their sessions were revoked.")
    return 0


def cmd_sessions_revoke(args) -> int:
    conn = _conn()
    account = _find_account(conn, args.email)
    count = accounts.revoke_all_sessions(
        conn, actor=_actor(args), account_id=account.id
    )
    print(f"Revoked {count} session(s) for {account.name}.")
    return 0


def cmd_benchmark_hash(args) -> int:
    """Time scrypt on this machine and recommend parameters.

    Worth running on the Pi itself: it is a good deal slower than a laptop,
    and the right setting is the strongest one that still signs somebody in
    without them thinking the page has hung.
    """
    import time

    print(f"Current setting: n=2^{security.SCRYPT_N.bit_length() - 1}, "
          f"r={security.SCRYPT_R}, p={security.SCRYPT_P}\n")
    print(f"{'PARAMETERS':<28}{'TIME':>10}{'MEMORY':>10}   VERDICT")

    # OWASP lists these as equivalent minimums; they differ in how the work is
    # split between memory and passes, which matters a lot on a small machine.
    options = [(2**14, 8, 5), (2**15, 8, 3), (2**16, 8, 2), (2**17, 8, 1)]
    best = None
    for n, r, p in options:
        start = time.perf_counter()
        security.hash_password("benchmark placeholder", n=n, r=r, p=p)
        elapsed = (time.perf_counter() - start) * 1000
        memory = 128 * n * r / 1024 / 1024
        # Memory is capped as well as time. scrypt's memory cost is what makes
        # it hard to attack, but it is also allocated per concurrent login --
        # and a handful of simultaneous sign-ins at 128 MiB each will push a
        # 2 GB Pi into swap, turning a security control into an outage.
        if elapsed > float(args.target):
            verdict = "slow for a login"
        elif memory > float(args.max_memory):
            verdict = f"needs more than {args.max_memory} MiB per login"
        else:
            verdict = "comfortable"
            best = (n, r, p, elapsed)
        print(f"n=2^{n.bit_length()-1:<3} r={r} p={p:<14}{elapsed:>8.0f}ms"
              f"{memory:>8.0f}MiB   {verdict}")

    print()
    if best:
        n, r, p, elapsed = best
        print(f"Recommended: n=2^{n.bit_length()-1}, r={r}, p={p} "
              f"({elapsed:.0f}ms, {128 * n * r / 1024 / 1024:.0f}MiB per login).")
        if (n, r, p) != (security.SCRYPT_N, security.SCRYPT_R, security.SCRYPT_P):
            print("Set these in src/stockroom/security.py. Existing passwords keep")
            print("working and are re-hashed automatically on next sign-in.")
        else:
            print("That is what is already configured. Nothing to do.")
    else:
        print(f"Every option exceeds {args.target}ms on this machine. The lowest")
        print("OWASP-listed setting is already in use; keep it and accept the wait.")
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
    p.add_argument("--no-upload", action="store_true",
                   help="skip the configured off-box copies")
    p.set_defaults(func=cmd_backup)

    sub.add_parser("status", help="headline numbers").set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="usage figures and deaccession candidates")
    p.add_argument("--days", type=int, default=reports.DEFAULT_WINDOW_DAYS,
                   help="window to report on (default 365)")
    p.add_argument("--limit", type=int, default=10,
                   help="rows per section (default 10)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("doctor", help="check the system is still healthy")
    p.add_argument("--skip-remote", action="store_true",
                   help="do not contact the backup remote (offline, or in a hurry)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("prune", help="delete expired sessions and old login attempts")
    p.add_argument("--keep-days", type=int, default=90,
                   help="how long to keep login attempt records (default 90)")
    p.set_defaults(func=cmd_prune)

    # -- accounts ----------------------------------------------------------
    user = sub.add_parser("user", help="manage accounts").add_subparsers(
        dest="user_command", required=True
    )

    p = user.add_parser("create", help="create an account (the only way to make an admin)")
    p.add_argument("--first-name", required=True)
    p.add_argument("--last-name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="prompted for if omitted, which is safer")
    p.add_argument("--admin", action="store_true", help="make them an administrator")
    p.add_argument("--role", default="requester", choices=accounts.ROLES)
    p.set_defaults(func=cmd_user_create)

    p = user.add_parser("list", help="list accounts")
    p.add_argument("--status", choices=accounts.STATUSES)
    p.set_defaults(func=cmd_user_list)

    p = user.add_parser("approve", help="activate a pending account")
    p.add_argument("email")
    p.set_defaults(func=cmd_user_approve)

    p = user.add_parser("role", help="change an account's role")
    p.add_argument("email")
    p.add_argument("role", choices=accounts.ROLES)
    p.set_defaults(func=cmd_user_role)

    p = user.add_parser("disable", help="switch an account off")
    p.add_argument("email")
    p.add_argument("--enable", action="store_true", help="re-enable instead")
    p.set_defaults(func=cmd_user_disable)

    p = user.add_parser("passwd", help="set a new password")
    p.add_argument("email")
    p.set_defaults(func=cmd_user_passwd)

    sessions = sub.add_parser("sessions", help="manage sessions").add_subparsers(
        dest="sessions_command", required=True
    )
    p = sessions.add_parser("revoke", help="sign an account out everywhere")
    p.add_argument("email")
    p.set_defaults(func=cmd_sessions_revoke)

    p = sub.add_parser("benchmark-hash",
                       help="time password hashing and recommend parameters")
    p.add_argument("--target", default="600",
                   help="acceptable milliseconds per sign-in (default 600)")
    p.add_argument("--max-memory", default="64",
                   help="MiB of RAM one sign-in may use (default 64)")
    p.set_defaults(func=cmd_benchmark_hash)

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
    except PermissionError as exc:
        # Almost always the data directory, and almost always because the
        # command was run as the wrong user or against the wrong settings.
        # A bare traceback here sends people looking for a bug in the app.
        print(
            f"Error: permission denied opening {exc.filename or config.DB_PATH}\n"
            f"       The database is {config.DB_PATH}\n"
            f"       (from {config.ENV_FILE} if it exists, else the defaults).\n"
            f"       On the Pi, run as the service account:\n"
            f"           sudo -u stockroom /opt/stockroom/.venv/bin/stockroom ...",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
