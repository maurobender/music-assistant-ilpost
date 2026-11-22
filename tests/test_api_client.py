"""Tests for the Il Post API client against a fake Il Post server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import BaseTestServer, TestServer
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    RetriesExhausted,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.ilpost.api_client import IlPostClient
from music_assistant.providers.ilpost.constants import API_KEY

from . import payloads

PASSWORD = "secret-password"


@dataclass
class FakeIlPost:
    """State of the fake Il Post server, which tests adjust and inspect."""

    episodes: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    ignore_page: bool = False
    states: list[dict[str, Any]] = field(default_factory=list)
    tokens_issued: int = 0
    valid_tokens: set[str] = field(default_factory=set)
    login_status: int = 200
    login_body: str | None = None
    failures: list[int] = field(default_factory=list)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    login_forms: list[dict[str, str]] = field(default_factory=list)

    def expire_tokens(self) -> None:
        """Make the server reject every token issued so far."""
        self.valid_tokens.clear()

    async def login(self, request: web.Request) -> web.Response:
        """Handle the login endpoint."""
        form = await request.post()
        self.login_forms.append({k: str(v) for k, v in form.items()})
        if self.login_body is not None:
            return web.Response(status=self.login_status, text=self.login_body)
        if form.get("password") != PASSWORD:
            return web.json_response({"data": {"data": {}}}, status=401)
        self.tokens_issued += 1
        token = f"token-{self.tokens_issued}"
        self.valid_tokens.add(token)
        return web.json_response(payloads.login_ok(token))

    async def api(self, request: web.Request) -> web.Response:
        """Handle every authenticated endpoint."""
        body: dict[str, Any] = await request.json() if request.can_read_body else {}
        self.requests.append((request.method, request.path_qs, body))
        if request.headers.get("Apikey") != API_KEY:
            return web.Response(status=400)
        if request.headers.get("Token") not in self.valid_tokens:
            return web.json_response({"data": {}}, status=401)
        if self.failures:
            return web.Response(status=self.failures.pop(0), headers={"Retry-After": "0"})
        path = request.path
        if path == "/podcast/v1/podcast/":
            return web.json_response(payloads.listing([payloads.podcast(), {"id": "bad"}], 2))
        if path == "/podcast/v1/bff/hp":
            block = {"key": "archivio", "meta": {}, "data": [payloads.episode()]}
            return web.json_response({"data": [block, {"meta": {}}]})
        if path == "/user/v1/state/episode":
            wanted = set(body["items"])
            return web.json_response(
                {"data": [s for s in self.states if s["item"]["id"] in wanted]}
            )
        if path.startswith("/user/v1/state/episode/"):
            return web.json_response({"data": {"message": "updated"}})
        if path == f"/podcast/v1/podcast/{payloads.PODCAST_ID}/":
            page = 1 if self.ignore_page else int(request.query["pg"])
            hits = int(request.query["hits"])
            batch = self.episodes[(page - 1) * hits : page * hits]
            return web.json_response(payloads.listing(batch, self.total or len(self.episodes)))
        if path.startswith(f"/podcast/v1/podcast/{payloads.PODCAST_ID}/"):
            episode_id = int(path.rstrip("/").rsplit("/", 1)[1])
            match = [e for e in self.episodes if e["id"] == episode_id]
            return web.json_response({"data": match[0]} if match else {"data": []})
        return web.Response(status=404)


@pytest.fixture(autouse=True)
def fast_throttler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so retry tests stay fast."""
    monkeypatch.setattr(
        IlPostClient,
        "throttler",
        ThrottlerManager(rate_limit=1000, period=1, retry_attempts=3, initial_backoff=0),
    )


@pytest.fixture
def fake() -> FakeIlPost:
    """Return the state of the fake Il Post server."""
    return FakeIlPost(episodes=[payloads.episode(episode_id=i) for i in range(1, 4)])


@pytest.fixture
async def server(fake: FakeIlPost) -> AsyncIterator[BaseTestServer]:
    """Run the fake Il Post server."""
    app = web.Application()
    app.router.add_post("/user/v1/auth/login", fake.login)
    app.router.add_route("*", "/{tail:.*}", fake.api)
    async with TestServer(app) as test_server:
        yield test_server


@pytest.fixture
async def make_client(server: BaseTestServer) -> AsyncIterator[Callable[..., IlPostClient]]:
    """Return a factory of clients connected to the fake server."""
    async with aiohttp.ClientSession() as session:

        def _make(password: str = PASSWORD) -> IlPostClient:
            return IlPostClient(
                session,
                logging.getLogger("test"),
                "user@example.com",
                password,
                api_host=str(server.make_url("")),
            )

        yield _make


