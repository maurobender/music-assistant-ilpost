"""Il Post podcasts provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import (
    MediaItemType,
    Podcast,
    PodcastEpisode,
    RecommendationFolder,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider

from .api_client import IlPostClient
from .helpers import match_podcasts, position_to_sync
from .models import EpisodeProgress, IlPostPodcast
from .parsers import (
    apply_progress,
    audio_format,
    parse_episode,
    parse_home_block,
    parse_podcast,
    split_episode_item_id,
)

if TYPE_CHECKING:
    from datetime import datetime

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# errors after which progress sync is skipped instead of failing browsing or playback
TRANSIENT_ERRORS = (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return IlPostProvider(mass, manifest, config, SUPPORTED_FEATURES)


class IlPostProvider(MusicProvider):
    """Provider for the podcasts of Il Post."""

    _client: IlPostClient

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        email = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not email or not password:
            raise LoginFailed("Email and password are required for Il Post")
        self._client = IlPostClient(self.mass.http_session, self.logger, str(email), str(password))
        await self._client.login()

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Get every podcast published by Il Post."""
        for podcast in await self._get_podcasts():
            yield parse_podcast(podcast, self.domain, self.instance_id)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get full details for a podcast.

        :param prov_podcast_id: The Il Post podcast id.
        """
        # there is no single-podcast endpoint; the full listing is one cached call
        for podcast in await self._get_podcasts():
            if podcast.id == prov_podcast_id:
                return parse_podcast(podcast, self.domain, self.instance_id)
        raise MediaNotFoundError(f"podcast://{prov_podcast_id} not found on provider {self.domain}")

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get every episode of a podcast, with the progress stored on Il Post.

        :param prov_podcast_id: The Il Post podcast id.
        """
        episodes = await self._client.get_episodes(prov_podcast_id)
        progress = await self._get_progress([episode.id for episode in episodes])
        ranks = rank_episodes_by_date([episode.published for episode in episodes])
        for rank, episode in zip(ranks, episodes, strict=True):
            item = parse_episode(episode, self.domain, self.instance_id, position=rank)
            if progress is not None:
                apply_progress(item, progress.get(episode.id))
            yield item

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get full details for a podcast episode.

        :param prov_episode_id: The episode item id (podcast_id:episode_id).
        """
        podcast_id, episode_id = split_episode_item_id(prov_episode_id)
        episode = await self._client.get_episode(podcast_id, episode_id)
        item = parse_episode(episode, self.domain, self.instance_id)
        if (progress := await self._get_progress([episode_id])) is not None:
            apply_progress(item, progress.get(episode_id))
        return item

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get the stream of an episode: a public MP3 file that needs no authentication.

        :param item_id: The episode item id (podcast_id:episode_id).
        :param media_type: The media type of the item.
        """
        podcast_id, episode_id = split_episode_item_id(item_id)
        episode = await self._client.get_episode(podcast_id, episode_id)
        if not episode.audio_url:
            raise MediaNotFoundError(f"No audio URL found for episode {item_id}")
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=audio_format(episode.audio_url),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=episode.audio_url,
            duration=episode.duration or None,
            can_seek=True,
            allow_seek=True,
        )

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search the Il Post podcasts by title, author and description.

        :param search_query: The search query.
        :param media_types: The media types to include in the search.
        :param limit: The maximum number of items to return per type.
        """
        results = SearchResults()
        if media_types and MediaType.PODCAST not in media_types:
            return results
        matches = match_podcasts(await self._get_podcasts(), search_query)[:limit]
        results.podcasts = [parse_podcast(p, self.domain, self.instance_id) for p in matches]
        return results

    @use_cache(1800)
    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the blocks of the Il Post podcast home page."""
        return [
            parse_home_block(block, self.domain, self.instance_id)
            for block in await self._client.get_home()
            if block.episodes
        ]

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Return the (fully_played, position_ms, timestamp) resume point stored on Il Post.

        Il Post does not update its timestamp when the position changes, so none is reported.

        :param item_id: The episode item id (podcast_id:episode_id).
        :param media_type: The media type (should be PODCAST_EPISODE).
        """
        _, episode_id = split_episode_item_id(item_id)
        progress = (await self._get_progress([episode_id]) or {}).get(episode_id)
        if progress is None:
            return (False, 0, None)
        if progress.is_completed():
            return (True, 0, None)
        return (False, progress.position * 1000, None)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Store the listening position of an episode on Il Post.

        :param media_type: The media type of the played item.
        :param prov_item_id: The episode item id (podcast_id:episode_id).
        :param fully_played: Whether the episode was played to the end.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: Whether the episode is currently playing.
        """
        if media_type != MediaType.PODCAST_EPISODE or not isinstance(media_item, PodcastEpisode):
            return
        _, episode_id = split_episode_item_id(prov_item_id)
        to_store = position_to_sync(fully_played, position, media_item.duration or 0, is_playing)
        if to_store is None:
            return
        try:
            await self._client.set_position(episode_id, to_store)
        except TRANSIENT_ERRORS as err:
            self.logger.warning(
                "Could not store the position of %s on Il Post: %s", episode_id, err
            )

    @use_cache(3600 * 6)
    async def _get_podcasts(self) -> list[IlPostPodcast]:
        """Return every Il Post podcast."""
        return await self._client.get_podcasts()

    async def _get_progress(self, episode_ids: list[str]) -> dict[str, EpisodeProgress] | None:
        """Return the stored progress per episode id, None when it cannot be read."""
        try:
            return await self._client.get_progress(episode_ids)
        except TRANSIENT_ERRORS as err:
            self.logger.warning("Could not read the listening progress from Il Post: %s", err)
            return None
