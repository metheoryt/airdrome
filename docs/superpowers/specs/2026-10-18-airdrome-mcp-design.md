# Airdrome MCP server v1 (with `wanted`) — design

Status: agreed in chat, 2026-10-18. Brainstormed with the owner in chat; decisions marked **(owner)** are
his, **(call)** are mine and open to override.

## Intent

Airdrome is moving onto AI rails: the owner wants an AI (Claude Code now, claude.ai / phone later)
to look at his library and help him decide things, instead of him driving every step through the
CLI. The first job is `wanted` — deciding which file-less tracks are worth acquiring — but the
server is general: it shows the library and offers point levers, possibly richer than the CLI's.

The postgres MCP already in `.mcp.json` gives raw SQL on the live DB. This server adds what raw SQL
lacks: domain meaning (canon groups, bind provenance, play counts per group) and *safe* point edits
that keep Airdrome's invariants and survive the next `land` / `dedup`.

## Goals

- **`wanted`, two parts (owner, his words):**
  1. every track he added to a streaming library (Spotify, other connected services) that has no
     file — *all* of them, unfiltered;
  2. file-less tracks that have scrobbles — he picks from these himself, sorted by play count.
- **Read tools** that let the AI understand a track and the library (below).
- **Three point edits (owner agreed):** a verdict on a file-less track (*wanted* / *skip*);
  *same track* / *different tracks*; bind a scrobble alias to a track (or reject a bad bind).
- **A Spotify library importer** — part 1 has no Spotify data today (Spotify is scrobble-only in
  Airdrome). Ships *after* the server (see *Order*).

## Non-goals (v1)

- **No library-wide actions (owner).** `import`, `land`, `organize`, `dedup`, `sync`, `navi push`,
  `maint *` stay CLI-only. This also removes the long-call problem (`land` step 3/4 ≈ 43 min).
- No manual file → track binding (deferred: all 6,986 files are bound).
- No Soulseek acquisition — `wanted` verdicts feed it later.
- No public exposure yet (owner: "пока что попользуемся им локально").
- No file mirrors for the new manual decisions (owner): they live in the DB only. DB recreation is a
  development-phase habit; `duplicates.json` is a stopgap until recreation stops.

## Anti-goals

None stated. The owner judges by trying ("я сначала пробую, и только потом составляю мнение"), so
v1 should be small enough to try quickly and change.

## Constraints

- **FastMCP, HTTP transport (owner).** He will reach it over the network eventually; v1 listens on
  `127.0.0.1` on g15 **(call)**.
