"""Constants for the Il Post provider."""

from __future__ import annotations

from typing import Final

API_HOST: Final = "https://api-prod.ilpost.it"
LOGIN_PATH: Final = "/user/v1/auth/login"
PODCAST_PATH: Final = "/podcast/v1/podcast"
HOME_PATH: Final = "/podcast/v1/bff/hp"
STATE_PATH: Final = "/user/v1/state/episode"

# fixed key that the Il Post web and mobile apps send with every request
API_KEY: Final = "testapikey"
# the token has no expiry of its own; the apps refresh it after about two hours
TOKEN_LIFETIME: Final = 2 * 3600 - 300

PODCASTS_PAGE_SIZE: Final = 200
EPISODES_PAGE_SIZE: Final = 500
# stops a listing that never ends; 50 pages of 500 episodes is far more than any podcast has
MAX_PAGES: Final = 50
STATE_BATCH_SIZE: Final = 200

# Il Post has no "played" flag: an episode counts as listened once this share of it is played
FULLY_PLAYED_THRESHOLD: Final = 0.95

# names for the home page blocks that come without a label of their own
HOME_BLOCK_NAMES: Final[dict[str, str]] = {
    "podcasts_evidenza": "In evidenza",
    "all_new_episodes_week": "Nuovi episodi",
    "episode_featured": "Da non perdere",
    "all_podcasts": "Ultimi episodi",
    "archivio": "Archivio",
}
