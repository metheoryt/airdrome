from collections import defaultdict
from datetime import datetime, UTC

from sqlmodel import Session, select, func

from jellyfist.models import Track, TrackAlias, engine, TrackAliasScrobble
from ..models import Annotation, MediaFile, User, Scrobbles, AlbumArtist, engine as nv_engine


def sync_tracks_plays_to_navi(username: str):
    with Session(engine) as s, Session(nv_engine) as nvs:
        TrackSyncer(username).sync_all(s, nvs)


class TrackSyncer:
    def __init__(self, username: str):
        self.username = username
        self._user: User | None = None
        self._item_play_count = defaultdict(int)
        self._item_latest_play = dict()

    def get_user(self, nvs: Session):
        if self._user is None:
            self._user = nvs.exec(select(User).where(User.user_name == self.username)).one()
        return self._user

    def get_mediafile(self, track: Track, nvs: Session) -> MediaFile | None:
        return nvs.exec(select(MediaFile).where(MediaFile.path == track.path)).one_or_none()

    def goc_annotation(
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
        nvs.flush()
        return ann, True

    def update_track_annotation(
        self, mf: MediaFile, track: Track, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        track_ann, created = self.goc_annotation(mf.id, Annotation.ItemType.MEDIA_FILE, nvs)

        track_ann.play_count = play_count
        track_ann.play_date = latest_play
        # Only update in one direction (has rating / starred). Don't unset ratings during sync.
        if track.rating and not track.rating_computed:
            track_ann.rating = track.rating
            track_ann.rated_at = track.date_added
        if track.loved:
            track_ann.starred = True
            track_ann.rated_at = track.date_added

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

    def update_album_annotation(
        self, mf: MediaFile, track: Track, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        album_ann, created = self.goc_annotation(mf.album_id, Annotation.ItemType.ALBUM, nvs)

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

    def update_artist_annotation(
        self, mf: MediaFile, nvs: Session, play_count: int, latest_play: datetime | None
    ):
        roles = ["albumartist", "artist"]
        stmt = select(AlbumArtist.artist_id).where(
            AlbumArtist.album_id == mf.album_id, AlbumArtist.role.in_(roles)
        )
        for artist_id in nvs.exec(stmt):
            artist_ann, created = self.goc_annotation(artist_id, Annotation.ItemType.ARTIST, nvs)
            # play count and date
            self._add_play_count_date(artist_ann, play_count, latest_play)

    def update_annotations(self, track: Track, nvs: Session, play_count: int, latest_play: datetime | None):
        #
        # update track/album/artist annotations
        #
        mf = self.get_mediafile(track, nvs)
        if not mf:
            # no match @ Navi
            raise ValueError(f"No mediafile found for track {track.id}")

        self.update_track_annotation(mf, track, nvs, play_count, latest_play)
        self.update_album_annotation(mf, track, nvs, play_count, latest_play)
        self.update_artist_annotation(mf, nvs, play_count, latest_play)

        nvs.flush()
        return None

    def update_scrobbles(self, track: Track, s: Session, nvs: Session) -> tuple[int, datetime | None]:
        user = self.get_user(nvs)
        mf = self.get_mediafile(track, nvs)
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

        nvs.flush()

        # get stats
        stats_stmt = select(func.count(), func.max(Scrobbles.submission_time)).where(
            Scrobbles.user_id == user.id, Scrobbles.media_file_id == mf.id
        )
        cnt, latest_time = nvs.exec(stats_stmt).one()

        if latest_time:
            # convert int to datetime
            latest_time = datetime.fromtimestamp(latest_time, tz=UTC)

        return cnt, latest_time

    def update_track(self, track: Track, s: Session, nvs: Session):
        try:
            play_count, latest_play = self.update_scrobbles(track, s, nvs)
        except ValueError:
            print("Navidrome no match:", track.repr)
            return 0
        self.update_annotations(track, nvs, play_count=play_count, latest_play=latest_play)
        return play_count

    def sync_all(self, s: Session, nvs: Session):
        i = pc = 0
        for track in s.exec(select(Track).where(Track.path.is_not(None))):
            i += 1
            play_count = self.update_track(track, s, nvs)
            if play_count:
                pc += play_count
            print(f"{i:>6} tracks with {pc:>8} total plays synced", end="\r", flush=True)

        nvs.commit()
        print()
