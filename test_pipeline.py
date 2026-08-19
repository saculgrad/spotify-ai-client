"""
test_pipeline.py — tests for pipeline.py's generate -> resolve -> curate wiring.

Fully offline: a fake Anthropic client (mirrors test_generator.py's) and a
fake Spotify search client (mirrors test_resolver.py's) stand in for both
real services. This file verifies the WIRING is correct, not output
quality (that needs live credentials and human judgment).

Run:
    source .venv/bin/activate
    pytest test_pipeline.py -v
"""

from __future__ import annotations

import json

from generator import GenerationRequest
from pipeline import run_pipeline


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────

class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeAnthropicResponse:
    def __init__(self, stop_reason="end_turn", text=None):
        self.stop_reason = stop_reason
        self.content = [FakeTextBlock(text)] if text is not None else []


class FakeAnthropicMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        return FakeAnthropicResponse(text=self._response_text)


class FakeAnthropicClient:
    def __init__(self, candidates, wanted_variants=None):
        text = json.dumps({
            "wanted_variants": wanted_variants or [],
            "candidates": [
                {"title": t, "artist": a, "reason": "fits the vibe"} for t, a in candidates
            ],
        })
        self.messages = FakeAnthropicMessages(text)


def spotify_track(id_, name, artists, popularity=50, available_markets=("US",), explicit=False):
    return {
        "id": id_, "uri": f"spotify:track:{id_}", "name": name,
        "artists": [{"name": a} for a in artists],
        "popularity": popularity, "available_markets": list(available_markets),
        "explicit": explicit,
    }


class FakeSpotifySearchClient:
    """Maps a candidate title (case-insensitive substring match on the
    query) to canned search results — same shape as test_resolver.py's
    MockSpotify, but keyed so different candidates can resolve differently."""

    def __init__(self, catalog: dict[str, list[dict]]):
        self._catalog = catalog  # {lowercase title substring: [track, ...]}

    def search(self, q, type="track", limit=10, market=None):
        q_lower = q.lower()
        for key, tracks in self._catalog.items():
            if key in q_lower:
                return {"tracks": {"items": tracks}}
        return {"tracks": {"items": []}}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_happy_path_generates_resolves_and_keeps_tracks():
    anthropic = FakeAnthropicClient([
        ("Ain't No Sunshine", "Bill Withers"),
        ("Let's Stay Together", "Al Green"),
    ])
    spotify = FakeSpotifySearchClient({
        "ain't no sunshine": [spotify_track("t1", "Ain't No Sunshine", ["Bill Withers"])],
        "let's stay together": [spotify_track("t2", "Let's Stay Together", ["Al Green"])],
    })
    req = GenerationRequest(vibe_prompt="soul brunch", track_count=2)

    result = run_pipeline(anthropic, spotify, req)

    assert {t.track_id for t in result.kept} == {"t1", "t2"}
    assert result.generated_count == 2
    assert result.accepted_count == 2


def test_pipeline_drops_candidate_resolver_cant_match():
    anthropic = FakeAnthropicClient([
        ("Ain't No Sunshine", "Bill Withers"),
        ("A Song That Does Not Exist", "Nobody At All"),
    ])
    spotify = FakeSpotifySearchClient({
        "ain't no sunshine": [spotify_track("t1", "Ain't No Sunshine", ["Bill Withers"])],
    })
    req = GenerationRequest(vibe_prompt="soul brunch", track_count=2)

    result = run_pipeline(anthropic, spotify, req)

    assert [t.track_id for t in result.kept] == ["t1"]
    assert result.generated_count == 2
    assert result.accepted_count == 1
    assert result.resolver_dropped_by_reason == {"no_search_results": 1}


def test_pipeline_resolver_dropped_by_reason_empty_when_resolver_drops_nothing():
    anthropic = FakeAnthropicClient([("Ain't No Sunshine", "Bill Withers")])
    spotify = FakeSpotifySearchClient({
        "ain't no sunshine": [spotify_track("t1", "Ain't No Sunshine", ["Bill Withers"])],
    })
    req = GenerationRequest(vibe_prompt="soul brunch", track_count=1)

    result = run_pipeline(anthropic, spotify, req)

    assert result.resolver_dropped_by_reason == {}


