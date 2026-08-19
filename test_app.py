"""
test_app.py — end-to-end route tests for app.py's Flask review UI.

Uses Flask's test client against a real (tmp_path-backed) SessionStore and
RunLog, plus a combined fake client that plays both roles the real
spotipy.Spotify object would (`.search()` for resolver.py,
`._get()`/`._post()` for spotify_client.py) and a fake Anthropic client
(mirrors test_generator.py's). No network, no real credentials — this
verifies every route's logic (the full generate -> review -> approve/
remove -> finalize -> Spotify-write chain), not the real APIs' behavior.

Run:
    source .venv/bin/activate
    pytest test_app.py -v
"""

from __future__ import annotations

import json

import pytest

from app import create_app
from curation import RecentlyUsedLog
from logging_utils import RunLog
from session_store import SessionStore
from venue_config import VenueConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────

class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeAnthropicResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [FakeTextBlock(text)]


class FakeAnthropicMessages:
    def __init__(self, response_text, stop_reason="end_turn"):
        self._response_text = response_text
        self._stop_reason = stop_reason
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAnthropicResponse(self._response_text, stop_reason=self._stop_reason)


class FakeAnthropicClient:
    def __init__(self, candidates, wanted_variants=None):
        text = json.dumps({
            "wanted_variants": wanted_variants or [],
            "candidates": [
                {"title": t, "artist": a, "reason": "fits the vibe"} for t, a in candidates
            ],
        })
        self.messages = FakeAnthropicMessages(text)


class SequencedFakeAnthropicMessages:
    """Like FakeAnthropicMessages, but returns a DIFFERENT canned response on
    each successive .create() call instead of the same one every time —
    clamps to the last provided response if called more times than that."""

    def __init__(self, response_texts):
        self._response_texts = response_texts
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._response_texts) - 1)
        return FakeAnthropicResponse(self._response_texts[idx])


class SequencedFakeAnthropicClient:
    """Used to test generate_more(): the initial /generate call and the
    follow-up generate_more() call need to return DIFFERENT candidates so a
    test can tell "a genuinely new track got added" apart from "the same
    track came back and got correctly deduped."""

    def __init__(self, candidate_batches, wanted_variants=None):
        texts = [
            json.dumps({
                "wanted_variants": wanted_variants or [],
                "candidates": [{"title": t, "artist": a, "reason": "fits the vibe"} for t, a in batch],
            })
            for batch in candidate_batches
        ]
        self.messages = SequencedFakeAnthropicMessages(texts)


class FakeAlwaysRefusingAnthropicClient:
    """Simulates generator.py's documented real-world failure mode: every
    attempt comes back with stop_reason == "refusal", so after MAX_ATTEMPTS
    (3) generate_candidates() raises RuntimeError("LLM generation failed
    after 3 attempts: ..."). Used to prove /generate handles that for real,
    not a made-up exception type."""

    def __init__(self):
        self.messages = FakeAnthropicMessages("{}", stop_reason="refusal")


class SucceedsOnceThenAlwaysRefusesAnthropicClient:
    """The initial /generate call succeeds normally; every call after that
    (i.e. generate_more()) refuses forever. Used to test generate_more()'s
    own failure handling in isolation, without the initial /generate call
    that has to succeed first to reach a review session at all."""

    def __init__(self, first_candidates, wanted_variants=None):
        self._good_text = json.dumps({
            "wanted_variants": wanted_variants or [],
            "candidates": [{"title": t, "artist": a, "reason": "fits"} for t, a in first_candidates],
        })
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return FakeAnthropicResponse(self._good_text)
        return FakeAnthropicResponse("{}", stop_reason="refusal")


def spotify_track(id_, name, artists, popularity=50, available_markets=("US",), explicit=False):
    return {
        "id": id_, "uri": f"spotify:track:{id_}", "name": name,
        "artists": [{"name": a} for a in artists],
        "popularity": popularity, "available_markets": list(available_markets),
        "explicit": explicit,
    }


class FakeCombinedSpotifyClient:
    """Plays both roles the real spotipy.Spotify client would: `.search()`
    for resolver.py, `._get()`/`._post()` for spotify_client.py's writes
    AND reads (list_playlists, get_playlist_track_ids, get_house_taste_sample)."""

    def __init__(self, search_catalog=None, created_playlist_id="created_pl",
                 existing_playlists=None, playlist_items=None):
        self._catalog = search_catalog or {}
        self.post_calls = []
        self.get_calls = []
        self._created_playlist_id = created_playlist_id
        self._existing_playlists = existing_playlists or []          # [{"id", "name"}, ...]
        self._playlist_items = playlist_items or {}                  # {playlist_id: [{"track": {...}}, ...]}

    def search(self, q, type="track", limit=10, market=None):
        q_lower = q.lower()
        for key, tracks in self._catalog.items():
            if key in q_lower:
                return {"tracks": {"items": tracks}}
        return {"tracks": {"items": []}}

    def _post(self, url, args=None, payload=None, **kwargs):
        self.post_calls.append({"url": url, "payload": payload})
        if url == "me/playlists":
            return {"id": self._created_playlist_id, "uri": f"spotify:playlist:{self._created_playlist_id}"}
        return {"snapshot_id": "snap1"}

    def _get(self, url, args=None, payload=None, **kwargs):
        self.get_calls.append({"url": url, "payload": payload})
        if url == "me/playlists":
            return {"items": self._existing_playlists}
        if url.startswith("playlists/") and url.endswith("/items"):
            playlist_id = url.split("/")[1]
            return {"items": self._playlist_items.get(playlist_id, [])}
        return {"items": []}


class FailingWriteSpotifyClient(FakeCombinedSpotifyClient):
    """Search still works normally (so generate()/review still succeed) but
    every write call fails — simulates a bad manually-typed playlist id, a
    network hiccup, or a rate limit during finalize()."""

    def _post(self, url, args=None, payload=None, **kwargs):
        raise RuntimeError("simulated Spotify write failure")


class FailingReadSpotifyClient(FakeCombinedSpotifyClient):
    """Search and writes still work normally, but every read (_get) call
    fails — simulates a Spotify API hiccup during the best-effort dedupe
    (get_playlist_track_ids) and house-taste (get_house_taste_sample)
    fetches in generate()/generate_more(), both of which are documented as
    "never blocks generation" — this fake is what actually proves that."""

    def _get(self, url, args=None, payload=None, **kwargs):
        raise RuntimeError("simulated Spotify read failure")


# ─────────────────────────────────────────────────────────────────────────────
# App fixture
# ─────────────────────────────────────────────────────────────────────────────

