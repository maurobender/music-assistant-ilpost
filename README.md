# Il Post for Music Assistant

A [Music Assistant](https://music-assistant.io) provider for the podcasts of
[Il Post](https://www.ilpost.it/podcasts/), using your Il Post account.

- **Library**: every Il Post podcast (about 80) with all of its episodes.
- **Playback**: episodes are plain MP3 files (no token needed), with seeking.
- **Progress sync**: listening progress is read from and written back to your Il Post
  account, so you can switch between Music Assistant, the Il Post app and ilpost.it.
- **Search**: podcasts by title, author or description.
- **Recommendations**: the blocks of the Il Post podcast home page ("Nuovi episodi",
  "In evidenza", "Archivio", ...).

The provider uses an unofficial API (the one the Il Post website and apps use), so it may break
if Il Post changes it. Transcripts are not supported: the API only says whether an episode has
one, not its text.

## Layout

```
ilpost/             the provider, i.e. music_assistant/providers/ilpost in the server
  __init__.py       provider: library, episodes, stream, search, home, progress sync
  api_client.py     HTTP client: login, token refresh, retries, paging, id validation
  models.py         typed models of the API responses, parsed defensively
  parsers.py        conversion of those models into Music Assistant media items
  helpers.py        search matching and progress sync decisions
  constants.py      endpoints, page sizes and thresholds
  setup_flow.py     setup form (email and password)
  manifest.json, strings.json, icon.svg, icon_monochrome.svg
tests/              unit tests (fake Il Post server, mocked Music Assistant)
scripts/
  dev_setup.sh      prepares the development environment
  check.sh          runs every lint, type check and test
  live_check.py     runs the provider against the real API with your account
  probe_api.py      raw API exploration: login, podcasts, episodes, audio URL
  probe_state.py    raw API exploration of the progress endpoints
```

## Install on Music Assistant running in Docker

The provider needs server 2.10.13 or later (the oldest version tested; it needs the setup flow API,
`setup_flow.py`). On an older image it may not show up or may fail to load.

1. Copy the `ilpost/` folder to the Docker host, for example to
   `/opt/music-assistant/custom_providers/ilpost`.

2. Find where the providers live inside the container (replace `music-assistant` with your
   container name):

   ```bash
   docker exec music-assistant python3 -c "import music_assistant.providers as p; print(p.__path__[0])"
   ```

   It prints something like `/app/venv/lib/python3.14/site-packages/music_assistant/providers`.

3. Mount the folder there as `ilpost`. With docker compose:

   ```yaml
   services:
     music-assistant:
       image: ghcr.io/music-assistant/server:latest
       # ... your existing settings ...
       volumes:
         - ./data:/data
         - /opt/music-assistant/custom_providers/ilpost:/app/venv/lib/python3.14/site-packages/music_assistant/providers/ilpost:ro
   ```

   With `docker run`, add `-v /opt/music-assistant/custom_providers/ilpost:<path from step 2>/ilpost:ro`.

4. Recreate the container so it picks up the volume:

   ```bash
   docker compose up -d --force-recreate music-assistant
   ```

5. In Music Assistant open **Settings → Providers → Add provider → Il Post** and enter your
   Il Post email and password. The podcasts appear after the first library sync.

The mount survives image updates. If the path from step 2 changes (for example with a new
Python version), update the mount.

To see the provider's logs:

```bash
docker logs -f music-assistant 2>&1 | grep -i ilpost
```

For more detail, set the provider's log level to **Debug** in its settings.

## Install on the Home Assistant add-on

The provider works with the stable add-on (server 2.10.13 and later; CI tests it against the
latest release) and with the beta add-on. The add-on runs the same server image as Docker,
but an add-on cannot mount extra folders. The provider has to be copied into the add-on's container, and **the copy is lost
whenever the Supervisor recreates that container**: when the add-on is restarted or updated
from Home Assistant, and when the host reboots. After any of those, repeat steps 3 and 4.
Your provider settings are kept in the meantime; the provider just shows as unavailable until
it is copied back.

1. Install the **Advanced SSH & Web Terminal** add-on (from the community add-ons) and turn
   off its **Protection mode**, so it can run `docker`. Open its terminal.

2. Copy the `ilpost/` folder to `/share/ilpost` on Home Assistant, for example with the Samba
   share add-on or `scp`. `/share` survives reboots and updates, so it is the copy to restore
   from.

3. Copy the provider into the Music Assistant container. The stable add-on's container is
   `addon_d5369777_music_assistant`; the beta one is `addon_d5369777_music_assistant_beta`
   (`docker ps --format '{{.Names}}' | grep music_assistant` lists it):

   ```bash
   C=addon_d5369777_music_assistant
   P=$(docker exec "$C" python3 -c "import music_assistant.providers as p; print(p.__path__[0])")
   docker cp /share/ilpost "$C:$P/ilpost"
   ```

4. Restart the container with `docker restart`, which keeps the copied files. Restarting the
   add-on from the Home Assistant UI would recreate the container and drop them.

   ```bash
   docker restart addon_d5369777_music_assistant
   ```

5. In Music Assistant open **Settings → Providers → Add provider → Il Post** and enter your
   Il Post email and password.

The lasting way to install a custom provider on the add-on is Music Assistant's community
catalog, which installs providers into the add-on's data folder. This repository already has
the layout the catalog expects (the provider in a top-level folder named after its domain);
it needs to be listed in the catalog's registry first.

## Development

Everything runs inside a Music Assistant server checkout, so the provider is linted, typed and
tested against the real server code. [uv](https://docs.astral.sh/uv/) is required.

```bash
scripts/dev_setup.sh
```

This clones the server into `../music-assistant-server` (set `MA_DIR` to use another path),
creates its virtualenv with the lint and test tools, and links `ilpost/` into it as
`music_assistant/providers/ilpost`. The server's `aiolibdatachannel` dependency is skipped
because it does not build on macOS and the provider does not need it.

Then run every check:

```bash
scripts/check.sh
```

It runs, in order:

- ruff lint and format on the whole repository, with a config that mirrors the server's;
- ruff and mypy on the provider with the server's own config (mypy checks the whole server
  package there; only errors in the provider fail the run);
- the server's provider checks (manifest, translatable labels, config entries);
- mypy on the tests and scripts;
- pytest with coverage.

`scripts/check.sh --fast` runs only the lint and format step. To run it before every commit,
together with checks that block committing `.env` files and private keys:

```bash
../music-assistant-server/.venv/bin/pre-commit install
```

### Continuous integration

`.github/workflows/ci.yml` runs `scripts/dev_setup.sh`, `scripts/check.sh` and the pre-commit
hooks on every push to `main` and on every pull request. It tests against two server versions:
the `dev` branch and the latest release (what the Home Assistant add-on runs). A weekly
scheduled run catches server changes that break the provider. To test against a specific
server branch or tag, start the workflow by hand and set `ma_ref`. Locally,
`MA_REF=<branch or tag> scripts/dev_setup.sh` does the same for a new checkout.

The checks against the real API are not part of CI, since they need an Il Post account.

### Checks against the real API

Copy `.env.example` to `.env` and fill in `ILPOST_EMAIL` and `ILPOST_PASSWORD`. `.env` is
git-ignored.

```bash
../music-assistant-server/.venv/bin/python scripts/live_check.py
```

`live_check.py` runs the provider end to end and never writes to your account: position
writes are captured instead of sent. The probe scripts only need uv:

```bash
uv run scripts/probe_api.py
```

```bash
uv run scripts/probe_state.py
```

Their raw responses are saved in `scripts/output/` (git-ignored).
`probe_state.py --write EPISODE_ID SECONDS` stores a position on your account.

## API notes

- Login: `POST https://api-prod.ilpost.it/user/v1/auth/login` with the form fields `username`
  and `password`. Every request sends `Apikey: testapikey` and `Token: <token>`. The token is
  renewed every two hours and after a 401.
- Podcasts and episodes: `/podcast/v1/podcast/...`, paged with `pg` and `hits`. Home page:
  `/podcast/v1/bff/hp`.
- Progress: `POST /user/v1/state/episode` with `{"items": [ids]}` reads positions, and
  `POST /user/v1/state/episode/{id}` with `{"data": {"position": seconds}}` stores one. Reads
  reflect a write only after a few seconds. There is no "played" flag: the provider treats an
  episode as listened from 95% and stores the final position when it ends.
- Audio (`episode_raw_url`) is a public MP3 under `www.ilpost.it/wp-content/uploads/`.

## Security notes

- Credentials are stored by Music Assistant (the password as a secure string) and only sent to
  the Il Post login endpoint. The session token is kept in memory.
- Ids from Music Assistant are validated as plain digits before they go into a URL path, so a
  crafted item id cannot reach other API endpoints with your token.
- Only absolute `http(s)` URLs from the API are used for streams and images.
