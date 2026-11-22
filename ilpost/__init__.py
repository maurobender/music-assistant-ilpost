"""Il Post podcasts provider for Music Assistant."""

from __future__ import annotations

import contextlib
import html
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
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
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import from_iso_string
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider

from .api_client import IlPostClient

if TYPE_CHECKING:
    from datetime import datetime

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# share of an episode that counts as listened, matching how the Il Post apps show progress
FULLY_PLAYED_THRESHOLD = 0.95
TRANSIENT_ERRORS = (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
# names for the home page blocks whose own label is empty
HOME_BLOCK_NAMES = {
    "podcasts_evidenza": "In evidenza",
    "all_new_episodes_week": "Nuovi episodi",
    "episode_featured": "Da non perdere",
    "all_podcasts": "Ultimi episodi",
    "archivio": "Archivio",
}

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
        """Get all podcasts published by Il Post."""
        for podcast_data in await self._get_all_podcasts():
            yield self._convert_podcast(podcast_data)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get full details for a podcast.

        :param prov_podcast_id: The Il Post podcast id.
        """
        # there is no single-podcast endpoint, the full listing is one cached call
        for podcast_data in await self._get_all_podcasts():
            if str(podcast_data.get("id")) == prov_podcast_id:
                return self._convert_podcast(podcast_data)
        raise MediaNotFoundError(f"podcast://{prov_podcast_id} not found on provider {self.domain}")

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes of a podcast, with the listening progress stored on Il Post.

        :param prov_podcast_id: The Il Post podcast id.
        """
        episodes = [ep for ep in await self._client.get_episodes(prov_podcast_id) if ep.get("id")]
        positions = await self._get_positions([str(ep["id"]) for ep in episodes])
        ranks = rank_episodes_by_date([ep.get("date") or None for ep in episodes])
        for rank, episode_data in zip(ranks, episodes, strict=True):
            episode = self._convert_episode(episode_data, position=rank)
            if positions is not None:
                self._apply_progress(episode, positions.get(str(episode_data["id"])))
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get full details for a podcast episode.

        :param prov_episode_id: The episode item id (format: podcast_id:episode_id).
        """
        podcast_id, episode_id = self._split_episode_id(prov_episode_id)
        episode_data = await self._client.get_episode(podcast_id, episode_id)
        episode = self._convert_episode(episode_data)
        positions = await self._get_positions([episode_id])
        if positions is not None:
            self._apply_progress(episode, positions.get(episode_id))
        return episode

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get streamable URL and details for the given episode.

        The audio is a plain MP3 file that needs no authentication.

        :param item_id: The episode item id (format: podcast_id:episode_id).
        :param media_type: The media type of the item.
        """
        podcast_id, episode_id = self._split_episode_id(item_id)
        episode_data = await self._client.get_episode(podcast_id, episode_id)
        url = episode_data.get("episode_raw_url")
        if not url:
            raise MediaNotFoundError(f"No audio URL found for episode {item_id}")
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(content_type=ContentType.try_parse(str(url))),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=str(url),
            duration=self._duration(episode_data),
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
        query = search_query.casefold().strip()
        if not query:
            return results
        podcasts = await self._get_all_podcasts()
        # title matches first, then author or description matches
        ranked = sorted(
            (
                (0 if query in self._text(p.get("title")).casefold() else 1, idx, p)
                for idx, p in enumerate(podcasts)
                if any(
                    query in self._text(p.get(field)).casefold()
                    for field in ("title", "author", "description")
                )
            ),
            key=lambda entry: (entry[0], entry[1]),
        )
        results.podcasts = [self._convert_podcast(p) for _, _, p in ranked[:limit]]
        return results

    @use_cache(1800)
    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the blocks of the Il Post podcast home page (featured, new episodes, ...)."""
        folders: list[RecommendationFolder] = []
        for block in await self._client.get_home():
            key = str(block.get("key") or "")
            items: list[MediaItemType] = [
                self._convert_episode(entry)
                for entry in block.get("data") or []
                if isinstance(entry, dict) and entry.get("object") == "episode" and entry.get("id")
            ]
            if not key or not items:
                continue
            meta = block.get("meta") or {}
            name = (meta.get("label") if meta.get("show_label") else None) or HOME_BLOCK_NAMES.get(
                key, key.replace("_", " ").capitalize()
            )
            folders.append(
                RecommendationFolder(
                    item_id=key,
                    provider=self.instance_id,
                    name=name,
                    items=UniqueList(items),
                )
            )
        return folders

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Return the (fully_played, position_ms, timestamp) resume point stored on Il Post.

        Il Post keeps a timestamp but does not update it when the position changes, so it is
        not reported.

        :param item_id: The episode item id (format: podcast_id:episode_id).
        :param media_type: The media type (should be PODCAST_EPISODE).
        """
        _, episode_id = self._split_episode_id(item_id)
        positions = await self._get_positions([episode_id])
        if not positions or episode_id not in positions:
            return (False, 0, None)
        position, duration = positions[episode_id]
        if self._is_completed(position, duration):
            return (True, 0, None)
        return (False, position * 1000, None)

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
        Sync the listening position of an episode back to Il Post.

        :param media_type: The media type of the played item.
        :param prov_item_id: The episode item id (format: podcast_id:episode_id).
        :param fully_played: Whether the episode was played to the end.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: Whether the episode is currently playing.
        """
        if media_type != MediaType.PODCAST_EPISODE or not isinstance(media_item, PodcastEpisode):
            return
        _, episode_id = self._split_episode_id(prov_item_id)
        duration = media_item.duration or 0
        # MA also reports fully_played when an episode is skipped or stopped, so check the
        # position before storing it as listened
        if fully_played and duration > 0 and position >= duration * FULLY_PLAYED_THRESHOLD:
            # Il Post has no played flag, an episode counts as listened when the
            # position reaches the end
            position = duration
        elif position <= 0 and is_playing:
            return
        try:
            await self._client.set_position(episode_id, max(position, 0))
        except TRANSIENT_ERRORS as err:
            self.logger.warning("Could not sync the position of %s to Il Post: %s", episode_id, err)

    @use_cache(3600 * 6)
    async def _get_all_podcasts(self) -> list[dict[str, Any]]:
        """Return the raw data of every Il Post podcast."""
        return await self._client.get_podcasts()

    async def _get_positions(self, episode_ids: list[str]) -> dict[str, tuple[int, int]] | None:
        """Return the stored positions per episode id, or None when they cannot be read."""
        # progress is supplementary, a failure here must not break browsing or playback
        try:
            return await self._client.get_positions(episode_ids)
        except TRANSIENT_ERRORS as err:
            self.logger.warning("Could not read the listening progress from Il Post: %s", err)
            return None

    @staticmethod
    def _is_completed(position: int, duration: int) -> bool:
        """Return whether a stored position means the episode was listened to the end."""
        return duration > 0 and position >= duration * FULLY_PLAYED_THRESHOLD

    def _apply_progress(self, episode: PodcastEpisode, progress: tuple[int, int] | None) -> None:
        """
        Set the played state and resume point of an episode from its stored progress.

        Both fields are always set so a library sync can clear stale values.

        :param episode: The episode to update in place.
        :param progress: The stored (position, duration) in seconds, None when there is none.
        """
        position, duration = progress or (0, 0)
        duration = duration or episode.duration or 0
        completed = self._is_completed(position, duration)
        episode.fully_played = completed
        episode.resume_position_ms = 0 if completed else position * 1000

    @staticmethod
    def _split_episode_id(item_id: str) -> tuple[str, str]:
        """Split an episode item id into the podcast id and the episode id."""
        podcast_id, sep, episode_id = item_id.partition(":")
        if not sep or not podcast_id or not episode_id:
            raise MediaNotFoundError(f"Invalid Il Post episode id: {item_id}")
        return podcast_id, episode_id

    @staticmethod
    def _text(value: Any) -> str:
        """Return a plain string from an API text field, with HTML entities decoded."""
        return html.unescape(str(value)).strip() if value else ""

    @staticmethod
    def _duration(episode_data: dict[str, Any]) -> int:
        """Return the duration of an episode in seconds, 0 when unknown."""
        with contextlib.suppress(TypeError, ValueError):
            if milliseconds := int(episode_data.get("milliseconds") or 0):
                return milliseconds // 1000
            return int(episode_data.get("minutes") or 0) * 60
        return 0

    def _image(self, url: Any) -> UniqueList[MediaItemImage]:
        """Return the image list for an image URL, empty when there is none."""
        if not url:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=str(url),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    def _convert_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """
        Convert raw Il Post podcast data to a Podcast.

        :param podcast_data: Raw podcast data from the podcast listing.
        """
        podcast_id = str(podcast_data["id"])
        podcast = Podcast(
            item_id=podcast_id,
            provider=self.instance_id,
            name=self._text(podcast_data.get("title")),
            publisher=self._text(podcast_data.get("author")) or "Il Post",
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=podcast_data.get("share_url") or None,
                )
            },
            metadata=MediaItemMetadata(
                description=self._text(podcast_data.get("description")) or None,
                images=self._image(podcast_data.get("image")),
            ),
        )
        with contextlib.suppress(TypeError, ValueError):
            podcast.total_episodes = int(podcast_data.get("count") or 0) or None
        return podcast

    def _convert_episode(self, episode_data: dict[str, Any], position: int = 0) -> PodcastEpisode:
        """
        Convert raw Il Post episode data to a PodcastEpisode.

        :param episode_data: Raw episode data from any episode endpoint.
        :param position: The episode's listing position, 0 when unknown.
        """
        parent = episode_data.get("parent") or {}
        podcast_id = str(parent.get("id") or "")
        item_id = f"{podcast_id}:{episode_data['id']}"
        url = episode_data.get("episode_raw_url") or ""
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=self._text(episode_data.get("title")) or "Episode",
            podcast=ItemMapping(
                media_type=MediaType.PODCAST,
                item_id=podcast_id,
                provider=self.instance_id,
                name=self._text(parent.get("title")),
            ),
            position=position,
            duration=self._duration(episode_data),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=bool(url),
                    audio_format=AudioFormat(content_type=ContentType.try_parse(url or "mp3")),
                    url=episode_data.get("share_url") or None,
                )
            },
        )
        description = episode_data.get("content_html") or episode_data.get("summary")
        if description:
            episode.metadata.description = str(description)
        episode.metadata.images = self._image(episode_data.get("image") or parent.get("image"))
        if date := episode_data.get("date"):
            with contextlib.suppress(ValueError, TypeError):
                episode.metadata.release_date = from_iso_string(date)
        return episode