async def test_login_sends_the_credentials_as_a_form(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """Login posts the email as username and the password."""
    await make_client().login()
    assert fake.login_forms == [{"username": "user@example.com", "password": PASSWORD}]


async def test_wrong_password_raises_login_failed(
    make_client: Callable[..., IlPostClient],
) -> None:
    """Rejected credentials raise LoginFailed."""
    with pytest.raises(LoginFailed):
        await make_client(password="wrong").login()


async def test_login_with_invalid_json(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """An HTML error page from the login endpoint is reported, not crashed on."""
    fake.login_body = "<html>maintenance</html>"
    with pytest.raises(ProviderUnavailableError):
        await make_client().login()


async def test_podcasts_skip_invalid_records(make_client: Callable[..., IlPostClient]) -> None:
    """Podcasts without a valid id are left out of the listing."""
    podcasts = await make_client().get_podcasts()
    assert [p.id for p in podcasts] == [str(payloads.PODCAST_ID)]


async def test_home_blocks(make_client: Callable[..., IlPostClient]) -> None:
    """Home blocks without a key are left out."""
    blocks = await make_client().get_home()
    assert [(b.key, len(b.episodes)) for b in blocks] == [("archivio", 1)]


async def test_episodes_are_read_across_pages(
    make_client: Callable[..., IlPostClient],
    fake: FakeIlPost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every page of a listing is read until the reported total."""
    monkeypatch.setattr("music_assistant.providers.ilpost.api_client.EPISODES_PAGE_SIZE", 2)
    fake.episodes = [payloads.episode(episode_id=i) for i in range(1, 6)]
    episodes = await make_client().get_episodes(str(payloads.PODCAST_ID))
    assert [e.id for e in episodes] == ["1", "2", "3", "4", "5"]
    assert len(fake.requests) == 3


async def test_listing_that_never_ends_is_capped(
    make_client: Callable[..., IlPostClient],
    fake: FakeIlPost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that ignores the page number cannot make the client loop forever."""
    monkeypatch.setattr("music_assistant.providers.ilpost.api_client.EPISODES_PAGE_SIZE", 2)
    monkeypatch.setattr("music_assistant.providers.ilpost.api_client.MAX_PAGES", 4)
    fake.ignore_page = True
    fake.total = 1000
    episodes = await make_client().get_episodes(str(payloads.PODCAST_ID))
    assert len(fake.requests) == 4
    assert len(episodes) == 8


async def test_expired_token_logs_in_again_once(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """A rejected token triggers one new login and the request is retried."""
    client = make_client()
    await client.get_episode(str(payloads.PODCAST_ID), "1")
    fake.expire_tokens()
    episode = await client.get_episode(str(payloads.PODCAST_ID), "2")
    assert episode.id == "2"
    assert fake.tokens_issued == 2


async def test_concurrent_rejections_share_one_login(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """Requests rejected at the same time reuse the token of the first new login."""
    client = make_client()
    await client.login()
    fake.expire_tokens()
    await asyncio.gather(*(client.get_episode(str(payloads.PODCAST_ID), str(i)) for i in (1, 2, 3)))
    assert fake.tokens_issued == 2


async def test_server_errors_are_retried(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """Rate limiting and server errors are retried."""
    fake.failures = [429, 503]
    episode = await make_client().get_episode(str(payloads.PODCAST_ID), "1")
    assert episode.id == "1"


async def test_persistent_server_errors_give_up(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """Retries stop after the configured attempts."""
    fake.failures = [503] * 10
    with pytest.raises(RetriesExhausted):
        await make_client().get_episode(str(payloads.PODCAST_ID), "1")


async def test_missing_episode(make_client: Callable[..., IlPostClient]) -> None:
    """An episode the API does not return raises MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await make_client().get_episode(str(payloads.PODCAST_ID), "999")


@pytest.mark.parametrize("bad_id", ["../../user/v1/auth/me", "1/2", "1?x=y", "abc", ""])
async def test_ids_cannot_escape_the_url_path(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost, bad_id: str
) -> None:
    """Ids that are not plain digits are rejected before any request is made."""
    client = make_client()
    with pytest.raises(MediaNotFoundError):
        await client.get_episode(str(payloads.PODCAST_ID), bad_id)
    with pytest.raises(MediaNotFoundError):
        await client.set_position(bad_id, 10)
    assert fake.requests == []


async def test_progress_is_read_in_batches(
    make_client: Callable[..., IlPostClient],
    fake: FakeIlPost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress is looked up in batches and only stored entries are returned."""
    monkeypatch.setattr("music_assistant.providers.ilpost.api_client.STATE_BATCH_SIZE", 2)
    fake.states = [payloads.state(1, 100, 1800), payloads.state(3, 1790, 1800)]
    progress = await make_client().get_progress(["1", "2", "3"])
    assert {k: v.position for k, v in progress.items()} == {"1": 100, "3": 1790}
    assert [body for _, _, body in fake.requests] == [{"items": [1, 2]}, {"items": [3]}]


async def test_set_position_posts_seconds(
    make_client: Callable[..., IlPostClient], fake: FakeIlPost
) -> None:
    """A position is stored as whole seconds on the episode's state."""
    await make_client().set_position("7", 42)
    assert fake.requests == [("POST", "/user/v1/state/episode/7", {"data": {"position": 42}})]
