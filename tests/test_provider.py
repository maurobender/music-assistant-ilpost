"""Tests for the Il Post provider with a mocked API client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.providers.ilpost import IlPostProvider
from music_assistant.providers.ilpost.models import (
    EpisodeProgress,
    HomeBlock,
    IlPostEpisode,
    IlPostPodcast,
)
from music_assistant.providers.ilpost.parsers import parse_episode

from . import payloads

if TYPE_CHECKING:
    from music_assistant_models.media_items import PodcastEpisode


def _episode(episode_id: int, **overrides: Any) -> IlPostEpisode:
    episode = IlPostEpisode.from_api(payloads.episode(episode_id=episode_id, **overrides))
    assert episode is not None
    return episode


@pytest.fixture
def client() -> AsyncMock:
    """Return a mocked Il Post API client."""
    client = AsyncMock()
    podcast = IlPostPodcast.from_api(payloads.podcast())
    client.get_podcasts.return_value = [podcast]
    client.get_episodes.return_value = [
        _episode(3, date="2026-11-21T08:00:00+01:00"),
        _episode(2, date="2026-11-20T08:00:00+01:00"),
        _episode(1, date="2026-11-19T08:00:00+01:00"),
    ]
    client.get_progress.return_value = {}
    return client


@pytest.fixture
def provider(client: AsyncMock) -> IlPostProvider:
    """Return a provider backed by the mocked client and a cache that always misses."""
    mass = AsyncMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.create_task = MagicMock(side_effect=lambda coro, **_: asyncio.ensure_future(coro))
    manifest = MagicMock()
    manifest.domain = "ilpost"
    config = MagicMock()
    config.instance_id = "ilpost"
    config.get_value.return_value = None
    prov = IlPostProvider(mass, manifest, config)
    prov._client = client
    return prov


async def test_library_podcasts(provider: IlPostProvider) -> None:
    """The library holds every Il Post podcast."""
    podcasts = [p async for p in provider.get_library_podcasts()]
    assert [(p.item_id, p.name, p.publisher, p.total_episodes) for p in podcasts] == [
        ("1001", "Podcast \u2013 One", "Jane Doe", 3)
    ]
    assert podcasts[0].metadata.images
    assert podcasts[0].metadata.images[0].path == "https://example.com/cover.jpg"


async def test_get_unknown_podcast(provider: IlPostProvider) -> None:
    """A podcast that is not in the listing raises MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast("9999")


async def test_episodes_carry_rank_and_progress(
    provider: IlPostProvider, client: AsyncMock
) -> None:
    """Episodes are ranked by date and carry the progress stored on Il Post."""
    client.get_progress.return_value = {
        "3": EpisodeProgress(position=600, duration=1800),
        "2": EpisodeProgress(position=1795, duration=1800),
    }
    episodes = {e.item_id: e async for e in provider.get_podcast_episodes("1001")}
    newest, middle, oldest = episodes["1001:3"], episodes["1001:2"], episodes["1001:1"]
    assert newest.position > middle.position > oldest.position
    assert (newest.fully_played, newest.resume_position_ms) == (False, 600_000)
    assert (middle.fully_played, middle.resume_position_ms) == (True, 0)
    assert (oldest.fully_played, oldest.resume_position_ms) == (False, 0)
    client.get_progress.assert_awaited_once_with(["3", "2", "1"])


async def test_episodes_survive_a_progress_failure(
    provider: IlPostProvider, client: AsyncMock
) -> None:
    """When progress cannot be read, episodes are still listed without a played state."""
    client.get_progress.side_effect = ResourceTemporarilyUnavailable("down")
    episodes = [e async for e in provider.get_podcast_episodes("1001")]
    assert len(episodes) == 3
    assert all(e.fully_played is None for e in episodes)


async def test_stream_details(provider: IlPostProvider, client: AsyncMock) -> None:
    """The stream is the episode's MP3, played over plain HTTP."""
    client.get_episode.return_value = _episode(5001)
    stream = await provider.get_stream_details("1001:5001", MediaType.PODCAST_EPISODE)
    client.get_episode.assert_awaited_once_with("1001", "5001")
    assert stream.stream_type == StreamType.HTTP
    assert stream.path == "https://example.com/audio/5001.mp3"
    assert stream.audio_format.content_type == ContentType.MP3
    assert stream.duration == 1800
    assert stream.can_seek


