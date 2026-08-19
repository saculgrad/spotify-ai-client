"""
test_session_store.py — thorough tests for session_store.py's SQLite-backed
ReviewSession persistence.

Real SQLite files against pytest's tmp_path — no mocking, since this
genuinely is local file I/O (same philosophy as test_curation.py's
RecentlyUsedLog tests).

Run:
    source .venv/bin/activate
    pytest test_session_store.py -v
"""

from __future__ import annotations

import pytest

from resolver import Candidate, MatchResult
from review import APPROVED, PENDING, REGENERATE_REQUESTED, REMOVED
from session_store import SessionStore


def mk(id_, title="Song", artist="Artist", explicit=None):
    return MatchResult(
        candidate=Candidate(title, artist), accepted=True, reason="ok",
        track_id=id_, track_uri=f"spotify:track:{id_}",
        track_name=title, track_artists=artist, explicit=explicit, score=0.95,
        subscores={"title_sim": 1.0, "artist_sim": 1.0, "penalty": 0.0, "penalty_words": [], "base": 1.0},
    )


# ─────────────────────────────────────────────────────────────────────────────
# create / load basics
# ─────────────────────────────────────────────────────────────────────────────

def test_create_returns_an_id_and_load_round_trips_tracks(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a", "Song A"), mk("b", "Song B")])

    assert isinstance(session_id, str) and session_id

    session = store.load(session_id)
    assert session is not None
    assert [i.result.track_id for i in session.items()] == ["a", "b"]
    assert all(i.status == PENDING for i in session.items())


def test_load_preserves_all_match_result_fields(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    track = mk("a", title="Ain't No Sunshine", artist="Bill Withers", explicit=False)
    session_id = store.create([track])

    [item] = store.load(session_id).items()
    r = item.result

    assert r.track_id == "a"
    assert r.track_uri == "spotify:track:a"
    assert r.track_name == "Ain't No Sunshine"
    assert r.track_artists == "Bill Withers"
    assert r.explicit is False
    assert r.score == 0.95
    assert r.subscores["title_sim"] == 1.0
    assert r.candidate.title == "Ain't No Sunshine"
    assert r.candidate.artist == "Bill Withers"


def test_load_unknown_session_id_returns_none(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    assert store.load("nonexistent") is None


def test_create_with_empty_track_list(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([])
    session = store.load(session_id)
    assert session.items() == []


def test_create_propagates_reviewsession_validation_errors(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    with pytest.raises(ValueError, match="Duplicate"):
        store.create([mk("a"), mk("a")])


def test_two_different_sessions_are_independent(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    id1 = store.create([mk("a")])
    id2 = store.create([mk("b")])

    assert id1 != id2
    assert [i.result.track_id for i in store.load(id1).items()] == ["a"]
    assert [i.result.track_id for i in store.load(id2).items()] == ["b"]


# ─────────────────────────────────────────────────────────────────────────────
# save — status/note persistence and cross-load durability
# ─────────────────────────────────────────────────────────────────────────────

def test_save_persists_status_and_note_changes(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a"), mk("b"), mk("c")])

    session = store.load(session_id)
    session.approve("a")
    session.remove("b", reason="wrong version")
    session.request_regenerate("c", note="too slow")
    store.save(session_id, session)

    reloaded = store.load(session_id)
    statuses = {i.result.track_id: i.status for i in reloaded.items()}
    notes = {i.result.track_id: i.note for i in reloaded.items()}
    assert statuses == {"a": APPROVED, "b": REMOVED, "c": REGENERATE_REQUESTED}
    assert notes["b"] == "wrong version"
    assert notes["c"] == "too slow"


def test_save_from_a_second_store_instance_pointed_at_same_file_is_visible(tmp_path):
    # Simulates two different Flask worker processes sharing one db file.
    db_path = tmp_path / "sessions.db"
    store1 = SessionStore(db_path)
    session_id = store1.create([mk("a")])

    store2 = SessionStore(db_path)   # a different "process"
    session = store2.load(session_id)
    session.approve("a")
    store2.save(session_id, session)

    # back on "store1" (or a fresh instance) — change must be visible
    store3 = SessionStore(db_path)
    assert store3.load(session_id).items()[0].status == APPROVED


def test_save_overwrites_previous_state_not_appends(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a")])

    session = store.load(session_id)
    session.approve("a")
    store.save(session_id, session)

    session2 = store.load(session_id)
    session2.remove("a", reason="changed my mind")
    store.save(session_id, session2)

    final = store.load(session_id)
    assert len(final.items()) == 1
    assert final.items()[0].status == REMOVED


def test_save_on_nonexistent_id_is_a_silent_no_op(tmp_path):
    # Documented SQL UPDATE-on-missing-row behavior — callers should load()
    # first if they need to distinguish "saved" from "silently did nothing."
    store = SessionStore(tmp_path / "sessions.db")
    from review import ReviewSession
    phantom_session = ReviewSession([mk("a")])
    store.save("does-not-exist", phantom_session)   # must not raise
    assert store.load("does-not-exist") is None


# ─────────────────────────────────────────────────────────────────────────────
# delete
# ─────────────────────────────────────────────────────────────────────────────

def test_delete_removes_the_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a")])
    assert store.load(session_id) is not None

    store.delete(session_id)

    assert store.load(session_id) is None


def test_delete_nonexistent_id_does_not_raise(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.delete("never-existed")   # must not raise


def test_delete_only_removes_the_targeted_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    id1 = store.create([mk("a")])
    id2 = store.create([mk("b")])

    store.delete(id1)

    assert store.load(id1) is None
    assert store.load(id2) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Reopening the store (simulates a server restart)
# ─────────────────────────────────────────────────────────────────────────────

def test_data_survives_reopening_the_store(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session_id = store.create([mk("a")])
    session = store.load(session_id)
    session.approve("a")
    store.save(session_id, session)
    del store   # simulate process exit

    fresh_store = SessionStore(db_path)   # simulate process restart
    reloaded = fresh_store.load(session_id)
    assert reloaded is not None
    assert reloaded.items()[0].status == APPROVED


def test_init_db_is_idempotent_across_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    store1 = SessionStore(db_path)
    session_id = store1.create([mk("a")])
    store2 = SessionStore(db_path)   # re-runs CREATE TABLE IF NOT EXISTS — must not error or wipe data
    assert store2.load(session_id) is not None


# ─────────────────────────────────────────────────────────────────────────────
# target metadata — where the batch is headed (Spotify playlist id/name)
# ─────────────────────────────────────────────────────────────────────────────

def test_create_without_target_get_target_returns_none(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a")])
    assert store.get_target(session_id) is None


def test_create_with_target_round_trips(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    target = {"action": "append", "playlist_id": "pl123", "playlist_name": None}
    session_id = store.create([mk("a")], target=target)

    assert store.get_target(session_id) == target


def test_get_target_unknown_session_returns_none(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    assert store.get_target("does-not-exist") is None


def test_target_survives_save_calls(tmp_path):
    # save() (approve/remove/etc.) must not clobber the target set at create().
    store = SessionStore(tmp_path / "sessions.db")
    target = {"action": "create", "playlist_id": None, "playlist_name": "Sunday Brunch"}
    session_id = store.create([mk("a")], target=target)

    session = store.load(session_id)
    session.approve("a")
    store.save(session_id, session)

    assert store.get_target(session_id) == target


def test_target_survives_reopening_the_store(tmp_path):
    db_path = tmp_path / "sessions.db"
    target = {"action": "append", "playlist_id": "pl123", "playlist_name": None}
    store = SessionStore(db_path)
    session_id = store.create([mk("a")], target=target)
    del store

    fresh_store = SessionStore(db_path)
    assert fresh_store.get_target(session_id) == target


# ─────────────────────────────────────────────────────────────────────────────
# prune — abandoned-session cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _backdate(db_path, session_id, when):
    import sqlite3
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE review_sessions SET updated_at = ? WHERE id = ?",
            (when.isoformat(), session_id),
        )
        conn.commit()


def test_prune_removes_sessions_older_than_cutoff(tmp_path):
    import datetime as dt

    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    old_id = store.create([mk("a")])
    fresh_id = store.create([mk("b")])

    _backdate(db_path, old_id, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10))

    removed = store.prune(older_than_days=7)

    assert removed == 1
    assert store.load(old_id) is None
    assert store.load(fresh_id) is not None


def test_prune_uses_updated_at_not_created_at(tmp_path):
    # A session actively being reviewed (save() called recently) must survive
    # pruning even if it was originally created long ago.
    import datetime as dt
    import sqlite3

    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session_id = store.create([mk("a")])

    # Manually backdate created_at only, leaving updated_at fresh — simulates
    # a long review that started a while back but is still being worked on.
    with sqlite3.connect(str(db_path)) as conn:
        old_stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
        conn.execute("UPDATE review_sessions SET created_at = ? WHERE id = ?", (old_stamp, session_id))
        conn.commit()

    removed = store.prune(older_than_days=7)

    assert removed == 0
    assert store.load(session_id) is not None


def test_prune_nothing_to_remove_returns_zero(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.create([mk("a")])
    assert store.prune(older_than_days=7) == 0


def test_prune_default_window_leaves_fresh_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create([mk("a")])
    store.prune()   # uses DEFAULT_PRUNE_AFTER_DAYS
    assert store.load(session_id) is not None


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
