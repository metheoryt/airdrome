from collections import defaultdict
from datetime import UTC, datetime

from rich.progress import TextColumn
from sqlmodel import Session, delete, func, select

from airdrome.console import console, make_progress
from airdrome.models import Track, TrackAlias, TrackAliasScrobble, TrackFile, engine

from ..models import AlbumArtist, Annotation, MediaFile, Scrobbles, User, get_nv_engine


def sync_tracks_plays_to_navi(username: str, reset: bool):
    with Session(engine) as s, Session(get_nv_engine()) as nvs:
        TrackSyncer(username, reset).sync_all(s, nvs)


def _normalize_dates(dts: list[datetime | None]) -> list[datetime]:
    return [v.replace(tzinfo=UTC) for v in dts if v]


class TrackSyncer:
    def __init__(self, username: str, reset: bool):
        self.username = username
        self.reset = reset
        self._user: User | None = None
        self._item_play_count: dict[str, int] = defaultdict(int)
        self._item_latest_play: dict[str, datetime] = {}

    def get_user(self, nvs: Session) -> User:
        if self._user is None:
            self._user = nvs.exec(select(User).where(User.user_name == self.username)).one()
        return self._user

    @staticmethod
    def _get_mediafile(track: Track, nvs: Session) -> MediaFile | None:
        mf = track.main_file
        if not mf or not mf.navidrome_path:
            return None
        return nvs.exec(select(MediaFile).where(MediaFile.path == mf.navidrome_path)).one_or_none()

    def _goc_annotation(
        self, item_id: str, item_type: Annotation.ItemType, nvs: Session
    ) -> tuple[Annotation, bool]:
        user = self.get_user(nvs)
        ann = nvs.exec(
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
        self, mf: MediaFile, track: Track, s: Session, nvs: Session
    ) -> tuple[int, datetime | None, datetime | None]:
        user = self.get_user(nvs)

        for scr in s.exec(select(TrackAliasScrobble).join(TrackAlias).where(TrackAlias.track_id == track.id)):
            submission_time = int(scr.date.timestamp())
            exists = nvs.exec(
                select(Scrobbles).where(
                    Scrobbles.user_id == user.id,
                    Scrobbles.media_file_id == mf.id,
                    Scrobbles.submission_time == submission_time,
                )
            ).one_or_none()
            if not exists:
                nvs.add(Scrobbles(user_id=user.id, media_file_id=mf.id, submission_time=submission_time))

        nvs.flush()

        cnt, latest_time, first_time = nvs.exec(
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
        track: Track,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        apple_track = track.apple_tracks[0] if track.apple_tracks else None

        date_candidates = [mf.created_at, first_play]
        if apple_track:
            date_candidates.append(apple_track.date_added)
        valid_dates = _normalize_dates(date_candidates)
        if valid_dates:
            earliest = min(valid_dates)
            mf.created_at = earliest
            mf.birth_time = earliest

        track_ann, _ = self._goc_annotation(mf.id, Annotation.ItemType.MEDIA_FILE, nvs)
        track_ann.play_count = play_count
        track_ann.play_date = latest_play

        if apple_track:
            if apple_track.rating and not apple_track.rating_computed:
                track_ann.rating = apple_track.rating
                track_ann.rated_at = apple_track.date_added
            if apple_track.loved:
                track_ann.starred = True
                track_ann.starred_at = apple_track.date_added

    def update_album_annotation(
        self,
        mf: MediaFile,
        track: Track,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        apple_track = track.apple_tracks[0] if track.apple_tracks else None

        date_candidates = [mf.album_model.created_at, first_play]
        if apple_track:
            date_candidates.append(apple_track.date_added)
        valid_dates = _normalize_dates(date_candidates)
        if valid_dates:
            mf.album_model.created_at = min(valid_dates)

        album_ann, _ = self._goc_annotation(mf.album_id, Annotation.ItemType.ALBUM, nvs)
        self._add_play_count_date(album_ann, play_count, latest_play)

        if apple_track:
            if not album_ann.rating and apple_track.album_rating and not apple_track.album_rating_computed:
                album_ann.rating = apple_track.album_rating
                album_ann.rated_at = apple_track.date_added
            if not album_ann.starred and apple_track.album_loved:
                album_ann.starred = True
                album_ann.rated_at = apple_track.date_added

    def update_artist_annotations(
        self, mf: MediaFile, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        stmt = select(AlbumArtist.artist_id).where(
            AlbumArtist.album_id == mf.album_id,
            AlbumArtist.role.in_(["albumartist", "artist"]),
        )
        for artist_id in nvs.exec(stmt):
            artist_ann, _ = self._goc_annotation(artist_id, Annotation.ItemType.ARTIST, nvs)
            self._add_play_count_date(artist_ann, play_count, latest_play)

    def update_track(self, track: Track, s: Session, nvs: Session) -> int:
        mf = self._get_mediafile(track, nvs)
        if not mf:
            return 0

        play_count, latest_play, first_play = self.update_scrobbles(mf, track, s, nvs)
        self.update_media_file(mf, track, nvs, play_count, latest_play, first_play)
        self.update_album_annotation(mf, track, nvs, play_count, latest_play, first_play)
        self.update_artist_annotations(mf, nvs, play_count, latest_play)
        nvs.flush()
        return play_count

    def sync_all(self, s: Session, nvs: Session):
        if self.reset:
            latest_scrobble = s.exec(
                select(TrackAliasScrobble).order_by(TrackAliasScrobble.date.desc())
            ).first()
            if latest_scrobble:
                res = nvs.exec(
                    delete(Scrobbles).where(Scrobbles.submission_time < latest_scrobble.date.timestamp())
                )
                nvs.commit()
                console.print(
                    f"[yellow]deleted {res.rowcount} scrobbles older than "
                    f"{latest_scrobble.date.isoformat()}[/yellow]"
                )

        stmt = (
            select(Track)
            .join(TrackFile, (TrackFile.track_id == Track.id) & (TrackFile.is_main.is_(True)))
            .where(TrackFile.library_path.is_not(None))
        )
        total = s.exec(select(func.count()).select_from(stmt.subquery())).one()

        i = pc = 0
        with make_progress(TextColumn("  [cyan]{task.fields[plays]}[/cyan] plays")) as progress:
            task = progress.add_task("Syncing tracks to Navidrome", total=total, plays=0)
            for track in s.exec(stmt):
                i += 1
                pc += self.update_track(track, s, nvs)
                progress.update(task, advance=1, plays=pc)

        nvs.commit()
        console.print(f"[green]{i} tracks synced with {pc} total plays[/green]")
