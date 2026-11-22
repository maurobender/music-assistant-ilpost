"""Exercise the Il Post provider against the real API, without a running Music Assistant.

Needs a Music Assistant server checkout with its virtualenv, and the provider linked in:

    ln -s "$PWD/ilpost" <ma-server>/music_assistant/providers/ilpost
    <ma-server>/.venv/bin/python scripts/test_provider.py

Reads ILPOST_EMAIL / ILPOST_PASSWORD from .env. Nothing is written to the Il Post account:
position writes from on_played are captured instead of sent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.ilpost import IlPostProvider
from music_assistant.providers.ilpost.api_client import IlPostClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_api import load_env  # noqa: E402

MORNING_ID = "227474"  # a podcast with > 1000 episodes, exercises paging


class NoCache:
    """Cache stand-in: always a miss, never stores."""

    async def get_with_freshness(self, *args: Any, **kwargs: Any) -> tuple[None, bool, bool]:
        return None, False, False

    async def set(self, *args: Any, **kwargs: Any) -> None:
        return None


def make_provider(session: aiohttp.ClientSession, email: str, password: str) -> IlPostProvider:
    prov = IlPostProvider.__new__(IlPostProvider)
    def create_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.ensure_future(coro)

    prov.mass = SimpleNamespace(  # type: ignore[assignment]
        cache=NoCache(), http_session=session, create_task=create_task
    )
    prov.manifest = SimpleNamespace(domain="ilpost")  # type: ignore[assignment]
    prov.config = SimpleNamespace(instance_id="ilpost--test", name=None)  # type: ignore[assignment]
    prov.logger = logging.getLogger("ilpost")
    prov._client = IlPostClient(session, prov.logger, email, password)
    return prov


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    email, password = load_env()
    async with aiohttp.ClientSession() as session:
        print("== Login")
        bad = IlPostClient(session, logging.getLogger("bad"), email, "wrong-password")
        try:
            await bad.login()
            check("wrong password is rejected", False)
        except LoginFailed:
            check("wrong password is rejected", True)
        prov = make_provider(session, email, password)
        await prov._client.login()
        check("login", True, f"subscriber={prov._client.is_subscriber}")

        print("\n== Library")
        podcasts = [p async for p in prov.get_library_podcasts()]
        check("library podcasts", len(podcasts) > 50, f"{len(podcasts)} podcasts")
        first = podcasts[0]
        print(f"     e.g. {first.item_id} {first.name!r} by {first.publisher}, "
              f"{first.total_episodes} eps, image={bool(first.metadata.images)}")
        morning = await prov.get_podcast(MORNING_ID)
        check("get_podcast", morning.item_id == MORNING_ID, morning.name)

        print("\n== Episodes (paging + progress)")
        episodes = [e async for e in prov.get_podcast_episodes(MORNING_ID)]
        check("all episodes fetched", len(episodes) >= (morning.total_episodes or 0) - 5,
              f"{len(episodes)} of {morning.total_episodes}")
        newest = max(episodes, key=lambda e: e.position)
        check("newest episode ranks highest", newest is episodes[0], newest.name)
        played = [e for e in episodes if e.fully_played]
        started = [e for e in episodes if not e.fully_played and e.resume_position_ms]
        print(f"     listened={len(played)} in progress={len(started)}")
        sample = (started or played or episodes)[0]
        print(f"     sample {sample.item_id} {sample.name!r} dur={sample.duration}s "
              f"played={sample.fully_played} resume={sample.resume_position_ms}ms "
              f"date={sample.metadata.release_date}")

        print("\n== Single episode + stream")
        ep = await prov.get_podcast_episode(sample.item_id)
        check("get_podcast_episode", ep.item_id == sample.item_id and ep.duration > 0,
              f"{ep.name!r} podcast={ep.podcast.name!r}")  # type: ignore[union-attr]
        stream = await prov.get_stream_details(ep.item_id, MediaType.PODCAST_EPISODE)
        async with session.get(stream.path, headers={"Range": "bytes=0-1023"}) as resp:
            check("stream URL plays without auth", resp.status == 206
                  and resp.headers.get("Content-Type") == "audio/mpeg",
                  f"{resp.status} {resp.headers.get('Content-Type')} {stream.audio_format.content_type}")
        resume = await prov.get_resume_position(ep.item_id, MediaType.PODCAST_EPISODE)
        check("get_resume_position matches listing",
              resume[0] == ep.fully_played and resume[1] == ep.resume_position_ms, str(resume))

        print("\n== Search")
        for query in ("morning", "baricco", "zzzz-nothing"):
            res = await prov.search(query, [MediaType.PODCAST])
            print(f"     {query!r}: {[p.name for p in res.podcasts]}")

        print("\n== Recommendations (home)")
        folders = await prov.get_recommendations()
        for folder in folders:
            print(f"     {folder.item_id}: {folder.name!r} ({len(folder.items)} episodes)")
        check("home blocks", len(folders) >= 3)

        print("\n== on_played logic (writes captured, not sent)")
        writes: list[tuple[str, int]] = []

        async def capture(episode_id: str, position: int) -> None:
            writes.append((episode_id, position))

        prov._client.set_position = capture  # type: ignore[method-assign]
        ep_id = ep.item_id.split(":")[1]
        dur = ep.duration
        await prov.on_played(MediaType.PODCAST_EPISODE, ep.item_id, False, 120, ep, True)
        await prov.on_played(MediaType.PODCAST_EPISODE, ep.item_id, True, dur - 5, ep, False)
        await prov.on_played(MediaType.PODCAST_EPISODE, ep.item_id, True, 300, ep, False)
        await prov.on_played(MediaType.PODCAST_EPISODE, ep.item_id, False, 0, ep, False)
        await prov.on_played(MediaType.PODCAST_EPISODE, ep.item_id, False, 0, ep, True)
        expected = [(ep_id, 120), (ep_id, dur), (ep_id, 300), (ep_id, 0)]
        check("progress / end / skip / mark unplayed / start", writes == expected, str(writes))

        print("\n== Expired token")
        prov._client._token = "expired-token"
        again = await prov.get_podcast(MORNING_ID)
        check("re-login after 401", again.item_id == MORNING_ID)


if __name__ == "__main__":
    asyncio.run(main())