def test_pipeline_applies_explicit_filter_when_only_explicit_result_exists():
    # WAP has only one search result and it's explicit, so resolver.py's
    # allow_explicit gate rejects it before it ever reaches curation's
    # accepted list — accepted_count reflects that (1, not 2), and
    # curation's own dropped_by_reason has nothing to say about it since it
    # never saw the candidate. See test_pipeline_prefers_clean_over_explicit_
    # when_both_exist below for the "prefer clean" half of this gate.
    anthropic = FakeAnthropicClient([("WAP", "Cardi B"), ("Clean Song", "Some Artist")])
    spotify = FakeSpotifySearchClient({
        "wap": [spotify_track("t1", "WAP", ["Cardi B"], explicit=True)],
        "clean song": [spotify_track("t2", "Clean Song", ["Some Artist"], explicit=False)],
    })
    req = GenerationRequest(vibe_prompt="anything", track_count=2)

    result = run_pipeline(anthropic, spotify, req, allow_explicit=False)

    assert [t.track_id for t in result.kept] == ["t2"]
    assert result.accepted_count == 1
    assert result.house_rules.summary["dropped_by_reason"] == {}


def test_pipeline_prefers_clean_over_explicit_when_both_exist_in_same_results():
    anthropic = FakeAnthropicClient([("Song", "Artist")])
    spotify = FakeSpotifySearchClient({
        "song": [
            spotify_track("explicit1", "Song", ["Artist"], popularity=90, explicit=True),
            spotify_track("clean1", "Song", ["Artist"], popularity=50, explicit=False),
        ],
    })
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    result = run_pipeline(anthropic, spotify, req, allow_explicit=False)

    assert [t.track_id for t in result.kept] == ["clean1"]
    assert result.accepted_count == 1


def test_pipeline_applies_artist_diversity_cap():
    anthropic = FakeAnthropicClient([
        ("Song A", "Same Artist"), ("Song B", "Same Artist"), ("Song C", "Same Artist"),
    ])
    spotify = FakeSpotifySearchClient({
        "song a": [spotify_track("t1", "Song A", ["Same Artist"])],
        "song b": [spotify_track("t2", "Song B", ["Same Artist"])],
        "song c": [spotify_track("t3", "Song C", ["Same Artist"])],
    })
    req = GenerationRequest(vibe_prompt="anything", track_count=3)

    result = run_pipeline(anthropic, spotify, req, max_per_artist=2)

    assert len(result.kept) == 2
    assert result.house_rules.summary["dropped_by_reason"]["artist_diversity_cap"] == 1


def test_pipeline_dedupes_against_existing_playlist_ids():
    anthropic = FakeAnthropicClient([("Song A", "Artist"), ("Song B", "Artist")])
    spotify = FakeSpotifySearchClient({
        "song a": [spotify_track("t1", "Song A", ["Artist"])],
        "song b": [spotify_track("t2", "Song B", ["Artist"])],
    })
    req = GenerationRequest(vibe_prompt="anything", track_count=2)

    result = run_pipeline(anthropic, spotify, req, existing_playlist_ids={"t1"})

    assert [t.track_id for t in result.kept] == ["t2"]


def test_pipeline_wanted_variants_flow_from_generator_into_resolver():
    # A wanted "live" variant should stop resolver.py's live-version penalty
    # from tanking the match — this only works if wanted_variants actually
    # flows from the generator's output into resolve_tracklist's call.
    anthropic = FakeAnthropicClient(
        [("Wonderwall", "Oasis")], wanted_variants=["live"],
    )
    spotify = FakeSpotifySearchClient({
        "wonderwall": [spotify_track("t1", "Wonderwall - Live", ["Oasis"])],
    })
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    result = run_pipeline(anthropic, spotify, req)

    assert [t.track_id for t in result.kept] == ["t1"]


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
