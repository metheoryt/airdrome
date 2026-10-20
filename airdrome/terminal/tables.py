"""Rich tables for terminal output."""

from rich.table import Table

from airdrome.enums import Source
from airdrome.models import Track


def _table_row(t: Track) -> dict[str, str]:
    """One track as an ordered column -> cell mapping (the keys are the table's headers)."""
    xml_n = sum(1 for st in t.source_tracks if st.provider == Source.APPLE_XML)
    ams_n = sum(1 for st in t.source_tracks if st.provider == Source.APPLE_MS)
    return {
        "ID": str(t.id),
        "Title": t.title,
        "Artist": t.artist or "",
        "Album artist": t.album_artist or "",
        "Album": t.album or "",
        "Track #": str(t.track_n) if t.track_n else "",
        "Disc #": str(t.disc_n) if t.disc_n else "",
        "Year": str(t.year) if t.year else "",
        "Comp": "✅" if t.compilation else "",
        "Time": f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "",
        "Files": "\n".join([f.duration_str or "--:--" for f in t.files]) if len(t.files) else "",
        "Date added": t.date_added.strftime("%Y-%m-%d"),
        "❤️": "❤️" if t.loved else "",
        "Album ❤️": "❤️" if t.album_loved else "",
        "XML": str(xml_n) if xml_n else "",
        "AMS": str(ams_n) if ams_n else "",
    }


def dedup_table(key: str, tracks: list[Track], canons: list[int | None]) -> Table:
    """Table of one duplicate group: a row per track with the canon id it points at."""
    table = Table(title=f"Duplicates by {key}")
    table.add_column("Index", style="blue")
    table.add_column("Canon ID", style="blue")

    for h in _table_row(tracks[0]):
        style = "yellow"
        if h in ("Date added", "❤️", "Album ❤️", "Files"):
            style = "green"
        if h in ("XML", "AMS"):
            style = "red"
        table.add_column(h, style=style)

    for i, t in enumerate(tracks):
        row_kw = {}
        if t.twins:
            row_kw["style"] = "bold"
        if t.canon_id:
            row_kw["style"] = "dim"
        table.add_row(f"{i + 1}", f"{canons[i] or '-'}", *_table_row(t).values(), **row_kw)

    return table
