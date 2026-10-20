"""The FastMCP server: thin tools over the wanted lists, the track card and the manual point edits.

Each tool opens its own `tool_session`, calls one domain function and returns plain JSON data, so a tool
call is one transaction: it commits when the call succeeds and rolls back when it raises. An `EditError`
or any other `ValueError` (unknown id, bad cursor, an old-format mirror) reaches the client as a tool error
carrying its message.
"""

import threading
from collections.abc import Callable
from dataclasses import asdict

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic_core import to_jsonable_python
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from airdrome.conf import settings
from airdrome.enums import Verdict
from airdrome.library import card, edits, search
from airdrome.normalize.dedup.mirror import restore_if_empty
from airdrome.normalize.dedup.store import resolve_library
from airdrome.terminal.status import pipeline_counts
from airdrome.wanted import lists, verdicts

from .session import tool_session


INSTRUCTIONS = """Airdrome is the owner's music library hub (it migrates a library and listening history
into Navidrome). Use these tools to help the owner decide which missing tracks to acquire and to fix
track identity by hand; `search_music` answers "do I have this anywhere?". Every list except
`search_music` is paged: pass the previous `next_cursor` back as `cursor`; it is None at the end.
Ids are Airdrome track ids and alias ids. Every edit commits at once and survives later `land` and
`dedup` runs."""

# Writes run one at a time. Sync tools run in a threadpool, so two overlapping edits could commit in
# one order and write their dedup-mirror snapshots in the other, leaving the file behind the DB.
_WRITE_LOCK = threading.Lock()


def seed_decisions(engine: Engine) -> int:
    """Seed empty decision tables from the durable mirror, committed; returns the rows restored.

    Run once before serving, so reads (the track card's decisions) see the manual decisions of a freshly
    rebuilt DB at once. Writes do not depend on it: `write` repeats the seed before every edit. An
    old-format mirror raises, so the server does not start on a file it would later overwrite.
    """
    with _WRITE_LOCK, tool_session(engine) as s:
        return restore_if_empty(s, settings.duplicates_file)


