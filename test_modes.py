"""
test_modes.py — thorough tests for modes.py's presets, target resolution,
and scheduling window logic. Fully offline.

Run:
    source .venv/bin/activate
    pytest test_modes.py -v
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from modes import (
    DAY_PART_PRESETS,
    build_generation_request,
    current_day_part,
    resolve_target_playlist,
)


# ─────────────────────────────────────────────────────────────────────────────
# build_generation_request
# ─────────────────────────────────────────────────────────────────────────────

def test_no_mode_passes_prompt_through_unchanged():
    req = build_generation_request("warm soul brunch music", track_count=10)
    assert req.vibe_prompt == "warm soul brunch music"
    assert req.track_count == 10
    # defaults from GenerationRequest itself, not touched by any preset
    assert req.explicit_ok is True
    assert req.avoid_obvious is False


def test_known_mode_applies_preset_fields():
    req = build_generation_request("music for the room", track_count=15, mode="brunch")
    assert req.explicit_ok is False
    assert req.avoid_obvious is False
    assert "warm, easygoing" in req.vibe_prompt


def test_mode_is_case_insensitive_and_trims_whitespace():
    req = build_generation_request("x", track_count=5, mode="  BRUNCH  ")
    assert req.explicit_ok is False


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="Unknown day-part mode"):
        build_generation_request("x", track_count=5, mode="elevenses")


def test_explicit_override_wins_over_preset():
    # brunch preset sets explicit_ok=False; explicit override should win.
    req = build_generation_request("x", track_count=5, mode="brunch", explicit_ok=True)
    assert req.explicit_ok is True


def test_override_field_not_touched_by_preset_still_applies():
    req = build_generation_request("x", track_count=5, mode="dinner", artist_diversity_cap=3)
    assert req.artist_diversity_cap == 3
    assert req.avoid_obvious is True  # untouched preset field still applied


def test_empty_vibe_prompt_with_mode_uses_hint_alone():
    req = build_generation_request("", track_count=5, mode="late")
    assert req.vibe_prompt == DAY_PART_PRESETS["late"]["vibe_hint"]


def test_all_presets_have_a_vibe_hint():
    for name, preset in DAY_PART_PRESETS.items():
        assert preset.get("vibe_hint"), f"{name} preset is missing a vibe_hint"


# ─────────────────────────────────────────────────────────────────────────────
# resolve_target_playlist
# ─────────────────────────────────────────────────────────────────────────────

def test_explicit_playlist_id_always_wins():
    result = resolve_target_playlist(
        mode="brunch",
        explicit_playlist_id="pl_explicit",
        standing_playlists={"brunch": "pl_brunch"},
    )
    assert result.action == "append"
    assert result.playlist_id == "pl_explicit"


def test_mode_with_standing_playlist_appends():
    result = resolve_target_playlist(mode="dinner", standing_playlists={"dinner": "pl_dinner_1"})
    assert result.action == "append"
    assert result.playlist_id == "pl_dinner_1"


def test_mode_without_standing_playlist_creates_with_suggested_name():
    result = resolve_target_playlist(mode="late", standing_playlists={"brunch": "pl_brunch_1"})
    assert result.action == "create"
    assert result.playlist_name == "Late Mix"
    assert result.playlist_id is None


def test_mode_is_case_insensitive_for_standing_playlist_lookup():
    result = resolve_target_playlist(mode="DINNER", standing_playlists={"dinner": "pl_dinner_1"})
    assert result.action == "append"
    assert result.playlist_id == "pl_dinner_1"


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="Unknown day-part mode"):
        resolve_target_playlist(mode="elevenses")


def test_no_mode_no_explicit_id_creates_with_no_suggested_name():
    result = resolve_target_playlist()
    assert result.action == "create"
    assert result.playlist_name is None
    assert result.playlist_id is None


def test_empty_standing_playlists_dict_defaults_safely():
    result = resolve_target_playlist(mode="brunch", standing_playlists=None)
    assert result.action == "create"
    assert result.playlist_name == "Brunch Mix"


# ─────────────────────────────────────────────────────────────────────────────
# current_day_part — window boundaries, including the midnight wrap
# ─────────────────────────────────────────────────────────────────────────────

def _at(hour, minute=0):
    return datetime(2026, 1, 1, hour, minute)


def test_current_day_part_inside_brunch_window():
    assert current_day_part(_at(10, 0)) == "brunch"


def test_current_day_part_inside_dinner_window():
    assert current_day_part(_at(18, 30)) == "dinner"


def test_current_day_part_late_window_before_midnight():
    assert current_day_part(_at(22, 0)) == "late"


def test_current_day_part_late_window_after_midnight():
    assert current_day_part(_at(1, 0)) == "late"


def test_current_day_part_outside_every_window_returns_none():
    # 14:00-17:00 and 2:00-9:00 are gaps in the configured schedule.
    assert current_day_part(_at(15, 0)) is None
    assert current_day_part(_at(5, 0)) is None


def test_current_day_part_start_boundary_is_inclusive():
    assert current_day_part(_at(9, 0)) == "brunch"     # brunch starts exactly at 9:00
    assert current_day_part(_at(17, 0)) == "dinner"    # dinner starts exactly at 17:00


def test_current_day_part_end_boundary_is_exclusive():
    assert current_day_part(_at(13, 0)) is None        # brunch ends exactly at 13:00
    assert current_day_part(_at(21, 0)) == "late"       # 21:00 is dinner's end AND late's start;
    # late's window starts at 21:00 and DAY_PART_SCHEDULE order means brunch/dinner are
    # checked first and don't claim 21:00 (dinner's end is exclusive), so late wins here.


def test_current_day_part_midnight_exact():
    assert current_day_part(_at(0, 0)) == "late"


def test_current_day_part_late_window_end_boundary_exclusive():
    assert current_day_part(_at(2, 0)) is None


def test_current_day_part_defaults_to_now_when_omitted():
    # Just verify it runs without error and returns a valid value (str or None).
    result = current_day_part()
    assert result is None or result in DAY_PART_PRESETS


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
