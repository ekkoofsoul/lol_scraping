import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from playwright.async_api import async_playwright, Response


# ----------------------------
# Riot Data Dragon: champion list
# ----------------------------

async def fetch_latest_ddragon_version(session: aiohttp.ClientSession) -> str:
    async with session.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
        r.raise_for_status()
        versions = await r.json()
        return versions[0]

async def fetch_all_champions(session: aiohttp.ClientSession) -> List[str]:
    """
    Returns champion slugs used by Lolalytics URLs (usually lowercase, no spaces/apostrophes).
    We'll start from Riot's official IDs, then normalize to lolalytics-style.
    """
    version = await fetch_latest_ddragon_version(session)
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    async with session.get(url) as r:
        r.raise_for_status()
        data = await r.json()

    # Riot "id" is already the canonical key (e.g., "AurelionSol", "KhaZix")
    ids = sorted(data["data"].keys())

    def to_lolalytics_slug(riot_id: str) -> str:
        # Lolalytics uses lowercase and usually removes punctuation.
        # Commonly works: AurelionSol -> aurelionsol, KhaZix -> khazix
        return re.sub(r"[^a-z0-9]", "", riot_id.lower())

    return [to_lolalytics_slug(x) for x in ids]


# ----------------------------
# Lolalytics JSON capture & parsing (same idea as earlier)
# ----------------------------

@dataclass
class DuoStat:
    champion: str
    winrate: Optional[float] = None
    games: Optional[int] = None
    delta1: Optional[float] = None
    delta2: Optional[float] = None

def _try_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _try_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None

def _walk(obj: Any):
    stack = [([], obj)]
    while stack:
        path, cur = stack.pop()
        yield path, cur
        if isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((path + [k], v))
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                stack.append((path + [i], v))

