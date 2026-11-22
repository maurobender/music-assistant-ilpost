"""
Typed models of the Il Post API responses.

The API is unofficial and loosely typed (ids come as numbers or strings, durations may be
missing, titles carry HTML entities), so every response is parsed here once. Records without
a valid id are dropped, so the rest of the provider never sees partial data.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from mashumaro import DataClassDictMixin

from .constants import FULLY_PLAYED_THRESHOLD


def parse_id(value: Any) -> str | None:
    """Return an Il Post id as a string of digits, or None when it is not one."""
    text = str(value).strip() if value is not None else ""
    return text if text.isascii() and text.isdigit() else None


def parse_int(value: Any) -> int:
    """Return a non-negative int from a loosely typed field, 0 when it is not a number."""
    try:
        return max(int(float(value)), 0)
    except TypeError, ValueError:
        return 0


def parse_text(value: Any) -> str:
    """Return a text field with its HTML entities decoded."""
    return html.unescape(str(value)).strip() if value else ""


def parse_url(value: Any) -> str | None:
    """Return a URL when it is an absolute http(s) URL, None otherwise."""
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return value.strip()


def parse_datetime(value: Any) -> datetime | None:
    """Return a timestamp from an ISO 8601 string, None when it cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class IlPostPodcast(DataClassDictMixin):
    """
    A podcast (show) published by Il Post.

    Serializable because the provider keeps the podcast listing in the Music Assistant cache.
    """

    id: str
    title: str
    author: str = ""
    description: str = ""
    image: str | None = None
    share_url: str | None = None
    episode_count: int = 0

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> IlPostPodcast | None:
        """Parse a podcast from the API, None when it has no valid id."""
        if (podcast_id := parse_id(data.get("id"))) is None:
            return None
        return cls(
            id=podcast_id,
            title=parse_text(data.get("title")),
            author=parse_text(data.get("author")),
            description=parse_text(data.get("description")),
            image=parse_url(data.get("image")),
            share_url=parse_url(data.get("share_url")),
            episode_count=parse_int(data.get("count")),
        )


@dataclass(frozen=True, slots=True)
class IlPostEpisode:
    """An episode of an Il Post podcast."""

    id: str
    podcast_id: str
    title: str
    podcast_title: str = ""
    description: str | None = None
    image: str | None = None
    audio_url: str | None = None
    published: datetime | None = None
    duration: int = 0
    share_url: str | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> IlPostEpisode | None:
        """Parse an episode from the API, None when it lacks a valid episode or podcast id."""
        parent = data.get("parent")
        parent = parent if isinstance(parent, Mapping) else {}
        episode_id = parse_id(data.get("id"))
        podcast_id = parse_id(parent.get("id"))
        if episode_id is None or podcast_id is None:
            return None
        milliseconds = parse_int(data.get("milliseconds"))
        return cls(
            id=episode_id,
            podcast_id=podcast_id,
            title=parse_text(data.get("title")),
            podcast_title=parse_text(parent.get("title")),
            # kept as HTML, the way podcast show notes are passed to Music Assistant
            description=str(data.get("content_html") or data.get("summary") or "") or None,
            image=parse_url(data.get("image")) or parse_url(parent.get("image")),
            audio_url=parse_url(data.get("episode_raw_url")),
            published=parse_datetime(data.get("date")),
            duration=milliseconds // 1000 if milliseconds else parse_int(data.get("minutes")) * 60,
            share_url=parse_url(data.get("share_url")),
        )


@dataclass(frozen=True, slots=True)
class EpisodeProgress:
    """The listening position Il Post stores for an episode, in seconds."""

    position: int
    duration: int = 0

    def is_completed(self, fallback_duration: int = 0) -> bool:
        """Return whether the episode was listened to the end."""
        duration = self.duration or fallback_duration
        return duration > 0 and self.position >= duration * FULLY_PLAYED_THRESHOLD

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> tuple[str, EpisodeProgress] | None:
        """Parse a stored state entry into (episode id, progress), None when it is invalid."""
        item = data.get("item")
        if not isinstance(item, Mapping) or (episode_id := parse_id(item.get("id"))) is None:
            return None
        state = item.get("data")
        if not isinstance(state, Mapping):
            return None
        return episode_id, cls(
            position=parse_int(state.get("position")),
            duration=parse_int(state.get("duration")),
        )


@dataclass(frozen=True, slots=True)
class HomeBlock:
    """A block (featured, new this week, ...) of the Il Post podcast home page."""

    key: str
    label: str | None
    episodes: tuple[IlPostEpisode, ...]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> HomeBlock | None:
        """Parse a home page block, None when it has no key."""
        if not (key := str(data.get("key") or "")):
            return None
        meta = data.get("meta")
        meta = meta if isinstance(meta, Mapping) else {}
        label = parse_text(meta.get("label")) if meta.get("show_label") else ""
        entries = data.get("data")
        episodes = tuple(
            episode
            for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, Mapping)
            and entry.get("object") == "episode"
            and (episode := IlPostEpisode.from_api(entry)) is not None
        )
        return cls(key=key, label=label or None, episodes=episodes)
