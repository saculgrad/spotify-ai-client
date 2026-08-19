"""
curation.py — house-rules filtering layer

Sits between resolver.py (LLM candidate -> real Spotify track) and the
write step (create/append to a playlist, not yet built). Given the
resolver's ACCEPTED tracks, applies venue policy before anything gets
written:

1. Drop tracks already in the target playlist (fetch-and-diff).
2. Drop tracks used recently, via a local rolling log (RecentlyUsedLog).
3. Apply the explicit-content policy.
4. Cap how many tracks by the same artist can appear (artist-diversity cap).

Every function here is pure local logic or local-file I/O — nothing in this
module calls Spotify or an LLM, so it can be built, run, and tested without
either set of credentials. It consumes resolver.MatchResult directly (the
same "resolved candidate" object resolver.py already produces) rather than
inventing a parallel representation.

Nothing here ever silently drops a track without a reason — every drop is
recorded so a caller (eventually the review UI) can show the human why a
track didn't make the final list.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from resolver import MatchResult, normalize, primary_artist

# ─────────────────────────────────────────────────────────────────────────────
# TUNING KNOBS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_RECENT_WINDOW_DAYS = 30   # how far back "recently used" looks by default
DEFAULT_PRUNE_AFTER_DAYS = 180    # RecentlyUsedLog.prune() default retention


# ─────────────────────────────────────────────────────────────────────────────
# Result shapes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Dropped:
    result: MatchResult
    reason: str


@dataclass
class FilterOutcome:
    kept: list[MatchResult]
    dropped: list[Dropped]

    @property
    def summary(self) -> dict:
        return {"kept": len(self.kept), "dropped": len(self.dropped)}


@dataclass
class HouseRulesOutcome:
    kept: list[MatchResult]
    dropped: list[Dropped]

    @property
    def summary(self) -> dict:
        by_reason: dict[str, int] = {}
        for d in self.dropped:
            key = d.reason.split(" (", 1)[0]   # bucket "artist_diversity_cap (...)" -> "artist_diversity_cap"
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "total": len(self.kept) + len(self.dropped),
            "kept": len(self.kept),
            "dropped": len(self.dropped),
            "dropped_by_reason": by_reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Individual filters
# ─────────────────────────────────────────────────────────────────────────────

def filter_resolver_accepted(results: Iterable[MatchResult]) -> FilterOutcome:
    """Defensive guard: only pass through results the resolver actually
    accepted. Protects against a caller mistakenly feeding in the resolver's
    full accepted+dropped list instead of just result["accepted"]."""
    kept, dropped = [], []
    for r in results:
        if r.accepted:
            kept.append(r)
        else:
            dropped.append(Dropped(r, "not_accepted_by_resolver"))
    return FilterOutcome(kept, dropped)


def filter_explicit(
    results: Iterable[MatchResult],
    allow_explicit: bool = True,
    drop_unknown: bool = False,
) -> FilterOutcome:
    """
    Explicit-content policy.
    - allow_explicit=True: pass everything through unchanged.
    - allow_explicit=False: drop explicit==True. explicit==None (Spotify
      didn't report the flag) is kept unless drop_unknown=True — Spotify
      populates this field on essentially every real search result, so
      None is mostly a testing/mock-data artifact, not a real gap.
    """
    if allow_explicit:
        return FilterOutcome(kept=list(results), dropped=[])

    kept, dropped = [], []
    for r in results:
        if r.explicit is True:
            dropped.append(Dropped(r, "explicit_track"))
        elif r.explicit is None and drop_unknown:
            dropped.append(Dropped(r, "explicit_unknown"))
        else:
            kept.append(r)
    return FilterOutcome(kept, dropped)


def cap_artist_diversity(
    results: Iterable[MatchResult],
    max_per_artist: Optional[int],
) -> FilterOutcome:
    """
    Keep at most `max_per_artist` tracks per artist, preserving input order
    (first N occurrences of an artist are kept; later ones are dropped).
    max_per_artist=None disables the cap entirely.

    Grouping key is the normalized PRIMARY artist (resolver.primary_artist)
    so "Drake" and "Drake feat. Future" count against the same cap, while a
    band name containing '&'/'and'/',' (e.g. "Bob Marley & The Wailers")
    still groups as one artist rather than being torn apart by the split —
    consistent with how resolver.py already treats such names in scoring.
    """
    if max_per_artist is None:
        return FilterOutcome(kept=list(results), dropped=[])
    if max_per_artist < 1:
        raise ValueError(f"max_per_artist must be >= 1, got {max_per_artist}")

    counts: dict[str, int] = {}
    kept, dropped = [], []
    for r in results:
        key = normalize(primary_artist(r.track_artists or ""))
        seen_so_far = counts.get(key, 0)
        if seen_so_far < max_per_artist:
            counts[key] = seen_so_far + 1
            kept.append(r)
        else:
            dropped.append(
                Dropped(r, f"artist_diversity_cap ({r.track_artists!r} already has {max_per_artist})")
            )
    return FilterOutcome(kept, dropped)


def dedupe_against_ids(
    results: Iterable[MatchResult],
    seen_ids: Iterable[str],
    reason: str,
) -> FilterOutcome:
    """Generic id-membership dedupe. A result with no track_id (shouldn't
    happen among resolver-accepted results, but garbage-in is a real
    scenario) can't be checked against seen_ids, so it's kept rather than
    dropped on a comparison we can't actually make."""
    seen = set(seen_ids)
    kept, dropped = [], []
    for r in results:
        if r.track_id is not None and r.track_id in seen:
            dropped.append(Dropped(r, reason))
        else:
            kept.append(r)
    return FilterOutcome(kept, dropped)


def dedupe_against_playlist(results: Iterable[MatchResult], existing_track_ids: Iterable[str]) -> FilterOutcome:
    return dedupe_against_ids(results, existing_track_ids, "already_in_playlist")


def dedupe_against_recent_log(results: Iterable[MatchResult], recent_track_ids: Iterable[str]) -> FilterOutcome:
    return dedupe_against_ids(results, recent_track_ids, "recently_used")


# ─────────────────────────────────────────────────────────────────────────────
# Recently-used log — local JSON file, no network
# ─────────────────────────────────────────────────────────────────────────────

class RecentlyUsedLog:
    """
    JSON-backed log of {track_id: last_used_iso_timestamp}, used to stop the
    same songs getting re-added every week (spec section 8, "quality
    levers"). Local state only — no network calls, so this is fully
    buildable and testable without any API credentials.

    A missing file is treated as an empty log. A corrupted/unexpected-shape
    file is also treated as an empty log (with a warning printed to
    stderr) rather than crashing the run — consistent with resolver.py's
    "surface, don't crash the batch" philosophy.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._entries: dict[str, str] = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            print(f"  ! recently-used log at {self.path} is corrupted ({e}); starting fresh", file=sys.stderr)
            return {}
        if not isinstance(raw, dict):
            print(f"  ! recently-used log at {self.path} has an unexpected shape; starting fresh", file=sys.stderr)
            return {}
        return raw

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2, sort_keys=True))

    def record(self, track_ids: Iterable[str], when: Optional[datetime] = None) -> None:
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        for tid in track_ids:
            self._entries[tid] = stamp

    def is_recent(self, track_id: str, within_days: int = DEFAULT_RECENT_WINDOW_DAYS) -> bool:
        stamp = self._entries.get(track_id)
        if stamp is None:
            return False
        try:
            recorded_at = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - recorded_at) <= timedelta(days=within_days)

    def ids_used_within(self, within_days: int = DEFAULT_RECENT_WINDOW_DAYS) -> set[str]:
        return {tid for tid in self._entries if self.is_recent(tid, within_days)}

    def prune(self, older_than_days: int = DEFAULT_PRUNE_AFTER_DAYS) -> int:
        """Drop entries older than `older_than_days` (and any unparseable
        entries) so the log doesn't grow forever. Returns count removed."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        kept: dict[str, str] = {}
        for tid, stamp in self._entries.items():
            try:
                recorded_at = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if recorded_at >= cutoff:
                kept[tid] = stamp
        removed = len(self._entries) - len(kept)
        self._entries = kept
        return removed


# ─────────────────────────────────────────────────────────────────────────────
# Composed pipeline
# ─────────────────────────────────────────────────────────────────────────────

def apply_house_rules(
    results: Iterable[MatchResult],
    *,
    allow_explicit: bool = True,
    drop_unknown_explicit: bool = False,
    max_per_artist: Optional[int] = None,
    existing_playlist_ids: Iterable[str] = (),
    recent_log_ids: Iterable[str] = (),
) -> HouseRulesOutcome:
    """
    Run the full house-rules pipeline in a fixed order:
      0. Drop anything the resolver didn't actually accept (defensive).
      1. Drop tracks already in the target playlist.
      2. Drop tracks used recently (rolling log).
      3. Apply the explicit-content policy.
      4. Apply the artist-diversity cap — last, so it caps the pool that
         actually survives every other rule, not a pool still full of
         dupes/explicit tracks that would never have been written anyway.
    """
    all_dropped: list[Dropped] = []

    outcome = filter_resolver_accepted(results)
    stage, all_dropped = outcome.kept, all_dropped + outcome.dropped

    outcome = dedupe_against_playlist(stage, existing_playlist_ids)
    stage, all_dropped = outcome.kept, all_dropped + outcome.dropped

    outcome = dedupe_against_recent_log(stage, recent_log_ids)
    stage, all_dropped = outcome.kept, all_dropped + outcome.dropped

    outcome = filter_explicit(stage, allow_explicit=allow_explicit, drop_unknown=drop_unknown_explicit)
    stage, all_dropped = outcome.kept, all_dropped + outcome.dropped

    outcome = cap_artist_diversity(stage, max_per_artist)
    stage, all_dropped = outcome.kept, all_dropped + outcome.dropped

    return HouseRulesOutcome(kept=stage, dropped=all_dropped)


# ─────────────────────────────────────────────────────────────────────────────
# Demo — runs fully offline, no credentials of any kind needed
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    from resolver import Candidate

    def mk(id_, title, artist, explicit=False, popularity=50):
        return MatchResult(
            candidate=Candidate(title, artist),
            accepted=True,
            reason="ok",
            track_id=id_,
            track_uri=f"spotify:track:{id_}",
            track_name=title,
            track_artists=artist,
            popularity=popularity,
            explicit=explicit,
            score=1.0,
        )

    results = [
        mk("t1", "Ain't No Sunshine", "Bill Withers"),
        mk("t2", "Lean On Me", "Bill Withers"),
        mk("t3", "Grandma's Hands", "Bill Withers"),
        mk("t4", "Let's Stay Together", "Al Green"),
        mk("t5", "WAP", "Cardi B", explicit=True),
        mk("t6", "Already Here", "Nobody"),   # pretend it's already in the playlist
    ]

    outcome = apply_house_rules(
        results,
        allow_explicit=False,
        max_per_artist=2,
        existing_playlist_ids={"t6"},
    )

    print("=== KEPT ===")
    for r in outcome.kept:
        print(f"  ✓ {r.track_name} — {r.track_artists}")

    print("\n=== DROPPED ===")
    for d in outcome.dropped:
        print(f"  ✗ {d.result.track_name} — {d.result.track_artists}: {d.reason}")

    print(f"\n=== SUMMARY ===\n  {outcome.summary}")


if __name__ == "__main__":
    _demo()
