"""
spotify_client.py — OAuth + write scaffold

This is the "one-file patch" module for Spotify auth and playlist writes,
kept separate from resolver.py (which owns search/matching) so that a write-
endpoint churn and a search-shape churn never touch the same diff. Together,
resolver.py + spotify_client.py are the only files that know Spotify's
endpoints, field names, or auth flow — generator.py and curation.py stay
Spotify-agnostic, per CLAUDE.md's isolation principle.

**Live-confirmed 2026-08-13** (create playlist, add items, read items,
round-tripped end to end against a real account) — CLAUDE.md's documented
Feb-2026 endpoint moves were accurate:
  - create playlist = POST /me/playlists          (old /users/{id}/playlists removed) — confirmed
  - add items       = POST /playlists/{id}/items  (was /tracks) — confirmed
  - read items      = GET  /playlists/{id}/items  (was /tracks) — confirmed
  - add-items limit = 100 URIs per call (batch beyond that) — not yet exercised at volume, but documented by Spotify
  - the item field inside each playlist entry actually is named "item" now
    (confirmed live), not "track" — get_playlist_track_ids() still checks
    BOTH defensively in case that changes again; this API has moved before.

**Real bug found and fixed by this live check, not a Spotify-side issue:**
every GET call below originally passed pagination (`limit`/`offset`) via
`payload=`. In spotipy's `_get()`/`_internal_call()`, `payload` becomes the
JSON **request body** — correct for POST/PUT, wrong for a GET, where
pagination has to be actual URL query params. Sending a body on a GET
returned a raw-HTML 400 from Spotify's edge (before it even reached
routing — the tell was an HTML error page instead of Spotify's usual JSON
error shape). Fixed by passing `args={...}` instead, which spotipy folds
into the query string. If you ever see an HTML 400 body from a Spotify API
call again, check this class of mistake first.

Deliberately uses spotipy's low-level `sp._get()` / `sp._post()` rather than
its high-level convenience methods (`user_playlist_create`,
`playlist_add_items`, ...) — those may still target the pre-Feb-2026 paths
depending on the installed spotipy version, and pinning the exact path here
means a future endpoint change is a one-function patch instead of an
upgrade-and-hope.

Setup
-----
    pip install spotipy
    export SPOTIPY_CLIENT_ID=...
    export SPOTIPY_CLIENT_SECRET=...
    export SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback   # NOT localhost

Usage
-----
    sp = get_client()
    playlist = create_playlist(sp, "Sunday Brunch", description="warm 60s-70s soul")
    add_tracks_to_playlist(sp, playlist["id"], result.uris)
    existing_ids = get_playlist_track_ids(sp, playlist["id"])  # feeds curation.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# ─────────────────────────────────────────────────────────────────────────────
# TUNING KNOBS
# ─────────────────────────────────────────────────────────────────────────────

REDIRECT_URI = "http://127.0.0.1:8888/callback"   # Spotify rejects "localhost" outright
DEFAULT_CACHE_PATH = ".spotify_cache"              # local token cache; keep this gitignored

SCOPES = " ".join([
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-private",
])

MAX_ITEMS_PER_ADD_CALL = 100   # Spotify's hard limit on POST/GET /playlists/{id}/items
MAX_PLAYLISTS_PER_LIST_CALL = 50   # GET /me/playlists has a SEPARATE, smaller cap — confirmed
                                    # live 2026-08-13: limit=51 and limit=100 both 400 ("Invalid
                                    # limit"), limit=50 works. Different endpoint, different limit —
                                    # don't reuse MAX_ITEMS_PER_ADD_CALL here again.
MAX_PLAYLIST_PAGES = 200       # sanity cap on pagination (200 * 100 = 20,000 tracks)


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. See spotify_client.py's "
            "module docstring for the full setup steps."
        )
    return value


def build_auth_manager(cache_path: Optional[str] = None):
    """Build a spotipy SpotifyOAuth manager from env vars. Does not touch the
    network — safe to call and inspect without triggering a login."""
    import spotipy.oauth2

    client_id = _require_env("SPOTIPY_CLIENT_ID")
    client_secret = _require_env("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI", REDIRECT_URI)
    if redirect_uri != REDIRECT_URI:
        # Not a hard error — some setups genuinely need a different port —
        # but a bare "localhost" is a documented, guaranteed rejection.
        if "localhost" in redirect_uri:
            raise RuntimeError(
                f"SPOTIPY_REDIRECT_URI={redirect_uri!r} uses 'localhost', which Spotify "
                f"rejects. Register exactly {REDIRECT_URI!r} (or another 127.0.0.1 URI)."
            )

    resolved_cache_path = cache_path or os.environ.get("SPOTIPY_CACHE_PATH", DEFAULT_CACHE_PATH)
    cache_handler = spotipy.oauth2.CacheFileHandler(cache_path=resolved_cache_path)

    return spotipy.oauth2.SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        cache_handler=cache_handler,
    )


def get_client(auth_manager=None):
    """Return an authenticated spotipy.Spotify client. First call opens a
    browser for the one-time OAuth consent screen; spotipy caches and
    silently refreshes the token on subsequent calls via cache_path."""
    import spotipy

    return spotipy.Spotify(auth_manager=auth_manager or build_auth_manager())


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────

def create_playlist(sp, name: str, description: str = "", public: bool = False) -> dict:
    """POST /me/playlists. Returns the created playlist object (has "id", "uri", ...)."""
    return sp._post(
        "me/playlists",
        payload={"name": name, "description": description, "public": public},
    )


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def add_tracks_to_playlist(sp, playlist_id: str, track_uris: Iterable[str]) -> list[dict]:
    """POST /playlists/{id}/items, batched at MAX_ITEMS_PER_ADD_CALL per call.
    Returns one response dict per batch (each typically has "snapshot_id").
    Empty input makes zero API calls."""
    uris = list(track_uris)
    responses = []
    for batch in _chunk(uris, MAX_ITEMS_PER_ADD_CALL):
        responses.append(sp._post(f"playlists/{playlist_id}/items", payload={"uris": batch}))
    return responses


def get_playlist_track_ids(sp, playlist_id: str) -> set[str]:
    """
    GET /playlists/{id}/items, paginated. Returns the set of track IDs
    currently in the playlist — feed this straight into
    curation.dedupe_against_playlist(existing_playlist_ids=...).

    Defensive against the documented-but-unverified field rename
    (entry["item"] vs entry["track"]), a locally-unavailable/removed track
    showing up as a null entry, and a malformed entry missing an "id" —
    none of those should crash the whole fetch.
    """
    ids: set[str] = set()
    offset = 0
    for _ in range(MAX_PLAYLIST_PAGES):
        # NOTE: `args=`, not `payload=` — payload becomes the JSON *request
        # body* in spotipy's _internal_call, which is correct for POST/PUT
        # but wrong for a GET's pagination params. Sending limit/offset as a
        # body on a GET returns a raw-HTML 400 from Spotify's edge before it
        # even reaches routing (discovered against the live API 2026-08-13 —
        # see CLAUDE.md's live-verification notes for the diagnostic).
        page = sp._get(
            f"playlists/{playlist_id}/items",
            args={"limit": MAX_ITEMS_PER_ADD_CALL, "offset": offset},
        )
        entries = (page or {}).get("items", [])
        if not entries:
            break
        for entry in entries:
            track = entry.get("item") or entry.get("track")
            if not track:
                continue
            track_id = track.get("id")
            if track_id:
                ids.add(track_id)
        if len(entries) < MAX_ITEMS_PER_ADD_CALL:
            break
        offset += MAX_ITEMS_PER_ADD_CALL
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Reads — playlist picker + house-taste grounding
# ─────────────────────────────────────────────────────────────────────────────

def list_playlists(sp) -> list[dict]:
    """
    GET /me/playlists, paginated. Returns [{"id": ..., "name": ...}, ...] for
    the review UI's playlist picker — feeds venue_config.standing_playlists
    setup and the "append to an existing playlist" dropdown, so staff pick
    a playlist by name instead of typing a raw Spotify ID.
    """
    playlists: list[dict] = []
    offset = 0
    for _ in range(MAX_PLAYLIST_PAGES):
        page = sp._get("me/playlists", args={"limit": MAX_PLAYLISTS_PER_LIST_CALL, "offset": offset})
        entries = (page or {}).get("items", [])
        if not entries:
            break
        for entry in entries:
            if not entry:
                continue
            pid, name = entry.get("id"), entry.get("name")
            if pid and name:
                playlists.append({"id": pid, "name": name})
        if len(entries) < MAX_PLAYLISTS_PER_LIST_CALL:
            break
        offset += MAX_PLAYLISTS_PER_LIST_CALL
    return playlists


def get_house_taste_sample(sp, playlist_ids: Iterable[str], limit_per_playlist: int = 50,
                            max_total: int = 200) -> list[str]:
    """
    Sample "Artist - Title" lines from the given playlists (venue_config's
    house_taste_playlist_ids) for generator.py's house_taste few-shot
    grounding — CLAUDE.md's "single biggest quality lever." Same defensive
    item/track field handling as get_playlist_track_ids(); a track missing
    a name or artist is skipped rather than producing a malformed line.
    """
    lines: list[str] = []
    for playlist_id in playlist_ids:
        offset = 0
        fetched_this_playlist = 0
        while fetched_this_playlist < limit_per_playlist and len(lines) < max_total:
            page_limit = min(MAX_ITEMS_PER_ADD_CALL, limit_per_playlist - fetched_this_playlist)
            page = sp._get(
                f"playlists/{playlist_id}/items",
                args={"limit": page_limit, "offset": offset},
            )
            entries = (page or {}).get("items", [])
            if not entries:
                break
            for entry in entries:
                track = entry.get("item") or entry.get("track")
                fetched_this_playlist += 1
                if track:
                    name = track.get("name")
                    artist_names = [a.get("name", "") for a in track.get("artists", [])]
                    artists = ", ".join(a for a in artist_names if a)
                    if name and artists:
                        lines.append(f"{artists} - {name}")
                if len(lines) >= max_total or fetched_this_playlist >= limit_per_playlist:
                    break
            if len(entries) < page_limit:
                break
            offset += len(entries)
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Demo — needs live credentials; not runnable in this environment yet
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    sp = get_client()
    playlist = create_playlist(sp, "AI Playlist Tool Test", description="scaffold smoke test")
    print(f"Created playlist: {playlist['id']} ({playlist.get('external_urls', {}).get('spotify')})")

    existing = get_playlist_track_ids(sp, playlist["id"])
    print(f"Existing tracks: {len(existing)}")


if __name__ == "__main__":
    _demo()
