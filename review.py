"""
review.py — human review state machine

CLAUDE.md's spec (section 4.6): "Always show the resolved list before it
goes live: approve, remove individual tracks, or regenerate. This single
step is what makes the whole thing reliable despite LLM hallucination."

This module is the state machine behind that step, not a UI. It's
UI-framework-agnostic on purpose — a CLI prompt loop or a Flask route can
both sit on top of ReviewSession with zero changes here, and neither
decision needs to be made to build and test this layer. It operates on
resolver.MatchResult (the same "resolved candidate" object used throughout
the pipeline) and produces the final URI list ready for
spotify_client.add_tracks_to_playlist().

Usage
-----
    session = ReviewSession(house_rules_outcome.kept)
    session.remove(bad_id, reason="wrong version")
    session.request_regenerate(iffy_id, note="want something more upbeat")
    session.approve_all_pending()
    uris = session.final_uris()   # -> spotify_client.add_tracks_to_playlist()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from resolver import MatchResult

PENDING = "pending"
APPROVED = "approved"
REMOVED = "removed"
REGENERATE_REQUESTED = "regenerate_requested"

_VALID_STATUSES = {PENDING, APPROVED, REMOVED, REGENERATE_REQUESTED}


@dataclass
class ReviewItem:
    result: MatchResult
    status: str = PENDING
    note: Optional[str] = None


class ReviewSession:
    """
    Tracks per-track review status for one batch of resolved-and-curated
    tracks. Every transition is an explicit action (approve/remove/
    request_regenerate) that overrides whatever status the track had
    before — a human changing their mind is a first-class case, not an
    error.
    """

    def __init__(self, tracks: list[MatchResult]):
        seen_ids = set()
        for t in tracks:
            if t.track_id is None:
                raise ValueError("ReviewSession requires every track to have a track_id")
            if t.track_id in seen_ids:
                raise ValueError(f"Duplicate track_id in ReviewSession input: {t.track_id!r}")
            seen_ids.add(t.track_id)

        self._order: list[str] = [t.track_id for t in tracks]
        self._items: dict[str, ReviewItem] = {t.track_id: ReviewItem(result=t) for t in tracks}

    def _get(self, track_id: str) -> ReviewItem:
        item = self._items.get(track_id)
        if item is None:
            raise KeyError(f"No track with id {track_id!r} in this review session")
        return item

    # ── actions ────────────────────────────────────────────────────────────

    def approve(self, track_id: str) -> None:
        item = self._get(track_id)
        item.status = APPROVED
        item.note = None

    def remove(self, track_id: str, reason: Optional[str] = None) -> None:
        item = self._get(track_id)
        item.status = REMOVED
        item.note = reason

    def request_regenerate(self, track_id: str, note: Optional[str] = None) -> None:
        item = self._get(track_id)
        item.status = REGENERATE_REQUESTED
        item.note = note

    def add_tracks(self, tracks: list[MatchResult]) -> None:
        """Append newly generated tracks to this session as PENDING,
        leaving every existing item's status untouched. Backs the "generate
        more" follow-up flow — new candidates join the same review, they
        don't replace it. Same validation as the constructor (missing/
        duplicate track_id raises `ValueError`), checked against the FULL
        existing session, not just other tracks in this same call — the
        caller is expected to have already deduped against the session's
        track ids via curation before calling this (see `app.py`'s
        `generate_more()`), so a duplicate reaching here indicates a real
        bug upstream, not a case to silently paper over.
        """
        for t in tracks:
            if t.track_id is None:
                raise ValueError("ReviewSession requires every track to have a track_id")
            if t.track_id in self._items:
                raise ValueError(f"Duplicate track_id in ReviewSession input: {t.track_id!r}")
            self._order.append(t.track_id)
            self._items[t.track_id] = ReviewItem(result=t)

    def approve_all_pending(self) -> int:
        """Approve every track still in PENDING status (does not touch
        already-removed or already-regenerate-requested tracks). Returns
        the number of tracks approved."""
        count = 0
        for item in self._items.values():
            if item.status == PENDING:
                item.status = APPROVED
                count += 1
        return count

    # ── queries ────────────────────────────────────────────────────────────

    def _by_status(self, status: str) -> list[ReviewItem]:
        return [self._items[tid] for tid in self._order if self._items[tid].status == status]

    def items(self) -> list[ReviewItem]:
        """Every track in original order with its current status/note — the
        full internal state. Meant for rendering (a UI needs every status,
        not just one bucket) and for serialization (session_store.py)."""
        return [self._items[tid] for tid in self._order]

    def pending(self) -> list[MatchResult]:
        return [item.result for item in self._by_status(PENDING)]

    def approved(self) -> list[MatchResult]:
        return [item.result for item in self._by_status(APPROVED)]

    def removed(self) -> list[tuple[MatchResult, Optional[str]]]:
        return [(item.result, item.note) for item in self._by_status(REMOVED)]

    def regenerate_requests(self) -> list[tuple[MatchResult, Optional[str]]]:
        return [(item.result, item.note) for item in self._by_status(REGENERATE_REQUESTED)]

    def final_uris(self) -> list[str]:
        """Approved tracks' URIs, in original resolver order — ready to hand
        to spotify_client.add_tracks_to_playlist(). Skips any approved item
        that somehow lacks a URI rather than raising, since that's a data
        problem downstream code should surface, not this one."""
        return [
            item.result.track_uri
            for item in self._by_status(APPROVED)
            if item.result.track_uri
        ]

    def summary(self) -> dict:
        counts = {status: 0 for status in _VALID_STATUSES}
        for item in self._items.values():
            counts[item.status] += 1
        counts["total"] = len(self._items)
        return counts


# ─────────────────────────────────────────────────────────────────────────────
# Demo — runs fully offline
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    from resolver import Candidate

    def mk(id_, title, artist):
        return MatchResult(
            candidate=Candidate(title, artist), accepted=True, reason="ok",
            track_id=id_, track_uri=f"spotify:track:{id_}",
            track_name=title, track_artists=artist, score=1.0,
        )

    session = ReviewSession([
        mk("t1", "Ain't No Sunshine", "Bill Withers"),
        mk("t2", "Lean On Me", "Bill Withers"),
        mk("t3", "Some Weird Cover", "Cover Band"),
        mk("t4", "Let's Stay Together", "Al Green"),
    ])

    session.remove("t3", reason="wrong version, want the original")
    session.request_regenerate("t4", note="want something more upbeat")
    session.approve_all_pending()

    print("=== APPROVED ===")
    for r in session.approved():
        print(f"  ✓ {r.track_name} — {r.track_artists}")

    print("\n=== REMOVED ===")
    for r, reason in session.removed():
        print(f"  ✗ {r.track_name} — {r.track_artists}: {reason}")

    print("\n=== REGENERATE REQUESTS ===")
    for r, note in session.regenerate_requests():
        print(f"  ↻ {r.track_name} — {r.track_artists}: {note}")

    print(f"\n=== SUMMARY ===\n  {session.summary()}")
    print(f"\n=== FINAL URIS ===\n  {session.final_uris()}")


if __name__ == "__main__":
    _demo()
