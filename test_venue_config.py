"""
test_venue_config.py — tests for venue_config.py's JSON-backed config loader.

Fully offline, real file I/O against tmp_path.

Run:
    source .venv/bin/activate
    pytest test_venue_config.py -v
"""

from __future__ import annotations

import json

from venue_config import VenueConfig, load_venue_config


def test_missing_file_returns_defaults(tmp_path):
    config = load_venue_config(tmp_path / "does_not_exist.json")
    assert config == VenueConfig()
    assert config.house_taste_playlist_ids == []
    assert config.blocklist == []
    assert config.standing_playlists == {}


def test_loads_all_fields(tmp_path):
    path = tmp_path / "venue_config.json"
    path.write_text(json.dumps({
        "house_taste_playlist_ids": ["pl_house1", "pl_house2"],
        "blocklist": ["Nickelback", "Baby Shark"],
        "standing_playlists": {"brunch": "pl_brunch", "dinner": "pl_dinner"},
    }))

    config = load_venue_config(path)

    assert config.house_taste_playlist_ids == ["pl_house1", "pl_house2"]
    assert config.blocklist == ["Nickelback", "Baby Shark"]
    assert config.standing_playlists == {"brunch": "pl_brunch", "dinner": "pl_dinner"}


def test_partial_file_fills_missing_fields_with_defaults(tmp_path):
    path = tmp_path / "venue_config.json"
    path.write_text(json.dumps({"blocklist": ["Nickelback"]}))

    config = load_venue_config(path)

    assert config.blocklist == ["Nickelback"]
    assert config.house_taste_playlist_ids == []
    assert config.standing_playlists == {}


def test_corrupted_json_falls_back_to_defaults(tmp_path, capsys):
    path = tmp_path / "venue_config.json"
    path.write_text("{not valid json{{{")

    config = load_venue_config(path)

    assert config == VenueConfig()
    assert "corrupted" in capsys.readouterr().err


def test_unexpected_shape_falls_back_to_defaults(tmp_path, capsys):
    path = tmp_path / "venue_config.json"
    path.write_text(json.dumps(["not", "a", "dict"]))

    config = load_venue_config(path)

    assert config == VenueConfig()
    assert "unexpected shape" in capsys.readouterr().err


def test_null_field_values_do_not_crash(tmp_path):
    # Defensive: a hand-edited JSON file might set a field to null instead
    # of omitting it.
    path = tmp_path / "venue_config.json"
    path.write_text(json.dumps({"blocklist": None, "standing_playlists": None}))

    config = load_venue_config(path)

    assert config.blocklist == []
    assert config.standing_playlists == {}


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