def make_app(tmp_path, candidates=None, search_catalog=None, wanted_variants=None,
             spotify=None, anthropic=None, venue_config=None):
    if anthropic is None:
        anthropic = FakeAnthropicClient(candidates or [], wanted_variants=wanted_variants)
    if spotify is None:
        spotify = FakeCombinedSpotifyClient(search_catalog or {})
    flask_app = create_app(
        anthropic_client=anthropic,
        spotify_client=spotify,
        session_store=SessionStore(tmp_path / "sessions.db"),
        run_log=RunLog(tmp_path / "log.jsonl"),
        # Every piece of local state gets its own tmp_path — a stray
        # recently_used.json/venue_config.json in the real project directory
        # from manual testing must never leak into what a test sees.
        recently_used_log=RecentlyUsedLog(tmp_path / "recently_used.json"),
        venue_config=venue_config if venue_config is not None else VenueConfig(),
    )
    flask_app.testing = True
    return flask_app, spotify


TWO_TRACK_CATALOG = {
    "ain't no sunshine": [spotify_track("t1", "Ain't No Sunshine", ["Bill Withers"])],
    "let's stay together": [spotify_track("t2", "Let's Stay Together", ["Al Green"])],
}
TWO_TRACK_CANDIDATES = [("Ain't No Sunshine", "Bill Withers"), ("Let's Stay Together", "Al Green")]

THREE_TRACK_CATALOG = {
    "ain't no sunshine": [spotify_track("t1", "Ain't No Sunshine", ["Bill Withers"])],
    "let's stay together": [spotify_track("t2", "Let's Stay Together", ["Al Green"])],
    "golden hour": [spotify_track("t3", "Golden Hour", ["JVKE"])],
}
THREE_TRACK_CANDIDATES = [
    ("Ain't No Sunshine", "Bill Withers"),
    ("Let's Stay Together", "Al Green"),
    ("Golden Hour", "JVKE"),
]


# ─────────────────────────────────────────────────────────────────────────────
# GET /
# ─────────────────────────────────────────────────────────────────────────────

