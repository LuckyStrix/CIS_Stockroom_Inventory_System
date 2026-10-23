"""`stockroom archive`: bulk archiving after a reconciliation.

It exists so a stocktake that finds a dozen items gone is one command, not a
dozen trips through the web UI -- which makes all-or-nothing the property that
matters. A typo in the middle of the list must not leave half of it archived.
"""

from __future__ import annotations

import argparse

from stockroom import cli, service


def _args(*items, reason="Not found in recount", commit=False):
    return argparse.Namespace(
        items=list(items), reason=reason, commit=commit, actor="Carter <c@rit.edu>"
    )


def _make(conn, actor, name):
    return service.create_item(conn, actor=actor, name=name, quantity=1, unit="C1", shelf="B")


def test_a_dry_run_archives_nothing(conn, actor, capsys):
    a, b = _make(conn, actor, "Scotch Tape"), _make(conn, actor, "Measuring tapes")

    assert cli.cmd_archive(_args(a.barcode, str(b.id))) == 0

    assert "Dry run -- would archive 2 item(s)" in capsys.readouterr().out
    assert not service.get_item(conn, a.id).is_archived
    assert not service.get_item(conn, b.id).is_archived
    assert not service.list_events(conn, action="item.archive")


def test_commit_archives_every_item_with_the_reason_and_actor(conn, actor):
    a, b = _make(conn, actor, "Scotch Tape"), _make(conn, actor, "Measuring tapes")

    assert cli.cmd_archive(_args(a.barcode, b.barcode, commit=True)) == 0

    for item in (a, b):
        assert service.get_item(conn, item.id).is_archived
        (event,) = service.list_events(conn, item_id=item.id, action="item.archive")
        assert "Not found in recount" in event.summary
        assert event.actor.startswith("Carter")


def test_one_unknown_barcode_archives_none_of_them(conn, actor, capsys):
    a, b = _make(conn, actor, "Scotch Tape"), _make(conn, actor, "Measuring tapes")

    assert cli.cmd_archive(_args(a.barcode, "CIS-999999", b.barcode, commit=True)) == 1

    captured = capsys.readouterr()
    assert "CIS-999999" in captured.out
    assert "Nothing was archived" in captured.err
    assert not service.get_item(conn, a.id).is_archived
    assert not service.get_item(conn, b.id).is_archived
    assert not service.list_events(conn, action="item.archive")


def test_an_item_on_loan_refuses_the_whole_batch(conn, actor, capsys):
    a, lent = _make(conn, actor, "Scotch Tape"), _make(conn, actor, "Tape Dispenser")
    service.checkout(conn, actor=actor, item_id=lent.id, person_name="Meg",
                     person_email="meg@rit.edu", quantity=1)

    assert cli.cmd_archive(_args(a.barcode, lent.barcode, commit=True)) == 1

    assert "checked out" in capsys.readouterr().out
    assert not service.get_item(conn, a.id).is_archived


def test_naming_an_item_twice_is_refused_not_silently_merged(conn, actor):
    a = _make(conn, actor, "Scotch Tape")

    assert cli.cmd_archive(_args(a.barcode, a.barcode, commit=True)) == 1

    assert not service.get_item(conn, a.id).is_archived


def test_the_parser_wires_it_up():
    args = cli.build_parser().parse_args(
        ["--actor", "Carter", "archive", "CIS-000005", "CIS-000008", "--reason", "gone"]
    )
    assert args.func is cli.cmd_archive
    assert args.items == ["CIS-000005", "CIS-000008"]
    assert args.commit is False
