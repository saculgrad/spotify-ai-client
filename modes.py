"""
modes.py — day-part "modes" and target-playlist resolution

CLAUDE.md's spec (Phase 3 polish): "multiple house 'modes' (brunch/dinner/
late)" and "topping up a small set of standing playlists" vs. a new
playlist per request. This module is the pure-logic glue between those
ideas and the rest of the pipeline:

- DAY_PART_PRESETS + build_generation_request() turn a mode name into a
  pre-filled generator.GenerationRequest (explicit overrides always win
  over the preset).
- resolve_target_playlist() decides whether a run should append to an
  existing standing playlist or create a new one.
- current_day_part() is the "scheduling" piece — a pure function mapping a
  timestamp to which day-part window it falls in. It does NOT set up any
  actual recurring execution: wiring this into cron/launchd/Task Scheduler
  is a one-time machine-configuration step that belongs to whoever owns
  the box the tool runs on, not something to script here.

Nothing in this module touches Spotify or an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

from generator import GenerationRequest

# ─────────────────────────────────────────────────────────────────────────────
# Day-part presets
# ─────────────────────────────────────────────────────────────────────────────
# "vibe_hint" is folded into the free-text vibe_prompt the LLM sees; every
# other key is a direct GenerationRequest field override.

DAY_PART_PRESETS: dict[str, dict] = {
    "brunch": {
        "vibe_hint": "warm, easygoing, mid-tempo, daytime energy",
        "explicit_ok": False,
        "avoid_obvious": False,
    },
    "dinner": {
        "vibe_hint": "sophisticated, mellow, good for conversation without overpowering it",
        "explicit_ok": False,
        "avoid_obvious": True,
    },
    "late": {
        "vibe_hint": "moodier, a bit more energy, can lean edgier",
        "explicit_ok": True,
        "avoid_obvious": False,
    },
}

# (window_start, window_end) in local time, end exclusive. A window that
# wraps past midnight (like "late") is expressed with end < start and
# handled specially in current_day_part().
DAY_PART_SCHEDULE: dict[str, tuple[time, time]] = {
    "brunch": (time(9, 0), time(13, 0)),
    "dinner": (time(17, 0), time(21, 0)),
    "late": (time(21, 0), time(2, 0)),   # wraps past midnight
}


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in DAY_PART_PRESETS:
        raise ValueError(f"Unknown day-part mode {mode!r}. Known modes: {sorted(DAY_PART_PRESETS)}")
    return normalized


def build_generation_request(
    vibe_prompt: str,
    track_count: int,
    mode: Optional[str] = None,
    **overrides,
) -> GenerationRequest:
    """
    Build a GenerationRequest, optionally seeded from a day-part preset.
    Preset fields are applied first; anything in **overrides is applied on
    top and always wins, so a caller can use a preset as a starting point
    and still override any individual field.
    """
    fields: dict = {}
    combined_prompt = vibe_prompt

    if mode:
        normalized = _normalize_mode(mode)
        preset = dict(DAY_PART_PRESETS[normalized])
        hint = preset.pop("vibe_hint", None)
        fields.update(preset)
        if hint:
            combined_prompt = f"{vibe_prompt} ({hint})" if vibe_prompt else hint

    fields.update(overrides)
    return GenerationRequest(vibe_prompt=combined_prompt, track_count=track_count, **fields)


# ─────────────────────────────────────────────────────────────────────────────
# Target playlist resolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TargetResolution:
    action: str                          # "append" or "create"
    playlist_id: Optional[str] = None    # set when action == "append"
    playlist_name: Optional[str] = None  # suggested name when action == "create"


def resolve_target_playlist(
    *,
    mode: Optional[str] = None,
    explicit_playlist_id: Optional[str] = None,
    standing_playlists: Optional[dict[str, str]] = None,
) -> TargetResolution:
    """
    Decide whether this run appends to an existing playlist or creates a
    new one. Precedence:
      1. explicit_playlist_id always wins outright (caller knows exactly
         where they want this to go).
      2. Otherwise, if `mode` names a known day-part AND that day-part has
         a standing playlist configured, append there.
      3. Otherwise, create a new playlist (named after the mode if one was
         given, otherwise left for the caller to name).

    `standing_playlists` keys must already be normalized day-part names
    (lowercase, matching DAY_PART_PRESETS) — this function normalizes the
    `mode` argument to match, but doesn't reach into the caller's dict.
    """
    standing_playlists = standing_playlists or {}

    if explicit_playlist_id:
        return TargetResolution(action="append", playlist_id=explicit_playlist_id)

    if mode:
        normalized = _normalize_mode(mode)
        if normalized in standing_playlists:
            return TargetResolution(action="append", playlist_id=standing_playlists[normalized])
        return TargetResolution(action="create", playlist_name=f"{normalized.title()} Mix")

    return TargetResolution(action="create", playlist_name=None)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduling — pure "which window are we in" lookup only
# ─────────────────────────────────────────────────────────────────────────────

def current_day_part(now: Optional[datetime] = None) -> Optional[str]:
    """
    Return the day-part name whose window `now` (local time) falls into,
    or None if it's outside every configured window. Windows are checked
    in DAY_PART_SCHEDULE's insertion order; the first match wins, so keep
    windows non-overlapping if you edit the schedule.
    """
    current = (now or datetime.now()).time()
    for name, (start, end) in DAY_PART_SCHEDULE.items():
        if start <= end:
            if start <= current < end:
                return name
        else:
            # wraps past midnight, e.g. 21:00 -> 02:00
            if current >= start or current < end:
                return name
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Demo — runs fully offline
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    req = build_generation_request(
        "songs for the dining room",
        track_count=20,
        mode="dinner",
        artist_diversity_cap=2,
    )
    print("Generation request built from 'dinner' preset:")
    print(f"  vibe_prompt: {req.vibe_prompt!r}")
    print(f"  explicit_ok: {req.explicit_ok}, avoid_obvious: {req.avoid_obvious}, "
          f"artist_diversity_cap: {req.artist_diversity_cap}")

    target = resolve_target_playlist(mode="brunch", standing_playlists={"brunch": "pl_brunch_123"})
    print(f"\nTarget resolution for 'brunch': {target}")

    target2 = resolve_target_playlist(mode="late", standing_playlists={"brunch": "pl_brunch_123"})
    print(f"Target resolution for 'late' (no standing playlist): {target2}")

    for hour in [10, 18, 23, 1]:
        probe = datetime.now().replace(hour=hour, minute=0)
        print(f"current_day_part at {hour:02d}:00 -> {current_day_part(probe)}")


if __name__ == "__main__":
    _demo()
