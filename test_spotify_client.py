"""
test_spotify_client.py — tests for spotify_client.py's auth config and write
batching/pagination logic.

None of this touches the network. `build_auth_manager()` only constructs a
spotipy.oauth2.SpotifyOAuth object (pure config, no I/O) so it's safe to
exercise directly; create_playlist/add_tracks_to_playlist/
get_playlist_track_ids are tested against a fake client that mimics
spotipy's low-level `_get`/`_post` signature.

Run:
    source .venv/bin/activate
    pytest test_spotify_client.py -v
"""

from __future__ import annotations

import pytest

from spotify_client import (
    MAX_ITEMS_PER_ADD_CALL,
    MAX_PLAYLISTS_PER_LIST_CALL,
    REDIRECT_URI,
    SCOPES,
    add_tracks_to_playlist,
    build_auth_manager,
    create_playlist,
    get_house_taste_sample,
    get_playlist_track_ids,
    list_playlists,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fake spotipy low-level client
# ─────────────────────────────────────────────────────────────────────────────

class FakeSpotifyWriteClient:
    """Mimics spotipy.Spotify's _get/_post signature: (url, args=None,
    payload=None, **kwargs) -> dict. Records every call; returns queued
    responses in order (falls back to a sane empty default if the queue
    runs out)."""

    def __init__(self, post_responses=None, get_responses=None):
        self.post_calls = []
        self.get_calls = []
        self._post_responses = list(post_responses or [])
        self._get_responses = list(get_responses or [])

    def _post(self, url, args=None, payload=None, **kwargs):
        self.post_calls.append({"url": url, "payload": payload})
        return self._post_responses.pop(0) if self._post_responses else {}

    def _get(self, url, args=None, payload=None, **kwargs):
        # Records both `args` and `payload` distinctly (not merged) so a
        # test can catch the real bug this mock is meant to guard against:
        # pagination params sent as `payload` (a GET request body) instead
        # of `args` (actual URL query params) — see spotify_client.py's
        # module docstring for the live incident that found this the hard
        # way. Production code should always use `args=` for GET calls.
        self.get_calls.append({"url": url, "payload": payload, "args": args})
        return self._get_responses.pop(0) if self._get_responses else {"items": []}


# ─────────────────────────────────────────────────────────────────────────────
# build_auth_manager — pure config, no network
# ─────────────────────────────────────────────────────────────────────────────

def test_build_auth_manager_missing_client_id_raises(monkeypatch):
    monkeypatch.delenv("SPOTIPY_CLIENT_ID", raising=False)
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    with pytest.raises(RuntimeError, match="SPOTIPY_CLIENT_ID"):
        build_auth_manager()


def test_build_auth_manager_missing_client_secret_raises(monkeypatch):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.delenv("SPOTIPY_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SPOTIPY_CLIENT_SECRET"):
        build_auth_manager()


def test_build_auth_manager_rejects_localhost_redirect_uri(monkeypatch):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback")
    with pytest.raises(RuntimeError, match="localhost"):
        build_auth_manager()


def test_build_auth_manager_uses_documented_default_redirect_and_full_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("SPOTIPY_REDIRECT_URI", raising=False)

    auth = build_auth_manager(cache_path=str(tmp_path / "cache"))

    assert auth.redirect_uri == REDIRECT_URI
    assert set(auth.scope.split()) == set(SCOPES.split())


def test_build_auth_manager_accepts_non_localhost_127_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:9999/callback")

    auth = build_auth_manager(cache_path=str(tmp_path / "cache"))

    assert auth.redirect_uri == "http://127.0.0.1:9999/callback"


def test_build_auth_manager_default_cache_path_used_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("SPOTIPY_CACHE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    auth = build_auth_manager()

    assert auth.cache_handler.cache_path == ".spotify_cache"


def test_build_auth_manager_explicit_cache_path_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    custom = str(tmp_path / "custom_cache")

    auth = build_auth_manager(cache_path=custom)

    assert auth.cache_handler.cache_path == custom


# ─────────────────────────────────────────────────────────────────────────────
# create_playlist
# ─────────────────────────────────────────────────────────────────────────────

def test_create_playlist_posts_to_documented_endpoint_with_correct_payload():
    sp = FakeSpotifyWriteClient(post_responses=[{"id": "pl1", "uri": "spotify:playlist:pl1"}])

    result = create_playlist(sp, "Sunday Brunch", description="warm soul", public=True)

    assert result == {"id": "pl1", "uri": "spotify:playlist:pl1"}
    assert len(sp.post_calls) == 1
    call = sp.post_calls[0]
    assert call["url"] == "me/playlists"
    assert call["payload"] == {"name": "Sunday Brunch", "description": "warm soul", "public": True}


def test_create_playlist_defaults_description_and_public():
    sp = FakeSpotifyWriteClient(post_responses=[{"id": "pl1"}])
    create_playlist(sp, "New Playlist")
    assert sp.post_calls[0]["payload"] == {"name": "New Playlist", "description": "", "public": False}


# ─────────────────────────────────────────────────────────────────────────────
# add_tracks_to_playlist — batching boundaries
# ─────────────────────────────────────────────────────────────────────────────

def test_add_tracks_empty_list_makes_no_calls():
    sp = FakeSpotifyWriteClient()
    result = add_tracks_to_playlist(sp, "pl1", [])
    assert result == []
    assert sp.post_calls == []


def test_add_tracks_exactly_at_limit_is_one_call():
    sp = FakeSpotifyWriteClient(post_responses=[{"snapshot_id": "s1"}])
    uris = [f"spotify:track:{i}" for i in range(MAX_ITEMS_PER_ADD_CALL)]

    add_tracks_to_playlist(sp, "pl1", uris)

    assert len(sp.post_calls) == 1
    assert len(sp.post_calls[0]["payload"]["uris"]) == MAX_ITEMS_PER_ADD_CALL


def test_add_tracks_one_over_limit_splits_into_two_calls():
    sp = FakeSpotifyWriteClient(post_responses=[{"snapshot_id": "s1"}, {"snapshot_id": "s2"}])
    uris = [f"spotify:track:{i}" for i in range(MAX_ITEMS_PER_ADD_CALL + 1)]

    responses = add_tracks_to_playlist(sp, "pl1", uris)

    assert len(sp.post_calls) == 2
    assert len(sp.post_calls[0]["payload"]["uris"]) == MAX_ITEMS_PER_ADD_CALL
    assert len(sp.post_calls[1]["payload"]["uris"]) == 1
    assert responses == [{"snapshot_id": "s1"}, {"snapshot_id": "s2"}]


def test_add_tracks_exactly_double_limit_is_two_full_calls():
    sp = FakeSpotifyWriteClient(post_responses=[{}, {}])
    uris = [f"spotify:track:{i}" for i in range(MAX_ITEMS_PER_ADD_CALL * 2)]

    add_tracks_to_playlist(sp, "pl1", uris)

    assert len(sp.post_calls) == 2
    assert all(len(c["payload"]["uris"]) == MAX_ITEMS_PER_ADD_CALL for c in sp.post_calls)


def test_add_tracks_hits_documented_endpoint():
    sp = FakeSpotifyWriteClient(post_responses=[{}])
    add_tracks_to_playlist(sp, "pl123", ["spotify:track:a"])
    assert sp.post_calls[0]["url"] == "playlists/pl123/items"


def test_add_tracks_preserves_order_across_batches():
    sp = FakeSpotifyWriteClient(post_responses=[{}, {}])
    uris = [f"spotify:track:{i}" for i in range(MAX_ITEMS_PER_ADD_CALL + 5)]

    add_tracks_to_playlist(sp, "pl1", uris)

    all_sent = sp.post_calls[0]["payload"]["uris"] + sp.post_calls[1]["payload"]["uris"]
    assert all_sent == uris


# ─────────────────────────────────────────────────────────────────────────────
# get_playlist_track_ids — pagination + defensive field handling
# ─────────────────────────────────────────────────────────────────────────────

def test_get_playlist_track_ids_empty_playlist():
    sp = FakeSpotifyWriteClient(get_responses=[{"items": []}])
    ids = get_playlist_track_ids(sp, "pl1")
    assert ids == set()
    assert len(sp.get_calls) == 1


def test_get_playlist_track_ids_hits_documented_endpoint_with_pagination_params():
    sp = FakeSpotifyWriteClient(get_responses=[{"items": []}])
    get_playlist_track_ids(sp, "pl123")
    call = sp.get_calls[0]
    assert call["url"] == "playlists/pl123/items"
    # Pagination MUST go via `args` (URL query params), not `payload` (a GET
    # request body) — the latter is what caused a live 400 against the real
    # API on 2026-08-13. See spotify_client.py's module docstring.
    assert call["args"] == {"limit": MAX_ITEMS_PER_ADD_CALL, "offset": 0}
    assert call["payload"] is None


def test_get_playlist_track_ids_handles_new_item_field_name():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"item": {"id": "t1"}}, {"item": {"id": "t2"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1", "t2"}


def test_get_playlist_track_ids_handles_old_track_field_name():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"id": "t1"}}, {"track": {"id": "t2"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1", "t2"}


def test_get_playlist_track_ids_skips_null_track_entries():
    # A removed/locally-unavailable track can show up as a null entry.
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": None}, {"track": {"id": "t1"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1"}


def test_get_playlist_track_ids_skips_entries_missing_both_fields():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"something_else": True}, {"track": {"id": "t1"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1"}


def test_get_playlist_track_ids_skips_entries_with_track_but_no_id():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"name": "no id here"}}, {"track": {"id": "t1"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1"}


def test_get_playlist_track_ids_paginates_across_full_pages():
    page1_items = [{"track": {"id": f"t{i}"}} for i in range(MAX_ITEMS_PER_ADD_CALL)]
    page2_items = [{"track": {"id": "t_last"}}]
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": page1_items},
        {"items": page2_items},
    ])

    ids = get_playlist_track_ids(sp, "pl1")

    assert len(ids) == MAX_ITEMS_PER_ADD_CALL + 1
    assert "t_last" in ids
    assert len(sp.get_calls) == 2
    assert sp.get_calls[0]["args"]["offset"] == 0
    assert sp.get_calls[1]["args"]["offset"] == MAX_ITEMS_PER_ADD_CALL


def test_get_playlist_track_ids_stops_on_partial_page_without_extra_call():
    # A page with fewer than the limit means "last page" — must not fetch again.
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"id": "t1"}}, {"track": {"id": "t2"}}]},
    ])

    ids = get_playlist_track_ids(sp, "pl1")

    assert ids == {"t1", "t2"}
    assert len(sp.get_calls) == 1


