import httpx
import os
from urllib.parse import quote, urlparse


BLUESKY_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors"
SERPER_API = "https://google.serper.dev/search"


async def search_bluesky(name: str, limit: int = 3) -> list[dict]:
    """Search Bluesky globally for actors matching name. Returns list of {handle, display_name}."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                BLUESKY_API,
                params={"q": name, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            actors = r.json().get("actors", [])
            return [
                {
                    "handle": a["handle"],
                    "display_name": a.get("displayName") or a["handle"],
                    "url": f"https://bsky.app/profile/{a['handle']}",
                }
                for a in actors[:limit]
            ]
        except Exception:
            return []


async def search_serper(query: str) -> str | None:
    """Run a Google search via Serper and return the top organic URL."""
    api_key = os.environ["SERPER_API_KEY"]
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                SERPER_API,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 3},
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("organic", [])
            if results:
                return results[0].get("link")
        except Exception:
            pass
    return None


async def search_twitter(name: str) -> str | None:
    """Search Google for a Twitter/X profile (profile URLs only, not tweets)."""
    api_key = os.environ["SERPER_API_KEY"]
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                SERPER_API,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": f'"{name}" site:x.com OR site:twitter.com', "num": 5},
                timeout=10,
            )
            r.raise_for_status()
            for result in r.json().get("organic", []):
                url = result.get("link", "")
                parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
                if len(parts) == 1 and not parts[0].startswith(("#", "i", "search")):
                    return url
        except Exception:
            pass
    return None


async def search_instagram(name: str) -> str | None:
    """Search Google for an Instagram profile (profile URLs only, not posts)."""
    api_key = os.environ["SERPER_API_KEY"]
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                SERPER_API,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": f'"{name}" site:instagram.com', "num": 5},
                timeout=10,
            )
            r.raise_for_status()
            for result in r.json().get("organic", []):
                url = result.get("link", "")
                parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
                if len(parts) == 1 and not parts[0].startswith(("p", "reel", "explore", "stories")):
                    return url
        except Exception:
            pass
    return None


async def lookup_all(name: str, enabled_platforms: set | None = None) -> dict:
    """Look up a name on all platforms concurrently, skipping disabled ones."""
    import asyncio
    if enabled_platforms is None:
        enabled_platforms = {"bluesky", "twitter", "instagram"}

    tasks = {}
    if "bluesky" in enabled_platforms:
        tasks["bluesky"] = asyncio.create_task(search_bluesky(name))
    if "twitter" in enabled_platforms:
        tasks["twitter"] = asyncio.create_task(search_twitter(name))
    if "instagram" in enabled_platforms:
        tasks["instagram"] = asyncio.create_task(search_instagram(name))

    results = await asyncio.gather(*tasks.values())
    result_map = dict(zip(tasks.keys(), results))

    return {
        "name": name,
        "bluesky":   result_map.get("bluesky", []),
        "twitter":   result_map.get("twitter", None),
        "instagram": result_map.get("instagram", None),
    }


def twitter_search_url(name: str) -> str:
    return f"https://x.com/search?q={quote(name)}"


def instagram_search_url(name: str) -> str:
    return f"https://www.instagram.com/explore/search/?q={quote(name)}"
