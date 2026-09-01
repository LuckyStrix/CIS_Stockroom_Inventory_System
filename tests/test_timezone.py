"""Reading typed dates and printing stored ones on the stockroom's clock.

Storage is UTC everywhere and stays that way -- the timestamps sort
lexicographically, which the overdue query relies on. What these cover is the
two boundaries where a human is involved, both of which used to skip the
conversion entirely, in opposite directions:

* a time the system generated was true UTC and displayed as though it were
  local, so a 2pm checkout read as 18:00;
* a date somebody typed was local and had a Z stapled on, so a loan due
  "Friday" went overdue at 19:59 on Friday afternoon.
"""

from __future__ import annotations

import pytest

from stockroom import config, service
from stockroom.web.app import fmt_date, fmt_datetime
from stockroom.web.deps import local_to_utc, utc_to_local


def test_the_default_timezone_is_the_stockrooms():
    assert config.TIMEZONE_NAME == "America/New_York"


@pytest.mark.parametrize(
    "typed, expected",
    [
        # EDT, UTC-4
        ("2026-09-03T14:00", "2026-09-03T18:00:00Z"),
        # EST, UTC-5 -- the same wall-clock time on the other side of DST
        ("2026-01-15T14:00", "2026-01-15T19:00:00Z"),
    ],
)
def test_a_typed_time_is_read_as_local(typed, expected):
    assert local_to_utc(typed) == expected


def test_a_due_date_lasts_until_the_end_of_the_local_day():
    """"Due Friday" must not expire while the building is still open.

    Stapling a Z on gave 23:59:59Z, which is 19:59 in Rochester: a student
    with an item due Friday was overdue from Friday teatime.
    """
    assert local_to_utc("2026-09-03", end=True) == "2026-09-04T03:59:59Z"
    assert local_to_utc("2026-09-03") == "2026-09-03T04:00:00Z"


@pytest.mark.parametrize(
    "stored, shown",
    [
        ("2026-09-03T18:00:00Z", "03 Sep 2026, 14:00"),
        ("2026-01-15T19:00:00Z", "15 Jan 2026, 14:00"),
    ],
)
def test_a_stored_time_is_shown_as_local(stored, shown):
    assert fmt_datetime(stored) == shown


def test_a_time_survives_the_round_trip():
    assert fmt_datetime(local_to_utc("2026-09-03T14:00")) == "03 Sep 2026, 14:00"


def test_a_date_near_midnight_shows_the_right_day():
    """The failure that makes a timezone bug visible to a human.

    23:00 local on the 3rd is 03:00 UTC on the 4th; printed without
    converting back, the history said the loan happened tomorrow.
    """
    stored = local_to_utc("2026-09-03T23:00")
    assert stored == "2026-09-04T03:00:00Z"
    assert fmt_date(stored) == "03 Sep 2026"


@pytest.mark.parametrize("value", ["", None])
def test_nothing_in_gives_nothing_out(value):
    assert local_to_utc(value or "") is None
    assert utc_to_local(value) is None
    assert fmt_datetime(value) == "—"


def test_an_unparseable_value_is_passed_through_rather_than_raising():
    """The service layer rejects bad input; this must not 500 on the way."""
    assert local_to_utc("nonsense") == "nonsense"
    assert fmt_datetime("nonsense") == "—"


def test_an_already_utc_value_is_left_alone():
    assert local_to_utc("2026-09-03T18:00:00Z") == "2026-09-03T18:00:00Z"


def test_an_unknown_timezone_falls_back_instead_of_killing_the_service(caplog):
    """A typo in /etc/stockroom.env must not stop the Pi booting."""
    assert config._load_timezone("Not/AZone") is not None
    assert str(config._load_timezone("Not/AZone")) == "America/New_York"


def test_a_loan_due_today_is_not_overdue_until_the_local_day_ends(conn, actor,
                                                                 item, person):
    """The end-to-end version: the arithmetic that sent the wrong chasers."""
    due = local_to_utc("2026-09-03", end=True)
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=1, due_at=due)
    # 22:00 local on the due date is 02:00 UTC the next day: still fine.
    assert not loan.is_overdue("2026-09-04T02:00:00Z")
    # 00:30 local the following day is 04:30 UTC: now overdue.
    assert loan.is_overdue("2026-09-04T04:30:00Z")