def test_get_playlist_track_ids_deduplicates_repeated_ids():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"id": "t1"}}, {"track": {"id": "t1"}}]},
    ])
    assert get_playlist_track_ids(sp, "pl1") == {"t1"}


# ─────────────────────────────────────────────────────────────────────────────
# list_playlists — the playlist-picker data source
# ─────────────────────────────────────────────────────────────────────────────

def test_list_playlists_empty():
    sp = FakeSpotifyWriteClient(get_responses=[{"items": []}])
    assert list_playlists(sp) == []


def test_list_playlists_hits_documented_endpoint_with_pagination_params():
    sp = FakeSpotifyWriteClient(get_responses=[{"items": []}])
    list_playlists(sp)
    call = sp.get_calls[0]
    assert call["url"] == "me/playlists"
    # NOT MAX_ITEMS_PER_ADD_CALL — GET /me/playlists has its own, smaller
    # cap (confirmed live 2026-08-13: limit=100 and even limit=51 both 400
    # with "Invalid limit"; limit=50 is the real ceiling for this endpoint).
    assert call["args"] == {"limit": MAX_PLAYLISTS_PER_LIST_CALL, "offset": 0}
    assert call["payload"] is None


def test_list_playlists_returns_id_and_name():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"id": "pl1", "name": "Sunday Brunch"}, {"id": "pl2", "name": "Late Night"}]},
    ])
    assert list_playlists(sp) == [
        {"id": "pl1", "name": "Sunday Brunch"},
        {"id": "pl2", "name": "Late Night"},
    ]