def test_index_renders_form_with_modes(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")

    assert resp.status_code == 200
    assert b"vibe_prompt" in resp.data
    assert b"brunch" in resp.data.lower()


def test_pages_render_viewport_meta_tag_for_mobile(tmp_path):
    """Regression test: base.html had no viewport meta tag, so any page
    would render desktop-zoomed (unusable) on a phone — relevant now that
    the team is expected to access this over the local network from their
    own devices, not just the one laptop."""
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")

    assert b'name="viewport"' in resp.data
    assert b"width=device-width" in resp.data


def test_index_renders_avoid_obvious_and_ignore_recently_used_checkboxes(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")

    assert b'name="avoid_obvious"' in resp.data
    assert b'name="ignore_recently_used"' in resp.data


def test_index_allow_explicit_and_ignore_recently_used_checked_by_default(tmp_path):
    """Policy change 2026-08-18, per the owner's explicit instruction that
    this is a permanent default (not just testing convenience) — see
    CLAUDE.md. Real behavior change, worth a dedicated regression test
    rather than just eyeballing the template."""
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")

    assert b'id="allow_explicit" name="allow_explicit" checked' in resp.data
    assert b'id="ignore_recently_used" name="ignore_recently_used" checked' in resp.data


def test_index_and_review_page_label_recently_used_checkbox_as_allow_not_ignore(tmp_path):
    """Regression test for a real UX confusion the owner flagged: the label
    "Ignore recently-used songs" reads as "checking this excludes them,"
    when checking it actually does the opposite (bypasses the exclusion,
    so they're allowed back in). Relabeled to "Allow recently-used songs"
    on both forms — parallel to the "Allow explicit tracks" checkbox right
    above it — without touching the underlying id/name/RunLogEntry field
    (still `ignore_recently_used`), since real data is already logged
    under that name and renaming it would fragment the log's schema for no
    user-facing benefit."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    index_resp = client.get("/")
    assert b"Allow recently-used songs" in index_resp.data
    assert b"Ignore recently-used songs" not in index_resp.data

    session_id = _generate_session(client)
    review_resp = client.get(f"/review/{session_id}")
    assert b"Allow recently-used songs" in review_resp.data
    assert b"Ignore recently-used songs" not in review_resp.data


def test_index_avoid_obvious_still_unchecked_by_default(tmp_path):
    """Only allow_explicit/ignore_recently_used defaults changed — confirm
    avoid_obvious (a separate, unrelated toggle) wasn't accidentally
    flipped along with them."""
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")

    assert b'id="avoid_obvious" name="avoid_obvious" checked' not in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /generate
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_creates_session_and_redirects_to_review(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})

    assert resp.status_code == 302
    assert "/review/" in resp.headers["Location"]


def test_generate_missing_vibe_prompt_redirects_home_with_friendly_message(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/generate", data={"track_count": "2"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    followed = client.post("/generate", data={"track_count": "2"}, follow_redirects=True)
    assert b"describe the vibe" in followed.data.lower()


def test_generate_invalid_track_count_redirects_home_with_friendly_message(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "not a number"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"track count" in resp.data.lower()
    assert b"number" in resp.data.lower()


def test_generate_invalid_max_per_artist_redirects_home_with_friendly_message(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post(
        "/generate",
        data={"vibe_prompt": "x", "track_count": "2", "max_per_artist": "not a number"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"max tracks per artist" in resp.data.lower()


@pytest.mark.parametrize("bad_value", ["0", "-1", "-5"])
def test_generate_non_positive_max_per_artist_redirects_home_with_friendly_message(tmp_path, bad_value):
    """Regression test: curation.cap_artist_diversity() raises ValueError for
    max_per_artist < 1, and the HTML input's min="1" is only a client-side
    hint — posting the form directly used to crash this route with a raw
    500 instead of the friendly-message pattern every other validation
    error here uses."""
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post(
        "/generate",
        data={"vibe_prompt": "x", "track_count": "2", "max_per_artist": bad_value},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"max tracks per artist" in resp.data.lower()
    assert b"1 or more" in resp.data.lower()


@pytest.mark.parametrize("bad_value", ["0", "-1", "101", "10000"])
def test_generate_out_of_range_track_count_redirects_home_with_friendly_message(tmp_path, bad_value):
    """Regression test: track_count had no server-side bounds check at all —
    index.html's min="1" max="100" is client-side only. 0/negative wastes an
    LLM call for nothing useful; a huge value means an expensive, slow
    Claude + Spotify run with no cap."""
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post(
        "/generate",
        data={"vibe_prompt": "x", "track_count": bad_value},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"track count" in resp.data.lower()
    assert b"between 1 and 100" in resp.data.lower()


def test_generate_track_count_at_bounds_succeeds(tmp_path):
    """1 and 100 are valid (inclusive), not off-by-one errors."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "1"})
    assert resp.status_code == 302
    assert "/review/" in resp.headers["Location"]


def test_generate_without_configured_clients_shows_friendly_error(tmp_path):
    flask_app = create_app(
        session_store=SessionStore(tmp_path / "sessions.db"),
        run_log=RunLog(tmp_path / "log.jsonl"),
    )
    flask_app.testing = True
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2"})

    assert resp.status_code == 503
    assert b"set up yet" in resp.data.lower()   # apostrophe in "isn't" is HTML-escaped by Jinja2
    assert b"go back to the start" in resp.data.lower()


def test_generate_llm_failure_redirects_home_with_friendly_message_instead_of_crashing(tmp_path):
    """Regression test: generator.py documents raising RuntimeError after 3
    failed attempts (Claude refusal/truncation/bad JSON) — a real failure
    mode, not hypothetical — and generate() previously had zero handling
    for it, crashing to a raw 500. Uses the real retry-exhaustion code path
    (an always-refusing fake client), not a synthetic exception."""
    flask_app, spotify = make_app(tmp_path, search_catalog=TWO_TRACK_CATALOG,
                                   anthropic=FakeAlwaysRefusingAnthropicClient())
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"went wrong" in resp.data.lower()
    assert b"try again" in resp.data.lower()


def test_generate_llm_failure_redirects_home_not_to_a_review_page(tmp_path):
    """A successful generate() always redirects to /review/<new-id> — landing
    on "/" instead confirms no (half-formed) session was created."""
    flask_app, _ = make_app(tmp_path, search_catalog=TWO_TRACK_CATALOG,
                             anthropic=FakeAlwaysRefusingAnthropicClient())
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2"})

    assert resp.headers["Location"] == "/"


def test_generate_success_flashes_a_summary_message(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    resp = client.post(
        "/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"}, follow_redirects=True,
    )

    assert b"2 tracks to review" in resp.data


def test_generate_writes_a_run_log_entry(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2", "mode": "brunch"})

    entries = flask_app.config["RUN_LOG"].read_all()
    assert len(entries) == 1
    assert entries[0]["vibe_prompt"] == "soul brunch"
    assert entries[0]["mode"] == "brunch"
    assert entries[0]["accepted_count"] == 2


def test_generate_run_log_entry_includes_resolver_drop_reasons(tmp_path):
    """Regression test for a real support question: "why did more than half
    my songs not get approved?" was unanswerable from the log before this,
    because dropped_summary only ever captured curation's reasons — a run
    where the RESOLVER alone rejected most candidates (no curation drops at
    all) showed up as an empty dropped_summary with no explanation. One of
    the two candidates here has no matching entry in the fake catalog, so
    the resolver drops it with "no_search_results" — never reaching
    curation at all."""
    candidates = [("Ain't No Sunshine", "Bill Withers"), ("A Song That Does Not Exist", "Nobody At All")]
    flask_app, _ = make_app(tmp_path, candidates=candidates, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})

    entries = flask_app.config["RUN_LOG"].read_all()
    assert entries[0]["resolver_dropped_summary"] == {"no_search_results": 1}
    assert entries[0]["dropped_summary"] == {}   # curation never saw this drop at all


def test_generate_run_log_records_whether_avoid_obvious_and_ignore_recently_used_were_checked(tmp_path):
    """Regression test for a real "was this even actually testing the fix?"
    moment: the owner reported disappointing results for a run, and it was
    unanswerable whether the "Prefer lesser-known songs"/"Ignore
    recently-used songs" checkboxes were actually on for that run, since the
    log only had the vibe prompt text and counts. Now both toggle states get
    recorded regardless of whether the checkbox was checked or not."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={
        "vibe_prompt": "x", "track_count": "2",
        "avoid_obvious": "on", "ignore_recently_used": "on",
    })

    entries = flask_app.config["RUN_LOG"].read_all()
    assert entries[0]["avoid_obvious"] is True
    assert entries[0]["ignore_recently_used"] is True


def test_generate_run_log_records_false_when_toggles_not_checked(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "x", "track_count": "2"})

    entries = flask_app.config["RUN_LOG"].read_all()
    assert entries[0]["avoid_obvious"] is False
    assert entries[0]["ignore_recently_used"] is False


def test_generate_respects_max_per_artist(tmp_path):
    catalog = {
        "song a": [spotify_track("t1", "Song A", ["Same Artist"])],
        "song b": [spotify_track("t2", "Song B", ["Same Artist"])],
    }
    flask_app, _ = make_app(
        tmp_path, candidates=[("Song A", "Same Artist"), ("Song B", "Same Artist")],
        search_catalog=catalog,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={
        "vibe_prompt": "x", "track_count": "2", "max_per_artist": "1",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    review_resp = client.get(f"/review/{session_id}")
    # Only one of the two same-artist tracks should have made it into the session
    assert review_resp.data.count(b"Same Artist") == 1


# ─────────────────────────────────────────────────────────────────────────────
# GET /review/<id>
# ─────────────────────────────────────────────────────────────────────────────

def test_review_unknown_session_shows_friendly_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/review/does-not-exist")

    assert resp.status_code == 404
    assert b"go back to the start" in resp.data.lower()
    assert b"traceback" not in resp.data.lower()   # not a raw Werkzeug error page


def test_review_page_shows_tracks_and_summary(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    gen_resp = client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})
    session_id = gen_resp.headers["Location"].rsplit("/", 1)[-1]

    resp = client.get(f"/review/{session_id}")

    assert resp.status_code == 200
    # Jinja2 autoescapes the apostrophes ('t -> &#39;t), so check substrings
    # around them rather than the literal raw title strings.
    assert b"Bill Withers" in resp.data
    assert b"No Sunshine" in resp.data
    assert b"Al Green" in resp.data
    assert b"Stay Together" in resp.data
    assert b"pending" in resp.data.lower()


def test_review_page_has_reason_and_note_inputs_for_remove_and_regenerate(tmp_path):
    # Regression check for the missing form fields that made Remove/Regenerate
    # reasons impossible to actually enter from the UI.
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.get(f"/review/{session_id}")

    assert b'name="reason"' in resp.data
    assert b'name="note"' in resp.data
    assert b"doesn't automatically replace the track" in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# approve/remove/regenerate
# ─────────────────────────────────────────────────────────────────────────────

def _generate_session(client):
    resp = client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_approve_updates_status_and_persists(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t1/approve")
    assert resp.status_code == 302

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert session.items()[0].status == "approved"


def test_remove_with_reason_persists_note(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    client.post(f"/review/{session_id}/track/t1/remove", data={"reason": "wrong version"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    item = [i for i in session.items() if i.result.track_id == "t1"][0]
    assert item.status == "removed"
    assert item.note == "wrong version"


def test_regenerate_persists_note(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    client.post(f"/review/{session_id}/track/t2/regenerate", data={"note": "too slow"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    item = [i for i in session.items() if i.result.track_id == "t2"][0]
    assert item.status == "regenerate_requested"
    assert item.note == "too slow"


def test_approve_flashes_confirmation_with_track_name(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t1/approve", follow_redirects=True)

    assert b"Approved" in resp.data
    assert b"No Sunshine" in resp.data   # apostrophe-safe substring, see earlier note


def test_remove_flashes_confirmation_with_track_name(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t2/remove", follow_redirects=True)

    assert b"Removed" in resp.data
    assert b"Stay Together" in resp.data


def test_regenerate_flashes_confirmation_with_track_name(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t1/regenerate", follow_redirects=True)

    assert b"Flagged" in resp.data
    assert b"regeneration" in resp.data


def test_approve_unknown_track_id_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/nonexistent/approve")

    assert resp.status_code == 404


def test_approve_unknown_session_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/review/nonexistent/track/t1/approve")

    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Scroll-position anchor on the post-action redirect — added because a plain
# redirect to /review/<id> reloads the WHOLE page and lands at the top every
# time, which is disorienting when working through a long list one row at a
# time. The fix stays entirely server-side (no JS): redirect to a URL
# fragment naming the just-acted-on row's `id`, and the browser scrolls that
# row to the top of the screen natively — so staff can immediately see the
# status change took effect, without losing their place in the list.
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_redirects_to_its_own_track_anchor(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t2/approve")

    assert resp.headers["Location"] == f"/review/{session_id}#track-t2"


def test_remove_redirects_to_its_own_track_anchor(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t2/remove")

    assert resp.headers["Location"] == f"/review/{session_id}#track-t2"


def test_regenerate_redirects_to_its_own_track_anchor(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/track/t2/regenerate")

    assert resp.headers["Location"] == f"/review/{session_id}#track-t2"


def test_review_page_renders_row_id_for_each_track(tmp_path):
    """The anchors above only work if each row actually carries the matching
    id= the redirect points at."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.get(f"/review/{session_id}")

    assert b'id="track-t1"' in resp.data
    assert b'id="track-t2"' in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /review/<id>/approve_all
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_all_approves_every_pending_track(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/approve_all", follow_redirects=True)

    assert b"Approved 2 remaining" in resp.data
    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert all(i.status == "approved" for i in session.items())


def test_approve_all_leaves_already_removed_tracks_alone(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/remove")

    client.post(f"/review/{session_id}/approve_all")

    session = flask_app.config["SESSION_STORE"].load(session_id)
    statuses = {i.result.track_id: i.status for i in session.items()}
    assert statuses == {"t1": "removed", "t2": "approved"}


def test_approve_all_unknown_session_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/review/nonexistent/approve_all")

    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POST /review/<id>/generate_more — mid-review follow-up generation, informed
# by what's already been approved/removed/regenerate-requested in this
# session. New candidates join the session as pending; nothing already
# decided is touched.
# ─────────────────────────────────────────────────────────────────────────────

FOUR_TRACK_CATALOG = dict(THREE_TRACK_CATALOG, **{
    "new song": [spotify_track("t4", "New Song", ["New Artist"])],
})


def _generate_three_track_session(client):
    resp = client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "3"})
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_generate_more_adds_new_track_as_pending(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "a few more", "track_count": "1"})
    assert resp.status_code == 302

    session = flask_app.config["SESSION_STORE"].load(session_id)
    ids_by_status = {item.result.track_id: item.status for item in session.items()}
    assert ids_by_status == {"t1": "pending", "t2": "pending", "t3": "pending", "t4": "pending"}


def test_generate_more_preserves_existing_approved_and_removed_state(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")
    client.post(f"/review/{session_id}/track/t2/remove", data={"reason": "wrong version"})

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "a few more", "track_count": "1"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    by_id = {item.result.track_id: item for item in session.items()}
    assert by_id["t1"].status == "approved"
    assert by_id["t2"].status == "removed"
    assert by_id["t2"].note == "wrong version"
    assert by_id["t3"].status == "pending"
    assert by_id["t4"].status == "pending"


def test_generate_more_missing_prompt_shows_friendly_error(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more", data={"track_count": "1"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"describe what you want" in resp.data.lower()


def test_generate_more_invalid_track_count_shows_friendly_error(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "more", "track_count": "not a number"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"track count" in resp.data.lower()
    assert b"number" in resp.data.lower()


def test_generate_more_out_of_range_track_count_shows_friendly_error(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "more", "track_count": "101"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"between 1 and 100" in resp.data.lower()


def test_generate_more_dedupes_against_existing_session_tracks(tmp_path):
    """The second round re-suggests one track already in the session (t1)
    alongside one genuinely new one (t4) — only the new one should be added."""
    anthropic = SequencedFakeAnthropicClient([
        THREE_TRACK_CANDIDATES,
        [("Ain't No Sunshine", "Bill Withers"), ("New Song", "New Artist")],
    ])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "2"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    ids = [item.result.track_id for item in session.items()]
    assert ids.count("t1") == 1   # not duplicated
    assert "t4" in ids


def test_generate_more_dedupes_against_target_playlist(tmp_path):
    # First round suggests t2 (NOT on the target playlist, so it survives
    # into the session normally). Second round re-suggests t1 — which is
    # already on the target playlist, even though it was never part of
    # THIS review session — plus the genuinely new t4.
    anthropic = SequencedFakeAnthropicClient([
        [("Let's Stay Together", "Al Green")],
        [("Ain't No Sunshine", "Bill Withers"), ("New Song", "New Artist")],
    ])
    spotify = FakeCombinedSpotifyClient(
        FOUR_TRACK_CATALOG,
        playlist_items={"pl_existing": [{"track": {"id": "t1"}}]},
    )
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, spotify=spotify)
    client = flask_app.test_client()
    resp = client.post("/generate", data={
        "vibe_prompt": "soul brunch", "track_count": "1", "playlist_id": "pl_existing",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "2"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    ids = [item.result.track_id for item in session.items()]
    assert "t2" in ids   # first round's track, unaffected
    assert "t1" not in ids   # already on the target playlist, excluded even though not yet in this session
    assert "t4" in ids


def test_generate_more_dedupe_and_house_taste_fetch_failures_do_not_block_generation(tmp_path):
    """Same regression coverage as the main /generate route's version above
    — generate_more() has an identical pair of best-effort try/except
    blocks around the same two calls (get_playlist_track_ids/
    get_house_taste_sample), exercised here since a target playlist is set
    for this session."""
    anthropic = SequencedFakeAnthropicClient([
        [("Let's Stay Together", "Al Green")], [("New Song", "New Artist")],
    ])
    spotify = FailingReadSpotifyClient(FOUR_TRACK_CATALOG)
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, spotify=spotify)
    client = flask_app.test_client()
    resp = client.post("/generate", data={
        "vibe_prompt": "soul brunch", "track_count": "1", "playlist_id": "pl_existing",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    resp2 = client.post(f"/review/{session_id}/generate_more",
                         data={"additional_prompt": "more", "track_count": "1"})

    assert resp2.status_code == 302
    assert f"/review/{session_id}" in resp2.headers["Location"]


def test_generate_more_includes_approved_tracks_in_house_taste_grounding(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more like this", "track_count": "1"})

    second_call_prompt = anthropic.messages.calls[1]["messages"][0]["content"]
    assert "Bill Withers - Ain't No Sunshine" in second_call_prompt


def test_generate_more_includes_removed_track_with_reason_in_prompt(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)
    client.post(f"/review/{session_id}/track/t2/remove", data={"reason": "too slow"})

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "1"})

    second_call_prompt = anthropic.messages.calls[1]["messages"][0]["content"]
    assert "explicitly rejected them" in second_call_prompt
    assert "Al Green - Let's Stay Together (reason: too slow)" in second_call_prompt


def test_generate_more_includes_regenerate_requested_track_with_note_in_prompt(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)
    client.post(f"/review/{session_id}/track/t3/regenerate", data={"note": "want something slower"})

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "1"})

    second_call_prompt = anthropic.messages.calls[1]["messages"][0]["content"]
    assert "Golden Hour" in second_call_prompt
    assert "wanted instead: want something slower" in second_call_prompt


def test_generate_more_respects_allow_explicit_unchecked(tmp_path):
    catalog = dict(THREE_TRACK_CATALOG, **{
        "clean or explicit song": [spotify_track("t4", "Clean or Explicit Song", ["New Artist"], explicit=True)],
    })
    anthropic = SequencedFakeAnthropicClient([
        THREE_TRACK_CANDIDATES, [("Clean or Explicit Song", "New Artist")],
    ])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=catalog)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "1"})   # allow_explicit omitted = unchecked

    session = flask_app.config["SESSION_STORE"].load(session_id)
    ids = [item.result.track_id for item in session.items()]
    assert "t4" not in ids   # the only result was explicit, and allow_explicit was off


def test_generate_more_avoid_obvious_reaches_the_second_llm_call(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "1", "avoid_obvious": "on"})

    second_call_prompt = anthropic.messages.calls[1]["messages"][0]["content"]
    assert "avoid the most obvious" in second_call_prompt.lower()


