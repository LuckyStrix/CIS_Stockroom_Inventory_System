"""Operational pages: is this thing still working, and what does it tell us?

The `stockroom doctor` command answers the same question from a shell on the
Pi, which is the right place for it and the wrong place to expect anyone to
go. Nobody SSHes into a stockroom appliance once a month. This is the same
report, on a page staff already have open.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import diagnostics, reports
from .deps import get_conn, page, require_staff

router = APIRouter()


@router.get("/diagnostics", response_class=HTMLResponse)
def health_report(request: Request, check_remote: bool = False):
    """The full health report.

    The backup remote is only contacted when asked. It is a network round
    trip, and a page that hangs for ten seconds behind a slow campus link is a
    page nobody opens.
    """
    require_staff(request)
    report = diagnostics.run_all(get_conn(), skip_remote=not check_remote)
    return page(
        request,
        "diagnostics.html",
        report=report,
        checked_remote=check_remote,
    )


@router.get("/reports", response_class=HTMLResponse)
def usage_reports(request: Request, days: int = reports.DEFAULT_WINDOW_DAYS):
    """What a year of the audit log adds up to.

    All reads, all single queries over data already kept. The charts are
    server-rendered SVG -- see reports.bar_chart for why there is no charting
    library here.
    """
    require_staff(request)
    conn = get_conn()
    days = max(7, min(int(days), 3650))
    return page(
        request,
        "reports.html",
        days=days,
        headline=reports.headline(conn, days=days),
        most_borrowed=reports.most_borrowed(conn, days=days),
        never_borrowed=reports.never_borrowed(conn, days=days),
        durations=reports.loan_durations(conn, days=days),
        weeks=reports.busiest_weeks(conn),
        borrowers=reports.busiest_borrowers(conn, days=days),
        offenders=reports.overdue_offenders(conn, days=days),
        unaccounted=reports.unaccounted(conn),
        out_of_service=reports.out_of_service(conn),
        chart=reports.bar_chart,
    )
