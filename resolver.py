"""
resolver.py — Track resolver spike (Phase 0)

Purpose
-------
Turn an LLM's candidate list of {"title", "artist"} guesses into REAL Spotify
tracks, choosing the right version and honestly dropping anything it can't match
well. This is the quality-critical piece of the tool: if matching is bad, the
whole playlist is bad. Run this in isolation first, on a handful of prompts, and
eyeball the output + drop rate before building anything else around it.

What it does
------------
1. Normalizes titles/artists (case, diacritics, noise like "(Remastered 2011)").
2. Searches Spotify (strict field query first, loose fallback).
3. Scores every returned track: title similarity, artist similarity, and
   penalties for junk variants (karaoke / cover / tribute / sped up / etc.).
4. Applies hard gates (wrong artist or wrong title => reject) + a score
   threshold, then picks the best survivor or drops the candidate with a reason.
5. Dedupes and reports an accept/drop summary so you can measure match quality.

The scoring is deliberately TRANSPARENT (every sub-score is returned) because the
entire point of this spike is to let you tune the knobs at the top of the file.

Setup
-----
    pip install spotipy rapidfuzz unidecode

    export SPOTIPY_CLIENT_ID=...           # from developer.spotify.com/dashboard
    export SPOTIPY_CLIENT_SECRET=...
    export SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback   # NOT localhost

Auth note (2026): Spotify has been moving metadata endpoints away from the
app-only Client Credentials flow, so this uses user auth (Authorization Code).
The resolver functions themselves take an already-authenticated `sp` client, so
they're auth-agnostic and easy to unit-test with a fake client.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from rapidfuzz import fuzz
from unidecode import unidecode

# ─────────────────────────────────────────────────────────────────────────────
# TUNING KNOBS — this is what you adjust while eyeballing results
# ─────────────────────────────────────────────────────────────────────────────

WEIGHT_TITLE = 0.5          # how much title similarity counts toward the score
WEIGHT_ARTIST = 0.5         # how much artist similarity counts toward the score

ARTIST_MIN = 0.60           # hard gate: below this, it's the wrong artist -> reject
TITLE_MIN = 0.60            # hard gate: below this, it's the wrong song  -> reject
ACCEPT_THRESHOLD = 0.72     # final score needed to accept a match (0..1)

SEARCH_LIMIT = 10           # Dev Mode caps search results low (~10). Don't raise blindly.

MAX_RESOLVER_WORKERS = 8    # resolve_tracklist() searches candidates concurrently via a thread
                             # pool instead of one at a time — each resolve_track() call is
                             # dominated by network wait, not CPU, so threads help despite the
                             # GIL. Capped well under a full batch so we don't fire 20+ requests
                             # at Spotify at once and risk 429s.

# Junk / unwanted variants. If one of these words appears in the FOUND track's
# title but the user did NOT ask for it, subtract the penalty. Tune freely.
VARIANT_PENALTIES = {
    "karaoke": 0.60,
    "made famous by": 0.60,
    "tribute": 0.55,
    "cover": 0.35,
    "in the style of": 0.55,
    "sped up": 0.30,
    "spedup": 0.30,
    "slowed": 0.30,
    "nightcore": 0.50,
    "8 bit": 0.45,
    "8-bit": 0.45,
    "instrumental": 0.35,   # penalize UNLESS the prompt wanted instrumental
    "remix": 0.20,          # mild: sometimes fine, often not what's meant
    "live": 0.15,           # mild: prefer studio unless asked
}

# Parenthetical / trailing noise stripped for MATCHING only (kept for display).
NOISE_PATTERNS = [
    r"\(feat\..*?\)", r"\(ft\..*?\)", r"feat\..*", r"ft\..*",
    r"\(with .*?\)",
    r"\(.*?remaster.*?\)", r"-\s*.*remaster.*",
    r"\(.*?mono.*?\)", r"\(.*?stereo.*?\)",
    r"\(.*?deluxe.*?\)", r"\(.*?anniversary.*?\)",
    r"\(.*?radio edit.*?\)", r"\(.*?single version.*?\)",
    r"\(.*?album version.*?\)", r"\(.*?bonus.*?\)",
    r"-\s*\d{4}\s*remaster.*",
    # Clean/explicit tags shouldn't affect title matching at all — which
    # cut you get is decided by the `explicit` boolean field (see
    # resolve_track's allow_explicit gate), not by string-matching this
    # text. Without stripping these, a literally-tagged "(Clean)" title
    # takes a title-similarity hit for no reason, which can push a
    # perfectly good clean match below TITLE_MIN.
    r"\(.*?clean.*?\)", r"-\s*.*clean.*",
    r"\(.*?explicit.*?\)", r"-\s*.*explicit.*",
]


# ─────────────────────────────────────────────────────────────────────────────
# Normalization helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, strip diacritics, collapse punctuation/whitespace."""
    text = unidecode(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)     # punctuation -> space
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_noise(title: str) -> str:
    """Remove remaster/feat/version noise so 'Song (Remastered 2011)' == 'Song'."""
    t = (title or "").lower()
    for pat in NOISE_PATTERNS:
        t = re.sub(pat, " ", t)
    return normalize(t)


