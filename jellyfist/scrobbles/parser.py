from datetime import datetime
from typing import Iterator

from sqlmodel import Session, select

from jellyfist.enums import Platform
from jellyfist.models import TrackAlias, TrackAliasScrobble


class ScrobbleParser:
    platform: Platform

    def _iterate_scrobbles(self) -> Iterator[tuple[TrackAlias, datetime]]:
        raise NotImplementedError()

    def _scrobble_list_by_alias(self) -> Iterator[tuple[TrackAlias, list[datetime]]]:
        aliases: dict[str, tuple[TrackAlias, set[datetime]]] = {}
        for alias, date in self._iterate_scrobbles():
            if alias.repr not in aliases:
                aliases[alias.repr] = (alias, set())
            aliases[alias.repr][1].add(date)

        for alias, dates in aliases.values():
            yield alias, sorted(dates)

    def import_aliases_scrobbles(self, s: Session):
        aliases_imported = aliases_ignored = scrobbles_imported = scrobbles_ignored = 0
        for alias, dates in self._scrobble_list_by_alias():
            alias, created = TrackAlias.get_or_create(
                s, title=alias.title, artist=alias.artist, album=alias.album
            )
            if created:
                aliases_imported += 1
            else:
                aliases_ignored += 1

            new_scrobbles = self.import_scrobbles(s, alias, dates)
            scrobbles_imported += len(new_scrobbles)
            scrobbles_ignored += len(dates) - len(new_scrobbles)
            s.flush()
        s.commit()
        return aliases_imported, aliases_ignored, scrobbles_imported, scrobbles_ignored

    def import_scrobbles(self, s: Session, ta: TrackAlias, dates: list[datetime]) -> list[TrackAliasScrobble]:
        # search for date only, since this is a single-user database
        existing_dates = {
            tas.date for tas in s.exec(select(TrackAliasScrobble).where(TrackAliasScrobble.date.in_(dates)))
        }
        new_dates = [d for d in dates if d not in existing_dates]
        new_scrobbles = []
        for date in new_dates:
            scrobble = TrackAliasScrobble(
                date=date,
                alias_id=ta.id,
                platform=self.platform,
            )
            s.add(scrobble)
            new_scrobbles.append(scrobble)
        return new_scrobbles
