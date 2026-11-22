"""Tests for search matching and progress sync decisions."""

from __future__ import annotations

import pytest

from music_assistant.providers.ilpost.helpers import match_podcasts, position_to_sync
from music_assistant.providers.ilpost.models import IlPostPodcast

PODCASTS = [
    IlPostPodcast(id="1", title="Morning", author="Francesco Costa"),
    IlPostPodcast(id="2", title="Live Baricco", author="Alessandro Baricco"),
    IlPostPodcast(id="3", title="Mai più", author="Someone", description="Con Baricco"),
    IlPostPodcast(id="4", title="Wild Baricco"),
]


def test_title_matches_come_first() -> None:
    """Title matches keep their order and come before author or description matches."""
    assert [p.id for p in match_podcasts(PODCASTS, "BARICCO")] == ["2", "4", "3"]


@pytest.mark.parametrize("query", ["", "   ", "weekend"])
def test_no_match(query: str) -> None:
    """Empty queries and unknown words match nothing."""
    assert match_podcasts(PODCASTS, query) == []


@pytest.mark.parametrize(
    ("fully_played", "position", "duration", "is_playing", "expected"),
    [
        # progress while playing is stored as is
        (False, 120, 1800, True, 120),
        # the end of the episode is stored as the full duration
        (True, 1795, 1800, False, 1800),
        # a skip reported as fully played keeps the real position
        (True, 300, 1800, False, 300),
        # without a duration, completion cannot be told apart from a skip
        (True, 300, 0, False, 300),
        # marked as unplayed
        (False, 0, 1800, False, 0),
        # the start of playback stores nothing
        (False, 0, 1800, True, None),
    ],
)
def test_position_to_sync(
    fully_played: bool, position: int, duration: int, is_playing: bool, expected: int | None
) -> None:
    """Playback reports map to the position stored on Il Post."""
    assert position_to_sync(fully_played, position, duration, is_playing) == expected
