<!-- KB refreshed against fcbff68 on 2026-09-12 -->

# Airdrome — project memory

Repo-local, git-tracked memory. Durable *workflow* facts that don't belong in
[AGENTS.md](../../AGENTS.md) (current-state map), [README.md](../../README.md) (user-facing)
or [ROADMAP.md](../../ROADMAP.md) (forward-looking).

## Worktrees

- A fresh worktree (Orca or plain `git worktree add`) starts **without `.venv` and
  `.env`** — both are gitignored, so nothing carries over from the base checkout. No
  `uv run` command works until you `uv sync` and create `.env` (`DB_DSN`, `LIBRARY_DIR`
  — see README *Configuration*). The repo has no `.orca/worktree-setup.sh`, so this is
  manual per worktree.
- The test suite additionally needs the compose Postgres up (`docker compose up -d`,
  port 5437). **On g15 (native Ubuntu, Docker 29.8.0 at `/usr/bin/docker`) this works
  from any checkout** — verified 2026-09-11, `airdrome-db-1` up 14 h. The old "Docker
  is unreachable, use another box" caveat described the destroyed `g614jv` WSL distro
  and is now in global memory as a WSL-only trap.

## Real-library provenance (2026-09-09)

- **The 881 hand-made dedup groups came from `latitude:/mnt/immich/xs-keepers/repos/airdrome/`**
  — an archived June 2026 checkout of this repo, the keepers pile from the retired `xs`
  drive. It is the only place they survived the 2026-09-07 g513ie Windows wipe. That
  directory also holds the non-reconstructible cloud exports (`apple/` 620 M,
  `listenbrainz/` two zips, `spotify/`, `lastfm/`, `auto-dedup.history.json`).
- **Live setup on g15:** `LIBRARY_DIR=/home/me/Music/Airdrome` (airdrome's managed output —
  `Library/` + `Copies/`), imported from `/home/me/Music/PicardedMusic` (66 G, Picard-tagged,
  the untouched source). The cloud exports were copied into `LIBRARY_DIR` alongside them.
- **`land` is slow for a structural reason, not a broken box.** `_bind_track_files` runs
  `possible_locations(max_suffix=2)` — 12-24 candidate paths per Apple source track — as one
  `ILIKE '%…%'` per candidate: ~490k full scans of `trackfile` at 8-11 ms each, ~40 min for
  27k source tracks. An index does not fix it (at ~7k files the planner costs a seq scan
  cheaper than a forced `pg_trgm` lookup, 215 vs 256, though the lookup measures 0.28 ms).
  The fix is batching the candidates against one in-memory dict of `source_path`.
- Scrobble import dedupes on **timestamp alone, across platforms** (`get_fresh_scrobbles`,
  "single-user database"). Overlapping exports of the same service are safe; two services
  with clock skew would double-count the same play.
- **Alias match coverage tops out around a third** — 26,745 of 79,051 aliases matched on the
  2026-09-09 run. The unmatched ~52k are scrobbles for tracks that are not in the library at
  all, so that is the ceiling on play-history coverage, not a matching failure to tune away.
  <!-- src: airdrome fcbff68 | 2026-09-12 -->

## Local wiring (2026-09-12)

- **The gortex wiring is committed on purpose** — the `gortex` entry in `.mcp.json` plus
  `.claude/settings.json` (its MCP `permissions.allow` block), so the integration is
  reproducible for anyone who clones rather than being a property of one machine's daemon.
  `.gortex/` is local index state and is gitignored, and so is `.claude/skills/generated/` —
  that second line is what stops a future bare `gortex init` from committing the ~20 generated
  routing skills it sprays.
  <!-- src: airdrome e0daf7a | 2026-09-12 -->
