"""API client for the (unofficial) Il Post podcast API."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    import logging

API_HOST = "https://api-prod.ilpost.it"
LOGIN_URL = f"{API_HOST}/user/v1/auth/login"
PODCAST_URL = f"{API_HOST}/podcast/v1/podcast"
HOME_URL = f"{API_HOST}/podcast/v1/bff/hp"
STATE_URL = f"{API_HOST}/user/v1/state/episode"

# a fixed key the Il Post web and mobile apps send with every call
API_KEY = "testapikey"
# the token carries no expiry, the apps treat it as valid for about two hours
TOKEN_LIFETIME = 3600 * 2 - 300
EPISODES_PAGE_SIZE = 500
PODCASTS_PAGE_SIZE = 200
STATE_BATCH_SIZE = 200


class _TokenExpiredError(Exception):
    """Raised internally when the API rejects the current token."""


class IlPostClient:
    """Client for the Il Post podcast API."""

    throttler = ThrottlerManager(rate_limit=5, period=1)

    def __init__(
        self,
        session: aiohttp.ClientSession,
        logger: logging.Logger,
        email: str,
        password: str,
    ) -> None:
        """
        Initialize the client.

        :param session: The aiohttp session to use for requests (typically mass.http_session).
        :param logger: The provider logger.
        :param email: The Il Post account email address.
        :param password: The Il Post account password.
        """
        self.session = session
        self.logger = logger
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()
        self.is_subscriber = False

    async def login(self) -> None:
        """Authenticate with Il Post and store the session token."""
        async with self._login_lock:
            await self._login()

    async def _login(self) -> None:
        try:
            async with self.session.post(
                LOGIN_URL,
                data={"username": self._email, "password": self._password},
                headers={"Apikey": API_KEY},
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise ResourceTemporarilyUnavailable(
                        f"Il Post login temporarily unavailable ({response.status})",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                body = await response.json(loads=json_loads, content_type=None)
        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting Il Post: {err}"
            ) from err
        user = ((body or {}).get("data") or {}).get("data") or {}
        token = user.get("token") if isinstance(user, dict) else None
        if not token:
            raise LoginFailed("Il Post login failed, check the email and password")
        self._token = str(token)
        self._token_expires = time.monotonic() + TOKEN_LIFETIME
        self.is_subscriber = bool(user.get("issubscriber"))
        self.logger.debug("Logged in to Il Post (subscriber: %s)", self.is_subscriber)

    async def get_podcasts(self) -> list[dict[str, Any]]:
        """Return all podcasts published by Il Post."""
        return await self._get_paged(f"{PODCAST_URL}/", PODCASTS_PAGE_SIZE)

    async def get_episodes(self, podcast_id: str) -> list[dict[str, Any]]:
        """
        Return all episodes of a podcast, newest first.

        :param podcast_id: The Il Post podcast id.
        """
        return await self._get_paged(f"{PODCAST_URL}/{podcast_id}/", EPISODES_PAGE_SIZE)

    async def get_episode(self, podcast_id: str, episode_id: str) -> dict[str, Any]:
        """
        Return a single episode.

        :param podcast_id: The Il Post podcast id.
        :param episode_id: The Il Post episode id.
        """
        body = await self._request("GET", f"{PODCAST_URL}/{podcast_id}/{episode_id}/")
        data = body.get("data")
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or not data.get("id"):
            raise MediaNotFoundError(f"Il Post episode {episode_id} not found")
        return data

    async def get_home(self) -> list[dict[str, Any]]:
        """Return the blocks (featured, new this week, ...) of the podcast home page."""
        body = await self._request("GET", HOME_URL)
        return [block for block in body.get("data") or [] if isinstance(block, dict)]

    async def get_positions(self, episode_ids: list[str]) -> dict[str, tuple[int, int]]:
        """
        Return the stored (position, duration) in seconds per episode id.

        Episodes without a stored position are left out.

        :param episode_ids: The Il Post episode ids to look up.
        """
        result: dict[str, tuple[int, int]] = {}
        for start in range(0, len(episode_ids), STATE_BATCH_SIZE):
            batch = [int(ep_id) for ep_id in episode_ids[start : start + STATE_BATCH_SIZE]]
            body = await self._request("POST", STATE_URL, json={"items": batch})
            for entry in body.get("data") or []:
                item = (entry or {}).get("item") or {}
                state = item.get("data") or {}
                if item.get("id") is None or not isinstance(state, dict):
                    continue
                result[str(item["id"])] = (
                    int(state.get("position") or 0),
                    int(state.get("duration") or 0),
                )
        return result

    async def set_position(self, episode_id: str, position: int) -> None:
        """
        Store the listening position of an episode on the user's account.

        :param episode_id: The Il Post episode id.
        :param position: The position in seconds.
        """
        await self._request(
            "POST", f"{STATE_URL}/{episode_id}", json={"data": {"position": int(position)}}
        )

    async def _get_paged(self, url: str, page_size: int) -> list[dict[str, Any]]:
        """Return every item of a paged listing endpoint."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._request("GET", url, params={"pg": page, "hits": page_size})
            batch = [item for item in body.get("data") or [] if isinstance(item, dict)]
            items.extend(batch)
            total = (((body.get("head") or {}).get("data")) or {}).get("total") or 0
            if not batch or len(items) >= int(total):
                return items
            page += 1

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Perform an authenticated request, logging in again once when the token expired."""
        if not self._token or time.monotonic() > self._token_expires:
            await self.login()
        try:
            return await self._throttled_request(method, url, **kwargs)
        except _TokenExpiredError:
            self.logger.debug("Il Post token rejected, logging in again")
            await self.login()
        try:
            return await self._throttled_request(method, url, **kwargs)
        except _TokenExpiredError as err:
            raise LoginFailed("Il Post rejected the session token after a fresh login") from err

    @throttle_with_retries
    async def _throttled_request(self, /, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Perform a single request and return the decoded JSON body."""
        headers = {"Apikey": API_KEY, "Token": self._token or ""}
        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as response:
                if response.status in (401, 403):
                    raise _TokenExpiredError
                if response.status == 404:
                    raise MediaNotFoundError(f"Il Post resource not found: {url}")
                if response.status == 429 or response.status >= 500:
                    raise ResourceTemporarilyUnavailable(
                        f"Il Post temporarily unavailable ({response.status})",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status != 200:
                    text = await response.text()
                    raise ProviderUnavailableError(
                        f"Il Post request to {url} failed ({response.status}): {text[:200]}"
                    )
                body = await response.json(loads=json_loads, content_type=None)
        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting Il Post: {err}"
            ) from err
        return cast("dict[str, Any]", body) if isinstance(body, dict) else {}
