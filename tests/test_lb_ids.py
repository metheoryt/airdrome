"""ListenBrainz listens keep both the player-submitted and the service-mapped recording id."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from airdrome.cloud.listenbrainz.scrobble import ListenBrainzScrobbleParser
from airdrome.models import TrackAlias, TrackAliasScrobble


SUBMITTED = "11111111-1111-1111-1111-111111111111"
MAPPED = "22222222-2222-2222-2222-222222222222"


def _listen(ts: int, title: str, additional: dict | None, mapping: dict | None) -> str:
    meta: dict = {"track_name": title, "artist_name": "Artist", "release_name": "Album"}
    if additional is not None:
        meta["additional_info"] = additional
    if mapping is not None:
        meta["mbid_mapping"] = mapping
    return json.dumps({"listened_at": ts, "track_metadata": meta})


def test_lb_parser_keeps_both_ids(tmp_path, session):
    lines = [
        _listen(1_700_000_000, "Both", {"recording_mbid": SUBMITTED}, {"recording_mbid": MAPPED}),
        _listen(1_700_000_100, "Neither", None, None),
        _listen(1_700_000_200, "Null blocks", None, {"recording_mbid": None}),
    ]
    (tmp_path / "listens").mkdir()
    (tmp_path / "listens" / "2023_11.jsonl").write_text("\n".join(lines))

    parser = ListenBrainzScrobbleParser(tmp_path)
    records = list(parser._iterate_scrobbles())
    assert [(r.submitted_mbid, r.mapped_mbid) for r in records] == [
        (SUBMITTED, MAPPED),
        (None, None),
        (None, None),
    ]
    assert records[0].at == datetime.fromtimestamp(1_700_000_000, tz=UTC)

    # ...and the ids reach the stored scrobble rows.
    parser.import_aliases_scrobbles(session)
    both = session.scalars(select(TrackAlias).where(TrackAlias.title == "Both")).one()
    stored = session.scalars(select(TrackAliasScrobble).where(TrackAliasScrobble.alias_id == both.id)).one()
    assert (stored.submitted_mbid, stored.mapped_mbid) == (SUBMITTED, MAPPED)