def test_generate_more_ignore_recently_used_bypasses_the_exclusion(tmp_path):
    recent_log = RecentlyUsedLog(tmp_path / "recent.json")
    recent_log.record(["t4"])
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    flask_app.config["RECENTLY_USED_LOG"] = recent_log
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "more", "track_count": "1", "ignore_recently_used": "on"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    ids = [item.result.track_id for item in session.items()]
    assert "t4" in ids


def test_generate_more_unknown_session_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/review/nonexistent/generate_more", data={"additional_prompt": "more"})

    assert resp.status_code == 404


def test_generate_more_without_configured_clients_shows_friendly_error(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    session_id = _generate_three_track_session(flask_app.test_client())
    # simulate a not-yet-configured server for the SECOND call only
    flask_app.config["ANTHROPIC_CLIENT"] = None
    flask_app.config["SPOTIFY_CLIENT"] = None
    client = flask_app.test_client()

    resp = client.post(f"/review/{session_id}/generate_more", data={"additional_prompt": "more"})

    assert resp.status_code == 503


def test_generate_more_llm_failure_shows_friendly_message_instead_of_crashing(tmp_path):
    anthropic = SucceedsOnceThenAlwaysRefusesAnthropicClient(THREE_TRACK_CANDIDATES)
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "more", "track_count": "1"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"went wrong" in resp.data.lower()


def test_generate_more_no_new_survivors_shows_friendly_message(tmp_path):
    """The follow-up round only re-suggests a track already in the session
    — everything gets deduped, nothing new survives."""
    anthropic = SequencedFakeAnthropicClient([
        THREE_TRACK_CANDIDATES, [("Ain't No Sunshine", "Bill Withers")],
    ])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "more", "track_count": "1"},
                        follow_redirects=True)

    assert resp.status_code == 200
    assert b"try a different prompt" in resp.data.lower()
    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert session.summary()["total"] == 3   # unchanged


