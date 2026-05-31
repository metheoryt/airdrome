from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from sqlalchemy import Column, func, select
from sqlalchemy.orm import Session

from airdrome.models import Track

from .grouping import merge_overlapping_groups
from .persistence import load_confirmed_groups, save_confirmed_groups


class FilterMode(Enum):
    RESOLVED_ALL = "resolved all"
    RESOLVED_UNCONFIRMED = "resolved unconfirmed"
    RESOLVED_CONFIRMED = "resolved confirmed"
    AUTO_RESOLVED = "auto-resolved"

    def next(self) -> FilterMode:
        members = list(FilterMode)
        return members[(members.index(self) + 1) % len(members)]


@dataclass
class Page:
    tracks: list[Track]
    canons: list[int | None] = field(default_factory=list)
    chosen_canons: list[int | None] = field(default_factory=list)
    confirmed: bool = False
    auto_resolved: bool = False  # canon_ids already set in DB by auto-dedup

    def __post_init__(self):
        self.canons = [t.canon_id for t in self.tracks]
        self.chosen_canons = list(self.canons)
        self.auto_resolved = any(c is not None for c in self.canons)

    def confirm(self) -> None:
        self.confirmed = True
        self.auto_resolved = False

    def reset(self) -> None:
        self.confirmed = False
        self.auto_resolved = False
        self.chosen_canons = [None] * len(self.tracks)

    def set_canon(self, canon_idx: int, member_idxs: list[int]) -> None:
        members = [t.id for t in self.tracks]
        for idx in (canon_idx, *member_idxs):
            if idx not in range(len(members)):
                raise ValueError(f"Index out of range: {idx + 1}")
        for member_idx in member_idxs:
            if canon_idx == member_idx:
                raise ValueError(f"Can't mark track as canon of itself: {canon_idx + 1}")
            if members[member_idx] in self.chosen_canons:
                raise ValueError(f"Already chosen as a canon: {member_idx + 1}")
            if self.chosen_canons[canon_idx]:
                raise ValueError(f"Already has a canon: {canon_idx + 1}")
        for member_idx in member_idxs:
            self.chosen_canons[member_idx] = members[canon_idx]
        self.confirmed = False


@dataclass
class DeduplicatorState:
    pages: dict[str, Page] = field(default_factory=dict)
    current_idx: int = 0
    filter_mode: FilterMode = FilterMode.RESOLVED_ALL
    partial_match: str = ""
    pages_iter: list[tuple[str, Page]] = field(default_factory=list, init=False)
    _mode_idx: dict = field(default_factory=lambda: dict.fromkeys(FilterMode, 0), init=False)

    def __post_init__(self):
        self.pages_iter = list(self.pages.items())

    def filtered_pages(self) -> list[tuple[str, Page]]:
        pages = self.pages_iter
        if self.partial_match:
            pages = [
                (k, p)
                for k, p in pages
                if any(
                    self.partial_match in v
                    for t in p.tracks
                    for v in (t.title_norm, t.artist_norm, t.album_artist_norm, t.album_norm)
                    if v is not None
                )
            ]
        match self.filter_mode:
            case FilterMode.RESOLVED_ALL:
                return [(k, p) for k, p in pages if not p.auto_resolved]
            case FilterMode.RESOLVED_UNCONFIRMED:
                return [(k, p) for k, p in pages if not p.auto_resolved and not p.confirmed]
            case FilterMode.RESOLVED_CONFIRMED:
                return [(k, p) for k, p in pages if not p.auto_resolved and p.confirmed]
            case FilterMode.AUTO_RESOLVED:
                return [(k, p) for k, p in pages if p.auto_resolved and not p.confirmed]

    def switch_mode(self) -> None:
        self._mode_idx[self.filter_mode] = self.current_idx
        self.filter_mode = self.filter_mode.next()
        new_total = len(self.filtered_pages())
        saved = self._mode_idx[self.filter_mode]
        self.current_idx = min(saved, new_total - 1) if new_total > 0 else 0

    def current_page(self) -> tuple[str, Page] | None:
        filtered = self.filtered_pages()
        if not filtered:
            return None
        return filtered[self.current_idx]

    def clamp(self) -> None:
        total = len(self.filtered_pages())
        if total > 0 and self.current_idx >= total:
            self.current_idx = total - 1

    def go_next(self) -> bool:
        total = len(self.filtered_pages())
        if self.current_idx < total - 1:
            self.current_idx += 1
            return True
        return False

    def go_prev(self) -> bool:
        if self.current_idx > 0:
            self.current_idx -= 1
            return True
        return False


