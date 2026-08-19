# CLAUDE.md — AI Playlist Tool for Spotify

> Handoff context from a planning + prototyping session held in the Claude web app.
> Read this first; it captures the decisions, the hard-won API constraints, what's
> already built and tested, and what to do next. Verify anything marked *verify*
> against Spotify's live changelog before relying on it.

## Project goal
Build a small internal tool for a restaurant that already maintains many Spotify
playlists. They want an easy way to **(a) add songs to existing playlists** and
**(b) create new playlists from a plain-language description**, without doing it by
hand. Single venue, single shared Spotify account.

## Current real-world priority (2026-08-13)
The restaurant's actual, immediate need, stated directly by the owner: they
already have playlists that "work" (good vibe) but are **too short**, and
they need to **strictly avoid explicit songs**. This reframed two build
priorities as the ones that matter most right now:
1. **Explicit vs. clean version detection.** Not just "drop explicit
   songs" (that silently shrinks the playlist) — prefer the clean edit
   when one exists in the same search results. See `resolver.py`'s
   `allow_explicit` gate below.
2. **Growing a playlist from a seed.** "Way longer" means: take an
   existing playlist that already works, and add more songs that match
   its style. See `app.py`'s "grow an existing playlist" feature below —
   picking an existing playlist as the target automatically uses its own
   tracks as house-taste grounding, no separate "seed" concept needed.

`index.html`'s "Allow explicit tracks" checkbox defaulted **unchecked**
because of this from 2026-08-13 until 2026-08-18 — **superseded**, see the
"Form defaults flipped" entry in `app.py`'s "What's built" section below
for the current, permanent default. The underlying capability this
priority drove — `resolver.py`'s `allow_explicit` gate preferring a clean
edit over explicit when both exist in the same search results — is
unchanged and still applies whenever `allow_explicit=False` is actually
in effect (i.e., whenever the checkbox is unchecked for a given run); only
the checkbox's *default* state on page load changed.

## Core architecture decision — do not undo this
The AI lives in a **language model, not in Spotify**. Flow:

    staff describes a vibe (or pastes seed songs)
      -> an LLM returns candidate tracks as {title, artist}
      -> code resolves each candidate to a REAL Spotify track via Search
      -> code creates or appends the playlist

**Why:** Spotify deprecated the endpoints that used to power "AI playlists" — the
recommendations engine and audio-features/audio-analysis — for apps at this tier. We
therefore cannot use Spotify for song selection or for tempo/energy/BPM filtering.
The LLM does selection; Spotify only searches and saves.

## Spotify Web API constraints (2024–2026) — *verify* before relying on any of these
- **Dev Mode**: app owner must hold Spotify Premium; capped at **5 authorized
  users**; 1 Client ID per developer. Extended quota (past those caps) realistically
  needs a registered business + ~250k MAU — not attainable here. Design for ONE
  shared restaurant account, not per-employee logins.
- **Redirect URI**: `localhost` is rejected. Register exactly
  `http://127.0.0.1:8888/callback`.
- **Auth**: Authorization Code + PKCE (user token). Spotify has been moving metadata
  endpoints (incl. Search) away from app-only Client Credentials — don't rely on
  client-credentials for search.
- **Endpoint moves (Feb 2026)**:
  - create playlist = `POST /me/playlists`  (old `/users/{id}/playlists` removed)
  - add items      = `POST /playlists/{id}/items`  (was `/tracks`)
  - reorder/replace= `PUT /playlists/{id}/items`
  - remove         = `DELETE /playlists/{id}/tracks`
  - add-items limit = **100 URIs per call** (batch beyond that)
  - playlist item field is now `items`/`item`, not `tracks`/`track`
- **Gone — don't design around**: recommendations, audio-features, audio-analysis,
  related-artists, artist top tracks, several bulk-metadata & public-profile
  endpoints. **Confirmed live 2026-08-19**: batch `GET /tracks?ids=...` is
  one of them — 403s for this app's tier even with a valid user token that
  works fine for `GET /tracks/{id}` (single) and `search`. Not used
  anywhere in the app (nothing here ever needs to look up arbitrary tracks
  after the fact), only hit during an ad-hoc diagnostic — but worth knowing
  before reaching for it if a future feature ever wants bulk track lookup.
- **Search result cap**: Dev Mode reportedly caps search results low (~5–10).
  Tighter queries beat raising the limit.
- **Churn**: this API has shipped breaking changes roughly every few months for two
  years. **Isolate all Spotify-specific code (endpoints, field names, auth) behind
  one module** so the next change is a one-file patch.

## Stack
- Python (venv pinned to system 3.9.6 in this environment — CLAUDE.md
  originally called for 3.12+; nothing built so far needs anything newer
  than 3.9 actually provides, see `.venv/`)
- `spotipy` (OAuth/refresh + endpoints), `rapidfuzz` (fuzzy matching),
  `unidecode` (diacritics), `anthropic` (LLM SDK), `pytest` — all pinned in
  `requirements.txt`
- Env vars: `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`,
  `SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback`, `ANTHROPIC_API_KEY`
  (or `ant auth login`)

## Module map (pipeline order)
`generator.py` (LLM candidates) → `resolver.py` (real Spotify tracks) →
`curation.py` (house rules) → `review.py` (human approve/remove) →
`spotify_client.py` (auth + write). `pipeline.py` wires the first three
into one call; `session_store.py` persists a `review.py` session (plus
its resolved target playlist) between HTTP requests; `app.py` is the
Flask web UI sitting on top of all of it, with `demo.py` as a
credential-free way to click through it. `modes.py`, `logging_utils.py`,
and `venue_config.py` (house taste / blocklist / standing-playlists
config) are supporting pieces touching several stages. Only `resolver.py`
and `spotify_client.py` know anything about Spotify's endpoints/field
names/auth; only `generator.py` knows anything about the Claude API; only
`app.py` knows anything about HTTP/Flask. Everything else is pure Python,
independently testable without any of those three.

## What's built: `curation.py`
The house-rules filtering layer — sits between `resolver.py`'s accepted
tracks and the (not-yet-built) write step. Every function is pure local
logic or local-file I/O; nothing in this module touches Spotify or an LLM,
so it's fully buildable, runnable, and testable with zero credentials
(`python curation.py` runs its demo end-to-end right now).
- `filter_resolver_accepted()` — defensive guard: drops anything the caller
  passed in that the resolver didn't actually accept, rather than treating
  it as a legitimate track.
- `filter_explicit()` — explicit-content policy. `explicit=True` dropped
  when `allow_explicit=False`; `explicit=None` (flag missing) is kept
  unless `drop_unknown=True`.
- `cap_artist_diversity()` — keeps the first N tracks per artist (input
  order), grouping by the normalized *primary* artist so featured-artist
  noise and `&`-band names (e.g. "Bob Marley & The Wailers") group
  correctly — reuses `resolver.py`'s `normalize()`/`primary_artist()`
  rather than reimplementing that logic.
- `dedupe_against_playlist()` / `dedupe_against_recent_log()` — thin
  wrappers over a generic `dedupe_against_ids()` id-membership filter.
- `RecentlyUsedLog` — a small JSON-backed `{track_id: last_used_timestamp}`
  log (local file, no network) so the same songs don't get re-added every
  week per the spec's quality-levers list. Corrupted/unexpected-shape files
  degrade to an empty log with a stderr warning rather than crashing.
- `apply_house_rules()` — composes all of the above in a fixed, documented
  order (accepted-only → dedupe vs. playlist → dedupe vs. recent log →
  explicit filter → artist cap last) and returns a `HouseRulesOutcome` with
  a `summary` breaking down drops by reason.
- **Tested (fully offline, no credentials of any kind):** 41 tests covering
  each filter individually, order-sensitive artist-cap behavior (case
  folding, `&`-band grouping, featured-artist grouping, non-positive caps
  rejected), dedupe edge cases (empty inputs, `None` track_id, duplicate
  seen-ids), `RecentlyUsedLog` file I/O against `tmp_path` (missing file,
  round-trip save/reload, corrupted JSON, wrong-shape JSON, prune, and the
  `within_days` boundary — which caught two wrong assumptions of mine
  during writing, since real elapsed time between `record()` and
  `is_recent()` is never exactly zero), and the full `apply_house_rules()`
  pipeline composition with every reason bucket verified in one pass.

## What's built: `generator.py`
The LLM candidate-generation layer. Given a plain-language vibe prompt (plus
optional era/language/explicit/avoid-obvious toggles, an artist-diversity
hint, a blocklist, and a house-taste few-shot sample), it calls the Claude
API and returns `{title, artist}` candidates ready for `resolver.py`.
- Uses **structured outputs** (`output_config.format` / `json_schema`) so the
  response is guaranteed valid JSON — no fence-stripping needed. Still
  retries (up to 3 attempts) on a safety refusal, a `max_tokens` truncation,
  or unparseable JSON.
- **Over-generates** ~1.4x the requested count (`OVERGENERATION_FACTOR`) so
  the list survives `resolver.py`'s drops.
- Emits a `wanted_variants` set the model fills in from a known vocabulary
  that matches `resolver.py`'s `VARIANT_PENALTIES` keys (live, remix,
  instrumental, cover, karaoke, tribute, sped up, slowed, nightcore,
  8-bit) — pass this straight into `resolve_tracklist(wanted_variants=...)`.
- **Blocklist is enforced in code, not just prompted** — `_apply_blocklist()`
  substring-matches (via `resolver.normalize()`) every candidate's title and
  artist against the blocklist after generation, so a banned artist/track
  can't slip through because the model ignored an instruction.
- Defensively drops any candidate missing a usable title/artist rather than
  crashing (`_parse_candidates()`), mirroring `resolver.py`'s "drop with a
  reason, don't crash" philosophy.