def primary_artist(artist: str) -> str:
    """Take the lead artist from things like 'A feat. B' or 'A & B' or 'A, B'."""
    a = re.split(r"\bfeat\b|\bft\b|\bwith\b|&|,|/", artist or "", maxsplit=1)[0]
    return normalize(a)


def ratio(a: str, b: str) -> float:
    """Fuzzy similarity in 0..1 (token-sort handles word reordering)."""
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b) / 100.0


def best_artist_sim(cand_artist: str, track_artist_names: list[str]) -> float:
    """
    Best artist similarity, robust to both featured-artist noise AND bands whose
    real name contains '&' / 'and' / ','.

    We compare the candidate against each track artist using BOTH the full artist
    string and the primary-artist fallback, and also against all track artists
    joined — then take the max. This way stripping 'feat.' still helps for
    collabs, without punishing a legitimate band name like
    'Bob Marley & The Wailers' (which the primary-only split would shrink to
    'Bob Marley' and score poorly).
    """
    full = normalize(cand_artist)
    primary = primary_artist(cand_artist)
    joined = normalize(" ".join(track_artist_names))
    best = ratio(full, joined)  # candidate vs all credited artists as one string
    for a in track_artist_names:
        na = normalize(a)
        best = max(best, ratio(full, na), ratio(primary, na))
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    title: str
    artist: str


@dataclass
class MatchResult:
    candidate: Candidate
    accepted: bool
    reason: str
    # populated when a track was found (even if ultimately rejected):
    track_id: Optional[str] = None
    track_uri: Optional[str] = None
    track_name: Optional[str] = None
    track_artists: Optional[str] = None
    popularity: Optional[int] = None
    explicit: Optional[bool] = None   # None = unknown (e.g. field missing from result)
    score: float = 0.0
    subscores: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_track(cand: Candidate, track: dict, wanted_variants: set[str]) -> tuple[float, dict]:
    """Return (final_score, subscores) for one search-result track vs the candidate."""
    cand_title = strip_noise(cand.title)

    track_title_raw = track.get("name", "")
    track_title = strip_noise(track_title_raw)
    track_artist_names = [a.get("name", "") for a in track.get("artists", [])]

    title_sim = ratio(cand_title, track_title)
    # robust to featured-artist noise AND '&'/'and'/',' in real band names
    artist_sim = best_artist_sim(cand.artist, track_artist_names)

    # variant penalty: junk words present in the found title that the user didn't ask for
    lowered = track_title_raw.lower()
    penalty = 0.0
    hits = []
    for word, pen in VARIANT_PENALTIES.items():
        if word in lowered and word not in wanted_variants:
            penalty += pen
            hits.append(word)

    base = WEIGHT_TITLE * title_sim + WEIGHT_ARTIST * artist_sim
    final = max(0.0, base - penalty)

    return final, {
        "title_sim": round(title_sim, 3),
        "artist_sim": round(artist_sim, 3),
        "penalty": round(penalty, 3),
        "penalty_words": hits,
        "base": round(base, 3),
    }


def _playable(track: dict, market: str) -> bool:
    """Availability guard. With a market set, prefer is_playable; else check markets."""
    if track.get("is_playable") is False:
        return False
    markets = track.get("available_markets")
    if markets is not None and market and market not in markets:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