def test_list_playlists_skips_entries_missing_id_or_name():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"id": "pl1"}, {"name": "No ID Here"}, None, {"id": "pl2", "name": "Valid"}]},
    ])
    assert list_playlists(sp) == [{"id": "pl2", "name": "Valid"}]


def test_list_playlists_paginates_across_full_pages():
    page1 = [{"id": f"pl{i}", "name": f"Playlist {i}"} for i in range(MAX_PLAYLISTS_PER_LIST_CALL)]
    page2 = [{"id": "pl_last", "name": "Last One"}]
    sp = FakeSpotifyWriteClient(get_responses=[{"items": page1}, {"items": page2}])

    result = list_playlists(sp)

    assert len(result) == MAX_PLAYLISTS_PER_LIST_CALL + 1
    assert result[-1] == {"id": "pl_last", "name": "Last One"}
    assert len(sp.get_calls) == 2
    assert sp.get_calls[0]["args"]["limit"] == MAX_PLAYLISTS_PER_LIST_CALL
    assert sp.get_calls[1]["args"]["offset"] == MAX_PLAYLISTS_PER_LIST_CALL


# ─────────────────────────────────────────────────────────────────────────────
# get_house_taste_sample — grounding data for generator.py
# ─────────────────────────────────────────────────────────────────────────────

