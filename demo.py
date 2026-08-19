"""
demo.py — run the review UI locally with fake data, no credentials needed

This is NOT the real app — it stands in fake Anthropic and Spotify clients
so you can click through the actual generate -> review -> approve/remove/
regenerate -> finalize flow in a real browser before any Spotify Developer
app or Anthropic API key exists. The fake "LLM" ignores whatever you type
and always suggests the same small, deliberately mixed catalog below, so
you can see real behaviors in action:

  - Two Bill Withers songs are suggested — set "Max tracks per artist" to 1
    on the form to watch the artist-diversity cap drop one of them.
  - "WAP" has both an explicit and a clean search result. "Allow explicit
    tracks" is UNCHECKED by default (this venue's priority) — leave it that
    way and the resolver picks the clean edit automatically, rather than
    losing the song entirely. Check the box to see the explicit cut show up
    instead.
  - One suggested song ("Photograph" by "Nickelback") is on the venue
    blocklist (venue_config.blocklist) — it never reaches review regardless
    of any form toggle, since blocklist filtering happens inside
    generator.py before resolution even starts.
  - One suggested song has NO matching entry in the fake Spotify catalog —
    it gets silently dropped by the resolver, same as a real hallucinated
    song would.
  - Pick "Brunch" as the time of day: it's configured as a standing
    playlist (venue_config.standing_playlists) that already contains
    "Redbone" — watch that track get excluded automatically as a dupe,
    and the playlist picker on the review page's finalize form come back
    pre-filled with that playlist instead of "create new."
  - Or pick any playlist from the "grow an existing playlist" dropdown on
    the form directly — that's spotify_client.list_playlists() for real
    (against the fake catalog), not a hardcoded list, and doing so also
    seeds house-taste grounding from that playlist's own tracks (the fake
    LLM ignores the prompt content, but test_app.py asserts the seeded
    content actually reaches it).
  - Finalize doesn't actually write anywhere real — the fake Spotify client
    just records the call and hands back a fake playlist id.

Run:
    source .venv/bin/activate
    python demo.py
Then open http://127.0.0.1:5000 in a browser.

Uses its own session/log/config files (all prefixed demo_) so it never
touches anything a real run would use.
"""

from __future__ import annotations

import json

from app import create_app
from curation import RecentlyUsedLog
from logging_utils import RunLog
from session_store import SessionStore
from venue_config import VenueConfig

DEMO_SESSION_DB = "demo_review_sessions.db"
DEMO_RUN_LOG = "demo_run_log.jsonl"
DEMO_RECENTLY_USED_LOG = "demo_recently_used.json"

STANDING_BRUNCH_PLAYLIST_ID = "demo_playlist_brunch"
HOUSE_TASTE_PLAYLIST_ID = "demo_playlist_house_taste"


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeAnthropicResponse:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [FakeTextBlock(text)]


class FakeAnthropicMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        # Ignores the actual prompt (and therefore the house_taste/blocklist
        # content baked into it) — always returns the same demo catalog. The
        # blocklist filter still runs for real afterward, in generator.py.
        return FakeAnthropicResponse(self._response_text)


DEMO_CANDIDATES = [
    ("Ain't No Sunshine", "Bill Withers"),
    ("Lean On Me", "Bill Withers"),
    ("Let's Stay Together", "Al Green"),
    ("WAP", "Cardi B"),
    ("Redbone", "Childish Gambino"),
    ("Photograph", "Nickelback"),                                      # blocklisted
    ("A Song That Does Not Exist Anywhere", "Totally Fake Artist"),    # will not resolve
]


class FakeAnthropicClient:
    def __init__(self):
        text = json.dumps({
            "wanted_variants": [],
            "candidates": [
                {"title": t, "artist": a, "reason": "demo fixture"} for t, a in DEMO_CANDIDATES
            ],
        })
        self.messages = FakeAnthropicMessages(text)


def _track(id_, name, artists, explicit=False):
    return {
        "id": id_, "uri": f"spotify:track:{id_}", "name": name,
        "artists": [{"name": a} for a in artists],
        "popularity": 60, "available_markets": ["US"], "explicit": explicit,
    }


