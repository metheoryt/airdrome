import json

import pytest
from sqlalchemy import delete, select

from airdrome.models import DedupGroup
from airdrome.normalize.dedup.mirror import install_mirror, restore_if_empty, write_mirror
from airdrome.normalize.dedup.persistence import (
    apply_manual_overrides,
    import_dedup_groups,
    save_confirmed_groups,
)

from factories import make_dedup_group, make_page, make_track


@pytest.fixture()
def mirror_path(tmp_path):
    """A mirror path whose parent does not exist yet — the real `.airdrome/` case."""
    return tmp_path / ".airdrome" / "duplicates.json"


def _confirmed_page(session, label="k"):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    page = make_page([t1, t2])
    page.chosen_canons = [None, t1.id]
    page.confirmed = True
    return {label: page}, (t1, t2)


# --- the write half: snapshot on flush, file on commit ---


def test_mirror_written_after_commit(session, mirror_path):
    install_mirror(session, mirror_path)
    pages, (t1, t2) = _confirmed_page(session)

    save_confirmed_groups(session, pages)
    session.commit()

    data = json.loads(mirror_path.read_text(encoding="utf-8"))
    assert list(data) == ["k"]
    assert data["k"]["members"] == sorted([t1.duplicate_hash, t2.duplicate_hash])


def test_mirror_not_written_before_the_commit(session, mirror_path):
    install_mirror(session, mirror_path)
    pages, _ = _confirmed_page(session)

    save_confirmed_groups(session, pages)

    assert not mirror_path.exists()


def test_rollback_drops_the_snapshot_so_a_later_commit_does_not_write_it(session, mirror_path):
    """A snapshot left in session.info would be written by the next unrelated commit."""
    install_mirror(session, mirror_path)
    pages, _ = _confirmed_page(session)

    save_confirmed_groups(session, pages)
    session.rollback()

    make_track(session, "Unrelated")
    session.commit()

    assert not mirror_path.exists()


def test_clearing_every_group_writes_an_empty_object(session, mirror_path):
    """The guard against resurrection: a stale file would re-seed the next empty DB."""
    install_mirror(session, mirror_path)
    pages, _ = _confirmed_page(session)
    save_confirmed_groups(session, pages)
    session.commit()

    pages["k"].confirmed = False  # the user reset the group in the TUI
    save_confirmed_groups(session, pages)
    session.commit()

    assert json.loads(mirror_path.read_text(encoding="utf-8")) == {}


def test_commit_that_touched_no_group_leaves_the_mirror_alone(session, mirror_path):
    install_mirror(session, mirror_path)

    make_track(session, "Unrelated")
    session.commit()

    assert not mirror_path.exists()


def test_write_mirror_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / ".airdrome" / "duplicates.json"

    write_mirror({"a": {"members": ["h"], "canon_hashes": [None]}}, path)
    write_mirror({}, path)

    assert [p.name for p in path.parent.iterdir()] == ["duplicates.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == {}


# --- the read half: restore into an empty table only ---


def test_restore_seeds_an_empty_table(session, mirror_path):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    write_mirror(
        {"k": {"members": [t1.duplicate_hash, t2.duplicate_hash], "canon_hashes": [None, t1.duplicate_hash]}},
        mirror_path,
    )

    assert restore_if_empty(session, mirror_path) == 1

    [g] = session.scalars(select(DedupGroup)).all()
    assert {m.member_hash for m in g.members} == {t1.duplicate_hash, t2.duplicate_hash}


def test_restore_leaves_a_non_empty_table_untouched(session, mirror_path):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="stored")
    write_mirror({"other": {"members": ["ghost-a", "ghost-b"], "canon_hashes": [None, None]}}, mirror_path)

    assert restore_if_empty(session, mirror_path) == 0

    labels = {g.label for g in session.scalars(select(DedupGroup))}
    assert labels == {"stored"}


def test_restore_is_a_noop_without_a_file(session, mirror_path):
    assert restore_if_empty(session, mirror_path) == 0


def test_restore_is_a_noop_on_an_empty_mirror(session, mirror_path):
    write_mirror({}, mirror_path)

    assert restore_if_empty(session, mirror_path) == 0


def test_restore_then_commit_reproduces_the_same_mirror(session, mirror_path):
    """restore → mirror must be a fixpoint, or a DB rebuild would drift the file."""
    install_mirror(session, mirror_path)
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    original = {
        "k": {"members": sorted([t1.duplicate_hash, t2.duplicate_hash]), "canon_hashes": [None, None]}
    }
    write_mirror(original, mirror_path)

    restore_if_empty(session, mirror_path)
    session.commit()

    assert json.loads(mirror_path.read_text(encoding="utf-8")) == original


# --- the loop the feature exists for ---


def test_a_wiped_table_recovers_its_canons_from_the_mirror(session, mirror_path):
    """The whole point: a schema rebuild drops the rows, the mirror puts the picks back."""
    install_mirror(session, mirror_path)
    pages, (t1, t2) = _confirmed_page(session)
    save_confirmed_groups(session, pages)
    session.commit()

    session.execute(delete(DedupGroup))  # stands in for the rebuilt database
    session.commit()
    assert session.scalars(select(DedupGroup)).all() == []

    assert restore_if_empty(session, mirror_path) == 1
    apply_manual_overrides(session)

    assert t2.canon_id == t1.id
    assert t1.canon_id is None


def test_a_rolled_back_import_leaves_the_mirror_alone(session, mirror_path):
    """`dedup-import --dry-run` must not make the file claim what the DB rejected."""
    install_mirror(session, mirror_path)
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    write_mirror({"kept": {"members": ["a", "b"], "canon_hashes": [None, None]}}, mirror_path)

    import_dedup_groups(
        session,
        {"new": {"members": [t1.duplicate_hash, t2.duplicate_hash], "canon_hashes": [None, None]}},
    )
    session.rollback()

    assert list(json.loads(mirror_path.read_text(encoding="utf-8"))) == ["kept"]