def build_server(engine: Engine) -> FastMCP:
    """The `airdrome` MCP server, every tool running in its own session on `engine`."""
    # Masked: an unexpected exception (SQL text, bound params) reaches the client as a generic error;
    # the expected ones are raised as `ToolError`, whose message always comes through.
    mcp = FastMCP("airdrome", instructions=INSTRUCTIONS, mask_error_details=True)

    def call(s: Session, fn: Callable[[Session], dict]) -> dict:
        """Run `fn` on `s` and serialize its result; a `ValueError` becomes a tool error (and rolls back)."""
        try:
            # Serialize inside the session: the result may still read lazy attributes.
            return to_jsonable_python(fn(s))
        except ValueError as exc:  # EditError is a ValueError (so is a corrupt mirror's JSONDecodeError)
            raise ToolError(str(exc)) from exc

    def run(fn: Callable[[Session], dict]) -> dict:
        """Run `fn` in one committed session."""
        with tool_session(engine) as s:
            return call(s, fn)

    def write(fn: Callable[[Session], dict]) -> dict:
        """Run `fn` in one committed session under the write lock, held through the commit and mirror write.

        The decision tables are re-seeded from the mirror first, in the same session: every edit snapshots
        the *whole* tables into the mirror, so a DB rebuilt while the server runs would otherwise have its
        first edit overwrite every manual decision in the file. A no-op (two indexed probes) when
        decisions exist.
        """

        def seeded(s: Session) -> dict:
            """Seed empty decision tables from the mirror, then run the edit."""
            restore_if_empty(s, settings.duplicates_file)
            return fn(s)

        with _WRITE_LOCK, tool_session(engine) as s:
            return call(s, seeded)

    @mcp.tool
    def wanted_library(
        cursor: str | None = None, limit: int = 50, verdict: Verdict | None = None, query: str | None = None
    ) -> dict:
        """List part 1 of the wanted list: tracks in the owner's library that have no audio file yet.

        The library is what the owner deliberately collected (the iTunes/Apple Music library XML, later
        Spotify liked songs and own playlists); scrobble-only tracks are not in it. Rows are canon groups
        (duplicates collapsed, plays summed over the group), oldest `date_added` first. Each row has
        plays, first/last played, months played, playlist count, loved, rating, sources, the owner's
        `verdict` and `similar_with_file` (a similar track that already has a file — maybe the same song).
        `verdict` filters: "wanted" or "skip"; omitted, rows marked "skip" are hidden. `query` matches
        title/artist/album. `limit` defaults to 50, max 200.
        """
        return run(lambda s: asdict(lists.wanted_library(s, cursor, limit, verdict, query)))

    @mcp.tool
    def wanted_scrobbled(
        cursor: str | None = None,
        limit: int = 50,
        verdict: Verdict | None = None,
        query: str | None = None,
        include_unmatched: bool = True,
    ) -> dict:
        """List part 2 of the wanted list: what the owner listened to (scrobbles) but has no file for.

        Rows of kind "track" are file-less canon groups with plays; rows of kind "unmatched" are scrobbled
        aliases bound to no track, collapsed on artist + title (`alias_ids` lists them; `rejected: true`
        when the owner marked one as matching nothing, false on track rows). Most played first. Same
        signals and filters as `wanted_library`; "skip" rows are hidden unless `verdict` asks for them.
        `include_unmatched=false` keeps only track rows. `limit` defaults to 50, max 200.
        """
        return run(
            lambda s: asdict(lists.wanted_scrobbled(s, cursor, limit, verdict, query, include_unmatched))
        )

    @mcp.tool
    def track_card(track_id: int) -> dict:
        """Everything about the canon group of `track_id` (any member id works).

        Returns the canon, every member with its sources and files (path, bitrate, which one is main),
        bound aliases (top 50 by scrobbles, plus `aliases_total`), summed plays, playlists, the group's
        verdict, and `decisions`: `pairs` (every manual same/different decision naming a member, newest
        first, each end with its key and live `track_id`, and a `status` — accepted, superseded by a newer
        decision, or dead because an end names no track in the library) and `canons` (manual canon
        choices among the members). Use it before any edit to check identity.
        """
        return run(lambda s: card.track_card(s, track_id))

    @mcp.tool
    def search_music(query: str, limit: int = 20) -> dict:
        """Find music by words, in the library and in the music folders on disk.

        Words match whole, in any order, ignoring case, accents and punctuation; the last word may be
        cut short. `tracks` lists canon groups with a matching member (title, artist, album, album artist),
        most played first, each with its files (`has_file`), plays, `in_library` and verdict; pass its
        `track_id` to `track_card`. `disk` walks the configured music folders: a `folder` row is a folder
        whose path matches, with its audio count, how many of those are imported, and its subfolders with
        no audio at all (often only cover art left: a file that was there once); a `file` row is an audio
        file whose path matches, `track_id` null when it was never imported. Each section returns at most
        `limit` rows (default 20, max 200) and its `total`; narrow the query rather than paging.
        """
        return run(lambda s: search.search(s, query, limit))

    @mcp.tool
    def unmatched_aliases(cursor: str | None = None, limit: int = 50, query: str | None = None) -> dict:
        """Scrobbled aliases bound to no track, collapsed on artist + title, each with bind candidates.

        Every row carries up to 5 candidate canon tracks with a similarity score (looser than `land`'s
        own matching, so check each by hand). Bind the right one with `bind_aliases`, or mark the row
        as matching nothing with `reject_aliases` (rejected rows say `rejected: true`).
        `limit` defaults to 50, max 200.
        """
        return run(lambda s: card.unmatched_aliases(s, cursor, limit, query))

    @mcp.tool
    def library_status() -> dict:
        """Per-stage counts of the pipeline: imported, canonical, files, dedup, synced and wanted verdicts."""
        return run(pipeline_counts)

    @mcp.tool
    def set_verdict(
        verdict: Verdict,
        track_id: int | None = None,
        alias_ids: list[int] | None = None,
        note: str | None = None,
    ) -> dict:
        """Record the owner's verdict on a wanted row: "wanted" (acquire it) or "skip" (never ask again).

        Give exactly one target: `track_id` (the whole canon group gets the verdict, replacing older
        verdicts on its members) or `alias_ids` (an unmatched row; one verdict per alias). "skip" hides
        the row from the wanted lists and beats "wanted" wherever both apply. `note` is free text kept
        with the verdict. Verdicts survive `land` and `dedup`.
        """
        if (track_id is None) == (not alias_ids):
            raise ToolError("give exactly one target: track_id or alias_ids")

        def go(s: Session) -> dict:
            """Apply the verdict to the one target given."""
            if track_id is not None:
                row = verdicts.set_track_verdict(s, track_id, verdict, note)
                return {"verdict": verdict, "track_id": row.track_id, "alias_ids": []}
            rows = verdicts.set_alias_verdict(s, alias_ids or [], verdict, note)
            return {"verdict": verdict, "track_id": None, "alias_ids": [r.alias_id for r in rows]}

        return write(go)

    @mcp.tool
    def clear_verdict(track_id: int | None = None, alias_ids: list[int] | None = None) -> dict:
        """Remove verdicts so the row shows again as undecided.

        `track_id` clears the whole group's verdicts (members and their bound aliases); `alias_ids`
        clears those aliases'. Returns how many verdict rows were deleted.
        """
        if track_id is None and not alias_ids:
            raise ToolError("give track_id and/or alias_ids")
        return write(lambda s: {"deleted": verdicts.clear_verdict(s, track_id=track_id, alias_ids=alias_ids)})

    @mcp.tool
    def mark_same_track(track_id_a: int, track_id_b: int) -> dict:
        """Mark two tracks as the same track: their canon groups merge into one.

        Stored as a manual decision (the newest decision wins), so it survives `land` and `dedup` and
        outranks automatic grouping, recording ids included: it can fold one recording into the one the
        owner listens to. An older "different" decision it contradicts is superseded. The result is what
        a following `dedup` run gives; returns the card of the group `track_id_a` ends in, whose
        `decisions` show what was accepted.
        """
        return write(lambda s: card.track_card(s, edits.mark_same(s, track_id_a, track_id_b).id))

    @mcp.tool
    def mark_different_tracks(track_id_a: int, track_id_b: int) -> dict:
        """Mark two tracks as different tracks: they are kept in separate groups.

        Stored as a manual decision (the newest decision wins) that survives `land` and `dedup` and blocks
        automatic grouping of the two. Older "same" decisions it contradicts are superseded, so when the
        two were joined through a chain of manual decisions, the resolution decides which track leaves:
        read both returned cards (`a` and `b`) for the outcome.
        """

        def go(s: Session) -> dict:
            """Split the pair and read both cards back."""
            edits.mark_different(s, track_id_a, track_id_b)
            res = resolve_library(s)  # one resolution for both cards
            return {"a": card.track_card(s, track_id_a, res), "b": card.track_card(s, track_id_b, res)}

        return write(go)

    @mcp.tool
    def bind_aliases(alias_ids: list[int], track_id: int) -> dict:
        """Bind scrobbled aliases to a track by hand, moving their plays into its history.

        A manual bind outranks automatic matching and survives `land`. Returns the track's card.
        """
        return write(lambda s: card.track_card(s, edits.bind_aliases(s, alias_ids, track_id).id))

    @mcp.tool
    def reject_aliases(alias_ids: list[int]) -> dict:
        """Mark aliases as matching no track: unbound for good, their plays removed from any track.

        `land` will not match them again. Undo by binding them with `bind_aliases`.
        """

        def go(s: Session) -> dict:
            """Reject the aliases and echo their ids."""
            edits.reject_aliases(s, alias_ids)
            return {"rejected": alias_ids}

        return write(go)

    return mcp
