"""Synthetic Il Post API payloads, shaped like the real responses."""

from __future__ import annotations

from typing import Any

PODCAST_ID = 1001
EPISODE_ID = 5001


def podcast(podcast_id: int = PODCAST_ID, **overrides: Any) -> dict[str, Any]:
    """Return a podcast as the podcast listing returns it."""
    return {
        "id": podcast_id,
        "title": "Podcast &#8211; One",
        "author": "Jane Doe",
        "description": "A podcast about things",
        "image": "https://example.com/cover.jpg",
        "share_url": "https://example.com/podcasts/one/",
        "count": 3,
        "access_level": "all",
        **overrides,
    }


def episode(
    episode_id: int = EPISODE_ID, podcast_id: int = PODCAST_ID, **overrides: Any
) -> dict[str, Any]:
    """Return an episode as the episode endpoints return it."""
    return {
        "id": episode_id,
        "title": f"Episode {episode_id}",
        "content_html": "<p>Show notes</p>",
        "image": "https://example.com/episode.jpg",
        "date": "2026-11-20T08:00:00+01:00",
        "milliseconds": 1_800_000,
        "minutes": 30,
        "episode_raw_url": f"https://example.com/audio/{episode_id}.mp3",
        "share_url": f"https://example.com/episodes/{episode_id}/",
        "object": "episode",
        "transcript": False,
        "parent": {"id": podcast_id, "title": "Podcast One", "image": "https://example.com/p.jpg"},
        **overrides,
    }


def state(episode_id: int, position: int, duration: int) -> dict[str, Any]:
    """Return a stored progress entry as the state endpoint returns it."""
    return {
        "id": f"1_{episode_id}",
        "object": "episode",
        "user_id": 1,
        "item": {
            "id": episode_id,
            "data": {"timestamp": 1_700_000_000, "position": position, "duration": duration},
        },
    }


def listing(items: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    """Wrap items the way the paged listings do."""
    return {"head": {"data": {"total": len(items) if total is None else total}}, "data": items}


def login_ok(token: str = "token-1") -> dict[str, Any]:
    """Return a successful login response."""
    return {"data": {"data": {"token": token, "issubscriber": 1}}}