def _search(sp, query: str, market: str) -> list[dict]:
    try:
        res = sp.search(q=query, type="track", limit=SEARCH_LIMIT, market=market)
        return res.get("tracks", {}).get("items", []) or []
    except Exception as e:                      # noqa: BLE001 (spike: surface, don't crash batch)
        print(f"  ! search error for {query!r}: {e}")
        return []


def resolve_track(sp, cand: Candidate, market: str = "US",
                  wanted_variants: Optional[set[str]] = None,
                  allow_explicit: bool = True) -> MatchResult:
    """Resolve ONE candidate to the best real track, or drop it with a reason.

    allow_explicit=False makes explicit a gate, not just a downstream filter
    (curation.filter_explicit still exists as a defense-in-depth backstop).
    That distinction matters: without this, the walk below would happily
    commit to the top-scoring result even when it's the explicit cut of a
    song, and a clean radio edit sitting right next to it in the SAME
    result set would never get a look — the candidate would resolve fine
    here and only get thrown away later in curation, losing the song
    entirely instead of substituting the clean version. explicit=None
    (Spotify didn't report the flag) is never gated — same "unknown isn't
    guilty" policy as curation.filter_explicit's default.
    """
    wanted_variants = wanted_variants or set()

    # strict field-filtered query first; loose fallback if it returns nothing
    strict = f'track:"{cand.title}" artist:"{cand.artist}"'
    loose = f"{cand.title} {cand.artist}"
    items = _search(sp, strict, market) or _search(sp, loose, market)

    if not items:
        return MatchResult(cand, accepted=False, reason="no_search_results")

    # score every result, best first
    scored = [(*score_track(cand, tr, wanted_variants), tr) for tr in items]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Walk results best-to-worst and accept the first one that clears every hard
    # gate — don't stop at the top scorer. A track can out-score everything else
    # and still fail a gate (e.g. not playable here), while a slightly lower-
    # scoring alternative in the SAME result set is perfectly valid; without this
    # fallback that valid alternative was silently thrown away with the winner.
    fallback: Optional[MatchResult] = None
    for score, subs, tr in scored:
        r = MatchResult(
            candidate=cand,
            accepted=False,
            reason="",
            track_id=tr.get("id"),
            track_uri=tr.get("uri"),
            track_name=tr.get("name"),
            track_artists=", ".join(a.get("name", "") for a in tr.get("artists", [])),
            popularity=tr.get("popularity"),
            explicit=tr.get("explicit"),
            score=round(score, 3),
            subscores=subs,
        )

        if not _playable(tr, market):
            r.reason = "not_playable_in_market"
        elif not allow_explicit and tr.get("explicit") is True:
            r.reason = "explicit_track"
        elif subs["artist_sim"] < ARTIST_MIN:
            r.reason = f"artist_mismatch ({subs['artist_sim']})"
        elif subs["title_sim"] < TITLE_MIN:
            r.reason = f"title_mismatch ({subs['title_sim']})"
        elif score < ACCEPT_THRESHOLD:
            r.reason = f"below_threshold ({r.score} < {ACCEPT_THRESHOLD})"
        else:
            r.accepted = True
            r.reason = "ok"
            return r

        if fallback is None:
            fallback = r  # report the top scorer's reason if nothing clears every gate

    return fallback