class Deduplicator:
    COLUMN_SETS: ClassVar[list[list[Column]]] = [
        [Track.artist_norm, Track.title_norm],
        [Track.album_artist_norm, Track.title_norm],
        [Track.album_norm, Track.title_norm],
    ]

    def __init__(self, s: Session, partial_match: str = ""):
        self.s = s
        self.state = DeduplicatorState(partial_match=partial_match)

    def get_track_groups(self, cols: list[Column]) -> list[tuple[str, list[Track]]]:
        # Skip empty normalized keys: an empty artist/album/title would
        # otherwise collapse every unrelated track sharing that blank into
        # one giant bogus group.
        non_empty = [col != "" for col in cols]
        combinations = self.s.execute(
            select(*cols, func.count(Track.id).label("count"))
            # do not exclude them, since they can appear in a broader group
            # .where(Track.canon_id.is_(None))  # exclude tracks already marked as twins
            .where(*non_empty)
            .group_by(*cols)
            .having(func.count(Track.id) > 1)
            .order_by(*cols)
        )
        groups = []
        for *col_vals, _count in combinations:
            col_to_val = list(zip(cols, col_vals, strict=False))
            key = ",".join(f"{c.name}={v}" for c, v in col_to_val)
            track_group = list(
                self.s.scalars(
                    select(Track)
                    .where(
                        # Track.canon_id.is_(None),
                        *[col == val for col, val in col_to_val]
                    )
                    .order_by(
                        Track.date_added.asc().nulls_last(),
                        Track.year.asc().nulls_last(),
                        Track.loved.desc().nulls_last(),
                        Track.id,
                    )
                )
            )
            groups.append((key, track_group))
        return groups

    def dedup_pages(self, groups: list[tuple[str, list[Track]]]) -> list[tuple[str, list[Track]]]:
        return merge_overlapping_groups(self.s, groups)

    def fill_state(self) -> None:
        groups = []
        for cols in self.COLUMN_SETS:
            for key, tracks in self.get_track_groups(cols):
                groups.append((key, tracks))
        groups = self.dedup_pages(groups)
        pages = {key: Page(tracks=tracks) for key, tracks in groups}
        self.state = DeduplicatorState(pages=pages, current_idx=0, partial_match=self.state.partial_match)
        load_confirmed_groups(self.s, self.state.pages)

    def apply_changes(self) -> int:
        """Stage confirmed canon picks onto the session. Caller is responsible for commit."""
        changed = 0
        for _key, page in self.state.pages_iter:
            if not page.confirmed:
                continue
            for i, track in enumerate(page.tracks):
                new_canon = page.chosen_canons[i]
                if new_canon != page.canons[i]:
                    track.canon_id = new_canon
                    self.s.add(track)
                    changed += 1
        save_confirmed_groups(self.s, self.state.pages)
        return changed

    def run(self) -> None:
        from .tui import DeduplicatorUI

        print("loading...", end="\r")
        self.fill_state()
        filtered = self.state.filtered_pages()
        self.state.current_idx = next((i for i, (_, p) in enumerate(filtered) if not p.confirmed), 0)
        print(" done!", end="\r")
        DeduplicatorUI(self).serve()