def test_generate_more_writes_a_run_log_entry(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    client.post(f"/review/{session_id}/generate_more",
                data={"additional_prompt": "a few more upbeat ones", "track_count": "1"})

    entries = flask_app.config["RUN_LOG"].read_all()
    more_entries = [e for e in entries if e["action"] == "generate_more"]
    assert len(more_entries) == 1
    assert more_entries[0]["vibe_prompt"] == "a few more upbeat ones"
    assert more_entries[0]["final_track_ids"] == ["t4"]


def test_generate_more_redirects_anchored_to_first_new_track(tmp_path):
    anthropic = SequencedFakeAnthropicClient([THREE_TRACK_CANDIDATES, [("New Song", "New Artist")]])
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=FOUR_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.post(f"/review/{session_id}/generate_more",
                        data={"additional_prompt": "more", "track_count": "1"})

    assert resp.headers["Location"] == f"/review/{session_id}#track-t4"


def test_review_page_renders_generate_more_form(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=THREE_TRACK_CANDIDATES, search_catalog=THREE_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_three_track_session(client)

    resp = client.get(f"/review/{session_id}")

    assert f'/review/{session_id}/generate_more'.encode() in resp.data
    assert b'name="additional_prompt"' in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# Punch-list wiring: house_taste / blocklist reaching the LLM call
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_passes_venue_house_taste_and_blocklist_to_the_llm(tmp_path):
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={
            "pl_house": [{"track": {"name": "Ain't No Sunshine", "artists": [{"name": "Bill Withers"}]}}],
        },
    )
    venue_config = VenueConfig(house_taste_playlist_ids=["pl_house"], blocklist=["Nickelback"])
    flask_app, _ = make_app(tmp_path, spotify=spotify, anthropic=anthropic, venue_config=venue_config)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "Bill Withers - Ain't No Sunshine" in sent_prompt
    assert "Nickelback" in sent_prompt


def test_generate_prefer_less_popular_checkbox_reaches_the_llm_prompt(tmp_path):
    """"Prefer lesser-known songs" wires straight into generator.py's
    already-existing (but previously never surfaced in the UI)
    avoid_obvious flag — added after investigating why "grow an existing
    playlist" runs asking for deep cuts had much lower approval rates than
    broad/popular-vibe runs."""
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={
        "vibe_prompt": "soul brunch", "track_count": "2", "avoid_obvious": "on",
    })

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "avoid the most obvious" in sent_prompt.lower()


