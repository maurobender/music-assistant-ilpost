"""
Run the Il Post provider against the real API, without a running Music Assistant.

Needs the development environment from scripts/dev_setup.sh. Reads ILPOST_EMAIL and
ILPOST_PASSWORD from .env. Nothing is written to the account: position writes are captured.

    ../music-assistant-server/.venv/bin/python scripts/live_check.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.ilpost import IlPostProvider
from music_assistant.providers.ilpost.api_client import IlPostClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_api import load_env

MORNING_ID = "227474"  # more than 1000 episodes, so it exercises paging


def make_provider(session: aiohttp.ClientSession, email: str, password: str) -> IlPostProvider:
    """Return a provider with a real API client and a cache that always misses."""
    mass = AsyncMock()
    mass.http_session = session
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.create_task = MagicMock(side_effect=lambda coro, **_: asyncio.ensure_future(coro))
    manifest = MagicMock()
    manifest.domain = "ilpost"
    config = MagicMock()
    config.instance_id = "ilpost"
    config.get_value.return_value = None
    provider = IlPostProvider(mass, manifest, config)
    provider._client = IlPostClient(session, logging.getLogger("ilpost"), email, password)
    return provider


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Print a check result and return it."""
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")
    return ok


async def run(session: aiohttp.ClientSession, email: str, password: str) -> bool:
    """Run every check and return whether all passed."""
    results: list[bool] = []
    print("== Login")
    try:
        await IlPostClient(session, logging.getLogger("bad"), email, "wrong-password").login()
        results.append(check("wrong password is rejected", False))
    except LoginFailed:
        results.append(check("wrong password is rejected", True))
    provider = make_provider(session, email, password)
    await provider._client.login()

    print("\n== Library")
    podcasts = [p async for p in provider.get_library_podcasts()]
    results.append(check("library podcasts", len(podcasts) > 50, f"{len(podcasts)} podcasts"))
    morning = await provider.get_podcast(MORNING_ID)
    results.append(check("get_podcast", morning.item_id == MORNING_ID, morning.name))

    print("\n== Episodes")
    episodes = [e async for e in provider.get_podcast_episodes(MORNING_ID)]
    expected = (morning.total_episodes or 0) - 10
    results.append(check("all pages read", len(episodes) >= expected, f"{len(episodes)}"))
    played = sum(1 for e in episodes if e.fully_played)
    started = [e for e in episodes if not e.fully_played and e.resume_position_ms]
    print(f"     listened={played} in progress={len(started)}")
    sample = (started or episodes)[0]

    print("\n== Single episode and stream")
    episode = await provider.get_podcast_episode(sample.item_id)
    results.append(check("get_podcast_episode", episode.duration > 0, episode.name))
    stream = await provider.get_stream_details(episode.item_id, MediaType.PODCAST_EPISODE)
    async with session.get(str(stream.path), headers={"Range": "bytes=0-1023"}) as resp:
        playable = resp.status == 206 and resp.headers.get("Content-Type") == "audio/mpeg"
        results.append(check("stream plays without auth", playable, str(resp.status)))
    resume = await provider.get_resume_position(episode.item_id, MediaType.PODCAST_EPISODE)
    matches = resume[:2] == (bool(episode.fully_played), episode.resume_position_ms)
    results.append(check("resume position matches the listing", matches, str(resume)))

    print("\n== Search and home")
    found = await provider.search("baricco", [MediaType.PODCAST])
    results.append(check("search", bool(found.podcasts), str([p.name for p in found.podcasts])))
    folders = await provider.get_recommendations()
    names = [f"{f.name} ({len(f.items)})" for f in folders]
    results.append(check("home blocks", len(folders) >= 3, ", ".join(names)))

    print("\n== Progress sync (captured, not sent)")
    writes: list[tuple[str, int]] = []

    async def capture(episode_id: str, position: int) -> None:
        writes.append((episode_id, position))

    provider._client.set_position = capture  # type: ignore[method-assign]
    await provider.on_played(MediaType.PODCAST_EPISODE, episode.item_id, False, 120, episode, True)
    episode_id = episode.item_id.split(":")[1]
    results.append(check("on_played", writes == [(episode_id, 120)], str(writes)))

    print("\n== Expired token")
    provider._client._token = "expired-token"
    again = await provider.get_podcast_episode(episode.item_id)
    results.append(check("logs in again after a 401", again.item_id == episode.item_id))
    return all(results)


async def main() -> int:
    """Run the live checks."""
    logging.basicConfig(level=logging.WARNING)
    email, password = load_env()
    async with aiohttp.ClientSession() as session:
        return 0 if await run(session, email, password) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
