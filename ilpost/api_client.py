"""Client for the (unofficial) Il Post podcast API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

from .constants import (
    API_HOST,
    API_KEY,
    EPISODES_PAGE_SIZE,
    HOME_PATH,
    LOGIN_PATH,
    MAX_PAGES,
    PODCAST_PATH,
    PODCASTS_PAGE_SIZE,
    STATE_BATCH_SIZE,
    STATE_PATH,
    TOKEN_LIFETIME,
)
from .models import EpisodeProgress, HomeBlock, IlPostEpisode, IlPostPodcast, parse_id

if TYPE_CHECKING:
    import logging

type JsonObject = dict[str, Any]


class _TokenRejectedError(Exception):
    """The API rejected the session token."""


def _raise_if_unavailable(response: aiohttp.ClientResponse) -> None:
    """Raise the retryable error for a rate-limited or failing response."""
    backoff = parse_retry_after(response.headers.get("Retry-After"))
    if response.status == 429:
        raise RateLimited("Il Post is rate limiting requests", backoff_time=backoff)
    if response.status >= 500:
        raise ResourceTemporarilyUnavailable(
            f"Il Post temporarily unavailable ({response.status})", backoff_time=backoff
        )


def _require_id(value: str) -> str:
    """Return an id that is safe to put in a URL path, or raise MediaNotFoundError."""
    if (valid := parse_id(value)) is None:
        raise MediaNotFoundError(f"Invalid Il Post id: {value!r}")
    return valid


class IlPostClient:
    """Client for the Il Post podcast API."""

    throttler = ThrottlerManager(rate_limit=5, period=1)

    def __init__(
        self,
        session: aiohttp.ClientSession,
        logger: logging.Logger,
        email: str,
        password: str,
        api_host: str = API_HOST,
    ) -> None:
        """
        Initialize the client.

        :param session: The aiohttp session to use, normally mass.http_session.
        :param logger: The provider logger.
        :param email: The Il Post account email address.
        :param password: The Il Post account password.
        :param api_host: The API base URL, overridable for tests.
        """
        self.session = session
        self.logger = logger
        self._email = email
        self._password = password
        self._api_host = api_host.rstrip("/")
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()

    async def login(self) -> None:
        """Log in, raising LoginFailed when the credentials are rejected."""
        await self._get_token(force=True)

    async def get_podcasts(self) -> list[IlPostPodcast]:
        """Return every podcast published by Il Post."""
        items = await self._get_paged(f"{PODCAST_PATH}/", PODCASTS_PAGE_SIZE)
        return [p for item in items if (p := IlPostPodcast.from_api(item)) is not None]

    async def get_episodes(self, podcast_id: str) -> list[IlPostEpisode]:
        """
        Return every episode of a podcast, newest first.

        :param podcast_id: The Il Post podcast id.
        """
        path = f"{PODCAST_PATH}/{_require_id(podcast_id)}/"
        items = await self._get_paged(path, EPISODES_PAGE_SIZE)
        return [e for item in items if (e := IlPostEpisode.from_api(item)) is not None]

    async def get_episode(self, podcast_id: str, episode_id: str) -> IlPostEpisode:
        """
        Return a single episode.

        :param podcast_id: The Il Post podcast id.
        :param episode_id: The Il Post episode id.
        """
        path = f"{PODCAST_PATH}/{_require_id(podcast_id)}/{_require_id(episode_id)}/"
        data = (await self._request("GET", path)).get("data")
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, Mapping) or (episode := IlPostEpisode.from_api(data)) is None:
            raise MediaNotFoundError(f"Il Post episode {episode_id} not found")
        return episode

    async def get_home(self) -> list[HomeBlock]:
        """Return the blocks of the podcast home page."""
        data = (await self._request("GET", HOME_PATH)).get("data")
        return [
            block
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, Mapping) and (block := HomeBlock.from_api(item)) is not None
        ]

    async def get_progress(self, episode_ids: list[str]) -> dict[str, EpisodeProgress]:
        """
        Return the stored listening progress per episode id.

        Episodes without stored progress are left out.

        :param episode_ids: The Il Post episode ids to look up.
        """
        ids = [int(_require_id(episode_id)) for episode_id in episode_ids]
        progress: dict[str, EpisodeProgress] = {}
        for start in range(0, len(ids), STATE_BATCH_SIZE):
            body = await self._request(
                "POST", STATE_PATH, json={"items": ids[start : start + STATE_BATCH_SIZE]}
            )
            entries = body.get("data")
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, Mapping) and (parsed := EpisodeProgress.from_api(entry)):
                    progress[parsed[0]] = parsed[1]
        return progress

    async def set_position(self, episode_id: str, position: int) -> None:
        """
        Store the listening position of an episode.

        :param episode_id: The Il Post episode id.
        :param position: The position in seconds.
        """
        await self._request(
            "POST",
            f"{STATE_PATH}/{_require_id(episode_id)}",
            json={"data": {"position": max(int(position), 0)}},
        )

    async def _get_paged(self, path: str, page_size: int) -> list[JsonObject]:
        """Return every item of a paged listing."""
        items: list[JsonObject] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._request("GET", path, params={"pg": page, "hits": page_size})
            data = body.get("data")
            batch = [
                item for item in (data if isinstance(data, list) else []) if isinstance(item, dict)
            ]
            items.extend(batch)
            head = body.get("head")
            head_data = head.get("data") if isinstance(head, Mapping) else None
            total = head_data.get("total") if isinstance(head_data, Mapping) else None
            if isinstance(total, int):
                if not batch or len(items) >= total:
                    return items
            elif len(batch) < page_size:
                return items
        self.logger.warning("Stopped reading %s after %d pages", path, MAX_PAGES)
        return items

    async def _get_token(self, force: bool = False, rejected: str | None = None) -> str:
        """
        Return a valid session token, logging in when there is none.

        :param force: Log in even when the current token looks valid.
        :param rejected: A token the API rejected; a newer one is reused instead of logging in.
        """
        async with self._login_lock:
            token_is_fresh = self._token is not None and time.monotonic() < self._token_expires
            if self._token and token_is_fresh and not force and self._token != rejected:
                return self._token
            self._token = await self._login()
            self._token_expires = time.monotonic() + TOKEN_LIFETIME
            return self._token

    async def _login(self) -> str:
        """Log in and return the session token."""
        url = f"{self._api_host}{LOGIN_PATH}"
        credentials = {"username": self._email, "password": self._password}
        try:
            async with self.session.post(
                url, data=credentials, headers={"Apikey": API_KEY}
            ) as response:
                _raise_if_unavailable(response)
                try:
                    body = await response.json(loads=json_loads, content_type=None)
                except ValueError as err:
                    raise ProviderUnavailableError(
                        f"Il Post login returned an invalid response ({response.status})"
                    ) from err
        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting Il Post: {err}"
            ) from err
        outer = body.get("data") if isinstance(body, Mapping) else None
        user = outer.get("data") if isinstance(outer, Mapping) else None
        token = user.get("token") if isinstance(user, Mapping) else None
        if not isinstance(token, str) or not token:
            raise LoginFailed("Il Post login failed, check the email and password")
        self.logger.debug("Logged in to Il Post")
        return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> JsonObject:
        """Perform an authenticated request, logging in again once if the token is rejected."""
        token = await self._get_token()
        try:
            return await self._send(method, path, token, **kwargs)
        except _TokenRejectedError:
            self.logger.debug("Il Post rejected the session token, logging in again")
        token = await self._get_token(rejected=token)
        try:
            return await self._send(method, path, token, **kwargs)
        except _TokenRejectedError as err:
            raise LoginFailed("Il Post rejected a freshly issued session token") from err

    @throttle_with_retries
    async def _send(self, /, method: str, path: str, token: str, **kwargs: Any) -> JsonObject:
        """Perform a single request and return its JSON body."""
        url = f"{self._api_host}{path}"
        headers = {"Apikey": API_KEY, "Token": token}
        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as response:
                if response.status in (401, 403):
                    raise _TokenRejectedError
                if response.status == 404:
                    raise MediaNotFoundError(f"Il Post resource not found: {path}")
                _raise_if_unavailable(response)
                if response.status != 200:
                    raise ProviderUnavailableError(
                        f"Il Post request to {path} failed ({response.status})"
                    )
                try:
                    body = await response.json(loads=json_loads, content_type=None)
                except ValueError as err:
                    raise ProviderUnavailableError(
                        f"Il Post returned invalid JSON for {path}"
                    ) from err
        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting Il Post: {err}"
            ) from err
        return body if isinstance(body, dict) else {}