def test_generate_without_prefer_less_popular_omits_the_avoid_obvious_hint(tmp_path):
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    flask_app, _ = make_app(tmp_path, anthropic=anthropic, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "soul brunch", "track_count": "2"})

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "avoid the most obvious" not in sent_prompt.lower()


def test_generate_uses_the_target_playlist_itself_as_a_seed_for_grounding(tmp_path):
    # "Grow an existing playlist" is the seed-playlist feature: pick a
    # playlist to append to, and its own tracks automatically become house
    # taste grounding too — no separate venue_config entry required.
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={
            "pl_seed": [{"track": {"name": "Superstition", "artists": [{"name": "Stevie Wonder"}]}}],
        },
    )
    flask_app, _ = make_app(tmp_path, spotify=spotify, anthropic=anthropic)
    client = flask_app.test_client()

    client.post("/generate", data={
        "vibe_prompt": "more like this", "track_count": "2", "playlist_id": "pl_seed",
    })

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "Stevie Wonder - Superstition" in sent_prompt


def test_generate_combines_venue_house_taste_with_the_seed_playlist(tmp_path):
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={
            "pl_venue_wide": [{"track": {"name": "September", "artists": [{"name": "Earth, Wind & Fire"}]}}],
            "pl_seed": [{"track": {"name": "Superstition", "artists": [{"name": "Stevie Wonder"}]}}],
        },
    )
    venue_config = VenueConfig(house_taste_playlist_ids=["pl_venue_wide"])
    flask_app, _ = make_app(tmp_path, spotify=spotify, anthropic=anthropic, venue_config=venue_config)
    client = flask_app.test_client()

    client.post("/generate", data={
        "vibe_prompt": "more like this", "track_count": "2", "playlist_id": "pl_seed",
    })

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "Earth, Wind & Fire - September" in sent_prompt
    assert "Stevie Wonder - Superstition" in sent_prompt


def test_generate_without_a_target_playlist_only_uses_venue_wide_house_taste(tmp_path):
    anthropic = FakeAnthropicClient(TWO_TRACK_CANDIDATES)
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={
            "pl_venue_wide": [{"track": {"name": "September", "artists": [{"name": "Earth, Wind & Fire"}]}}],
        },
    )
    venue_config = VenueConfig(house_taste_playlist_ids=["pl_venue_wide"])
    flask_app, _ = make_app(tmp_path, spotify=spotify, anthropic=anthropic, venue_config=venue_config)
    client = flask_app.test_client()

    client.post("/generate", data={"vibe_prompt": "anything", "track_count": "2"})   # no playlist_id, no mode

    sent_prompt = anthropic.messages.calls[0]["messages"][0]["content"]
    assert "Earth, Wind & Fire - September" in sent_prompt


# ─────────────────────────────────────────────────────────────────────────────
# Punch-list wiring: dedupe against an existing/standing playlist
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_with_explicit_playlist_id_dedupes_against_its_current_tracks(tmp_path):
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={"pl_existing": [{"track": {"id": "t1"}}]},   # t1 already in the target
    )
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG, spotify=spotify,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={
        "vibe_prompt": "soul brunch", "track_count": "2", "playlist_id": "pl_existing",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert [i.result.track_id for i in session.items()] == ["t2"]   # t1 deduped out


def test_generate_dedupe_and_house_taste_fetch_failures_do_not_block_generation(tmp_path):
    """Regression test found via a coverage check: get_playlist_track_ids()/
    get_house_taste_sample() failing (network hiccup, rate limit, etc.) must
    not crash /generate — both are documented as best-effort, "never blocks
    generation," but that was previously only a comment, not a tested
    behavior. Picking a target playlist triggers BOTH calls (dedupe fetch
    for the append target, house-taste fetch since it also seeds grounding
    from that same playlist)."""
    spotify = FailingReadSpotifyClient(TWO_TRACK_CATALOG)
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG, spotify=spotify,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={
        "vibe_prompt": "soul brunch", "track_count": "2", "playlist_id": "pl_existing",
    })

    assert resp.status_code == 302
    assert "/review/" in resp.headers["Location"]


def test_generate_with_mode_uses_standing_playlist_from_venue_config(tmp_path):
    spotify = FakeCombinedSpotifyClient(
        TWO_TRACK_CATALOG,
        playlist_items={"pl_brunch_standing": [{"track": {"id": "t2"}}]},
    )
    venue_config = VenueConfig(standing_playlists={"brunch": "pl_brunch_standing"})
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG,
        spotify=spotify, venue_config=venue_config,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2", "mode": "brunch"})
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert [i.result.track_id for i in session.items()] == ["t1"]   # t2 deduped out

    target = flask_app.config["SESSION_STORE"].get_target(session_id)
    assert target == {"action": "append", "playlist_id": "pl_brunch_standing", "playlist_name": None}


def test_generate_explicit_playlist_id_wins_over_mode_standing_playlist(tmp_path):
    # resolve_target_playlist()'s documented precedence: explicit choice beats mode.
    venue_config = VenueConfig(standing_playlists={"brunch": "pl_from_mode"})
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG, venue_config=venue_config,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={
        "vibe_prompt": "x", "track_count": "2", "mode": "brunch", "playlist_id": "pl_explicit",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    target = flask_app.config["SESSION_STORE"].get_target(session_id)
    assert target["playlist_id"] == "pl_explicit"


def test_review_page_finalize_form_prefilled_from_resolved_target(tmp_path):
    venue_config = VenueConfig(standing_playlists={"brunch": "pl_brunch_standing"})
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG, venue_config=venue_config,
    )
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2", "mode": "brunch"})
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    review_resp = client.get(f"/review/{session_id}")
    assert b'value="pl_brunch_standing"' in review_resp.data


