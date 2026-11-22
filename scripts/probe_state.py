# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp>=3.9"]
# ///
"""
Probe Il Post's per-user episode state (listening progress).

Endpoints found in the ilpost.it podcast web app:
  POST {USER}/state/episode            body {"items": [ids]}         -> bulk read
  GET  {USER}/state/episode/{id}                                     -> single read
  POST {USER}/state/episode/{id}       body {"data": {"position": s}} -> write

Default run is read-only. Pass `--write EPISODE_ID SECONDS` to store a position
(this changes the state of your Il Post account).

Run with:  uv run scripts/probe_state.py [--write EPISODE_ID SECONDS]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_api import APIKEY, BASE, LOGIN_URL, UA, dump, load_env

USER = "https://api-prod.ilpost.it/user/v1"


async def call(
    session: aiohttp.ClientSession, method: str, url: str, token: str, body: Any = None
) -> tuple[int, Any]:
    headers = {"Apikey": APIKEY, "Token": token, "Content-Type": "application/json"}
    async with session.request(method, url, headers=headers, json=body) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return resp.status, text


async def main() -> None:
    email, password = load_env()
    write: tuple[int, int] | None = None
    if len(sys.argv) == 4 and sys.argv[1] == "--write":
        write = (int(sys.argv[2]), int(sys.argv[3]))

    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
        async with session.post(
            LOGIN_URL, data={"username": email, "password": password}, headers={"Apikey": APIKEY}
        ) as resp:
            token = (await resp.json(content_type=None))["data"]["data"]["token"]
        print("== Logged in")

        # Recent episodes from the user's most likely listened shows (home: new this week)
        status, home = await call(
            session, "GET", "https://api-prod.ilpost.it/podcast/v1/bff/hp", token
        )
        eps: list[dict[str, Any]] = []
        for block in home["data"]:
            if isinstance(block.get("data"), list):
                eps += [e for e in block["data"] if e.get("object") == "episode"]
        ids = list(dict.fromkeys(e["id"] for e in eps))[:40]
        print(
            f"  {len(ids)} episode ids from home; 'listened' present in home items: "
            f"{sum(1 for e in eps if e.get('listened'))}"
        )

        print("\n== Bulk state (POST /state/episode)")
        status, bulk = await call(session, "POST", f"{USER}/state/episode", token, {"items": ids})
        dump("state_bulk", bulk)
        print(f"  HTTP {status}: {json.dumps(bulk, ensure_ascii=False)[:800]}")

        print("\n== Single state (GET /state/episode/{id})")
        status, one = await call(session, "GET", f"{USER}/state/episode/{ids[0]}", token)
        dump("state_single", one)
        print(f"  {ids[0]}: HTTP {status}: {json.dumps(one, ensure_ascii=False)[:500]}")

        print("\n== Does the podcast API add 'listened' with a token?")
        pid = eps[0]["parent"]["id"]
        status, plist = await call(session, "GET", f"{BASE}/{pid}/?pg=1&hits=5", token)
        print(
            f"  podcast {pid}: listened values = {[e.get('listened') for e in plist.get('data', [])]}"
        )

        if write:
            ep_id, secs = write
            print(f"\n== Write position {secs}s on {ep_id}")
            status, res = await call(
                session,
                "POST",
                f"{USER}/state/episode/{ep_id}",
                token,
                {"data": {"position": secs}},
            )
            print(f"  HTTP {status}: {json.dumps(res, ensure_ascii=False)[:500]}")
            # Reads lag writes by a few seconds; the stored timestamp is not updated.
            await asyncio.sleep(8)
            status, back = await call(session, "GET", f"{USER}/state/episode/{ep_id}", token)
            print(f"  read back: HTTP {status}: {json.dumps(back, ensure_ascii=False)[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
