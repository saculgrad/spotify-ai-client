"""
test_resolver.py — adversarial unit tests for resolver.py against a mock Spotify
client (no network / no credentials required).

Covers the cases CLAUDE.md says were tested in the earlier session:
  1. karaoke/cover sitting ahead of the real track in search results -> skipped
  2. diacritics folding (accented candidate vs unaccented Spotify data, or vice versa)
  3. identical titles disambiguated by artist
  4. non-existent song -> dropped
  5. not-playable-in-market -> blocked
  6. dedupe across a batch (two candidates resolving to the same track id)

Plus a regression test for best_artist_sim() handling bands with '&' in their name.

Run:
    source .venv/bin/activate
    pytest test_resolver.py -v
"""

from __future__ import annotations

import time

from resolver import (
    ACCEPT_THRESHOLD,
    Candidate,
    best_artist_sim,
    resolve_track,
    resolve_tracklist,
)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Spotify client
# ─────────────────────────────────────────────────────────────────────────────

def mk_track(id, name, artists, popularity=50, available_markets=None, is_playable=None, explicit=None):
    """Build a Spotify-shaped track dict."""
    return {
        "id": id,
        "uri": f"spotify:track:{id}",
        "name": name,
        "artists": [{"name": a} for a in artists],
        "popularity": popularity,
        "available_markets": available_markets,
        "is_playable": is_playable,
        "explicit": explicit,
    }


class MockSpotify:
    """
    Stands in for an authenticated spotipy client. resolve_track() issues up to
    two .search() calls (strict field query, then a loose fallback) — this mock
    returns the same canned `items` for both, since these tests are about the
    resolver's SCORING/GATING logic, not Spotify's query parsing.
    """

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def search(self, q, type="track", limit=10, market=None):
        self.calls += 1
        return {"tracks": {"items": self.items}}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Karaoke/cover ranked first by Spotify -> resolver must not trust result #0
# ─────────────────────────────────────────────────────────────────────────────

