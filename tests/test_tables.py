from airdrome.terminal.tables import _table_row
from tests.factories import make_track


def test_table_row_blank_year_is_empty_not_none(session) -> None:
    """A track with no year renders an empty cell, not the text "None"."""
    assert _table_row(make_track(session, title="x"))["Year"] == ""
    assert _table_row(make_track(session, title="y", year=1999))["Year"] == "1999"
