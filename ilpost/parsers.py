"""Conversion of Il Post API models into Music Assistant media items."""

from __future__ import annotations

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    UniqueList,
)

from .constants import HOME_BLOCK_NAMES
from .models import EpisodeProgress, HomeBlock, IlPostEpisode, IlPostPodcast, parse_id


def episode_item_id(podcast_id: str, episode_id: str) -> str:
    """Return the Music Assistant item id of an episode."""
    # the podcast id is part of it because the single-episode endpoint needs both
    return f"{podcast_id}:{episode_id}"


def split_episode_item_id(item_id: str) -> tuple[str, str]:
    """Return the (podcast id, episode id) of an episode item id, validated."""
    podcast_id, _, episode_id = item_id.partition(":")
    valid_podcast_id, valid_episode_id = parse_id(podcast_id), parse_id(episode_id)
    if valid_podcast_id is None or valid_episode_id is None:
        raise MediaNotFoundError(f"Invalid Il Post episode id: {item_id!r}")
    return valid_podcast_id, valid_episode_id


def audio_format(url: str | None) -> AudioFormat:
    """Return the audio format of an episode file, MP3 unless the URL says otherwise."""
    content_type = ContentType.try_parse(url) if url else ContentType.UNKNOWN
    return AudioFormat(
        content_type=ContentType.MP3 if content_type == ContentType.UNKNOWN else content_type
    )


def _images(url: str | None, instance_id: str) -> UniqueList[MediaItemImage]:
    if not url:
        return UniqueList()
    return UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB, path=url, provider=instance_id, remotely_accessible=True
            )
        ]
    )


def parse_podcast(podcast: IlPostPodcast, domain: str, instance_id: str) -> Podcast:
    """Return the Music Assistant podcast for an Il Post podcast."""
    return Podcast(
        item_id=podcast.id,
        provider=instance_id,
        name=podcast.title,
        publisher=podcast.author or "Il Post",
        total_episodes=podcast.episode_count or None,
        provider_mappings={
            ProviderMapping(
                item_id=podcast.id,
                provider_domain=domain,
                provider_instance=instance_id,
                url=podcast.share_url,
            )
        },
        metadata=MediaItemMetadata(
            description=podcast.description or None,
            images=_images(podcast.image, instance_id),
        ),
    )


def parse_episode(
    episode: IlPostEpisode, domain: str, instance_id: str, position: int = 0
) -> PodcastEpisode:
    """
    Return the Music Assistant episode for an Il Post episode.

    :param episode: The Il Post episode.
    :param domain: The provider domain.
    :param instance_id: The provider instance id.
    :param position: The episode's position in its podcast, 0 when unknown.
    """
    item_id = episode_item_id(episode.podcast_id, episode.id)
    item = PodcastEpisode(
        item_id=item_id,
        provider=instance_id,
        name=episode.title or "Episode",
        podcast=ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=episode.podcast_id,
            provider=instance_id,
            name=episode.podcast_title,
        ),
        position=position,
        duration=episode.duration,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
                available=episode.audio_url is not None,
                audio_format=audio_format(episode.audio_url),
                url=episode.share_url,
            )
        },
    )
    item.metadata.description = episode.description
    item.metadata.release_date = episode.published
    item.metadata.images = _images(episode.image, instance_id)
    return item


def apply_progress(episode: PodcastEpisode, progress: EpisodeProgress | None) -> None:
    """
    Set the played state and resume point of an episode from its stored progress.

    Both fields are always set, so a library sync also clears stale values.
    """
    progress = progress or EpisodeProgress(position=0)
    completed = progress.is_completed(fallback_duration=episode.duration or 0)
    episode.fully_played = completed
    episode.resume_position_ms = 0 if completed else progress.position * 1000


def parse_home_block(block: HomeBlock, domain: str, instance_id: str) -> RecommendationFolder:
    """Return the recommendation folder for a block of the Il Post home page."""
    items: list[MediaItemType] = [
        parse_episode(episode, domain, instance_id) for episode in block.episodes
    ]
    return RecommendationFolder(
        item_id=block.key,
        provider=instance_id,
        name=block.label
        or HOME_BLOCK_NAMES.get(block.key)
        or block.key.replace("_", " ").capitalize(),
        items=UniqueList(items),
    )