# ─────────────────────────────────────────────────────────────────────────────
# Punch-list wiring: RecentlyUsedLog
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_excludes_recently_used_tracks(tmp_path):
    recent_log = RecentlyUsedLog(tmp_path / "recent.json")
    recent_log.record(["t1"])
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG,
    )
    flask_app.config["RECENTLY_USED_LOG"] = recent_log
    client = flask_app.test_client()

    resp = client.post("/generate", data={"vibe_prompt": "x", "track_count": "2"})
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert [i.result.track_id for i in session.items()] == ["t2"]


def test_generate_ignore_recently_used_bypasses_the_exclusion(tmp_path):
    """The 'Ignore recently-used songs' checkbox exists specifically for
    active testing against the same playlist within days, where the normal
    30-day window would otherwise exclude a track added in an earlier test
    run just hours before."""
    recent_log = RecentlyUsedLog(tmp_path / "recent.json")
    recent_log.record(["t1"])
    flask_app, _ = make_app(
        tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG,
    )
    flask_app.config["RECENTLY_USED_LOG"] = recent_log
    client = flask_app.test_client()

    resp = client.post("/generate", data={
        "vibe_prompt": "x", "track_count": "2", "ignore_recently_used": "on",
    })
    session_id = resp.headers["Location"].rsplit("/", 1)[-1]

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert {i.result.track_id for i in session.items()} == {"t1", "t2"}


def test_finalize_records_written_tracks_in_recently_used_log(tmp_path):
    recent_log = RecentlyUsedLog(tmp_path / "recent.json")
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    flask_app.config["RECENTLY_USED_LOG"] = recent_log
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    assert recent_log.is_recent("t1") is True
    # reload from disk to confirm .save() actually happened, not just in-memory
    reloaded = RecentlyUsedLog(tmp_path / "recent.json")
    assert reloaded.is_recent("t1") is True


# ─────────────────────────────────────────────────────────────────────────────
# Punch-list wiring: index() — playlist picker + day-part preselect
# ─────────────────────────────────────────────────────────────────────────────

def test_index_lists_existing_playlists_in_the_picker(tmp_path):
    spotify = FakeCombinedSpotifyClient(
        existing_playlists=[{"id": "pl1", "name": "Sunday Brunch"}, {"id": "pl2", "name": "Late Night"}],
    )
    flask_app, _ = make_app(tmp_path, spotify=spotify)
    client = flask_app.test_client()

    resp = client.get("/")

    assert b"Sunday Brunch" in resp.data
    assert b"Late Night" in resp.data
    assert b'value="pl1"' in resp.data


def test_index_playlist_picker_empty_when_client_unconfigured(tmp_path):
    flask_app = create_app(
        session_store=SessionStore(tmp_path / "sessions.db"),
        run_log=RunLog(tmp_path / "log.jsonl"),
        recently_used_log=RecentlyUsedLog(tmp_path / "recent.json"),
        venue_config=VenueConfig(),
    )
    flask_app.testing = True
    client = flask_app.test_client()

    resp = client.get("/")   # must render even with no Spotify client configured

    assert resp.status_code == 200


def test_index_preselects_the_current_day_part_mode(tmp_path):
    from modes import current_day_part

    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.get("/")
    body = resp.data.decode()

    expected_mode = current_day_part()
    if expected_mode is None:
        assert "selected" not in body.split('id="mode"')[1].split("</select>")[0]
    else:
        mode_block = body.split('id="mode"')[1].split("</select>")[0]
        assert f'value="{expected_mode}" selected' in mode_block


def test_index_prunes_abandoned_sessions(tmp_path):
    import datetime as dt
    import sqlite3

    flask_app, _ = make_app(tmp_path)
    store = flask_app.config["SESSION_STORE"]
    old_id = store.create([])
    with sqlite3.connect(str(tmp_path / "sessions.db")) as conn:
        old_stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
        conn.execute("UPDATE review_sessions SET updated_at = ? WHERE id = ?", (old_stamp, old_id))
        conn.commit()

    client = flask_app.test_client()
    client.get("/")   # triggers the opportunistic prune

    assert store.load(old_id) is None


# ─────────────────────────────────────────────────────────────────────────────
# POST /review/<id>/finalize
# ─────────────────────────────────────────────────────────────────────────────

def test_finalize_with_nothing_approved_redirects_to_review_with_friendly_message(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/finalize")
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/review/{session_id}"

    followed = client.post(f"/review/{session_id}/finalize", follow_redirects=True)
    assert b"approve at least one track" in followed.data.lower()


def test_finalize_creates_new_playlist_when_no_playlist_id_given(tmp_path):
    flask_app, spotify = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")
    client.post(f"/review/{session_id}/track/t2/approve")

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_name": "Sunday Brunch"})

    assert resp.status_code == 200
    assert b"created_pl" in resp.data
    create_calls = [c for c in spotify.post_calls if c["url"] == "me/playlists"]
    assert len(create_calls) == 1
    assert create_calls[0]["payload"]["name"] == "Sunday Brunch"
    add_calls = [c for c in spotify.post_calls if c["url"] == "playlists/created_pl/items"]
    assert len(add_calls) == 1
    assert set(add_calls[0]["payload"]["uris"]) == {"spotify:track:t1", "spotify:track:t2"}


def test_finalize_appends_to_existing_playlist_when_id_given(tmp_path):
    flask_app, spotify = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_id": "existing_pl_123"})

    assert resp.status_code == 200
    assert not any(c["url"] == "me/playlists" for c in spotify.post_calls)
    add_calls = [c for c in spotify.post_calls if c["url"] == "playlists/existing_pl_123/items"]
    assert len(add_calls) == 1
    assert add_calls[0]["payload"]["uris"] == ["spotify:track:t1"]


def test_finalize_renders_open_in_spotify_link_for_new_playlist(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_name": "Sunday Brunch"})

    assert b'href="https://open.spotify.com/playlist/created_pl"' in resp.data


def test_finalize_renders_open_in_spotify_link_for_existing_playlist(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_id": "existing_pl_123"})

    assert b'href="https://open.spotify.com/playlist/existing_pl_123"' in resp.data


def test_finalize_write_failure_redirects_to_review_with_friendly_message(tmp_path):
    """Regression test: create_playlist()/add_tracks_to_playlist() had zero
    error handling — a bad manually-typed playlist id (the override field
    is free text, never validated), a network hiccup, or a rate limit would
    crash this route to a raw 500 instead of the friendly-message pattern
    every other error case in this app uses."""
    spotify = FailingWriteSpotifyClient(TWO_TRACK_CATALOG)
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, spotify=spotify)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/review/{session_id}"

    followed = client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"}, follow_redirects=True)
    assert followed.status_code == 200
    assert b"couldn" in followed.data.lower()   # "Couldn't write to Spotify..." (apostrophe HTML-escaped)


def test_finalize_write_failure_does_not_delete_the_session(tmp_path):
    """The whole point of redirecting back to review instead of erroring out
    is that the human can fix the playlist id and retry — that only works
    if the session is still there."""
    spotify = FailingWriteSpotifyClient(TWO_TRACK_CATALOG)
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, spotify=spotify)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    session = flask_app.config["SESSION_STORE"].load(session_id)
    assert session is not None
    assert session.items()[0].status == "approved"   # the approval survived, not lost


def test_finalize_write_failure_does_not_record_run_log_or_recently_used(tmp_path):
    spotify = FailingWriteSpotifyClient(TWO_TRACK_CATALOG)
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, spotify=spotify)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    entries = flask_app.config["RUN_LOG"].read_all()
    assert [e for e in entries if e["action"] in ("append", "create")] == []
    assert not flask_app.config["RECENTLY_USED_LOG"].ids_used_within()