def test_get_house_taste_sample_empty_playlist_ids():
    sp = FakeSpotifyWriteClient()
    assert get_house_taste_sample(sp, []) == []
    assert sp.get_calls == []


def test_get_house_taste_sample_formats_artist_dash_title():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"name": "Ain't No Sunshine", "artists": [{"name": "Bill Withers"}]}}]},
    ])
    assert get_house_taste_sample(sp, ["pl1"]) == ["Bill Withers - Ain't No Sunshine"]


def test_get_house_taste_sample_sends_pagination_via_args_not_payload():
    # Third function with the same pagination-on-a-GET pattern as
    # get_playlist_track_ids/list_playlists — same live bug class applies.
    sp = FakeSpotifyWriteClient(get_responses=[{"items": []}])
    get_house_taste_sample(sp, ["pl1"], limit_per_playlist=10)
    call = sp.get_calls[0]
    assert call["args"] == {"limit": 10, "offset": 0}
    assert call["payload"] is None


def test_get_house_taste_sample_joins_multiple_artists():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"name": "Song", "artists": [{"name": "A"}, {"name": "B"}]}}]},
    ])
    assert get_house_taste_sample(sp, ["pl1"]) == ["A, B - Song"]


def test_get_house_taste_sample_handles_new_item_field_name():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"item": {"name": "Song", "artists": [{"name": "Artist"}]}}]},
    ])
    assert get_house_taste_sample(sp, ["pl1"]) == ["Artist - Song"]


def test_get_house_taste_sample_skips_track_missing_name_or_artists():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [
            {"track": {"name": "", "artists": [{"name": "Artist"}]}},
            {"track": {"name": "No Artist Song", "artists": []}},
            {"track": None},
            {"track": {"name": "Good Song", "artists": [{"name": "Good Artist"}]}},
        ]},
    ])
    assert get_house_taste_sample(sp, ["pl1"]) == ["Good Artist - Good Song"]


def test_get_house_taste_sample_combines_multiple_playlists():
    sp = FakeSpotifyWriteClient(get_responses=[
        {"items": [{"track": {"name": "Song A", "artists": [{"name": "Artist A"}]}}]},
        {"items": [{"track": {"name": "Song B", "artists": [{"name": "Artist B"}]}}]},
    ])
    result = get_house_taste_sample(sp, ["pl1", "pl2"])
    assert result == ["Artist A - Song A", "Artist B - Song B"]


def test_get_house_taste_sample_respects_limit_per_playlist():
    items = [{"track": {"name": f"Song {i}", "artists": [{"name": "Artist"}]}} for i in range(10)]
    sp = FakeSpotifyWriteClient(get_responses=[{"items": items}])

    result = get_house_taste_sample(sp, ["pl1"], limit_per_playlist=3)

    assert len(result) == 3


def test_get_house_taste_sample_respects_max_total_across_playlists():
    items_a = [{"track": {"name": f"A{i}", "artists": [{"name": "Artist"}]}} for i in range(5)]
    items_b = [{"track": {"name": f"B{i}", "artists": [{"name": "Artist"}]}} for i in range(5)]
    sp = FakeSpotifyWriteClient(get_responses=[{"items": items_a}, {"items": items_b}])

    result = get_house_taste_sample(sp, ["pl1", "pl2"], limit_per_playlist=10, max_total=6)

    assert len(result) == 6


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
