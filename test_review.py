"""
test_review.py — thorough tests for review.py's ReviewSession state machine.

Fully offline — pure in-memory logic, no fixtures needed beyond
resolver.MatchResult objects.

Run:
    source .venv/bin/activate
    pytest test_review.py -v
"""

from __future__ import annotations

import pytest

from resolver import Candidate, MatchResult
from review import (
    APPROVED,
    PENDING,
    REGENERATE_REQUESTED,
    REMOVED,
    ReviewSession,
)


_UNSET = object()


def mk(id_, title="Song", artist="Artist", uri=_UNSET):
    resolved_uri = f"spotify:track:{id_}" if uri is _UNSET else uri
    return MatchResult(
        candidate=Candidate(title, artist), accepted=True, reason="ok",
        track_id=id_, track_uri=resolved_uri,
        track_name=title, track_artists=artist, score=1.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Construction
# ─────────────────────────────────────────────────────────────────────────────

def test_construction_starts_everything_pending():
    session = ReviewSession([mk("a"), mk("b")])
    assert session.summary() == {
        "pending": 2, "approved": 0, "removed": 0, "regenerate_requested": 0, "total": 2,
    }
    assert {r.track_id for r in session.pending()} == {"a", "b"}


def test_construction_empty_list():
    session = ReviewSession([])
    assert session.summary() == {
        "pending": 0, "approved": 0, "removed": 0, "regenerate_requested": 0, "total": 0,
    }
    assert session.approved() == []
    assert session.final_uris() == []


def test_construction_rejects_track_missing_id():
    with pytest.raises(ValueError, match="track_id"):
        ReviewSession([mk(None)])


def test_construction_rejects_duplicate_track_ids():
    with pytest.raises(ValueError, match="Duplicate"):
        ReviewSession([mk("a"), mk("a")])


# ─────────────────────────────────────────────────────────────────────────────
# approve / remove / request_regenerate — basic transitions
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_moves_track_to_approved():
    session = ReviewSession([mk("a")])
    session.approve("a")
    assert [r.track_id for r in session.approved()] == ["a"]
    assert session.pending() == []


def test_remove_moves_track_to_removed_with_reason():
    session = ReviewSession([mk("a")])
    session.remove("a", reason="wrong version")
    removed = session.removed()
    assert len(removed) == 1
    assert removed[0][0].track_id == "a"
    assert removed[0][1] == "wrong version"


def test_remove_without_reason_defaults_to_none():
    session = ReviewSession([mk("a")])
    session.remove("a")
    assert session.removed()[0][1] is None


def test_request_regenerate_moves_track_with_note():
    session = ReviewSession([mk("a")])
    session.request_regenerate("a", note="too slow")
    reqs = session.regenerate_requests()
    assert len(reqs) == 1
    assert reqs[0][1] == "too slow"


def test_unknown_track_id_raises_keyerror_on_every_action():
    session = ReviewSession([mk("a")])
    with pytest.raises(KeyError):
        session.approve("nonexistent")
    with pytest.raises(KeyError):
        session.remove("nonexistent")
    with pytest.raises(KeyError):
        session.request_regenerate("nonexistent")


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency and "changing your mind" transitions
# ─────────────────────────────────────────────────────────────────────────────

def test_double_approve_is_idempotent():
    session = ReviewSession([mk("a")])
    session.approve("a")
    session.approve("a")
    assert len(session.approved()) == 1
    assert session.summary()["approved"] == 1


def test_approve_after_remove_overrides_to_approved():
    session = ReviewSession([mk("a")])
    session.remove("a", reason="too obscure")
    session.approve("a")
    assert session.approved()[0].track_id == "a"
    assert session.removed() == []
    # note is cleared when re-approved
    assert session._items["a"].note is None


def test_remove_after_approve_overrides_to_removed():
    session = ReviewSession([mk("a")])
    session.approve("a")
    session.remove("a", reason="changed my mind")
    assert session.approved() == []
    assert session.removed()[0][1] == "changed my mind"


def test_regenerate_after_approve_overrides_to_regenerate_requested():
    session = ReviewSession([mk("a")])
    session.approve("a")
    session.request_regenerate("a", note="want something else")
    assert session.approved() == []
    assert len(session.regenerate_requests()) == 1


def _apply(session, action, track_id):
    if action == "approve":
        session.approve(track_id)
    elif action == "remove":
        session.remove(track_id, "note")
    else:
        session.request_regenerate(track_id, "note")


@pytest.mark.parametrize("first_action", ["approve", "remove", "request_regenerate"])
@pytest.mark.parametrize("second_action", ["approve", "remove", "request_regenerate"])
def test_every_pairwise_transition_lands_in_the_second_actions_status(first_action, second_action):
    expected_status = {"approve": APPROVED, "remove": REMOVED, "request_regenerate": REGENERATE_REQUESTED}
    session = ReviewSession([mk("a")])

    _apply(session, first_action, "a")
    _apply(session, second_action, "a")

    assert session._items["a"].status == expected_status[second_action]


# ─────────────────────────────────────────────────────────────────────────────
# approve_all_pending
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_all_pending_only_touches_pending():
    session = ReviewSession([mk("a"), mk("b"), mk("c")])
    session.remove("b", reason="explicit content")

    count = session.approve_all_pending()

    assert count == 2
    assert {r.track_id for r in session.approved()} == {"a", "c"}
    assert session.removed()[0][0].track_id == "b"


def test_approve_all_pending_on_empty_pending_does_nothing():
    session = ReviewSession([mk("a")])
    session.approve("a")
    count = session.approve_all_pending()
    assert count == 0
    assert session.summary()["approved"] == 1


def test_approve_all_pending_does_not_touch_regenerate_requests():
    session = ReviewSession([mk("a"), mk("b")])
    session.request_regenerate("a", note="nope")
    session.approve_all_pending()
    assert session.regenerate_requests()[0][0].track_id == "a"
    assert {r.track_id for r in session.approved()} == {"b"}


# ─────────────────────────────────────────────────────────────────────────────
# add_tracks — "generate more" follow-up flow: new candidates join the
# session as PENDING without disturbing anything already decided
# ─────────────────────────────────────────────────────────────────────────────

def test_add_tracks_appends_as_pending():
    session = ReviewSession([mk("a")])
    session.approve("a")

    session.add_tracks([mk("b"), mk("c")])

    assert session.summary() == {
        "pending": 2, "approved": 1, "removed": 0, "regenerate_requested": 0, "total": 3,
    }
    assert {r.track_id for r in session.pending()} == {"b", "c"}
    assert session.approved()[0].track_id == "a"   # untouched


def test_add_tracks_preserves_existing_decisions():
    session = ReviewSession([mk("a"), mk("b"), mk("c")])
    session.approve("a")
    session.remove("b", reason="wrong version")
    session.request_regenerate("c", note="want something slower")

    session.add_tracks([mk("d")])

    by_id = {item.result.track_id: item for item in session.items()}
    assert by_id["a"].status == APPROVED
    assert by_id["b"].status == REMOVED
    assert by_id["b"].note == "wrong version"
    assert by_id["c"].status == REGENERATE_REQUESTED
    assert by_id["c"].note == "want something slower"
    assert by_id["d"].status == PENDING


def test_add_tracks_appends_to_end_of_original_order():
    """New tracks land AFTER existing ones in original order — matters for
    final_uris() ordering and for the review page's "scroll to first new
    track" anchor, both of which rely on items() order."""
    session = ReviewSession([mk("a"), mk("b")])
    session.add_tracks([mk("c"), mk("d")])
    assert [item.result.track_id for item in session.items()] == ["a", "b", "c", "d"]


def test_add_tracks_rejects_track_missing_id():
    session = ReviewSession([mk("a")])
    with pytest.raises(ValueError, match="track_id"):
        session.add_tracks([mk(None)])


def test_add_tracks_rejects_duplicate_against_existing_session_track():
    session = ReviewSession([mk("a")])
    with pytest.raises(ValueError, match="Duplicate"):
        session.add_tracks([mk("a")])


def test_add_tracks_rejects_duplicate_within_the_new_batch_itself():
    session = ReviewSession([mk("a")])
    with pytest.raises(ValueError, match="Duplicate"):
        session.add_tracks([mk("b"), mk("b")])


def test_add_tracks_with_empty_list_does_nothing():
    session = ReviewSession([mk("a")])
    session.add_tracks([])
    assert session.summary()["total"] == 1


def test_newly_added_track_can_then_be_approved_and_reaches_final_uris():
    """End-to-end sanity check for the whole point of this feature: a track
    added via add_tracks() isn't a second-class citizen — it can be
    approved like any other and shows up in final_uris()."""
    session = ReviewSession([mk("a")])
    session.approve("a")
    session.add_tracks([mk("b")])

    session.approve("b")

    assert session.final_uris() == ["spotify:track:a", "spotify:track:b"]


# ─────────────────────────────────────────────────────────────────────────────
# final_uris — order and edge cases
# ─────────────────────────────────────────────────────────────────────────────

def test_final_uris_preserves_original_input_order_not_approval_order():
    session = ReviewSession([mk("a"), mk("b"), mk("c")])
    # approve out of order
    session.approve("c")
    session.approve("a")
    session.approve("b")

    assert session.final_uris() == ["spotify:track:a", "spotify:track:b", "spotify:track:c"]


def test_final_uris_excludes_non_approved():
    session = ReviewSession([mk("a"), mk("b"), mk("c")])
    session.approve("a")
    session.remove("b")
    # c stays pending
    assert session.final_uris() == ["spotify:track:a"]


def test_final_uris_skips_approved_track_with_no_uri():
    session = ReviewSession([mk("a", uri=None), mk("b")])
    session.approve("a")
    session.approve("b")
    assert session.final_uris() == ["spotify:track:b"]


def test_final_uris_empty_when_nothing_approved():
    session = ReviewSession([mk("a"), mk("b")])
    assert session.final_uris() == []


# ─────────────────────────────────────────────────────────────────────────────
# summary
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# items() — full-state accessor (used by session_store.py for serialization)
# ─────────────────────────────────────────────────────────────────────────────

def test_items_returns_every_track_in_original_order_regardless_of_status():
    session = ReviewSession([mk("a"), mk("b"), mk("c")])
    session.approve("b")
    session.remove("a")

    items = session.items()

    assert [i.result.track_id for i in items] == ["a", "b", "c"]
    assert [i.status for i in items] == [REMOVED, APPROVED, PENDING]


def test_items_reflects_notes():
    session = ReviewSession([mk("a")])
    session.remove("a", reason="wrong version")
    [item] = session.items()
    assert item.note == "wrong version"


def test_summary_counts_always_sum_to_total():
    session = ReviewSession([mk(f"t{i}") for i in range(6)])
    session.approve("t0")
    session.approve("t1")
    session.remove("t2")
    session.request_regenerate("t3")
    # t4, t5 stay pending

    summary = session.summary()
    assert summary["total"] == 6
    assert summary["approved"] + summary["removed"] + summary["regenerate_requested"] + summary["pending"] == 6
    assert summary == {"pending": 2, "approved": 2, "removed": 1, "regenerate_requested": 1, "total": 6}


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
