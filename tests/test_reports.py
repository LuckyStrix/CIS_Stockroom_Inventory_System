"""Usage reporting, and the end-to-end walk through everything phase 3 added.

The reports are all reads over data the system already keeps, so what is worth
testing is that they say true things -- particularly the ones somebody will
quote in a budget conversation.
"""

from __future__ import annotations

import re

import pytest

from stockroom import db, reports, service, stocktake
from stockroom.reports import Row


def _age_loan(conn, loan_id, *, out: str, back: str | None = None):
    """Backdate a loan, so the windowed reports have something to see."""
    conn.execute("UPDATE loan SET checked_out_at = ?, returned_at = ? WHERE id = ?",
                 (out, back, loan_id))
    conn.commit()


@pytest.fixture
def history(conn, actor, item, person):
    """A camera borrowed three times, a tripod once, a light meter never."""
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=2, unit="Unit A", shelf="1")
    tripod = service.create_item(conn, actor=actor, name="Tripod", quantity=2,
                                 unit="Unit A", shelf="Floor")
    service.create_item(conn, actor=actor, name="Light meter", quantity=1,
                        unit="Unit A", shelf="2")

    for n in range(3):
        loan = service.checkout(conn, actor=actor, item_id=camera.id,
                                person_id=person.id, quantity=1)
        _age_loan(conn, loan.id, out=f"2026-08-0{n + 1}T09:00:00Z",
                  back=f"2026-08-0{n + 3}T09:00:00Z")
    loan = service.checkout(conn, actor=actor, item_id=tripod.id,
                            person_id=person.id, quantity=1)
    _age_loan(conn, loan.id, out="2026-08-01T09:00:00Z",
              back="2026-08-11T09:00:00Z")
    return {"camera": camera, "tripod": tripod}


def test_most_borrowed_counts_trips_to_the_counter(conn, history):
    rows = reports.most_borrowed(conn)
    assert rows[0].label == "Canon EOS R5"
    assert rows[0].value == 3


def test_never_borrowed_finds_the_deaccession_candidates(conn, history):
    names = [r.label for r in reports.never_borrowed(conn)]
    assert "Light meter" in names
    assert "Canon EOS R5" not in names


def test_never_borrowed_says_when_it_last_went_out(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id)
    _age_loan(conn, loan.id, out="2020-01-01T09:00:00Z",
              back="2020-01-05T09:00:00Z")
    row = next(r for r in reports.never_borrowed(conn)
               if r.label == "SanDisk 64GB SD Card")
    assert row.detail == "last out 2020-01-01"


def test_median_duration_is_not_dragged_by_one_long_loan(conn, actor, item,
                                                         person):
    """A camera on a semester-long research project must not distort this."""
    for out, back in [("2026-08-01", "2026-08-02"), ("2026-08-03", "2026-08-04"),
                      ("2026-08-05", "2026-12-05")]:
        loan = service.checkout(conn, actor=actor, item_id=item.id,
                                person_id=person.id, quantity=1)
        _age_loan(conn, loan.id, out=f"{out}T09:00:00Z", back=f"{back}T09:00:00Z")

    row = next(r for r in reports.loan_durations(conn)
               if r.label == "SanDisk 64GB SD Card")
    assert row.value == 1.0, "the median is a day, even with a four-month outlier"


def test_busiest_borrowers(conn, history, person):
    rows = reports.busiest_borrowers(conn)
    assert rows[0].label == person.name
    assert rows[0].value == 4


def test_overdue_offenders_ignores_someone_with_one_late_return(conn, actor,
                                                                item, person):
    """One late return out of one is not a pattern."""
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, due_at="2020-01-01T00:00:00Z")
    _age_loan(conn, loan.id, out="2019-12-01T09:00:00Z", back=None)
    assert reports.overdue_offenders(conn, days=3650) == []


def test_overdue_offenders_reports_a_share_not_a_count(conn, actor, item,
                                                       person):
    for n in range(4):
        due = "2020-01-01T00:00:00Z" if n < 2 else "2099-01-01T00:00:00Z"
        loan = service.checkout(conn, actor=actor, item_id=item.id,
                                person_id=person.id, due_at=due)
        _age_loan(conn, loan.id, out="2019-12-01T09:00:00Z", back=None)

    rows = reports.overdue_offenders(conn, days=3650)
    assert rows[0].value == 50
    assert rows[0].detail == "2 late of 4"


def test_unaccounted_is_the_list_that_justifies_a_budget(conn, actor, item):
    service.open_hold(conn, actor=actor, item_id=item.id, state="gone",
                      quantity=2)
    service.open_hold(conn, actor=actor, item_id=item.id, state="missing",
                      quantity=1)
    rows = reports.unaccounted(conn)
    assert rows[0].value == 3


