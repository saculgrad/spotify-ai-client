# AI Playlist Tool for Spotify

A small internal web tool that lets a venue describe a playlist in plain
language — *"warm 60s-70s soul for Sunday brunch, nothing too loud"* — and
get real, working Spotify tracks suggested, reviewed, and added, without
anyone hand-searching for songs.

Built for a single restaurant with one shared Spotify account. The AI lives
in the language model, not in Spotify: an LLM (Claude) suggests candidate
songs, and the app resolves each one to a real Spotify track, applies house
rules, and puts a human review step in front of anything that actually gets
written to a playlist.

## What it does

1. **Describe a vibe.** Type a prompt, pick how many songs, optionally pick
   a day-part mode (brunch/dinner/late) or an existing playlist to grow.
2. **Claude suggests candidates.** `{title, artist}` pairs — generated with
   extra headroom (~1.4x) since some will fail to resolve or get filtered.
3. **Every candidate is resolved against the real Spotify catalog.** Fuzzy
   matched, scored, and gated — wrong-artist, wrong-song, karaoke/cover/
   remix noise, and not-playable-in-market results are all rejected rather
   than trusted blindly.
4. **House rules run automatically:** explicit tracks are filtered (or
   swapped for a clean edit if one exists in the same results), duplicates
   against the target playlist and a rolling "recently used" log are
   dropped, and an artist-diversity cap can be applied.
5. **A human reviews every survivor** — approve, remove, or flag for
   regeneration — before anything is written to Spotify. Nothing goes live
   without an explicit approval.
6. **Optionally, generate more mid-review.** Ask for additional songs
   informed by what's already been approved (style grounding) and what was
   rejected (steered away from) in the same session, without touching
   anything already decided.
7. **Finalize** creates a new playlist or appends to an existing one, and
   only the approved tracks are written.

## Key features

- **Explicit vs. clean detection** — prefers a clean edit over an explicit
  cut when both exist in the same search results, instead of just dropping
  the song.
- **Grow an existing playlist** — pick a playlist to append to, and its own
  tracks automatically become both the dedupe target and the style
  reference for new suggestions.
- **"Generate more"** — mid-review follow-up generation that knows what
  you've approved and rejected so far.
- **Recently-used tracking** — a rolling local log keeps playlists from
  repeating the same songs week over week (with a one-click bypass for
  rapid testing/iteration).
- **"Prefer lesser-known songs"** — nudges suggestions toward deeper cuts
  instead of the most obvious, overplayed picks for a vibe.
- **Day-part modes** — brunch/dinner/late presets with their own tone and
  optional standing playlists.
- **Full audit trail** — every generation run is logged locally (prompt,
  counts, drop reasons at every stage) for debugging and dedupe history.

## Architecture

```
generator.py  →  resolver.py  →  curation.py  →  review.py  →  spotify_client.py
(LLM candidates) (real tracks)   (house rules)   (human review) (auth + write)
```

`pipeline.py` wires the first three stages together; `session_store.py`
persists a review session between requests (SQLite); `app.py` is the Flask
web UI on top of all of it. Only `resolver.py`/`spotify_client.py` know
anything about Spotify's API specifics, and only `generator.py` knows
anything about the Claude API — the rest is plain, independently-testable
Python.

See **[CLAUDE.md](CLAUDE.md)** for the full build history, every module's
design rationale, and known limitations — it's the living design doc this
project was built from.

## Getting started

### Requirements

- Python 3.9+
- A Spotify account with Premium (required for Spotify's developer "Dev
  Mode," which this app runs under)
- A [Spotify Developer](https://developer.spotify.com/dashboard) app —
  register one and note the Client ID/Secret. Redirect URI must be exactly
  `http://127.0.0.1:8888/callback` (Spotify rejects a bare `localhost`
  redirect).
- An [Anthropic API key](https://console.anthropic.com/)

### Install

```bash
git clone https://github.com/saculgrad/spotify-ai-client.git
cd spotify-ai-client
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root (gitignored — never commit this):

```bash
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
ANTHROPIC_API_KEY=your_anthropic_api_key
```

Then load them into your shell before running the app (the app doesn't
auto-load `.env` itself):

```bash
set -a && source .env && set +a
```

### Run it

```bash
python app.py
```

Opens on `http://127.0.0.1:5000`. First run will open a browser for Spotify
OAuth consent; the token is cached locally afterward.

### Try it without any credentials

```bash
python demo.py
```

A standalone version with a fixed fake catalog standing in for both Spotify
and Claude — click through the whole review flow with no API keys needed.

### Run the tests

```bash
pytest
```

349 tests, fully offline (mocked Spotify/Claude clients) — no credentials
required.

## Project structure

| File | Responsibility |
|---|---|
| `generator.py` | Calls Claude, returns candidate `{title, artist}` pairs |
| `resolver.py` | Matches candidates to real, scored Spotify tracks |
| `curation.py` | House rules: explicit filter, dedupe, artist-diversity cap |
| `review.py` | Human approve/remove/regenerate state machine |
| `spotify_client.py` | OAuth + all Spotify write/read endpoints |
| `pipeline.py` | Wires generate → resolve → curate into one call |
| `session_store.py` | Durable (SQLite) review-session storage |
| `modes.py` | Day-part presets and target-playlist resolution |
| `venue_config.py` | Local venue config: house taste, blocklist, standing playlists |
| `logging_utils.py` | Local JSONL run log for debugging/dedupe history |
| `app.py` | The Flask web UI wiring everything together |
| `demo.py` | Credential-free standalone demo |

## Known limitations

- No audio-feature targeting (BPM/energy) — Spotify removed those endpoints
  for apps at this tier. Song selection relies entirely on the LLM's
  semantic sense of a vibe.
- Spotify's Dev Mode caps search results and authorized users, and doesn't
  return track popularity data for this app's access tier.
- Re-recording/version collisions (e.g. "Taylor's Version") can resolve to
  the right song but not necessarily the intended specific pressing.

See `CLAUDE.md` for the full, current list.
