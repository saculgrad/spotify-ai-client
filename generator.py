"""
generator.py — LLM candidate generator (Phase 1 piece)

Turns a plain-language vibe prompt into a candidate tracklist of
{"title", "artist"} guesses for resolver.py to turn into real Spotify tracks.
This module is Claude-API-specific but Spotify-agnostic — it never touches
Spotify, so it can be tuned independently of the resolver, per CLAUDE.md's
"isolate the churny layer" principle. The two modules meet only at the
shared `Candidate` shape and the `wanted_variants` set.

What it does
------------
1. Builds a system + user prompt: house rules, the restaurant's own
   playlists as few-shot grounding (the single biggest quality lever per
   the project spec), the blocklist, and the request's toggles (era,
   language, explicit policy, "avoid obvious hits").
2. Asks for ~1.4x the target count so the candidate list survives
   resolver.py's drops.
3. Uses the Claude API's structured-outputs (`output_config.format`) so the
   response is guaranteed valid JSON matching the schema — no fence-
   stripping needed. Still retries on a safety refusal or a max_tokens
   truncation, and defensively drops any candidate missing a usable
   title/artist.
4. Applies the blocklist as a hard filter in code — never trust the prompt
   alone to keep a banned artist/track out.

Setup
-----
    pip install anthropic
    export ANTHROPIC_API_KEY=...      # or `ant auth login`

Usage
-----
    import anthropic
    from generator import GenerationRequest, generate_candidates
    from resolver import resolve_tracklist

    client = anthropic.Anthropic()
    req = GenerationRequest(
        vibe_prompt="warm 60s-70s soul for Sunday brunch, nothing too loud",
        track_count=20,
        blocklist=["Nickelback", "Baby Shark"],
        house_taste=["Bill Withers - Ain't No Sunshine", "Al Green - Let's Stay Together"],
    )
    result = generate_candidates(client, req)
    resolved = resolve_tracklist(sp, result.candidates, wanted_variants=result.wanted_variants)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Optional

from resolver import Candidate, normalize

# ─────────────────────────────────────────────────────────────────────────────
# TUNING KNOBS
# ─────────────────────────────────────────────────────────────────────────────

MODEL = "claude-opus-4-8"     # project spec notes a cheaper model works fine for
                               # this task (song-name generation isn't hard) — swap
                               # to claude-haiku-4-5 here if cost matters more than
                               # quality; defaulting to the strongest model for now.
OVERGENERATION_FACTOR = 1.4   # ask for this many times the target count
MAX_TOKENS = 4096             # plenty for a few dozen {title, artist, reason} rows
MAX_ATTEMPTS = 3              # 1 initial try + 2 retries on refusal/truncation/malformed JSON

# Variant words the resolver's scoring knows how to handle (resolver.py
# VARIANT_PENALTIES). Surfaced to the model so it emits wanted_variants using
# words the resolver will actually recognize.
KNOWN_VARIANTS = [
    "live", "remix", "instrumental", "cover", "karaoke", "tribute",
    "sped up", "slowed", "nightcore", "8-bit",
]


# ─────────────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GenerationRequest:
    vibe_prompt: str
    track_count: int
    era: Optional[str] = None
    language: Optional[str] = None
    explicit_ok: bool = True
    avoid_obvious: bool = False
    artist_diversity_cap: Optional[int] = None
    house_taste: list[str] = field(default_factory=list)   # "Artist - Title" lines
    blocklist: list[str] = field(default_factory=list)      # artist and/or track names
    # "Artist - Title" lines, optionally with a trailing "(reason: ...)" —
    # tracks a human reviewer already rejected THIS session (removed or
    # flagged for regeneration). Deliberately separate from `blocklist`:
    # blocklist is a permanent, venue-wide hard ban; this is a soft,
    # per-request "steer away from" signal that only applies to this one
    # generation call. See app.py's generate_more() for how it's built.
    previously_rejected: list[str] = field(default_factory=list)
    model: str = MODEL


@dataclass
class GenerationResult:
    candidates: list[Candidate]
    wanted_variants: set[str]
    requested_count: int
    generated_count: int
    blocked_count: int
    malformed_count: int


# ─────────────────────────────────────────────────────────────────────────────
# Structured output schema
# ─────────────────────────────────────────────────────────────────────────────

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "wanted_variants": {
            "type": "array",
            "items": {"type": "string"},
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "artist": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "artist", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["wanted_variants", "candidates"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    return (
        "You generate candidate song lists for a restaurant's Spotify playlists. "
        "You do not have access to Spotify — a separate system will search for and "
        "validate every song you suggest, and will silently drop anything that "
        "doesn't resolve to a real, matching track. Because of that:\n"
        "- Only suggest real, released songs with the correct recording artist. "
        "Do not invent songs or misattribute a song to the wrong artist.\n"
        "- Prefer the original studio recording unless the request clearly wants "
        "something else (a live version, a specific remix, an instrumental).\n"
        "- Avoid duplicate songs within your own list.\n"
        "- `wanted_variants` is a list of tags describing recording variants the "
        f"request actually wants, using words from this set where applicable: "
        f"{', '.join(KNOWN_VARIANTS)}. Leave it empty for an ordinary studio-track "
        "request — most requests want that."
    )


def _build_user_prompt(request: GenerationRequest, overgenerated_count: int) -> str:
    lines = [
        f"Request: {request.vibe_prompt}",
        f"Generate exactly {overgenerated_count} candidate tracks "
        f"(this is deliberately more than the {request.track_count} the venue "
        "wants, to survive downstream filtering — do not mention this padding, "
        "just provide good candidates).",
    ]

    if request.era:
        lines.append(f"Era/decade: {request.era}")
    if request.language:
        lines.append(f"Language: {request.language}")
    if not request.explicit_ok:
        lines.append("Exclude explicit tracks.")
    if request.avoid_obvious:
        lines.append(
            "Avoid the most obvious, overplayed choices for this vibe. This "
            "applies even for high-energy/party requests — do not default to "
            "a track just because it is a famous genre-defining anthem. "
            "Deliberately favor deeper cuts and lesser-known artists that "
            "still fit the energy: a good result should include several "
            "tracks a well-versed music fan would recognize but a casual "
            "listener probably would not, not just each artist's single "
            "biggest hit."
        )
    if request.artist_diversity_cap:
        lines.append(
            f"Do not suggest more than {request.artist_diversity_cap} tracks by "
            "the same artist."
        )

    if request.blocklist:
        lines.append(
            "Never suggest any of these artists or tracks (blocklisted by the "
            "venue): " + "; ".join(request.blocklist)
        )

    if request.house_taste:
        sample = "\n".join(f"  - {line}" for line in request.house_taste[:200])
        lines.append(
            "For reference, here is a sample of songs already on the venue's "
            "playlists — match this house taste and era/genre range where it's "
            "relevant to the request:\n" + sample
        )

    if request.previously_rejected:
        sample = "\n".join(f"  - {line}" for line in request.previously_rejected[:100])
        lines.append(
            "The following were suggested earlier in this same review session "
            "and a human reviewer explicitly rejected them — do not suggest any "
            "of them again, and lean away from very similar tracks unless they "
            "are clearly a better fit for the request. Where a reason or a "
            "note on what was wanted instead is given, use it as a guide:\n"
            + sample
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing / validation
# ─────────────────────────────────────────────────────────────────────────────

def _parse_candidates(payload: dict) -> tuple[list[Candidate], int]:
    """Turn the validated JSON payload into Candidates, dropping any row that's
    missing a usable title/artist (structured outputs guarantees the fields
    exist and are strings, but not that they're non-empty)."""
    candidates: list[Candidate] = []
    malformed = 0
    for row in payload.get("candidates", []):
        title = str(row.get("title", "")).strip()
        artist = str(row.get("artist", "")).strip()
        if not title or not artist:
            malformed += 1
            continue
        candidates.append(Candidate(title=title, artist=artist))
    return candidates, malformed


def _apply_blocklist(candidates: list[Candidate], blocklist: list[str]) -> tuple[list[Candidate], int]:
    """Hard filter — never rely on the prompt alone to keep a banned
    artist/track out. A blocklist entry matches if it's a substring of the
    candidate's (normalized) title or artist."""
    if not blocklist:
        return candidates, 0

    normalized_terms = [normalize(term) for term in blocklist if term.strip()]
    kept: list[Candidate] = []
    blocked = 0
    for cand in candidates:
        cand_title = normalize(cand.title)
        cand_artist = normalize(cand.artist)
        if any(term in cand_artist or term in cand_title for term in normalized_terms):
            blocked += 1
            continue
        kept.append(cand)
    return kept, blocked


# ─────────────────────────────────────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_candidates(client, request: GenerationRequest) -> GenerationResult:
    """Generate a candidate tracklist for one request. Raises RuntimeError if
    every attempt is refused, truncated, or unparseable."""
    overgenerated_count = max(
        request.track_count + 1,
        math.ceil(request.track_count * OVERGENERATION_FACTOR),
    )
    system = _build_system_prompt()
    user = _build_user_prompt(request, overgenerated_count)

    last_error = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.messages.create(
            model=request.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
        )

        if response.stop_reason == "refusal":
            last_error = f"refused on attempt {attempt}"
            continue
        if response.stop_reason == "max_tokens":
            last_error = f"truncated at max_tokens on attempt {attempt}"
            continue

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            last_error = f"malformed JSON on attempt {attempt}: {e}"
            continue

        candidates, malformed_count = _parse_candidates(payload)
        wanted_variants = {
            str(v).strip().lower() for v in payload.get("wanted_variants", []) if str(v).strip()
        }
        generated_count = len(candidates) + malformed_count
        candidates, blocked_count = _apply_blocklist(candidates, request.blocklist)

        return GenerationResult(
            candidates=candidates,
            wanted_variants=wanted_variants,
            requested_count=request.track_count,
            generated_count=generated_count,
            blocked_count=blocked_count,
            malformed_count=malformed_count,
        )

    raise RuntimeError(f"LLM generation failed after {MAX_ATTEMPTS} attempts: {last_error}")


# ─────────────────────────────────────────────────────────────────────────────
# Demo — run this to sanity-check generation (needs only ANTHROPIC_API_KEY, no
# Spotify credentials)
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    import anthropic

    client = anthropic.Anthropic()

    request = GenerationRequest(
        vibe_prompt="warm 60s-70s soul for Sunday brunch, nothing too loud",
        track_count=15,
        era="1960s-1970s",
        explicit_ok=False,
        avoid_obvious=True,
        blocklist=["Nickelback", "Baby Shark"],
        house_taste=[
            "Bill Withers - Ain't No Sunshine",
            "Al Green - Let's Stay Together",
            "Marvin Gaye - Let's Get It On",
            "Roberta Flack - Killing Me Softly with His Song",
        ],
    )

    result = generate_candidates(client, request)

    print(f"\nRequested {result.requested_count}, generated {result.generated_count} "
          f"({result.malformed_count} malformed, {result.blocked_count} blocked)")
    print(f"wanted_variants: {result.wanted_variants or '(none)'}\n")
    for c in result.candidates:
        print(f"  - {c.title} — {c.artist}")


if __name__ == "__main__":
    _demo()
