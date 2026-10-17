"""The identity ladder for unbound files: recording id → fingerprint → tags.

Spec: ``docs/design/identity-ladder.md`` (*The ladder — file → track*). A recording-level rung (id,
fingerprint) only answers *which dedup group* a file belongs to; the release inside that group is
chosen by three rules (``_bind_into_group``). Only when no recording-level rung matches does the
file fall to its tags, and creating a Track from tags is the very last step.

Recording identity *groups* tracks — it never merges or re-keys them. A compilation copy of an album
track becomes its own ``Track`` and is linked as a twin of the album track's group canon.
"""

import contextlib
from collections import Counter, defaultdict
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from airdrome.console import detail
from airdrome.enums import BindMethod
from airdrome.library.fingerprint import FP_MATCH, FingerprintIndex
from airdrome.models import DedupGroupMember, Track, TrackFile


# A fingerprint match links silently only when the two durations are this close (spec, *Automatic vs
# reviewed merges*); a wider gap (radio edit, remaster, long shared intro) is left for dedup review.
FP_MAX_DURATION_GAP = 2.0

# Confidence by recording-id provenance: a tag Picard wrote outranks an AcoustID lookup.
MBID_CONFIDENCE = {BindMethod.MBID: 1.0, BindMethod.ACOUSTID: 0.9}


# The Track fields that feed ``duplicate_hash`` (beyond the tags key), which stored dedup groups are
# keyed on.
_HASHED_DEFAULTS = ("track_n", "disc_n", "year", "duration")


def stored_member_hashes(s: Session) -> set[str]:
    """Every ``duplicate_hash`` a stored (hand-made) dedup group names."""
    return set(s.scalars(select(DedupGroupMember.member_hash)))


def backfill(track: Track, defaults: dict, stored_hashes: AbstractSet[str]) -> bool:
    """Fill ``track``'s NULL fields from ``defaults``; return whether any changed.

    A track whose current ``duplicate_hash`` a stored dedup group names keeps its NULL hashed fields:
    filling one would change the hash, and the hand-made group would silently stop governing
    (``stored_canons``) or vetoing (``stored_cannot_links``) it. Elsewhere the backfill stays — a
    fresh-DB ``land`` (no stored groups yet) relies on it to reproduce the mirror's hashes.
    """
    if track.duplicate_hash in stored_hashes:
        defaults = {k: v for k, v in defaults.items() if k not in _HASHED_DEFAULTS}
    return track.fill_nulls(defaults)


def upsert_track(
    s: Session, *, defaults: dict, stored_hashes: AbstractSet[str] = frozenset(), **key: object
) -> tuple[Track, bool, bool]:
    """Get-or-create a canonical Track by ``key``, backfilling NULLs from ``defaults`` (see ``backfill``).

    Returns ``(track, created, updated)``: ``created`` if a new row was made, ``updated`` if an
    existing row had any NULL fields filled. Unify's source-track stage; the ladder splits the two steps.
    """
    track, created = Track.get_or_create(s, defaults=defaults, **key)
    updated = not created and backfill(track, defaults, stored_hashes)
    return track, created, updated


def recording_of_track(t: Track) -> str | None:
    """The ``recording_mbid`` all of ``t``'s id-carrying files agree on; None if none or they disagree.

    Files without an id neither contribute nor veto. Disagreement yields None so that one mis-bound
    file cannot chain two recording groups together (spec, recording-id provenance).
    """
    ids = {f.recording_mbid for f in t.files if f.recording_mbid}
    return ids.pop() if len(ids) == 1 else None


def is_conflicted(t: Track) -> bool:
    """Whether ``t``'s files carry recording ids that disagree (so ``recording_of_track`` is None)."""
    return recording_of_track(t) is None and any(f.recording_mbid for f in t.files)


def _blank(value: str | None) -> bool:
    """Whether a tag value is missing or whitespace only."""
    return not (value and value.strip())


def _group(track: Track) -> tuple[Track, list[Track]]:
    """``(canon, members)`` of ``track``'s dedup group; canon links are terminal, so one hop."""
    canon = track.canon or track
    return canon, [canon, *canon.twins]


def build_mbid_index(s: Session) -> dict[str, list[Track]]:
    """Tracks keyed by the recording id their files agree on, each list ordered by Track.id.

    A track whose files disagree on the id is absent (see ``recording_of_track``).
    """
    by_mbid: dict[str, list[Track]] = defaultdict(list)
    with_ids = (
        select(Track)
        .where(Track.files.any(TrackFile.recording_mbid.is_not(None)))
        .options(selectinload(Track.files))
        .order_by(Track.id)
    )
    for t in s.scalars(with_ids):
        if rec := recording_of_track(t):
            by_mbid[rec].append(t)
    return dict(by_mbid)


