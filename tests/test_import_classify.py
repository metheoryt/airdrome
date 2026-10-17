"""`import <dirs>` over loose folders: collapse repeats, classify new / have / upgrade, import, swap.

The wavs are synthesized once per module (pure-Python synthesis is slow). A "lower bitrate re-encode"
is the same seeded audio written at 22050 Hz instead of 44100 Hz: no encoder is installed, and
mutagen reports a 16-bit mono wav's bitrate as rate * 16 (352 vs 705 kbps) while chromaprint
fingerprints both as the same recording.
"""

import shutil
from pathlib import Path

import pytest
from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE
from sqlalchemy import func, select

from airdrome.conf import settings
from airdrome.console import console
from airdrome.ingest.sources import MusicFolderImporter
from airdrome.library import COPIES_SUBDIR, MAIN_SUBDIR, MUSIC_SUBDIR, fingerprint
from airdrome.library.organize import FileOrganizer
from airdrome.library.scan import Verdict, classify
from airdrome.library.tags import file_md5, read_tags
from airdrome.models import ScanCache, Track, TrackFile
from airdrome.normalize.dedup.persistence import stored_cannot_links, stored_canons
from tests.conftest import write_wav
from tests.factories import make_dedup_group, make_track


SECONDS = 12


@pytest.fixture(scope="module")
def bank(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Source wavs, built once: song 1 at two bitrates, song 2 at two bitrates."""
    d = tmp_path_factory.mktemp("bank")
    return {
        "one_hi": write_wav(d / "one_hi.wav", seed=1, seconds=SECONDS),
        "one_lo": write_wav(d / "one_lo.wav", seed=1, seconds=SECONDS, rate=22050),
        "two_hi": write_wav(d / "two_hi.wav", seed=2, seconds=SECONDS),
        "two_lo": write_wav(d / "two_lo.wav", seed=2, seconds=SECONDS, rate=22050),
    }


@pytest.fixture(scope="module")
def prints(bank: dict[str, Path]) -> dict[str, fingerprint.Fingerprint]:
    """Fingerprints of the bank, computed once for seeding library files."""
    return {k: fingerprint.compute(p) for k, p in bank.items()}


@pytest.fixture()
def lib_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty library root; `settings.library_dir` points at it so no test reads the live one."""
    d = tmp_path / "lib"
    d.mkdir()
    monkeypatch.setattr(settings, "library_dir", d)
    return d


@pytest.fixture()
def seed_library(session, lib_dir: Path, bank: dict[str, Path], prints: dict[str, fingerprint.Fingerprint]):
    """Factory: a library with one track whose main file (a bank wav) is already placed in Library/."""

    def _seed(key: str) -> tuple[Track, TrackFile]:
        track = make_track(session, title="Song", artist="Artist", album="Album")
        rel = Path(MAIN_SUBDIR) / MUSIC_SUBDIR / track.generate_relative_path(ext="wav")
        (lib_dir / rel).parent.mkdir(parents=True)
        shutil.copy(bank[key], lib_dir / rel)
        tf = TrackFile(
            source_path=Path(f"/old/import/{key}.wav"),
            library_path=rel,
            is_main=True,
            track=track,
            bitrate=read_tags(bank[key]).bitrate,
            content_hash=file_md5(bank[key]),
            fingerprint=prints[key].raw,
            fp_duration=prints[key].duration,
        )
        session.add(tf)
        session.flush()
        return track, tf

    return _seed


@pytest.fixture()
def loose_root(tmp_path: Path) -> Path:
    """The folder of loose files a test imports."""
    return tmp_path / "loose"


@pytest.fixture()
def loose(loose_root: Path, bank: dict[str, Path]):
    """Factory copying a bank wav into the loose folder (optionally tagged); returns the new path."""

    def _drop(key: str, rel: str, title: str | None = None, artist: str | None = None) -> Path:
        dst = loose_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(bank[key], dst)
        if title:
            # Tags change the bytes (a distinct content_hash) but not the audio, so not the fingerprint.
            w = WAVE(dst)
            w.add_tags()
            assert w.tags is not None
            w.tags.add(TIT2(encoding=3, text=title))
            w.tags.add(TPE1(encoding=3, text=artist or "Someone"))
            w.save()
        return dst

    return _drop


@pytest.fixture()
def compute_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap `fingerprint.compute`, recording every path it is called with."""
    calls: list[Path] = []
    real = fingerprint.compute

    def counting(path: Path) -> fingerprint.Fingerprint:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(fingerprint, "compute", counting)
    return calls


def _n_files(session) -> int:
    """How many TrackFile rows exist."""
    return session.scalar(select(func.count()).select_from(TrackFile))


# ── classify ────────────────────────────────────────────────────────────────────


def test_repeats_collapse(session, seed_library, loose, loose_root) -> None:
    """The same bytes in two folders are one recording with two paths."""
    seed_library("one_hi")
    a = loose("two_hi", "a/x.wav")
    b = loose("two_hi", "b/y.wav")

    (cand,) = classify(session, [loose_root])

    assert sorted(cand.paths) == sorted([a, b])
    assert cand.verdict is Verdict.NEW


def test_same_audio_different_bytes_collapses_to_best(session, seed_library, loose, loose_root) -> None:
    """Identical audio at two bitrates collapses by fingerprint; the best copy is the higher bitrate."""
    seed_library("one_hi")
    hi = loose("two_hi", "a/x.wav")
    lo = loose("two_lo", "c/z.wav")

    (cand,) = classify(session, [loose_root])

    assert sorted(cand.paths) == sorted([hi, lo])
    assert cand.best == hi


def test_reencoded_copy_is_have(session, seed_library, loose, loose_root) -> None:
    """A lower-bitrate copy of a library recording is `have`, and importing it writes no TrackFile."""
    track, _main = seed_library("one_hi")
    loose("one_lo", "cd1/01.wav")

    (cand,) = classify(session, [loose_root])
    assert cand.verdict is Verdict.HAVE
    assert cand.group_canon is track

    MusicFolderImporter(loose_root).import_tracks(session)
    assert _n_files(session) == 1
    assert session.scalar(select(func.count()).select_from(ScanCache)) == 1  # only the cache is written


def test_better_copy_is_upgrade_and_swaps(
    session, seed_library, loose, loose_root, lib_dir: Path, bank
) -> None:
    """A higher-bitrate copy is `upgrade`; after import + organize it holds Library/, the old one Copies/."""
    track, old = seed_library("one_lo")
    old_rel = old.library_path
    loose("one_hi", "dvd/song.wav")

    (cand,) = classify(session, [loose_root])
    assert cand.verdict is Verdict.UPGRADE

    MusicFolderImporter(loose_root).import_tracks(session)
    new = session.scalars(select(TrackFile).where(TrackFile.id != old.id)).one()
    assert new.track is track
    assert new.fingerprint is not None  # carried from the scan cache, so `land` need not recompute

    FileOrganizer(dst_dir=lib_dir).organize(session)

    assert new.library_path == old_rel
    assert new.is_main and not old.is_main
    assert old.library_path.parts[0] == COPIES_SUBDIR
    assert file_md5(lib_dir / new.library_path) == file_md5(bank["one_hi"])
    assert file_md5(lib_dir / old.library_path) == file_md5(bank["one_lo"])


def test_unknown_is_new(session, seed_library, loose, loose_root) -> None:
    """Unmatched audio is `new`; importing creates its TrackFile and the ladder gives it a Track."""
    seed_library("one_hi")
    loose("two_hi", "misc/track.wav", title="Other", artist="Band")

    (cand,) = classify(session, [loose_root])
    assert cand.verdict is Verdict.NEW
    assert cand.group_canon is None

    MusicFolderImporter(loose_root).import_tracks(session)
    tf = session.scalars(select(TrackFile).where(TrackFile.source_path == cand.best)).one()
    assert tf.track is not None
    assert (tf.track.title, tf.track.artist) == ("Other", "Band")


def test_rerun_uses_cache_and_adds_nothing(session, seed_library, loose, loose_root, compute_calls) -> None:
    """Review Focus #5: a second run over the same folders fingerprints nothing and imports nothing."""
    seed_library("one_lo")
    loose("one_hi", "a/up.wav")
    loose("two_hi", "b/new.wav", title="Other", artist="Band")

    first = classify(session, [loose_root])
    assert sorted(c.verdict for c in first) == sorted([Verdict.UPGRADE, Verdict.NEW])
    MusicFolderImporter(loose_root).import_tracks(session)
    n_files = _n_files(session)
    assert n_files == 3
    computed = len(compute_calls)

    compute_calls.clear()
    # Drop the identity map so run 2 reads the cache rows back from Postgres, not from memory.
    session.expire_all()
    MusicFolderImporter(loose_root).import_tracks(session)
    second = classify(session, [loose_root])

    assert compute_calls == []
    assert computed == 2
    assert _n_files(session) == n_files
    # The same recordings are listed again; what run 1 imported is now in the library.
    assert sorted(c.best for c in second) == sorted(c.best for c in first)
    assert {c.verdict for c in second} == {Verdict.HAVE}


def test_rerun_never_reimports_an_unbound_new_file(session, seed_library, loose, loose_root) -> None:
    """An untagged, unmatched file stays unbound after run 1; run 2 still reports it but adds nothing."""
    seed_library("one_hi")
    loose("two_hi", "x/untitled.wav")
    MusicFolderImporter(loose_root).import_tracks(session)
    assert _n_files(session) == 2

    (cand,) = classify(session, [loose_root])
    assert (cand.verdict, cand.in_library) == (Verdict.NEW, True)
    MusicFolderImporter(loose_root).import_tracks(session)
    assert _n_files(session) == 2


def test_upgrade_beside_an_imported_copy_is_imported(
    session, seed_library, loose, loose_root, prints
) -> None:
    """A folder holding both a copy the library imported and a better one: the better one is imported."""
    track, main = seed_library("one_lo")
    imported = loose("one_lo", "a/old.wav")
    main.source_path = imported  # the library's main came from this very folder (copy-mode organize)
    better = loose("one_hi", "b/new.wav")

    (cand,) = classify(session, [loose_root])
    assert cand.verdict is Verdict.UPGRADE
    assert (cand.best, cand.in_library) == (better, False)

    MusicFolderImporter(loose_root).import_tracks(session)
    assert session.scalars(select(TrackFile).where(TrackFile.source_path == better)).one().track is track


def test_unfingerprinted_library_file_is_fingerprinted_first(
    session, seed_library, loose, loose_root
) -> None:
    """R13: a bound library file without a fingerprint gets one, so an untagged copy reads `have`."""
    track, main = seed_library("one_hi")
    main.fingerprint = main.fp_duration = None
    session.flush()
    loose("one_lo", "cd/01.wav")

    (cand,) = classify(session, [loose_root])

    assert (cand.verdict, cand.group_canon) == (Verdict.HAVE, track)
    assert main.fingerprint is not None


def test_corrupt_file_is_reported_and_skipped(session, seed_library, loose, loose_root) -> None:
    """An unreadable file never aborts the import; the readable ones are still classified."""
    seed_library("one_hi")
    loose("two_hi", "a/fine.wav")
    bad = loose_root / "a" / "broken.mp3"
    bad.write_bytes(b"not audio at all")

    (cand,) = classify(session, [loose_root])

    assert bad not in cand.paths
    MusicFolderImporter(loose_root).import_tracks(session)  # does not raise


def test_dry_run_swap_touches_nothing(session, seed_library, loose, loose_root, lib_dir: Path, bank) -> None:
    """A dry-run organize of an imported upgrade plans the swap but leaves every byte where it was."""
    _track, old = seed_library("one_lo")
    old_rel = old.library_path
    src = loose("one_hi", "dvd/song.wav")
    MusicFolderImporter(loose_root).import_tracks(session)
    new = session.scalars(select(TrackFile).where(TrackFile.id != old.id)).one()

    FileOrganizer(dst_dir=lib_dir, dry_run=True).organize(session)

    assert new.library_path == old_rel  # planned
    assert old.library_path.parts[0] == COPIES_SUBDIR
    assert src.exists()
    assert file_md5(lib_dir / old_rel) == file_md5(bank["one_lo"])
    assert not (lib_dir / COPIES_SUBDIR).exists()


def test_report_names_untagged_copies_after_the_library_track(
    session, seed_library, loose, loose_root
) -> None:
    """An untagged copy's report line borrows the matched track's artist and title, then the totals."""
    seed_library("one_hi")
    loose("one_lo", "cd/01.wav")
    loose("one_lo", "cd2/01.wav")

    with console.capture() as cap:
        MusicFolderImporter(loose_root).import_tracks(session)

    out = cap.get()
    assert "have     Artist — Song  [2 copies]" in out
    assert "0 new, 0 upgrade, 1 have — 0 file(s) to import" in out


def test_import_onto_stored_group_member_keeps_the_group(session, seed_library, loose, loose_root) -> None:
    """I-1: a file whose tags hit a hand-made group's member does not backfill its hashed fields."""
    seeded, _ = seed_library("one_hi")
    member = make_track(session, title="Other", artist="X")  # cloud-only: no file, no duration
    make_dedup_group(session, [(seeded, None), (member, None)])  # "not duplicates"
    canons, vetoes = stored_canons(session), stored_cannot_links(session)
    assert canons and vetoes
    tagged = loose("two_hi", "a/other.wav", title="Other", artist="X")

    MusicFolderImporter(loose_root).import_tracks(session)

    assert session.scalars(select(TrackFile).where(TrackFile.source_path == tagged)).one().track is member
    assert member.duration is None
    assert (stored_canons(session), stored_cannot_links(session)) == (canons, vetoes)


def test_untagged_new_files_are_reported_unbound(session, seed_library, loose, loose_root) -> None:
    """I-3: a new file with no tags that matches nothing is imported unbound, and the summary says so."""
    seed_library("one_hi")
    untitled = loose("two_hi", "x/untitled.wav")

    with console.capture() as cap:
        MusicFolderImporter(loose_root).import_tracks(session)

    out = cap.get()
    assert "1 new, 0 upgrade, 0 have — 1 file(s) to import" in out
    assert "1 new file(s) have no tags: imported unbound" in out
    assert session.scalars(select(TrackFile).where(TrackFile.source_path == untitled)).one().track is None


def test_tagged_new_file_has_no_unbound_line(session, seed_library, loose, loose_root) -> None:
    """A new file with a title gets a Track from its tags, so no unbound line is printed."""
    seed_library("one_hi")
    loose("two_hi", "x/named.wav", title="Named")

    with console.capture() as cap:
        MusicFolderImporter(loose_root).import_tracks(session)

    assert "imported unbound" not in cap.get()


def test_empty_library_scan_skips_an_unreadable_file(session, loose, loose_root) -> None:
    """M-4: on the empty-library bulk scan one corrupt file is reported and skipped, not fatal."""
    fine = loose("one_hi", "a/fine.wav")
    bad = loose_root / "a" / "broken.mp3"
    bad.write_bytes(b"not audio at all")

    with console.capture() as cap:
        MusicFolderImporter(loose_root).import_tracks(session)

    assert "skipped" in cap.get() and "broken.mp3" in cap.get()
    assert [tf.source_path for tf in session.scalars(select(TrackFile))] == [fine]