- **`.mcp.json`'s `postgres` server needs `--with 'mcp<2'`.** `postgres-mcp` does not cap its
  own `mcp` dependency, so `uvx` resolves the 2.x SDK — where FastMCP was renamed to
  MCPServer — and the package's v1-era import dies with `ModuleNotFoundError: No module named
  'mcp.server.fastmcp'` before the handshake. The only symptom the client shows is a failed
  reconnect, which reads as a database problem and is not one: the compose Postgres on 5437
  was up and reachable throughout.
  <!-- src: airdrome 86b30ef | 2026-09-12 -->
- **`LIBRARY_DIR` in `.env` must be an absolute path** — pydantic-settings does not expand `~`
  from a dotenv file, so `~/Music/Airdrome` would be taken as a literal directory named `~`.
  <!-- src: airdrome fcbff68 | 2026-09-12 -->

## Writing code and scripts against this repo (2026-09-12)

- **A standalone script that touches the ORM must `import airdrome.cloud.sources` first** —
  that import is what registers the mappers, and the CLI does it for you, so a script written
  against the models alone fails in a way the same code never does under `airdrome ...`. The
  engine lives in `airdrome.models`; there is no `airdrome.db`.
  <!-- src: airdrome af7f9e0 | 2026-09-12 -->
- **Do not stub the session with a `MagicMock` in the CLI tests.** A mock silently accepts
  `event.listens_for(...)` in a way no real session would, so a listener that could never
  attach still passes its test. Use a real unbound `Session` — swapping the stub is what
  proved `install_mirror`'s wiring.
  <!-- src: airdrome af7f9e0 | 2026-09-12 -->
- **`PlaylistAdapter` must not inherit `contextlib.AbstractContextManager`.** Doing so makes
  `__exit__` abstract and every test fake then fails to instantiate (14 tests, 2026-09-08).
  The ABC carries concrete no-op `__enter__`/`__exit__` instead — a type-only default so
  `ExitStack.enter_context` checks out, unreachable in practice because both concrete adapters
  override them.
  <!-- src: airdrome e0daf7a | 2026-09-12 -->
- **`_apply_override` hardcodes `return True`**, so a conflicted playlist that resolves to no
  actual change still counts as `+` in the `N/M reconciled` tally. Pre-existing and cosmetic,
  but reached far more often since conflicts stopped being human-resolved.
  <!-- src: airdrome bc37456 | 2026-09-12 -->
- **More than one agent session may be editing this checkout at once.** A commit from a
  parallel session landed on `main` between two commits of another on 2026-09-08; nothing was
  lost, but re-read `git log` before assuming your HEAD is the newest thing on the branch.
  <!-- src: airdrome c257a88 | 2026-09-12 -->

## Picard / tagging vendor facts (routed here from the 2026-09-12 shared proposals)

The harvest tagged these `global`; `/memory-review` routed them here on
2026-09-12 because they are domain facts, not fleet-wide ones. Source: session
`0ff09805`.

- **MusicBrainz Picard 3's config is a QSettings INI that Picard rewrites WHOLE on
  exit**, so any external edit must be made with Picard CLOSED and then verified
  across a real launch/quit cycle (md5 the ini before and after). Write it through
  Picard's own config API (`sys.path.insert(0,'/usr/lib/picard')`;
  `import picard.options`; `picard.config.setup_config(app, ini)`) because
  structured values such as `list_of_scripts` are pickled QVariant blobs, not plain
  ini text. Option names changed in 3: `standardize_artists` →
  `standardize_artist_names` (now a `StandardizeArtistNames` Enum), `image_filename`
  → `cover_image_filename`. `picard-cli` supports only `completions`, `plugins`,
  `profiles` — **there is no headless tagging.**
- **Picard needs no AcoustID key to Scan.** It ships its own *application* key
  (`ACOUSTID_KEY = 'v8pQ6oyB'` in `picard/const/__init__.py`) and uses it for every
  lookup; the `acoustid_apikey` setting is the user's *account* key and is passed as
  `user=` to `/submit` only, i.e. it enables contributing fingerprints back.
  Submissions are public and permanent, so submit only from matches already
  confirmed. (Verified end to end: an untagged file resolved at score 0.985 with no
  user key set.)
- **mutagen returns MP4 freeform atom values as `bytes`.** `str()` on one yields
  `"b'<uuid>'"`, so the identical MusicBrainz album ID compares UNEQUAL between an
  `.m4a` and the `.mp3` beside it — which fakes "this file changed" findings on any
  mixed-format folder (it faked 26 of 50 in the case measured). Decode explicitly
  (`value.decode('utf-8','replace')`) on both sides, and remember a JSON snapshot
  taken with the naive `str()` needs the same unwrapping on read-back.
- **Navidrome tag mapping, two limits that only bite MP4** (read from
  `navidrome/resources/mappings.yaml`, not inferred): there is **no MP4 alias for
  `ALBUMARTISTS`**, and on MP4 Picard's `date` lands in `©day`, which Navidrome
  reads as *releasedate*. So of Navidrome's three documented Picard tagger scripts,
  the multi-value `ARTISTS` one works on m4a while the `ALBUMARTISTS` and
  release/original-date ones are partly inert there; MP3 is unaffected. Also:
  Picard 3 writes `TXXX:ALBUMARTISTS` natively, so `$setmulti(albumartists,…)` only
  adds a duplicate lowercase frame, and `$if(%_recordingcomment%,$set(subtitle,…))`
  fills `TIT3` with bare words like "explicit" that Navidrome renders as a track
  subtitle.

## Two music trees, two roles (g15, measured 2026-09-12)

- `~/Music/PicardedMusic` (66 G, 6,986 audio files — 5,775 mp3 / 1,211 m4a) is the
  **Picard-tagged source of truth**, copied from the iTunes library and never
  written by airdrome.
- `~/Music/Airdrome` is airdrome's `LIBRARY_DIR` — its managed output (`Library/` +
  `Copies/`, ~64 G copied, plus the `.airdrome/duplicates.json` mirror and the cloud
  exports).
- `~/Music/OldMusic` is a third, untouched pile.
- **Do not point `LIBRARY_DIR` at PicardedMusic**: `organize` would write a second
  layout inside the tree it just imported, and the next `import` would re-ingest
  airdrome's own output.