def test_skips_karaoke_ahead_of_real_track():
    items = [
        mk_track("karaoke1", "Ain't No Sunshine (Karaoke Version)", ["Bill Withers"],
                  popularity=50, available_markets=["US"]),
        mk_track("real1", "Ain't No Sunshine", ["Bill Withers"],
                  popularity=80, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    cand = Candidate("Ain't No Sunshine", "Bill Withers")

    r = resolve_track(sp, cand, market="US")

    assert r.accepted, r.reason
    assert r.track_id == "real1"


def test_skips_cover_band_even_with_higher_popularity():
    items = [
        mk_track("cover1", "Redemption Song - Tribute to Bob Marley", ["The Wailerz Tribute Band"],
                  popularity=90, available_markets=["US"]),
        mk_track("real1", "Redemption Song", ["Bob Marley & The Wailers"],
                  popularity=60, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    cand = Candidate("Redemption Song", "Bob Marley & The Wailers")

    r = resolve_track(sp, cand, market="US")

    assert r.accepted, r.reason
    assert r.track_id == "real1"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Diacritics folding
# ─────────────────────────────────────────────────────────────────────────────

def test_folds_diacritics_accented_candidate_vs_plain_track_data():
    # Candidate has accents (as an LLM might emit); Spotify's own data often
    # doesn't, or vice versa. normalize() unidecodes both sides.
    items = [
        mk_track("piaf1", "La Vie En Rose", ["Edith Piaf"],
                  popularity=70, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    cand = Candidate("La Vie En Rose", "Édith Piaf")  # Édith Piaf

    r = resolve_track(sp, cand, market="US")

    assert r.accepted, r.reason
    assert r.track_id == "piaf1"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Identical titles disambiguated by artist
# ─────────────────────────────────────────────────────────────────────────────

def test_disambiguates_identical_titles_by_artist():
    items = [
        mk_track("cover_yesterday", "Yesterday", ["Karaoke Legends"],
                  popularity=40, available_markets=["US"]),
        mk_track("beatles_yesterday", "Yesterday", ["The Beatles"],
                  popularity=95, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    cand = Candidate("Yesterday", "The Beatles")

    r = resolve_track(sp, cand, market="US")

    assert r.accepted, r.reason
    assert r.track_id == "beatles_yesterday"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Non-existent song -> dropped
# ─────────────────────────────────────────────────────────────────────────────

def test_drops_nonexistent_song():
    sp = MockSpotify(items=[])
    cand = Candidate("A Song That Does Not Exist", "Nobody At All")

    r = resolve_track(sp, cand, market="US")

    assert not r.accepted
    assert r.reason == "no_search_results"
    assert sp.calls == 2  # strict query, then loose fallback, both empty


# ─────────────────────────────────────────────────────────────────────────────
# 5. Not playable in market -> blocked
# ─────────────────────────────────────────────────────────────────────────────

def test_blocks_not_playable_in_market_via_is_playable_flag():
    items = [
        mk_track("np1", "Golden Hour", ["JVKE"], popularity=80,
                  available_markets=["US"], is_playable=False),
    ]
    sp = MockSpotify(items)
    cand = Candidate("Golden Hour", "JVKE")

    r = resolve_track(sp, cand, market="US")

    assert not r.accepted
    assert r.reason == "not_playable_in_market"


def test_blocks_not_playable_in_market_via_available_markets():
    items = [
        mk_track("np2", "Golden Hour", ["JVKE"], popularity=80,
                  available_markets=["DE", "FR"]),  # no US
    ]
    sp = MockSpotify(items)
    cand = Candidate("Golden Hour", "JVKE")

    r = resolve_track(sp, cand, market="US")

    assert not r.accepted
    assert r.reason == "not_playable_in_market"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Dedupe across a batch
# ─────────────────────────────────────────────────────────────────────────────

def test_dedupes_across_batch():
    # Two different candidates that both resolve to the same underlying track.
    items = [
        mk_track("dup1", "Golden Hour", ["JVKE"], popularity=80, available_markets=["US"]),
    ]

    class RoutingMockSpotify:
        """Returns the shared track for either candidate's query."""
        def search(self, q, type="track", limit=10, market=None):
            return {"tracks": {"items": items}}

    sp = RoutingMockSpotify()
    candidates = [
        Candidate("Golden Hour", "JVKE"),
        Candidate("Golden Hour (Single)", "JVKE"),  # slightly different title, same track
    ]

    result = resolve_tracklist(sp, candidates, market="US")

    assert result["summary"]["accepted"] == 1
    assert result["summary"]["dropped"] == 1
    dup = [r for r in result["dropped"] if r.reason == "duplicate"]
    assert len(dup) == 1


def test_summary_buckets_dropped_reasons_stripping_parenthetical_detail():
    """Added so app.py can log WHY the resolver rejected candidates, not
    just how many — a real support question ("why did more than half my
    songs not get approved?") turned out to be unanswerable without this,
    since the old summary only had an aggregate drop count. Two different
    artist_mismatch candidates have DIFFERENT parenthetical detail (e.g.
    "artist_mismatch (0.1)" vs "artist_mismatch (0.2)") — they must still
    bucket together as one "artist_mismatch" key, not two separate ones,
    matching curation.HouseRulesOutcome.summary's existing convention."""
    real_track = mk_track("real1", "Golden Hour", ["JVKE"], popularity=80, available_markets=["US"])

    class RoutingMockSpotify:
        def search(self, q, type="track", limit=10, market=None):
            if "nonexistent" in q.lower():
                return {"tracks": {"items": []}}
            return {"tracks": {"items": [real_track]}}

    sp = RoutingMockSpotify()
    candidates = [
        Candidate("Golden Hour", "JVKE"),                        # accepted
        Candidate("Golden Hour", "Totally Different Artist"),    # artist_mismatch
        Candidate("Some Other Song", "Some Other Artist"),        # artist_mismatch (different detail)
        Candidate("nonexistent song", "nonexistent artist"),      # no_search_results
    ]

    result = resolve_tracklist(sp, candidates, market="US")

    assert result["summary"]["dropped_by_reason"] == {
        "artist_mismatch": 2,
        "no_search_results": 1,
    }


def test_resolve_tracklist_dropped_by_reason_empty_when_nothing_dropped():
    items = [mk_track("t1", "Song", ["Artist"], popularity=50, available_markets=["US"])]
    sp = MockSpotify(items)

    result = resolve_tracklist(sp, [Candidate("Song", "Artist")], market="US")

    assert result["summary"]["dropped_by_reason"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# Regression: best_artist_sim() must not penalize bands with '&' in their name
# ─────────────────────────────────────────────────────────────────────────────

def test_best_artist_sim_handles_ampersand_band_name():
    sim = best_artist_sim("Bob Marley & The Wailers", ["Bob Marley & The Wailers"])
    assert sim > 0.95


def test_best_artist_sim_handles_featured_artist_noise():
    # candidate says "Artist A feat. Artist B"; track credits both separately.
    sim = best_artist_sim("Artist A feat. Artist B", ["Artist A", "Artist B"])
    assert sim >= ACCEPT_THRESHOLD  # comfortably above the artist gate too


# ─────────────────────────────────────────────────────────────────────────────
# More realistic "conflicting search results" cases — several correct-looking
# tracks in one result set, resolver must pick (or reject) the right one.
# ─────────────────────────────────────────────────────────────────────────────

def test_prefers_studio_over_live_when_live_not_wanted():
    items = [
        mk_track("live1", "Wonderwall - Live at Wembley", ["Oasis"],
                  popularity=70, available_markets=["US"]),
        mk_track("studio1", "Wonderwall", ["Oasis"],
                  popularity=90, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Wonderwall", "Oasis"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "studio1"


def test_accepts_live_when_explicitly_wanted():
    # Only a live take exists in this result set, and the prompt asked for one
    # (wanted_variants carries that intent through from the LLM layer).
    items = [
        mk_track("live1", "Wonderwall - Live", ["Oasis"],
                  popularity=70, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Wonderwall", "Oasis"), market="US",
                       wanted_variants={"live"})

    assert r.accepted, r.reason
    assert r.track_id == "live1"


def test_rejects_sped_up_in_favor_of_original():
    items = [
        mk_track("spedup1", "Say So (Sped Up)", ["Doja Cat"],
                  popularity=95, available_markets=["US"]),  # more popular, still wrong
        mk_track("orig1", "Say So", ["Doja Cat"],
                  popularity=60, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Say So", "Doja Cat"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "orig1"


def test_remix_alone_falls_below_threshold_and_is_dropped():
    # No plain original in the result set at all — only a remix. score_track()
    # puts "Blinding Lights - Remix" at ~0.717, just under ACCEPT_THRESHOLD
    # (0.72), so this should be an honest drop rather than a silent wrong match.
    items = [
        mk_track("remix1", "Blinding Lights - Remix", ["The Weeknd"],
                  popularity=80, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Blinding Lights", "The Weeknd"), market="US")

    assert not r.accepted
    assert r.reason.startswith("below_threshold")


def test_radio_edit_and_album_version_are_not_penalized_as_junk():
    # These are noise-stripped (NOISE_PATTERNS), not variant-penalized
    # (VARIANT_PENALTIES) — unlike "remix" or "live", they should match cleanly.
    items = [
        mk_track("re1", "Blinding Lights (Radio Edit)", ["The Weeknd"],
                  popularity=80, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Blinding Lights", "The Weeknd"), market="US")

    assert r.accepted, r.reason
    assert r.subscores["title_sim"] == 1.0
    assert r.subscores["penalty"] == 0.0


def test_clean_and_explicit_title_tags_are_noise_stripped_not_penalized():
    # A literally-tagged "(Clean)"/"(Explicit)" title shouldn't take a
    # title-similarity hit — which cut you get is decided by the `explicit`
    # boolean field (see the allow_explicit gate tests below), not by
    # string-matching this text.
    for tag, track_id in [("(Clean)", "c1"), ("(Explicit)", "e1"), ("- Clean Version", "c2")]:
        items = [mk_track(track_id, f"Blinding Lights {tag}", ["The Weeknd"],
                           popularity=80, available_markets=["US"])]
        sp = MockSpotify(items)
        r = resolve_track(sp, Candidate("Blinding Lights", "The Weeknd"), market="US")

        assert r.accepted, f"{tag}: {r.reason}"
        assert r.subscores["title_sim"] == 1.0, f"{tag}: title_sim={r.subscores['title_sim']}"


def test_mislabeled_karaoke_track_with_original_artist_in_credits():
    # Realistic and nasty: karaoke catalogs often credit the ORIGINAL artist
    # (for searchability) even though the performer is a studio-band knockoff.
    # artist_sim alone can't catch this — only the variant-word penalty can.
    items = [
        mk_track("karaoke1",
                  "Party In The U.S.A. (Made Famous By Miley Cyrus) [Karaoke Version]",
                  ["Miley Cyrus"], popularity=30, available_markets=["US"]),
        mk_track("real1", "Party in the U.S.A.", ["Miley Cyrus"],
                  popularity=85, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Party in the U.S.A.", "Miley Cyrus"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "real1"


def test_wrong_artist_with_matching_title_is_rejected_outright():
    # A cover/tribute act released under a name that shares no similarity with
    # the real artist at all (not just a variant-word case) — the artist hard
    # gate must catch it even though the title is a perfect match.
    items = [
        mk_track("wrong1", "Yesterday", ["Moonlight Session Singers"],
                  popularity=40, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Yesterday", "The Beatles"), market="US")

    assert not r.accepted
    assert r.reason.startswith("artist_mismatch")


def test_same_artist_different_songs_with_overlapping_words_disambiguated():
    # Adele has both "Hello" and "Rolling in the Deep" — a title that shares no
    # words should not be confused for one that does share the artist.
    items = [
        mk_track("wrong_song", "Rolling in the Deep", ["Adele"],
                  popularity=95, available_markets=["US"]),
        mk_track("right_song", "Hello", ["Adele"],
                  popularity=90, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Hello", "Adele"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "right_song"


def test_same_title_different_artists_of_similar_fame():
    # "Hello" exists as a hit for BOTH Adele and Lionel Richie — a title
    # collision between two genuinely famous, unrelated recordings.
    items = [
        mk_track("richie1", "Hello", ["Lionel Richie"],
                  popularity=80, available_markets=["US"]),
        mk_track("adele1", "Hello", ["Adele"],
                  popularity=98, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Hello", "Lionel Richie"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "richie1"  # must not default to the more popular Adele cut


# ─────────────────────────────────────────────────────────────────────────────
# Known limitations — these tests document CURRENT behavior (not necessarily
# desired behavior). See CLAUDE.md "Known weak spots to watch". If resolver.py
# is changed to fix this, flip the assertions accordingly.
#
# (The other limitation this section used to document — resolve_track() only
# gate-checking the single top-scored result — was fixed; see
# test_falls_back_to_runner_up_when_top_scorer_fails_a_gate() above.)
# ─────────────────────────────────────────────────────────────────────────────

def test_LIMITATION_rerecording_tag_barely_survives_TITLE_MIN_on_short_titles():
    # Good news first: for a SHORT title, "(Taylor's Version)" drags title_sim
    # down to ~0.54 — below TITLE_MIN (0.60) — so the hard gate correctly
    # rejects it rather than silently accepting the wrong pressing.
    items = [
        mk_track("tv_short", "Love Story (Taylor's Version)", ["Taylor Swift"],
                  popularity=90, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Love Story", "Taylor Swift"), market="US")

    assert not r.accepted
    assert r.reason.startswith("title_mismatch")


def test_LIMITATION_rerecording_wins_outright_when_original_is_absent_on_long_titles():
    # But the gate is proportional: on a LONGER title, "(Taylor's Version)" is
    # a smaller fraction of the token set, so title_sim clears both TITLE_MIN
    # and ACCEPT_THRESHOLD comfortably (~0.91 combined). If the original 2010
    # recording isn't in the (search-capped) result set, the resolver
    # confidently accepts the re-recording as if it were an exact match —
    # correct song, wrong pressing, and nothing here would catch it.
    items = [
        mk_track("tv_long", "We Are Never Ever Getting Back Together (Taylor's Version)",
                  ["Taylor Swift"], popularity=90, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(
        sp,
        Candidate("We Are Never Ever Getting Back Together", "Taylor Swift"),
        market="US",
    )

    assert r.accepted
    assert r.track_id == "tv_long"


def test_falls_back_to_runner_up_when_top_scorer_fails_a_gate():
    # resolve_track() scores every result and walks them best-to-worst,
    # accepting the first one that clears every hard gate. Here the top
    # scorer isn't playable in-market, but an equally-good alternative sits
    # right next to it in the same result set — that one should win instead
    # of the whole candidate being dropped.
    items = [
        mk_track("best_unplayable", "Golden Hour", ["JVKE"],
                  popularity=90, available_markets=["DE"]),   # scores 1.0, but no US
        mk_track("alt_playable", "Golden Hour (Radio Edit)", ["JVKE"],
                  popularity=70, available_markets=["US"]),   # also scores 1.0, IS playable
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Golden Hour", "JVKE"), market="US")

    assert r.accepted, r.reason
    assert r.track_id == "alt_playable"


def test_drops_when_no_result_clears_every_gate_reports_top_scorer_reason():
    # If NOTHING in the result set clears every gate, still drop — and report
    # the top-scored attempt's reason (most informative for debugging), not
    # just whichever gate failure was scanned last.
    items = [
        mk_track("unplayable_only", "Golden Hour", ["JVKE"],
                  popularity=90, available_markets=["DE"]),
        mk_track("wrong_artist_only", "Golden Hour", ["Some Cover Band"],
                  popularity=50, available_markets=["US"]),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Golden Hour", "JVKE"), market="US")

    assert not r.accepted
    assert r.reason == "not_playable_in_market"
    assert r.track_id == "unplayable_only"


# ─────────────────────────────────────────────────────────────────────────────
# explicit flag capture — feeds the downstream house-rules explicit filter
# ─────────────────────────────────────────────────────────────────────────────

def test_captures_explicit_true():
    items = [mk_track("e1", "Explicit Song", ["Some Artist"],
                       popularity=50, available_markets=["US"], explicit=True)]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Explicit Song", "Some Artist"), market="US")

    assert r.accepted, r.reason
    assert r.explicit is True


def test_captures_explicit_unknown_when_field_missing():
    # explicit defaults to None (unreported) — mirrors sparse/mocked data.
    items = [mk_track("e2", "Clean Song", ["Some Artist"],
                       popularity=50, available_markets=["US"])]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Clean Song", "Some Artist"), market="US")

    assert r.accepted, r.reason
    assert r.explicit is None


# ─────────────────────────────────────────────────────────────────────────────
# allow_explicit=False — prefer a clean version over an explicit one, don't
# just accept the explicit one and let curation.py throw the whole song away
# ─────────────────────────────────────────────────────────────────────────────

def test_allow_explicit_true_accepts_explicit_track_unaffected():
    # Regression check: default behavior (explicit allowed) must not change.
    items = [mk_track("e1", "Song", ["Artist"], available_markets=["US"], explicit=True)]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Song", "Artist"), market="US", allow_explicit=True)

    assert r.accepted, r.reason
    assert r.explicit is True


def test_allow_explicit_false_prefers_clean_version_over_higher_scoring_explicit_one():
    # The explicit cut is a slightly better title match (exact), the clean
    # cut is a near-exact match — without the gate, resolve_track would
    # commit to the explicit one (it scores higher) and curation.py would
    # then drop the whole candidate, losing the song even though a clean
    # version was sitting right there in the same result set.
    items = [
        mk_track("explicit1", "Song", ["Artist"], available_markets=["US"], explicit=True),
        mk_track("clean1", "Song (Clean)", ["Artist"], available_markets=["US"], explicit=False),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Song", "Artist"), market="US", allow_explicit=False)

    assert r.accepted, r.reason
    assert r.track_id == "clean1"


def test_allow_explicit_false_drops_candidate_when_only_explicit_version_exists():
    items = [mk_track("explicit1", "Song", ["Artist"], available_markets=["US"], explicit=True)]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Song", "Artist"), market="US", allow_explicit=False)

    assert not r.accepted
    assert r.reason == "explicit_track"
    assert r.track_id == "explicit1"   # still reports which one it was, for debugging


def test_allow_explicit_false_does_not_gate_unknown_explicit_status():
    # explicit=None (Spotify didn't report the flag) is never treated as
    # guilty — same "unknown isn't explicit" policy as curation.filter_explicit.
    items = [mk_track("unknown1", "Song", ["Artist"], available_markets=["US"])]   # explicit=None
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Song", "Artist"), market="US", allow_explicit=False)

    assert r.accepted, r.reason
    assert r.explicit is None


def test_allow_explicit_false_falls_back_through_multiple_explicit_results_to_a_clean_one():
    items = [
        mk_track("explicit1", "Song", ["Artist"], popularity=90, available_markets=["US"], explicit=True),
        mk_track("explicit2", "Song - Remix", ["Artist"], popularity=80, available_markets=["US"], explicit=True),
        mk_track("clean1", "Song (Clean)", ["Artist"], popularity=50, available_markets=["US"], explicit=False),
    ]
    sp = MockSpotify(items)
    r = resolve_track(sp, Candidate("Song", "Artist"), market="US", allow_explicit=False)

    assert r.accepted, r.reason
    assert r.track_id == "clean1"


def test_resolve_tracklist_threads_allow_explicit_through_to_each_candidate():
    items = [
        mk_track("explicit1", "Song", ["Artist"], available_markets=["US"], explicit=True),
        mk_track("clean1", "Song (Clean)", ["Artist"], available_markets=["US"], explicit=False),
    ]
    sp = MockSpotify(items)
    result = resolve_tracklist(sp, [Candidate("Song", "Artist")], market="US", allow_explicit=False)

    assert result["summary"]["accepted"] == 1
    assert result["accepted"][0].track_id == "clean1"


# ─────────────────────────────────────────────────────────────────────────────
# resolve_tracklist() concurrency — added when resolve_tracklist() switched from
# a sequential loop to a thread pool (one resolve_track() call per candidate,
# run concurrently). These specifically try to trigger the race conditions that
# a naive parallel implementation would get wrong, not just re-run the old
# sequential-era assertions.
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_tracklist_handles_empty_candidate_list():
    """max_workers=0 would crash ThreadPoolExecutor outright — guard against
    the empty-batch edge case explicitly rather than relying on it never
    happening."""
    sp = MockSpotify(items=[])

    result = resolve_tracklist(sp, [], market="US")

    assert result == {
        "accepted": [],
        "dropped": [],
        "uris": [],
        "summary": {"total": 0, "accepted": 0, "dropped": 0, "drop_rate": 0.0},
    }
    assert sp.calls == 0


def test_dedupe_keeps_first_candidate_by_input_order_even_when_it_resolves_slowest():
    """Two candidates resolve to the same track; the first one BY INPUT ORDER
    is deliberately made the SLOWEST to finish searching. A parallel
    implementation that collected results in completion order (instead of
    preserving input order via ThreadPoolExecutor.map) would have the fast
    second candidate finish first and incorrectly "win" the dedupe. This is
    the concurrency property resolve_tracklist()'s docstring calls
    load-bearing — this test actually forces the race, not just the
    lucky-timing case a plain unit test would usually hit."""
    same_track = [mk_track("dup1", "Golden Hour", ["JVKE"], popularity=80, available_markets=["US"])]

    class SlowFirstMockSpotify:
        def search(self, q, type="track", limit=10, market=None):
            if "Slow" in q:
                time.sleep(0.1)
            return {"tracks": {"items": same_track}}

    sp = SlowFirstMockSpotify()
    candidates = [
        Candidate("Golden Hour Slow", "JVKE"),  # first in input order, slowest to resolve
        Candidate("Golden Hour", "JVKE"),        # second in input order, resolves fast
    ]

    result = resolve_tracklist(sp, candidates, market="US")

    assert result["summary"]["accepted"] == 1
    assert result["summary"]["dropped"] == 1
    assert result["accepted"][0].candidate.title == "Golden Hour Slow"
    dup = result["dropped"][0]
    assert dup.reason == "duplicate"
    assert dup.candidate.title == "Golden Hour"


def test_resolve_tracklist_prewarms_token_exactly_once_per_batch():
    """The token pre-warm exists to avoid N worker threads each independently
    racing to refresh an expired token. Confirm it's actually called, and
    called exactly ONCE up front (on the calling thread, before the pool
    starts) regardless of how many candidates are in the batch — not once
    per candidate, which would defeat the point."""
    items = [mk_track("real1", "Ain't No Sunshine", ["Bill Withers"],
                       popularity=80, available_markets=["US"])]

    class RecordingAuthManager:
        def __init__(self):
            self.calls = 0

        def get_access_token(self, as_dict=False):
            self.calls += 1
            return "fake-token"

    class SpWithAuthManager(MockSpotify):
        def __init__(self, items):
            super().__init__(items)
            self.auth_manager = RecordingAuthManager()

    sp = SpWithAuthManager(items)
    candidates = [Candidate("Ain't No Sunshine", "Bill Withers") for _ in range(3)]

    resolve_tracklist(sp, candidates, market="US")

    assert sp.auth_manager.calls == 1


def test_resolve_tracklist_tolerates_token_prewarm_failure():
    """The pre-warm is best-effort: if it fails for any reason, resolution
    should still proceed (a real auth failure will surface per-candidate via
    _search()'s own error handling instead) rather than crashing the batch."""
    items = [mk_track("real1", "Ain't No Sunshine", ["Bill Withers"],
                       popularity=80, available_markets=["US"])]

    class FailingAuthManager:
        def get_access_token(self, as_dict=False):
            raise RuntimeError("token refresh boom")

    class SpWithAuthManager(MockSpotify):
        def __init__(self, items):
            super().__init__(items)
            self.auth_manager = FailingAuthManager()

    sp = SpWithAuthManager(items)

    result = resolve_tracklist(sp, [Candidate("Ain't No Sunshine", "Bill Withers")], market="US")

    assert result["summary"]["accepted"] == 1


def test_resolve_tracklist_skips_prewarm_when_no_auth_manager():
    """Fake/test clients (and the plain MockSpotify used throughout this
    file) don't have an auth_manager attribute at all — getattr(..., None)
    must skip the pre-warm cleanly rather than raising."""
    sp = MockSpotify(items=[mk_track("real1", "Ain't No Sunshine", ["Bill Withers"],
                                      popularity=80, available_markets=["US"])])
    assert not hasattr(sp, "auth_manager")

    result = resolve_tracklist(sp, [Candidate("Ain't No Sunshine", "Bill Withers")], market="US")

    assert result["summary"]["accepted"] == 1


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
