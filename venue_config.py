"""
venue_config.py — venue-specific configuration

Local JSON file, no network — this is what closes three of the "built but
never wired up" gaps flagged in CLAUDE.md's punch list:
  - generator.py's house_taste grounding (CLAUDE.md calls this the single
    biggest quality lever) had no source to pull from
  - generator.py's blocklist enforcement had nothing to enforce
  - modes.resolve_target_playlist()'s standing_playlists had no config

A non-technical owner edits venue_config.json directly (it's plain JSON,
no code) rather than this needing a settings UI. A missing or malformed
file degrades to empty defaults with a stderr warning, same "surface,
don't crash the run" pattern as curation.RecentlyUsedLog and
logging_utils.RunLog.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "venue_config.json"


@dataclass
class VenueConfig:
    house_taste_playlist_ids: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=list)
    standing_playlists: dict[str, str] = field(default_factory=dict)   # mode -> playlist_id


def load_venue_config(path=DEFAULT_CONFIG_PATH) -> VenueConfig:
    p = Path(path)
    if not p.exists():
        return VenueConfig()

    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(f"  ! venue config at {p} is corrupted ({e}); using defaults", file=sys.stderr)
        return VenueConfig()

    if not isinstance(raw, dict):
        print(f"  ! venue config at {p} has an unexpected shape; using defaults", file=sys.stderr)
        return VenueConfig()

    return VenueConfig(
        house_taste_playlist_ids=list(raw.get("house_taste_playlist_ids", []) or []),
        blocklist=list(raw.get("blocklist", []) or []),
        standing_playlists=dict(raw.get("standing_playlists", {}) or {}),
    )
