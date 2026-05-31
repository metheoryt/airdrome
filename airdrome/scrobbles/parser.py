from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import make_progress
from airdrome.enums import Source
from airdrome.models import TrackAlias, TrackAliasScrobble


@dataclass
class ImportStats:
    aliases_created: int = 0
    aliases_ignored: int = 0
    aliases_skipped: int = 0
    scrobbles_created: int = 0
    scrobbles_ignored: int = 0


class ScrobbleParser:
    platform: Source

    def _iterate_scrobbles(self) -> Iterator[tuple[TrackAlias, datetime]]:
        raise NotImplementedError()

    def _scrobble_list_by_alias(self) -> Iterator[tuple[TrackAlias, list[datetime]]]:
        aliases: dict[str, tuple[TrackAlias, set[datetime]]] = {}
        for alias, date in self._iterate_scrobbles():
            if alias.repr not in aliases:
                aliases[alias.repr] = (alias, set())
            aliases[alias.repr][1].add(date)

        with make_progress(transient=True) as progress:
            task = progress.add_task("Scrobbles", total=len(aliases))
            for alias, dates in aliases.values():
                yield alias, sorted(dates)
                progress.advance(task)

    def import_aliases_scrobbles(self, s: Session) -> ImportStats:
        stats = ImportStats()
        for alias, dates in self._scrobble_list_by_alias():
            new_scrobbles = self.get_fresh_scrobbles(s, dates)
            stats.scrobbles_created += len(new_scrobbles)
            stats.scrobbles_ignored += len(dates) - len(new_scrobbles)
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

    def get_fresh_scrobbles(self, s: Session, dates: list[datetime]) -> list[TrackAliasScrobble]:
        # search for date only, since this is a single-user database
        dates_uniq = set(dates)
        existing_dates = {
            scrobble.date
            for scrobble in s.scalars(
                select(TrackAliasScrobble).where(TrackAliasScrobble.date.in_(dates_uniq))
            )
        }
        new_dates = dates_uniq.difference(existing_dates)
        new_scrobbles = []
        for date in new_dates:
            new_scrobbles.append(TrackAliasScrobble(date=date, platform=self.platform))
        return new_scrobbles
