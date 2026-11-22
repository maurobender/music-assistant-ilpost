"""Pure helpers of the Il Post provider: search matching and progress sync decisions."""

from __future__ import annotations

from collections.abc import Sequence

from .constants import FULLY_PLAYED_THRESHOLD
from .models import IlPostPodcast


def match_podcasts(podcasts: Sequence[IlPostPodcast], query: str) -> list[IlPostPodcast]:
    """
    Return the podcasts matching a search query, title matches first.

    :param podcasts: The podcasts to search.
    :param query: The text to look for in the title, author or description.
    """
    needle = query.casefold().strip()
    if not needle:
        return []
    by_title = [p for p in podcasts if needle in p.title.casefold()]
    by_other = [
        p
        for p in podcasts
        if p not in by_title
        and (needle in p.author.casefold() or needle in p.description.casefold())
    ]
    return by_title + by_other


def position_to_sync(
    fully_played: bool, position: int, duration: int, is_playing: bool
) -> int | None:
    """
    Return the position to store on Il Post for a playback report, None to store nothing.

    :param fully_played: Whether Music Assistant reports the episode as played to the end.
    :param position: The reported position in seconds.
    :param duration: The episode duration in seconds, 0 when unknown.
    :param is_playing: Whether the episode is still playing.
    """
    # Music Assistant also reports fully_played when an episode is skipped or stopped, so
    # only a position near the end counts. Il Post has no played flag: a listened episode
    # is stored with its position at the end.
    if fully_played and duration > 0 and position >= duration * FULLY_PLAYED_THRESHOLD:
        return duration
    if position <= 0:
        # a report at 0 while stopped means the user marked the episode as unplayed
        return None if is_playing else 0
    return position