- **Auth = a secret token link, not OAuth (owner)** — single user. URL shape
  `http://127.0.0.1:<port>/mcp/<token>` (the exact routing is the builder's). The token is
  `AIRDROME_MCP_TOKEN` in `.env`, a `SecretStr`, never printed or logged. Enabled from day one even
  locally **(call)**, so going public changes nothing in the server. A token in a URL can land in
  proxy logs; the remedy is rotating it.
- **The token never enters git.** `.mcp.json` is tracked; register the server either with env
  expansion (`"url": "http://127.0.0.1:<port>/mcp/${AIRDROME_MCP_TOKEN}"`) or in local scope
  (`claude mcp add --scope local`). Not the builder's choice to skip.
- **Public later** via latitude / cyphy.kz (owner) — a ROADMAP item, same token link.
- **Session per tool call.** The root `app` callback opens one session per command and commits on
  close; a server that runs for hours cannot live under it. `airdrome mcp` bypasses the callback the
  way `status` does (early return on the command name) and opens a session per tool call: commit on
  success, roll back on error. Business logic keeps the house rule — takes `s: Session`, flushes,
  never commits. Sessions that can touch `DedupGroup` get `install_mirror`, so `duplicates.json`
  stays current while the mirror exists.
- **No migration on start (call).** `airdrome mcp` checks the schema is at head and refuses to start
  otherwise, rather than migrating the live DB as a side effect of launching a server.
- **Every list is bounded.** `wanted` part 1 is "all of them" (thousands of rows): every list tool
  takes a page size and a cursor, returns compact rows, and the full card is a separate call.
- **Invariants edits must keep:** `canon_id` is terminal (no chains); exactly one `is_main` file per
  group (`recompute_main_files` after any canon change); a stored "not duplicates" is a hard,
  transitive cannot-link; one recording id per group.
- Python 3.14, the repo's conventions (one-line docstrings, ANN, `ty`), tests against the compose
  Postgres.

## What the server shows

All rows resolve to the **canonical** track (one hop through `canon_id`); counts are summed over
the whole group, as `navi push` does.

- **`wanted`** — one tool with a `part` argument, or two tools (builder's call).
  - *Part 1, library:* canonical groups where some member is in the owner's library and no member
    has a file. **Library = (owner, 2026-10-18)** every iTunes XML track ("в itunes xml все — мои
    треки"), plus on Spotify his saved (liked) tracks and the tracks of *his own* playlists — "not
    everything ever played". **Apple Media Services rows do not count** (they cover 13,193 of 13,243
    groups, i.e. everything streamed). Today that is 3,218 groups, all from iTunes XML. Unranked — the owner wants all of them; default order by date added.
  - *Part 2, scrobbled:* file-less groups with ≥ 1 play, sorted by play count — **plus unmatched
    scrobbles (owner, 2026-10-18)**: aliases bound to no track (52,287 aliases, 86,314 scrobbles,
    ~30% of all), shown as rows marked "unrecognized, N plays" in the same ranking. One song is often
    several aliases (album or platform differs), so rows collapse aliases on normalized
    artist + title **(call)**; the row lists its alias ids.
  - Both exclude groups with a *skip* verdict; *wanted* ones are shown, marked.
  - Each row carries signals the AI can reason with **(call)**: play count, first/last play and how
    many distinct months they span, playlist count, loved/rating, which library sources hold it, and
    whether it looks like a near-duplicate of a group *that has a file* (the ROADMAP's "ghost"
    case). The near-duplicate flag must not be a 6.9k × 6.1k trigram pass per call — precompute it,
    cache it per process, or compute it only for the page being returned (builder's call).
  - Filters worth having: by part, by verdict, text search on title/artist.
- **Track card** — the group's members, files (path, bitrate, `is_main`, `bind_method`/confidence),
  source rows, aliases with `match_method`/confidence and play counts, playlists, dedup group and
  its stored review if any, verdict.
- **Unmatched aliases** — paged, sorted by scrobble count, each with the top few candidate tracks
  and scores (the alias ladder's fuzzy step, read-only). Feeds edit 3.
- **Library status** — the same numbers `airdrome status` prints (reuse its logic, don't fork it).

## Point edits

Each edit is one call on one track or alias, returns the new state of what it touched, and is
idempotent. Each must survive a re-run of `land` and `dedup`.

1. **Verdict: wanted / skip** on a canonical track *or on an unmatched-scrobble row*. New table,
   e.g. `wanted_verdict(track_id nullable unique, alias_id nullable unique, verdict, note nullable,
   decided_at)` with exactly one of the two ids set; a verdict on a collapsed scrobble row is stored
   on each of its aliases. When such an alias is later bound to a track (edit 3 or `land`), its
   verdict counts for that track's group, *skip* winning a conflict **(call)**. Keyed by track id: `Track` rows are never re-keyed, and the
   owner accepts that a DB recreation drops them. A verdict on a twin is stored on its canon. A later
   file arriving for the group does not delete the verdict — `wanted` simply stops listing the
   group. *Wanted* is what the Soulseek loop will consume.
2. **Same track / different tracks** on two (or more) tracks. Built on the existing manual dedup
   storage (`dedupgroup` / `dedupgroupmember`, member-hash identity): *same* stores a confirmed
   group with a chosen canon (default: the existing `canon_order` pick) and applies it to
   `canon_id` now; *different* stores the all-NULL "not duplicates" group, which `dedup` already
   treats as a hard cannot-link, and unlinks them now. Both run `recompute_main_files`. Merging with
   an already stored group that shares a member is the builder's to get right (the TUI has the
   precedent).
3. **Bind an alias to a track / reject a bind.** Bind sets `track_id`, `match_method = MANUAL`,
   `match_confidence = 1.0`. `match_aliases` only touches aliases with `track_id IS NULL`, so a
   manual bind already survives `land`. **Reject needs a negative the ladder skips:** e.g.
   `track_id NULL` + `match_method = MANUAL` meaning "reviewed, matches nothing", with
   `do_match_aliases` filtering those out (or a separate column — builder's call). Plays: `TrackPlay`
   rows come from `copy_plays`, so a bind or reject must add/remove that alias's `TrackPlay` rows in
   the same call, or play counts and `wanted` part 2 stay wrong until the next `land`. Verify that no
   other path (`augment_aliases`, `land --rebuild-playlists`, `maint renormalize`) clears a manual
   alias binding; none was found on a first look.

## Spotify library importer

Settled in ROADMAP *Ingest sources* (2026-09-09); restated so this doc stands alone. The export is
`~/Загрузки/my_spotify_data.zip` (2026-09-16): `YourLibrary.json` (763 saved tracks, 49 albums,
120 artists; track = `artist`, `album`, `track`, `uri`), `Playlist1.json` (62 playlists, 3,364
items; item = `trackName`, `artistName`, `albumName`, `trackUri`, `addedDate`). No duration, no
album artist.

- Saved tracks → `SourceTrack(provider=SPOTIFY, source_id=<track uri>)`; tracks of own playlists
  also become Spotify `SourceTrack`s (they are library too — see *wanted* part 1); own playlists →
  `SourcePlaylist` / `SourcePlaylistTrack`. `provides = TRACKS | PLAYLISTS`. Saved albums and
  artists are out (no Album entity; no track lists). Followed (not owned) playlists are out — the
  export does not distinguish them by itself, so the builder decides how (`collaborators`, owner
  fields, or import all and say so).
- **The scrobble half retires in the same change:** `SpotifyImporter` inherits the `spotify` name and
  `--as spotify`, `detect` keys on the library files, and `SpotifyScrobbleParser`,
  `get_spotify_streaming_history`, `get_spotify_scrobbles`, `SpotifyRecord` go. Otherwise
  `--as spotify` silently means the wrong thing in between.
- **Gate the Apple path rebuild for Spotify rows:** `possible_locations` rebuilds *Apple* paths from
  any source's metadata, which can mis-bind a Spotify row to a file that merely path-matches, and
  costs ~18 `ILIKE` scans per track. The identity ladder still applies.
- **`land --merge-playlists`** would merge a Spotify playlist into a same-named Apple one. A choice
  to surface, not inherit silently — the builder asks the owner when it is reached.

## Order

**Server first (owner, 2026-10-18).** It works on today's data (iTunes XML for part 1, scrobbles
for part 2). The Spotify library importer is a separate, later change; its section above is kept so
the facts need not be re-derived, and the `land --merge-playlists` question is asked then.

## Calls made after the builder check

- **Schema rollout (call):** follow the repo habit — edit models, regenerate the single migration,
  recreate the DB and re-run the pipeline. The owner accepts that recreation drops manual state.
- **Edits act on groups (call):** *same(A, B)* merges A's whole group with B's whole group (one
  canon, `canon_order` default); *different(A, B)* separates A's group from B's group and leaves each
  group's other links alone. Stored to `dedupgroup` so `dedup` reproduces exactly that.
- **CLI and server may both run (call):** no guard in v1. Every tool call reads fresh state (no
  cross-call caches that a `dedup` could stale); the mirror is a whole-table snapshot at commit, so
  the last commit writes the current table. Revisit if it bites.
- **Verdict conflicts (call):** a verdict is looked up through any member of the group; when a merge
  joins a *wanted* and a *skip*, *skip* wins.

## Left to the builder

Tool names and argument shapes; one vs. two `wanted` tools; port; token routing; how the
near-duplicate flag is computed; the reject-alias encoding; cursor format; how to test the server
(FastMCP's in-process client is the obvious route).
