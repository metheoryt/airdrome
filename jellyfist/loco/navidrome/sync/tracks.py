from collections import defaultdict
from datetime import datetime, UTC

from sqlmodel import Session, select, func, delete

from jellyfist.models import Track, TrackAlias, engine, TrackAliasScrobble
from ..models import Annotation, MediaFile, User, Scrobbles, AlbumArtist, engine as nv_engine, Album


def sync_tracks_plays_to_navi(username: str, reset: bool):
    with Session(engine) as s, Session(nv_engine) as nvs:
        TrackSyncer(username, reset).sync_all(s, nvs)


def _normalize_dates(dts: list[datetime | None]):
    return [v.replace(tzinfo=UTC) for v in dts if v]


class TrackSyncer:
    def __init__(self, username: str, reset: bool):
        self.username = username
        self.reset = reset
        self._user: User | None = None
        self._item_play_count = defaultdict(int)
        self._item_latest_play = dict()

    @staticmethod
    def _get_mediafile(track: Track, nvs: Session) -> MediaFile | None:
        return nvs.exec(select(MediaFile).where(MediaFile.path == track.path)).one_or_none()

    def get_user(self, nvs: Session):
        if self._user is None:
            self._user = nvs.exec(select(User).where(User.user_name == self.username)).one()
        return self._user

    def _goc_annotation(
        self, item_id: str, item_type: Annotation.ItemType, nvs: Session
    ) -> tuple[Annotation, bool]:
        """Get or create an Annotation for a MediaFile or an Album."""
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

        ann = Annotation(
            user_id=user.id,
            item_id=item_id,
            item_type=item_type,
        )
        nvs.add(ann)
        return ann, True

    def _add_play_count_date(self, ann: Annotation, play_count: int, latest_play: datetime | None):
        # set increment play count
        self._item_play_count[ann.item_id] += play_count
        ann.play_count = self._item_play_count[ann.item_id]

        # update the latest play date
        if latest_play:
            if ann.item_id not in self._item_latest_play:
                self._item_latest_play[ann.item_id] = latest_play
            self._item_latest_play[ann.item_id] = max(latest_play, self._item_latest_play[ann.item_id])
            ann.play_date = self._item_latest_play[ann.item_id]

    def update_media_file(
        self,
        mf: MediaFile,
        track: Track,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        # it seems that this is the Navidrome Date Added
        mf.created_at = min(_normalize_dates([mf.created_at, track.date_added, first_play]))
        mf.birth_time = min(_normalize_dates([mf.birth_time, track.date_added, first_play]))

        track_ann, created = self._goc_annotation(mf.id, Annotation.ItemType.MEDIA_FILE, nvs)

        track_ann.play_count = play_count
        track_ann.play_date = latest_play

        # Only update in one direction (has rating / starred). Don't unset ratings during sync.
        if track.rating and not track.rating_computed:
            track_ann.rating = track.rating
            track_ann.rated_at = track.date_added
        if track.loved:
            track_ann.starred = True
            track_ann.rated_at = track.date_added

    def update_album_annotation(
        self,
        mf: MediaFile,
        track: Track,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        # update album created_at
        mf.album_model.created_at = min(
            _normalize_dates([mf.album_model.created_at, track.date_added, first_play])
        )

        album_ann, created = self._goc_annotation(mf.album_id, Annotation.ItemType.ALBUM, nvs)

        # play count and date
        self._add_play_count_date(album_ann, play_count, latest_play)

        # rating
        if not album_ann.rating and track.album_rating and not track.album_rating_computed:
            album_ann.rating = track.album_rating
            album_ann.rated_at = track.date_added

        # starred
        if not album_ann.starred and track.album_loved:
            album_ann.starred = True
            album_ann.rated_at = track.date_added

    def update_artist_annotations(
        self, mf: MediaFile, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        roles = ["albumartist", "artist"]
        stmt = select(AlbumArtist.artist_id).where(
            AlbumArtist.album_id == mf.album_id, AlbumArtist.role.in_(roles)
        )
        for artist_id in nvs.exec(stmt):
            artist_ann, created = self._goc_annotation(artist_id, Annotation.ItemType.ARTIST, nvs)
            # play count and date
            self._add_play_count_date(artist_ann, play_count, latest_play)

    def update_annotations(
        self,
        track: Track,
        nvs: Session,
        play_count: int,
        latest_play: datetime | None,
        first_play: datetime | None,
    ):
        #
        # update track/album/artist annotations
        #
        mf = self._get_mediafile(track, nvs)
        if not mf:
            # no match @ Navi
            raise ValueError(f"No mediafile found for track {track.id}")

        self.update_media_file(mf, track, nvs, play_count, latest_play, first_play)
        self.update_album_annotation(mf, track, nvs, play_count, latest_play, first_play)
        self.update_artist_annotations(mf, nvs, play_count, latest_play)

        nvs.flush()
        return None

    def update_scrobbles(
        self, track: Track, s: Session, nvs: Session
    ) -> tuple[int, datetime | None, datetime | None]:
        user = self.get_user(nvs)
        mf = self._get_mediafile(track, nvs)
        if not mf:
            raise ValueError(f"No mediafile found for track {track.id}")

        scrobbles_stmt = select(TrackAliasScrobble).join(TrackAlias).where(TrackAlias.track_id == track.id)
        for scr in s.exec(scrobbles_stmt):
            scr: TrackAliasScrobble

            # convert datetime to int
            submission_time = int(scr.date.timestamp())

            scrobble = nvs.exec(
                select(Scrobbles).where(
                    Scrobbles.user_id == user.id,
                    Scrobbles.media_file_id == mf.id,
                    Scrobbles.submission_time == submission_time,
                )
            ).one_or_none()

            if scrobble:
                continue

            scrobble = Scrobbles(user_id=user.id, media_file_id=mf.id, submission_time=submission_time)
            nvs.add(scrobble)

        # flush to get actual stats
        nvs.flush()

        # get stats
        stats_stmt = select(
            func.count(), func.max(Scrobbles.submission_time), func.min(Scrobbles.submission_time)
        ).where(Scrobbles.user_id == user.id, Scrobbles.media_file_id == mf.id)
        cnt, latest_time, first_time = nvs.exec(stats_stmt).one()

        # convert int to datetime
        if latest_time:
            latest_time = datetime.fromtimestamp(latest_time).replace(tzinfo=UTC)
        if first_time:
            first_time = datetime.fromtimestamp(first_time).replace(tzinfo=UTC)

        return cnt, latest_time, first_time

    def update_track(self, track: Track, s: Session, nvs: Session):
        try:
            play_count, latest_play, first_play = self.update_scrobbles(track, s, nvs)
        except ValueError:
            print("Navidrome no match:", track.repr)
            return 0
        self.update_annotations(
            track, nvs, play_count=play_count, latest_play=latest_play, first_play=first_play
        )
        return play_count

    def sync_all(self, s: Session, nvs: Session):
        if self.reset:
            # Delete all imported scrobbles.
            # They should be older than the latest jellyfist scrobble.
            latest_scrobble = s.exec(
                select(TrackAliasScrobble).order_by(TrackAliasScrobble.date.desc())
            ).first()
            if latest_scrobble:
                res = nvs.exec(
                    delete(Scrobbles).where(Scrobbles.submission_time < latest_scrobble.date.timestamp())
                )
                nvs.commit()
                print(f"deleted {res.rowcount} scrobbles older than", latest_scrobble.date.isoformat())
        i = pc = 0
        for track in s.exec(select(Track).where(Track.path.is_not(None))):
            i += 1
            play_count = self.update_track(track, s, nvs)
            if play_count:
                pc += play_count
            print(f"{i:>6} tracks with {pc:>8} total plays synced", end="\r", flush=True)

        nvs.commit()
        print()