def test_finalize_only_writes_approved_tracks_not_pending_or_removed(tmp_path):
    flask_app, spotify = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")
    client.post(f"/review/{session_id}/track/t2/remove")   # explicitly removed, not approved

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    add_calls = [c for c in spotify.post_calls if c["url"] == "playlists/pl1/items"]
    assert add_calls[0]["payload"]["uris"] == ["spotify:track:t1"]


def test_finalize_deletes_the_session(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    assert flask_app.config["SESSION_STORE"].load(session_id) is None


def test_finalize_writes_a_run_log_entry(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/finalize", data={"playlist_id": "pl1"})

    entries = flask_app.config["RUN_LOG"].read_all()
    finalize_entries = [e for e in entries if e["action"] in ("append", "create")]
    assert len(finalize_entries) == 1
    assert finalize_entries[0]["target_playlist_id"] == "pl1"
    assert finalize_entries[0]["final_track_ids"] == ["t1"]


def test_finalize_unknown_session_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/review/nonexistent/finalize", data={"playlist_id": "pl1"})

    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POST /review/<id>/cancel — leave the review screen without writing to
# Spotify. Deletes the session outright (mirrors finalize()'s own cleanup)
# rather than just linking back to "/" and leaving it for SessionStore's
# 7-day prune, so a cancelled review can never be resumed/finalized later
# via browser-back or a saved link.
# ─────────────────────────────────────────────────────────────────────────────

def test_cancel_redirects_to_index(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/cancel")

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_cancel_flashes_confirmation(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.post(f"/review/{session_id}/cancel", follow_redirects=True)

    assert b"discarded" in resp.data
    assert b"nothing was written" in resp.data.lower()


def test_cancel_deletes_the_session(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")   # some in-progress review state

    client.post(f"/review/{session_id}/cancel")

    assert flask_app.config["SESSION_STORE"].load(session_id) is None


def test_cancel_makes_the_review_page_404_afterward(tmp_path):
    """Not just "redirected away" — the underlying review is genuinely gone,
    so browser-back or a saved link can't resurrect and finalize it later."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    client.post(f"/review/{session_id}/cancel")
    resp = client.get(f"/review/{session_id}")

    assert resp.status_code == 404


def test_cancel_does_not_write_anything_to_spotify(tmp_path):
    flask_app, spotify = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/cancel")

    assert spotify.post_calls == []


def test_cancel_does_not_record_a_finalize_run_log_entry(tmp_path):
    """generate() itself already logs a "pending_review" entry when the
    session is created (existing behavior, unrelated to cancel) — what
    matters here is that cancelling never adds an "append"/"create" entry,
    the kind finalize() writes for an actual Spotify write."""
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)
    client.post(f"/review/{session_id}/track/t1/approve")

    client.post(f"/review/{session_id}/cancel")

    entries = flask_app.config["RUN_LOG"].read_all()
    assert [e for e in entries if e["action"] in ("append", "create")] == []


def test_cancel_unknown_session_returns_404(tmp_path):
    flask_app, _ = make_app(tmp_path)
    client = flask_app.test_client()

    resp = client.post("/review/nonexistent/cancel")

    assert resp.status_code == 404


def test_review_page_renders_cancel_button(tmp_path):
    flask_app, _ = make_app(tmp_path, candidates=TWO_TRACK_CANDIDATES, search_catalog=TWO_TRACK_CATALOG)
    client = flask_app.test_client()
    session_id = _generate_session(client)

    resp = client.get(f"/review/{session_id}")

    assert f'/review/{session_id}/cancel'.encode() in resp.data
    assert b"Cancel and go back home" in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# Full flow
# ─────────────────────────────────────────────────────────────────────────────

def test_full_flow_generate_review_mixed_actions_then_finalize(tmp_path):
    catalog = {
        "song a": [spotify_track("t1", "Song A", ["Artist 1"])],
        "song b": [spotify_track("t2", "Song B", ["Artist 2"])],
        "song c": [spotify_track("t3", "Song C", ["Artist 3"])],
    }
    candidates = [("Song A", "Artist 1"), ("Song B", "Artist 2"), ("Song C", "Artist 3")]
    flask_app, spotify = make_app(tmp_path, candidates=candidates, search_catalog=catalog)
    client = flask_app.test_client()

    gen_resp = client.post("/generate", data={"vibe_prompt": "mixed vibes", "track_count": "3"})
    session_id = gen_resp.headers["Location"].rsplit("/", 1)[-1]

    client.post(f"/review/{session_id}/track/t1/approve")
    client.post(f"/review/{session_id}/track/t2/remove", data={"reason": "not a fit"})
    # t3 stays pending — deliberately not approved

    resp = client.post(f"/review/{session_id}/finalize", data={"playlist_name": "Mixed Vibes"})

    assert resp.status_code == 200
    add_calls = [c for c in spotify.post_calls if c["url"].startswith("playlists/")]
    assert add_calls[0]["payload"]["uris"] == ["spotify:track:t1"]   # only the approved one


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
