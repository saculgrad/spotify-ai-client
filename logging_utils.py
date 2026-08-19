"""
logging_utils.py — run log (local file, JSONL)

CLAUDE.md's spec (section 2, "Local store"): "a log of what was generated
(for dedupe and debugging)." This is that log: one JSON object per line
(JSONL), appended after every generate -> resolve -> curate -> write run,
so a venue owner (or a future you) can answer "why did last week's brunch
playlist have that song in it" without re-running anything.

Pure local file I/O — no Spotify or LLM call needed to build, run, or test
this. A single malformed line doesn't take down the whole log: it's
skipped with a warning, and every valid line around it still reads back
fine (JSONL's append-only, one-line-per-record shape is exactly what makes
that possible — unlike a single JSON array, a bad line can't corrupt
sibling records).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class RunLogEntry:
    vibe_prompt: str
    requested_count: int
    generated_count: int
    accepted_count: int
    final_track_ids: list[str]
    action: str                                # "append" or "create"
    mode: Optional[str] = None
    target_playlist_id: Optional[str] = None
    dropped_summary: dict = field(default_factory=dict)
    # dropped_summary above is curation's own drop reasons (dedupe/explicit/
    # artist-cap). resolver_dropped_summary is the earlier, resolver-stage
    # reasons (no_search_results/artist_mismatch/title_mismatch/below_threshold/
    # not_playable_in_market/duplicate) — kept as a separate field rather than
    # merged into dropped_summary because the two are genuinely different
    # pipeline stages, and merging them would make it impossible to tell "the
    # LLM suggested something that doesn't exist" apart from "curation
    # filtered something that does." Added 2026-08-18 after a real support
    # question ("why did more than half my songs not get approved?") turned
    # out to be unanswerable from the log as it existed before this field —
    # dropped_summary was empty even on a run where the resolver alone
    # rejected 60% of generated candidates.
    resolver_dropped_summary: dict = field(default_factory=dict)
    # Whether the "Prefer lesser-known songs" / "Ignore recently-used songs"
    # checkboxes were actually on for this run — added 2026-08-19 after a
    # direct instance of not being able to tell: the owner asked whether a
    # run's disappointing results meant a prompt fix wasn't working, and it
    # turned out to be unanswerable from the log which toggles were even in
    # effect for that specific run. False by default so old-and-new code
    # constructing an entry without these still works (mirrors dropped_summary/
    # resolver_dropped_summary's default_factory pattern above).
    avoid_obvious: bool = False
    ignore_recently_used: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class RunLog:
    """Append-only JSONL log at `path`. Each line is one RunLogEntry."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, entry: RunLogEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(entry), sort_keys=True))
            f.write("\n")

    def read_all(self) -> list[dict]:
        """Return every valid entry as a dict, in append order. A
        malformed line is skipped (with a warning to stderr) rather than
        failing the whole read."""
        if not self.path.exists():
            return []
        entries = []
        for lineno, line in enumerate(self.path.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  ! run log {self.path}:{lineno} is corrupted ({e}); skipping", file=sys.stderr)
        return entries


# ─────────────────────────────────────────────────────────────────────────────
# Demo — runs fully offline
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = RunLog(Path(tmp) / "run_log.jsonl")

        log.append(RunLogEntry(
            vibe_prompt="warm 60s-70s soul for Sunday brunch",
            requested_count=15,
            generated_count=21,
            accepted_count=17,
            final_track_ids=["t1", "t2", "t3"],
            action="append",
            mode="brunch",
            target_playlist_id="pl_brunch_123",
            dropped_summary={"already_in_playlist": 2, "explicit_track": 1},
        ))
        log.append(RunLogEntry(
            vibe_prompt="moody late-night jazz",
            requested_count=10,
            generated_count=14,
            accepted_count=10,
            final_track_ids=["t4", "t5"],
            action="create",
        ))

        print(f"Log written to {log.path}")
        for entry in log.read_all():
            print(f"  [{entry['timestamp']}] {entry['vibe_prompt']!r} -> "
                  f"{len(entry['final_track_ids'])} tracks, action={entry['action']}")


if __name__ == "__main__":
    _demo()
