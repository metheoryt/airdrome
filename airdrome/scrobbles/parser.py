from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import make_progress
from airdrome.enums import Source
from airdrome.models import TrackAlias, TrackAliasScrobble


@dataclass
class ScrobbleRecord:
    """One parsed play: its alias, when it happened, and any MusicBrainz ids the listen carried."""

    alias: TrackAlias
    at: datetime
    submitted_mbid: str | None = None  # sent by the player that knew the file's id
    mapped_mbid: str | None = None  # the service's own text-match guess; only ever a weak hint


@dataclass
class ImportStats:
    aliases_created: int = 0
    aliases_ignored: int = 0
    aliases_skipped: int = 0
    scrobbles_created: int = 0
    scrobbles_ignored: int = 0


class ScrobbleParser:
    platform: Source

    def _iterate_scrobbles(self) -> Iterator[ScrobbleRecord]:
        raise NotImplementedError()

    def _scrobble_list_by_alias(self) -> Iterator[tuple[TrackAlias, list[ScrobbleRecord]]]:
        aliases: dict[str, tuple[TrackAlias, dict[datetime, ScrobbleRecord]]] = {}
        for record in self._iterate_scrobbles():
            if record.alias.repr not in aliases:
                aliases[record.alias.repr] = (record.alias, {})
            # One record per timestamp (single-user database); the first one seen wins.
            aliases[record.alias.repr][1].setdefault(record.at, record)

        with make_progress(transient=True) as progress:
            task = progress.add_task("Scrobbles", total=len(aliases))
            for alias, by_date in aliases.values():
                yield alias, [by_date[d] for d in sorted(by_date)]
                progress.advance(task)

    def import_aliases_scrobbles(self, s: Session) -> ImportStats:
        stats = ImportStats()
        for alias, records in self._scrobble_list_by_alias():
            new_scrobbles = self.get_fresh_scrobbles(s, records)
            stats.scrobbles_created += len(new_scrobbles)
            stats.scrobbles_ignored += len(records) - len(new_scrobbles)
            if not new_scrobbles:
                stats.aliases_skipped += 1
                continue

            alias, created = TrackAlias.get_or_create(
                s, title=alias.title, artist=alias.artist, album=alias.album
            )
            if created:
                stats.aliases_created += 1
            else:
                stats.aliases_ignored += 1

            alias.scrobbles.extend(new_scrobbles)
            s.flush()

        return stats

    def get_fresh_scrobbles(self, s: Session, records: list[ScrobbleRecord]) -> list[TrackAliasScrobble]:
        """Scrobbles for the records whose timestamp is not stored yet, carrying their listen ids."""
        # search for date only, since this is a single-user database
        by_date = {r.at: r for r in records}
        existing_dates = {
            scrobble.date
            for scrobble in s.scalars(select(TrackAliasScrobble).where(TrackAliasScrobble.date.in_(by_date)))
        }
        return [
            TrackAliasScrobble(
                date=date,
                platform=self.platform,
                submitted_mbid=record.submitted_mbid,
                mapped_mbid=record.mapped_mbid,
            )
            for date, record in by_date.items()
            if date not in existing_dates
        ]