def _looks_like_row(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    keys = {str(k).lower() for k in item.keys()}
    has_name = any(k in keys for k in ["champion", "champ", "name", "target", "ally", "enemy"])
    has_wr = any(k in keys for k in ["winrate", "wr", "win"])
    return has_name and has_wr

def _extract_rows_from_json(payload: Any) -> List[dict]:
    candidates = []
    for _, v in _walk(payload):
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            if sum(1 for x in v if _looks_like_row(x)) >= max(1, len(v) // 3):
                candidates.append(v)
    candidates.sort(key=len, reverse=True)
    return candidates[0] if candidates else []

def _row_to_duostat(row: dict) -> DuoStat:
    lower = {str(k).lower(): k for k in row.keys()}

    def get(*names):
        for n in names:
            if n in lower:
                return row[lower[n]]
        return None

    champ = get("champion", "champ", "name", "target", "ally", "enemy")
    if isinstance(champ, dict):
        champ = champ.get("name") or champ.get("champion") or champ.get("key") or str(champ)
    champ = str(champ) if champ is not None else "UNKNOWN"

    win = get("winrate", "wr", "win")
    games = get("games", "count", "n", "matches")
    d1 = get("delta1", "delta_1", "d1")
    d2 = get("delta2", "delta_2", "d2")

    return DuoStat(
        champion=champ,
        winrate=_try_float(win),
        games=_try_int(games),
        delta1=_try_float(d1),
        delta2=_try_float(d2),
    )

async def scrape_one(page, champion: str, role: str, region: str, tier: str, patch: Optional[str], top_n: int) -> Dict[str, Any]:
    base = f"https://lolalytics.com/lol/{champion}/build/"
    params = []
    if tier: params.append(f"tier={tier}")
    if region: params.append(f"region={region}")
    if patch: params.append(f"patch={patch}")
    if role: params.append(f"lane={role}")
    url = base + ("?" + "&".join(params) if params else "")

    captured_json: List[Tuple[str, Any]] = []
    debug_responses: List[Dict[str, Any]] = []

    # capture ANY likely XHR/fetch responses, even if content-type isn't application/json
    def looks_like_api(u: str) -> bool:
        u = u.lower()
        return any(k in u for k in [
            "/api/", "graphql", "matchup", "counter", "synergy", "duo", "pair"
        ])

    async def on_response(resp: Response):
        try:
            u = resp.url
            status = resp.status
            ct = (resp.headers.get("content-type") or "").lower()

            # keep a small debug trail
            if len(debug_responses) < 50:
                debug_responses.append({"url": u, "status": status, "content_type": ct})

            if status != 200:
                return

            if not looks_like_api(u):
                # Still allow actual JSON content-types even if URL doesn't look like API
                if "json" not in ct:
                    return

            # Try resp.json() first, else parse text as JSON
            data = None
            try:
                data = await resp.json()
            except Exception:
                try:
                    txt = await resp.text()
                except Exception:
                    return
                data = _safe_json_from_text(txt)

            if data is not None:
                captured_json.append((u, data))

        except Exception:
            pass

    page.on("response", on_response)

    # Load page
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # Nudge lazy-loading
    await page.mouse.wheel(0, 3500)
    await page.wait_for_timeout(1200)
    await page.mouse.wheel(0, 3500)
    await page.wait_for_timeout(1200)

    # --- Post-process captured JSON (reuse your existing _extract_rows_from_json etc.) ---
    synergy_rows: List[dict] = []
    counter_rows: List[dict] = []

    for u, payload in captured_json:
        u_low = u.lower()
        payload_str = ""
        try:
            payload_str = json.dumps(payload).lower()
        except Exception:
            payload_str = ""

        is_synergy = any(k in u_low for k in ["synergy", "duo", "pair"]) or any(k in payload_str for k in ["synergy", "duo", "pair", "ally"])
        is_counter = any(k in u_low for k in ["counter", "matchup", "vs"]) or any(k in payload_str for k in ["counter", "matchup", "enemy", "weak"])

        rows = _extract_rows_from_json(payload)
        if not rows:
            continue

        if is_synergy and not synergy_rows:
            synergy_rows = rows
        if is_counter and not counter_rows:
            counter_rows = rows

    synergies = [_row_to_duostat(r) for r in synergy_rows]
    weak_against = [_row_to_duostat(r) for r in counter_rows]

    synergies.sort(key=lambda s: (s.winrate is not None, s.winrate), reverse=True)
    weak_against.sort(key=lambda s: (s.winrate is not None, s.winrate), reverse=True)

    def norm(xs):
        out = []
        for s in xs[:top_n]:
            out.append({
                "champion": s.champion,
                "winrate": s.winrate,
                "games": s.games,
                "delta1": s.delta1,
                "delta2": s.delta2,
            })
        return out

    return {
        "champion": champion,
        "role": role,
        "region": region,
        "tier": tier,
        "patch": patch,
        "source_url": url,
        "synergies_top": norm(synergies),
        "weak_against_top": norm(weak_against),
        "captured_json_responses": len(captured_json),
        # helpful when it still fails:
        "debug_first_responses": debug_responses[:10],
    }


# ----------------------------
# Cache I/O (jsonl + index)
# ----------------------------

def load_done_keys(index_path: str) -> set:
    if not os.path.exists(index_path):
        return set()
    with open(index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    return set(idx.get("done_keys", []))

def save_done_keys(index_path: str, done_keys: set):
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"done_keys": sorted(done_keys)}, f)
    os.replace(tmp, index_path)

def append_jsonl(path: str, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ----------------------------
# Main build loop
# ----------------------------

async def build_cache(
    out_jsonl: str = "lolalytics_cache.jsonl",
    out_index: str = "lolalytics_cache_index.json",
    roles: List[str] = None,
    region: str = "all",
    tier: str = "emerald_plus",
    patch: Optional[str] = None,
    top_n: int = 15,
    concurrency_pages: int = 2,
):
    roles = roles or ["top", "jungle", "middle", "bottom", "support"]

    async with aiohttp.ClientSession() as session:
        champs = await fetch_all_champions(session)

    done = load_done_keys(out_index)

    # Keep a modest pace by adding jitter between tasks per worker
    sem = asyncio.Semaphore(concurrency_pages)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        async def worker(champion: str, role: str):
            key = f"{champion}|{role}|{region}|{tier}|{patch or ''}"
            if key in done:
                return

            async with sem:
                page = await context.new_page()
                try:
                    data = await scrape_one(page, champion, role, region, tier, patch, top_n)
                    append_jsonl(out_jsonl, data)
                    done.add(key)
                    save_done_keys(out_index, done)
                except Exception as e:
                    # record an error row so you can see what failed
                    append_jsonl(out_jsonl, {
                        "champion": champion,
                        "role": role,
                        "region": region,
                        "tier": tier,
                        "patch": patch,
                        "error": repr(e),
                    })
                finally:
                    await page.close()

            # jitter to reduce block risk
            await asyncio.sleep(0.5 + random.random() * 0.8)

        tasks = []
        for c in champs:
            for r in roles:
                tasks.append(asyncio.create_task(worker(c, r)))

        await asyncio.gather(*tasks)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(build_cache())

