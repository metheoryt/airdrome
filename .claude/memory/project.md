<!-- KB refreshed against c4d5423 on 2026-07-26 -->

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
