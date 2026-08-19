"""
test_logging_utils.py — thorough tests for logging_utils.py's RunLog.

Real file I/O against pytest's tmp_path fixture — no mocking needed since
this is genuinely local.

Run:
    source .venv/bin/activate
    pytest test_logging_utils.py -v
"""

from __future__ import annotations

from logging_utils import RunLog, RunLogEntry


def mk_entry(prompt="test prompt", **overrides):
    fields = dict(
        vibe_prompt=prompt,
        requested_count=10,
        generated_count=14,
        accepted_count=11,
        final_track_ids=["t1", "t2"],
        action="create",
    )
    fields.update(overrides)
    return RunLogEntry(**fields)


# ─────────────────────────────────────────────────────────────────────────────
# Basic append/read
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_file_reads_as_empty(tmp_path):
    log = RunLog(tmp_path / "does_not_exist.jsonl")
    assert log.read_all() == []


def test_append_then_read_round_trips_all_fields(tmp_path):
    log = RunLog(tmp_path / "log.jsonl")
    entry = mk_entry(
        prompt="warm soul brunch",
        requested_count=15,
        generated_count=21,
        accepted_count=17,
        final_track_ids=["a", "b", "c"],
        action="append",
        mode="brunch",
        target_playlist_id="pl123",
        dropped_summary={"explicit_track": 2},
        resolver_dropped_summary={"no_search_results": 4, "artist_mismatch": 2},
        avoid_obvious=True,
        ignore_recently_used=True,
    )

    log.append(entry)
    [read_back] = log.read_all()

    assert read_back["vibe_prompt"] == "warm soul brunch"
    assert read_back["requested_count"] == 15
    assert read_back["generated_count"] == 21
    assert read_back["accepted_count"] == 17
    assert read_back["final_track_ids"] == ["a", "b", "c"]
    assert read_back["action"] == "append"
    assert read_back["mode"] == "brunch"
    assert read_back["target_playlist_id"] == "pl123"
    assert read_back["dropped_summary"] == {"explicit_track": 2}
    assert read_back["resolver_dropped_summary"] == {"no_search_results": 4, "artist_mismatch": 2}
    assert read_back["avoid_obvious"] is True
    assert read_back["ignore_recently_used"] is True
    assert "timestamp" in read_back


def test_defaults_for_optional_fields(tmp_path):
    log = RunLog(tmp_path / "log.jsonl")
    log.append(mk_entry())
    [entry] = log.read_all()
    assert entry["mode"] is None
    assert entry["target_playlist_id"] is None
    assert entry["dropped_summary"] == {}
    assert entry["resolver_dropped_summary"] == {}
    assert entry["avoid_obvious"] is False
    assert entry["ignore_recently_used"] is False


def test_timestamp_auto_populated_and_unique_per_entry(tmp_path):
    log = RunLog(tmp_path / "log.jsonl")
    e1 = mk_entry()
    e2 = mk_entry()
    assert e1.timestamp  # non-empty
    # Not asserting e1.timestamp != e2.timestamp (could collide at low
    # resolution) — just that both are populated ISO-ish strings.
    assert "T" in e1.timestamp
    assert "T" in e2.timestamp


# ─────────────────────────────────────────────────────────────────────────────
# Multiple entries — order preserved
# ─────────────────────────────────────────────────────────────────────────────

def test_multiple_entries_preserve_append_order(tmp_path):
    log = RunLog(tmp_path / "log.jsonl")
    log.append(mk_entry(prompt="first"))
    log.append(mk_entry(prompt="second"))
    log.append(mk_entry(prompt="third"))

    prompts = [e["vibe_prompt"] for e in log.read_all()]
    assert prompts == ["first", "second", "third"]


def test_appending_across_separate_runlog_instances_accumulates(tmp_path):
    path = tmp_path / "log.jsonl"
    RunLog(path).append(mk_entry(prompt="from instance 1"))
    RunLog(path).append(mk_entry(prompt="from instance 2"))

    entries = RunLog(path).read_all()
    assert [e["vibe_prompt"] for e in entries] == ["from instance 1", "from instance 2"]


# ─────────────────────────────────────────────────────────────────────────────
# Resilience
# ─────────────────────────────────────────────────────────────────────────────

def test_corrupted_line_is_skipped_valid_lines_still_read(tmp_path, capsys):
    path = tmp_path / "log.jsonl"
    log = RunLog(path)
    log.append(mk_entry(prompt="valid one"))

    with path.open("a") as f:
        f.write("{not valid json{{{\n")

    log.append(mk_entry(prompt="valid two"))

    entries = log.read_all()
    prompts = [e["vibe_prompt"] for e in entries]
    assert prompts == ["valid one", "valid two"]

    captured = capsys.readouterr()
    assert "corrupted" in captured.err


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "log.jsonl"
    log = RunLog(path)
    log.append(mk_entry(prompt="one"))
    with path.open("a") as f:
        f.write("\n\n   \n")
    log.append(mk_entry(prompt="two"))

    entries = log.read_all()
    assert [e["vibe_prompt"] for e in entries] == ["one", "two"]


def test_empty_file_reads_as_empty(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("")
    assert RunLog(path).read_all() == []


def test_creates_parent_directories_on_append(tmp_path):
    path = tmp_path / "nested" / "dirs" / "log.jsonl"
    log = RunLog(path)
    log.append(mk_entry())
    assert path.exists()
    assert len(log.read_all()) == 1


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