def test_out_of_service_excludes_things_that_are_merely_lost(conn, actor, item):
    service.open_hold(conn, actor=actor, item_id=item.id, state="gone",
                      quantity=1)
    service.open_hold(conn, actor=actor, item_id=item.id, state="repair",
                      quantity=2)
    rows = reports.out_of_service(conn)
    assert len(rows) == 1
    assert rows[0].value == 2


def test_a_closed_hold_stops_being_reported(conn, actor, item):
    hold = service.open_hold(conn, actor=actor, item_id=item.id, state="broken")
    service.close_hold(conn, actor=actor, hold_id=hold.id)
    assert reports.out_of_service(conn) == []


def test_the_window_is_respected(conn, history):
    assert reports.most_borrowed(conn, days=365)
    assert reports.headline(conn, days=1)["loans"] == 0


# ---------------------------------------------------------------------------
# the charts
# ---------------------------------------------------------------------------


def test_a_chart_is_svg_with_no_inline_style():
    """The CSP has no 'unsafe-inline': a style="" bar renders invisible."""
    svg = reports.bar_chart([Row("Camera", 5), Row("Tripod", 2)])
    assert svg.startswith("<svg")
    assert "style=" not in svg
    assert 'class="chart-bar"' in svg


def test_chart_bars_are_proportional():
    svg = reports.bar_chart([Row("Big", 10), Row("Small", 1)])
    widths = [int(w) for w in re.findall(r'<rect[^>]*width="(\d+)"', svg)]
    assert widths[0] > widths[1] * 5


def test_an_item_name_cannot_break_out_of_the_chart():
    """Labels are item names, which are whatever somebody typed."""
    svg = reports.bar_chart([Row('</text><script>alert(1)</script>', 1)])
    assert "<script>" not in svg
    assert "&lt;/text&gt;" in svg


def test_an_empty_chart_renders_nothing_rather_than_a_broken_axis():
    assert reports.bar_chart([]) == ""


def test_a_chart_of_zeroes_does_not_divide_by_zero():
    svg = reports.bar_chart([Row("Nothing", 0), Row("Also nothing", 0)])
    assert "<svg" in svg


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_the_whole_phase_three_workflow(conn, actor, person):
    """A tracked camera goes out, comes back broken, and is found missing.

    This is the walkthrough from the plan: each stage's machinery meeting the
    next one's, all reading the same derived availability figure.
    """
    # Stage B: a tracked item with two individually identified bodies.
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=2, unit="Unit A", shelf="1",
                                 tracked=True)
    first = service.create_unit(conn, actor=actor, item_id=camera.id,
                                asset_tag="CIS-U-1")
    service.create_unit(conn, actor=actor, item_id=camera.id, asset_tag="CIS-U-2")
    assert service.get_item(conn, camera.id).available == 2

    # Stage C: it goes out in a basket with something else.
    tripod = service.create_item(conn, actor=actor, name="Tripod", quantity=1,
                                 unit="Unit A", shelf="Floor")
    loans = service.checkout_many(conn, actor=actor, person_id=person.id,
                                  lines=[(camera.id, 1), (tripod.id, 1)])
    assert service.get_item(conn, camera.id).available == 1

    # Stage B again: it comes back damaged, in one action at the counter.
    camera_loan = next(l for l in loans if l.item_id == camera.id)
    service.return_loan(conn, actor=actor, loan_id=camera_loan.id,
                        condition="broken", unit_id=first.id,
                        note="bent lens mount")
    after = service.get_item(conn, camera.id)
    assert (after.out_qty, after.held_qty, after.available) == (0, 1, 1)

    # The public page agrees, and says nothing about why.
    from stockroom.publish.render import build_payload, render_json
    published = next(i for i in build_payload(conn)["items"]
                     if i["name"] == "Canon EOS R5")
    assert published["available"] == 1 and published["quantity"] == 2
    assert "bent lens mount" not in render_json(conn)

    # Stage D: a stocktake of Unit A. Only one camera is on the shelf (one is
    # broken), and the tripod is still out, so scanning one camera is correct.
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit A")
    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          code="CIS-U-2")
    result = stocktake.finish_stocktake(conn, actor=actor,
                                        stocktake_id=session.id)
    assert result.is_clean, [d.item_name for d in result.problems]

    # Stage E: the reports see the loss once the second body goes missing too.
    service.open_hold(conn, actor=actor, item_id=camera.id, state="missing",
                      quantity=1, note="not found at the next count")
    assert reports.unaccounted(conn)[0].value == 1
    assert service.get_item(conn, camera.id).available == 0

    # Stage A: through all of it, the audit chain still verifies.
    chain = service.verify_audit_chain(conn)
    assert chain.ok, chain
    assert chain.head == service.audit_head(conn)

    # And `stockroom doctor` is happy about the parts it can see.
    from stockroom import diagnostics
    report = diagnostics.run_all(conn, skip_remote=True)
    assert "audit chain" not in [c.name for c in report.failures]
    assert "data consistency" not in [c.name for c in report.failures]
