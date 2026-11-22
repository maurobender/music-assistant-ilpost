"""Tests for parsing the Il Post API responses."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from music_assistant.helpers.api import parse_value
from music_assistant.providers.ilpost.models import (
    EpisodeProgress,
    HomeBlock,
    IlPostEpisode,
    IlPostPodcast,
    parse_id,
    parse_int,
    parse_url,
)

from . import payloads


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (123, "123"),
        ("123", "123"),
        (" 42 ", "42"),
        ("../user", None),
        ("1/2", None),
        ("", None),
        (None, None),
        ("١٢", None),
        (-5, None),
    ],
)
def test_parse_id_accepts_only_ascii_digits(value: object, expected: str | None) -> None:
    """Ids end up in URL paths, so anything but plain digits is rejected."""
    assert parse_id(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(10, 10), ("10", 10), ("12.7", 12), (None, 0), ("x", 0), (-3, 0)]
)
def test_parse_int_is_lenient(value: object, expected: int) -> None:
    """Numeric fields come as numbers, strings or null."""
    assert parse_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a.mp3", "https://example.com/a.mp3"),
        ("http://example.com/a.mp3", "http://example.com/a.mp3"),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("/relative.mp3", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_url_only_allows_absolute_http(value: object, expected: str | None) -> None:
    """Only http(s) URLs may reach the stream or the frontend."""
    assert parse_url(value) == expected


def test_podcast_from_api() -> None:
    """A podcast is parsed with its HTML entities decoded."""
    podcast = IlPostPodcast.from_api(payloads.podcast())
    assert podcast == IlPostPodcast(
        id="1001",
        title="Podcast \u2013 One",
        author="Jane Doe",
        description="A podcast about things",
        image="https://example.com/cover.jpg",
        share_url="https://example.com/podcasts/one/",
        episode_count=3,
    )


def test_podcast_without_id_is_dropped() -> None:
    """Records without a usable id never reach the provider."""
    assert IlPostPodcast.from_api(payloads.podcast(podcast_id=None)) is None  # type: ignore[arg-type]


def test_podcast_survives_the_cache_round_trip() -> None:
    """The podcast listing is cached, so it must rebuild from its serialized form."""
    podcast = IlPostPodcast.from_api(payloads.podcast())
    assert podcast is not None
    rebuilt = parse_value("podcasts", [podcast.to_dict()], list[IlPostPodcast])
    assert rebuilt == [podcast]


def test_episode_from_api() -> None:
    """An episode carries its podcast, audio URL, duration and date."""
    episode = IlPostEpisode.from_api(payloads.episode())
    assert episode is not None
    assert episode.id == "5001"
    assert episode.podcast_id == "1001"
    assert episode.podcast_title == "Podcast One"
    assert episode.audio_url == "https://example.com/audio/5001.mp3"
    assert episode.duration == 1800
    assert episode.description == "<p>Show notes</p>"
    assert episode.published == datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


def test_episode_falls_back_to_minutes_and_podcast_image() -> None:
    """Missing milliseconds and image fall back to minutes and the podcast cover."""
    episode = IlPostEpisode.from_api(payloads.episode(milliseconds=None, image=None, date="nope"))
    assert episode is not None
    assert episode.duration == 1800
    assert episode.image == "https://example.com/p.jpg"
    assert episode.published is None


def test_episode_rejects_unsafe_audio_url() -> None:
    """A non-http audio URL leaves the episode without audio instead of streaming it."""
    episode = IlPostEpisode.from_api(payloads.episode(episode_raw_url="file:///etc/passwd"))
    assert episode is not None
    assert episode.audio_url is None


@pytest.mark.parametrize("parent", [None, {}, {"id": "abc"}, "1001"])
def test_episode_without_valid_podcast_is_dropped(parent: object) -> None:
    """The episode item id needs the podcast id, so episodes without one are dropped."""
    assert IlPostEpisode.from_api(payloads.episode(parent=parent)) is None


def test_progress_from_api() -> None:
    """A state entry yields the episode id and its position and duration."""
    assert EpisodeProgress.from_api(payloads.state(5001, 600, 1800)) == (
        "5001",
        EpisodeProgress(position=600, duration=1800),
    )


@pytest.mark.parametrize("entry", [{}, {"item": None}, {"item": {"id": 1, "data": None}}])
def test_invalid_progress_is_dropped(entry: dict[str, object]) -> None:
    """Malformed state entries are ignored."""
    assert EpisodeProgress.from_api(entry) is None


@pytest.mark.parametrize(
    ("progress", "fallback", "expected"),
    [
        (EpisodeProgress(1710, 1800), 0, True),
        (EpisodeProgress(1700, 1800), 0, False),
        (EpisodeProgress(1710, 0), 1800, True),
        (EpisodeProgress(1710, 0), 0, False),
    ],
)
def test_progress_completion(progress: EpisodeProgress, fallback: int, expected: bool) -> None:
    """An episode counts as listened from 95% of its duration."""
    assert progress.is_completed(fallback_duration=fallback) is expected


def test_home_block_keeps_only_valid_episodes() -> None:
    """Home blocks keep their label and only the entries that are valid episodes."""
    block = HomeBlock.from_api(
        {
            "key": "archivio",
            "meta": {"label": "Archivio", "show_label": True},
            "data": [payloads.episode(), {"object": "podcast", "id": 1}, "junk"],
        }
    )
    assert block is not None
    assert block.label == "Archivio"
    assert [episode.id for episode in block.episodes] == ["5001"]


def test_home_block_hidden_label() -> None:
    """A label the site does not show is not used."""
    block = HomeBlock.from_api({"key": "x", "meta": {"label": "Hidden", "show_label": False}})
    assert block == HomeBlock(key="x", label=None, episodes=())