def resolve_tracklist(sp, candidates: list[Candidate], market: str = "US",
                      wanted_variants: Optional[set[str]] = None,
                      allow_explicit: bool = True) -> dict:
    """Resolve a whole list: dedupe accepted URIs, and report a drop summary.

    Each candidate's resolve_track() call runs concurrently in a thread pool
    (MAX_RESOLVER_WORKERS workers) rather than one at a time — this is pure
    network wait, not CPU work, so threads help despite the GIL. Results are
    collected via ThreadPoolExecutor.map(), which returns them in the SAME
    ORDER as `candidates` regardless of which search finishes first. That
    ordering guarantee is load-bearing: the dedupe-by-track-id loop below
    keeps the FIRST candidate (by input order) that resolves to a given
    track and drops any later one as "duplicate" — if results came back in
    completion order instead, which candidate "wins" a duplicate would
    depend on network timing, not on what the caller actually asked for.

    Before starting the pool, one token check runs synchronously on the
    calling thread: spotipy's SpotifyOAuth has no lock around
    refresh_access_token(), so if the cached token happened to already be
    expired, every worker thread would independently detect that and race
    to POST its own refresh (and write the on-disk token cache) at once.
    Forcing a single check up front means the cache is fresh before any
    thread touches it — access tokens last ~1hr, this batch takes seconds —
    so that race never has a chance to trigger in practice.
    """
    accepted: list[MatchResult] = []
    dropped: list[MatchResult] = []
    seen_ids: set[str] = set()

    if not candidates:
        return {
            "accepted": accepted,
            "dropped": dropped,
            "uris": [],
            "summary": {"total": 0, "accepted": 0, "dropped": 0, "drop_rate": 0.0},
        }

    auth_manager = getattr(sp, "auth_manager", None)
    if auth_manager is not None:
        try:
            auth_manager.get_access_token(as_dict=False)
        except Exception as e:                      # noqa: BLE001 (best-effort pre-warm; a
            print(f"  ! token pre-warm failed, continuing anyway: {e}")  # real auth failure
            # will still surface per-candidate below via _search()'s own error handling.

    resolve_one = partial(resolve_track, sp, market=market,
                           wanted_variants=wanted_variants, allow_explicit=allow_explicit)
    with ThreadPoolExecutor(max_workers=min(MAX_RESOLVER_WORKERS, len(candidates))) as executor:
        results = list(executor.map(resolve_one, candidates))

    for res in results:
        if res.accepted:
            if res.track_id in seen_ids:
                res.accepted = False
                res.reason = "duplicate"
                dropped.append(res)
            else:
                seen_ids.add(res.track_id)
                accepted.append(res)
        else:
            dropped.append(res)

    total = len(candidates)
    dropped_by_reason: dict[str, int] = {}
    for r in dropped:
        # bucket "artist_mismatch (0.45)" / "below_threshold (0.65 < 0.72)" ->
        # "artist_mismatch" / "below_threshold" — same convention curation.py's
        # HouseRulesOutcome.summary already uses, so a caller merging both
        # summaries gets consistent bucket names instead of one aggregated and
        # one full of one-off, unaggregatable detail strings.
        key = r.reason.split(" (", 1)[0]
        dropped_by_reason[key] = dropped_by_reason.get(key, 0) + 1

    return {
        "accepted": accepted,
        "dropped": dropped,
        "uris": [r.track_uri for r in accepted],
        "summary": {
            "total": total,
            "accepted": len(accepted),
            "dropped": len(dropped),
            "drop_rate": round(len(dropped) / total, 3) if total else 0.0,
            "dropped_by_reason": dropped_by_reason,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Demo — run this to sanity-check matching on real Spotify data
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope="playlist-read-private",   # search needs a user token in 2026
        open_browser=True,
    ))

    # Pretend these came out of the LLM. Note the deliberate hard cases:
    #  - a remaster-tagged title, an accented artist, and a title prone to
    #    karaoke/cover pollution.
    candidates = [
        Candidate("Ain't No Sunshine", "Bill Withers"),
        Candidate("Redemption Song (Remastered)", "Bob Marley & The Wailers"),
        Candidate("La Vie En Rose", "Édith Piaf"),
        Candidate("Golden Hour", "JVKE"),
        Candidate("A Song That Does Not Exist", "Nobody At All"),  # should drop
    ]

    result = resolve_tracklist(sp, candidates, market="US")

    print("\n=== ACCEPTED ===")
    for r in result["accepted"]:
        print(f"  ✓ {r.track_name} — {r.track_artists}  "
              f"(score {r.score}, {r.subscores})")

    print("\n=== DROPPED ===")
    for r in result["dropped"]:
        got = f" [best guess: {r.track_name} — {r.track_artists}]" if r.track_name else ""
        print(f"  ✗ {r.candidate.title} — {r.candidate.artist}: {r.reason}{got}")

    print("\n=== SUMMARY ===")
    print(f"  {result['summary']}")


if __name__ == "__main__":
    _demo()