async def test_stream_without_audio(provider: IlPostProvider, client: AsyncMock) -> None:
    """An episode without a usable audio URL cannot be streamed."""
    client.get_episode.return_value = _episode(5001, episode_raw_url=None)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("1001:5001", MediaType.PODCAST_EPISODE)


@pytest.mark.parametrize("item_id", ["5001", "1001:", ":5001", "1001:../x", "a:b"])
async def test_invalid_episode_item_ids(
    provider: IlPostProvider, client: AsyncMock, item_id: str
) -> None:
    """Malformed episode item ids are rejected without calling the API."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(item_id)
    client.get_episode.assert_not_awaited()


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (None, (False, 0, None)),
        (EpisodeProgress(position=120, duration=1800), (False, 120_000, None)),
        (EpisodeProgress(position=1800, duration=1800), (True, 0, None)),
    ],
)
async def test_resume_position(
    provider: IlPostProvider,
    client: AsyncMock,
    progress: EpisodeProgress | None,
    expected: tuple[bool, int, None],
) -> None:
    """The resume point comes from the progress stored on Il Post."""
    client.get_progress.return_value = {"5001": progress} if progress else {}
    result = await provider.get_resume_position("1001:5001", MediaType.PODCAST_EPISODE)
    assert result == expected


@pytest.fixture
def episode(provider: IlPostProvider) -> PodcastEpisode:
    """Return a Music Assistant episode of 30 minutes."""
    return parse_episode(_episode(5001), provider.domain, provider.instance_id)


async def test_on_played_stores_the_position(
    provider: IlPostProvider, client: AsyncMock, episode: PodcastEpisode
) -> None:
    """Playback progress of an episode is stored on Il Post."""
    await provider.on_played(MediaType.PODCAST_EPISODE, "1001:5001", True, 1798, episode)
    client.set_position.assert_awaited_once_with("5001", 1800)


async def test_on_played_survives_api_errors(
    provider: IlPostProvider, client: AsyncMock, episode: PodcastEpisode
) -> None:
    """A failed position write is logged and does not break playback."""
    client.set_position.side_effect = ResourceTemporarilyUnavailable("down")
    await provider.on_played(MediaType.PODCAST_EPISODE, "1001:5001", False, 60, episode, True)
    client.set_position.assert_awaited_once_with("5001", 60)


async def test_get_podcast_episode(provider: IlPostProvider, client: AsyncMock) -> None:
    """A single episode carries its stored progress."""
    client.get_episode.return_value = _episode(5001)
    client.get_progress.return_value = {"5001": EpisodeProgress(position=60, duration=1800)}
    episode = await provider.get_podcast_episode("1001:5001")
    assert episode.item_id == "1001:5001"
    assert episode.resume_position_ms == 60_000
    assert episode.podcast is not None
    assert episode.podcast.name == "Podcast One"


async def test_init_requires_credentials(provider: IlPostProvider) -> None:
    """Without credentials the provider does not start."""
    provider.get_setup_value = MagicMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(LoginFailed):
        await provider.handle_async_init()


async def test_on_played_ignores_other_media(provider: IlPostProvider, client: AsyncMock) -> None:
    """Only podcast episodes are synced."""
    await provider.on_played(MediaType.TRACK, "1001:5001", False, 10, MagicMock())
    client.set_position.assert_not_awaited()


async def test_search(provider: IlPostProvider) -> None:
    """Podcasts are found by title; other media types return nothing."""
    found = await provider.search("one", [MediaType.PODCAST])
    assert [p.item_id for p in found.podcasts] == ["1001"]
    assert not (await provider.search("one", [MediaType.TRACK])).podcasts


async def test_recommendations(provider: IlPostProvider, client: AsyncMock) -> None:
    """Home blocks become recommendation folders; empty blocks are skipped."""
    client.get_home.return_value = [
        HomeBlock(key="all_new_episodes_week", label="Nuovi episodi", episodes=(_episode(1),)),
        HomeBlock(key="archivio", label=None, episodes=(_episode(2),)),
        HomeBlock(key="empty", label=None, episodes=()),
    ]
    folders = await provider.get_recommendations()
    assert [(f.item_id, f.name, len(f.items)) for f in folders] == [
        ("all_new_episodes_week", "Nuovi episodi", 1),
        ("archivio", "Archivio", 1),
    ]
