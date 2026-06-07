from collections import defaultdict
from datetime import UTC, datetime

from rich.progress import TextColumn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.console import done, make_progress
from airdrome.models import Track, TrackFile, TrackGroup, TrackPlay

from ..models import AlbumArtist, Annotation, MediaFile, Scrobbles, User, get_nv_engine


def sync_tracks_plays_to_navi(s: Session, username: str):
    with Session(get_nv_engine()) as nvs:
        TrackSyncer(username).sync_all(s, nvs)


def _normalize_dates(dts: list[datetime | None]) -> list[datetime]:
    return [v.replace(tzinfo=UTC) for v in dts if v]


class TrackSyncer:
    def __init__(self, username: str):
        self.username = username
        self._user: User | None = None
        self._item_play_count: dict[str, int] = defaultdict(int)
        self._item_latest_play: dict[str, datetime] = {}

    def get_user(self, nvs: Session) -> User:
        if self._user is None:
            self._user = nvs.scalars(select(User).where(User.user_name == self.username)).one()
        return self._user

    @staticmethod
    def _get_mediafile(group: TrackGroup, nvs: Session) -> MediaFile | None:
        mf = group.main_file
        if not mf or not mf.navidrome_path:
            return None
        return nvs.scalars(select(MediaFile).where(MediaFile.path == mf.navidrome_path)).one_or_none()

    def _goc_annotation(
        self, item_id: str, item_type: Annotation.ItemType, nvs: Session
    ) -> tuple[Annotation, bool]:
        user = self.get_user(nvs)
        ann = nvs.scalars(
            select(Annotation).where(
                Annotation.item_id == item_id,
                Annotation.item_type == item_type,
                Annotation.user_id == user.id,
            )
        ).one_or_none()
        if ann:
            return ann, False
        ann = Annotation(user_id=user.id, item_id=item_id, item_type=item_type)
        nvs.add(ann)
        return ann, True

    def _add_play_count_date(self, ann: Annotation, play_count: int, latest_play: datetime | None):
        self._item_play_count[ann.item_id] += play_count
        ann.play_count = self._item_play_count[ann.item_id]
        if latest_play:
            prev = self._item_latest_play.get(ann.item_id)
            self._item_latest_play[ann.item_id] = max(latest_play, prev) if prev else latest_play
            ann.play_date = self._item_latest_play[ann.item_id]

    def update_scrobbles(
        self, mf: MediaFile, group_ids: list[int], s: Session, nvs: Session
    ) -> tuple[int, datetime | None, datetime | None]:
        user = self.get_user(nvs)

        for play in s.scalars(select(TrackPlay).where(TrackPlay.track_id.in_(group_ids))):
            submission_time = int(play.played_at.timestamp())
            exists = nvs.scalars(
                select(Scrobbles).where(
                    Scrobbles.user_id == user.id,
                    Scrobbles.media_file_id == mf.id,
                    Scrobbles.submission_time == submission_time,
                )
            ).one_or_none()
            if not exists:
                nvs.add(Scrobbles(user_id=user.id, media_file_id=mf.id, submission_time=submission_time))

        nvs.flush()

        cnt, latest_time, first_time = nvs.execute(
            select(
                func.count(), func.max(Scrobbles.submission_time), func.min(Scrobbles.submission_time)
            ).where(Scrobbles.user_id == user.id, Scrobbles.media_file_id == mf.id)
        ).one()

        latest_dt = datetime.fromtimestamp(latest_time, tz=UTC) if latest_time else None
        first_dt = datetime.fromtimestamp(first_time, tz=UTC) if first_time else None
        return cnt, latest_dt, first_dt

    def update_media_file(
        self,
        mf: MediaFile,
        group: TrackGroup,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        # The group has existed since its earliest member was added; use that as
        # the representative "added" date for created_at and rating timestamps.
        added = group.date_added
        date_candidates = [mf.created_at, first_play, added]
        valid_dates = _normalize_dates(date_candidates)
        if valid_dates:
            earliest = min(valid_dates)
            mf.created_at = earliest
            mf.birth_time = earliest

        track_ann, _ = self._goc_annotation(mf.id, Annotation.ItemType.MEDIA_FILE, nvs)
        track_ann.play_count = play_count
        track_ann.play_date = latest_play

        if group.rating:
            track_ann.rating = group.rating
            track_ann.rated_at = added
        if group.loved:
            track_ann.starred = True
            track_ann.starred_at = added

    def update_album_annotation(
        self,
        mf: MediaFile,
        group: TrackGroup,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        added = group.date_added
        date_candidates = [mf.album_model.created_at, first_play, added]
        valid_dates = _normalize_dates(date_candidates)
        if valid_dates:
            mf.album_model.created_at = min(valid_dates)

        album_ann, _ = self._goc_annotation(mf.album_id, Annotation.ItemType.ALBUM, nvs)
        self._add_play_count_date(album_ann, play_count, latest_play)

        if not album_ann.rating and group.album_rating:
            album_ann.rating = group.album_rating
            album_ann.rated_at = added
        if not album_ann.starred and group.album_loved:
            album_ann.starred = True
            album_ann.rated_at = added

    def update_artist_annotations(
        self, mf: MediaFile, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        stmt = select(AlbumArtist.artist_id).where(
            AlbumArtist.album_id == mf.album_id,
            AlbumArtist.role.in_(["albumartist", "artist"]),
        )
        for artist_id in nvs.scalars(stmt):
            artist_ann, _ = self._goc_annotation(artist_id, Annotation.ItemType.ARTIST, nvs)
            self._add_play_count_date(artist_ann, play_count, latest_play)

    def update_track(self, track: Track, s: Session, nvs: Session) -> int:
        # `track` owns the organized main file; the MediaFile is keyed off it, but
        # plays and ratings are aggregated across its whole dedup group.
        group = track.group
        mf = self._get_mediafile(group, nvs)
        if not mf:
            return 0

        play_count, latest_play, first_play = self.update_scrobbles(mf, group.ids, s, nvs)
        self.update_media_file(mf, group, nvs, play_count, latest_play, first_play)
        self.update_album_annotation(mf, group, nvs, play_count, latest_play, first_play)
        self.update_artist_annotations(mf, nvs, play_count, latest_play)
        nvs.flush()
        return play_count

    def sync_all(self, s: Session, nvs: Session):
        stmt = (
            select(Track)
            .join(TrackFile, (TrackFile.track_id == Track.id) & (TrackFile.is_main.is_(True)))
            .where(TrackFile.library_path.is_not(None))
        )
        total = s.scalars(select(func.count()).select_from(stmt.subquery())).one()

        i = pc = 0
        with make_progress(TextColumn("  [cyan]{task.fields[plays]}[/cyan] plays")) as progress:
            task = progress.add_task("Syncing tracks to Navidrome", total=total, plays=0)
            for track in s.scalars(stmt):
                i += 1
                pc += self.update_track(track, s, nvs)
                progress.update(task, advance=1, plays=pc)

        nvs.commit()
        done(f"{i} tracks synced with {pc} total plays")