@dataclass
class LadderContext:
    """Per-run lookup state over the *bound* files; files bound during the run are added as they go."""

    by_mbid: dict[str, list[Track]]
    fp_index: FingerprintIndex  # keyed by TrackFile.id
    file_track: dict[int, Track]
    fp_duration: dict[int, float]  # TrackFile.id → fp_duration, for the duration gate
    # duplicate_hash values a stored (hand-made) dedup group names; see ``backfill``.
    stored_hashes: set[str] = field(default_factory=set)
    stats: Counter[str] = field(default_factory=Counter)  # created / updated / per method

    @classmethod
    def build(cls, s: Session) -> LadderContext:
        """Index every bound file once: by its track's agreed recording id and by fingerprint."""
        by_mbid = build_mbid_index(s)

        fp_index = FingerprintIndex()
        file_track: dict[int, Track] = {}
        fp_duration: dict[int, float] = {}
        fingerprinted = (
            select(TrackFile)
            .where(TrackFile.track_id.is_not(None), TrackFile.fingerprint.is_not(None))
            .options(selectinload(TrackFile.track))
            .order_by(TrackFile.id)
        )
        for tf in s.scalars(fingerprinted):
            cls._index_file(tf, fp_index, file_track, fp_duration)
        return cls(by_mbid, fp_index, file_track, fp_duration, stored_member_hashes(s))

    @staticmethod
    def _index_file(
        tf: TrackFile, fp_index: FingerprintIndex, file_track: dict[int, Track], fp_duration: dict[int, float]
    ) -> None:
        """Add one bound file's fingerprint to the index maps."""
        if tf.track is None or not tf.fingerprint:
            return
        fp_index.add(tf.id, tf.fingerprint)
        file_track[tf.id] = tf.track
        if tf.fp_duration is not None:
            fp_duration[tf.id] = tf.fp_duration

    def add(self, tf: TrackFile, track: Track, previous_recording: str | None, *, rekey: bool = True) -> None:
        """Record ``tf`` just bound to ``track``, re-keying the track if its agreed id changed.

        ``rekey=False`` still drops a stale old key but never adds the track under its new one.
        """
        self._index_file(tf, self.fp_index, self.file_track, self.fp_duration)
        current = recording_of_track(track)
        if current == previous_recording:
            return
        if previous_recording and track in self.by_mbid.get(previous_recording, []):
            self.by_mbid[previous_recording].remove(track)
        if current and rekey:
            self.by_mbid.setdefault(current, []).append(track)


def _file_defaults(tf: TrackFile) -> dict:
    """Track defaults derived from a file: rounded duration, tag year, and the file's oldest timestamp."""
    year = None
    if tf.date:
        with contextlib.suppress(ValueError, IndexError):
            year = int(tf.date[:4])
    defaults: dict = {"duration": round(tf.duration) if tf.duration else None, "year": year}
    try:
        st = tf.source_path.stat()
        # st_ctime is creation on Windows / inode-change on Linux; mtime is content edit.
        # min() yields the oldest known timestamp for the file across platforms.
        defaults["date_added"] = datetime.fromtimestamp(min(st.st_ctime, st.st_mtime), tz=UTC)
    except OSError:
        pass
    return defaults


def _track_from_tags(s: Session, tf: TrackFile, ctx: LadderContext) -> tuple[Track, bool]:
    """Get-or-create the Track keyed on the file's own tags, counting a creation in ``ctx``.

    An existing Track is returned untouched: the caller backfills it (``_backfill``) only once it has
    decided to bind the file there, so a refused hit never edits another recording's track.
    """
    track, created = Track.get_or_create(s, defaults=_file_defaults(tf), **_tags_key(tf))
    ctx.stats["created"] += created
    return track, created


def _backfill(track: Track, tf: TrackFile, ctx: LadderContext) -> None:
    """Fill ``track``'s NULL fields from the file it is about to receive (``backfill``), counting it."""
    ctx.stats["updated"] += backfill(track, _file_defaults(tf), ctx.stored_hashes)