- Model defaults to `claude-opus-4-8` (a tuning knob at the top of the
  file) — the project spec notes a cheaper model is plenty for this task if
  cost matters more than quality.
- **`avoid_obvious` prompt wording strengthened (2026-08-19) — a real,
  live-diagnosed quality bug, not a wiring bug.** The owner reported
  "Prefer lesser-known songs" barely changing anything on a real run.
  Wiring was already confirmed correct (`test_app.py` checks the flag
  reaches the prompt) — the question was whether the LLM was actually
  *acting* on it. A live A/B test against the real Claude API (same
  prompt, `avoid_obvious=True` vs `False`) showed the original mild
  wording ("go a little deeper into each artist's catalog or lean on
  lesser-known artists who fit") worked fine for some vibes (a "warm 90s
  R&B" request meaningfully shifted from Boyz II Men/Mariah Carey greatest
  hits to genuine deeper cuts) but **barely moved the needle for
  high-energy/party vibes** — a "classic frat party" request still came
  back full of all-time chart-topping anthems (Toxic, All the Small
  Things, Ms. Jackson) even with the flag on. Hypothesis: the model treats
  "party"/"high energy" as implicitly meaning "must be a widely-recognized
  crowd-pleaser," and weighs that assumption more heavily than a mild
  textual nudge when the two are in tension. Tested by strengthening the
  wording to explicitly override that assumption ("This applies even for
  high-energy/party requests — do not default to a track just because it
  is a famous genre-defining anthem...") and re-running the *identical*
  frat-party prompt live: markedly deeper results (Huey, Rich Boy, Young
  Jeezy, Lupe Fiasco's "Kick, Push" instead of his biggest hit). Shipped
  the strengthened wording. **Tested (offline, locks in the wording reaches
  the prompt — the quality claim itself was validated live, not by an
  offline test):** the new instruction text present when `avoid_obvious=True`,
  absent when `False`. 2 new tests.
- **Deeper investigation (2026-08-19) — does `avoid_obvious` generalize to
  ANY genre? Conclusion: not via prompt engineering alone; nothing shipped
  from this.** The owner confirmed the checkbox was on for a real house/EDM
  run that still came back mostly genre-canon (see `app.py`'s toggle-
  logging entry). Six live-tested variants across three genres (EDM — known
  failure, hip-hop frat-party — known success as a regression check, and
  "upbeat country road trip" — a fresh genre neither had been tuned
  against):
  1. Self-reasoning ("privately think of the obvious picks, then avoid
     them," single call) — barely moved the needle for EDM/country.
  2. Two-call, dynamically ask Claude to name obvious *songs*, feed that
     list back as "don't use these" — the model avoided the exact named
     songs, then substituted *other* songs at the same fame level, often
     by the same artist. Also produced one malformed-looking candidate —
     a reliability wrinkle from reflecting the model's own output back
     into the prompt this way.
  3. Two-call, dynamically ask for obvious *artists* (10 names) instead of
     songs, ban those artists entirely — same failure pattern: avoided
     the 10 named artists exactly, substituted other equally-famous
     artists not on the list (deadmau5, Tiësto for EDM; Luke Combs, Morgan
     Wallen for country — none of them named, all arguably as "obvious").
     **One concerning loophole found**: once credited a featured vocalist
     ("Foxes") instead of the "banned" headliner (Zedd) for what's
     functionally the same famous song.
  4. Combined: wider dynamic artist list (15-20 names) + the "avoid a
     best-of-compilation, favor DJ's-crate-level familiarity" framing —
     the best result of the six, genuinely more deep cuts mixed in (Green
     Velvet, Eli Brown for EDM; Dwight Yoakam, Randy Houser for country),
     but still let through some very famous tracks (FISHER, MEDUZA, John
     Summit; Brooks & Dunn, "Wagon Wheel" — the latter recurred across
     *every* country variant tested, seemingly unavoidable without
     naming it explicitly).
  **Root cause, well-evidenced across all six attempts:** telling the
  model to avoid specific named things (songs or artists) does not
  generalize to avoiding the *category of fame* those things represent —
  it reliably finds equally-famous substitutes not on whatever list was
  given. Not a wording problem; closer to a structural limit of
  prompt-based steering for this specific task.
  **A code-level (non-prompt) alternative was considered and ruled out**:
  `MatchResult.popularity` already exists but is unused — checked whether
  it could back a deterministic post-hoc filter, and confirmed live that
  Spotify's `popularity` field comes back `None` for every track this
  app's credentials can see (checked via both `search` and individual
  `GET /tracks/{id}`, including for objectively massive tracks like Daft
  Punk's "One More Time"). Not available at this app's access tier —
  don't design around it.
  **Where this leaves things:** variant 4 (dynamic wide artist list +
  compilation framing) is a genuine, measurable improvement over what's
  currently shipped, at the cost of one extra Claude API call (~1-3s) per
  `generate()`. Owner chose to hold off shipping it and keep testing the
  current (mild-relative-to-variant-4, but already-shipped) wording first.
  If revisiting this: variant 4's exact prompts are the ones to start
  from, not variants 1-3 (already shown weaker/riskier).
- **`previously_rejected` field added (2026-08-19) — backs the "generate
  more" follow-up feature** (see `app.py`'s and `review.py`'s entries).
  "Artist - Title" lines, optionally with a trailing "(reason: ...)" or
  "(wanted instead: ...)", for tracks a human already removed or flagged
  for regeneration THIS session — rendered as an explicit "don't suggest
  these again" instruction. Deliberately a **separate field from
  `blocklist`**, not folded into it: `blocklist` is a permanent, venue-wide
  hard ban (survives forever, applies to every future generation);
  `previously_rejected` is a soft, per-request steer built fresh from one
  review session's state and thrown away with it. Capped at 100 lines
  (same pattern as `house_taste`'s 200-line cap) so a very long iterative
  session doesn't grow the prompt unboundedly.
- **Tested (mock Anthropic client, no API key needed):** happy path,
  correct over-generation count sent in the prompt, blocklist-by-artist,
  blocklist-by-track-title, malformed-row dropping, retry-then-succeed on
  refusal/truncation/malformed-JSON, raising after all retries are
  exhausted, the two `avoid_obvious` wording tests above, and
  `previously_rejected` reaching the prompt (present when populated,
  omitted when empty). 13/13 pass.
- **Live-verified against the real Claude API** multiple times since this
  file first said "not tested yet" — the initial live run (see "Live API
  verification" below) and the `avoid_obvious` A/B diagnostic above both
  used the real API, not mocks.

## What's built: `resolver.py`
The track resolver — the quality-critical piece. Given LLM candidates
`{title, artist}`, it searches Spotify, **scores every result (never trusts result
#0)**, and either picks the best real track or drops the candidate with a reason.
- Score = `WEIGHT_TITLE*title_sim + WEIGHT_ARTIST*artist_sim - variant_penalty`.
- Hard gates: artist mismatch / title mismatch / not-playable-in-market /
  (if `allow_explicit=False`) explicit → reject.
- Variant penalties: karaoke, cover, tribute, sped-up, nightcore, instrumental,
  remix, live — unless the caller passes them in `wanted_variants`.
- Dedupe by track id; batch returns accept/drop lists + a drop-rate summary.
- **Tuning knobs are grouped at the top of the file** — adjust while eyeballing
  output (`ACCEPT_THRESHOLD`, `ARTIST_MIN`, `TITLE_MIN`, weights, penalty table).
- Recent fix: `best_artist_sim()` compares the candidate against the full artist
  string, the primary-artist fallback, and the joined track-artist list, taking the
  max — so bands with `&`/`and`/`,` in their name (e.g. "Bob Marley & The Wailers")
  aren't wrongly penalized.
- **`allow_explicit` gate (2026-08-13, driven by the venue priority above).**
  `resolve_track(..., allow_explicit=False)` treats an explicit result as a
  hard gate failure in the same best-to-worst walk used for the
  not-playable-in-market fallback — so if a clean edit is sitting in the
  same search results as the explicit one, the walk picks the clean one
  instead of committing to the (usually higher-scoring, since it's often
  the more popular/canonical result) explicit cut and losing the song
  entirely when `curation.filter_explicit()` drops it downstream. Only
  drops the candidate outright when *no* clean version exists in the
  results at all. `explicit=None` (unreported) is never gated — same
  "unknown isn't guilty" policy as `curation.filter_explicit()`.
  `NOISE_PATTERNS` also gained `(Clean)`/`(Explicit)`/`- Clean`/`- Explicit`
  stripping — those tags were dragging down `title_sim` for no reason
  (which cut you get is decided by the boolean field, not by matching this
  text), and could otherwise push an otherwise-perfect clean match below
  `TITLE_MIN` on short titles. **Both gates are enforced twice on purpose**
  — this one *prefers* clean over explicit at resolve time;
  `curation.filter_explicit()` remains the downstream backstop for
  anything that still slips through explicit (no clean version existed,
  or the flag was unknown at resolve time).
- **`resolve_tracklist()` parallelized (2026-08-13).** Was a plain
  sequential `for` loop calling `resolve_track()` once per candidate;
  now runs each candidate's search concurrently in a `ThreadPoolExecutor`
  (`MAX_RESOLVER_WORKERS = 8`). Motivated by real, felt latency in the
  live app: `/generate` chains a Claude call + several sequential
  playlist-read calls + resolution, and resolution was ~20 sequential
  Spotify Search round trips (one per over-generated candidate) — pure
  network wait, so threads help despite the GIL. Two things had to be
  gotten right, both because "be careful nothing breaks" was the explicit
  ask:
  - **Order preservation.** Uses `ThreadPoolExecutor.map()`, which returns
    results in input order regardless of completion order — required
    because the dedupe-by-track-id loop keeps the *first candidate by
    input order* that resolves to a given track and drops any later one
    as `"duplicate"`. If results were collected in completion order
    instead (e.g. via `as_completed()`), which candidate "wins" a
    duplicate would depend on network timing instead of on what the
    caller asked for.
  - **Token-refresh race.** spotipy's `SpotifyOAuth` has no lock around
    `refresh_access_token()` — if the cached token happened to be expired
    when a batch starts, every worker thread would independently detect
    that and race to POST its own refresh (and write the on-disk token
    cache) at once. Fixed by pre-warming the token with one synchronous
    `auth_manager.get_access_token()` call on the calling thread before
    the pool starts (best-effort — a fake test client with no
    `auth_manager` attribute, or a real failure, both degrade gracefully
    rather than crashing the batch).
  - Empty candidate lists are special-cased before the pool is created —
    `ThreadPoolExecutor(max_workers=0)` raises outright otherwise.
  - **Tested:** an empty-batch guard, a token-prewarm-called-exactly-once
    test, prewarm-failure tolerance, prewarm-skipped-when-no-auth-manager,
    and — the one that actually exercises the race, not just re-runs old
    assertions — a test where the *first* candidate by input order is
    deliberately made the *slowest* to resolve (via a real `time.sleep` in
    the fake client), confirming the dedupe still keeps that first
    candidate rather than letting the faster second one win. 5 new tests,
    274 total (was 269).
- **`resolve_tracklist()`'s summary gained `dropped_by_reason` (2026-08-18).**
  Driven by a real support question the owner asked after live testing:
  "why did more than half my songs not get approved?" It turned out to be
  unanswerable from `run_log.jsonl` — the log only ever recorded
  *curation's* drop reasons (dedupe/explicit/artist-cap), and on the run
  that actually prompted the question, curation had dropped nothing at
  all; all the loss happened at the resolver stage with zero visibility
  into why. Buckets `dropped`'s reasons the same way
  `curation.HouseRulesOutcome.summary` already does — strip the
  parenthetical detail (`"artist_mismatch (0.45)"` → `"artist_mismatch"`)
  so multiple candidates failing the same gate for different specific
  scores still count toward one bucket. Threaded through
  `pipeline.PipelineResult.resolver_dropped_by_reason` and
  `logging_utils.RunLogEntry.resolver_dropped_summary` (see their entries)
  so `app.py` can log it. **Tested:** reason-bucketing with mixed
  parenthetical detail, empty-when-nothing-dropped. 2 new tests.
- **Live diagnostic (2026-08-18) — is the resolver actually biased against
  less-popular songs?** Investigated directly against the real Spotify
  Search API (not mocks) with a set of genuinely real deep-cut songs
  (Freddie Gibbs, Roc Marciano, Your Old Droog, Ka, billy woods — chosen to
  separate "resolver rejected a real match" from "the LLM hallucinated a
  song that doesn't exist," which is what the earlier drop-rate data alone
  couldn't distinguish). **Result: 6/7 resolved at a perfect 1.0 score**,
  including a lowercase-stylized artist name (`billy woods`) and multi-
  artist credits — `score_track()` doesn't use popularity at all (confirmed
  by reading the code, not just the score formula in this file), and
  normalization handles casing/diacritics/collab-credit noise the same for
  obscure and mainstream artists alike. The one non-match (`"Alfredo"` by
  Freddie Gibbs & The Alchemist) turned out to almost certainly be *my own
  test candidate's fault* — "Alfredo" is the *album* title, not a track on
  it — which is itself a useful data point: it's a live demonstration of
  exactly the hallucination risk this investigation was trying to isolate.
  **Conclusion: resolver-side text-matching strictness is NOT well
  supported as the primary cause** of the "half my songs get dropped"
  pattern the owner reported — when given an accurate title/artist, deep
  cuts resolve exactly as well as mainstream tracks. The much more likely
  explanations, in order: (1) the recently-used 30-day window compounding
  during rapid same-playlist testing (see `curation.py`'s and `app.py`'s
  `ignore_recently_used` entries), and (2) LLM hallucination naturally
  increasing for less-mainstream asks — not fixable in `resolver.py`,
  addressed instead by wiring up `generator.py`'s already-existing (but
  previously never surfaced) `avoid_obvious` flag, which nudges the LLM's
  *generation* itself toward deeper cuts rather than trying to make
  *resolution* more lenient after the fact (see `app.py`'s entry). **One
  real, structural strictness lever *was* identified but deliberately NOT
  applied without confirmation:** `ACCEPT_THRESHOLD=0.72` sits above the
  mathematical floor (0.60) that clearing both `ARTIST_MIN`/`TITLE_MIN`
  alone would allow, so a candidate that's decent-but-imperfect on *both*
  dimensions at once can still get rejected even though neither individual
  gate caught it. This is a real, low-risk-to-loosen lever (human review is
  already the safety net downstream) — but nothing in this specific live
  test actually landed in that dead zone, so there's no direct evidence
  yet that it's a meaningful contributor. Left as a documented, ready-to-
  apply option rather than changed speculatively.

## Tested / not tested
- **Tested (mock Spotify client, adversarial cases):** skips karaoke/cover sitting
  ahead of the real track; folds diacritics; disambiguates identical titles by
  artist; drops non-existent songs; blocks not-playable-in-market; dedupes;
  captures the `explicit` flag; `allow_explicit=False` prefers a clean
  version over a higher-scoring explicit one, falls back through multiple
  explicit results to find a clean one, drops the candidate outright when
  only explicit exists (with the correct `explicit_track` reason), doesn't
  gate unknown explicit status, and — regression check — `allow_explicit=True`
  behaves exactly as before; `(Clean)`/`(Explicit)` tags noise-stripped
  correctly. 32/32 tests pass, including a gate-fallback fix (see git
  history / prior session) and two documented re-recording-collision
  limitations.
- **Live-verified (2026-08-13, real Spotify Search API, owner's personal
  account via `python resolver.py`):** 4/5 accepted, 1/5 correctly dropped
  (the deliberately-fake candidate — `artist_mismatch`, with an honest
  "best guess" logged, no silent wrong match). Confirms several fixes hold
  up against real data, not just mocks: remaster-stripping + the
  `&`-band-name fix together on "Redemption Song (Remastered)" → "Bob
  Marley & The Wailers" (score 1.0), diacritics folding on "Édith Piaf"
  (score 1.0), and a clean exact match on "Golden Hour" — "JVKE" (score
  1.0). **Still not live-verified:** the write side (`create_playlist`,
  `add_tracks_to_playlist`) — this only exercised `GET /search`, not the
  Feb-2026-flagged `POST /me/playlists` / `POST /playlists/{id}/items`
  endpoints in `spotify_client.py`. That's the next live check.

## What's built: `review.py`
The human review state machine — CLAUDE.md's spec calls this "the single
step that makes the whole thing reliable despite LLM hallucination."
Deliberately **not** a UI: `ReviewSession` is UI-framework-agnostic pure
logic (no Flask, no CLI prompt loop baked in) so that decision can be made
later without touching this file. A CLI or web layer just calls
`session.approve(id)` / `.remove(id, reason)` / `.request_regenerate(id, note)`.
- Every action **overrides** whatever status a track had before — approving
  something you'd removed, or vice versa, is a normal "changed my mind"
  case, not an error. `approve_all_pending()` only touches tracks still
  `pending`, so it never silently un-does an explicit remove/regenerate call.
- `final_uris()` returns approved tracks' URIs in **original resolver
  order** (not approval order) — ready for
  `spotify_client.add_tracks_to_playlist()`.
- `items()` returns every track in original order with its current
  status/note regardless of bucket — added for rendering (a UI needs every
  status, not just one) and for `session_store.py`'s serialization.
- Constructor rejects duplicate `track_id`s and tracks missing a
  `track_id` outright (`ValueError`) — a review session over ambiguous
  input would silently corrupt itself later, so this is caught at
  construction instead.
- **Tested (fully offline):** every pairwise state transition (9
  combinations, parametrized), idempotent double-approve, `approve_all_pending`
  leaving removed/regenerate-requested tracks alone, `final_uris()` ordering
  and its skip-if-no-uri edge case, unknown-track-id `KeyError`s, empty
  session, `items()` full-state ordering. 32/32 pass — including a bug the
  tests caught in my own test helper (`uri=None` vs. "no uri passed" were
  indistinguishable with a plain default argument; fixed with a sentinel).
- **`add_tracks()` added (2026-08-19) — backs the "generate more" feature**
  (see `app.py`'s and `generator.py`'s entries). Appends newly generated
  tracks to an EXISTING session as `PENDING`, leaving every already-decided
  item's status/note untouched — the constructor's shape (build once from a
  fixed list) wasn't enough once a review needed to grow mid-session. Same
  validation as the constructor (`ValueError` on a missing/duplicate
  `track_id`), checked against the *full* existing session so a bug that
  lets a duplicate slip past `app.py`'s dedupe is caught here rather than
  silently corrupting the session.
  - **Tested:** appends as pending without disturbing existing
    approved/removed/regenerate-requested items, lands after existing
    items in original order (matters for `final_uris()` ordering and the
    review page's scroll-anchor), rejects a missing track_id, rejects a
    duplicate against an existing session track, rejects a duplicate
    *within* the new batch itself, a no-op on an empty list, and an
    end-to-end check that a track added this way can be approved and
    reaches `final_uris()` like any other. 8 new tests, 40/40 pass (was 32).

## What's built: `spotify_client.py`
The OAuth + write scaffold. Kept as its own file, separate from
`resolver.py` (which owns *search*), so a write-endpoint churn and a
search-shape churn never land in the same diff — see "Module map" above.
- `build_auth_manager()` reads `SPOTIPY_CLIENT_ID`/`SECRET`/`REDIRECT_URI`
  from env with clear `RuntimeError`s on anything missing (better than
  spotipy's generic errors), and hard-rejects a `localhost` redirect URI
  before it ever reaches Spotify's servers. Pure config — no network call,
  so it's fully testable.
- `create_playlist()`, `add_tracks_to_playlist()` (batched at 100/call,
  Spotify's documented limit), `get_playlist_track_ids()` (paginated,
  feeds straight into `curation.dedupe_against_playlist()`) all use
  spotipy's **low-level** `sp._get()`/`sp._post()` rather than its
  high-level convenience methods — pins the exact endpoint paths CLAUDE.md
  documents rather than trusting whatever the installed spotipy version's
  high-level methods happen to hit.
- `list_playlists()` (paginated `GET /me/playlists`) feeds the review UI's
  playlist picker. `get_house_taste_sample()` samples "Artist - Title"
  lines from configured playlists for `generator.py`'s house_taste
  grounding — both added to close punch-list category A gaps; same
  defensive `item`/`track` field handling as `get_playlist_track_ids()`.
- **`*verify*` — genuinely unconfirmed:** the Feb-2026 endpoint moves this
  relies on (`POST /me/playlists`, `POST/GET /playlists/{id}/items`, and
  the `item`-vs-`track` field rename) come from CLAUDE.md, not a live
  account — there isn't one set up yet. Every read function checks
  **both** `entry["item"]` and `entry["track"]` defensively so it keeps
  working whichever shape turns out to be live.
- **Tested (fully offline — `_get`/`_post` mocked, no network):** env-var
  validation, localhost rejection, default vs. custom cache path,
  create_playlist payload shape, add-batching at the exact 100/101/200
  boundaries (this is where an off-by-one would actually bite), pagination
  across full/partial pages, defensive handling of null tracks/missing
  fields/duplicate ids in playlist entries, `list_playlists()` pagination
  and malformed-entry skipping, `get_house_taste_sample()`'s
  `limit_per_playlist`/`max_total` caps and multi-playlist combination.
  38/38 pass. **Genuinely not testable without live credentials:** the
  actual OAuth login flow and whether the endpoint/field-name assumptions
  above are still correct.

## What's built: `modes.py`
Day-part "modes" (brunch/dinner/late) and target-playlist resolution —
CLAUDE.md's Phase 3 polish items, pulled forward since they're pure logic.
- `build_generation_request()` turns a mode name into a pre-filled
  `generator.GenerationRequest` (era/explicit/avoid-obvious defaults per
  day-part, folded into the vibe prompt as a hint) — explicit overrides
  always win over the preset.
- `resolve_target_playlist()` decides append-to-standing-playlist vs.
  create-new, with a documented precedence: explicit playlist ID > mode
  with a configured standing playlist > create new.
- `current_day_part()` is the "scheduling" piece from the roadmap, but only
  the pure "which time window are we in" lookup (handles the
  past-midnight wrap for "late" correctly). **Actually wiring this into a
  recurring job (cron/launchd/Task Scheduler) is a one-time machine-config
  step for whoever runs the tool — deliberately not scripted here**, since
  that's local-machine state I shouldn't modify without being asked.
- **Tested (fully offline):** preset application, override precedence,
  case-insensitivity, unknown-mode errors, every window boundary
  (start-inclusive/end-exclusive, including the exact 21:00 handoff
  between dinner's end and late's start, and the midnight wrap). 24/24 pass.

## What's built: `logging_utils.py`
The run log from spec section 2 ("a log of what was generated, for dedupe
and debugging") — JSONL (one JSON object per line) rather than a single
JSON array, specifically so one corrupted line can never take down every
record around it. `RunLogEntry` captures the prompt, counts at each stage,
final track ids, and the curation drop-reason summary; `RunLog.append()`/
`.read_all()` are the whole interface.
- **`resolver_dropped_summary` field added (2026-08-18)**, alongside the
  existing `dropped_summary` (curation's reasons) — see `resolver.py`'s and
  `pipeline.py`'s entries for why the two needed to stay separate fields
  rather than merge into one. `default_factory=dict` keeps it backward
  compatible with reading old log lines written before this field existed
  (`RunLog.read_all()` returns raw parsed JSON dicts, never reconstructs a
  `RunLogEntry`, so an old line simply comes back without the key rather
  than erroring).
- **`avoid_obvious`/`ignore_recently_used` fields added (2026-08-19).**
  Whether those two checkboxes were actually on for a given run — added
  after a direct instance of not being able to answer that. The owner
  reported disappointing "Prefer lesser-known songs" results for a real
  house/EDM run; investigating it live required asking the owner whether
  the checkbox was even checked, since the log had no way to tell. Both
  `False` by default, same pattern as `dropped_summary`/
  `resolver_dropped_summary` above.
- **Tested (fully offline, `tmp_path`):** round-trip of every field
  (including both new ones), defaults-when-omitted, append order preserved
  across multiple `RunLog` instances pointed at the same file,
  corrupted-line resilience (valid lines on either side still read back),
  blank-line handling, missing file, auto-created parent dirs. 10/10 pass.

## What's built: `venue_config.py`
Local JSON config (`venue_config.json`, gitignored — venue-specific, not
code) holding `house_taste_playlist_ids`, `blocklist`, and
`standing_playlists` (mode → playlist id). Closes three of punch-list
category A's gaps at once: before this, `generator.py`'s house_taste
grounding and blocklist enforcement had no source to read from, and
`modes.resolve_target_playlist()`'s `standing_playlists` had no config
either. A non-technical owner edits the JSON file directly — no settings
UI needed for three list/dict fields. Same "corrupted file degrades to
defaults with a stderr warning" pattern as `curation.RecentlyUsedLog` and
`logging_utils.RunLog`.
- **Tested (fully offline, `tmp_path`):** missing file, full/partial field
  loading, corrupted JSON, wrong-shape JSON, `null` field values (a
  hand-edited file might null a field instead of omitting it). 6/6 pass.

## What's built: `pipeline.py`
Wires `generator.generate_candidates()` → `resolver.resolve_tracklist()` →
`curation.apply_house_rules()` into one `run_pipeline()` call, so `app.py`
doesn't have to know the three-stage shape. This module verifies the
*wiring* — does a generated candidate really flow into the resolver, does
the resolver's accepted list really flow into house rules — not output
*quality*, which needs live credentials and a human ear.
- Confirms `wanted_variants` genuinely flows generator → resolver (a live
  test that a "live" variant tag actually stops the resolver's live-take
  penalty from tanking the match).
- `allow_explicit` now threads into `resolve_tracklist()`, not just
  `apply_house_rules()` — since `resolver.py`'s own gate can now reject an
  explicit-only candidate *before* curation ever sees it, `accepted_count`
  and `house_rules.summary["dropped_by_reason"]` correctly reflect where
  each drop actually happened, rather than curation's summary claiming
  drops it never actually made.
- `PipelineResult` gained `resolver_dropped_by_reason` (2026-08-18) — lifted
  straight from `resolve_tracklist()`'s new `summary["dropped_by_reason"]`
  (see `resolver.py`'s entry) so `app.py` can log resolver-stage drop
  reasons alongside curation's, without `app.py` having to reach into
  `resolve_tracklist()`'s internals itself.
- **Tested (fully offline):** happy path, resolver drops a hallucinated
  candidate (now also asserting the new `resolver_dropped_by_reason`),
  explicit filter and artist-diversity cap both apply through the full
  chain, dedupe against an existing-playlist id set, the `wanted_variants`
  flow-through above, resolver preferring a clean version over explicit
  when both exist in the same results, the only-explicit-exists case
  correctly showing up in `accepted_count` rather than curation's
  `dropped_by_reason` (a test that initially asserted the old, wrong
  accounting — see `resolver.py`'s entry above for why the accounting
  changed), and `resolver_dropped_by_reason` being empty when the resolver
  drops nothing. 8/8 pass (was 7).

## What's built: `session_store.py`
Durable, multi-worker-safe storage for `review.py`'s `ReviewSession`
state — SQLite (stdlib, no new dependency), not an in-memory dict, since
Flask requests are stateless and "usable by someone other than the
developer" means two staff members on different devices (or a server
restart mid-review) shouldn't lose work. `ReviewSession` itself stays pure
in-memory logic; this module only serializes/deserializes it.
- `create()`/`load()`/`save()`/`delete()` — `create()` runs
  `ReviewSession`'s own validation (duplicate/missing `track_id`) before
  anything touches the database, so a bad input never gets a session id.
- `save()` on an id that doesn't exist is a silent SQL-UPDATE no-op, not
  an error — documented on the method rather than silently surprising a
  caller who didn't `load()` first.
- `create(tracks, target=...)` / `get_target()` — optional freeform
  metadata (which playlist this batch is headed to) set once at creation
  and fetched independently of the `ReviewSession` itself. Exists so
  `app.py`'s finalize form stays consistently pre-filled across every
  review action (approve/remove/regenerate), not just the page right after
  generating — before this, the resolved target would only have been known
  on the very first page view.
- `prune(older_than_days=7)` — deletes sessions whose `updated_at` is past
  the cutoff (an abandoned `generate()` nobody ever finalized). Uses
  `updated_at`, not `created_at`, so a session actively being reviewed
  can't get swept out from under someone mid-review. Called opportunistically
  from `app.py`'s `index()` route on every page load — cheap enough that no
  separate scheduler/cron is needed.
- **Tested (real SQLite files against `tmp_path`, no mocking):**
  round-trip of every `MatchResult` field, status/note persistence,
  independent sessions not bleeding into each other, a second `SessionStore`
  instance pointed at the same file seeing changes made by a first
  (simulates two worker processes), save-overwrites-not-appends, delete
  semantics, target metadata round-tripping and surviving both `save()`
  calls and a store restart, `prune()`'s cutoff behavior (including that it
  keys off `updated_at` not `created_at`), and — closing the loop on the
  whole reason this module exists — **data survives closing and reopening
  the store** (simulates a server restart). 24/24 pass.

## What's built: `app.py` — the Flask review UI
The web page from the spec: a prompt form, a results table with
approve/remove/regenerate per track, and a finalize step that writes to
Spotify. Route handlers are deliberately thin (`app.py:1`) — parse the
form, call `pipeline.py`/`review.py`/`session_store.py`/`spotify_client.py`,
render a template, redirect. `create_app()` is a factory that takes the
Anthropic/Spotify clients as arguments specifically so tests (and this
session's manual smoke test) can inject fakes — that's what makes the
whole request/response cycle verifiable without live credentials.
- Routes: `GET /`, `POST /generate`, `GET /review/<id>`,
  `POST /review/<id>/track/<id>/{approve,remove,regenerate}`,
  `POST /review/<id>/approve_all`, `POST /review/<id>/finalize`. Plain
  server-rendered HTML forms (Jinja2 templates in `templates/`) — no JS
  framework, no build step.
- `finalize` creates a new playlist (if no `playlist_id` given) or appends
  to an existing one, writes only `session.final_uris()` (approved tracks
  only — pending/removed/regenerate-requested never get written), logs a
  `RunLogEntry`, and deletes the session from `session_store.py`.
- **UX pass for non-technical staff (2026-08-12):** user-mistake cases
  (blank prompt, non-numeric `track_count`/`max_per_artist`, finalizing
  with nothing approved) now flash a plain-language message and redirect
  back to a normal page — they used to `abort(400)` into a raw error page.
  Every approve/remove/regenerate flashes a one-line confirmation naming
  the track. The 404 ("stale review link") and 503 ("not configured yet")
  cases still render a dedicated `error.html`. Remove and Regenerate have
  actual text inputs for reason/note. The regenerate button is explicitly
  labeled "Flag for regeneration" with a caption clarifying it does **not**
  auto-replace the track, since the state machine intentionally stays a
  manual flag (see `review.py`) rather than calling the LLM automatically.
- **Punch-list category A wiring pass (2026-08-12):** the target playlist
  (append vs. create) is now decided on the **generate** step, not
  finalize — a "add to an existing playlist" dropdown (real data, via
  `spotify_client.list_playlists()`) and mode both feed
  `modes.resolve_target_playlist()`, whose result is what makes
  dedupe-against-that-playlist possible during generation instead of only
  being a theoretical feature. That resolved target is stored via
  `session_store`'s `target` metadata so the finalize form on the review
  page stays correctly pre-filled across every review action — not just
  the first page view — while still letting a human override it there.
  `venue_config`'s `house_taste_playlist_ids` and `blocklist` now actually
  reach `generator.py` (fetched best-effort via `spotify_client.py`; a
  fetch failure never blocks generation, it just falls back to no
  grounding/no extra blocklist entries for that run). `curation.RecentlyUsedLog`
  is now instantiated in `create_app()`, its `ids_used_within()` feeds
  every `run_pipeline()` call, and `finalize()` calls `.record()` +
  `.save()` on the tracks actually written — the "don't repeat the same 30
  songs weekly" feature is live now, not just built. `review.approve_all_pending()`
  has a button. `modes.current_day_part()` pre-selects the mode dropdown.
  `index()` opportunistically calls `session_store.prune()` on every load.
  See CLAUDE.md's "Known problems and gaps" section below for what this
  closed and what (category D's dev-server/secret-key notes) is
  deliberately still left alone.
- **Seed-playlist grounding + explicit-avoidance-by-default (2026-08-13,
  driven by the venue priority at the top of this file).** Picking an
  existing playlist to append to (via the dropdown or a mode's standing
  playlist) now *also* adds that playlist to the house-taste sources for
  that generation — combined with any venue-wide `house_taste_playlist_ids`,
  not replacing them. This is the whole "seed playlist" feature: no
  separate seed field, "grow an existing playlist" already means "and use
  it as the style reference." The `allow_explicit` checkbox on `index.html`
  defaulted **unchecked** as of this date — since superseded, see "Form
  defaults flipped (2026-08-18)" below.
- **Scroll-position anchor on approve/remove/regenerate (2026-08-13).** The
  owner flagged that clicking approve/remove on a track reloaded the whole
  review page back to the top — annoying when working through a long list
  one row at a time. Root cause: every single-track action redirects to
  `GET /review/<id>` with a plain POST-redirect-GET, and this app has no
  JavaScript by deliberate design (see this file's UI section), so there
  was nothing preserving scroll position across that reload. Fixed
  entirely server-side, no JS added: each track `<tr>` in `review.html`
  now carries `id="track-{{ track_id }}"`, and `approve_track()`/
  `remove_track()`/`regenerate_track()` redirect with a URL fragment via
  Flask's `url_for(..., _anchor=f"track-{track_id}")` pointing at the
  **same track that was just acted on** — the browser scrolls that row to
  the top of the screen natively on load, so staff immediately see the
  status change took effect without losing their place in the list. (An
  earlier version of this anchored on the *next pending* track instead —
  the owner asked for it to stay on the just-acted-on row instead, which
  turned out simpler too: no need to walk `session.items()` looking for
  what's next, the anchor is just the `track_id` the route already has.)
  `approve_all` was deliberately left without an anchor change — it's a
  bulk action with no single row to point at.
- **`POST /review/<id>/cancel` — leave without writing to Spotify
  (2026-08-13).** There was previously no way off the review page except
  finalize; the owner asked for an explicit "go back home without writing"
  option. Deletes the session outright (`SESSION_STORE.delete()`, the same
  cleanup `finalize()` already does after a successful write) rather than
  just linking back to `/` and leaving the row for `SessionStore.prune()`'s
  7-day sweep — asked the owner directly which behavior they wanted, since
  it changes whether a cancelled review can be resumed via browser-back or
  a saved link (with prune-only cleanup, it technically still could be,
  until the sweep runs). They chose delete-immediately. A new form/button
  on `review.html`, next to (but visually distinct from) the finalize form,
  posts to it and redirects to `/` with a flash confirmation.
- **Small-bug/papercut audit and fixes (2026-08-13).** The owner asked for
  a pass over the app looking for exactly this class of issue — small bugs
  and rough edges, not new features. Found by actually reading the code
  and, for the two crash claims, reproducing them directly before writing
  a fix. All six fixes below verified with zero regressions across the
  full suite. A seventh finding — no loading feedback on the "Generate
  suggestions" button, which needs a small bit of inline JS to fix
  properly, the one deliberate exception to this app's otherwise fully
  JS-free design — was deliberately left unfixed at the owner's call,
  pending a decision on that tradeoff.
  - **`max_per_artist=0`/negative crashed `/generate` with a raw 500** —
    confirmed by reproducing it directly before fixing. `curation.cap_artist_diversity()`
    raises `ValueError` below 1; `index.html`'s `min="1"` is client-side
    only. Now validated server-side with the same friendly-flash pattern
    as every other input error in this route.
  - **`track_count` had no server-side bounds check at all** — `index.html`'s
    `min="1" max="100"` was a client-side hint only; posting the form
    directly let 0, negative, or arbitrarily large values through. New
    `MIN_TRACK_COUNT`/`MAX_TRACK_COUNT` constants enforce the same 1–100
    range server-side.
  - **No error handling around the generate → resolve → curate pipeline in
    `generate()`** — `generator.py` documents raising `RuntimeError` after
    3 failed attempts (Claude refusal/truncation/bad JSON), a real failure
    mode. Now caught, logged to stderr, and flashed as "Something went
    wrong generating suggestions — please try again," redirecting home (no
    session exists yet at that point).
  - **No error handling around the actual Spotify write in `finalize()`** —
    the single most consequential action in the app had zero resilience. A
    manually-typed playlist ID in the review page's override field is free
    text, never validated; a network hiccup or rate limit would also
    crash it. Now caught and flashed with a message telling the human to
    check the playlist ID and retry, redirecting back to `/review/<id>`
    — deliberately *not* deleting the session on failure, so the
    approved/removed selections survive for a retry. **Known residual
    limitation, not fixed here:** if `create_playlist()` succeeds but
    `add_tracks_to_playlist()` fails right after, retrying with the same
    "create new" form creates a *second* playlist rather than resuming the
    first, since the session's target metadata isn't updated mid-failure.
    Judged out of scope for a papercut pass — fixing it properly means
    idempotent-retry design, not a one-line catch.
  - **The "done" page showed a raw playlist ID with no way to actually
    reach the playlist.** `open.spotify.com/playlist/<id>` is a stable,
    deterministic URL needing no extra API call — `finalize()` now builds
    it (`SPOTIFY_WEB_PLAYLIST_URL`) and `done.html` renders it as a real
    "Open the playlist in Spotify" link instead of the bare ID.
  - **No `<meta name="viewport">` tag anywhere.** Any page rendered
    desktop-zoomed on a phone — relevant now that the team is expected to
    reach this over the local network from their own devices, not just the
    one laptop. Added to `base.html`, covers every page since they all
    extend it.
  - **Tested:** 16 new tests — the two confirmed-crash fixes each get a
    parametrized regression test, the LLM-failure fix uses the *real*
    failure code path (an always-refusing fake Anthropic client that
    genuinely exhausts `generator.py`'s 3 retries, not a synthetic
    exception), a bounds-inclusive sanity check (1 and 100 both still
    succeed, not just rejected at the edges), the finalize-write-failure
    path checked three ways (friendly redirect, session survives for a
    retry, no run-log/recently-used side effects from a write that never
    actually happened), the Open-in-Spotify link rendering correctly for
    both the create and append cases, and the viewport tag's presence.
    302/302 pass (was 286).
- **"Half my songs aren't getting approved" investigation (2026-08-18)** —
  see `resolver.py`'s "Live diagnostic" entry for the full investigation
  and conclusion. Three things came out of it:
  - **`ignore_recently_used` checkbox** ("Ignore recently-used songs") — a
    full bypass, not a shorter window. `generate()` passes an empty set
    for `recent_log_ids` instead of calling
    `RECENTLY_USED_LOG.ids_used_within()` when checked. Exists because the
    30-day window (`curation.DEFAULT_RECENT_WINDOW_DAYS`) is right for real
    weekly operation but actively works against rapid same-playlist
    testing — every test run eats into the pool the next one can draw
    from, which is exactly what was compounding the owner's drop-rate
    complaint in the middle of their testing history.
  - **`avoid_obvious` checkbox** ("Prefer lesser-known songs") — wires
    straight into `generator.py`'s `GenerationRequest.avoid_obvious`,
    which has existed since `generator.py` was first built but was never
    actually reachable from the UI (only `modes.py`'s day-part presets set
    it internally, e.g. `late` mode). Unconditionally overrides whatever a
    selected mode would set, same precedence rule already established for
    `allow_explicit` — the human's choice on *this* request wins over
    whatever a mode defaults to. Nudges the LLM's own *generation* toward
    deeper cuts, rather than trying to make *resolution* more lenient
    after the fact — the live diagnostic found this is the more direct
    fix for "I want less popular songs," since resolver-side matching
    turned out not to be the bottleneck.
  - **`resolver_dropped_summary` now logged** alongside the existing
    curation `dropped_summary` — see `pipeline.py`'s and
    `logging_utils.py`'s entries. This is what made the investigation
    itself possible to do with real data instead of guesswork the *next*
    time this question comes up.
  - **Tested:** the bypass checkbox actually including a recently-used
    track, the `avoid_obvious` checkbox's exact prompt line reaching the
    fake Anthropic client's request (and *not* appearing when unchecked),
    the resolver-drop-reason log entry (a candidate with no catalog match
    showing up in `resolver_dropped_summary` while `dropped_summary` stays
    empty — reproducing the exact "empty curation summary but real losses"
    shape that made the original question hard to answer), and both new
    checkboxes rendering on the form. 5 new tests.
- **Form defaults flipped (2026-08-18) — `allow_explicit` and
  `ignore_recently_used` now default to checked/on.** Explicitly requested
  by the owner as a **permanent** policy change (confirmed directly, not
  assumed) — not a temporary testing convenience, so this is not something
  to revert later without being asked. This reverses two previously
  deliberate, spec-driven defaults documented earlier in this file:
  - `allow_explicit` had defaulted unchecked since 2026-08-13, driven by
    the restaurant's then-stated "strictly avoid explicit songs"
    requirement (see "Current real-world priority" at the top of this
    file, now annotated as superseded). The underlying clean-vs-explicit
    preference logic in `resolver.py` is untouched — it still applies
    whenever a run actually has `allow_explicit=False`, which now requires
    unchecking the box rather than being the out-of-the-box behavior.
  - `ignore_recently_used` (added earlier this same day) had defaulted
    unchecked, meaning the 30-day dedupe window applied automatically. It
    now defaults to bypassed — the dedupe logic itself
    (`curation.RecentlyUsedLog`) is unchanged, it just no longer runs
    unless a human unchecks the box for a given generation. Help text on
    `index.html` rewritten to describe the *unchecking* behavior instead
    of the checking behavior, since the default flipped.
  - `avoid_obvious` ("Prefer lesser-known songs") was **not** touched —
    still defaults unchecked.
  - **Tested:** both flipped checkboxes actually render `checked` in the
    HTML by default, and a dedicated test confirming `avoid_obvious`
    wasn't accidentally flipped along with them. 2 new tests.
- **`avoid_obvious`/`ignore_recently_used` toggle state now logged
  (2026-08-19).** Direct trigger: investigating a "the lesser-known-songs
  checkbox doesn't seem to be working" report required asking the owner
  whether the checkbox was even checked for the run in question, because
  the log had no way to answer that. `generate()`'s `RunLogEntry(...)` call
  now passes `avoid_obvious=prefer_less_popular` and
  `ignore_recently_used=ignore_recently_used` — see `logging_utils.py`'s
  entry for the field definitions. Only logged at generate() time (not
  finalize) since that's where the toggle decision is actually made.
  **What this investigation actually found, worth flagging as still
  open:** the owner confirmed the checkbox WAS checked for a real
  "fun house and EDM...hyped up" run, and the strengthened `avoid_obvious`
  wording (see `generator.py`'s entry) still returned mostly genre-canon
  tracks (Daft Punk "One More Time", Avicii, Calvin Harris, FISHER
  "Losing It", both of deadmau5's most iconic tracks) — only 2-3 of 40
  were genuinely deep cuts (Âme "Rej", Laurent Garnier). The frat-party
  fix's live A/B evidence doesn't automatically generalize to every genre;
  house/EDM may need its own diagnosis the same way frat-party did, rather
  than assuming the one fix covers all vibes. Not yet investigated further
  — flagged here so a future session (or the owner) doesn't have to
  re-discover this from scratch.
  - **Tested:** both toggle states correctly recorded as `True` when
    checked and `False` when omitted. 2 new tests.
- **`POST /review/<id>/generate_more` — mid-review follow-up generation
  (2026-08-19).** New feature request: after approving/removing some
  tracks, generate MORE candidates informed by what's already been decided
  — not just what's on the target playlist, but what was explicitly
  rejected. A new form on `review.html` ("Generate more") takes an
  additional prompt, a track count, and its own
  allow_explicit/avoid_obvious/ignore_recently_used checkboxes (same
  defaults as the main form); new candidates are merged into the SAME
  session via `review.ReviewSession.add_tracks()` as new pending rows —
  approve/remove/regenerate-requested tracks are left completely alone.
  - **Approved tracks feed house-taste grounding** — same mechanism the
    seed-playlist feature already uses (`spotify_ops.get_house_taste_sample()`
    on the target playlist), just combined with "Artist - Title" lines
    built from `session.items()` where `status == "approved"`. No new
    grounding mechanism needed, just a new source feeding the existing one.
  - **Removed and regenerate-requested tracks become `generator.py`'s new
    `previously_rejected`** — built from `session.items()`, with each
    removed track's reason (`"(reason: ...)"`if given) or each
    regenerate-requested track's note (`"(wanted instead: ...)"` if given,
    else `"(flagged for regeneration)"`) appended for extra signal. This
    is the actual point of the feature — CLAUDE.md's own iteration on this
    exact page (recently-used bypass, drop-reason logging,
    `avoid_obvious`) shows how much diagnostic value a rejection's stated
    reason carries; this puts that same value to work as generation input.
  - **Dedupe** covers the whole session regardless of status (approved,
    removed, regenerate-requested, and still-pending all count — no point
    re-suggesting a song already decided on either way) plus, if there's a
    target playlist, its existing tracks too — reuses `run_pipeline()`'s
    existing `existing_playlist_ids` mechanism unchanged, no new dedupe
    logic needed.
  - **Logged with its own `action="generate_more"`** in `run_log.jsonl`
    (distinct from `"pending_review"`) so this feature's usage is
    separately analyzable later, following the same
    `dropped_summary`/`resolver_dropped_summary`/`avoid_obvious`/
    `ignore_recently_used` shape as the main `generate()` log entry.
  - **Known limitation, not addressed here:** if a `max_per_artist` cap was
    used on the original `/generate` call, it is NOT re-applied
    cumulatively across rounds — `generate_more()` doesn't expose an
    artist-cap control at all, since `curation.cap_artist_diversity()`
    only counts within the batch it's given, with no awareness of an
    artist's count from a prior round. Judged out of scope for this
    feature's first version rather than silently implying a guarantee
    that doesn't hold; would need `apply_house_rules()` to accept a
    starting per-artist count to do properly.
  - **Tested:** new tracks land as pending without disturbing existing
    decisions, dedupe against the whole session (a re-suggested track
    already in the session, regardless of status, is excluded; a genuinely
    new one still gets added) and against the target playlist's real
    existing tracks, approved tracks reaching the second LLM call's
    prompt, a removed track's reason and a regenerate-requested track's
    note both reaching the prompt correctly formatted, `allow_explicit`/
    `avoid_obvious`/`ignore_recently_used` all independently respected on
    the follow-up round, missing-prompt/invalid-track-count/out-of-range
    friendly errors (mirroring the main form's validation), unknown-
    session 404, not-configured 503, an LLM failure on the *second* call
    specifically (using a fake that succeeds once then always refuses, to
    isolate this route's own error handling from the initial `/generate`
    call that has to succeed first), the "nothing new survived this round"
    friendly message with the session verified unchanged, a run-log entry
    with the distinct `generate_more` action, the redirect anchored to the
    first newly-added track, and the form actually rendering on the review
    page. 20 new tests.
  - **Coverage-verified, not just eyeballed (2026-08-19).** Asked directly
    whether this feature was thoroughly tested — rather than re-asserting
    the count, ran `coverage` (`pip install coverage`, ad hoc — not added
    to `requirements.txt`, just a one-off verification tool) against
    `app.py`/`review.py`/`generator.py` and read the actual missing-line
    report line by line. Found two real, genuine gaps: the best-effort
    `except Exception` bodies around `get_playlist_track_ids()`/
    `get_house_taste_sample()` inside `generate_more()` were never
    exercised — and the *identical* pattern in the original `/generate`
    route had the same gap, pre-dating this session, not a new regression.
    Both documented as "never blocks generation" in a comment but not
    actually proven by a test until now. Fixed with a new
    `FailingReadSpotifyClient` fake (raises on every `_get`, search/writes
    unaffected) and one regression test per route confirming generation
    still succeeds despite the read failure. +2 tests (1 app.py test above
    already counted in the 20; this added 1 more to the original
    `/generate` route's suite). Every other "missing" line the coverage
    report flagged was confirmed to be pre-existing, unrelated code
    (`index()`'s own best-effort blocks, `remove_track`/`regenerate_track`'s
    404 paths, `approve_all`'s zero-count branch, both files'
    `if __name__ == "__main__"` blocks, and `review.py`/`generator.py`'s
    `_demo()` CLI helpers) — not part of this feature, most of it
    predating this whole conversation.
- **"Ignore recently-used songs" relabeled to "Allow recently-used songs"
  (2026-08-19).** Real UX confusion the owner caught: the old label read as
  "checking this excludes recently-used songs," when checking it actually
  does the opposite (bypasses the exclusion, so they're allowed back in —
  which is also the current default, see "Form defaults flipped" above).
  Fixed on both `index.html` and `review.html`'s "generate more" form —
  now parallel to "Allow explicit tracks" right above it, same
  checked-means-allowed reading. **Deliberately did NOT rename the
  underlying `id`/`name`/`RunLogEntry` field** (still `ignore_recently_used`
  everywhere in code and in `run_log.jsonl`) — real data is already logged
  under that name, and renaming it would fragment the log's schema for a
  fix that's purely about the user-facing label text.
  - **Tested:** both pages render "Allow recently-used songs" and do NOT
    render the old "Ignore recently-used songs" text. 1 new test.
- **Tested (Flask's test client, real `tmp_path`-backed `SessionStore` +
  `RunLog` + `RecentlyUsedLog`, fake combined Anthropic/Spotify clients —
  no network):** every route including friendly-redirect validation
  errors, friendly 404/503 pages, flash confirmations, the reason/note
  inputs actually present in the rendered HTML, max-per-artist reaching
  the table, finalize creating vs. appending, finalize writing only
  approved tracks, session deletion after finalize, `approve_all` leaving
  removed/regenerate-requested tracks alone, house_taste/blocklist content
  actually reaching the fake Anthropic request payload, dedupe against an
  explicit playlist id AND against a mode's standing playlist, explicit
  choice winning over mode per `resolve_target_playlist()`'s documented
  precedence, the finalize form pre-fill surviving via stored target
  metadata, recently-used tracks excluded from generation and recorded
  after finalize (round-tripped through a fresh `RecentlyUsedLog` instance
  to confirm the `.save()` actually happened), the playlist picker
  rendering real entries and degrading to empty (not crashing) when no
  Spotify client is configured, the day-part preselect (computed against
  the real current time so the assertion works regardless of when tests
  run), opportunistic pruning on `index()`, the target playlist itself
  being used as a house-taste seed, that seed combining (not replacing)
  with venue-wide `house_taste_playlist_ids`, and generating with no
  target playlist chosen falling back to venue-wide grounding only. 45/45
  pass. One real bug the tests caught along the way: `make_app()`'s test
  fixture didn't isolate `RecentlyUsedLog`/`venue_config` into `tmp_path`
  at first, so a stray `recently_used.json` from earlier manual testing in
  the real project directory silently marked `t1`/`t2` as "recently used"
  and zeroed out several tests' expected results — fixed by pointing every
  piece of local state at `tmp_path`, matching the pattern already used
  for `SessionStore`/`RunLog`. Scroll-anchor fix above added 4 more
  covering the redirect's exact `#track-<id>` target for each of
  approve/remove/regenerate and the row `id` actually being present in
  rendered HTML. The cancel-review feature above added 8 more: redirect
  target, flash confirmation, the session actually deleted (not just
  redirected away from), the review URL genuinely 404ing afterward (not
  resumable), confirming zero Spotify write calls happen, confirming no
  finalize-style run-log entry gets written (`generate()`'s own
  `"pending_review"` log entry is separate, pre-existing behavior — caught
  by an initial wrong test assumption that a naive "no log entries at all"
  check would have failed for the wrong reason), unknown-session 404, and
  the button actually rendering on the page. 57/57 pass (was 45).
- **Also manually smoke-tested against a real running dev server four
  times** (not just the Flask test client) — original route wiring, the
  UX pass, the category-A wiring pass, and the explicit/seed-playlist
  priority pass (confirmed the checkbox defaults unchecked in real HTML,
  and that "WAP (Clean)" — not "WAP" — is what actually shows up in review
  when both an explicit and clean search result exist), each confirming
  the actual rendered behavior (not just assertions) over real HTTP via
  `curl`. This is as close to "click through in a browser" as this environment allows
  without live credentials — see `demo.py` for a version you can actually
  click through yourself, no credentials needed.
- **Not runnable for real yet** — `if __name__ == "__main__"` builds real
  `anthropic.Anthropic()` and `spotify_client.get_client()` clients, which
  needs the credentials that are still on hold (see below).

## What's built: `demo.py`
A standalone, runnable demo of the review UI with a fixed fake catalog
standing in for both Spotify and Claude — `python demo.py` then open
`http://127.0.0.1:5000`, no credentials needed. Deliberately shows off
real behaviors, not just a static page: two same-artist songs (set "Max
tracks per artist" to 1 to see the cap drop one), "WAP" has both an
explicit and a clean search result — "Allow explicit tracks" defaults
**unchecked**, so leaving it that way shows the resolver picking the clean
edit automatically instead of losing the song (check the box to see the
explicit cut instead), one suggested song blocklisted via `venue_config`
(never reaches review no matter what toggles are set), one suggested song
with no matching entry in the fake Spotify catalog (silently disappears,
same as a real hallucinated song), a "Brunch" mode wired to a standing
playlist that already contains one of the suggested tracks (watch it get
excluded as a dupe, and the finalize form come back pre-filled with that
playlist instead of "create new"), and a real playlist picker dropdown on
the form itself (`spotify_client.list_playlists()` against the fake
catalog, not hardcoded) that also seeds house-taste grounding from
whichever playlist you pick — the "grow an existing playlist" / seed
feature. Uses its own `demo_review_sessions.db` /
`demo_run_log.jsonl` / `demo_recently_used.json` (all gitignored) so it can
never collide with a real run.

## Known weak spots to watch
- Re-recordings / "Version" collisions ("Taylor's Version", single vs album cut)
  resolve to *a* correct track but maybe not the *specific* one intended.
- `wanted_variants`: if a prompt legitimately wants live/instrumental, pass those
  words in so they aren't penalized. The LLM layer should emit this set.
- Search cap (~10): a real track buried behind karaoke uploads can be missed;
  tighten the query rather than raising the limit.
- LLM hallucination (invented songs → dropped in resolution) and obviousness bias
  (leans to famous tracks). Grounding on the restaurant's own playlists counters
  both.
- No audio-feature targeting: precise BPM/energy is impossible; rely on the LLM's
  semantic sense of energy.

## Next steps (in order)
1. ~~**LLM generation layer.**~~ **Done** — `generator.py`.
2. ~~**OAuth + write scaffold.**~~ **Code done** — `spotify_client.py`
   (auth config, `create_playlist`, batched `add_tracks_to_playlist`,
   paginated `get_playlist_track_ids`), fully mock-tested. **Live
   verification is what's left**, and that's blocked on Spotify
   credentials — see "On hold" below.
3. ~~**Dedupe against the target playlist.**~~ **Done** — `curation.py`.
4. ~~**Review UI + house rules.**~~ **Done, including the UI.** House rules
   in `curation.py`, the approve/remove/regenerate state machine in
   `review.py`, and — as of this round — an actual Flask web UI (`app.py`
   + `templates/`) sitting on top of it, backed by `pipeline.py` (wiring)
   and `session_store.py` (durable, multi-worker-safe persistence, chosen
   over an in-memory dict because "usable by someone other than the
   developer" was a stated goal). Chose Flask over Streamlit specifically
   for that multi-user framing — see conversation history for the tradeoff
   discussion.
5. ~~**Polish: day-part modes, target-playlist resolution, a scheduling
   window lookup, logging.**~~ **Done** — `modes.py` and
   `logging_utils.py`. **Not done:** actually wiring `current_day_part()`
   into a recurring job (cron/launchd/etc.) — that's local machine
   configuration, not something to script without being asked.

**Every agent-doable piece of the original roadmap is now built.** What's
left is entirely live-credential verification (see below) plus whatever
new polish ideas come up once the tool is actually used against a real
account — there's no more "buildable without credentials" backlog.

## Live API verification — status as of 2026-08-13 (no longer on hold; owner is actively testing with a personal Spotify account first, restaurant account later — see below)
- ~~**`resolver.py` live run**~~ **Done** — 4/5 accepted, 1/5 correctly
  dropped against the real Search API. See `resolver.py`'s "Tested / not
  tested" section above for specifics.
- ~~**`generator.py` live run**~~ **Done** — real Claude API call, 21/21
  generated candidates well-formed, 0 malformed, 0 blocked, output quality
  looked good on the demo prompt (real, era-appropriate soul/Motown
  tracks, no obvious hallucinations). See `generator.py`'s section above.
- **Credentials setup:** registered under the owner's *personal* Spotify
  account for now, deliberately — plan is to validate the tool works, then
  switch to the restaurant's account later by adding it as an authorized
  user under the same Developer App and re-running the OAuth login (no
  re-registration needed). See the "simple path" discussion earlier this
  session for the full switch-over steps when that happens. **Whichever
  account owns the Developer App registration must keep Premium active
  indefinitely** (Dev Mode requirement) — if ownership should end up
  living with the restaurant long-term rather than depending on the
  owner's personal account, that's a second, separate Developer App
  registered directly under the restaurant's account, not a "switch" of
  the existing one.
- **Credential hygiene note:** the Spotify Client Secret and the
  `ANTHROPIC_API_KEY` were both pasted directly into chat during setup —
  flagged to the owner in the moment, stored in a local gitignored `.env`
  instead of restating them further, but both are still sitting in the
  conversation transcript. Recommended the owner rotate both once initial
  testing is done (Spotify dashboard → regenerate client secret; Anthropic
  Console → roll the API key). Don't assume that's happened — if you're
  troubleshooting an auth failure later, "the credentials were rotated
  after this session and `.env` is stale" is a real possible cause.
- ~~**`spotify_client.py` live run**~~ **Done — and it found a real bug.**
  `POST /me/playlists`, `POST /playlists/{id}/items`, and `GET
  /playlists/{id}/items` all confirmed live, round-tripped end to end
  (created a real playlist, added a real track, read it back, IDs
  matched). The Feb-2026 endpoint moves documented in this file were
  **accurate** — including the `item`-not-`track` field rename, confirmed
  live. What actually broke on the first attempt (`GET .../items` →
  400 with a raw HTML body, not Spotify's usual JSON error shape) was a
  bug in **our own code**, not Spotify's API: `get_playlist_track_ids()`,
  `list_playlists()`, and `get_house_taste_sample()` were all passing
  pagination (`limit`/`offset`) to spotipy's `_get()` via `payload=`, which
  becomes the JSON **request body** — correct for POST, wrong for GET,
  where pagination has to be real URL query params (`args=`). Fixed all
  three; see `spotify_client.py`'s module docstring for the full
  diagnostic. **Lesson for next time:** an HTML (not JSON) 400 body from a
  Spotify API call is a strong signal the request itself is malformed
  before it reaches routing — check the request shape, not the endpoint
  path, first.
- ~~**`app.py` live run**~~ **Done — real end-to-end generate → review →
  finalize, creating an actual playlist on the owner's account, worked
  well** (owner's words: "worked incredibly well"). One more real bug
  found and fixed during this: the "grow an existing playlist" dropdown on
  `index.html` was empty despite the owner having 29 real playlists.
  `GET /me/playlists` has its **own, smaller page-size cap than
  `/playlists/{id}/items`** — confirmed live, `limit=51` and `limit=100`
  both 400 with `"Invalid limit"`, `limit=50` is the real ceiling for this
  specific endpoint. `list_playlists()` had been reusing
  `MAX_ITEMS_PER_ADD_CALL` (100, correct for `/playlists/{id}/items`) for
  this call too — added a separate `MAX_PLAYLISTS_PER_LIST_CALL = 50`
  constant instead of sharing one page-size assumption across endpoints
  with different real limits. **Also fixed while in there:** the failure
  was completely silent — `index()`'s best-effort `except Exception:
  playlists = []` swallowed it with no error surfaced anywhere, and it was
  only spotted because the owner noticed the empty dropdown and asked, not
  because anything told either of us something had failed. Added
  `print(..., file=sys.stderr)` logging to all four best-effort
  `except Exception` blocks in `app.py` (`index()`'s prune + playlist
  listing, `generate()`'s dedupe-id fetch + house-taste fetch) so a
  developer watching the server log can see these failures next time,
  without changing the user-facing behavior (still degrades gracefully,
  never blocks the page).
- All ten modules pass their full mock/unit test suites (39 resolver + 13
  generator + 41 curation + 40 review + 39 spotify_client + 24 modes + 10
  logging_utils + 6 venue_config + 8 pipeline + 24 session_store + 104 app
  = **349 tests**, up from 269 after ten post-live-verification passes:
  `resolve_tracklist()` parallelized (+5), the review-page scroll-anchor
  fix (+4), the cancel-review feature (+8), the small-bug/papercut audit
  fixes (+16), the recently-used-bypass/drop-reason-logging/avoid_obvious
  investigation (+8: 2 resolver, 1 pipeline, 5 app), flipping the
  `allow_explicit`/`ignore_recently_used` form defaults (+2), strengthening
  the `avoid_obvious` prompt wording after a live A/B diagnosis (+2
  generator), logging whether `avoid_obvious`/`ignore_recently_used` were
  actually checked per run (+2 app), the "generate more" mid-review
  follow-up feature (+31: 8 review, 2 generator, 21 app — including a
  coverage-verification pass that found and closed 2 real gaps, one of
  them pre-existing in the original `/generate` route), and relabeling the
  "Allow recently-used songs" checkbox for clarity (+1 app) — see their
  entries above) independent of all of the above. **Every module in
  the pipeline is now
  live-verified against real Spotify + Claude accounts.**
- **Live test artifact:** a real playlist was created on the owner's
  personal Spotify account during this verification
  (`0Ib6daWjc9TsEdadZE2tkR`, one track added — "Ain't No Sunshine" by Bill
  Withers). Left in place pending the owner's call on whether to delete it.

## Known problems and gaps (punch list, opened 2026-08-12)
Categories A and D below were **fixed 2026-08-12** (see `app.py`'s and
`session_store.py`'s "What's built" sections above for the details);
categories B and C remain **on hold at the owner's request** — snapshots,
not active work.

### A. ~~Built-and-tested features that `app.py` never actually calls~~ — FIXED
All seven entries closed in one pass. Confirmed via `grep` before writing
"fixed" (same discipline as when these were first flagged, not just taking
the summary on faith):
- `generator.GenerationRequest.house_taste` — now populated via
  `spotify_client.get_house_taste_sample()` against `venue_config.house_taste_playlist_ids`
- `generator.GenerationRequest.blocklist` — now populated from `venue_config.blocklist`
- `curation.dedupe_against_playlist` — now runs for real; `generate()` calls
  `spotify_client.get_playlist_track_ids()` before generating whenever a target playlist is known
- `curation.RecentlyUsedLog` — now instantiated in `create_app()`, feeds every `run_pipeline()`
  call, and `finalize()` calls `.record()` + `.save()` after a successful write
- `modes.resolve_target_playlist()` — now called from `generate()`; its result is
  what actually decides append-vs-create, and is persisted via `session_store`'s new `target` metadata
- `review.approve_all_pending()` — now has a button + `POST /review/<id>/approve_all` route
- `modes.current_day_part()` — now called from `index()`, pre-selects the mode `<select>`

### B. Known product/design limitations (already documented elsewhere, not new)
| Gap | Why it matters | Fix needed |
|---|---|---|
| Re-recording collisions on long titles slip past `TITLE_MIN` (e.g. a "(Taylor's Version)" tag on a long title) | Resolves to the *correct song* but possibly the *wrong pressing* — see `test_LIMITATION_rerecording_wins_outright_when_original_is_absent_on_long_titles` in `test_resolver.py` | Would need explicit re-recording-tag detection in `resolver.py`'s title normalization — not started, and not a small change |
| Search-result cap (~5–10 in Spotify Dev Mode) | A real track buried behind karaoke uploads in the top results could be missed entirely | No code fix possible; only mitigation is tighter queries (partially done via strict-then-loose fallback already in `resolver.py`) |
| No audio-feature targeting (BPM/energy) | Permanent architectural limit — Spotify removed those endpoints for this app tier | No fix by design; LLM's semantic sense of energy is the deliberate substitute (see "Core architecture decision" at the top of this file) |

### C. Assumptions unverified until live credentials exist
| Gap | Why it matters | Fix needed |
|---|---|---|
| Feb-2026 Spotify endpoint/field-name assumptions in `spotify_client.py` (`POST /me/playlists`, `/playlists/{id}/items`, `item` vs `track` field) | Defensively coded (checks both old and new field names) but never confirmed against a real account | Nothing to fix yet — the first live run *is* the verification step; if wrong, `spotify_client.py` is the one file to patch, by design |
| Real generation quality is completely unknown | `generator.py` has never produced output from the real Claude API — can't judge whether prompts/grounding actually produce good playlists | N/A until a live run; likely needs prompt tuning in `generator.py` afterward based on real output |

### D. UI/UX gaps already flagged in conversation
Two of the original four items — the ones with a real code fix available
right now — were closed 2026-08-12:
- ~~No playlist picker~~ — **FIXED.** `spotify_client.list_playlists()` +
  a real dropdown on `index.html` (falls back to "create new" only if the
  Spotify client isn't configured or the listing call fails — never
  crashes the page).
- ~~No expiry/cleanup for abandoned review sessions~~ — **FIXED.**
  `SessionStore.prune(older_than_days=7)`, called opportunistically from
  `index()` on every page load.

The other two are left alone on purpose — not because they're hard, but
because their own "fix needed" already says now isn't the right time:
| Gap | Why it matters | Fix needed |
|---|---|---|
| Flask's built-in dev server, not a production WSGI server | Fine for the current "one restaurant, run from a laptop, low-volume" design goal | Only needed if usage ever grows past that — swap `app.run()` for gunicorn/similar then, not now |
| Hardcoded dev Flask `secret_key` unless `FLASK_SECRET_KEY` env var is set | Low risk today — no auth or sensitive data depends on it, only flash messages — but worth doing properly if this ever runs somewhere less private than a personal laptop | Set `FLASK_SECRET_KEY` before running anywhere shared |

## Guardrails / judgment
- Keep it **low-volume and human-in-the-loop**. Spotify is actively restricting
  automation/AI; a firehose of unattended writes risks the app's API access being
  throttled or revoked. (This is separate from music-licensing, which the owner has
  set aside.)
- Keep the Spotify layer isolated so the inevitable next API change is a one-file
  patch.

## Companion docs (download into this folder if you want them here)
- `spotify-ai-playlist-tool-spec.md` — full build spec + risk map.
- `ai-playlist-options-comparison.md` — buy-vs-build options comparison.
