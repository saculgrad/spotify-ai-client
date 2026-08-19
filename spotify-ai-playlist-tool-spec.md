# AI Playlist Tool for Spotify — Technical Spec & Risk Map

A working specification for a tool that lets restaurant staff generate and extend
Spotify playlists from a plain-language description, built around the only design
that survives Spotify's 2024–2026 API lockdown: **the LLM chooses the songs, and
Spotify is used only to search, validate, and save.**

> **Read this first.** Spotify has changed its Web API repeatedly and aggressively
> (Nov 2024, Feb 2025, Apr 2025, Nov 2025, Feb 2026). Every endpoint and limit in
> this document should be re-verified against the live changelog before you build,
> because the direction of travel is *more* restrictive, not less. Treat this spec
> as the shape of the solution, not a frozen contract.

---

## 1. What the tool does

1. A staff member describes what they want ("warm 60s–70s soul for Sunday brunch,
   nothing too loud, no explicit tracks") or pastes a few seed songs.
2. The tool optionally grounds the request in the restaurant's **existing
   playlists** so results match the house taste.
3. An LLM returns a candidate tracklist as structured data (title + artist).
4. The tool resolves each candidate to a real Spotify track via the Search API,
   scoring and filtering matches.
5. The tool shows the resolved list for a quick human review.
6. On approval, it creates a new playlist or appends to an existing one.

The AI lives entirely in step 3. Spotify is a dumb executor in steps 4–6. This is
deliberate: the recommendation engine and audio-analysis endpoints that used to do
step 3 are gone for apps like yours, so nothing here depends on them.

---

## 2. Architecture

```
[ Staff UI ]  ──►  [ Backend service ]  ──►  [ LLM API ]        (song generation)
                          │
                          ├──►  [ Spotify Web API ]             (search, create, append)
                          │
                          └──►  [ Local store ]                 (tokens, taste corpus,
                                                                 bl\allow-lists, logs)
```

**Components**

- **Staff UI** — a small web page. Prompt box, a few toggles (track count, era,
  language, explicit on/off, target: new playlist vs. append to an existing one),
  a results table with approve/remove/regenerate.
- **Backend service** — holds the Spotify client secret, runs OAuth, calls the LLM,
  runs the resolver. Never put the client secret in the browser.
- **Local store** — refresh token(s), a cached snapshot of the restaurant's existing
  playlists (the "taste corpus"), a blocklist/allowlist, and a log of what was
  generated (for dedupe and debugging).

You can collapse this to a single script for a personal MVP, but the moment more
than one person uses it or it runs unattended, you want the backend/store split.

---

## 3. Authentication (the fiddliest part — look here extra hard)

**Flow:** OAuth 2.0 Authorization Code with PKCE. The owner logs in once; you store
the refresh token and mint short-lived access tokens from it thereafter.

**Scopes you'll need**
- `playlist-modify-public`, `playlist-modify-private` — create and append.
- `playlist-read-private`, `playlist-read-collaborative` — read existing playlists
  to build the taste corpus.
- `user-read-private` — read the account's market/country for availability filtering.

**Redirect URI:** Spotify no longer accepts `localhost`. Register the loopback IP
**exactly** as `http://127.0.0.1:8888/callback` (or your chosen port). A `localhost`
value fails with a redirect-uri mismatch.

**Things that will bite you**
- **Refresh-token lifecycle.** Access tokens last ~1 hour. Your service must refresh
  silently and handle refresh failure (revoked access, password change) by
  re-prompting login. Build this on day one; retrofitting it is painful.
- **Premium requirement.** Development-Mode apps now require the *owner* account to
  hold active Spotify Premium. If that subscription lapses, the app stops working
  until it's renewed. Whoever owns the developer app must keep Premium live.
- **The 5-user cap.** New Development-Mode apps are limited to five authorized users
  and one Client ID per developer. For one restaurant on one account this is fine —
  but you cannot casually share the tool beyond five logins, and "extended quota"
  (the path past that) realistically requires a registered business with ~250k
  monthly active users, which you will never hit. Design for a single shared
  account, not per-employee logins.
- **Token storage security.** The refresh token is a long-lived key to the account.
  Encrypt it at rest; don't commit it; don't log it.

---

## 4. Step-by-step data flow

### 4.1 Build the taste corpus (grounding)
- `GET /me/playlists` to list the restaurant's playlists (paginate; 50 per page).
- For a chosen subset, `GET /playlists/{id}/items` to pull tracks (paginate; note the
  field is now `items`/`item`, not `tracks`/`track`, post-Feb 2026).
- Reduce to a compact representation: e.g. up to ~200 sampled `Artist — Title`
  lines, plus any genre/era labels you keep yourself. This becomes few-shot context.
- **Cache it.** Rebuild weekly or on demand, not on every request — it's the biggest
  source of latency and API calls.

### 4.2 Generate candidates (LLM)
- Send: a system prompt (rules), the taste corpus (few-shot), the user's request,
  and the parameters (count, era, language, explicit policy, "avoid obvious hits",
  artist-diversity rule, a blocklist of banned tracks/artists).
- **Demand structured output**: a JSON array of `{title, artist, reason?}`. Instruct
  the model to return JSON only, no prose, no markdown fences, then parse defensively
  (strip fences if present, validate schema, reject and retry on malformed output).
- Over-generate: ask for ~1.4× the target count so you still hit the target after
  drops in resolution.
- Low-to-moderate temperature for consistency; raise it if outputs feel samey.

### 4.3 Resolve candidates to real tracks (the quality bottleneck — look here extra hard)
For each `{title, artist}`:
- `GET /search?type=track&q=...&market=<country>&limit=<n>`.
- **Query construction matters.** Prefer field filters: `q=track:"X" artist:"Y"`.
  Fall back to a looser `q=X Y` if the strict query returns nothing.
- **Score the results** rather than taking result #0:
  - Fuzzy-match title and artist (normalize case, punctuation, diacritics, strip
    "(Remastered 2011)", "- Live", "feat." noise before comparing).
  - Penalize junk variants: karaoke, tribute/cover bands, "sped up", "nightcore",
    "8-bit", instrumental-of-a-vocal-track, unless explicitly wanted.
  - Prefer studio originals over live/remaster duplicates unless asked.
  - Confirm the artist actually matches (title collisions across different artists
    are common).
- **Availability check.** Respect `is_playable` / `available_markets` for the
  restaurant's market; a track that resolves may not be playable locally. Consider
  track relinking via the `market` parameter.
- **Drop policy.** If nothing clears your score threshold, drop the candidate and log
  it. Silent wrong matches are worse than an honest gap.
- **Dev-Mode search cap.** In Development Mode, search results are reportedly capped
  low (on the order of 5–10 results per query). Fewer candidates to match against
  makes scoring harder; verify the current cap and tune your matching accordingly.

### 4.4 Deduplicate
- Within the batch (same song can appear twice with different URIs).
- Against the **target playlist's existing tracks** (fetch and diff before adding).
- Optionally against a rolling "recently used" log so the same 30 songs don't get
  re-added every week.

### 4.5 Assemble the playlist
- **Create:** `POST /me/playlists` (the old `POST /users/{id}/playlists` was removed
  in Feb 2026). Returns the playlist id and a `snapshot_id`.
- **Append:** `POST /playlists/{id}/items` (this moved from `/tracks` to `/items`).
  **Max 100 URIs per call** — batch if you have more.
- **Reorder / replace:** `PUT /playlists/{id}/items`. **Remove:**
  `DELETE /playlists/{id}/tracks`.
- Set name, description, and optionally a cover image.

### 4.6 Human review
- Always show the resolved list before it goes live: approve, remove individual
  tracks, or regenerate. This single step is what makes the whole thing reliable
  despite LLM hallucination, and it's cheap to build.

---

## 5. Endpoint reference (verify against the live changelog)

**Still available / used here**
- `GET /me` — account + market.
- `GET /me/playlists`, `GET /playlists/{id}/items` — read for grounding & dedupe.
- `GET /search` — resolve candidates.
- `POST /me/playlists` — create.
- `POST /playlists/{id}/items` — append (was `/tracks`).
- `PUT /playlists/{id}/items` — reorder/replace.
- `DELETE /playlists/{id}/tracks` — remove.

**Gone / restricted — do NOT design around these**
- Recommendations (seed-based) — deprecated. This was the classic "AI playlist"
  engine; its removal is *why* the LLM does song selection now.
- Audio features / audio analysis (tempo, energy, key, danceability) — deprecated.
  No reliable BPM/energy filtering is possible anymore.
- Related artists, artist top tracks, new releases, several bulk-metadata and
  public-profile endpoints — removed or restricted in Dev Mode.

---

## 6. Every downside, and how much it should worry you

| Risk | Severity | Mitigation |
|---|---|---|
| **LLM hallucinates non-existent songs** | Medium | Over-generate; resolver drops non-matches; human review. |
| **Wrong version resolved** (live/remaster/karaoke/cover) | Medium–High | Variant-aware scoring; prefer studio originals; blocklist junk keywords. |
| **Obviousness bias / repetition across runs** | Medium | Few-shot on house catalog; "avoid obvious hits"; recently-used dedupe; artist-diversity cap. |
| **No audio-feature targeting** (precise BPM/energy gone) | Medium | Rely on LLM's semantic energy sense; accept approximation; don't promise exact tempo control. |
| **Freshness / training cutoff** (newest releases missing or faked) | Medium | Treat brand-new music as unreliable; verify current releases manually. |
| **Dev-Mode search result cap (5–10)** | Medium | Tighter queries; strict-then-loose fallback; accept a higher drop rate. |
| **Market/availability mismatch** | Low–Medium | Filter on `is_playable`/market; use `market` param and relinking. |
| **Explicit-content filtering imperfect** | Low–Medium | Filter on the `explicit` flag; still eyeball results; keyword blocklist. |
| **Genre taxonomy changed** (Spotify consolidated its genre set) | Low | Don't lean on Spotify genre strings; drive taste from your own labels + corpus. |
| **OAuth/refresh failure** | Medium | Robust silent refresh + re-auth path; alerting when it breaks. |
| **Premium lapse disables the app** | Medium | Keep owner account on Premium; monitor for the failure signature. |
| **Rate limiting (429s)** | Low–Medium | Respect `Retry-After`; backoff; cache the corpus; batch adds. |
| **Developer-terms / anti-automation risk** | **High — look here extra hard** | See §7. |
| **Ongoing API churn / maintenance** | **High** | Budget for periodic breakage; pin to the changelog; keep the resolver modular. |
| **Token/secret leakage** | High | Encrypt tokens at rest; secret stays server-side; never in the browser or repo. |

---

## 7. The two risks worth losing sleep over

**7.1 You are building automation on a platform actively hostile to automation.**
Spotify's own justification for the 2024–2026 lockdown is curbing "automation and
AI" risk. A tool that programmatically generates and writes playlists is exactly the
usage pattern they've been tightening against. The realistic exposure isn't a
lawsuit — it's that your **app's API access can be throttled or revoked**, or that a
future changelog removes a piece you depend on. Keep the tool low-volume, human-in-
the-loop (not a firehose of unattended writes), and don't build anything you can't
afford to have stop working on a quarter's notice. This is separate from the music-
licensing question you've already set aside; this is about the *developer* terms
governing the app itself.

**7.2 Maintenance is a standing cost, not a one-time build.** This API has had
breaking changes roughly every few months for two years running. Whatever you build
will need occasional repair. Structure the code so the Spotify layer (endpoints,
field names, auth) is isolated behind one module you can patch quickly when — not
if — the next changelog lands. The resolver and the LLM layer should not need to
know about Spotify's field renames.

---

## 8. Quality levers (what actually moves the needle)

In rough order of impact:
1. **Ground on the house catalog.** Few-shot the LLM with a sample of the
   restaurant's real playlists. This is the single biggest quality lever and the
   closest substitute for the recommendation engine you've lost.
2. **Variant-aware resolver.** Good title/artist matching and junk-variant rejection
   is the difference between a usable playlist and one peppered with karaoke tracks.
3. **Blocklist / allowlist.** A short banned list (tracks, artists, keywords) that
   staff can extend removes recurring annoyances permanently.
4. **Dedupe against recent output.** Stops the tool from serving the same 30 songs.
5. **Artist-diversity cap.** e.g. max 2 tracks per artist per playlist.
6. **Popularity band.** Optionally bias toward a recognizable-but-not-overplayed
   range using the track `popularity` field (this one is still available).

---

## 9. Suggested stack & effort

**Stack (pick what you know)**
- Python: `spotipy` handles OAuth/refresh and endpoints; any LLM SDK for generation.
- JS/TS: `spotify-web-api-node` or raw `fetch`; LLM SDK of choice.
- LLM: a mid-tier model is plenty (song-name generation is not a hard task). Budget
  models (Flash/Haiku/mini tiers) work well and cost a fraction of a cent per
  playlist. Reserve a stronger model only if outputs feel weak.
- Hosting: a small always-on service, or run on demand from a laptop for a true MVP.

**Effort**
- MVP (prompt → resolved list → create playlist), developer-built: ~1–3 days.
- Polished internal tool (auth persistence, review UI, dedupe, blocklist,
  append-to-existing, explicit filter): ~1–3 weeks part-time.
- Non-developer route: use a no-code orchestrator or a Spotify MCP server driven by
  a chat assistant, trading control for far less code.

**Running cost:** effectively a few dollars a month (LLM pennies per playlist +
cheap hosting). Spotify API is free; the real cost is your time and the maintenance
tail in §7.2.

---

## 10. Build phases

- **Phase 0 — spike:** hard-coded prompt, generate → resolve → print. Proves the
  matching quality, which is the make-or-break. Measure your drop rate and eyeball
  20 playlists before building anything else.
- **Phase 1 — MVP:** real OAuth, create/append, minimal UI, human review.
- **Phase 2 — quality:** taste-corpus grounding, variant-aware resolver, dedupe,
  blocklist, explicit filter.
- **Phase 3 — polish:** append-to-specific-playlist, scheduling, multiple house
  "modes" (brunch/dinner/late), logging and a small dashboard.

---

## 11. Decisions to make before you start

- One shared restaurant account (recommended) vs. per-staff logins (burns your
  5-user cap fast).
- New playlist per request vs. topping up a small set of standing playlists.
- How much human review — every track, or just a glance and go.
- Which existing playlists represent "house taste" for grounding.
- How aggressive the junk-variant filtering should be (some venues are fine with
  live versions; some aren't).
- Who owns maintenance when the next Spotify changelog breaks something.
