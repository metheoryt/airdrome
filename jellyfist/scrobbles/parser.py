from datetime import datetime
from typing import Iterator

from sqlmodel import Session, select

from jellyfist.enums import Platform
from jellyfist.models import TrackAlias, TrackAliasScrobble
from .schemas import TrackScrobble, TrackAliasSchema


class ScrobbleParser:
    platform: Platform

    def _iterate_scrobbles(self) -> Iterator[TrackScrobble]:
        raise NotImplementedError()

    def _scrobble_list_by_alias(self) -> Iterator[tuple[TrackAliasSchema, list[datetime]]]:
        aliases: dict[str, tuple[TrackAliasSchema, set[datetime]]] = {}
        for scrobble in self._iterate_scrobbles():
            if scrobble.alias.id not in aliases:
                aliases[scrobble.alias.id] = (scrobble.alias, set())
            aliases[scrobble.alias.id][1].add(scrobble.date)

        for alias, dates in aliases.values():
            yield alias, sorted(dates)

    def import_aliases_scrobbles(self, s: Session):
        aliases_imported = aliases_ignored = scrobbles_imported = scrobbles_ignored = 0
        for alias, dates in self._scrobble_list_by_alias():
            ta, created = self.import_alias(s, alias)
            if created:
                aliases_imported += 1
            else:
                aliases_ignored += 1

            for date in dates:
                scrobble, s_created = self.import_scrobble(s, ta, date)
                if s_created:
                    scrobbles_imported += 1
                else:
                    scrobbles_ignored += 1
            s.flush()
        s.commit()
        return aliases_imported, aliases_ignored, scrobbles_imported, scrobbles_ignored

    def import_alias(self, s: Session, alias: TrackAliasSchema) -> tuple[TrackAlias, bool]:
        """Direct import of a single alias, without trying to match it."""
        stmt = select(TrackAlias).where(
            TrackAlias.title == alias.title,
            TrackAlias.artist == alias.artist,
            TrackAlias.album == alias.album,
        )
        ta = s.exec(stmt).one_or_none()
        if ta:
            return ta, False
        ta = TrackAlias(
            artist=alias.artist,
            album=alias.album,
            title=alias.title,
            artist_norm=alias.artist_norm,
            album_norm=alias.album_norm,
            title_norm=alias.title_norm,
        )
        s.add(ta)
        s.flush()
        return ta, True

    def import_scrobble(self, s: Session, ta: TrackAlias, date: datetime) -> tuple[TrackAliasScrobble, bool]:
        # search for date only, since this is a single-user database
        scrobble = s.exec(select(TrackAliasScrobble).where(TrackAliasScrobble.date == date)).one_or_none()
        if scrobble:
            return scrobble, False

        scrobble = TrackAliasScrobble(
            date=date,
            alias_id=ta.id,
            platform=self.platform,
        )
        s.add(scrobble)
        return scrobble, True
