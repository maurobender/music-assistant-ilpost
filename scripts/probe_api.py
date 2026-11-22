# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Manual probe of the (unofficial) Il Post podcast API.

Reads ILPOST_EMAIL / ILPOST_PASSWORD from the repo's .env, logs in, lists podcasts
and episodes, prints a playable audio URL and checks whether that URL needs auth.

Run with:  uv run scripts/probe_api.py
Raw JSON responses are saved in scripts/output/ (gitignored) for inspection.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "output"
LOGIN_URL = "https://api-prod.ilpost.it/user/v1/auth/login"
BASE = "https://api-prod.ilpost.it/podcast/v1/podcast"
HOME_URL = "https://api-prod.ilpost.it/podcast/v1/bff/hp"
APIKEY = "testapikey"
UA = "ilpost-ma-probe/0.1"


def load_env() -> tuple[str, str]:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    email = os.environ.get("ILPOST_EMAIL")
    password = os.environ.get("ILPOST_PASSWORD")
    if not email or not password:
        sys.exit("Missing ILPOST_EMAIL / ILPOST_PASSWORD (put them in .env)")
    return email, password


def dump(name: str, data: Any) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))


async def get_json(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str], **kw: Any
) -> tuple[int, Any]:
    for attempt in range(5):
        async with session.get(url, headers=headers, **kw) as resp:
            if resp.status == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"  429 on {url}, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            try:
                body = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                body = await resp.text()
            return resp.status, body
    raise RuntimeError(f"Too many 429s for {url}")


async def probe_audio(session: aiohttp.ClientSession, url: str, headers: dict[str, str], label: str) -> int:
    hdrs = {"User-Agent": UA, "Range": "bytes=0-2047", **headers}
    async with session.get(url, headers=hdrs, allow_redirects=True) as resp:
        chunk = await resp.content.read(2048)
        hist = " -> ".join(str(h.url) for h in resp.history)
        print(f"  [{label}] HTTP {resp.status}  type={resp.headers.get('Content-Type')}  "
              f"len={resp.headers.get('Content-Length')}  range={resp.headers.get('Content-Range')}")
        if hist:
            print(f"    redirects: {hist} -> {resp.url}")
        magic = chunk[:3]
        kind = "ID3/mp3" if magic == b"ID3" or chunk[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") else repr(chunk[:16])
        print(f"    first bytes: {kind}")
        return resp.status


def summarize_keys(obj: Any) -> str:
    return ", ".join(sorted(obj.keys())) if isinstance(obj, dict) else type(obj).__name__


async def main() -> None:
    email, password = load_env()
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
        # 1. login
        print("== Login")
        async with session.post(LOGIN_URL, data={"username": email, "password": password},
                                headers={"Apikey": APIKEY}) as resp:
            login = await resp.json(content_type=None)
            print(f"  HTTP {resp.status}")
        token = (login.get("data") or {}).get("data", {}).get("token") if isinstance(login, dict) else None
        if not token:
            dump("login_error", login)
            sys.exit("  Login failed, see scripts/output/login_error.json")
        user_data = login["data"]["data"]
        print(f"  token OK (len {len(token)}); login data keys: {summarize_keys(user_data)}")
        auth = {"Apikey": APIKEY, "Token": token}
        anon = {"Apikey": APIKEY}

        # 2. podcasts
        print("\n== Podcasts")
        status, pods = await get_json(session, f"{BASE}/", auth, params={"pg": 1, "hits": 200})
        dump("podcasts", pods)
        plist = pods.get("data", []) if isinstance(pods, dict) else []
        print(f"  HTTP {status}, {len(plist)} podcasts; head: {json.dumps(pods.get('head'))[:200]}")
        if plist:
            print(f"  podcast keys: {summarize_keys(plist[0])}")
        for p in plist[:8]:
            print(f"   - {p.get('id')}: {p.get('title')}")
        if not plist:
            sys.exit("  No podcasts returned")

        # 3. episodes of first podcast
        pod = plist[0]
        print(f"\n== Episodes of '{pod.get('title')}' ({pod.get('id')})")
        status, eps = await get_json(session, f"{BASE}/{pod['id']}/", auth, params={"pg": 1, "hits": 5})
        dump("episodes", eps)
        elist = eps.get("data", [])
        total = ((eps.get("head") or {}).get("data") or {}).get("total")
        print(f"  HTTP {status}, total={total}, got {len(elist)}")
        if not elist:
            sys.exit("  No episodes returned")
        ep = elist[0]
        print(f"  episode keys: {summarize_keys(ep)}")
        for e in elist:
            print(f"   - {e.get('id')}: {e.get('date')}  {e.get('title')}  ({e.get('milliseconds')} ms)")

        # 4. single episode + by-ids
        print("\n== Single episode / by ids")
        status, one = await get_json(session, f"{BASE}/{pod['id']}/{ep['id']}/", auth)
        dump("episode", one)
        print(f"  single: HTTP {status}, type={type(one.get('data')).__name__}")
        ids = ",".join(str(e["id"]) for e in elist[:3])
        status, many = await get_json(session, f"{BASE}/episodes", auth, params={"ids": ids})
        dump("episodes_by_ids", many)
        print(f"  by ids ({ids}): HTTP {status}, n={len(many.get('data') or []) if isinstance(many, dict) else '?'}")

        # 5. same list without token: is the audio URL still there?
        print("\n== Episodes without token")
        status, eps_anon = await get_json(session, f"{BASE}/{pod['id']}/", anon, params={"pg": 1, "hits": 5})
        dump("episodes_anon", eps_anon)
        anon_list = eps_anon.get("data", []) if isinstance(eps_anon, dict) else []
        print(f"  HTTP {status}, got {len(anon_list)}")
        for a, b in zip(anon_list, elist):
            print(f"   - {b['id']}: raw_url with token={'yes' if b.get('episode_raw_url') else 'no'}, "
                  f"without={'yes' if a.get('episode_raw_url') else 'no'}, same={a.get('episode_raw_url') == b.get('episode_raw_url')}")

        # 6. audio URL
        print("\n== Audio URL")
        url = ep.get("episode_raw_url")
        print(f"  episode_raw_url: {url}")
        if url:
            parsed = urlparse(url)
            q = parse_qs(parsed.query)
            print(f"  host={parsed.netloc}  query params: {list(q) or 'none'}")
            await probe_audio(session, url, {}, "no headers")
            await probe_audio(session, url, auth, "with Apikey+Token")

        # 7. home
        print("\n== Home (bff/hp)")
        status, home = await get_json(session, HOME_URL, auth)
        dump("home", home)
        data = home.get("data") if isinstance(home, dict) else None
        print(f"  HTTP {status}, data type={type(data).__name__}")
        if isinstance(data, list):
            for block in data[:10]:
                print(f"   - {summarize_keys(block)[:150]}")
        elif isinstance(data, dict):
            print(f"   keys: {summarize_keys(data)}")


if __name__ == "__main__":
    asyncio.run(main())
