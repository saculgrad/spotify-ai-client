"""
test_generator.py — unit tests for generator.py against a mock Anthropic
client (no network / no API key required).

Mirrors test_resolver.py's approach: a lightweight fake client stands in for
`anthropic.Anthropic()` so these tests run offline and fast.

Run:
    source .venv/bin/activate
    pytest test_generator.py -v
"""

from __future__ import annotations

import json

import pytest

from generator import (
    MAX_ATTEMPTS,
    GenerationRequest,
    OVERGENERATION_FACTOR,
    generate_candidates,
)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Anthropic client
# ─────────────────────────────────────────────────────────────────────────────

class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, stop_reason="end_turn", text=None):
        self.stop_reason = stop_reason
        self.content = [FakeTextBlock(text)] if text is not None else []


class FakeMessages:
    """Returns queued FakeResponses in order; records every create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def payload_text(candidates, wanted_variants=None):
    return json.dumps({
        "wanted_variants": wanted_variants or [],
        "candidates": candidates,
    })


def cand(title, artist, reason="fits the vibe"):
    return {"title": title, "artist": artist, "reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

def test_generates_candidates_happy_path():
    text = payload_text(
        [cand("Ain't No Sunshine", "Bill Withers"), cand("Let's Stay Together", "Al Green")],
        wanted_variants=["live"],
    )
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="soul brunch", track_count=2)

    result = generate_candidates(client, req)

    assert result.requested_count == 2
    assert result.generated_count == 2
    assert result.malformed_count == 0
    assert result.blocked_count == 0
    assert result.wanted_variants == {"live"}
    assert [c.title for c in result.candidates] == ["Ain't No Sunshine", "Let's Stay Together"]
    assert [c.artist for c in result.candidates] == ["Bill Withers", "Al Green"]


def test_requests_overgenerated_count_in_prompt():
    import math

    text = payload_text([cand("Song", "Artist")])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=20)

    generate_candidates(client, req)

    expected_count = math.ceil(20 * OVERGENERATION_FACTOR)
    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert f"Generate exactly {expected_count} candidate tracks" in sent_prompt


def test_avoid_obvious_prompt_explicitly_overrides_party_vibe_assumption():
    """Regression test for a real quality bug: the owner reported that
    "Prefer lesser-known songs" barely changed anything for high-energy
    party vibes. A live A/B test against the real Claude API confirmed it —
    the original mild wording ("go a little deeper... lean on lesser-known
    artists") still returned essentially all-time chart-topping anthems for
    a "frat party" prompt (Toxic, All the Small Things, Ms. Jackson), while
    a stronger wording that explicitly overrides the model's apparent
    "party = must be a famous anthem" assumption produced genuinely deeper
    cuts for the identical prompt (Huey, Rich Boy, Young Jeezy, Lupe
    Fiasco's "Kick, Push" instead of his biggest hit). This just locks in
    that the strengthened wording actually reaches the prompt — the
    quality claim itself was validated live, not by this offline test."""
    text = payload_text([cand("Song", "Artist")])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=1, avoid_obvious=True)

    generate_candidates(client, req)

    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "even for high-energy/party requests" in sent_prompt
    assert "famous genre-defining anthem" in sent_prompt


def test_avoid_obvious_line_omitted_when_not_requested():
    text = payload_text([cand("Song", "Artist")])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=1, avoid_obvious=False)

    generate_candidates(client, req)

    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "avoid the most obvious" not in sent_prompt.lower()


# ─────────────────────────────────────────────────────────────────────────────
# previously_rejected — the "generate more" follow-up flow's rejection
# signal. Deliberately separate from blocklist (permanent, venue-wide hard
# filter): this is a soft, per-request steer, built fresh from one review
# session's removed/regenerate-requested tracks.
# ─────────────────────────────────────────────────────────────────────────────

def test_previously_rejected_reaches_the_prompt():
    text = payload_text([cand("Song", "Artist")])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(
        vibe_prompt="anything", track_count=1,
        previously_rejected=["Bill Withers - Ain't No Sunshine (reason: too slow)"],
    )

    generate_candidates(client, req)

    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "explicitly rejected them" in sent_prompt
    assert "Bill Withers - Ain't No Sunshine (reason: too slow)" in sent_prompt


def test_previously_rejected_line_omitted_when_empty():
    text = payload_text([cand("Song", "Artist")])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=1, previously_rejected=[])

    generate_candidates(client, req)

    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "explicitly rejected them" not in sent_prompt


# ─────────────────────────────────────────────────────────────────────────────
# Blocklist — hard filter in code, not trusted to the prompt alone
# ─────────────────────────────────────────────────────────────────────────────

def test_applies_blocklist_by_artist():
    text = payload_text([
        cand("Photograph", "Nickelback"),
        cand("Ain't No Sunshine", "Bill Withers"),
    ])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=2, blocklist=["Nickelback"])

    result = generate_candidates(client, req)

    assert result.blocked_count == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].artist == "Bill Withers"


def test_applies_blocklist_by_track_title():
    text = payload_text([
        cand("Baby Shark", "Pinkfong"),
        cand("Ain't No Sunshine", "Bill Withers"),
    ])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=2, blocklist=["Baby Shark"])

    result = generate_candidates(client, req)

    assert result.blocked_count == 1
    assert [c.title for c in result.candidates] == ["Ain't No Sunshine"]


# ─────────────────────────────────────────────────────────────────────────────
# Defensive parsing — malformed rows dropped, not crashed on
# ─────────────────────────────────────────────────────────────────────────────

def test_drops_candidate_with_empty_title_or_artist():
    text = payload_text([
        cand("", "Some Artist"),
        cand("Some Song", ""),
        cand("Ain't No Sunshine", "Bill Withers"),
    ])
    client = FakeClient([FakeResponse(text=text)])
    req = GenerationRequest(vibe_prompt="anything", track_count=3)

    result = generate_candidates(client, req)

    assert result.malformed_count == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Ain't No Sunshine"


# ─────────────────────────────────────────────────────────────────────────────
# Retry behavior
# ─────────────────────────────────────────────────────────────────────────────

def test_retries_after_refusal_then_succeeds():
    good_text = payload_text([cand("Song", "Artist")])
    client = FakeClient([
        FakeResponse(stop_reason="refusal"),
        FakeResponse(text=good_text),
    ])
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    result = generate_candidates(client, req)

    assert len(client.messages.calls) == 2
    assert len(result.candidates) == 1


def test_retries_after_max_tokens_truncation_then_succeeds():
    good_text = payload_text([cand("Song", "Artist")])
    client = FakeClient([
        FakeResponse(stop_reason="max_tokens", text="{not complete"),
        FakeResponse(text=good_text),
    ])
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    result = generate_candidates(client, req)

    assert len(client.messages.calls) == 2
    assert len(result.candidates) == 1


def test_retries_after_malformed_json_then_succeeds():
    good_text = payload_text([cand("Song", "Artist")])
    client = FakeClient([
        FakeResponse(text="not valid json {{{"),
        FakeResponse(text=good_text),
    ])
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    result = generate_candidates(client, req)

    assert len(client.messages.calls) == 2
    assert len(result.candidates) == 1


def test_raises_after_exhausting_all_attempts():
    client = FakeClient([FakeResponse(stop_reason="refusal") for _ in range(MAX_ATTEMPTS)])
    req = GenerationRequest(vibe_prompt="anything", track_count=1)

    with pytest.raises(RuntimeError):
        generate_candidates(client, req)

    assert len(client.messages.calls) == MAX_ATTEMPTS


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