DEMO_SEARCH_CATALOG = {
    "ain't no sunshine": [_track("demo1", "Ain't No Sunshine", ["Bill Withers"])],
    "lean on me": [_track("demo2", "Lean On Me", ["Bill Withers"])],
    "let's stay together": [_track("demo3", "Let's Stay Together", ["Al Green"])],
    "wap": [
        _track("demo4", "WAP", ["Cardi B"], explicit=True),
        _track("demo4b", "WAP (Clean)", ["Cardi B"], explicit=False),
    ],
    "redbone": [_track("demo5", "Redbone", ["Childish Gambino"])],
    "photograph": [_track("demo6", "Photograph", ["Nickelback"])],
    # deliberately no entry for "A Song That Does Not Exist Anywhere"
}

DEMO_PLAYLISTS = [
    {"id": STANDING_BRUNCH_PLAYLIST_ID, "name": "Brunch Regulars (standing)"},
    {"id": "demo_playlist_dinner", "name": "Dinner Service"},
]

DEMO_PLAYLIST_ITEMS = {
    # Redbone is already on the brunch playlist -> should get deduped out
    # automatically when generating for the "brunch" mode.
    STANDING_BRUNCH_PLAYLIST_ID: [{"track": {"id": "demo5"}}],
    # A little "house taste" sample for the grounding demo — the fake LLM
    # ignores it, but the wiring (fetch -> pass into the prompt) is real.
    HOUSE_TASTE_PLAYLIST_ID: [
        {"track": {"name": "Superstition", "artists": [{"name": "Stevie Wonder"}]}},
        {"track": {"name": "September", "artists": [{"name": "Earth, Wind & Fire"}]}},
    ],
}


class FakeSpotifyClient:
    """Plays both roles the real spotipy.Spotify client would: `.search()`
    for resolver.py, `._get()`/`._post()` for spotify_client.py's writes
    AND reads (list_playlists, get_playlist_track_ids, get_house_taste_sample)."""

    def __init__(self):
        self.post_calls = []

    def search(self, q, type="track", limit=10, market=None):
        q_lower = q.lower()
        for key, tracks in DEMO_SEARCH_CATALOG.items():
            if key in q_lower:
                return {"tracks": {"items": tracks}}
        return {"tracks": {"items": []}}

    def _post(self, url, args=None, payload=None, **kwargs):
        self.post_calls.append({"url": url, "payload": payload})
        if url == "me/playlists":
            return {"id": "demo_playlist_new", "uri": "spotify:playlist:demo_playlist_new"}
        return {"snapshot_id": "demo_snapshot"}

    def _get(self, url, args=None, payload=None, **kwargs):
        if url == "me/playlists":
            return {"items": DEMO_PLAYLISTS}
        if url.startswith("playlists/") and url.endswith("/items"):
            playlist_id = url.split("/")[1]
            return {"items": DEMO_PLAYLIST_ITEMS.get(playlist_id, [])}
        return {"items": []}


if __name__ == "__main__":
    flask_app = create_app(
        anthropic_client=FakeAnthropicClient(),
        spotify_client=FakeSpotifyClient(),
        session_store=SessionStore(DEMO_SESSION_DB),
        run_log=RunLog(DEMO_RUN_LOG),
        recently_used_log=RecentlyUsedLog(DEMO_RECENTLY_USED_LOG),
        venue_config=VenueConfig(
            house_taste_playlist_ids=[HOUSE_TASTE_PLAYLIST_ID],
            blocklist=["Nickelback"],
            standing_playlists={"brunch": STANDING_BRUNCH_PLAYLIST_ID},
        ),
    )
    print("Demo review UI — fake data, nothing real gets written.")
    print("Open http://127.0.0.1:5000 in your browser.")
    print("(Try 'Max tracks per artist' = 1, or pick 'Brunch' as the time of day.)")
    flask_app.run(debug=True, port=5000)
