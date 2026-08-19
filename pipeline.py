"""
pipeline.py — wires generator -> resolver -> curation into one call

The app layer (app.py's Flask routes) calls run_pipeline() once and gets
back a curated, review-ready list of MatchResults. Kept separate from
app.py so the wiring itself — does a generated candidate actually flow
correctly into the resolver, does the resolver's accepted list actually
flow correctly into house rules — is testable with fake Anthropic/Spotify
clients exactly like every other module here, with no Flask involved and
no live credentials needed to verify the plumbing is correct.

(Live credentials are still needed to judge whether the pipeline's *output
quality* is good — that's a separate concern from "is the wiring right,"
and this module only tests the latter.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from curation import HouseRulesOutcome, apply_house_rules
from generator import GenerationRequest, generate_candidates
from resolver import MatchResult, resolve_tracklist


@dataclass
class PipelineResult:
    kept: list[MatchResult]
    house_rules: HouseRulesOutcome
    generated_count: int
    accepted_count: int
    resolver_dropped_by_reason: dict[str, int]


def run_pipeline(
    anthropic_client,
    spotify_client,
    generation_request: GenerationRequest,
    *,
    market: str = "US",
    allow_explicit: bool = True,
    max_per_artist: Optional[int] = None,
    existing_playlist_ids: Iterable[str] = (),
    recent_log_ids: Iterable[str] = (),
) -> PipelineResult:
    """Run generate -> resolve -> curate in order, returning the final
    review-ready track list plus enough intermediate counts (generated,
    accepted) for a caller to report drop rates without re-deriving them."""
    gen_result = generate_candidates(anthropic_client, generation_request)

    resolved = resolve_tracklist(
        spotify_client,
        gen_result.candidates,
        market=market,
        wanted_variants=gen_result.wanted_variants,
        allow_explicit=allow_explicit,
    )

    # allow_explicit is enforced twice on purpose: resolver.py's gate makes
    # it PREFER a clean version over an explicit one when both show up in
    # the same search results (otherwise the explicit cut wins on score and
    # the song is lost entirely downstream); curation.py's filter is the
    # backstop for anything that still slipped through explicit (e.g. no
    # clean version exists at all, or the flag was unknown at resolve time).
    house_rules = apply_house_rules(
        resolved["accepted"],
        allow_explicit=allow_explicit,
        max_per_artist=max_per_artist,
        existing_playlist_ids=existing_playlist_ids,
        recent_log_ids=recent_log_ids,
    )

    return PipelineResult(
        kept=house_rules.kept,
        house_rules=house_rules,
        generated_count=gen_result.generated_count,
        accepted_count=len(resolved["accepted"]),
        resolver_dropped_by_reason=resolved["summary"]["dropped_by_reason"],
    )