def _bind(
    tf: TrackFile,
    track: Track,
    method: BindMethod,
    confidence: float,
    ctx: LadderContext,
    *,
    rekey: bool = True,
) -> Track:
    """Bind ``tf`` to ``track`` with its method and confidence, and add it to ``ctx``.

    ``rekey=False`` never adds ``track`` under a new id in ``by_mbid`` — for a tags landing on a track
    in a foreign group, which must not pull that group into later matches.
    """
    previous = recording_of_track(track)
    # The relationship (not track_id) keeps track.files current in memory for the rest of the run.
    tf.track = track
    tf.bind_method = method
    tf.bind_confidence = confidence
    ctx.add(tf, track, previous, rekey=rekey)
    ctx.stats[method] += 1
    return track


def _compatible(t: Track, rec: str | None) -> bool:
    """Whether ``t`` may receive a file of recording ``rec``: no id on either side, or the same id.

    A conflicted track (files disagreeing on the id) is refused whatever ``rec`` is: it has no single
    recording to be compatible with, and dedup leaves it out of every recording group too.
    """
    if is_conflicted(t):
        return False
    return rec is None or recording_of_track(t) in (None, rec)


def _link_twin(
    track: Track, canon: Track | None, rec: str | None, method: BindMethod, confidence: float
) -> bool:
    """Make ``track`` a twin of ``canon`` unless that would cross recordings, overwrite or chain.

    Returns whether ``track`` now belongs to the matched group (or stands alone), i.e. may be keyed
    under the file's recording id. dedup's rebuild owns reconciling the refused cases, so they are
    only logged here.
    """
    standalone = track.canon is None and not track.twins
    if canon is None:
        # The group's canon is of another recording: no same-recording canon to link to.
        detail(f"[dim yellow]ladder: track {track.id} not linked, canon is another recording[/dim yellow]")
        return standalone
    # The id check comes first: even a track already in this group must not take a foreign id's link.
    ref = rec or recording_of_track(canon)
    own = recording_of_track(track)
    if ref and own and own != ref:
        detail(f"[dim yellow]ladder: track {track.id} is {own}, not linked to {canon.id}[/dim yellow]")
        return False
    if track is canon or track.canon is canon:
        return True
    if track.canon is not None:
        detail(f"[dim yellow]ladder: track {track.id} twins {track.canon_id}, not {canon.id}[/dim yellow]")
        return False
    if track.twins:
        # It is another group's canon; linking it would create a canon chain.
        detail(f"[dim yellow]ladder: track {track.id} is a canon, not linked to {canon.id}[/dim yellow]")
        return False
    # The relationship keeps canon.twins current, so a later file of this run finds the new twin.
    track.canon = canon
    track.canon_method = method
    track.canon_confidence = confidence
    return True


@dataclass
class _Match:
    """A matched group, restricted to what the file's recording id is compatible with.

    ``rec`` is the effective recording id: the file's own, else (fingerprint rung) the matched track's.
    ``anchor`` is the track the rung matched (it agrees with ``rec``); ``canon`` is the group canon, or
    None when that canon's files agree on another recording; ``members`` excludes such tracks.
    """

    rec: str | None
    anchor: Track
    canon: Track | None
    members: list[Track]


def _bind_into_group(
    s: Session, tf: TrackFile, match: _Match, method: BindMethod, confidence: float, ctx: LadderContext
) -> Track:
    """Choose the release inside a matched group (spec rules 1 to 3) and bind ``tf`` there."""
    rec = match.rec
    # Rule 1: a member on the file's own album. A file with no album has no "own album" to match.
    # Members of another recording were already dropped (a flag-set group can hold them).
    if tf.album_norm:
        same_album = [m for m in match.members if m.album_norm == tf.album_norm]
        if same_album:
            target = min(same_album, key=lambda m: (m is not match.canon, m.id))
            return _bind(tf, target, method, confidence, ctx)
    # Rule 2: usable tags → this release gets its own Track, a twin of the group.
    if not (_blank(tf.title) or _blank(tf.artist) or _blank(tf.album)):
        track, created = _track_from_tags(s, tf, ctx)
        if _compatible(track, rec):
            if not created:
                _backfill(track, tf, ctx)
            in_group = _link_twin(track, match.canon, rec, method, confidence)
            return _bind(tf, track, method, confidence, ctx, rekey=in_group)
        # The tags name a track of another recording: binding there would break its ids (ruling).
        detail(f"[dim yellow]ladder: file {tf.id} tags hit track {track.id}, another recording[/dim yellow]")
    # Rule 3: junk tags ("Track 01") → the group's canon, or the matched track itself when that canon
    # is another recording.
    return _bind(tf, match.canon or match.anchor, method, confidence, ctx)


