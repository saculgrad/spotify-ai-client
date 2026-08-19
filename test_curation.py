"""
test_curation.py — thorough tests for curation.py's house-rules filters.

Everything here runs fully offline (no Spotify, no Anthropic, no network at
all) — RecentlyUsedLog tests use pytest's tmp_path fixture for real file
I/O against a scratch directory.

Run:
    source .venv/bin/activate
    pytest test_curation.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from curation import (
    DEFAULT_RECENT_WINDOW_DAYS,
    RecentlyUsedLog,
    apply_house_rules,
    cap_artist_diversity,
    dedupe_against_ids,
    dedupe_against_playlist,
    dedupe_against_recent_log,
    filter_explicit,
    filter_resolver_accepted,
)
from resolver import Candidate, MatchResult


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helper
# ─────────────────────────────────────────────────────────────────────────────

def mk(id_=None, title="Song", artist="Artist", explicit=None, accepted=True,
       reason="ok", popularity=50, score=1.0):
    return MatchResult(
        candidate=Candidate(title, artist),
        accepted=accepted,
        reason=reason,
        track_id=id_,
        track_uri=(f"spotify:track:{id_}" if id_ else None),
        track_name=title,
        track_artists=artist,
        popularity=popularity,
        explicit=explicit,
        score=score,
    )


# ─────────────────────────────────────────────────────────────────────────────
# filter_resolver_accepted — defensive guard against bad input
# ─────────────────────────────────────────────────────────────────────────────

def test_filter_resolver_accepted_keeps_only_accepted():
    good = mk("a", accepted=True)
    bad = mk(None, accepted=False, reason="no_search_results")

    outcome = filter_resolver_accepted([good, bad])

    assert outcome.kept == [good]
    assert len(outcome.dropped) == 1
    assert outcome.dropped[0].reason == "not_accepted_by_resolver"


def test_filter_resolver_accepted_empty_input():
    outcome = filter_resolver_accepted([])
    assert outcome.kept == []
    assert outcome.dropped == []


# ─────────────────────────────────────────────────────────────────────────────
# filter_explicit
# ─────────────────────────────────────────────────────────────────────────────

def test_filter_explicit_allow_true_passes_everything_including_explicit():
    tracks = [mk("a", explicit=True), mk("b", explicit=False), mk("c", explicit=None)]
    outcome = filter_explicit(tracks, allow_explicit=True)
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_filter_explicit_allow_false_drops_explicit_true_only_by_default():
    explicit_track = mk("a", explicit=True)
    clean_track = mk("b", explicit=False)
    unknown_track = mk("c", explicit=None)

    outcome = filter_explicit([explicit_track, clean_track, unknown_track], allow_explicit=False)

    assert outcome.kept == [clean_track, unknown_track]
    assert len(outcome.dropped) == 1
    assert outcome.dropped[0].result is explicit_track
    assert outcome.dropped[0].reason == "explicit_track"


def test_filter_explicit_drop_unknown_true_also_drops_none():
    unknown_track = mk("c", explicit=None)
    outcome = filter_explicit([unknown_track], allow_explicit=False, drop_unknown=True)

    assert outcome.kept == []
    assert outcome.dropped[0].reason == "explicit_unknown"


def test_filter_explicit_all_explicit_drops_all():
    tracks = [mk("a", explicit=True), mk("b", explicit=True)]
    outcome = filter_explicit(tracks, allow_explicit=False)
    assert outcome.kept == []
    assert len(outcome.dropped) == 2


def test_filter_explicit_empty_input():
    outcome = filter_explicit([], allow_explicit=False)
    assert outcome.kept == []
    assert outcome.dropped == []


# ─────────────────────────────────────────────────────────────────────────────
# cap_artist_diversity
# ─────────────────────────────────────────────────────────────────────────────

def test_cap_artist_diversity_none_disables_cap():
    tracks = [mk(f"t{i}", artist="Same Artist") for i in range(10)]
    outcome = cap_artist_diversity(tracks, None)
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_cap_artist_diversity_keeps_first_n_in_order():
    tracks = [mk(f"t{i}", title=f"Song {i}", artist="Same Artist") for i in range(5)]
    outcome = cap_artist_diversity(tracks, 2)

    assert [t.track_id for t in outcome.kept] == ["t0", "t1"]
    assert [d.result.track_id for d in outcome.dropped] == ["t2", "t3", "t4"]
    assert all(d.reason.startswith("artist_diversity_cap") for d in outcome.dropped)


def test_cap_artist_diversity_case_insensitive_grouping():
    tracks = [
        mk("t1", artist="Bill Withers"),
        mk("t2", artist="bill withers"),
        mk("t3", artist="BILL WITHERS"),
    ]
    outcome = cap_artist_diversity(tracks, 1)
    assert len(outcome.kept) == 1
    assert len(outcome.dropped) == 2


def test_cap_artist_diversity_ampersand_band_name_groups_as_one_artist():
    # Regression-adjacent to resolver.py's best_artist_sim fix: a band name
    # with '&' must group as ONE artist, not get torn apart by primary_artist().
    tracks = [
        mk("t1", artist="Bob Marley & The Wailers"),
        mk("t2", artist="Bob Marley & The Wailers"),
        mk("t3", artist="Bob Marley & The Wailers"),
    ]
    outcome = cap_artist_diversity(tracks, 2)
    assert len(outcome.kept) == 2
    assert len(outcome.dropped) == 1


def test_cap_artist_diversity_featured_artist_counts_against_primary():
    tracks = [
        mk("t1", artist="Drake"),
        mk("t2", artist="Drake feat. Future"),
        mk("t3", artist="Drake"),
    ]
    outcome = cap_artist_diversity(tracks, 2)
    assert len(outcome.kept) == 2
    assert len(outcome.dropped) == 1


def test_cap_artist_diversity_different_artists_all_within_cap_kept():
    tracks = [mk(f"t{i}", artist=f"Artist {i}") for i in range(5)]
    outcome = cap_artist_diversity(tracks, 1)
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_cap_artist_diversity_cap_larger_than_pool_drops_nothing():
    tracks = [mk(f"t{i}", artist="Same Artist") for i in range(3)]
    outcome = cap_artist_diversity(tracks, 10)
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_cap_artist_diversity_empty_input():
    outcome = cap_artist_diversity([], 2)
    assert outcome.kept == []
    assert outcome.dropped == []


@pytest.mark.parametrize("bad_cap", [0, -1, -100])
def test_cap_artist_diversity_rejects_non_positive_cap(bad_cap):
    with pytest.raises(ValueError):
        cap_artist_diversity([mk("t1")], bad_cap)


def test_cap_artist_diversity_missing_artist_string_does_not_crash():
    # track_artists could theoretically be None/empty on odd input — must not
    # raise, and tracks with no artist info group together under one bucket.
    tracks = [mk("t1", artist=""), mk("t2", artist="")]
    outcome = cap_artist_diversity(tracks, 1)
    assert len(outcome.kept) == 1
    assert len(outcome.dropped) == 1


# ─────────────────────────────────────────────────────────────────────────────
# dedupe_against_ids / dedupe_against_playlist / dedupe_against_recent_log
# ─────────────────────────────────────────────────────────────────────────────

def test_dedupe_against_ids_drops_matches_keeps_others():
    a, b, c = mk("a"), mk("b"), mk("c")
    outcome = dedupe_against_ids([a, b, c], {"a", "c"}, "some_reason")
    assert outcome.kept == [b]
    assert {d.result.track_id for d in outcome.dropped} == {"a", "c"}
    assert all(d.reason == "some_reason" for d in outcome.dropped)


def test_dedupe_against_ids_empty_seen_ids_drops_nothing():
    tracks = [mk("a"), mk("b")]
    outcome = dedupe_against_ids(tracks, set(), "reason")
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_dedupe_against_ids_empty_results():
    outcome = dedupe_against_ids([], {"a", "b"}, "reason")
    assert outcome.kept == []
    assert outcome.dropped == []


def test_dedupe_against_ids_none_track_id_is_kept_not_crashed():
    # Shouldn't happen among resolver-accepted results, but garbage input is
    # a real "might cause problems" scenario — must not raise, and since we
    # can't compare None against seen_ids meaningfully, keep it.
    track = mk(None)
    outcome = dedupe_against_ids([track], {"a", "b"}, "reason")
    assert outcome.kept == [track]
    assert outcome.dropped == []


def test_dedupe_against_ids_duplicate_seen_ids_input_handled_via_set():
    a = mk("a")
    outcome = dedupe_against_ids([a], ["a", "a", "a"], "reason")
    assert outcome.kept == []
    assert len(outcome.dropped) == 1


def test_dedupe_against_playlist_uses_correct_reason():
    a = mk("a")
    outcome = dedupe_against_playlist([a], {"a"})
    assert outcome.dropped[0].reason == "already_in_playlist"


def test_dedupe_against_recent_log_uses_correct_reason():
    a = mk("a")
    outcome = dedupe_against_recent_log([a], {"a"})
    assert outcome.dropped[0].reason == "recently_used"


# ─────────────────────────────────────────────────────────────────────────────
# RecentlyUsedLog — real file I/O against tmp_path
# ─────────────────────────────────────────────────────────────────────────────

def test_recently_used_log_missing_file_loads_empty(tmp_path):
    log = RecentlyUsedLog(tmp_path / "does_not_exist.json")
    assert log.is_recent("anything") is False
    assert log.ids_used_within() == set()


def test_recently_used_log_record_then_is_recent_true(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    log.record(["t1", "t2"])
    assert log.is_recent("t1") is True
    assert log.is_recent("t2") is True
    assert log.is_recent("t3") is False


def test_recently_used_log_outside_window_is_not_recent(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    old = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 5)
    log.record(["t1"], when=old)
    assert log.is_recent("t1") is False
    assert log.is_recent("t1", within_days=DEFAULT_RECENT_WINDOW_DAYS + 10) is True


def test_recently_used_log_record_twice_updates_not_duplicates(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    old = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 5)
    log.record(["t1"], when=old)
    assert log.is_recent("t1") is False

    log.record(["t1"])  # re-record now
    assert log.is_recent("t1") is True
    assert len(log._entries) == 1


def test_recently_used_log_save_and_reload_round_trips(tmp_path):
    path = tmp_path / "log.json"
    log = RecentlyUsedLog(path)
    log.record(["t1", "t2"])
    log.save()

    assert path.exists()

    reloaded = RecentlyUsedLog(path)
    assert reloaded.is_recent("t1") is True
    assert reloaded.is_recent("t2") is True


def test_recently_used_log_corrupted_json_starts_fresh(tmp_path, capsys):
    path = tmp_path / "log.json"
    path.write_text("{not valid json{{{")

    log = RecentlyUsedLog(path)

    assert log.ids_used_within() == set()
    captured = capsys.readouterr()
    assert "corrupted" in captured.err


def test_recently_used_log_unexpected_shape_starts_fresh(tmp_path, capsys):
    path = tmp_path / "log.json"
    path.write_text(json.dumps(["not", "a", "dict"]))

    log = RecentlyUsedLog(path)

    assert log.ids_used_within() == set()
    captured = capsys.readouterr()
    assert "unexpected shape" in captured.err


def test_recently_used_log_ids_used_within_mixed_ages(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    old = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 5)
    log.record(["old_track"], when=old)
    log.record(["fresh_track"])

    assert log.ids_used_within() == {"fresh_track"}


def test_recently_used_log_prune_removes_old_entries(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    very_old = datetime.now(timezone.utc) - timedelta(days=200)
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    log.record(["ancient"], when=very_old)
    log.record(["fresh"], when=recent)

    removed = log.prune(older_than_days=180)

    assert removed == 1
    assert "ancient" not in log._entries
    assert "fresh" in log._entries


def test_recently_used_log_prune_drops_unparseable_entries(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    log._entries["garbage"] = "not-a-timestamp"
    log.record(["fresh"])

    removed = log.prune()

    assert "garbage" not in log._entries
    assert "fresh" in log._entries


def test_recently_used_log_days_zero_boundary(tmp_path):
    log = RecentlyUsedLog(tmp_path / "log.json")
    log.record(["t1"])   # recorded "now"
    # within_days=0 is a same-instant window — any real elapsed time (even
    # microseconds) between record() and is_recent() pushes past the
    # boundary, so this is correctly False in practice, not a fencepost bug.
    assert log.is_recent("t1", within_days=0) is False

    # Comfortably inside vs. comfortably outside a 1-day window (avoid an
    # exact-1-day gap here too — same elapsed-time flakiness as above).
    within_window = datetime.now(timezone.utc) - timedelta(hours=12)
    log.record(["t2"], when=within_window)
    assert log.is_recent("t2", within_days=1) is True

    outside_window = datetime.now(timezone.utc) - timedelta(days=2)
    log.record(["t3"], when=outside_window)
    assert log.is_recent("t3", within_days=1) is False


# ─────────────────────────────────────────────────────────────────────────────
# apply_house_rules — full pipeline composition
# ─────────────────────────────────────────────────────────────────────────────

def test_apply_house_rules_defaults_pass_everything_through():
    tracks = [mk("a"), mk("b"), mk("c")]
    outcome = apply_house_rules(tracks)
    assert outcome.kept == tracks
    assert outcome.dropped == []


def test_apply_house_rules_combines_all_filters_with_correct_reasons():
    already_in_playlist = mk("p1", title="Already There", artist="X")
    recently_used = mk("r1", title="Recent", artist="Y")
    explicit_track = mk("e1", title="Explicit", artist="Z", explicit=True)
    over_cap_1 = mk("d1", title="Song 1", artist="Same Artist")
    over_cap_2 = mk("d2", title="Song 2", artist="Same Artist")
    over_cap_3 = mk("d3", title="Song 3", artist="Same Artist")
    clean_survivor = mk("s1", title="Survivor", artist="Solo Artist")
    not_accepted = mk(None, accepted=False, reason="no_search_results")

    all_results = [
        already_in_playlist, recently_used, explicit_track,
        over_cap_1, over_cap_2, over_cap_3, clean_survivor, not_accepted,
    ]

    outcome = apply_house_rules(
        all_results,
        allow_explicit=False,
        max_per_artist=2,
        existing_playlist_ids={"p1"},
        recent_log_ids={"r1"},
    )

    kept_ids = {t.track_id for t in outcome.kept}
    assert kept_ids == {"d1", "d2", "s1"}

    reasons_by_id = {d.result.track_id: d.reason for d in outcome.dropped}
    assert reasons_by_id["p1"] == "already_in_playlist"
    assert reasons_by_id["r1"] == "recently_used"
    assert reasons_by_id["e1"] == "explicit_track"
    assert reasons_by_id["d3"].startswith("artist_diversity_cap")
    assert reasons_by_id[None] == "not_accepted_by_resolver"

    summary = outcome.summary
    assert summary["total"] == len(all_results)
    assert summary["kept"] == 3
    assert summary["dropped"] == 5
    assert summary["dropped_by_reason"] == {
        "already_in_playlist": 1,
        "recently_used": 1,
        "explicit_track": 1,
        "artist_diversity_cap": 1,
        "not_accepted_by_resolver": 1,
    }


def test_apply_house_rules_empty_input():
    outcome = apply_house_rules([])
    assert outcome.kept == []
    assert outcome.dropped == []
    assert outcome.summary == {"total": 0, "kept": 0, "dropped": 0, "dropped_by_reason": {}}


def test_apply_house_rules_all_tracks_filtered_out():
    tracks = [mk("a", explicit=True), mk("b", explicit=True)]
    outcome = apply_house_rules(tracks, allow_explicit=False)
    assert outcome.kept == []
    assert len(outcome.dropped) == 2


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