def _restrict(tracks: list[Track], rec: str | None) -> _Match:
    """Build a ``_Match`` from the matched ``tracks`` (all compatible with ``rec``), lowest id first.

    The canon is the first matched track's group canon that is compatible with ``rec``; several
    groups can share one id before dedup has run (deterministic pick; dedup reconciles the rest).
    """
    ordered = sorted(tracks, key=lambda t: t.id)
    anchor, canon = ordered[0], None
    members: dict[int, Track] = {}
    for t in ordered:
        group_canon, group = _group(t)
        if canon is None and _compatible(group_canon, rec):
            anchor, canon = t, group_canon
        members.update((m.id, m) for m in group if _compatible(m, rec))
    return _Match(rec, anchor, canon, list(members.values()))


def _mbid_group(tf: TrackFile, ctx: LadderContext) -> _Match | None:
    """The group sharing ``tf``'s recording id, or None."""
    tracks = ctx.by_mbid.get(tf.recording_mbid or "")
    return _restrict(tracks, tf.recording_mbid) if tracks else None


def _fingerprint_match(tf: TrackFile, ctx: LadderContext) -> tuple[Track, float] | None:
    """The best bound file matching ``tf``'s fingerprint within the duration gate: (its track, score).

    A match whose track agrees on a recording id other than the file's own is skipped: an id
    outranks a fingerprint, so the file must not be pulled into that recording's group.
    """
    if not tf.fingerprint or tf.fp_duration is None:
        return None
    # Best first; a closer-scoring match outside the gate does not hide a usable one behind it.
    for key, score in ctx.fp_index.matches(tf.fingerprint, threshold=FP_MATCH):
        other = ctx.fp_duration.get(key)
        if other is None or abs(other - tf.fp_duration) > FP_MAX_DURATION_GAP:
            continue
        if _compatible(ctx.file_track[key], tf.recording_mbid):
            return ctx.file_track[key], score
    return None


def _tags_key(tf: TrackFile) -> dict[str, str | None]:
    """The tags rung's Track key: the file's own title, artist, album and album artist."""
    return {"title": tf.title, "artist": tf.artist, "album": tf.album, "album_artist": tf.album_artist}


def _locate(tf: TrackFile, ctx: LadderContext) -> tuple[BindMethod, _Match, float] | None:
    """The recording-level rungs, in order: ``(method, matched group, confidence)``, or None for tags.

    Shared by ``resolve_file`` (which binds into the group) and ``match_file`` (which only reads it), so
    the two can never disagree on which group a file belongs to.
    """
    if group := _mbid_group(tf, ctx):
        method = BindMethod.ACOUSTID if tf.mbid_source == BindMethod.ACOUSTID else BindMethod.MBID
        return method, group, MBID_CONFIDENCE[method]

    if fp := _fingerprint_match(tf, ctx):
        matched, score = fp
        # A file without an id takes the matched track's agreed id, so the rest of a flag-set group
        # (another recording) is excluded just as on the id rung.
        group = _restrict([matched], tf.recording_mbid or recording_of_track(matched))
        return BindMethod.FINGERPRINT, group, score
    return None


def resolve_file(s: Session, tf: TrackFile, ctx: LadderContext) -> Track | None:
    """Bind an unbound file through recording id → fingerprint → tags; return its Track.

    Returns None (file left unbound) when no rung matches and the title is blank: a Track is never
    created from nothing.
    """
    if located := _locate(tf, ctx):
        method, group, confidence = located
        return _bind_into_group(s, tf, group, method, confidence, ctx)

    if _blank(tf.title):
        return None
    track, created = _track_from_tags(s, tf, ctx)
    if not created:
        _backfill(track, tf, ctx)
    return _bind(tf, track, BindMethod.TAGS_EXACT, 1.0, ctx)


def match_file(s: Session, tf: TrackFile, ctx: LadderContext) -> Track | None:
    """The canon of the group ``resolve_file`` would put ``tf`` in, or None; binds and creates nothing.

    The same rungs in the same order (``_locate``, then exact tags), read-only: ``import`` classifies
    loose files with it before deciding which to import. ``tf`` may be transient (never added to ``s``).
    Where the matched group's canon is another recording, the matched track stands in, as in rule 3.
    """
    if located := _locate(tf, ctx):
        _method, group, _confidence = located
        return group.canon or group.anchor
    if _blank(tf.title):
        return None
    # The tags rung's key, looked up instead of upserted: no exact Track means the tags rung would
    # create one, i.e. the library does not have this recording.
    track = s.scalars(select(Track).filter_by(**_tags_key(tf))).one_or_none()
    return _group(track)[0] if track else None
